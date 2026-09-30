# darktable for TOS

Run [darktable](https://www.darktable.org), the open source photography
workflow application and RAW developer, on your TerraMaster NAS. The full
darktable desktop opens in a browser tab and your photographs stay as ordinary
files in a NAS shared folder.

- **Application ID** `tos-darktable`
- **Version** 1.0.1
- **Platform** TOS 7.0 or later, x86_64
- **Upstream** darktable 5.6.1
- **Licence** GPL-3.0-or-later

## Overview

darktable has a virtual lighttable for organising photographs and a darkroom
for developing them non-destructively, with more than sixty processing modules
and RAW support for over five hundred camera models. It is a desktop
application, and TOS applications are services with a web interface, so this
package joins the two: it runs darktable against a private virtual display and
streams that display to the browser over VNC.

The reason it is packaged this way rather than as a simpler native application
is the second half of that sentence. TOS 7 carries no desktop libraries, and
darktable needs a newer OpenEXR than the platform root filesystem provides, so
the package brings its own GTK stack, X server and VNC server with it and
points them at its own copy. Everything lives under
`/usr/local/tos-darktable`; the section on the runtime file manifest below
lists every path the application touches.

## Features

- The complete darktable interface — lighttable, darkroom, all modules —
  in a browser tab, with nothing to install on the computer you view it from.
- Photographs live in a shared folder named `darktable-photos`, so they are
  visible in the File Manager, reachable over SMB, and covered by volume
  snapshots without the application implementing any of that.
- Non-destructive editing: adjustments are recorded in sidecar files and the
  original RAW is never modified.
- Multi-core processing, and OpenCL acceleration when the NAS has a usable
  OpenCL device. Neither is required; darktable falls back to CPU rendering.
- Access is protected by a password generated during installation. The VNC
  transport itself is a unix socket reachable only by the application account,
  so there is no second door to the desktop.
- The session recovers on its own: if darktable exits, it is restarted
  underneath a display that stays up, and the browser reconnects to the same
  desktop. A **Restart session** button is available in the toolbar.

## Requirements

- TerraMaster NAS running TOS 7.0 or later
- x86_64 CPU
- 2 GB of RAM recommended. darktable is granted 200% CPU and 2048 MB, the
  platform's multimedia allowance; large RAW files may need more than the
  memory allowance on a busy system.

## Installation

From the TOS App Center, search for **darktable** and install it.

To install the package by hand:

```bash
sudo dpkg -i tos-darktable_x86_64.deb
```

Verify the download first:

```bash
sha256sum -c tos-darktable_x86_64.deb.sha256
```

Installing by hand does not create the shared folder or register the
application with the App Center; run `sudo dpkg --purge tos-darktable` and
install from the App Center instead if you want the full integration.

## Usage

1. Install the application.
2. Read the access password:

   ```bash
   sudo cat /usr/local/tos-darktable/data/access.txt
   ```

3. Open `http://<your-nas-address>:9312/` in a browser and sign in with any
   username and that password. From the App Center, the application opens at
   the same address in a new tab.
4. Copy your photographs into the `darktable-photos` shared folder. They appear
   in the File Manager and over SMB as soon as they are there.
5. Open the **import** panel in darktable and point it at that folder.

The password is generated once, during installation, and is never regenerated
by an upgrade — otherwise upgrading would lock you out of a desktop you were
already using. To change it, edit `data/access.txt` and restart the
application; the file is re-read on every request, so a restart is only needed
to be tidy.

### Toolbar

| Button | What it does |
| --- | --- |
| **Fit** | Scales the remote desktop to the browser window. On by default, because scrolling a desktop is worse than scaling it. |
| **Full screen** | Uses the whole display. |
| **Restart session** | Closes and reopens the editing session. This is the way back if darktable has stopped responding, and the way out of the degraded state described below. |

## Permissions

| Permission | Justification |
| --- | --- |
| Network: TCP port 9312 | The only network listener. Serves the web interface and the desktop stream. |
| File System: `/usr/local/tos-darktable/` | The application's own directory: its code, its bundled runtime, and its runtime data under `data/` and `logs/`. |
| User: `tos-darktable` | A dedicated non-root account, created by the platform. The service runs as this user and never as root. |
| Shared Folder: `darktable-photos` | Where photographs are kept, created with the platform's own `ter_share_add`. The user's files, not the application's. |
| No privileged mode, no host networking, no access to other applications' data. | |

## Ports

| Port | Protocol | Purpose |
| --- | --- | --- |
| 9312 | TCP | Web interface and the VNC desktop stream over WebSocket. Listens on all interfaces; every route except `/health` requires the access password. |

No other port is opened. The VNC server publishes a unix socket inside the
application's own `data/run/` directory instead of a TCP port, and the virtual
X server is started with `-nolisten tcp`.

## Runtime file manifest

Every path the application creates or writes at runtime, as required by section
12.9.6 of the TOS 7 application development guide. Anything not listed here is
never written.

| Path | Purpose | Format | Created when | Growth bound / rotation | Lifecycle |
| --- | --- | --- | --- | --- | --- |
| `data/access.txt` | The access password | Text | Once, by `postinst` | Under 1 KB | Persistent — never regenerated on upgrade |
| `data/config/` | darktable's configuration | SQLite, text | On first start | A few MB | Persistent |
| `data/cache/` | darktable's thumbnail and mipmap cache | Image files | On demand | Bounded by darktable's own cache settings | Regenerable — safe to delete |
| `data/library.db` | darktable's photograph database | SQLite | On first start | Proportional to the library | Persistent |
| `data/home/` | `HOME` for the service account | Directory | On start | Small | Persistent |
| `data/tmp/` | darktable's scratch space (`TMPDIR`) | Binary | On demand | Swept on every start | Temporary |
| `data/run/launcher.lock` | Prevents a second instance from starting | Lock file | On start | Fixed | Removed on stop |
| `data/run/session.json` | The process table, so an orphaned run can be cleaned up | JSON | On start and on restart | Under 1 KB | Removed on stop |
| `data/run/x11vnc.sock` | The VNC transport | Unix socket | On start | One socket | Removed before each start and on stop |
| `data/xauth/Xauthority` | X authentication cookie | Binary, mode 0600 | On start | Under 1 KB | Rewritten on every start |
| `data/fontconfig-cache/` | Font cache | Binary | On first text render | A few MB | Regenerable — safe to delete |
| `logs/launcher.log` | Launcher log | Text | On start | Rotated at 2 MB, three kept | Persistent, rotated |
| `logs/darktable.log` | darktable's own output | Text | On start | Truncated per start | Persistent |
| `logs/xvfb.log`, `logs/x11vnc.log` | X and VNC server output | Text | On start | Truncated per start | Persistent |
| `webui/` | The browser interface, unpacked from `webui.bz2` | HTML, JS, CSS | Once, by `postinst` | Under 1 MB | Replaced on upgrade |
| `/tmp/.X11-unix/X<n>` | The X display socket | Unix socket | On start | One socket | Removed on stop and on purge |
| `etc/fonts/fonts.conf` | Generated font configuration | XML | On start | Under 1 KB | Rewritten on every start |

**On `/tmp`:** the application writes no temporary files to the shared system
`/tmp`. The only path it touches there is `/tmp/.X11-unix`, which the X11
protocol defines as the fixed location for X display sockets and which the
bundled X server creates itself. That location is not configurable at runtime,
and `PrivateTmp=true` is deliberately not used — see the note in
`init.d/tos-darktable.service`. Everything else, including darktable's own
scratch space, is redirected into `data/tmp/`.

## Configuration

Settings live in `/usr/local/tos-darktable/tos-darktable.env` and are read by
the service on start.

| Setting | Default | Meaning |
| --- | --- | --- |
| `DTOS_LISTEN_PORT` | `9312` | The listening port. If you change it, change the port in `nginx/tos-darktable.conf` and in the App Center listing to match. |
| `DTOS_SCREEN_WIDTH` / `DTOS_SCREEN_HEIGHT` | `1600` / `1000` | The virtual desktop's size. Larger means more scrolling on a small screen; smaller means a cramped darkroom. |
| `DTOS_DT_THREADS` | `2` | Worker threads for darktable. Raise it on a NAS with more cores and a higher CPU allowance. |
| `DTOS_SHARE_NAME` | `darktable-photos` | The shared folder holding photographs. |
| `DTOS_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARN` or `ERROR`. |

After changing anything, restart the service:

```bash
sudo systemctl restart tos-darktable
```

## Diagnostics

```bash
# Is the bundle complete, and where is everything?
/usr/local/tos-darktable/bin/darktable-server --check

# What is running?
systemctl status tos-darktable
curl -u admin:$(sudo cat /usr/local/tos-darktable/data/access.txt) \
     http://127.0.0.1:9312/api/status

# What went wrong?
journalctl -u tos-darktable -n 100 --no-pager
tail -50 /usr/local/tos-darktable/logs/launcher.log
```

Two things that mislead:

- **`--check` run as `root` cannot judge access to the shared folder.** The
  platform grants access through an ACL on the folder itself and root bypasses
  ACLs, so a folder the service account cannot even enter reads as fine. Ask
  the service instead: `su -s /bin/sh tos-darktable -c 'ls /Volume1/darktable-photos'`.
- **A blank page in the browser with a healthy `/health`** means the WebSocket
  is not getting through. Check that `nginx/tos-darktable.conf` still carries
  the `Upgrade` and `Connection` headers, and that `logs/x11vnc.log` is not
  empty.

### Restarting from a stopped state

If the application reports itself **degraded**, darktable has exited more times
than the launcher is willing to retry automatically (five times in ten
minutes). Press **Restart session** in the toolbar, or:

```bash
sudo systemctl restart tos-darktable
```

The launcher log will name the reason for each exit.

## Known limitations

These are consequences of running a desktop RAW developer on a NAS and are
stated so they are not mistaken for defects.

- **OpenEXR import and export are unavailable.** darktable 5.6.1 requires
  OpenEXR 3.0; TOS 7 provides 2.5.7. The feature is compiled out rather than
  left in a state where it fails at runtime. RAW files are unaffected.
- **HEIF and HEIC import are unavailable** for the same kind of reason:
  darktable requires libheif 1.13 and the platform provides 1.12. If you need
  HEIC, convert to DNG or TIFF first.
- **Tethered shooting is unavailable.** darktable is built without camera
  support, because it would add the whole libgphoto2 data tree to the package
  for a feature no NAS can use.
- **The map view is unavailable**, since it needs online map tiles.
- **No GPU acceleration unless the NAS provides an OpenCL device.** darktable
  falls back to CPU rendering, which is correct but slower; its OpenCL support
  is loaded at runtime and costs nothing when there is no device.
- **The desktop is streamed, so it is network-bound.** It is comfortable on a
  local network and not intended for use over the internet.
- **The package is large** — several hundred megabytes installed. It carries
  its own GTK, X and VNC runtime, as the Overview explains.

## Building from source

The package is built by GitHub Actions; `ci/` and `tools/` in the repository
contain everything it runs.

```bash
# 1. Compile darktable and stage it. Run inside ubuntu:22.04.
ci/build-darktable.sh

# 2. Collect the shared library closure and the data files it needs.
ci/collect_deps.py --stage "$PWD/stage"

# 3. Assemble and package.
tools/build.py --platform x86_64 --stage "$PWD/stage"
```

`tools/selftest.py` checks the launcher's own logic with no runtime present,
and `tools/validate.py` reproduces the developer platform's automated
validation. `ci/verify-package.sh` installs the built package into a bare
Ubuntu 22.04 container and runs it end to end, which is the only check that
proves the bundled runtime is complete.

## Licence

This package is distributed under the **GNU General Public License, version 3
or later** — the same licence as darktable itself, whose source is at
<https://github.com/darktable-org/darktable>. The corresponding source for this
package, including every build script needed to produce the binaries in it, is
in this repository.

### Third-party components

The package bundles the following, each unmodified. Their licence texts are
installed under `/usr/local/tos-darktable/depends/share/licenses/`, collected
automatically from the build system's own records at package time.

| Component | Licence |
| --- | --- |
| darktable | GPL-3.0-or-later |
| Xvfb (X.Org) | MIT / X11 |
| x11vnc | GPL-2.0-or-later |
| GTK 3 and its dependencies (glib, pango, cairo, gdk-pixbuf, harfbuzz, …) | LGPL-2.1-or-later, MIT, BSD |
| noVNC | MPL-2.0 |
| DejaVu fonts | Bitstream Vera / public domain |
| Lensfun database | CC-BY-SA-3.0 |
| The launcher in `bin/` and `lib/` | GPL-3.0-or-later |

darktable, Xvfb and x11vnc are separate programs in one package: they
communicate over the X protocol and a unix socket and neither is a derivative
work of the other. noVNC is a browser library served to the browser unmodified.

## Support

Open an issue in this repository. When reporting a problem, include the output
of `darktable-server --check`, the last fifty lines of
`/usr/local/tos-darktable/logs/launcher.log`, and your TOS version.

## Changelog

### 1.0.1

- Publisher name corrected.
- Rebuilt so the package can be installed over 1.0.0; the App Center does not
  accept two packages carrying the same version.

### 1.0.0

- First release, based on darktable 5.6.1.
- Browser-based remote desktop; nothing to install on the viewing computer.
- Full darkroom and virtual lighttable.
- Photographs are kept in the `darktable-photos` shared folder, visible in the
  File Manager and over SMB.
- Multi-core processing and OpenCL acceleration when a device is available.
- Access protected by a generated password; the VNC transport is a unix socket
  reachable only by the application account.
- Automatic session recovery with a manual restart control.
- Tethered shooting, the map view, OpenEXR and HEIF are disabled; see Known
  limitations.
