#!/usr/bin/env python3
"""End-to-end check of a running installation, without a browser.

Connects to the application's own WebSocket endpoint, completes the RFC 6455
handshake and then the RFB handshake behind it, and asserts that a real VNC
session is on the other end. That single sequence exercises every layer in
order: HTTP Basic auth, the WebSocket bridge in lib/wsbridge.py, x11vnc, the
X server, and - indirectly - darktable, which is the only client keeping the
display busy.

Why not a screenshot: it would prove the same thing more slowly and less
precisely, and it would need a browser in the verification container. A decoded
RFB ServerInit carries the desktop's exact width and height, so a pass means
the display really is the one the launcher configured.

What this does NOT prove: that darktable has painted its windows. The X server
answers whether or not a client has drawn anything. Process liveness and the
launcher's own log are checked alongside, which together cover it.

Usage:
    ci/verify_ws.py --host 127.0.0.1 --port 9312 --password secret
    ci/verify_ws.py --url http://127.0.0.1:9312/ --password secret

Exit code 0 means every assertion held.
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import sys
import urllib.parse
import urllib.request

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OK = "\033[32m  ok  \033[0m"
BAD = "\033[31m FAIL \033[0m"


class Failure(Exception):
    pass


def recv_exactly(sock, count):
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise Failure("the connection closed after %d of %d bytes" % (count - remaining, count))
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def http_get(url, headers=None, timeout=10):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def basic_auth(user, password):
    token = base64.b64encode(("%s:%s" % (user, password)).encode("utf-8")).decode("ascii")
    return {"Authorization": "Basic " + token}


# --------------------------------------------------------------------------- #


def check_health(base_url, report):
    """The health endpoint is public and must answer without credentials."""
    status, body, _headers = http_get(base_url + "health")
    if status != 200:
        raise Failure("GET /health answered %s, expected 200" % status)
    payload = json.loads(body)
    if payload.get("status") != "ok":
        raise Failure("GET /health returned %r" % payload)
    report("health endpoint answers without credentials (%s)" % payload.get("version"))


def check_auth_required(base_url, report):
    """Everything else must be closed without a password."""
    status, _body, headers = http_get(base_url + "api/status")
    if status != 401:
        raise Failure("GET /api/status answered %s without credentials, expected 401" % status)
    if "www-authenticate" not in {key.lower() for key in headers}:
        raise Failure("the 401 did not carry a WWW-Authenticate header")
    report("unauthenticated requests to /api/status are refused with 401")


def check_status(base_url, user, password, report):
    status, body, _headers = http_get(base_url + "api/status", basic_auth(user, password))
    if status != 200:
        raise Failure("authenticated GET /api/status answered %s" % status)
    payload = json.loads(body)
    report("authenticated status: %s" % json.dumps(payload.get("drivers", [])))

    if payload.get("degraded"):
        raise Failure("the launcher reports itself degraded: %s" % payload["degraded"])
    if not payload.get("display"):
        raise Failure("no X display is reported")

    running = {entry["name"]: entry.get("running") for entry in payload.get("drivers", [])}
    for expected in ("Xvfb", "x11vnc", "darktable"):
        if not running.get(expected):
            raise Failure("%s is not running (%s)" % (expected, running))
    report("Xvfb, x11vnc and darktable are all running")
    return payload


def websocket_handshake(sock, path, host_header, authorization):
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET %s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: %s\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: binary\r\n"
        "%s"
        "\r\n"
    ) % (path, host_header, key, ("Authorization: %s\r\n" % authorization) if authorization else "")

    sock.sendall(request.encode("ascii"))

    # Read until the end of the response headers.
    buffer = b""
    while b"\r\n\r\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise Failure("the server closed during the WebSocket handshake")
        buffer += chunk

    head, _, _rest = buffer.partition(b"\r\n\r\n")
    text = head.decode("ascii", "replace")

    if not text.startswith("HTTP/1.1 101"):
        first = text.splitlines()[0] if text else "(no status line)"
        raise Failure("expected 101 Switching Protocols, got %r" % first)

    expected = base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()
    ).decode("ascii")
    lowered = text.lower()
    if expected.lower() not in lowered:
        raise Failure("Sec-WebSocket-Accept did not match the key we sent")

    return text


def read_ws_frame(sock):
    """Read one server-to-client frame. Server frames are never masked."""
    header = recv_exactly(sock, 2)
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recv_exactly(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recv_exactly(sock, 8))[0]
    return header[0] & 0x0F, recv_exactly(sock, length) if length else b""


def send_ws_frame(sock, payload, opcode=0x2):
    """Send a masked client frame, as the specification requires."""
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length <= 125:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", length)
    header += mask
    masked = bytes(byte ^ mask[index & 3] for index, byte in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def check_vnc_session(host, port, path, authorization, width, height, report):
    """Complete the WebSocket and RFB handshakes and check the ServerInit."""
    sock = socket.create_connection((host, port), timeout=15)
    sock.settimeout(15)
    try:
        headers = websocket_handshake(sock, path, "%s:%d" % (host, port), authorization)
        report("WebSocket handshake returned 101")
        if "sec-websocket-protocol: binary" in headers.lower():
            report("the binary subprotocol was negotiated")

        # --- RFB ---
        opcode, version = read_ws_frame(sock)
        if opcode != 0x2:
            raise Failure("expected a binary frame, got opcode 0x%X" % opcode)
        if not version.startswith(b"RFB "):
            raise Failure("the first payload was not a VNC version banner: %r" % version[:16])
        report("VNC banner: %s" % version.decode("ascii", "replace").strip())

        # Echo the version back, then negotiate security.
        send_ws_frame(sock, b"RFB 003.008\n")

        _opcode, security = read_ws_frame(sock)
        if not security:
            raise Failure("the server sent an empty security list")
        count = security[0]
        offered = list(security[1:1 + count])
        if 1 not in offered:
            raise Failure("the server does not offer the None security type: %r" % offered)
        report("security types offered: %r" % offered)

        # Choose None: the VNC socket is reachable only by the application
        # account, and the browser-facing gate is the HTTP password.
        send_ws_frame(sock, bytes([1]))

        _opcode, result = read_ws_frame(sock)
        # A 3.8 server sends SecurityResult even for None. All four bytes zero
        # cannot be a ServerInit, because that would mean a zero-width screen,
        # so the two are unambiguous.
        if len(result) >= 4 and result[:4] == b"\x00\x00\x00\x00":
            report("security result: OK")
            init_prefix = b""
        else:
            init_prefix = result

        send_ws_frame(sock, bytes([1]))  # ClientInit, shared

        payload = init_prefix + read_ws_frame(sock)[1]
        while len(payload) < 24:
            payload += read_ws_frame(sock)[1]

        actual_width, actual_height = struct.unpack(">HH", payload[0:4])
        name_length = struct.unpack(">I", payload[20:24])[0]

        name = payload[24:24 + name_length]
        while len(name) < name_length:
            name += read_ws_frame(sock)[1]

        report("ServerInit: %dx%d, desktop %r" % (actual_width, actual_height, name.decode("utf-8", "replace")))

        if (actual_width, actual_height) != (width, height):
            raise Failure(
                "the desktop is %dx%d but the launcher configured %dx%d"
                % (actual_width, actual_height, width, height)
            )
        report("the display geometry matches the configuration")

        return True
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="End-to-end check of a running installation")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9312)
    parser.add_argument("--url", help="base URL; overrides --host/--port")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1000)
    parser.add_argument("--ws-timeout", type=int, default=90,
                        help="seconds to wait for the first VNC frame")
    args = parser.parse_args(argv)

    if args.url:
        parsed = urllib.parse.urlsplit(args.url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        base_path = parsed.path if parsed.path.endswith("/") else parsed.path + "/"
    else:
        host, port, base_path = args.host, args.port, "/"

    base_url = "http://%s:%d%s" % (host, port, base_path)
    ws_path = base_path + "websocket"
    authorization = basic_auth(args.user, args.password)["Authorization"]

    checks = [
        ("health", lambda r: check_health(base_url, r)),
        ("auth", lambda r: check_auth_required(base_url, r)),
        ("status", lambda r: check_status(base_url, args.user, args.password, r)),
        (
            "vnc",
            lambda r: check_vnc_session(host, port, ws_path, authorization, args.width, args.height, r),
        ),
    ]

    print("verifying %s" % base_url)
    print("=" * 62)

    failures = 0
    for name, function in checks:
        try:
            function(lambda message: print("%s %s" % (OK, message)))
        except Failure as error:
            failures += 1
            print("%s %s: %s" % (BAD, name, error))
            break
        except (OSError, ValueError, KeyError) as error:
            failures += 1
            print("%s %s: unexpected error: %s" % (BAD, name, error))
            break

    print("-" * 62)
    if failures:
        print("RESULT: FAILED")
        return 1
    print("RESULT: PASSED - the desktop is reachable end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
