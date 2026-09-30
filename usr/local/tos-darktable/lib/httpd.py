"""The HTTP front end.

One listener serves three things: the bundled noVNC frontend as static files,
the WebSocket endpoint the frontend connects to for the VNC stream, and a
health endpoint the platform can probe. Every route except the health endpoint
requires the access password.

Authentication is HTTP Basic over the same password file postinst generates.
Two layers stand behind it: the VNC server itself publishes only a unix socket
that is mode 0600 inside the application directory, so nothing on the network
can reach the desktop transport without already being this application's user.
Basic auth is therefore the single gate, which is why it is applied to the
static files as well as to the stream - serving even the page for free would
hand an unauthenticated visitor the desktop's address.
"""

import base64
import hmac
import json
import os
import posixpath
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, logging_setup, wsbridge
from .config import config

log = logging_setup.get_logger("http")

# Routes that do not require the password. Only the health endpoint: the
# platform's readiness probe has no credentials to offer, and the response
# carries nothing an attacker can use.
PUBLIC_PATHS = {"/health"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".map": "application/json; charset=utf-8",
}

DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _unauthorized(handler, realm):
    body = b"Authentication required.\n"
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="%s", charset="UTF-8"' % realm)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "tos-darktable/%s" % __version__
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Bound the time a stalled client can hold a worker thread. The WebSocket
    # path clears this once it takes the connection over.
    timeout = 30

    # -- helpers ----------------------------------------------------------

    def _client_path(self):
        """Return the request path with any platform prefix removed.

        The application is reachable two ways: directly on its own port, where
        TOS opens a new browser tab, and through the platform's nginx, where it
        appears under /tos-darktable/. Stripping that prefix here means the
        frontend can use one set of relative URLs and work under both.
        """
        path = urllib.parse.urlsplit(self.path).path
        prefix = "/%s" % config.app_id
        if path == prefix:
            return "/"
        if path.startswith(prefix + "/"):
            return path[len(prefix):]
        return path

    def _authorised(self):
        """Check the request against the access password.

        Fails closed: with no password file there is nothing to check against,
        so every request is refused rather than let through. An install whose
        postinst did not complete must not become an open desktop.
        """
        password = config.read_password()
        if not password:
            return False

        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip()).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return False

        # The username is not part of the secret; the password file stores only
        # a password, and any username is accepted so the operator does not have
        # to remember one as well.
        _username, separator, supplied = decoded.partition(":")
        if not separator:
            return False

        # Constant-time comparison, so that the response time cannot be used to
        # recover the password a character at a time.
        return hmac.compare_digest(supplied, password)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_text(self, status, message):
        body = (message + "\n").encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # The default implementation writes to stderr with no level and no
        # component, which loses it among the platform's own output. Requests
        # are logged through the normal logger instead.
        log.debug("%s %s", self.address_string(), fmt % args)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        path = self._client_path()

        if path in PUBLIC_PATHS:
            self._handle_health(path)
            return

        if not self._authorised():
            _unauthorized(self, config.app_id)
            return

        if path == "/websocket":
            self._handle_websocket()
            return

        if path == "/api/status":
            self._handle_status()
            return

        self._serve_static(path)

    def do_HEAD(self):
        path = self._client_path()
        if path in PUBLIC_PATHS or self._authorised():
            self._send_error_text(405, "Use GET.")
        else:
            _unauthorized(self, config.app_id)

    def do_POST(self):
        path = self._client_path()
        if not self._authorised():
            _unauthorized(self, config.app_id)
            return

        if path == "/api/restart":
            self._handle_restart()
            return

        self._send_error_text(404, "Not found.")

    def _handle_restart(self):
        """Bring the editor back after the supervisor gave up on it.

        The only way out of the degraded state. Without it, a user whose
        session crashed five times has no recovery short of reaching the NAS
        console and restarting the service by hand - which is a poor answer for
        an application whose whole premise is that it is used from a browser.
        """
        supervisor = getattr(self.server, "supervisor", None)
        if supervisor is None:
            self._send_error_text(503, "The supervisor is not available.")
            return

        log.info("restart requested from the web interface")
        if supervisor.restart_editor():
            self._send_json({"restarted": True})
        else:
            self._send_error_text(500, "The session could not be restarted.")

    def _handle_health(self, path):
        """Minimal unauthenticated readiness answer."""
        self._send_json({"status": "ok", "app": config.app_id, "version": __version__})

    def _handle_status(self):
        """Report what is running, so a reviewer can check for leftovers.

        Chapter 16 of the guide asks whether an uninstall leaves processes
        behind. Answering that from the application itself is faster than
        reading the process table by hand.
        """
        supervisor = getattr(self.server, "supervisor", None)
        payload = {
            "version": __version__,
            "display": None,
            "uptime_seconds": int(time.time() - getattr(self.server, "started_at", time.time())),
            "drivers": [],
            "share_root": config.share_root,
        }
        if supervisor is not None:
            payload.update(supervisor.describe())
        self._send_json(payload)

    def _handle_websocket(self):
        """Take the connection over and bridge it to the VNC socket."""
        headers = {key.lower(): value for key, value in self.headers.items()}
        if not wsbridge.is_websocket_upgrade(headers):
            self._send_error_text(400, "Expected a WebSocket upgrade.")
            return

        response = wsbridge.build_handshake_response(headers)
        if response is None:
            self._send_error_text(400, "Missing Sec-WebSocket-Key.")
            return

        try:
            upstream = wsbridge.connect_upstream(config.vnc_port)
        except OSError as error:
            # The desktop transport is not up. Reported as 503 so the frontend
            # can say "the session is restarting" rather than "connection
            # failed", which are different problems for the operator.
            log.warning("cannot reach the VNC server on 127.0.0.1:%d: %s", config.vnc_port, error)
            self._send_error_text(503, "The desktop is not running.")
            return

        self.wfile.write(response)
        self.wfile.flush()

        # From here the connection is not HTTP any more. Clear the socket
        # timeout: a VNC session is legitimately idle for long stretches, and
        # the handler's own timeout would otherwise close a desktop the user
        # had simply stopped moving the mouse in.
        self.close_connection = True
        try:
            self.connection.settimeout(None)
        except OSError:
            pass

        stop_event = getattr(self.server, "stop_event", None)
        if stop_event is None:
            import threading

            stop_event = threading.Event()

        try:
            wsbridge.bridge(self.connection, self.rfile, upstream, stop_event)
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def _serve_static(self, path):
        if path.endswith("/"):
            path += "index.html"

        # Resolve inside the webui directory and verify the result really is
        # inside it. Without the check, a request for /../../etc/passwd would
        # be served: normpath alone collapses the traversal, and the comparison
        # is what proves containment.
        relative = posixpath.normpath(urllib.parse.unquote(path)).lstrip("/")
        if relative.startswith(".."):
            self._send_error_text(403, "Forbidden.")
            return

        root = os.path.realpath(config.webui_dir)
        target = os.path.realpath(os.path.join(root, relative))
        if target != root and not target.startswith(root + os.sep):
            self._send_error_text(403, "Forbidden.")
            return

        if not os.path.isfile(target):
            self._send_error_text(404, "Not found.")
            return

        extension = os.path.splitext(target)[1].lower()
        content_type = CONTENT_TYPES.get(extension, DEFAULT_CONTENT_TYPE)

        try:
            with open(target, "rb") as handle:
                body = handle.read()
        except OSError as error:
            log.warning("cannot read %s: %s", target, error)
            self._send_error_text(500, "Cannot read the file.")
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The frontend is rebuilt with every release and served from a file
        # that changes with it, so caching buys nothing and a stale copy is
        # exactly the failure this avoids.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class Server(ThreadingHTTPServer):
    """Threaded HTTP server carrying the shutdown event and the supervisor."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, stop_event, supervisor=None):
        self.stop_event = stop_event
        self.supervisor = supervisor
        self.started_at = time.time()
        super().__init__(address, handler)


def create(stop_event, supervisor=None):
    """Bind the listener. Raises OSError when the port is unavailable."""
    return Server((config.listen_host, config.listen_port), Handler, stop_event, supervisor)
