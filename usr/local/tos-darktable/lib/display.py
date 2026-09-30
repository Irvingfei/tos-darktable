"""The virtual display: an X server and a VNC server on top of it.

Neither is available as a library on a NAS, so both are bundled binaries under
``depends/bin`` and driven from here.

Two decisions worth knowing about:

**VNC listens on loopback only.** x11vnc is given ``-localhost``, so it binds
127.0.0.1 and nothing on the network can reach the desktop transport directly.
The only port the application exposes on all interfaces is the HTTP one, which
keeps the permission declaration to a single port. Authentication happens once,
at the HTTP layer.

An earlier design published a unix socket instead, which is tidier in principle
and does not work: the x11vnc that Ubuntu 22.04 ships rejects ``-rfbunixpath``
and ``-rfbunixmode`` as unrecognised options and exits. ``-localhost`` obtains
the same property with an option that exists.

**``-noreset`` on the X server.** Without it, Xvfb resets the display when its
last client disconnects. darktable is the only real client, so a crash would
take the whole virtual desktop with it and the user's recovery path - restart
the session and carry on - would be gone. With ``-noreset`` the display outlives
darktable and a restarted instance reconnects to it.
"""

import os
import struct

from . import logging_setup, processes

log = logging_setup.get_logger("display")

# Display numbers the launcher is willing to use. Writing the authority entry
# for the whole range up front means the cookie is already in place whichever
# number the X server ends up on.
DISPLAY_MIN = 99
DISPLAY_MAX = 120

FAMILY_LOCAL = 256
COOKIE_NAME = b"MIT-MAGIC-COOKIE-1"


