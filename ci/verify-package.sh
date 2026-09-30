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

APPID="tos-darktable"
PREFIX="/usr/local/$APPID"
# Unpacked somewhere world-traversable, not under /root.
#
# /root is mode 0700, so an application account cannot traverse into anything
# beneath it. The package's own directory was fine - ownership and modes were
# correct - but the preparation step's check that the account can reach data/
# failed, correctly, because the account could not get past /root to reach it
# at all. On a device the package lives under /usr/local, whose ancestors are
# traversable, so this was a property of the harness rather than the package.
EXTRACT="/opt/tos-verify"
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

# Everything the launcher and its children wrote.
#
# Each child logs to its own file - the X server's diagnostics are in
# xvfb.log, not in the launcher's - so a startup failure that only printed the
# launcher's log hid the one line that said why. Dumping all of them costs
# nothing when things work and saves a whole build cycle when they do not.
dump_logs() {
    echo "--- launcher log ---"
    cat /tmp/launcher.log 2>/dev/null || echo "(absent)"
    # Two locations, because they differ. The package's own log_dir default is
    # relative to where it was unpacked; the one in tos-darktable.env is the
    # absolute path the service uses on a device. In this container those are
    # /opt/tos-verify/usr/local/... and /usr/local/... respectively, so a dump
    # that checked only one of them found nothing and hid the reason for a
    # failure.
    for base in "$ROOT" "/usr/local/$APPID"; do
        for name in xvfb x11vnc darktable launcher; do
            if [ -f "$base/logs/$name.log" ]; then
                echo "--- $base/logs/$name.log ---"
                cat "$base/logs/$name.log"
                echo
            fi
        done
    done
}

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

# What the bundled keymap data actually looks like. The X server compiles its
# keymap from these files at startup and refuses to start when it cannot, so an
# incomplete copy is fatal - and a file listing is the only honest way to tell
# whether the copy is incomplete or the path is wrong.
XKB_DIR="$ROOT/depends/share/X11/xkb"
echo "  xkb tree: $(find "$XKB_DIR" -maxdepth 1 -type d 2>/dev/null | wc -l) top-level entries, $(find "$XKB_DIR" -type f 2>/dev/null | wc -l) files"
# Per subtree, because the total alone cannot say whether a copy is complete or
# merely smaller than expected. xkb-data on jammy carries roughly a thousand
# files, mostly under symbols/.
for sub in compat geometry keycodes rules symbols types; do
    echo "    $sub: $(find "$XKB_DIR/$sub" -type f 2>/dev/null | wc -l) files"
done
echo "  keycodes/evdev: $(ls -la "$XKB_DIR/keycodes/evdev" 2>&1)"

# --------------------------------------------------------------------------- #
log "running the launcher's own check"
"$ROOT/bin/darktable-server" --check

# --------------------------------------------------------------------------- #
log "creating the application account, as the platform does"

# The platform creates this account during installation, and it gives it the
# shared group allusers as its primary group without creating a group named
# after the application. Reproducing that here is not decoration: the first
# submission on a real device failed with status=216/GROUP precisely because a
# same-named group was assumed to exist, and the preparation step below does
# nothing at all until the account is there.
groupadd -f allusers 2>/dev/null || true
if ! id -u "$APPID" >/dev/null 2>&1; then
    useradd --no-create-home --shell /usr/sbin/nologin --gid allusers "$APPID"
    echo "  created $APPID (group allusers, no same-named group)"
else
    echo "  $APPID already exists"
fi
id "$APPID"

# --------------------------------------------------------------------------- #
log "running the preparation step"

# systemd runs this through ExecStartPre=+ before every start; doing it here
# means the verification covers the same path a device does, including the
# repairs that postinst could not make.
"$ROOT/bin/darktable-server" --prepare || {
    fail "the preparation step failed"
    exit 1
}

# --------------------------------------------------------------------------- #
log "probing the X server directly"

# After preparation, not before: the preparation step is what supplies the
# keymap compiler the X server needs, so probing earlier measured a state no
# device is ever in. The probe uses the same -xkbdir the launcher does, so a
# failure here is the launcher's failure with its output in front of us rather
# than one step removed and written to another file.
PROBE_DISPLAY=77
"$ROOT/depends/bin/Xvfb" ":$PROBE_DISPLAY" -screen 0 320x240x24 -nolisten tcp \
    -xkbdir "$XKB_DIR" > /tmp/xvfb-probe.log 2>&1 &
PROBE_PID=$!
sleep 3
if kill -0 "$PROBE_PID" 2>/dev/null; then
    echo "  Xvfb started and is running"
    kill -TERM "$PROBE_PID" 2>/dev/null || true
    wait "$PROBE_PID" 2>/dev/null || true
else
    echo "  Xvfb exited immediately. Its output:"
    sed 's/^/    /' /tmp/xvfb-probe.log
fi
echo "  /tmp/.X11-unix: $(ls -ld /tmp/.X11-unix 2>&1)"

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
        dump_logs
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
    dump_logs
    exit 1
fi
echo "  /health answered"

# --------------------------------------------------------------------------- #
log "running the end-to-end verification"

if ! python3 "$HERE/verify_ws.py" --host 127.0.0.1 --port "$PORT" --password "$PASSWORD"; then
    fail "end-to-end verification failed"
    dump_logs
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
