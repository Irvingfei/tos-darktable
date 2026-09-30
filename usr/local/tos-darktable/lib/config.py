"""Configuration for the darktable launcher.

Values come from the environment first, then from the packaged
``tos-darktable.env`` file that systemd loads through ``EnvironmentFile=``.
Reading the file here as well means the launcher behaves identically when it is
run by hand during a diagnostic, which is the only way to debug it on a device
where systemd owns the normal start path.

Layout
------
``<root>/``              metadata, lifecycle scripts, our Python modules
``<root>/app/``          darktable's own install tree, unchanged: cmake prefix
``<root>/depends/``      the bundled runtime closure (guide section 8.2)

darktable relocates itself through ``whereami`` plus the relative paths baked
in at configure time, so ``app/bin/darktable`` finds ``app/lib/darktable`` and
``app/share/darktable`` with no environment variables at all. Keeping that
tree under its own ``app/`` prefix is what stops it from colliding with our
modules in ``lib/``. Everything else - GTK, the X and VNC servers, their data
files - lives under ``depends/``, which the guide documents as the place for
bundled dependencies, and the launcher points the loader and GTK at it
explicitly rather than relying on any platform-side mapping.
"""

import json
import os

from . import APP_ID

# lib/config.py -> lib/ -> <app root>
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_FILE = os.path.join(APP_ROOT, "%s.env" % APP_ID)


