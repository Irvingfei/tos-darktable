#!/bin/bash
# Install the package into a bare container and prove it actually runs.
#
# This is the only check that proves the bundled runtime is complete. Every
# other check works by inspection; this one runs the application in an
# environment with no GTK, no X server, no VNC server and no darktable
# libraries, so anything the bundle forgot to carry fails here with a concrete
# message from the dynamic loader rather than on a user's NAS.
#
# The container provides python3, dpkg, binutils and curl and nothing else.
#
# Usage:
#   ci/verify-package.sh dist/tos-darktable_x86_64.deb

set -euo pipefail

DEB="${1:?usage: verify-package.sh <package.deb>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PREFIX="/usr/local/tos-darktable"
EXTRACT="/root/out"
ROOT="$EXTRACT$PREFIX"
PORT=9312
PASSWORD="verification-$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"

log()  { printf '\n=== %s\n' "$*"; }
fail() { printf '\n!!! %s\n' "$*" >&2; }

cleanup() {
    if [ -n "${LAUNCHER_PID:-}" ] && kill -0 "$LAUNCHER_PID" 2>/dev/null; then
        kill -TERM "$LAUNCHER_PID" 2>/dev/null || true
        wait "$LAUNCHER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
log "unpacking $DEB"
dpkg-deb -x "$DEB" "$EXTRACT"

if [ ! -x "$ROOT/bin/darktable-server" ]; then
    fail "$ROOT/bin/darktable-server is missing or not executable"
    echo "   The lifecycle scripts set the mode; if dpkg-deb was not what built"
    echo "   this archive, check tools/build.py's exec-bit handling."
    exit 1
fi

# --------------------------------------------------------------------------- #
log "checking the shared library closure"

# The bundle has to be on the search path for this check, exactly as the
# launcher puts it there at runtime.
#
# Without this, ldd resolves against the system, and a bare container has no
# GTK at all - so every bundled library reported its own siblings as missing
# and the check could never pass. It was measuring the wrong thing: the
# question is not "does the system have GTK", it is "is the bundle plus the
# base system sufficient". Only with the bundle on the path does a clean result
# answer that.
export LD_LIBRARY_PATH="$ROOT/depends/lib:$ROOT/app/lib/darktable"

missing_report="$(mktemp)"
count=0
while IFS= read -r -d '' file; do
    count=$((count + 1))
    if ldd "$file" 2>&1 | grep -q 'not found'; then
        {
            echo "--- $file"
            ldd "$file" 2>&1 | grep 'not found'
        } >> "$missing_report"
    fi
done < <(find "$ROOT" -type f \( -name '*.so*' -o -perm -u+x \) -print0)

if [ -s "$missing_report" ]; then
    fail "the bundle is incomplete; $count file(s) checked"
    head -40 "$missing_report"
    echo
    echo "Add the missing packages to ci/collect_deps.py's populate() list, or"
    echo "to the copy trees, and rebuild."
    exit 1
fi
rm -f "$missing_report"
echo "  $count file(s) checked, no unresolved library"

# --------------------------------------------------------------------------- #
log "running the launcher's own check"
"$ROOT/bin/darktable-server" --check

# --------------------------------------------------------------------------- #
log "starting the application"

mkdir -p "$ROOT/data" "$ROOT/webui"
chmod 0700 "$ROOT/data"

# The frontend is unpacked by postinst, which dpkg -x does not run.
if [ -f "$ROOT/webui.bz2" ]; then
    tar -xjf "$ROOT/webui.bz2" -C "$ROOT/webui"
    echo "  unpacked webui.bz2 ($(find "$ROOT/webui" -type f | wc -l) files)"
fi

# The password postinst would have generated.
printf '%s\n' "$PASSWORD" > "$ROOT/data/access.txt"
chmod 0600 "$ROOT/data/access.txt"

export DTOS_LISTEN_PORT="$PORT"
"$ROOT/bin/darktable-server" > /tmp/launcher.log 2>&1 &
LAUNCHER_PID=$!
echo "  launcher pid $LAUNCHER_PID"

# --------------------------------------------------------------------------- #
log "waiting for the health endpoint"

ready=0
for _ in $(seq 1 60); do
    if ! kill -0 "$LAUNCHER_PID" 2>/dev/null; then
        fail "the launcher exited during startup"
        cat /tmp/launcher.log
        exit 1
    fi
    if curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 1
done

if [ "$ready" != "1" ]; then
    fail "the health endpoint did not answer within 60 seconds"
    cat /tmp/launcher.log
    exit 1
fi
echo "  /health answered"

# --------------------------------------------------------------------------- #
log "running the end-to-end verification"

if ! python3 "$HERE/verify_ws.py" --host 127.0.0.1 --port "$PORT" --password "$PASSWORD"; then
    fail "end-to-end verification failed"
    echo "--- launcher log ---"
    cat /tmp/launcher.log
    for log in darktable xvfb x11vnc; do
        if [ -f "$ROOT/logs/$log.log" ]; then
            echo "--- $log.log (last 40 lines) ---"
            tail -40 "$ROOT/logs/$log.log"
        fi
    done
    exit 1
fi

# --------------------------------------------------------------------------- #
log "checking clean shutdown"

kill -TERM "$LAUNCHER_PID"
shutdown_start=$(date +%s)
if ! wait "$LAUNCHER_PID" 2>/dev/null; then
    fail "the launcher exited non-zero on SIGTERM"
    exit 1
fi
LAUNCHER_PID=""
shutdown_seconds=$(( $(date +%s) - shutdown_start ))

if [ "$shutdown_seconds" -gt 8 ]; then
    fail "shutdown took ${shutdown_seconds}s; the systemd unit allows 10s"
    exit 1
fi
echo "  stopped within ${shutdown_seconds}s"

# Nothing may be left behind. systemd enforces this through the cgroup on a
# real device, but a launcher started by hand is in no cgroup at all, which is
# the case this checks.
leftovers="$(pgrep -f "$ROOT" 2>/dev/null || true)"
if [ -n "$leftovers" ]; then
    fail "processes survived the shutdown:"
    ps -o pid,ppid,args -p $leftovers || true
    exit 1
fi
echo "  no processes left behind"

echo
echo "PACKAGE VERIFICATION PASSED"
