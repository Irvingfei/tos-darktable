"""The launcher: brings the session up, keeps it up, and takes it down cleanly.

The process tree, top to bottom::

    systemd  ->  darktable-server (this program)
                   |-- Xvfb      the virtual display
                   |-- x11vnc    the VNC server on that display
                   |-- darktable the editor itself
                   \\-- HTTP listener and per-connection WebSocket bridges

The design decision that shapes everything here is that **the launcher never
exits because darktable exited.** darktable is a desktop application being
driven over a remote connection, so it will sometimes crash - a bad RAW, a
driver quirk, an out-of-memory kill. If the launcher treated that as fatal the
unit would restart, the display would be rebuilt, the user's browser would drop
and systemd's start limit would eventually disable the application entirely.
Instead the editor is restarted underneath a display that stays up, the browser
reconnects to the same desktop, and after too many failures the application
reports itself degraded and keeps serving a page that says so.
"""

import os
import signal
import threading
import time

from . import __version__, bundle, display, httpd, logging_setup, processes
from .config import config

log = logging_setup.get_logger("launcher")

# Restart policy for the editor. Deliberately more forgiving than the systemd
# unit's own limit, because a crash here is contained: the unit stays up and
# only the editor is recycled.
MAX_EDITOR_RESTARTS = 5
RESTART_WINDOW_SECONDS = 600
RESTART_BACKOFF = (1.0, 2.0, 5.0, 10.0, 20.0)

# Restart policy for the display stack. A dead X or VNC server is not
# recoverable underneath a running session, so the whole stack is rebuilt.
MAX_DISPLAY_RESTARTS = 3

LOOP_INTERVAL = 0.5

# Shutdown budget. The systemd unit allows 10 seconds; the launcher targets 6
# so that systemd's own teardown always has room and never has to SIGKILL.
SHUTDOWN_EDITOR_TIMEOUT = 3.0
SHUTDOWN_VNC_TIMEOUT = 1.0
SHUTDOWN_X_TIMEOUT = 1.0


