#!/usr/bin/env python3
"""Build the TOS 7 application package for darktable.

Produces a Debian package laid out the way the platform's metadata parser
expects:

    DEBIAN/{control,preinst,postinst,prerm,postrm}
    usr/local/tos-darktable/
        config.ini, tos-darktable.lang, tos-darktable.env, webui.bz2
        bin/darktable-server          our launcher
        lib/*.py                      our launcher's modules
        images/icons/tos-darktable.svg
        init.d/tos-darktable.service
        nginx/tos-darktable.conf
        app/                          darktable's own install tree   (--stage)
        depends/                      the bundled runtime closure    (--stage)

The two large trees come from the build pipeline rather than from the
repository: ``app/`` is what cmake installed and ``depends/`` is what
ci/collect_deps.py collected. Passing ``--stage`` merges them in. Without it
the package is assembled without a runtime, which is enough to exercise the
metadata and the lifecycle scripts and is how the pipeline is tested before a
45-minute darktable build is spent.

Every text file is forced to LF before packaging. CRLF inside a payload is the
most common cause of the platform rejecting a package with "bad interpreter",
and a Windows checkout is exactly where it comes from.

Usage:
    python tools/build.py --platform x86_64
    python tools/build.py --platform x86_64 --stage /path/to/stage
    python tools/build.py --no-deb            # assemble the tree only
"""

import argparse
import bz2
import gzip
import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ID = "tos-darktable"
SYSTEM_ID = "tos-darktable"

BUILD_DIR = os.path.join(REPO_ROOT, "build")
DIST_DIR = os.path.join(REPO_ROOT, "dist")

SOURCE_APP_ROOT = os.path.join(REPO_ROOT, "usr", "local", APP_ID)
SOURCE_DEBIAN = os.path.join(REPO_ROOT, "DEBIAN")
SOURCE_WEBUI = os.path.join(REPO_ROOT, "webui")

# Directories copied from the repository. app/ and depends/ are not here: they
# are build products, merged in from --stage.
PAYLOAD_DIRS = ("bin", "lib", "images", "init.d", "nginx")

# Never shipped.
EXCLUDE_DIRS = {"__pycache__", ".git", ".github", "node_modules", ".workbuddy"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".orig", ".rej", ".log", ".swp")

# Files whose line endings matter to an interpreter or to systemd. Only these
# are scanned and rewritten; the runtime payload holds tens of thousands of
# files that are either binary or do not care.
TEXT_EXTENSIONS = (
    ".sh", ".py", ".ini", ".lang", ".service", ".conf", ".env",
    ".js", ".css", ".html", ".json", ".txt", ".md",
)


def log(message):
    print("[build] %s" % message, flush=True)


def remove_tree(path):
    """Remove a tree, tolerating a platform that intercepts deep deletions."""
    if not os.path.isdir(path):
        return
    if os.name == "nt":
        subprocess.call(["cmd", "/c", "rmdir", "/s", "/q", path])
    else:
        subprocess.call(["rm", "-rf", path])
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def force_lf(path):
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    if b"\r\n" not in data:
        return False
    with open(path, "wb") as handle:
        handle.write(data.replace(b"\r\n", b"\n"))
    return True


def normalise_tree(root):
    """Force LF across the text files of a tree. Returns the converted count."""
    converted = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item not in EXCLUDE_DIRS]
        for name in files:
            if not name.endswith(TEXT_EXTENSIONS):
                continue
            if force_lf(os.path.join(base, name)):
                converted += 1
                log("normalised CRLF -> LF: %s" % os.path.relpath(os.path.join(base, name), REPO_ROOT))
    return converted


def copy_tree(source, destination):
    """Copy a tree, dropping build artefacts."""
    copied = 0
    for base, dirs, files in os.walk(source):
        dirs[:] = [item for item in dirs if item not in EXCLUDE_DIRS]
        relative = os.path.relpath(base, source)
        target_dir = destination if relative == "." else os.path.join(destination, relative)
        os.makedirs(target_dir, exist_ok=True)
        for name in files:
            if name.endswith(EXCLUDE_SUFFIXES):
                continue
            source_file = os.path.join(base, name)
            target_file = os.path.join(target_dir, name)
            shutil.copy2(source_file, target_file)
            # A source file that is group- or world-writable would ship a
            # payload any local user could edit. Only checked where POSIX modes
            # are meaningful: on Windows every file reads as 0666 and rewriting
            # modes from that reading is what loses the execute bit.
            if os.name != "nt":
                mode = os.stat(source_file).st_mode & 0o777
                if mode & 0o022:
                    os.chmod(target_file, 0o755 if mode & 0o111 else 0o644)
            copied += 1
    return copied