def _read_env_file(path):
    """Parse a systemd EnvironmentFile: KEY=VALUE, ``#`` comments, no quoting."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return values

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        # A trailing inline comment is only a comment when it is unambiguously
        # separated from the value; paths here never contain ' #'.
        if " #" in value:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


_FILE_VALUES = _read_env_file(ENV_FILE)


def _setting(name, default=None):
    value = os.environ.get(name)
    if value:
        return value
    value = _FILE_VALUES.get(name)
    if value:
        return value
    return default


def _int_setting(name, default):
    raw = _setting(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class Config(object):
    """Resolved runtime configuration for one launcher instance."""

    def __init__(self):
        self.app_id = APP_ID
        self.root = APP_ROOT

        # The account the platform created for this application, read from the
        # package metadata rather than repeated here. It is needed at runtime
        # because the platform creates it *after* postinst has run, so the
        # ownership and shared-folder setup that install could not do has to be
        # redone by the service's own root-privileged preparation step.
        self.config_ini = os.path.join(APP_ROOT, "config.ini")
        self.app_user = self._read_app_user()

        self.data_dir = _setting("DTOS_DATA_DIR", os.path.join(APP_ROOT, "data"))
        self.log_dir = _setting("DTOS_LOG_DIR", os.path.join(APP_ROOT, "logs"))
        self.webui_dir = _setting("DTOS_WEBUI_DIR", os.path.join(APP_ROOT, "webui"))
        self.secret_file = _setting(
            "DTOS_SECRET_FILE", os.path.join(self.data_dir, "access.txt")
        )

        self.listen_host = _setting("DTOS_LISTEN_HOST", "0.0.0.0")
        self.listen_port = _int_setting("DTOS_LISTEN_PORT", 9312)

        self.screen_width = _int_setting("DTOS_SCREEN_WIDTH", 1600)
        self.screen_height = _int_setting("DTOS_SCREEN_HEIGHT", 1000)
        self.screen_depth = _int_setting("DTOS_SCREEN_DEPTH", 24)

        self.dt_threads = _int_setting("DTOS_DT_THREADS", 2)

        self.share_name = _setting("DTOS_SHARE_NAME", "darktable-photos")
        self.share_root = _setting("DTOS_SHARE_ROOT") or self._discover_share()

        self.log_level = _setting("DTOS_LOG_LEVEL", "INFO").upper()

        # darktable's own install tree, untouched.
        self.app_dir = os.path.join(APP_ROOT, "app")
        self.darktable_binary = os.path.join(self.app_dir, "bin", "darktable")
        self.darktable_cli = os.path.join(self.app_dir, "bin", "darktable-cli")
        self.darktable_datadir = os.path.join(self.app_dir, "share", "darktable")

        # The bundled third-party runtime closure.
        self.depends_dir = os.path.join(APP_ROOT, "depends")
        self.depends_lib = os.path.join(self.depends_dir, "lib")
        self.depends_bin = os.path.join(self.depends_dir, "bin")
        self.depends_share = os.path.join(self.depends_dir, "share")
        self.depends_etc = os.path.join(self.depends_dir, "etc")

        self.xvfb_binary = os.path.join(self.depends_bin, "Xvfb")
        self.x11vnc_binary = os.path.join(self.depends_bin, "x11vnc")
        self.xauth_binary = os.path.join(self.depends_bin, "xauth")
        self.xkb_dir = os.path.join(self.depends_share, "X11", "xkb")
        self.fontconfig_file = os.path.join(self.depends_etc, "fonts", "fonts.conf")

        # Paths under data/
        self.home_dir = os.path.join(self.data_dir, "home")
        self.tmp_dir = os.path.join(self.data_dir, "tmp")
        self.run_dir = os.path.join(self.data_dir, "run")
        self.xauth_file = os.path.join(self.data_dir, "xauth", "Xauthority")
        self.fontconfig_cache = os.path.join(self.data_dir, "fontconfig-cache")
        self.dt_config_dir = os.path.join(self.data_dir, "config")
        self.dt_cache_dir = os.path.join(self.data_dir, "cache")
        self.dt_library = os.path.join(self.data_dir, "library.db")

        # Files under data/run
        self.session_file = os.path.join(self.run_dir, "session.json")
        self.lock_file = os.path.join(self.run_dir, "launcher.lock")

        # The VNC transport. A loopback-only TCP port rather than a unix
        # socket, because the x11vnc that Ubuntu 22.04 ships rejects
        # -rfbunixpath and -rfbunixmode as unrecognised options - measured, not
        # assumed. -localhost keeps the listener on 127.0.0.1, so the LAN still
        # cannot reach it and the only exposed port remains the HTTP one.
        self.vnc_port = _int_setting("DTOS_VNC_PORT", 9313)

    def _read_app_user(self):
        """Return the ``user`` field of the packaged config.ini, or None.

        None means the metadata could not be read, which is not fatal: the
        preparation step simply has nothing to chown and says so, rather than
        guessing an account name and changing the ownership of the wrong thing.
        """
        try:
            with open(self.config_ini, "r", encoding="utf-8") as handle:
                return json.load(handle).get("user") or None
        except (OSError, ValueError):
            return None

    def _discover_share(self):
        """Find the photograph share by name across the mounted volumes.

        The share is created by postinst with the platform's ter_share_add, so
        at startup it is usually already there. Discovery rather than a fixed
        path keeps the application working when the share was created on a
        volume other than the first.
        """
        try:
            entries = os.listdir("/")
        except OSError:
            return None
        for entry in sorted(entries):
            if not entry.startswith("Volume"):
                continue
            candidate = os.path.join("/", entry, self.share_name)
            if os.path.isdir(candidate):
                return candidate
        return None

    def ensure_directories(self):
        """Create the runtime directories with the modes glib and X expect."""
        for path in (
            self.data_dir,
            self.log_dir,
            self.home_dir,
            self.tmp_dir,
            self.run_dir,
            os.path.dirname(self.xauth_file),
            self.fontconfig_cache,
            self.dt_config_dir,
            self.dt_cache_dir,
        ):
            os.makedirs(path, exist_ok=True)

        # glib refuses XDG_RUNTIME_DIR unless it is mode 0700, and warns to the
        # journal on every start when it is not.
        for path in (
            self.data_dir,
            self.log_dir,
            self.run_dir,
            os.path.dirname(self.xauth_file),
        ):
            try:
                os.chmod(path, 0o700)
            except OSError:
                pass

    def read_password(self):
        """Return the access password, or None when it has not been generated.

        Read fresh on each authentication request rather than cached at
        startup, so an operator who edits access.txt to rotate the password
        does not have to restart the service for it to take effect.
        """
        try:
            with open(self.secret_file, "r", encoding="utf-8") as handle:
                password = handle.read().strip()
        except OSError:
            return None
        return password or None


config = Config()
