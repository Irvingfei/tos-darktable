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
#   DARKTABLE_VERSION   upstream release to build          (default 5.6.1)
#   DARKTABLE_SHA       commit to pin the tag to           (optional)
#   PREFIX              install prefix seen at runtime     (default /usr/local/tos-darktable/app)
#   STAGE               DESTDIR for the install            (default $PWD/stage)
#   BUILD_DIR           out-of-tree build directory        (default $PWD/build)

set -euo pipefail

DARKTABLE_VERSION="${DARKTABLE_VERSION:-5.6.1}"
DARKTABLE_SHA="${DARKTABLE_SHA:-}"
PREFIX="${PREFIX:-/usr/local/tos-darktable/app}"
STAGE="${STAGE:-$PWD/stage}"
BUILD_DIR="${BUILD_DIR:-$PWD/build}"
SRC_DIR="${SRC_DIR:-$PWD/src}"
JOBS="${JOBS:-$(nproc)}"

log() { printf '\n=== %s\n' "$*"; }

# --------------------------------------------------------------------------- #
# 1. Source
# --------------------------------------------------------------------------- #

if [ ! -d "$SRC_DIR/.git" ]; then
    log "cloning darktable $DARKTABLE_VERSION"
    git clone --depth 1 --branch "release-$DARKTABLE_VERSION" \
        https://github.com/darktable-org/darktable.git "$SRC_DIR"
else
    log "reusing the existing checkout at $SRC_DIR"
fi

if [ -n "$DARKTABLE_SHA" ]; then
    # A tag that has been moved upstream would otherwise change what ships
    # without anything in the pipeline saying so. Comparing the commit turns
    # that into a build failure that names the cause.
    actual="$(git -C "$SRC_DIR" rev-parse HEAD)"
    if [ "$actual" != "$DARKTABLE_SHA" ]; then
        echo "error: release-$DARKTABLE_VERSION is at $actual, expected $DARKTABLE_SHA" >&2
        echo "       upstream re-tagged; update DARKTABLE_SHA deliberately." >&2
        exit 1
    fi
    log "pinned commit verified: $actual"
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

# A quick sanity check that the two binaries exist where the launcher expects
# them. A build that produced nothing usable should fail here rather than
# three steps later inside collect_deps.py.
for binary in darktable darktable-cli; do
    if [ ! -x "$STAGE$PREFIX/bin/$binary" ]; then
        echo "error: $binary was not installed into $STAGE$PREFIX/bin" >&2
        exit 1
    fi
done

log "done"
