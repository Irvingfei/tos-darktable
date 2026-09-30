"""A WebSocket to TCP bridge for the VNC stream.

The browser cannot open a raw TCP connection, and noVNC speaks VNC over a
WebSocket. Something therefore has to translate between the two. That is all
this module does: it is a byte pipe with framing on one end.

It is written here rather than taken from ``websockify`` on purpose. websockify
is LGPLv3, and vendoring it would add a licence obligation and a piece of the
runtime this project does not control, in exchange for behaviour that is about
two hundred lines of standard library. The two ends of this pipe are both ours:
noVNC is shipped in the package, and the other end is the x11vnc we bundle.

Only what a VNC stream needs is implemented:

* the RFC 6455 opening handshake, including the ``binary`` subprotocol noVNC
  asks for;
* binary, text, continuation, ping, pong and close frames on the client's side,
  with the client-to-server masking the specification requires. Fragmented
  messages are forwarded fragment by fragment rather than reassembled, which is
  correct for a byte pipe and is explained where it happens.

Not implemented, because nothing in this path can produce them: extensions
(noVNC negotiates none for a plain binary stream), and the ``base64``
subprotocol, which exists only for clients that cannot receive binary frames.
"""

import base64
import hashlib
import os
import socket
import struct
import threading

from . import logging_setup

log = logging_setup.get_logger("wsbridge")

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

# A single frame larger than this is refused rather than buffered. A VNC
# framebuffer update for the configured 1600x1000 screen is on the order of a
# few hundred kilobytes, so this is generous while still bounding what one
# client can make the process allocate.
MAX_FRAME_BYTES = 8 * 1024 * 1024

# The largest payload that fits in the 7-bit length field.
_SHORT_PAYLOAD = 125


def accept_key(client_key):
    """Compute the Sec-WebSocket-Accept value for a client's key."""
    digest = hashlib.sha1(client_key.strip().encode("ascii") + GUID).digest()
    return base64.b64encode(digest).decode("ascii")


def is_websocket_upgrade(headers):
    """True when the request headers ask for a WebSocket upgrade."""
    upgrade = (headers.get("upgrade") or "").lower()
    connection = (headers.get("connection") or "").lower()
    return upgrade == "websocket" and "upgrade" in connection


def build_handshake_response(headers):
    """Return the 101 response bytes, or None when the request is not valid."""
    key = headers.get("sec-websocket-key")
    if not key:
        return None

    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Accept: %s" % accept_key(key),
    ]

    # noVNC offers the "binary" subprotocol. Echoing it is what tells the client
    # it may send and expect binary frames; leaving it out makes some clients
    # fall back to a base64 encoding this server does not implement.
    offered = headers.get("sec-websocket-protocol") or ""
    protocols = [item.strip() for item in offered.split(",") if item.strip()]
    if "binary" in protocols:
        lines.append("Sec-WebSocket-Protocol: binary")

    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def encode_frame(opcode, payload):
    """Encode a server-to-client frame. Server frames are never masked."""
    length = len(payload)
    header = bytearray()
    header.append(0x80 | opcode)  # FIN set: this implementation never fragments outbound

    if length <= _SHORT_PAYLOAD:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)

    return bytes(header) + payload


def _read_exactly(reader, count):
    """Read exactly ``count`` bytes, or return None if the peer went away."""
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = reader.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class FrameReader(object):
    """Decodes client-to-server frames from a buffered stream."""

    def __init__(self, reader):
        self.reader = reader

    def read_frame(self):
        """Return ``(opcode, payload)``, or None at end of stream.

        Raises ``ValueError`` on a protocol violation, which the caller reports
        by closing the connection: a malformed frame means the peer is not
        speaking this protocol and there is nothing useful to say back.
        """
        header = _read_exactly(self.reader, 2)
        if header is None:
            return None

        first, second = header[0], header[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            raw = _read_exactly(self.reader, 2)
            if raw is None:
                return None
            length = struct.unpack(">H", raw)[0]
        elif length == 127:
            raw = _read_exactly(self.reader, 8)
            if raw is None:
                return None
            length = struct.unpack(">Q", raw)[0]

        if length > MAX_FRAME_BYTES:
            raise ValueError("frame of %d bytes exceeds the %d byte limit" % (length, MAX_FRAME_BYTES))

        # RFC 6455 section 5.1: every client-to-server frame must be masked,
        # and a server must fail the connection when one is not.
        if not masked:
            raise ValueError("client frame is not masked")

        mask = _read_exactly(self.reader, 4)
        if mask is None:
            return None

        payload = _read_exactly(self.reader, length) if length else b""
        if payload is None:
            return None

        unmasked = bytearray(payload)
        for index in range(len(unmasked)):
            unmasked[index] ^= mask[index & 3]

        return fin, opcode, bytes(unmasked)


def _close_frame(code=1000, reason=b""):
    return encode_frame(OPCODE_CLOSE, struct.pack(">H", code) + reason)


def bridge(client_sock, client_reader, upstream_sock, stop_event):
    """Pump bytes between an established WebSocket and a TCP connection.

    Runs the client-to-server direction on the calling thread and the
    server-to-client direction on a worker, so that either side blocking on a
    read cannot stall the other. Both sockets are closed on the way out, which
    is what unblocks the other thread.
    """
    reader = FrameReader(client_reader)
    finished = threading.Event()

    def pump_upstream_to_client():
        """Forward raw VNC bytes to the browser as binary frames."""
        try:
            while not stop_event.is_set() and not finished.is_set():
                data = upstream_sock.recv(65536)
                if not data:
                    break
                client_sock.sendall(encode_frame(OPCODE_BINARY, data))
        except OSError:
            pass
        except Exception as error:  # pragma: no cover - defensive
            log.debug("upstream to client ended: %s", error)
        finally:
            finished.set()
            try:
                client_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                upstream_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    worker = threading.Thread(target=pump_upstream_to_client, name="vnc-to-browser", daemon=True)
    worker.start()

    try:
        while not stop_event.is_set() and not finished.is_set():
            frame = reader.read_frame()
            if frame is None:
                break
            _fin, opcode, payload = frame

            if opcode == OPCODE_BINARY or opcode == OPCODE_TEXT or opcode == OPCODE_CONTINUATION:
                # Fragments are forwarded as they arrive rather than reassembled
                # into a whole message first. That is not an omission: the far
                # end is a TCP stream, where frame boundaries carry no meaning
                # and only byte order does. Reassembling would add buffering and
                # latency to a path whose whole purpose is to be a pipe, and
                # would buy nothing, since the bytes written are identical
                # either way.
                if payload:
                    upstream_sock.sendall(payload)
            elif opcode == OPCODE_PING:
                client_sock.sendall(encode_frame(OPCODE_PONG, payload))
            elif opcode == OPCODE_CLOSE:
                try:
                    client_sock.sendall(_close_frame())
                except OSError:
                    pass
                break
            elif opcode == OPCODE_PONG:
                # A reply to a ping we do not send. Nothing to do.
                pass
            else:
                raise ValueError("unsupported opcode 0x%X" % opcode)
    except (OSError, ValueError) as error:
        log.debug("client to upstream ended: %s", error)
    finally:
        finished.set()
        try:
            client_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            upstream_sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        worker.join(timeout=2.0)


def connect_upstream(socket_path, timeout=5.0):
    """Connect to the VNC server's unix socket."""
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    upstream.settimeout(timeout)
    try:
        upstream.connect(socket_path)
    except OSError:
        upstream.close()
        raise
    upstream.settimeout(None)
    return upstream