def write_xauthority(path, displays, cookie):
    """Write an Xauthority file covering ``displays`` with one shared cookie.

    The file format is the flat sequence of records libXau defines: a 16-bit
    family, a 16-bit-length address, the address, a 16-bit-length display
    number as a string, the number, then the 16-bit-length name and 16-bit-length
    data of the cookie itself. Every integer is big-endian.

    Hand-writing it rather than shelling out to ``xauth`` removes one bundled
    binary and its library closure, and the format is small enough to be
    verified in CI by connecting a client to the display.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # The address for FamilyLocal is the hostname as the X server sees it.
    try:
        host = os.uname().nodename.encode("utf-8")
    except AttributeError:
        host = b"localhost"

    records = []
    for number in displays:
        display_bytes = str(number).encode("ascii")
        record = struct.pack(">H", FAMILY_LOCAL)
        record += struct.pack(">H", len(host)) + host
        record += struct.pack(">H", len(display_bytes)) + display_bytes
        record += struct.pack(">H", len(COOKIE_NAME)) + COOKIE_NAME
        record += struct.pack(">H", len(cookie)) + cookie
        records.append(record)

    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(b"".join(records))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _socket_path(number):
    return "/tmp/.X11-unix/X%d" % number


def free_display(candidate_range=range(DISPLAY_MIN, DISPLAY_MAX + 1)):
    """Return the first display number with no server already on it.

    A stale socket file is treated as free only when nothing is listening on
    it: an X server that died without cleaning up leaves the file behind, and
    refusing to reuse that number forever would exhaust the range after a few
    crashes.
    """
    import socket as socket_module

    for number in candidate_range:
        path = _socket_path(number)
        if not os.path.exists(path):
            return number
        probe = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        probe.settimeout(0.2)
        try:
            probe.connect(path)
            # Something answered: genuinely in use.
            continue
        except OSError:
            # A file with nothing behind it. Reusable, but remove it first so
            # the new server can bind.
            try:
                os.unlink(path)
            except OSError:
                pass
            return number
        finally:
            probe.close()
    return None


class DisplaySession(object):
    """Owns the X server and the VNC server for one application session."""

    def __init__(self, config, cookie):
        self.config = config
        self.cookie = cookie
        self.display = None
        self.xvfb = None
        self.x11vnc = None

    # -- X server ---------------------------------------------------------

    def _xvfb_argv(self, number, with_xkbdir):
        argv = [
            self.config.xvfb_binary,
            ":%d" % number,
            "-screen",
            "0",
            "%dx%dx%d"
            % (self.config.screen_width, self.config.screen_height, self.config.screen_depth),
            # The X server is reachable only through the unix socket. Stated
            # explicitly rather than relying on the Debian default so that the
            # intent is visible to a reader and to a reviewer.
            "-nolisten",
            "tcp",
            "-auth",
            self.config.xauth_file,
            # Keep the display alive when the last client disconnects; see the
            # module docstring.
            "-noreset",
        ]
        if with_xkbdir and os.path.isdir(self.config.xkb_dir):
            argv += ["-xkbdir", self.config.xkb_dir]
        return argv

    def start_x_server(self):
        """Start Xvfb, retrying once without ``-xkbdir`` if it refuses the flag.

        ``-xkbdir`` is the documented way to relocate the keymap data, but the
        X server's option parser rejects what it does not know, so a build
        without that option must not be a hard failure: falling back to the
        host's default path is far better than no display at all.
        """
        displays = list(range(DISPLAY_MIN, DISPLAY_MAX + 1))
        write_xauthority(self.config.xauth_file, displays, self.cookie)

        for with_xkbdir in (True, False):
            number = free_display()
            if number is None:
                log.error("no free X display number in %d-%d", DISPLAY_MIN, DISPLAY_MAX)
                return False

            env = self.config_child_environment(number)
            child = processes.Child(
                "Xvfb",
                self._xvfb_argv(number, with_xkbdir),
                env,
                log_file=os.path.join(self.config.log_dir, "xvfb.log"),
            )
            if not child.start():
                return False

            if processes.wait_until(lambda: os.path.exists(_socket_path(number)), timeout=10.0):
                if child.poll() is not None:
                    # Exited after creating the socket, or never really came up.
                    log.error("Xvfb exited immediately with status %s", child.poll())
                    if with_xkbdir:
                        log.warning("retrying without -xkbdir")
                        continue
                    return False
                self.xvfb = child
                self.display = number
                log.info("X server ready on :%d", number)
                return True

            if child.poll() is None:
                log.error("X server did not publish a socket on :%d within 10s", number)
                child.stop()
                return False

            log.error("Xvfb failed with status %s", child.poll())
            if with_xkbdir:
                log.warning("retrying without -xkbdir")
                continue
            return False

        return False

    # -- VNC server -------------------------------------------------------

    def start_vnc_server(self):
        """Start x11vnc, listening on loopback only."""
        port = self.config.vnc_port

        argv = [
            self.config.x11vnc_binary,
            "-display",
            ":%d" % self.display,
            "-auth",
            self.config.xauth_file,
            "-rfbport",
            str(port),
            # Loopback only.
            #
            # The VNC server publishes a unix socket in an earlier design, so
            # that nothing but the HTTP bridge could reach it. The x11vnc that
            # Ubuntu 22.04 ships does not accept -rfbunixpath or -rfbunixmode -
            # it rejects them as unrecognised options and exits - so the same
            # property is obtained with -localhost instead, which binds the
            # listener to 127.0.0.1. The LAN still cannot reach it, and the
            # only port exposed on all interfaces remains the HTTP one.
            "-localhost",
            # No VNC password: authentication happens once at the HTTP layer,
            # and this listener is reachable only from this machine.
            "-nopw",
            # Keep serving after a browser tab closes, and allow the operator
            # to open the desktop from more than one tab.
            "-forever",
            "-shared",
            # The X DAMAGE extension misreports updates against the virtual
            # framebuffer of a headless server and leaves stale regions on
            # screen. Polling is slower on paper and correct in practice.
            "-noxdamage",
            "-repeat",
            "-quiet",
        ]

        child = processes.Child(
            "x11vnc",
            argv,
            self.config_child_environment(self.display),
            log_file=os.path.join(self.config.log_dir, "x11vnc.log"),
        )
        if not child.start():
            return False

        if not processes.wait_until(lambda: self._vnc_accepting(port), timeout=8.0):
            status = child.poll()
            log.error("x11vnc is not accepting connections on %d within 8s (status %s)", port, status)
            child.stop()
            return False

        if child.poll() is not None:
            log.error("x11vnc exited immediately with status %s", child.poll())
            return False

        self.x11vnc = child
        log.info("VNC server ready on 127.0.0.1:%d", port)
        return True

    @staticmethod
    def _vnc_accepting(port):
        """True when something is listening on the loopback port.

        Polled by connecting rather than by watching a PID, because a socket
        file appearing is not the same as a server accepting - and with a port
        there is no file to watch at all.
        """
        import socket as socket_module

        probe = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
        probe.settimeout(0.3)
        try:
            probe.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    # -- lifecycle --------------------------------------------------------

    def config_child_environment(self, display):
        from . import bundle

        return bundle.child_environment(self.config, display=":%d" % display)

    def start(self):
        if not self.start_x_server():
            return False
        return self.start_vnc_server()

    def stop(self):
        """Tear the stack down, VNC first so no client observes a dead X."""
        ok = True
        if self.x11vnc is not None:
            ok = self.x11vnc.stop() and ok
            self.x11vnc = None

        if self.xvfb is not None:
            ok = self.xvfb.stop() and ok
            self.xvfb = None

        # The X server removes its own socket on a clean exit; a killed one
        # does not. Removing it here keeps the next start from having to
        # diagnose a stale file.
        if self.display is not None:
            try:
                os.unlink(_socket_path(self.display))
            except OSError:
                pass
            self.display = None

        return ok

    def alive(self):
        return self.xvfb is not None and self.xvfb.alive() and self.x11vnc is not None and self.x11vnc.alive()
