#!/bin/bash
# Build darktable for TOS.
#
# Runs inside an ubuntu:22.04 container, which is deliberate: the runner image
# ships dozens of -dev packages, and every optional find_package() that
# succeeds on it adds a library to the bundle that would then be collected by
# ci/collect_deps.py. Building in a bare ubuntu:22.04 with an explicit package
# list is what makes the closure reproducible.
#
# The target root filesystem of TOS 7 is Ubuntu 22.04-compatible, so building
# here produces binaries whose glibc requirement matches the device exactly.
#
# Usage:
#   ci/build-darktable.sh
#
# Environment:
#   DARKTABLE_VERSION         upstream release to build        (default 5.6.1)
#   DARKTABLE_TARBALL_SHA256  expected hash of the release     (checked when set)
#   PREFIX                    install prefix seen at runtime   (default /usr/local/tos-darktable/app)
#   STAGE                     DESTDIR for the install          (default $PWD/stage)
#   BUILD_DIR                 out-of-tree build directory      (default $PWD/build)
#   WORK                      scratch directory for the source (default $PWD/work)

set -euo pipefail

DARKTABLE_VERSION="${DARKTABLE_VERSION:-5.6.1}"
DARKTABLE_TARBALL_SHA256="${DARKTABLE_TARBALL_SHA256:-}"
PREFIX="${PREFIX:-/usr/local/tos-darktable/app}"
STAGE="${STAGE:-$PWD/stage}"
BUILD_DIR="${BUILD_DIR:-$PWD/build}"
WORK="${WORK:-$PWD/work}"
JOBS="${JOBS:-$(nproc)}"

# STAGE is made absolute before it is used, and that matters.
#
# DESTDIR is applied by the generated install rules, and a *relative* DESTDIR
# has an ambiguous base - the message cmake prints while installing names the
# path as computed, which is not necessarily where the file is written. That
# produced an install whose own log said it had written
# stage/usr/local/tos-darktable/app/bin/darktable while the next command in
# this same script could not find it. An absolute DESTDIR removes the question.
#
# PREFIX is deliberately left alone: it is a path seen at runtime on the
# device, not on this machine.
mkdir -p "$STAGE"
STAGE="$(cd "$STAGE" && pwd)"

log() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------------------- #
# 1. Source
# --------------------------------------------------------------------------- #

# The published release tarball rather than a git clone.
#
# darktable's build needs several git submodules - rawspeed, OpenCL, LibRaw,
# lua among them - and a plain `git clone --depth 1` does not fetch them, so
# the configure step stops at "RawSpeed submodule not found". The release
# tarball has them all folded in, and it is the same bytes upstream publishes:
# checked against the copy this package was originally developed from, and
# identical.
#
# It is also easier to trust. A tag can be moved, which a commit pin catches;
# a tarball hash also catches a mirror serving something else, and it needs no
# git at all.

TARBALL_URL="https://github.com/darktable-org/darktable/releases/download/release-${DARKTABLE_VERSION}/darktable-${DARKTABLE_VERSION}.tar.xz"
TARBALL="$WORK/darktable-${DARKTABLE_VERSION}.tar.xz"
SRC_DIR="$WORK/darktable-${DARKTABLE_VERSION}"

mkdir -p "$WORK"

if [ ! -f "$TARBALL" ]; then
    log "downloading darktable $DARKTABLE_VERSION"
    curl -fsSL --retry 3 --retry-delay 5 -o "$TARBALL.part" "$TARBALL_URL"
    mv "$TARBALL.part" "$TARBALL"
else
    log "reusing the downloaded tarball at $TARBALL"
fi

if [ -n "$DARKTABLE_TARBALL_SHA256" ]; then
    echo "$DARKTABLE_TARBALL_SHA256  $TARBALL" | sha256sum -c - || {
        echo "error: the release tarball does not match the expected hash." >&2
        echo "       Either upstream republished the release, or something" >&2
        echo "       served different bytes. Update DARKTABLE_TARBALL_SHA256" >&2
        echo "       deliberately, after checking what changed." >&2
        exit 1
    }
    log "tarball verified: $DARKTABLE_TARBALL_SHA256"
else
    log "WARNING: DARKTABLE_TARBALL_SHA256 is not set; the source is unverified"
fi

if [ ! -d "$SRC_DIR/src" ]; then
    log "extracting"
    tar -xJf "$TARBALL" -C "$WORK"
else
    log "reusing the extracted source at $SRC_DIR"
fi

if [ ! -d "$SRC_DIR/src/external/rawspeed" ]; then
    echo "error: $SRC_DIR/src/external/rawspeed is missing. The tarball did not" >&2
    echo "       carry the bundled submodules, which the build requires." >&2
    exit 1
fi

# --------------------------------------------------------------------------- #
# 2. Configure
# --------------------------------------------------------------------------- #

log "configuring"