class Supervisor(object):
    def __init__(self):
        self.stop_event = threading.Event()
        self.lock = processes.LockFile(config.lock_file)
        self.session = processes.SessionRecord(config.session_file, config.root)

        self.display_session = None
        self.editor = None
        self.http_server = None
        self.http_thread = None

        self.display_restarts = 0
        self.editor_restarts = 0
        self.restart_times = []
        self.degraded_reason = None
        self.started_at = time.time()
        self.cookie = os.urandom(16)

    # -- startup ----------------------------------------------------------

    def start(self):
        config.ensure_directories()

        missing = bundle.missing_pieces(config)
        if missing:
            # Named individually. A missing shared library or X server produces
            # a failure that looks like anything but its cause, and on a device
            # the operator has one chance to read the log.
            for path in missing:
                log.error("the runtime bundle is incomplete: %s is missing", path)
            log.error(
                "the package was built without its dependency bundle; "
                "see ci/collect_deps.py in the build repository"
            )
            return False

        bundle.prepare(config)

        swept = self.session.sweep()
        if swept:
            log.warning("terminated %d process(es) left by a previous run", swept)

        if not self.start_display_stack():
            return False

        # The editor is started before the HTTP server so that a browser
        # arriving immediately still finds a session being prepared rather than
        # a connection refused.
        self.start_editor()

        if not self.start_http():
            return False

        self.session.write(self.children())
        return True

    def children(self):
        result = []
        if self.display_session is not None:
            if self.display_session.xvfb is not None:
                result.append(self.display_session.xvfb)
            if self.display_session.x11vnc is not None:
                result.append(self.display_session.x11vnc)
        if self.editor is not None:
            result.append(self.editor)
        return result

    def start_display_stack(self):
        session = display.DisplaySession(config, self.cookie)
        if not session.start():
            log.error("the virtual display could not be started")
            return False
        self.display_session = session
        return True

    def start_editor(self):
        argv = [
            config.darktable_binary,
            "--configdir",
            config.dt_config_dir,
            "--cachedir",
            config.dt_cache_dir,
            "--datadir",
            config.darktable_datadir,
            "--library",
            config.dt_library,
        ]

        editor = processes.Child(
            "darktable",
            argv,
            bundle.child_environment(config, display=":%d" % self.display_session.display),
            log_file=os.path.join(config.log_dir, "darktable.log"),
            cwd=config.home_dir,
        )
        if not editor.start():
            return False
        self.editor = editor
        self.session.write(self.children())
        return True

    def start_http(self):
        try:
            self.http_server = httpd.create(self.stop_event, self)
        except OSError as error:
            log.error(
                "cannot listen on %s:%d: %s",
                config.listen_host,
                config.listen_port,
                error,
            )
            return False

        self.http_thread = threading.Thread(
            target=self.http_server.serve_forever,
            name="http",
            kwargs={"poll_interval": 0.5},
            daemon=True,
        )
        self.http_thread.start()
        log.info(
            "%s %s listening on %s:%d",
            config.app_id,
            __version__,
            config.listen_host,
            config.listen_port,
        )
        return True

    # -- supervision ------------------------------------------------------

    def _restart_budget_exhausted(self):
        now = time.time()
        self.restart_times = [t for t in self.restart_times if now - t < RESTART_WINDOW_SECONDS]
        return len(self.restart_times) >= MAX_EDITOR_RESTARTS

    def _supervise_editor(self):
        """Restart the editor when it dies, or report the session degraded."""
        if self.editor is None:
            return
        status = self.editor.poll()
        if status is None:
            return

        log.warning("darktable exited with status %s", status)
        self.editor = None

        if self._restart_budget_exhausted():
            self.degraded_reason = (
                "the editing session ended %d times in %d minutes"
                % (len(self.restart_times), RESTART_WINDOW_SECONDS // 60)
            )
            log.error("%s; the session will not be restarted automatically", self.degraded_reason)
            log.error("restart it from the web page, or run: systemctl restart %s", config.app_id)
            return

        delay = RESTART_BACKOFF[min(len(self.restart_times), len(RESTART_BACKOFF) - 1)]
        self.restart_times.append(time.time())
        log.info("restarting the editor in %.0fs", delay)
        if self.stop_event.wait(delay):
            return

        if not self.start_editor():
            log.error("the editor could not be restarted")

    def _supervise_display(self):
        """Rebuild the whole display stack when it dies."""
        if self.display_session is None or self.display_session.alive():
            return

        log.error("the virtual display stopped")
        self.display_session.stop()
        self.display_session = None

        if self.editor is not None:
            self.editor.stop()
            self.editor = None

        self.display_restarts += 1
        if self.display_restarts > MAX_DISPLAY_RESTARTS:
            self.degraded_reason = "the virtual display could not be kept running"
            log.error("%s; giving up", self.degraded_reason)
            return

        log.info("rebuilding the virtual display (attempt %d)", self.display_restarts)
        # Everything from the old session has been stopped above, so the record
        # describes dead processes. Clearing it rather than sweeping it is
        # deliberate: sweep() terminates whatever the record names, and running
        # it here would mean the rebuild's first act is to kill processes it is
        # about to recreate if any of them turned out to still be alive.
        self.session.clear()
        if self.start_display_stack():
            self.start_editor()
            # The display is new, so any browser tab still holding the old
            # WebSocket is connected to a dead socket and will reconnect on its
            # own. Record the new process table before serving again.
            self.session.write(self.children())

    def run(self):
        """Supervision loop. Returns when a stop is requested."""
        while not self.stop_event.is_set():
            if self.degraded_reason is None:
                self._supervise_editor()
                self._supervise_display()
            self.stop_event.wait(LOOP_INTERVAL)

    def describe(self):
        """Machine-readable state for /api/status."""
        return {
            "display": self.display_session.display if self.display_session else None,
            "degraded": self.degraded_reason,
            "editor_restarts": self.editor_restarts,
            "display_restarts": self.display_restarts,
            "drivers": [
                {
                    "name": child.name,
                    "pid": child.pid(),
                    "running": child.alive(),
                }
                for child in self.children()
            ],
        }

    def restart_editor(self):
        """Reset the failure counter and bring the editor back.

        This is the way out of the degraded state: without it a user whose
        session crashed repeatedly has no recovery short of restarting the
        whole service from the NAS console.
        """
        if self.editor is not None:
            self.editor.stop()
            self.editor = None
        self.restart_times = []
        self.degraded_reason = None
        return self.start_editor()

    # -- shutdown ---------------------------------------------------------

    def shutdown(self):
        """Tear down within the systemd stop budget.

        Order matters: the editor first, so it is not left drawing into a
        display that has gone; then the HTTP listener, so no new WebSocket can
        attach to a socket about to disappear; then VNC; then X.
        """
        log.info("shutting down")

        if self.editor is not None:
            self.editor.stop(term_timeout=SHUTDOWN_EDITOR_TIMEOUT)
            self.editor = None

        if self.http_server is not None:
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception as error:  # pragma: no cover - defensive
                log.warning("error closing the HTTP listener: %s", error)
            self.http_server = None

        if self.display_session is not None:
            # VNC before X, so no client observes a display that has gone.
            if self.display_session.x11vnc is not None:
                self.display_session.x11vnc.stop(term_timeout=SHUTDOWN_VNC_TIMEOUT)
                self.display_session.x11vnc = None
            if self.display_session.xvfb is not None:
                self.display_session.xvfb.stop(term_timeout=SHUTDOWN_X_TIMEOUT)
                self.display_session.xvfb = None
            self.display_session.stop()
            self.display_session = None

        self.session.clear()
        self.lock.release()
        log.info("stopped")


def main(argv=None):
    """Entry point. Returns a process exit status."""
    config.ensure_directories()
    logging_setup.setup_logging(config.log_level, config.log_dir)

    supervisor = Supervisor()

    if not supervisor.lock.acquire():
        log.error(
            "another instance already holds %s; refusing to start a second one",
            config.lock_file,
        )
        return 1

    def request_stop(signum, _frame):
        # Only set the flag. Doing the teardown inside the handler would run it
        # on whichever thread the signal interrupted - possibly one blocked on
        # a socket - and the ordering the shutdown comment describes would not
        # hold. The main loop notices this within LOOP_INTERVAL.
        log.info("received signal %d", signum)
        supervisor.stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        if not supervisor.start():
            return 1
        supervisor.run()
    except Exception:
        log.exception("the launcher failed")
        return 1
    finally:
        # Runs on every path, including the failed start above, so a partial
        # bring-up cannot leave an X server or an editor behind.
        supervisor.shutdown()

    return 0
