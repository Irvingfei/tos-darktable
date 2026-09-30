#!/bin/bash
# Build script for MarkNest for TOS 7.
#
# The real work lives in tools/build.py so that the build behaves identically on
# Linux, macOS and Windows. This wrapper exists because the official template and
# the CI configuration invoke ./build.sh.
#
# Usage:
#   ./build.sh                 # build for x86_64
#   ./build.sh aarch64         # build for aarch64
#   ./build.sh x86_64 --no-deb # assemble the package tree only

set -euo pipefail

PLATFORM="${1:-x86_64}"
shift || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Prefer python3, fall back to python.
if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    echo "error: python 3.10 or newer is required" >&2
    exit 1
fi

case "$PLATFORM" in
    x86_64|aarch64) ;;
    *)
        echo "error: unsupported platform '$PLATFORM' (expected x86_64 or aarch64)" >&2
        exit 1
        ;;
esac

# Force LF across the working copy before assembling, so that a Windows checkout
# cannot leak CRLF into the package.
"$PYTHON" - "$SCRIPT_DIR" <<'PYCODE'
import os
import sys

root = sys.argv[1]
extensions = (".sh", ".py", ".ini", ".lang", ".service", ".conf", ".js", ".css", ".html", ".env")
skip_dirs = {".git", "__pycache__", "build", "dist", ".workbuddy"}
converted = 0
for base, dirs, files in os.walk(root):
    dirs[:] = [item for item in dirs if item not in skip_dirs]
    for name in files:
        if not name.endswith(extensions):
            continue
        path = os.path.join(base, name)
        with open(path, "rb") as handle:
            data = handle.read()
        if b"\r\n" in data:
            with open(path, "wb") as handle:
                handle.write(data.replace(b"\r\n", b"\n"))
            converted += 1
if converted:
    print("[build.sh] normalised %d file(s) to LF" % converted)
PYCODE

exec "$PYTHON" tools/build.py --platform "$PLATFORM" "$@"
