#!/usr/bin/env python3
"""Pure-Python inspector for the built Debian package.

``dpkg-deb`` is not available on Windows and is often absent from a developer
workstation, so ``tools/build.py`` can produce the archive with its own ar/tar
writer. This module reads the archive back and verifies the structure the TOS
platform parser depends on:

  * the ar container holds ``debian-binary`` and the two tar members, in that
    order
  * ``debian-binary`` contains ``2.0``
  * the control member holds ``./control`` and the lifecycle scripts, and the
    scripts are executable
  * the data member never contains the ``DEBIAN`` directory
  * the metadata sits under ``usr/local/<appid>/`` inside the data member,
    never at the archive root - this is the most common fatal packaging error
  * no payload file is group- or world-writable

The two tar members are found by prefix, not by exact name, and are opened
with compression auto-detection. ``dpkg-deb`` writes ``control.tar.xz`` and
``data.tar.xz`` for a ``-Zxz`` build while the built-in writer emits
``.tar.gz``, and hard-coding either one means the check only ever runs on the
machine that produced the package it already knew how to read.

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
    """Return (name, size, mode, is_dir, is_link) for a compressed tar.

    Opened with ``r:*`` so the compression is detected from the data rather
    than assumed from the member's extension: dpkg-deb writes .tar.xz for a
    -Zxz build and .tar.gz otherwise, and the reader has to cope with both.

    Decompression is streamed rather than done in one call: the data member
    holds several hundred megabytes once expanded, and materialising that in
    memory to inspect a few headers would be wasteful at best.
    """
    members = []
    with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:*") as archive:
        for member in archive:
            members.append((
                normalise(member.name),
                member.size,
                member.mode,
                member.isdir(),
                member.islnk() or member.issym(),
            ))
    return members


def _find_member(names, prefix):
    """Return the ar member name starting with ``prefix``, or None."""
    for name in names:
        if name.startswith(prefix):
            return name
    return None


def inspect(path, app_id=APP_ID, verbose=True):
    errors = []
    notes = []

    entries = read_ar(path)
    names = [name for name, _ in entries]

    if names[:1] != ["debian-binary"]:
        errors.append("the first ar member must be debian-binary, found %r" % (names[:1] or None))

    control_name = _find_member(names, "control.tar")
    data_name = _find_member(names, "data.tar")

    if control_name is None:
        errors.append("no control.tar member in the ar container: %s" % ", ".join(names))
    if data_name is None:
        errors.append("no data.tar member in the ar container: %s" % ", ".join(names))
    if len(names) != 3:
        errors.append("expected exactly three ar members, found %d: %s" % (len(names), ", ".join(names)))
    if errors:
        return errors, notes

    payloads = dict(entries)
    if payloads["debian-binary"].strip() != b"2.0":
        errors.append("debian-binary must contain 2.0")
    else:
        notes.append("debian-binary version is 2.0")
    notes.append("members: %s" % ", ".join(names))

    control_members = tar_members(payloads[control_name])
    control_files = [item[0] for item in control_members if not item[3]]

    for required in REQUIRED_CONTROL_FILES:
        if required not in control_files:
            errors.append("%s is missing %s" % (control_name, required))
    notes.append("%s holds %d file(s)" % (control_name, len(control_files)))

    for name, _size, mode, is_dir, _link in control_members:
        if is_dir:
            continue
        if name in ("preinst", "postinst", "prerm", "postrm") and not (mode & 0o111):
            errors.append("lifecycle script %s is not executable (mode %o)" % (name, mode))

    data_members = tar_members(payloads[data_name])
    all_names = [item[0] for item in data_members]
    data_files = [item[0] for item in data_members if not item[3]]

    if any(name == "DEBIAN" or name.startswith("DEBIAN/") for name in all_names):
        errors.append("%s must not contain the DEBIAN directory" % data_name)
    else:
        notes.append("%s excludes DEBIAN" % data_name)

    for required in REQUIRED_DATA_FILES:
        if required not in data_files:
            errors.append("%s is missing %s" % (data_name, required))
    notes.append("%s holds %d file(s)" % (data_name, len(data_files)))

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
        print("%s members (first %d of %d):" % (data_name, min(LISTING_LIMIT, len(data_files)), len(data_files)))
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