def pack_webui(source_dir, destination_bz2):
    """Compress the frontend into webui.bz2.

    bzip2, not xz: the platform unpacks this file with ``tar -xjf``. The
    archive holds plain files at its top level so that index.html lands beside
    the other payload files.
    """
    if not os.path.isdir(source_dir):
        raise SystemExit("webui source directory not found: %s" % source_dir)
    if not os.path.isfile(os.path.join(source_dir, "index.html")):
        raise SystemExit("webui/index.html is required")

    entries = []
    for base, dirs, files in os.walk(source_dir):
        dirs[:] = sorted(item for item in dirs if item not in EXCLUDE_DIRS)
        for name in sorted(files):
            if name.endswith(EXCLUDE_SUFFIXES):
                continue
            path = os.path.join(base, name)
            entries.append((path, os.path.relpath(path, source_dir)))

    # Deterministic: sorted, fixed mtime, no owner metadata. Two builds of the
    # same source must produce the same bytes or the checksum is meaningless.
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path, arcname in sorted(entries, key=lambda item: item[1]):
            info = archive.gettarinfo(path, arcname=arcname)
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.mode = 0o644
            with open(path, "rb") as handle:
                archive.addfile(info, handle)

    with open(destination_bz2, "wb") as sink:
        sink.write(bz2.compress(buffer.getvalue(), compresslevel=9))

    size = os.path.getsize(destination_bz2)
    log("webui.bz2 packed: %d file(s), %.1f MB" % (len(entries), size / (1024 * 1024)))
    return size


def syntax_check(root):
    """Compile every packaged Python module so a syntax error cannot ship."""
    failures = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [item for item in dirs if item not in EXCLUDE_DIRS]
        for name in files:
            if not name.endswith(".py") and name != "darktable-server":
                continue
            path = os.path.join(base, name)
            result = subprocess.run(
                [sys.executable, "-m", "py_compile", path], capture_output=True, text=True
            )
            if result.returncode != 0:
                failures.append("%s: %s" % (path, (result.stderr or "").strip()))
    if failures:
        for item in failures:
            log("SYNTAX ERROR %s" % item)
        raise SystemExit("python syntax check failed")
    log("python syntax check passed")


def read_version():
    import json

    with open(os.path.join(SOURCE_APP_ROOT, "config.ini"), "r", encoding="utf-8") as handle:
        return json.load(handle).get("version", "1.0.0")


