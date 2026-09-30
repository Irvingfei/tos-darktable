#!/bin/bash
# Install everything the darktable build needs, inside a bare ubuntu:22.04.
#
# Kept in a script rather than inlined in the workflow for two reasons: the
# dependency list then has exactly one home, and the workflow can capture this
# step's output with a plain `| tee` instead of an `exec` redirect that
# silently swallowed everything it was given.
#
# The required list is taken from the find_package(... REQUIRED) calls in
# darktable's own CMakeLists rather than from its published documentation,
# because that is what actually decides whether the configure step succeeds.
# libpugixml-dev is the one that is easy to miss: it appears nowhere in the
# published list.
set -eux

# Evidence, not decoration. The workflow runs this through `bash`, but a step's
# own shell is a separate matter: a container job's default shell is sh (dash
# on Ubuntu), and a bashism at the top of a step - `set -o pipefail` is the
# usual one - makes the step fail in zero seconds with nothing in any log,
# because the file redirecting its output never gets created. Printing the
# interpreter here means the next such failure is visible in the captured log
# instead of being inferred from timings.
echo "shell: $0 | BASH_VERSION=${BASH_VERSION:-<not bash>} | /bin/sh -> $(readlink -f /bin/sh)"

export DEBIAN_FRONTEND=noninteractive

# Paper over occasional mirror flakiness, and keep the install lean.
printf 'Acquire::Retries "10";\n' > /etc/apt/apt.conf.d/80retry
printf 'APT::Install-Recommends "false";\n' > /etc/apt/apt.conf.d/80recommends
printf 'APT::Get::Assume-Yes "true";\n' > /etc/apt/apt.conf.d/80forceyes

apt-get update

# Required. The configure step fails outright without any of these.
#
# gcc-12 and g++-12 specifically, not the default gcc-11 that jammy ships:
# darktable 5.6.1 refuses anything older than version 12 in
# cmake/compiler-versions.cmake and prints "GNU C compiler version 11.4.0 is
# too old and is unsupported", then the same for C++, then a libstdc++ version
# check that fails with "Unsupported libstdc++ version: 11".
#
# That is safe because Ubuntu 22.04's libstdc++6 is itself built from GCC 12 -
# 12.3.0-1ubuntu1~22.04 on the target device, exporting GLIBCXX_3.4.30 - even
# though gcc-11 is the default compiler. So a binary built here with g++-12
# runs against the system libstdc++ and does not need one bundled.
#
# intltool provides intltool-merge, which CMakeLists.txt looks for by name and
# fails on when absent.
#
# xsltproc is required too: CMakeLists.txt:353 aborts with "No XSLT interpreter
# found" without it. saxon-xslt would do as well, but xsltproc is one package.
apt-get install -y --no-install-recommends \
    build-essential gcc-12 g++-12 cmake ninja-build pkg-config \
    curl git ca-certificates gettext intltool libxml2-utils xsltproc \
    desktop-file-utils \
    python3 python3-minimal dpkg-dev binutils file \
    bzip2 xz-utils ccache patchelf \
    libglib2.0-dev libgtk-3-dev libxml2-dev libpotrace-dev \
    liblensfun-dev libsqlite3-dev libpango1.0-dev librsvg2-dev \
    libpng-dev libjpeg-dev libtiff-dev liblcms2-dev \
    libjson-glib-dev libcurl4-openssl-dev libexiv2-dev \
    libpugixml-dev libimath-dev zlib1g-dev libgdk-pixbuf-2.0-dev

# Optional image formats. darktable guards every one of these with
# if(X_FOUND), so a missing library costs that format and nothing else - which
# is why they are installed one at a time and tolerated when absent. A single
# bad name in a combined install would abort the whole step.
for package in libjxl-dev libavif-dev libheif-dev libwebp-dev \
               libopenjp2-7-dev libgraphicsmagick1-dev; do
    apt-get install -y --no-install-recommends "$package" \
        || echo "optional dependency $package is unavailable; skipping it"
done

# Sources for the runtime payload. These are copied into the bundle by
# ci/collect_deps.py; the finished package never depends on them.
apt-get install -y --no-install-recommends \
    xvfb x11vnc xauth x11-xkb-utils xkb-data fonts-dejavu-core \
    libglib2.0-bin libgtk-3-bin libgdk-pixbuf2.0-bin

# Runtime data and loaders that are only Recommends of packages already
# installed above, and are therefore absent because recommends are switched
# off. Neither breaks the build; both break the application in a way that is
# invisible until it is running.
#
#   liblensfun-data-v1  the lens database at /usr/share/lensfun. Without it
#                       darktable starts, loads, and silently reports that it
#                       cannot load the lens database - the corrections simply
#                       never apply.
#   librsvg2-common     the gdk-pixbuf SVG loader. darktable's interface is
#                       drawn with SVG icons, so without it every toolbar
#                       button renders blank.
#
# Installed individually and tolerated, so a renamed package degrades rather
# than failing the build; the echo lands in the captured apt log either way.
# Tools darktable treats as optional but whose absence it warns about, and
# which upstream's own CI image carries. Installed the same tolerant way: a
# missing one costs a warning, not a build.
for package in liblensfun-data-v1 librsvg2-common python3-jsonschema po4a; do
    apt-get install -y --no-install-recommends "$package" \
        || echo "optional runtime payload $package is unavailable; that feature will be missing"
done

echo "build dependencies installed"
