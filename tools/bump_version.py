#!/usr/bin/env python3
"""Single source of truth for the application version.

The version literal is needed in five places that cannot import each other:
the package metadata the platform reads, the Debian control file, the launcher
module that reports it over /health, the cache-buster query strings in the web
interface, and the banner the upgrade path in postinst prints. Keeping them in
sync by hand is how a stale ``?v=`` survives a release and the browser keeps
serving the previous stylesheet, which makes a real fix look like it never
shipped; or how the App Center keeps advertising the previous version.

So: ``VERSION`` at the repository root is the only place the number is
authored. Every other occurrence is generated from it.

Usage:
    python tools/bump_version.py --bump patch     # 1.0.0 -> 1.0.1, then sync
    python tools/bump_version.py --set 2.0.0
    python tools/bump_version.py --sync
    python tools/bump_version.py --check          # non-zero on drift
"""

import argparse
import io
import os
import re
import sys

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TOOLS_DIR)

APP_ID = "tos-darktable"
VERSION_FILE = os.path.join(REPO_ROOT, "VERSION")
CONFIG_INI = os.path.join(REPO_ROOT, "usr", "local", APP_ID, "config.ini")
BACKEND_INIT = os.path.join(REPO_ROOT, "usr", "local", APP_ID, "lib", "__init__.py")
CONTROL = os.path.join(REPO_ROOT, "DEBIAN", "control")
POSTINST = os.path.join(REPO_ROOT, "DEBIAN", "postinst")
INDEX_HTML = os.path.join(REPO_ROOT, "webui", "index.html")

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def read_version():
    with io.open(VERSION_FILE, "r", encoding="utf-8") as handle:
        return handle.read().strip()


def parse(version):
    match = SEMVER.match(version)
    if not match:
        raise SystemExit("VERSION must be MAJOR.MINOR.PATCH, found %r" % version)
    return tuple(int(part) for part in match.groups())


def bumped(version, level):
    major, minor, patch = parse(version)
    if level == "major":
        return "%d.0.0" % (major + 1)
    if level == "minor":
        return "%d.%d.0" % (major, minor + 1)
    return "%d.%d.%d" % (major, minor, patch + 1)


# --------------------------------------------------------------------------- #
# Writers. Each returns the number of replacements, and raises when its anchor
# has disappeared - a silent no-op here would reintroduce the drift this module
# exists to prevent.
# --------------------------------------------------------------------------- #


def _sub(path, pattern, replacement, expected, flags=0):
    text = io.open(path, "r", encoding="utf-8").read()
    new, count = re.subn(pattern, replacement, text, flags=flags)
    if count != expected:
        raise SystemExit(
            "%s: expected %d match(es) for the version, found %d"
            % (os.path.relpath(path, REPO_ROOT), expected, count)
        )
    io.open(path, "w", encoding="utf-8", newline="\n").write(new)
    return count


def write_config_ini(version):
    return _sub(
        CONFIG_INI,
        r'("version"\s*:\s*")[^"]*(")',
        lambda m: "%s%s%s" % (m.group(1), version, m.group(2)),
        1,
    )


def write_backend_init(version):
    return _sub(
        BACKEND_INIT,
        r'^(__version__\s*=\s*")[^"]*(")',
        lambda m: "%s%s%s" % (m.group(1), version, m.group(2)),
        1,
        flags=re.M,
    )


def write_control(version):
    return _sub(CONTROL, r"^(Version:\s*)\S+\s*$", lambda m: m.group(1) + version, 1, flags=re.M)


def write_postinst(version):
    return _sub(POSTINST, r"^(VERSION=)[0-9]+\.[0-9]+\.[0-9]+\s*$", lambda m: m.group(1) + version, 1, flags=re.M)


def write_index_html(version):
    return _sub(INDEX_HTML, r"\?v=[0-9]+\.[0-9]+\.[0-9]+", "?v=%s" % version, 2)


SYNCERS = [
    ("usr/local/%s/config.ini" % APP_ID, write_config_ini, 1),
    ("usr/local/%s/lib/__init__.py" % APP_ID, write_backend_init, 1),
    ("DEBIAN/control", write_control, 1),
    ("DEBIAN/postinst", write_postinst, 1),
    ("webui/index.html (?v=)", write_index_html, 2),
]


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #


def collect():
    """Return {label: found_version} for every derived location."""
    found = {}

    text = io.open(CONFIG_INI, "r", encoding="utf-8").read()
    match = re.search(r'"version"\s*:\s*"([^"]*)"', text)
    found["usr/local/%s/config.ini" % APP_ID] = match.group(1) if match else None

    text = io.open(BACKEND_INIT, "r", encoding="utf-8").read()
    match = re.search(r'^__version__\s*=\s*"([^"]*)"', text, flags=re.M)
    found["usr/local/%s/lib/__init__.py" % APP_ID] = match.group(1) if match else None

    text = io.open(CONTROL, "r", encoding="utf-8").read()
    match = re.search(r"^Version:\s*(\S+)\s*$", text, flags=re.M)
    found["DEBIAN/control"] = match.group(1) if match else None

    text = io.open(POSTINST, "r", encoding="utf-8").read()
    match = re.search(r"^VERSION=([0-9]+\.[0-9]+\.[0-9]+)", text, flags=re.M)
    found["DEBIAN/postinst"] = match.group(1) if match else None

    text = io.open(INDEX_HTML, "r", encoding="utf-8").read()
    tags = sorted(set(re.findall(r"\?v=([0-9]+\.[0-9]+\.[0-9]+)", text)))
    found["webui/index.html (?v=)"] = (
        tags[0] if len(tags) == 1 else (None if not tags else ",".join(tags))
    )

    return found


def check(quiet=False):
    version = read_version()
    parse(version)
    found = collect()
    drift = [(label, value) for label, value in found.items() if value != version]

    if not quiet:
        print("VERSION (source of truth): %s" % version)
        for label, value in found.items():
            print("  [%s] %-44s %s" % ("OK " if value == version else "BAD", label, value))

    return version, drift


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bump", choices=["major", "minor", "patch"])
    group.add_argument("--set", dest="explicit", metavar="X.Y.Z")
    group.add_argument("--sync", action="store_true")
    group.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    if args.check:
        _version, drift = check()
        if drift:
            for label, value in drift:
                print("  drift: %s carries %r" % (label, value))
            print("\nRESULT: FAILED - run `python tools/bump_version.py --sync`.")
            return 1
        print("\nRESULT: PASSED - every derived version literal matches VERSION.")
        return 0

    current = read_version()
    if args.bump:
        target = bumped(current, args.bump)
    elif args.explicit:
        target = args.explicit
        parse(target)
    else:
        target = current

    if target != current:
        io.open(VERSION_FILE, "w", encoding="utf-8", newline="\n").write(target + "\n")
        print("VERSION: %s -> %s" % (current, target))
    else:
        print("VERSION: %s (--sync)" % target)

    for label, writer, _expected in SYNCERS:
        count = writer(target)
        print("  synced %-44s (%d)" % (label, count))

    _version, drift = check(quiet=True)
    if drift:
        for label, value in drift:
            print("  STILL DRIFTED: %s -> %r" % (label, value))
        return 1
    print("all derived version literals now read %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