def write_control(control_path, platform, version, installed_size):
    architecture = "amd64" if platform == "x86_64" else "arm64"
    with open(os.path.join(SOURCE_DEBIAN, "control"), "r", encoding="utf-8") as handle:
        text = handle.read()

    lines = []
    for line in text.splitlines():
        if line.startswith("Architecture:"):
            lines.append("Architecture: %s" % architecture)
        elif line.startswith("Version:"):
            lines.append("Version: %s" % version)
        elif line.startswith("Installed-Size:"):
            lines.append("Installed-Size: %d" % installed_size)
        else:
            lines.append(line)

    with open(control_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def make_executable(path):
    mode = os.stat(path).st_mode
    os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


# Payload directories whose contents are programs by construction.
EXECUTABLE_DIRS = (
    "usr/local/%s/bin/" % APP_ID,
    "usr/local/%s/depends/bin/" % APP_ID,
    "usr/local/%s/app/bin/" % APP_ID,
)


def needs_exec_bit(arcname, path):
    """Decide whether a payload file must be executable.

    Decided from the path and the file's own content, never from the source
    file's permission bits. On Windows ``os.stat`` reports no execute bit at
    all for a file without a recognised extension, so a perfectly executable
    ``bin/darktable-server`` reads as 0644 there and would be packaged
    non-executable - a package that installs cleanly and then cannot start.
    """
    base = arcname.rsplit("/", 1)[-1]
    if base in ("preinst", "postinst", "prerm", "postrm"):
        return True
    if arcname.startswith(EXECUTABLE_DIRS):
        return True
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    # An ELF binary, or a script with an interpreter line.
    return head[:4] == b"\x7fELF" or head[:2] == b"#!"


def measure_size(root):
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return max(1, total // 1024)


def assemble(platform, version, stage=None, with_webui=True):
    """Create the package tree under build/tos-darktable/."""
    root = os.path.join(BUILD_DIR, APP_ID)
    remove_tree(root)

    debian = os.path.join(root, "DEBIAN")
    payload = os.path.join(root, "usr", "local", APP_ID)
    os.makedirs(debian, exist_ok=True)
    os.makedirs(payload, exist_ok=True)

    log("assembling package tree at %s" % root)

    for name in ("preinst", "postinst", "prerm", "postrm"):
        source = os.path.join(SOURCE_DEBIAN, name)
        if not os.path.isfile(source):
            raise SystemExit("missing lifecycle script: %s" % source)
        target = os.path.join(debian, name)
        shutil.copy2(source, target)
        make_executable(target)

    for item in PAYLOAD_DIRS:
        source = os.path.join(SOURCE_APP_ROOT, item)
        if os.path.isdir(source):
            count = copy_tree(source, os.path.join(payload, item))
            log("  %-10s %d file(s)" % (item + "/", count))

    for name in ("config.ini", "%s.lang" % APP_ID, "%s.env" % APP_ID):
        source = os.path.join(SOURCE_APP_ROOT, name)
        if not os.path.isfile(source):
            if name.endswith(".env"):
                continue
            raise SystemExit("missing required payload file: %s" % source)
        shutil.copy2(source, os.path.join(payload, name))

    # The runtime trees, when the pipeline produced them.
    if stage:
        for item in ("app", "depends"):
            source = os.path.join(stage, "usr", "local", APP_ID, item)
            if not os.path.isdir(source):
                log("  %-10s not present in the stage; skipping" % (item + "/"))
                continue
            destination = os.path.join(payload, item)
            if os.path.isdir(destination):
                remove_tree(destination)
            shutil.copytree(source, destination, symlinks=False)
            count = sum(len(files) for _b, _d, files in os.walk(destination))
            log("  %-10s %d file(s)" % (item + "/", count))
    else:
        log("  no --stage given: assembling without the runtime bundle")

    if with_webui:
        pack_webui(SOURCE_WEBUI, os.path.join(payload, "webui.bz2"))

    make_executable(os.path.join(payload, "bin", "darktable-server"))

    # The bundled executables have to keep their execute bit, and the copy
    # above may have gone through a filesystem that cannot record one.
    for extra in (
        os.path.join(payload, "depends", "bin"),
        os.path.join(payload, "app", "bin"),
    ):
        if not os.path.isdir(extra):
            continue
        for name in os.listdir(extra):
            path = os.path.join(extra, name)
            if os.path.isfile(path):
                make_executable(path)

    # Only the text files this repository authors are normalised. The runtime
    # trees are tens of thousands of files and are either binary or do not care.
    normalise_tree(payload)
    normalise_tree(debian)

    installed_size = measure_size(payload)
    write_control(os.path.join(debian, "control"), platform, version, installed_size)

    log("Installed-Size: %d KB (%.1f MB)" % (installed_size, installed_size / 1024.0))
    return root


# --------------------------------------------------------------------------- #
# .deb writers
# --------------------------------------------------------------------------- #


def build_deb(root, platform, version):
    os.makedirs(DIST_DIR, exist_ok=True)
    package = "%s_%s.deb" % (APP_ID, platform)
    output = os.path.join(DIST_DIR, package)

    binary = shutil.which("dpkg-deb")
    if binary:
        # -Zxz: the Python writer below can only produce gzip, and on a payload
        # this size that difference is well over a hundred megabytes.
        result = subprocess.run(
            [binary, "--build", "--root-owner-group", "-Zxz", root, output],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit("dpkg-deb failed: %s" % (result.stderr or result.stdout))
        log("built %s with dpkg-deb" % package)
    else:
        log("dpkg-deb is unavailable on this host; using the built-in writer")
        _manual_deb(root, output)
        log("built %s with the built-in writer" % package)

    _write_checksum(output)
    _inspect(output)
    return output


def _manual_deb(root, output):
    """Write a valid ar archive holding control.tar.gz and data.tar.gz.

    The layout mirrors ``dpkg-deb --build``: control.tar.gz holds DEBIAN/ with
    the ``./`` prefix dpkg expects, data.tar.gz holds everything else.
    """

    def add_entries(archive, source_root, skip_debian):
        entries = []
        for base, dirs, files in os.walk(source_root):
            dirs[:] = sorted(item for item in dirs if item not in EXCLUDE_DIRS)
            if skip_debian and base == source_root:
                dirs[:] = [item for item in dirs if item != "DEBIAN"]
            relative = os.path.relpath(base, source_root)
            if relative == ".":
                relative = ""
            for name in dirs:
                entries.append((os.path.join(base, name), "%s/%s" % (relative, name) if relative else name, True))
            for name in sorted(files):
                if name.endswith(EXCLUDE_SUFFIXES):
                    continue
                entries.append((os.path.join(base, name), "%s/%s" % (relative, name) if relative else name, False))

        for path, arcname, is_dir in sorted(entries, key=lambda item: item[1]):
            info = archive.gettarinfo(path, arcname="./" + arcname)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            # Pinned so the archive is reproducible; gettarinfo would stamp the
            # real mtime and the checksum would change on every build.
            info.mtime = 0
            if is_dir:
                info.mode = 0o755
                info.size = 0
                archive.addfile(info)
            else:
                # Mode decided from the path and the file's content, because
                # Windows reports 0666 for every regular file and a package
                # built there would otherwise ship the payload world-writable
                # and its launcher non-executable.
                info.mode = 0o755 if needs_exec_bit(arcname, path) else 0o644
                with open(path, "rb") as handle:
                    archive.addfile(info, handle)

    def tar_bytes(source_root, skip_debian):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
            root_info = tarfile.TarInfo("./")
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            root_info.mtime = 0
            archive.addfile(root_info)
            add_entries(archive, source_root, skip_debian)
        return gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0)

    control_tar = tar_bytes(os.path.join(root, "DEBIAN"), skip_debian=False)
    data_tar = tar_bytes(root, skip_debian=True)

    def ar_entry(name, payload):
        header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, 0, 0, 0, "100644", len(payload))
        out = header.encode("ascii") + payload
        if len(payload) % 2:
            out += b"\n"
        return out

    with open(output, "wb") as handle:
        handle.write(b"!<arch>\n")
        handle.write(ar_entry("debian-binary", b"2.0\n"))
        handle.write(ar_entry("control.tar.gz", control_tar))
        handle.write(ar_entry("data.tar.gz", data_tar))


def _write_checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    checksum = digest.hexdigest()
    with open(path + ".sha256", "w", encoding="ascii", newline="\n") as handle:
        handle.write("%s  %s\n" % (checksum, os.path.basename(path)))
    log("sha256 %s" % checksum)
    return checksum


def _inspect(path):
    """Verify the metadata sits where the platform's parser reads it."""
    binary = shutil.which("dpkg-deb")
    if binary:
        result = subprocess.run([binary, "-c", path], capture_output=True, text=True)
        listing = result.stdout or ""
        required = [
            "usr/local/%s/config.ini" % APP_ID,
            "usr/local/%s/%s.lang" % (APP_ID, APP_ID),
            "usr/local/%s/images/icons/%s.svg" % (APP_ID, APP_ID),
            "usr/local/%s/webui.bz2" % APP_ID,
            "usr/local/%s/bin/darktable-server" % APP_ID,
            "usr/local/%s/init.d/%s.service" % (APP_ID, SYSTEM_ID),
        ]
        missing = [item for item in required if item not in listing]
        if missing:
            raise SystemExit(
                "package inspection failed, missing inside data.tar: %s" % ", ".join(missing)
            )
        log("package inspection passed: metadata is under usr/local/%s/" % APP_ID)
        return

    sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
    try:
        import inspect_deb
    except ImportError:
        log("skipping package inspection (no reader available)")
        return
    errors, _notes = inspect_deb.inspect(path, app_id=APP_ID, verbose=False)
    if errors:
        raise SystemExit("package inspection failed: %s" % "; ".join(errors))
    log("package inspection passed: ar container and both tars verified")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the TOS 7 package for darktable")
    parser.add_argument("--platform", choices=("x86_64", "aarch64"), default="x86_64")
    parser.add_argument("--stage", help="DESTDIR produced by ci/build-darktable.sh")
    parser.add_argument("--no-deb", action="store_true", help="assemble the tree only")
    parser.add_argument("--no-webui", action="store_true", help="skip webui.bz2 packing")
    parser.add_argument("--skip-validate", action="store_true")
    args = parser.parse_args(argv)

    # Normalise before anything is compressed: CRLF inside a shell script or a
    # systemd unit is the single most common reason a submitted package is
    # rejected, and once webui.bz2 is built it is too late to fix.
    normalise_tree(os.path.join(REPO_ROOT, "DEBIAN"))
    normalise_tree(SOURCE_APP_ROOT)
    normalise_tree(SOURCE_WEBUI)
    normalise_tree(os.path.join(REPO_ROOT, "tools"))
    normalise_tree(os.path.join(REPO_ROOT, "ci"))

    if not args.skip_validate:
        log("running the pre-build validator")
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", "validate.py")], cwd=REPO_ROOT
        )
        if result.returncode != 0:
            raise SystemExit("validation failed - aborting the build")

    version = read_version()
    log("version %s, platform %s" % (version, args.platform))

    syntax_check(SOURCE_APP_ROOT)

    root = assemble(args.platform, version, stage=args.stage, with_webui=not args.no_webui)
    if args.no_deb:
        log("package tree ready at %s" % root)
        return 0

    package = build_deb(root, args.platform, version)
    if package:
        log("done: %s" % package)
    return 0


if __name__ == "__main__":
    sys.exit(main())
