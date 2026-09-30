#!/usr/bin/env python3
"""Offline self-test for the launcher's pure logic.

Everything in this file runs on any platform, with no X server, no VNC server
and no darktable binary. That is the point: the parts of this application that
can be wrong in a way a reviewer would never catch - the WebSocket framing, the
authority file's binary layout, the module-cache rewriting - are checked here,
on the workstation, before a package is ever built.

What is deliberately NOT here: anything that needs the bundled runtime. Those
paths are exercised by the container job in the build pipeline, which installs
the package into a bare Ubuntu 22.04 image and starts the real session.

Usage:
    python tools/selftest.py
    python tools/selftest.py -v
"""

import io
import os
import struct
import sys
import tempfile
import traceback

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_LIB = os.path.join(REPO_ROOT, "usr", "local", "tos-darktable")
if APP_LIB not in sys.path:
    sys.path.insert(0, APP_LIB)

from lib import bundle, display, wsbridge  # noqa: E402

VERBOSE = "-v" in sys.argv[1:] or "--verbose" in sys.argv[1:]

_results = []


def test(function):
    _results.append(function)
    return function


def expect(condition, message):
    if not condition:
        raise AssertionError(message)


def expect_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError("%s\n     expected: %r\n     actual:   %r" % (message, expected, actual))


# --------------------------------------------------------------------------- #
# WebSocket handshake
# --------------------------------------------------------------------------- #

@test
def websocket_accept_key_matches_rfc6455():
    """The example in RFC 6455 section 1.3, which is the only unambiguous check."""
    expect_equal(
        wsbridge.accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        "accept key does not match the RFC 6455 test vector",
    )


@test
def websocket_upgrade_detection():
    expect(
        wsbridge.is_websocket_upgrade({"upgrade": "websocket", "connection": "Upgrade"}),
        "a valid upgrade request was not recognised",
    )
    expect(
        wsbridge.is_websocket_upgrade({"upgrade": "WebSocket", "connection": "keep-alive, Upgrade"}),
        "case and a Connection list should both be tolerated",
    )
    expect(
        not wsbridge.is_websocket_upgrade({"upgrade": "websocket", "connection": "close"}),
        "a websocket upgrade without Connection: Upgrade must be refused",
    )
    expect(not wsbridge.is_websocket_upgrade({}), "an empty header set must be refused")


@test
def websocket_handshake_response():
    response = wsbridge.build_handshake_response(
        {"sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==", "sec-websocket-protocol": "binary"}
    )
    text = response.decode("ascii")
    expect(text.startswith("HTTP/1.1 101 "), "the handshake must answer 101")
    expect(
        "Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in text,
        "the accept header is missing or wrong",
    )
    # noVNC asks for the binary subprotocol; echoing it is what lets the client
    # send binary frames instead of falling back to base64.
    expect("Sec-WebSocket-Protocol: binary" in text, "the binary subprotocol was not echoed")

    expect(
        wsbridge.build_handshake_response({}) is None,
        "a request with no key must not produce a handshake",
    )


@test
def websocket_handshake_omits_unrequested_subprotocol():
    response = wsbridge.build_handshake_response({"sec-websocket-key": "AAAAAAAAAAAAAAAAAAAAAA=="})
    expect(
        b"Sec-WebSocket-Protocol" not in response,
        "a subprotocol must not be echoed when the client did not offer it",
    )


# --------------------------------------------------------------------------- #
# WebSocket framing
# --------------------------------------------------------------------------- #

def _client_frame(payload, opcode=wsbridge.OPCODE_BINARY, mask=b"\x01\x02\x03\x04", fin=True):
    """Build a masked client-to-server frame, as a browser would send it."""
    header = bytearray()
    header.append((0x80 if fin else 0x00) | opcode)
    length = len(payload)
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
    return bytes(header) + masked


