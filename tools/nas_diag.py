#!/usr/bin/env python3
"""Diagnose a TOS device before or after an installation attempt.

A maintainer tool. It is not part of the .deb payload and must never contain a
device credential in its source, because this repository is the input to the
packaging pipeline and an inline credential in it would be a release blocker.

Credentials come from the environment:

    DTOS_HOST, DTOS_PORT, DTOS_USER, DTOS_PASS

Usage:
    DTOS_HOST=10.18.15.122 DTOS_PORT=9222 DTOS_USER=admin DTOS_PASS=... \
        python3 tools/nas_diag.py
    python3 tools/nas_diag.py --cmd "id"
    python3 tools/nas_diag.py --script some.sh

Run it with no arguments and it prints a report that can be pasted whole into a
bug report.
"""

import argparse
import os
import sys

APP_ID = "tos-darktable"
PORT = 9312


def _setting(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    if default is not None:
        return default
    raise SystemExit(
        "%s is not set. Export DTOS_HOST / DTOS_PORT / DTOS_USER / DTOS_PASS "
        "before running this utility." % name
    )


def connect():
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=_setting("DTOS_HOST"),
        port=int(_setting("DTOS_PORT", "9222")),
        username=_setting("DTOS_USER"),
        password=_setting("DTOS_PASS"),
        timeout=20,
        banner_timeout=30,
        auth_timeout=30,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def run(client, command, timeout=60):
    _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, out, err


DIAGNOSTICS = [
    ("host", "uname -a; echo; cat /etc/os-release 2>/dev/null | head -4; echo; getconf GNU_LIBC_VERSION"),

    ("installed", "dpkg -l | grep -i %s || echo 'not installed'" % APP_ID),

    ("install-dir", "ls -la /usr/local/%s/ 2>&1 | head -30" % APP_ID),

    # The two trees the build pipeline produces. Their absence is the whole
    # explanation for a service that starts and cannot serve anything.
    ("runtime-bundle", "ls -d /usr/local/%s/app /usr/local/%s/depends 2>&1; "
                       "echo '--- app/bin ---'; ls /usr/local/%s/app/bin 2>&1; "
                       "echo '--- depends ---'; ls /usr/local/%s/depends 2>&1"
                       % (APP_ID, APP_ID, APP_ID, APP_ID)),

    ("service", "systemctl status %s --no-pager -l 2>&1 | head -30" % APP_ID),

    ("service-state", "echo -n 'is-active : '; systemctl is-active %s 2>&1; "
                      "echo -n 'is-enabled: '; systemctl is-enabled %s 2>&1; "
                      "echo -n 'restarts  : '; systemctl show %s -p NRestarts --value 2>&1"
                      % (APP_ID, APP_ID, APP_ID)),

    ("journal", "journalctl -u %s --no-pager -n 60 2>&1" % APP_ID),

    ("launcher-log", "tail -40 /usr/local/%s/logs/launcher.log 2>&1" % APP_ID),

    ("launcher-check", "/usr/local/%s/bin/darktable-server --check 2>&1 || echo \"exit=$?\"" % APP_ID),

    ("password", "ls -la /usr/local/%s/data/access.txt 2>&1" % APP_ID),

    ("listening", "ss -tlnp 2>/dev/null | grep -E ':%d|LISTEN' | head -20" % PORT),

    # Architecture question 1: can an X server publish its socket at all?
    # The path is fixed by the X11 protocol, so if it is not writable there is
    # no alternative and the whole design has to change.
    ("tmp-x11", "ls -ld /tmp /tmp/.X11-unix 2>&1; "
                "echo '--- can we create it? ---'; "
                "mkdir -p /tmp/.X11-unix 2>&1 && echo 'mkdir: ok' || echo 'mkdir: FAILED'; "
                "ls -ld /tmp/.X11-unix 2>&1"),

    ("x-server-test", "which Xvfb x11vnc xauth 2>&1; "
                      "echo '--- starting a throwaway display ---'; "
                      "(Xvfb :77 -screen 0 320x240x24 -nolisten tcp >/tmp/xvfb-test.log 2>&1 & "
                      "sleep 3; ls -la /tmp/.X11-unix/ 2>&1; pkill -f 'Xvfb :77'; "
                      "echo '--- xvfb log ---'; cat /tmp/xvfb-test.log) 2>&1"),

    # Architecture question 2: does the platform substitute ${ip} in
    # config.ini, or copy it through literally? The guide contradicts itself
    # (8.4.2 says use the placeholder, 8.4.3's table says use /<appid>/), and
    # any already-installed external-open application answers it directly.
    ("other-apps", "for d in /usr/local/*/config.ini; do "
                   "[ -f \"$d\" ] || continue; "
                   "echo \"--- $d\"; grep -E '\"(id|path|type|open_path|application_type)\"' \"$d\" 2>/dev/null; "
                   "done"),

    ("nginx-routes", "ls -la /etc/nginx/conf.d/ 2>&1 | head; "
                     "echo '--- AppAccessControl ---'; "
                     "ls -la /etc/nginx/AppAccessControl/ 2>&1 | head -20"),

    ("www-links", "ls -la /usr/www/ 2>&1 | head -20"),

    ("shares", "ls -la /Volume1/ 2>&1 | head -20; "
               "echo '--- ter_share_add ---'; "
               "which ter_share_add 2>&1; ter_share_add --help 2>&1 | head -10"),

    ("tz-and-tools", "echo -n 'python3 : '; python3 --version 2>&1; "
                     "echo -n 'nginx   : '; nginx -v 2>&1; "
                     "echo -n 'ter_cmd : '; which ter_share_add ter_user_add 2>&1"),

    ("users", "id %s 2>&1; getent group allusers 2>&1" % APP_ID),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cmd", help="run one command")
    parser.add_argument("--script", help="run a local shell script")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()

    client = connect()
    try:
        if args.cmd:
            code, out, err = run(client, args.cmd, args.timeout)
            sys.stdout.write("exit=%s\n--- stdout ---\n%s\n--- stderr ---\n%s\n" % (code, out, err))
            return 0

        if args.script:
            with open(args.script, "r", encoding="utf-8") as handle:
                command = handle.read()
            code, out, err = run(client, command, args.timeout)
            sys.stdout.write("exit=%s\n--- stdout ---\n%s\n--- stderr ---\n%s\n" % (code, out, err))
            return 0

        for label, command in DIAGNOSTICS:
            code, out, err = run(client, command, args.timeout)
            sys.stdout.write("\n" + "=" * 70 + "\n")
            sys.stdout.write("### %s (exit=%s)\n" % (label, code))
            sys.stdout.write("=" * 70 + "\n")
            sys.stdout.write(out)
            if err.strip():
                sys.stdout.write("--- stderr ---\n" + err)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
