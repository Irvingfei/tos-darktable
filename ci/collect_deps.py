#!/usr/bin/env python3
"""Assemble the bundled runtime that ships inside the package.

TOS 7 provides no desktop libraries and darktable needs a newer OpenEXR than
the platform root filesystem carries, so this application brings its own GTK
stack, X server and VNC server. Everything they need has to be collected into
``depends/`` at build time, from a machine that is the same distribution and
release as the device.

Three phases, in order:

``populate``
    Copy the non-library pieces - the X and VNC servers, gdk-pixbuf loaders,
    GTK input modules, GIO modules, schemas, fonts, keyboard data, the lensfun
    database - out of the build container into the staging tree.

``closure``
    Walk ``ldd`` over every ELF that will ship and copy the transitive set of
    shared libraries that are not part of the base system. This is the phase
    that is easy to get wrong, so it is written to be self-verifying: after
    copying a library it re-runs ``ldd`` on *the copy*, with the bundle on the
    search path, so that the loader's own resolution decides what is still
    missing rather than this script's idea of it.

``verify``
    Three independent checks that the result actually runs: nothing is "not
    found", no binary needs a glibc newer than the device has, and no RPATH
    leaked a path from the build machine.

Usage:
    ci/collect_deps.py --stage STAGE [--prefix /usr/local/tos-darktable]
    ci/collect_deps.py --stage STAGE --report bundle-report.json
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# --------------------------------------------------------------------------- #
# What must never be bundled
# --------------------------------------------------------------------------- #

# Libraries that come from the base system and must be resolved there. Two
# different reasons, both worth stating because the list looks arbitrary
# otherwise:
#
#   * the glibc family and the dynamic loader itself. A private copy of libc
#     that disagrees with the running kernel, or with a library loaded later by
#     something else in the process, is a class of failure that is very hard to
#     diagnose and buys nothing - no Ubuntu-derived root filesystem can boot
#     without these.
#
#   * libstdc++ and libgcc_s. The same argument as the compiler runtime: they
#     are Priority: important in Debian, pulled in by apt, dpkg and systemd, so
#     their absence would mean the system itself does not run. Excluding them
#     is only safe because TOS 7.x is contractually pinned to the
#     Ubuntu 22.04-compatible base; the container verification job in the build
#     pipeline settles it empirically on every build, and this script can be
#     told to bundle them instead with --bundle-compiler-runtime if that job
#     ever fails.
EXCLUDE_ALWAYS = {
    "ld-linux-x86-64.so.2",
    "ld-linux-aarch64.so.1",
    "libc.so.6",
    "libm.so.6",
    "libpthread.so.0",
    "libdl.so.2",
    "librt.so.1",
    "libutil.so.1",
    "libresolv.so.2",
    "libnsl.so.1",
    "libanl.so.1",
    "libcrypt.so.1",
    "libmvec.so.1",
    "libthread_db.so.1",
    "linux-vdso.so.1",
    # The dynamic linker's own internals, which are dlopen'd by the loader and
    # must come from the same place it does.
    "libnss_dns.so.2",
    "libnss_files.so.2",
}

EXCLUDE_COMPILER_RUNTIME = {
    "libgcc_s.so.1",
    "libstdc++.so.6",
}

# Never bundle a GL implementation. It is host- and driver-specific, and its
# absence is the desired outcome: with no GLX available, GTK's X11 backend
# falls back to cairo software rendering, which is the only path that works on
# a NAS with no GPU driver.
EXCLUDE_GL = {
    "libGL.so.1",
    "libGLX.so.0",
    "libEGL.so.1",
    "libGLdispatch.so.0",
    "libOpenGL.so.0",
    "libGLX_mesa.so.0",
    "libEGL_mesa.so.0",
}

# The glibc the device provides. Anything needing newer cannot run there, and
# the failure on the device would be "version GLIBC_2.38 not found" with no
# hint about which file caused it - so it is caught at build time instead.
GLIBC_CEILING = (2, 35)

SONAME_RE = re.compile(r"\(SONAME\)\s+Library soname:\s+\[([^\]]+)\]")
GLIBC_VERSION_RE = re.compile(r"GLIBC_(\d+)\.(\d+)")

# --------------------------------------------------------------------------- #
# Building blocks
# --------------------------------------------------------------------------- #


def run(argv, env=None):
    result = subprocess.run(argv, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout, result.stderr


def log(message):
    print("[collect] %s" % message, flush=True)


def warn(message):
    print("[collect] WARNING: %s" % message, file=sys.stderr, flush=True)


def is_elf(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def read_soname(path):
    """Return the DT_SONAME of an ELF, falling back to its file name."""
    code, out, _err = run(["readelf", "-d", path])
    if code == 0:
        match = SONAME_RE.search(out)
        if match:
            return match.group(1)
    return os.path.basename(path)


def ldd(path, library_path=""):
    """Return {needed_name: resolved_path_or_None} for an ELF.

    ``ldd`` runs the loader against the file. Every input here is a build
    output of this pipeline, so there is no untrusted input for the classic
    ``ldd``-on-a-hostile-binary concern to apply to.
    """
    env = dict(os.environ)
    if library_path:
        env["LD_LIBRARY_PATH"] = library_path

    code, out, err = run(["ldd", path], env=env)
    if code != 0 and not out:
        raise RuntimeError("ldd failed for %s: %s" % (path, err.strip()))

    resolved = {}
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("linux-vdso"):
            continue
        if "=>" in line:
            name, _, rest = line.partition("=>")
            name = name.strip()
            rest = rest.strip()
            if rest.startswith("not found"):
                resolved[name] = None
            else:
                resolved[name] = rest.split(" (")[0].strip()
        else:
            # A line with no '=>' is a library the loader found by name alone.
            name = line.split(" (")[0].strip()
            if name:
                resolved[name] = name
    return resolved


def copy_real(source, destination):
    """Copy the file a symlink chain points at, under the given name.

    Symlinks are flattened deliberately. A bundled symlink whose target was not
    also copied is a dangling reference that only fails on the device, and the
    loader is perfectly happy with a plain file named after the SONAME.
    """
    real = os.path.realpath(source)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copy2(real, destination)
    os.chmod(destination, 0o644)
    return real


# --------------------------------------------------------------------------- #
# Phase 1 - populate the non-library parts
# --------------------------------------------------------------------------- #

# (source glob or path, destination relative to depends/)
COPY_TREES = [
    ("/usr/share/X11/xkb", "share/X11/xkb"),
    ("/usr/share/glib-2.0/schemas", "share/glib-2.0/schemas"),
    ("/usr/share/lensfun", "share/lensfun"),
    ("/usr/share/fonts/truetype/dejavu", "share/fonts/dejavu"),
]

COPY_TOOLS = [
    "/usr/bin/Xvfb",
    "/usr/bin/x11vnc",
    "/usr/bin/xauth",
    "/usr/bin/xkbcomp",
]

MODULE_DIRS = [
    (
        "/usr/lib/x86_64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders",
        "lib/gdk-pixbuf-2.0/2.10.0/loaders",
    ),
    (
        "/usr/lib/x86_64-linux-gnu/gtk-3.0/3.0.0/immodules",
        "lib/gtk-3.0/3.0.0/immodules",
    ),
    ("/usr/lib/x86_64-linux-gnu/gio/modules", "lib/gio/modules"),
]


def populate(depends, report):
    """Copy the data files and helper binaries the runtime needs."""
    copied = []

    for tool in COPY_TOOLS:
        if not os.path.exists(tool):
            warn("%s is missing from the build container" % tool)
            continue
        destination = os.path.join(depends, "bin", os.path.basename(tool))
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(os.path.realpath(tool), destination)
        os.chmod(destination, 0o755)
        copied.append(os.path.relpath(destination, depends))

    for source, relative in COPY_TREES:
        if not os.path.isdir(source):
            warn("%s is missing from the build container" % source)
            continue
        destination = os.path.join(depends, relative)
        if os.path.isdir(destination):
            shutil.rmtree(destination)
        shutil.copytree(source, destination, symlinks=False)
        copied.append(relative)

    # GTK's own modules. These are dlopen'd rather than linked, so nothing in
    # the ldd walk would ever notice them - they have to be copied explicitly
    # and seeded into the closure separately.
    for source_dir, relative in MODULE_DIRS:
        if not os.path.isdir(source_dir):
            warn("%s is missing from the build container" % source_dir)
            continue
        destination_dir = os.path.join(depends, relative)
        os.makedirs(destination_dir, exist_ok=True)
        count = 0
        for name in sorted(os.listdir(source_dir)):
            source = os.path.join(source_dir, name)
            if not os.path.isfile(source) or not name.endswith(".so"):
                continue
            shutil.copy2(os.path.realpath(source), os.path.join(destination_dir, name))
            count += 1
        copied.append("%s (%d file(s))" % (relative, count))

    report["populated"] = copied

    # Two payload pieces that are only Recommends of packages the build already
    # needs, so with recommends switched off they are absent unless installed
    # by name. Neither stops the build, and neither produces an error at
    # runtime - they produce a wrong-looking application:
    #
    #   the SVG loader  darktable draws its whole interface with SVG icons, so
    #                   without it every toolbar button is blank;
    #   the lens database  darktable reports "could not load lens database" in
    #                   a log nobody reads, and corrections never apply.
    #
    # Checked here rather than left to the install script so the failure is
    # attributed to the bundle and not to darktable.
    svg_loader = os.path.join(
        depends, "lib/gdk-pixbuf-2.0/2.10.0/loaders/libpixbufloader-svg.so"
    )
    if not os.path.isfile(svg_loader):
        warn(
            "the gdk-pixbuf SVG loader is missing from the bundle; darktable's "
            "icons will not render. Install librsvg2-common in the build image."
        )
    report["svg_loader_bundled"] = os.path.isfile(svg_loader)

    lensfun = os.path.join(depends, "share/lensfun")
    lensfun_versions = [
        name for name in (os.listdir(lensfun) if os.path.isdir(lensfun) else [])
        if name.startswith("version_")
    ]
    if not lensfun_versions:
        warn(
            "the lensfun database is missing from the bundle; lens corrections "
            "will silently do nothing. Install liblensfun-data-v1 in the build image."
        )
    report["lensfun_versions"] = lensfun_versions

    # The schemas are a binary cache. Copying the .xml sources is not enough:
    # GSettings reads the compiled file, and without it every GTK setting falls
    # back to a default and the theme misbehaves in ways that look like bugs.
    schemas = os.path.join(depends, "share/glib-2.0/schemas")
    if os.path.isdir(schemas):
        code, _out, err = run(["glib-compile-schemas", schemas])
        if code != 0:
            warn("glib-compile-schemas failed: %s" % err.strip())
        else:
            log("compiled GSettings schemas")

    # gdk-pixbuf needs a cache naming its loaders. It is regenerated here
    # rather than copied, because the copy in the container points at the
    # container's paths; the launcher rewrites it again at startup so the
    # bundle stays relocatable.
    loaders_dir = os.path.join(depends, "lib/gdk-pixbuf-2.0/2.10.0/loaders")
    if os.path.isdir(loaders_dir):
        code, out, err = run(["gdk-pixbuf-query-loaders"] + sorted(
            os.path.join(loaders_dir, name)
            for name in os.listdir(loaders_dir)
            if name.endswith(".so")
        ))
        if code != 0:
            warn("gdk-pixbuf-query-loaders failed: %s" % err.strip())
        else:
            cache = os.path.join(depends, "lib/gdk-pixbuf-2.0/2.10.0/loaders.cache")
            with open(cache, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(out)
            log("wrote %d bytes of gdk-pixbuf loaders.cache" % len(out))

    # Same idea for GTK's input modules.
    immodules_dir = os.path.join(depends, "lib/gtk-3.0/3.0.0/immodules")
    if os.path.isdir(immodules_dir):
        code, out, _err = run(["gtk-query-immodules-3.0"] + sorted(
            os.path.join(immodules_dir, name)
            for name in os.listdir(immodules_dir)
            if name.endswith(".so")
        ))
        if code == 0 and out.strip():
            cache = os.path.join(depends, "lib/gtk-3.0/3.0.0/immodules.cache")
            with open(cache, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(out)
            log("wrote GTK immodules.cache")


# --------------------------------------------------------------------------- #
# Phase 2 - the shared library closure
# --------------------------------------------------------------------------- #


def seeds(depends, stage_app):
    """Every ELF that will ship, not just the entry points.

    Starting from the four executables alone would miss everything loaded with
    dlopen: darktable's own plugins and image loaders, the gdk-pixbuf loaders,
    GTK's input modules, GIO's modules. None of them appear in any ldd output,
    and a missing one shows up on the device as a blank window or a file that
    will not open.
    """
    found = []
    for root in (stage_app, depends):
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(base, name)
                if not os.path.isfile(path):
                    continue
                if name.endswith(".so") or os.access(path, os.X_OK) or is_elf(path):
                    if is_elf(path):
                        found.append(path)
    return sorted(set(found))


def closure(depends, stage_app, bundle_compiler_runtime, report, sources):
    """Copy the transitive non-system shared libraries into depends/lib."""
    exclude = set(EXCLUDE_ALWAYS) | set(EXCLUDE_GL)
    if not bundle_compiler_runtime:
        exclude |= EXCLUDE_COMPILER_RUNTIME

    library_dir = os.path.join(depends, "lib")
    os.makedirs(library_dir, exist_ok=True)

    bundled = {}
    frontier = list(seeds(depends, stage_app))
    visited = set()
    missing = []

    log("walking the dependency closure from %d ELF file(s)" % len(frontier))

    while frontier:
        elf = frontier.pop()
        if elf in visited:
            continue
        visited.add(elf)

        # The bundle is on the search path for the same reason it will be on
        # the device: so that a library already collected here resolves from
        # here, and the walk converges instead of following the same branch
        # into the container's /usr over and over.
        library_path = os.pathsep.join(
            [library_dir, os.path.join(stage_app, "lib", "darktable")]
        )

        try:
            resolved = ldd(elf, library_path)
        except RuntimeError as error:
            warn(str(error))
            continue

        for name, path in resolved.items():
            if name in exclude or os.path.basename(name) in exclude:
                continue

            if path is None:
                # Already satisfied from inside the bundle? Then it is fine and
                # the loader simply did not need to say so. Anything else is a
                # genuine hole and must be reported, not guessed at.
                if name in bundled:
                    continue
                missing.append((elf, name))
                continue

            if not os.path.isabs(path):
                continue

            real = os.path.realpath(path)
            soname = read_soname(real)
            if soname in exclude:
                continue

            if soname in bundled:
                continue

            destination = os.path.join(library_dir, soname)
            try:
                copy_real(real, destination)
            except OSError as error:
                warn("cannot copy %s: %s" % (real, error))
                continue

            bundled[soname] = {
                "source": real,
                "size": os.path.getsize(destination),
                "referenced_by": os.path.relpath(elf, depends),
            }
            # Queue the copy, not the original: the loader then resolves this
            # library's own dependencies against the bundle, so the final state
            # of the walk is the state the loader will see on the device.
            frontier.append(destination)

            if real not in sources:
                sources[real] = soname

    report["bundled_libraries"] = bundled
    report["walked_files"] = len(visited)
    report["missing"] = [{"file": f, "needs": n} for f, n in missing]

    log(
        "collected %d librar(ies) from %d file(s); %d unresolved reference(s)"
        % (len(bundled), len(visited), len(missing))
    )
    return bundled, missing


# --------------------------------------------------------------------------- #
# Phase 3 - verification
# --------------------------------------------------------------------------- #


def verify(depends, stage_app, bundled, report):
    """Prove the closure actually works, three ways."""
    errors = []
    library_dir = os.path.join(depends, "lib")

    # (a) Nothing may be unresolved, checked against the bundle alone.
    unresolved = []
    for elf in seeds(depends, stage_app):
        try:
            resolved = ldd(elf, library_dir)
        except RuntimeError as error:
            errors.append(str(error))
            continue
        for name, path in resolved.items():
            if path is None and name not in bundled:
                unresolved.append("%s needs %s" % (os.path.relpath(elf, depends), name))
    if unresolved:
        errors.extend(unresolved[:20])
    report["unresolved"] = unresolved

    # (b) No bundled library may need a glibc newer than the device has.
    ceiling_violations = []
    for name in sorted(bundled):
        path = os.path.join(library_dir, name)
        code, out, _err = run(["readelf", "-V", path])
        if code != 0:
            continue
        versions = [(int(a), int(b)) for a, b in GLIBC_VERSION_RE.findall(out)]
        if versions and max(versions) > GLIBC_CEILING:
            ceiling_violations.append(
                "%s requires glibc %d.%d" % (name, *max(versions))
            )
    if ceiling_violations:
        errors.extend(ceiling_violations)
    report["glibc_violations"] = ceiling_violations

    # (c) No absolute path from the build machine may survive in an RPATH.
    # This is the classic "works on the runner, fails on the device" defect.
    leaked = []
    for elf in seeds(depends, stage_app):
        code, out, _err = run(["readelf", "-d", elf])
        if code != 0:
            continue
        for line in out.splitlines():
            if "RPATH" not in line and "RUNPATH" not in line:
                continue
            for entry in re.findall(r"\[([^\]]+)\]", line):
                for part in entry.split(":"):
                    if part.startswith("$ORIGIN") or not part.startswith("/"):
                        continue
                    if part.startswith(("/home/", "/tmp/", "/build", "/workspace", os.getcwd())):
                        leaked.append("%s -> %s" % (os.path.relpath(elf, depends), part))
    if leaked:
        errors.extend(leaked)
    report["rpath_leaks"] = leaked

    return errors


def report_shadowing(bundled, report):
    """List bundled libraries that also exist on the build host, and how they compare.

    A bundled library that shadows a *newer* one on the device is the dangerous
    case: it applies to the whole process, including libraries that were
    deliberately excluded and expect the newer version. Reporting the
    comparison makes that visible instead of theoretical.
    """
    entries = []
    for name in sorted(bundled):
        host = None
        for directory in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib", "/usr/lib"):
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                host = os.path.realpath(candidate)
                break

        entries.append(
            {
                "library": name,
                "bundled_bytes": bundled[name]["size"],
                "present_on_host": host is not None,
                "host_path": host,
            }
        )
    report["shadowing"] = entries
    return entries


# --------------------------------------------------------------------------- #
# Phase 4 - license notices
# --------------------------------------------------------------------------- #


def collect_licenses(depends, report):
    """Copy the Debian copyright file for every package whose files we ship.

    Bundling a distro's binaries without its copyright notices is the kind of
    thing an app store compliance review looks for, and the notices are already
    on the build machine - the distro puts a machine-readable copyright file
    next to every package it installs.
    """
    from collections import Counter

    origin = {}

    # Map each bundled file back to the Debian package that owns it.
    for relative_root in ("bin", "lib", "share"):
        root = os.path.join(depends, relative_root)
        if not os.path.isdir(root):
            continue
        for base, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(base, name)
                code, out, _err = run(["dpkg", "-S", path])
                if code != 0 or not out.strip():
                    continue
                package = out.split(":", 1)[0].strip()
                origin.setdefault(package, 0)
                origin[package] += 1

    destination = os.path.join(depends, "share", "licenses")
    os.makedirs(destination, exist_ok=True)

    written = []
    for package in sorted(origin):
        source = "/usr/share/doc/%s/copyright" % package
        if not os.path.isfile(source):
            warn("no copyright file for %s" % package)
            continue
        target = os.path.join(destination, "%s.copyright" % package)
        shutil.copy2(source, target)
        written.append(package)

    report["license_packages"] = written
    log("collected %d copyright notice(s)" % len(written))

    # The counts are useful when deciding whether a component is worth its
    # weight; the largest contributors are usually the ones to look at first.
    if origin:
        biggest = Counter(origin).most_common(10)
        report["largest_contributors"] = [
            {"package": package, "files": count} for package, count in biggest
        ]


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", required=True, help="DESTDIR the build installed into")
    parser.add_argument("--prefix", default="/usr/local/tos-darktable")
    parser.add_argument("--report", default="bundle-report.json")
    parser.add_argument(
        "--bundle-compiler-runtime",
        action="store_true",
        help="also bundle libstdc++ and libgcc_s; use if the bare-container "
             "verification job reports a GLIBCXX_ or GCC_ symbol as missing",
    )
    args = parser.parse_args(argv)

    app_root = os.path.join(args.stage, args.prefix.lstrip("/"))
    app_dir = os.path.join(app_root, "app")
    depends = os.path.join(app_root, "depends")

    if not os.path.isdir(app_dir):
        print("error: %s does not exist; run ci/build-darktable.sh first" % app_dir, file=sys.stderr)
        return 2

    os.makedirs(depends, exist_ok=True)

    report = {}
    sources = {}

    log("staging root: %s" % app_root)
    populate(depends, report)
    bundled, missing = closure(depends, app_dir, args.bundle_compiler_runtime, report, sources)
    report_shadowing(bundled, report)
    collect_licenses(depends, report)

    errors = verify(depends, app_dir, bundled, report)

    total = sum(entry["size"] for entry in bundled.values())
    report["total_library_bytes"] = total
    report["errors"] = errors

    log("bundled library payload: %.1f MB" % (total / (1024 * 1024)))

    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    log("wrote %s" % args.report)

    if missing:
        print("\nUnresolved references:", file=sys.stderr)
        for elf, name in missing[:20]:
            print("  %s needs %s" % (os.path.relpath(elf, depends), name), file=sys.stderr)

    if errors:
        print("\nVerification failed:", file=sys.stderr)
        for item in errors[:20]:
            print("  %s" % item, file=sys.stderr)
        return 1

    log("verification passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