# Two groups of flags below. The first is mandatory - without any of them the
# configure step fails outright. The second is about size: each disabled
# optional dependency is a library and a data tree that would otherwise be
# bundled into a package that is already large.
#
# -DPROJECT_VERSION short-circuits the version detection, which otherwise
# shells out to git describe and falls back to "archive-<sha>" for a source
# tree without history.

# darktable 5.6.1 requires GCC 12 or newer; jammy's default compiler is 11 and
# the configure step refuses it outright. Ubuntu 22.04 does ship gcc-12, and
# its libstdc++6 is built from GCC 12 as well, so a binary produced here links
# against the system libstdc++ and needs none bundled. See
# ci/install-build-deps.sh for the full reasoning.
cmake -S "$SRC_DIR" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=gcc-12 \
    -DCMAKE_CXX_COMPILER=g++-12 \
    -DCMAKE_C_COMPILER_LAUNCHER=ccache \
    -DCMAKE_CXX_COMPILER_LAUNCHER=ccache \
    -DCMAKE_INSTALL_PREFIX="$PREFIX" \
    -DCMAKE_INSTALL_MESSAGE=NEVER \
    -DPROJECT_VERSION="$DARKTABLE_VERSION" \
    \
    -DBINARY_PACKAGE_BUILD=ON \
    \
    -DBUILD_TESTING=OFF \
    -DUSE_OPENEXR=OFF \
    -DUSE_GMIC=OFF \
    -DDONT_USE_INTERNAL_LUA=OFF \
    \
    -DBUILD_CMSTEST=OFF \
    -DBUILD_RS_IDENTIFY=OFF \
    -DBUILD_PRINT=OFF \
    -DTESTBUILD_OPENCL_PROGRAMS=OFF \
    -DVALIDATE_APPDATA_FILE=OFF \
    \
    -DUSE_CAMERA_SUPPORT=OFF \
    -DUSE_COLORD=OFF \
    -DUSE_MAP=OFF \
    -DUSE_PORTMIDI=OFF \
    -DUSE_KWALLET=OFF \
    -DUSE_LIBSECRET=OFF \
    -DUSE_SDL2=OFF \
    -DUSE_ICU=OFF \
    -DUSE_UNITY=OFF \
    -DUSE_AI=OFF \
    \
    -DUSE_OPENMP=ON \
    -DUSE_OPENCL=ON \
    -DUSE_LUA=ON \
    -DUSE_GRAPHICSMAGICK=ON \
    -DUSE_IMAGEMAGICK=OFF \
    -DUSE_OPENJPEG=ON \
    -DUSE_WEBP=ON \
    -DUSE_JXL=ON \
    -DUSE_AVIF=ON \
    -DUSE_HEIF=ON \
    -DUSE_XCF=ON \
    -DUSE_ISOBMFF=ON \
    -DUSE_DARKTABLE_PROFILING=OFF \
    2>&1 | tee "$PWD/cmake-configure.log"

# The configure log is the only record of which optional formats this build
# actually has. It is kept as an artifact so the README's feature list can be
# written from facts rather than from the flag list, which is not the same
# thing: a flag set to ON whose library is too old is silently turned off.
log "optional feature summary"
grep -E '^\-\- (Found|Could NOT find|Building)' "$PWD/cmake-configure.log" \
    | sort -u || true

# --------------------------------------------------------------------------- #
# 3. Build and install
# --------------------------------------------------------------------------- #

log "building with $JOBS job(s) - this takes 30 to 55 minutes cold"
# DESTDIR, not a prefix override: the prefix is baked in at configure time and
# darktable resolves its data directories relative to the executable, so the
# staged tree has to have exactly the shape it will have on the device. A
# --prefix override here would produce a tree that is correct in the staging
# directory and wrong once installed.
DESTDIR="$STAGE" cmake --build "$BUILD_DIR" --target install -- -j"$JOBS" \
    || { echo "build failed" >&2; exit 1; }

log "installed into $STAGE$PREFIX"

# List what actually landed, unconditionally. The check below failed once on a
# path that the install log said it had just written, and there was nothing in
# the log to say which of "absent" or "not executable" it was - the listing is
# cheaper than another forty-minute round trip to find out.
echo "--- contents of $STAGE$PREFIX/bin ---"
ls -la "$STAGE$PREFIX/bin" 2>&1 || true
echo "--- pwd: $(pwd) ---"

for binary in darktable darktable-cli; do
    target="$STAGE$PREFIX/bin/$binary"
    if [ ! -e "$target" ]; then
        echo "error: $target does not exist." >&2
        echo "       cwd is $(pwd); STAGE='$STAGE' PREFIX='$PREFIX'" >&2
        exit 1
    fi
    if [ ! -x "$target" ]; then
        echo "error: $target exists but is not executable." >&2
        ls -la "$target" >&2 || true
        exit 1
    fi
done
log "darktable and darktable-cli are installed and executable"
