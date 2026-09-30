#!/usr/bin/env python3
"""Pure-Python inspector for the built Debian package.

``dpkg-deb`` is not available on Windows and is often absent from a developer
workstation, so ``tools/build.py`` can produce the archive with its own ar/tar
writer. This module reads the archive back and verifies the structure the TOS
platform parser depends on:

  * the ar container holds exactly ``debian-binary``, ``control.tar.gz`` and
    ``data.tar.gz``, in that order
  * ``debian-binary`` contains ``2.0``
  * ``control.tar.gz`` holds ``./control`` and the lifecycle scripts, and the
    scripts are executable
  * ``data.tar.gz`` never contains the ``DEBIAN`` directory
  * the metadata sits under ``usr/local/<appid>/`` inside ``data.tar``, never
    at the archive root - this is the single most common fatal packaging error
  * no payload file is group- or world-writable

Usage:
    python tools/inspect_deb.py dist/tos-darktable_x86_64.deb
"""

import io
import os
import sys
import tarfile

AR_MAGIC = b"!<arch>\n"

APP_ID = "tos-darktable"

REQUIRED_DATA_FILES = (
    "usr/local/%s/config.ini" % APP_ID,
    "usr/local/%s/%s.lang" % (APP_ID, APP_ID),
    "usr/local/%s/%s.env" % (APP_ID, APP_ID),
    "usr/local/%s/webui.bz2" % APP_ID,
    "usr/local/%s/images/icons/%s.svg" % (APP_ID, APP_ID),
    "usr/local/%s/init.d/%s.service" % (APP_ID, APP_ID),
    "usr/local/%s/nginx/%s.conf" % (APP_ID, APP_ID),
    "usr/local/%s/bin/darktable-server" % APP_ID,
)

REQUIRED_CONTROL_FILES = ("control", "preinst", "postinst", "prerm", "postrm")

EXECUTABLE_IN_DATA = (
    "usr/local/%s/bin/darktable-server" % APP_ID,
)

# The payload is tens of thousands of files. Printing all of them buries the
# result, so the listing is capped and the cap is stated.
LISTING_LIMIT = 60


class InspectionError(Exception):
    pass


def read_ar(path):
    """Return an ordered list of (name, payload) from an ar archive."""
    with open(path, "rb") as handle:
        data = handle.read()
    if not data.startswith(AR_MAGIC):
        raise InspectionError("not an ar archive (missing !<arch> magic)")

    entries = []
    offset = len(AR_MAGIC)
    while offset + 60 <= len(data):
        header = data[offset:offset + 60]
        if header[58:60] != b"`\n":
            raise InspectionError("corrupt ar header at offset %d" % offset)
        name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii", "replace").strip() or "0")
        except ValueError:
            raise InspectionError("invalid ar size field for %r" % name)
        start = offset + 60
        entries.append((name, data[start:start + size]))
        offset = start + size
        if size % 2:
            offset += 1

    if not entries:
        raise InspectionError("ar archive contains no members")
    return entries


def normalise(name):
    cleaned = name.replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def tar_members(compressed):
    """Stream a gzipped tar and return (name, size, mode, is_dir, is_link).

    Decompression is streamed rather than done in one call: the data archive
    holds several hundred megabytes once expanded, and materialising that in
    memory to inspect a few headers would be wasteful at best.
    """
    members = []
    with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz") as archive:
        for member in archive:
            members.append((
                normalise(member.name),
                member.size,
                member.mode,
                member.isdir(),
                member.islnk() or member.issym(),
            ))
    return members


def inspect(path, app_id=APP_ID, verbose=True):
    errors = []
    notes = []

    entries = read_ar(path)
    names = [name for name, _ in entries]

    if names[:1] != ["debian-binary"]:
        errors.append("the first ar member must be debian-binary, found %r" % (names[:1] or None))
    if names != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        errors.append("unexpected ar members or order: %s" % ", ".join(names))
    if errors:
        return errors, notes

    payloads = dict(entries)
    if payloads["debian-binary"].strip() != b"2.0":
        errors.append("debian-binary must contain 2.0")
    else:
        notes.append("debian-binary version is 2.0")

    control_members = tar_members(payloads["control.tar.gz"])
    control_files = [item[0] for item in control_members if not item[3]]

    for required in REQUIRED_CONTROL_FILES:
        if required not in control_files:
            errors.append("control.tar.gz is missing %s" % required)
    notes.append("control.tar.gz holds %d file(s)" % len(control_files))

    for name, _size, mode, is_dir, _link in control_members:
        if is_dir:
            continue
        if name in ("preinst", "postinst", "prerm", "postrm") and not (mode & 0o111):
            errors.append("lifecycle script %s is not executable (mode %o)" % (name, mode))

    data_members = tar_members(payloads["data.tar.gz"])
    all_names = [item[0] for item in data_members]
    data_files = [item[0] for item in data_members if not item[3]]

    if any(name == "DEBIAN" or name.startswith("DEBIAN/") for name in all_names):
        errors.append("data.tar.gz must not contain the DEBIAN directory")
    else:
        notes.append("data.tar.gz excludes DEBIAN")

    for required in REQUIRED_DATA_FILES:
        if required not in data_files:
            errors.append("data.tar.gz is missing %s" % required)
    notes.append("data.tar.gz holds %d file(s)" % len(data_files))

    # The metadata must not sit at the archive root. The platform reads
    # config.ini out of data.tar at usr/local/<appid>/, and a copy at the root
    # is both ignored and a sign that the tree was assembled wrong.
    for stray in ("config.ini", "%s.lang" % app_id):
        if stray in data_files:
            errors.append("metadata %s is at the package root instead of usr/local/%s/" % (stray, app_id))

    for name, _size, mode, is_dir, _link in data_members:
        if is_dir or name not in EXECUTABLE_IN_DATA:
            continue
        if not (mode & 0o111):
            errors.append("%s is not executable (mode %o)" % (name, mode))

    world_writable = [
        (name, mode)
        for name, _size, mode, is_dir, _link in data_members
        if not is_dir and mode & 0o022
    ]
    if world_writable:
        for name, mode in world_writable[:10]:
            errors.append("payload file %s is group/world-writable (mode %o)" % (name, mode))
    else:
        notes.append("no payload file is group- or world-writable")

    if verbose:
        print("deb inspection - %s" % os.path.basename(path))
        print("=" * 62)
        for note in notes:
            print("  [OK  ] %s" % note)
        for item in errors:
            print("  [FAIL] %s" % item)
        print("-" * 62)
        print("data.tar.gz members (first %d of %d):" % (min(LISTING_LIMIT, len(data_files)), len(data_files)))
        for name in sorted(data_files)[:LISTING_LIMIT]:
            print("    %s" % name)
        if len(data_files) > LISTING_LIMIT:
            print("    ... and %d more" % (len(data_files) - LISTING_LIMIT))
        print("-" * 62)
        print("errors: %d" % len(errors))

    return errors, notes


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        dist = os.path.join(here, "dist")
        candidates = []
        if os.path.isdir(dist):
            candidates = [
                os.path.join(dist, name) for name in sorted(os.listdir(dist)) if name.endswith(".deb")
            ]
        if not candidates:
            print("usage: python tools/inspect_deb.py <package.deb>")
            return 2
        target = candidates[-1]
    else:
        target = argv[0]

    if not os.path.isfile(target):
        print("package not found: %s" % target)
        return 2

    errors, _notes = inspect(target)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
