"""Child process supervision and cleanup.

The launcher owns three long-lived children - the X server, the VNC server and
darktable itself - and systemd owns the launcher. ``KillMode=control-group``,
the default for ``Type=simple``, means a plain ``systemctl stop`` already
reaches every descendant through the cgroup. Two cases it does not cover, and
which this module exists for:

* a previous run that was ``SIGKILL``ed, leaving children reparented to init
  and therefore outside any cgroup we can address;
* a launcher started by hand during a diagnostic, which is in no unit's cgroup
  at all.

Both would leave an X server holding the display socket and a VNC server
holding the socket file, and the next start would fail in a way that looks like
a bug in this application. So the launcher records what it started, and on
startup terminates anything the record still points at.

The record is keyed by PID *and* process start time. A bare PID would be
dangerous: PIDs are reused, and a sweep that fired after a reboot could kill an
unrelated process that happened to inherit the number. The start time makes the
identity check exact.
"""

import errno
import json
import os
import signal
import subprocess
import time

from . import logging_setup

log = logging_setup.get_logger("processes")

# Escalation budget. The systemd unit allows 10 seconds to stop; the launcher
# targets 6 so that systemd's own teardown always has room.
TERM_TIMEOUT = 3.0
KILL_TIMEOUT = 1.0


def _proc_start_time(pid):
    """Return field 22 (starttime) of /proc/<pid>/stat, or None if gone.

    The comm field is wrapped in parentheses and may itself contain spaces and
    parentheses, so the fields after it are found by splitting on the *last*
    closing parenthesis rather than by splitting the whole line.
    """
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
    except OSError:
        return None
    close = data.rfind(")")
    if close < 0:
        return None
    fields = data[close + 2:].split()
    if len(fields) < 20:
        return None
    return fields[19]


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as handle:
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _pid_belongs_to_us(pid, start_time, root):
    """True when the live process at ``pid`` is the one the record described."""
    current = _proc_start_time(pid)
    if current is None or current != start_time:
        return False
    # Second gate: the process must actually be one of ours. This is belt and
    # braces against a PID *and* start-time collision, which is vanishingly
    # unlikely but would be destructive, and it costs nothing.
    return root in _proc_cmdline(pid)


def _proc_state(pid):
    """Return the single-letter state from /proc/<pid>/stat, or None."""
    try:
        with open("/proc/%d/stat" % pid, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
    except OSError:
        return None
    close = data.rfind(")")
    if close < 0:
        return None
    fields = data[close + 2:]
    return fields[:1] or None


def _pid_alive(pid):
    """True when the process exists and has not already exited.

    A zombie counts as gone. ``os.kill(pid, 0)`` succeeds for a process that
    has exited but whose parent has not reaped it yet - and a child this
    launcher started stays unreaped until it is waited on, so every shutdown
    saw its children as alive, waited out the whole SIGTERM allowance, and
    then SIGKILLed processes that had already stopped. Measured: three children
    "ignored SIGTERM" and shutdown took nine seconds against a ten-second
    budget, with the only real work being a 0.3s HTTP close.
    """
    state = _proc_state(pid)
    if state is None:
        return False
    return state != "Z"


def terminate(pid, term_timeout=TERM_TIMEOUT, kill_timeout=KILL_TIMEOUT):
    """Stop a process politely, then not politely. Returns True if it is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return True
        log.warning("cannot signal pid %d: %s", pid, error)
        return False

    deadline = time.time() + term_timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)

    log.warning("pid %d ignored SIGTERM after %.1fs; sending SIGKILL", pid, term_timeout)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as error:
        if error.errno == errno.ESRCH:
            return True
        log.warning("cannot SIGKILL pid %d: %s", pid, error)
        return False

    deadline = time.time() + kill_timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class Child(object):
    """One supervised child process."""

    def __init__(self, name, argv, env, log_file=None, cwd=None):
        self.name = name
        self.argv = argv
        self.env = env
        self.log_file = log_file
        self.cwd = cwd
        self.process = None
        self._handle = None

    def start(self):
        stdout = subprocess.DEVNULL
        stderr = subprocess.DEVNULL

        if self.log_file:
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            # Append, so a crash from a previous run is still readable after
            # the restart that follows it.
            self._handle = open(self.log_file, "ab")
            stdout = self._handle
            stderr = subprocess.STDOUT

        log.info("starting %s: %s", self.name, " ".join(self.argv))
        try:
            self.process = subprocess.Popen(
                self.argv,
                env=self.env,
                cwd=self.cwd,
                stdout=stdout,
                stderr=stderr,
                stdin=subprocess.DEVNULL,
                start_new_session=False,
            )
        except OSError as error:
            log.error("cannot start %s: %s", self.name, error)
            self._close_handle()
            return False
        return True

    def poll(self):
        if self.process is None:
            return None
        return self.process.poll()

    def alive(self):
        return self.poll() is None

    def pid(self):
        return self.process.pid if self.process else None

    def stop(self, term_timeout=TERM_TIMEOUT):
        """Stop the child. Returns True when it is confirmed gone."""
        if self.process is None:
            return True
        if self.process.poll() is not None:
            self._close_handle()
            return True

        pid = self.process.pid
        log.info("stopping %s (pid %d)", self.name, pid)
        gone = terminate(pid, term_timeout=term_timeout)

        # Reap it either way so the process table does not collect a zombie
        # for the remaining life of the launcher.
        try:
            self.process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass
        self._close_handle()
        return gone

    def _close_handle(self):
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


class SessionRecord(object):
    """The list of children a launcher run started, persisted across restarts."""

    def __init__(self, path, root):
        self.path = path
        self.root = root

    def write(self, children):
        entries = []
        for child in children:
            pid = child.pid()
            if pid is None:
                continue
            entries.append(
                {
                    "name": child.name,
                    "pid": pid,
                    "start_time": _proc_start_time(pid),
                }
            )
        payload = {"pid": os.getpid(), "children": entries}
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temporary, self.path)
        except OSError as error:
            log.warning("cannot write %s: %s", self.path, error)

    def read(self):
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def sweep(self):
        """Terminate whatever the previous run left behind.

        Runs before anything is started, so that the display socket and the
        listening port are free by the time they are needed.
        """
        record = self.read()
        if not record:
            return 0

        killed = 0
        for entry in record.get("children", []):
            pid = entry.get("pid")
            if not isinstance(pid, int) or pid <= 1:
                continue
            if not _pid_belongs_to_us(pid, entry.get("start_time"), self.root):
                continue
            log.warning(
                "sweeping orphaned %s from a previous run (pid %d)",
                entry.get("name", "process"),
                pid,
            )
            if terminate(pid):
                killed += 1

        try:
            os.unlink(self.path)
        except OSError:
            pass
        return killed

    def clear(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


class LockFile(object):
    """An exclusive lock so a second instance fails fast.

    systemd will not start a second copy of the unit, but an operator running
    the launcher by hand during a diagnostic easily would - and two launchers
    fighting over the display number and the listening port produce a
    confusing partial failure rather than a clear refusal.
    """

    def __init__(self, path):
        self.path = path
        self._handle = None

    def acquire(self):
        import fcntl

        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._handle = open(self.path, "a+")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._handle.close()
            self._handle = None
            return False
        self._handle.seek(0)
        self._handle.truncate()
        self._handle.write("%d\n" % os.getpid())
        self._handle.flush()
        return True

    def release(self):
        if self._handle is None:
            return
        import fcntl

        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._handle.close()
        self._handle = None


def wait_until(predicate, timeout, interval=0.1):
    """Poll ``predicate`` until it is true or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