@test
def websocket_frame_length_encoding():
    """The three length forms, checked against the header bytes they must produce."""
    expect_equal(wsbridge.encode_frame(wsbridge.OPCODE_BINARY, b"hello")[:2], b"\x82\x05",
                 "short payload length is wrong")

    medium = wsbridge.encode_frame(wsbridge.OPCODE_BINARY, b"x" * 126)
    expect_equal(medium[:2], b"\x82\x7e", "126-byte payload must use the 16-bit form")
    expect_equal(struct.unpack(">H", medium[2:4])[0], 126, "16-bit length is wrong")

    large = wsbridge.encode_frame(wsbridge.OPCODE_BINARY, b"x" * 70000)
    expect_equal(large[:2], b"\x82\x7f", "70000-byte payload must use the 64-bit form")
    expect_equal(struct.unpack(">Q", large[2:10])[0], 70000, "64-bit length is wrong")


@test
def websocket_frame_round_trip():
    """Every length form, masked on the way in, unmasked correctly on the way out."""
    for size in (0, 1, 125, 126, 127, 65535, 65536, 100000):
        payload = bytes((index * 7 + 3) & 0xFF for index in range(size))
        reader = wsbridge.FrameReader(io.BytesIO(_client_frame(payload)))
        frame = reader.read_frame()
        expect(frame is not None, "a %d byte frame did not decode" % size)
        _fin, opcode, decoded = frame
        expect_equal(opcode, wsbridge.OPCODE_BINARY, "opcode changed for a %d byte frame" % size)
        expect_equal(decoded, payload, "payload changed for a %d byte frame" % size)


@test
def websocket_unmasking_uses_the_frame_mask():
    """A different mask must still round-trip; a fixed unmask would not."""
    payload = b"the quick brown fox"
    for mask in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff", b"\xde\xad\xbe\xef"):
        reader = wsbridge.FrameReader(io.BytesIO(_client_frame(payload, mask=mask)))
        _fin, _opcode, decoded = reader.read_frame()
        expect_equal(decoded, payload, "unmasking failed for mask %r" % mask)


@test
def websocket_rejects_unmasked_client_frame():
    """RFC 6455 requires the server to fail the connection on an unmasked frame."""
    unmasked = b"\x82\x05hello"
    reader = wsbridge.FrameReader(io.BytesIO(unmasked))
    try:
        reader.read_frame()
    except ValueError:
        return
    raise AssertionError("an unmasked client frame must be rejected")


@test
def websocket_rejects_oversized_frame():
    header = b"\x82\x7f" + struct.pack(">Q", wsbridge.MAX_FRAME_BYTES + 1) + b"\x00\x00\x00\x00"
    reader = wsbridge.FrameReader(io.BytesIO(header))
    try:
        reader.read_frame()
    except ValueError:
        return
    raise AssertionError("a frame above the size limit must be rejected")


@test
def websocket_clean_eof_is_not_an_error():
    reader = wsbridge.FrameReader(io.BytesIO(b""))
    expect(reader.read_frame() is None, "end of stream must return None, not raise")


# --------------------------------------------------------------------------- #
# X authority file
# --------------------------------------------------------------------------- #

def _parse_xauthority(data):
    """Read back the libXau record format. Written independently of the writer."""
    entries = []
    offset = 0
    while offset < len(data):
        family = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        address = data[offset:offset + length]
        offset += length
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        number = data[offset:offset + length]
        offset += length
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        name = data[offset:offset + length]
        offset += length
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        cookie = data[offset:offset + length]
        offset += length
        entries.append((family, address, number, name, cookie))
    return entries


@test
def xauthority_is_well_formed():
    cookie = bytes(range(16))
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "Xauthority")
        display.write_xauthority(path, [99, 100], cookie)

        with open(path, "rb") as handle:
            data = handle.read()

    entries = _parse_xauthority(data)
    expect_equal(len(entries), 2, "one record per display was expected")

    for index, number in enumerate((b"99", b"100")):
        family, _address, parsed_number, name, parsed_cookie = entries[index]
        expect_equal(family, display.FAMILY_LOCAL, "family must be FamilyLocal")
        expect_equal(parsed_number, number, "display number %r is wrong" % number)
        expect_equal(name, b"MIT-MAGIC-COOKIE-1", "cookie name is wrong")
        expect_equal(parsed_cookie, cookie, "cookie bytes changed")


@test
def xauthority_is_not_world_readable():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "Xauthority")
        display.write_xauthority(path, [99], bytes(16))
        if os.name == "nt":
            return  # Windows does not carry POSIX modes; the packaged file is
                    # shipped 0600 by the build and re-chmodded by postinst.
        mode = os.stat(path).st_mode & 0o777
        expect_equal(mode, 0o600, "the authority file must not be readable by others")


# --------------------------------------------------------------------------- #
# Bundled module cache rewriting
# --------------------------------------------------------------------------- #

SAMPLE_LOADERS_CACHE = '''"libpixbufloader-png.so"
"png"
"PNG image"
"image/png"
""
"LGPL"
""
""
"/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-png.so"
"png"
"PNG image"
"image/png"
""
"LGPL"
""
""
'''


@test
def module_cache_paths_are_repointed():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "loaders.cache")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(SAMPLE_LOADERS_CACHE)

        bundle.rewrite_module_cache(path, "/opt/app/depends/lib/gdk-pixbuf-2.0/2.10.0/loaders")

        with open(path, "r", encoding="utf-8") as handle:
            rewritten = handle.read()

    expect(
        "/usr/lib/x86_64-linux-gnu" not in rewritten,
        "an absolute build-host path survived the rewrite",
    )
    expect(
        '"/opt/app/depends/lib/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-png.so"' in rewritten,
        "the loader was not repointed at the bundle",
    )
    # The metadata lines are not paths and must be left exactly as they were:
    # a rewrite that touched them would corrupt the cache's alternating layout.
    expect('"PNG image"' in rewritten, "non-path entries must not be rewritten")
    expect_equal(
        rewritten.count('"png"'), 2, "the module name entries must be untouched"
    )


@test
def missing_module_cache_is_reported_not_fatal():
    result = bundle.rewrite_module_cache("/nonexistent/loaders.cache", "/tmp")
    expect_equal(result, -1, "a missing cache must report -1 rather than raise")


# --------------------------------------------------------------------------- #
# Environment assembly
# --------------------------------------------------------------------------- #

@test
def child_environment_is_self_contained():
    from lib.config import config

    env = bundle.child_environment(config, display=":99")

    expect_equal(env["DISPLAY"], ":99", "DISPLAY was not set")
    expect_equal(env["TMPDIR"], config.tmp_dir, "TMPDIR must point inside the application")
    expect_equal(env["HOME"], config.home_dir, "HOME must point inside the application")
    expect_equal(env["XDG_RUNTIME_DIR"], config.run_dir, "XDG_RUNTIME_DIR was not set")
    expect_equal(env["GDK_BACKEND"], "x11", "GTK must be pinned to the X11 backend")

    # Every relocated path must be inside the application root, or the bundle
    # is not actually relocatable and will reach for the build host.
    for name in (
        "LD_LIBRARY_PATH",
        "GTK_PATH",
        "GTK_IM_MODULE_FILE",
        "GDK_PIXBUF_MODULE_FILE",
        "GSETTINGS_SCHEMA_DIR",
        "FONTCONFIG_FILE",
    ):
        value = env.get(name, "")
        expect(value, "%s was not set" % name)
        for part in value.split(os.pathsep):
            expect(
                part.startswith(config.root),
                "%s points outside the application: %s" % (name, part),
            )


@test
def child_environment_has_no_display_when_unspecified():
    from lib.config import config

    env = bundle.child_environment(config)
    expect("DISPLAY" not in env, "DISPLAY must not be set before the display number is known")


# --------------------------------------------------------------------------- #
# runner
# --------------------------------------------------------------------------- #

def main():
    passed = 0
    failed = []

    for function in _results:
        name = function.__name__.replace("_", " ")
        try:
            function()
        except Exception as error:
            failed.append((name, error))
            print("  FAIL  %s" % name)
            if VERBOSE:
                traceback.print_exc()
            else:
                print("        %s" % error)
        else:
            passed += 1
            if VERBOSE:
                print("  ok    %s" % name)

    print("-" * 62)
    print("passed: %d   failed: %d   total: %d" % (passed, len(failed), len(_results)))
    if failed:
        print("\nRESULT: FAILED")
        return 1
    print("\nRESULT: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
