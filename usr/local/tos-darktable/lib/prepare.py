"""Root-privileged repair, run before every service start.

This exists because of an ordering fact that is not obvious and cost a
submission: **the platform creates the application account after postinst has
already run.** Anything postinst does that depends on that account therefore
fails during installation, and it fails *silently* - `chown` reports an
unknown user, `ter_share_add -owner` creates the folder but registers an owner
that does not exist yet. The service then starts as an account that owns
nothing and cannot even enter its own data directory, which surfaces as a
`PermissionError` deep inside the launcher.

So the work is done here instead, on every start, as root, where the account is
guaranteed to exist: the unit runs this through ``ExecStartPre=+`` before the
main process.

Two things it refuses to do:

* Guess an account name. If config.ini cannot be read, it says so and changes
  nothing, because chowning a tree to the wrong user is worse than not chowning
  it at all.
* Judge access to the shared folder as root. The platform grants access through
  an ACL on the object, and root bypasses ACLs, so ``os.access`` here would
  report a folder the service cannot enter as perfectly fine. The check forks,
  drops to the application account, and tries for real.
"""

import grp
import os
import pwd
import subprocess

from . import logging_setup

log = logging_setup.get_logger("prepare")

# Directories the service writes to. Ownership of these is what the service
# actually needs; the rest of the tree is read-only to it in practice.
WRITABLE = ("data", "logs")

# Modes, reasserted on every start because an upgrade unpacks new files with
# the modes the archive carried and the previous run's modes are not preserved.
MODE_7000 = {"data": 0o700, "logs": 0o700}
MODE_0750 = {"data/tmp": 0o750, "data/home": 0o750}
MODE_0700_NESTED = ("data/run", "data/xauth")


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def resolve_account(user):
    """Return ``(uid, gid, group_name)`` for an account, or None.

    The group comes from the account's own record rather than from a name
    matching the username. The platform creates application accounts with the
    shared group ``allusers`` as their primary group and does not create a
    group named after the application, so assuming a same-named group is how
    the first submission produced a unit that died with status=216/GROUP.
    """
    if not user:
        return None
    try:
        record = pwd.getpwnam(user)
    except KeyError:
        return None

    gid = record.pw_gid
    try:
        group_name = grp.getgrgid(gid).gr_name
    except KeyError:
        group_name = str(gid)
    return record.pw_uid, gid, group_name


def _chown_tree(path, uid, gid):
    """Recursively set ownership, following no symlinks out of the tree."""
    changed = 0
    for base, dirs, files in os.walk(path):
        for name in [None] + dirs + files:
            target = base if name is None else os.path.join(base, name)
            try:
                os.chown(target, uid, gid, follow_symlinks=False)
                changed += 1
            except OSError:
                pass
    return changed


def fix_ownership(config, uid, gid):
    """Give the application account ownership of everything it must write."""
    root = os.path.realpath(config.root)

    # The whole tree is chowned, not just the writable parts: a stale root-owned
    # file left by a previous install inside data/ is enough to stop the
    # service, and the tree is small.
    changed = _chown_tree(root, uid, gid)
    log.info("set ownership of %d path(s) to uid %d gid %d", changed, uid, gid)

    for name, mode in list(MODE_7000.items()) + list(MODE_0750.items()):
        path = os.path.join(config.root, name)
        try:
            os.makedirs(path, exist_ok=True)
            # chown after creating, always.
            #
            # makedirs creates as the *calling* user - root here - regardless
            # of who owns the parent, and the chown pass above ran before this
            # directory existed. Without this line a data/ that had to be
            # created is owned by root with the right mode, which looks
            # correct in a listing and is unusable by the service. That is
            # exactly what the verification container found.
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        except OSError as error:
            log.warning("cannot prepare %s: %s", path, error)

    for name in MODE_0700_NESTED:
        path = os.path.join(config.root, name)
        try:
            os.makedirs(path, exist_ok=True)
            os.chown(path, uid, gid)
            os.chmod(path, 0o700)
        except OSError as error:
            log.warning("cannot prepare %s: %s", path, error)


def can_access_as(path, uid, gid):
    """True when the application account can list ``path``.

    Checked by forking and dropping privileges, not with ``os.access``: root
    bypasses the platform's ACL, so an access check made as root answers a
    different question than the one that matters and gets it wrong in the
    direction that hurts - reporting an unusable folder as fine.
    """
    if not os.path.isdir(path):
        return False
    try:
        pid = os.fork()
    except OSError:
        return False

    if pid == 0:  # child
        try:
            os.setgid(gid)
            os.setuid(uid)
            os.listdir(path)
        except Exception:
            os._exit(1)
        os._exit(0)

    try:
        _pid, status = os.waitpid(pid, 0)
    except OSError:
        return False
    return os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0


def _volume():
    try:
        entries = sorted(os.listdir("/"))
    except OSError:
        return None
    for entry in entries:
        if entry.startswith("Volume"):
            return entry
    return None


def ensure_share(config, uid, gid):
    """Make sure the photograph folder exists and the service can reach it.

    ``ter_share_add`` is the platform's own tool and the only sanctioned way to
    obtain a shared folder the application account may write to. Two things
    about it are worth knowing:

    * ``-owner`` defaults to root, so it must be passed explicitly.
    * Calling it again for a folder that already exists is harmless, and is the
      repair that matters here: when the folder was created during installation
      the account did not exist yet, so the owner it recorded was not the
      service's. Re-running it now, with the account present, is what makes the
      folder usable.
    """
    name = config.share_name
    volume = _volume()

    if not volume:
        log.warning("no /Volume* directory found; the photograph folder is unavailable")
        return False

    share_path = os.path.join("/", volume, name)

    if can_access_as(share_path, uid, gid):
        log.info("shared folder %s is reachable by the service account", share_path)
        return True

    log.warning("%s is not reachable by the service account; re-registering it", share_path)

    tool = None
    for candidate in ("/usr/sbin/ter_share_add", "/usr/bin/ter_share_add"):
        if os.path.exists(candidate):
            tool = candidate
            break

    if tool is None:
        log.warning("ter_share_add is not installed; the photograph folder cannot be created")
        return False

    try:
        result = subprocess.run(
            [tool, "-device", volume, "-name", name, "-owner", config.app_user],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("ter_share_add failed: %s", error)
        return False

    if result.returncode != 0:
        log.warning(
            "ter_share_add exited %d: %s",
            result.returncode,
            (result.stderr or result.stdout or "").strip()[:200],
        )
        return False

    if can_access_as(share_path, uid, gid):
        log.info("shared folder %s is now reachable by the service account", share_path)
        return True

    # Not fatal. darktable starts and works; it simply has no folder to browse
    # until an administrator grants one, which is a far better outcome than
    # refusing to start the whole application over one directory.
    log.warning(
        "shared folder %s still is not reachable by %s. Grant access to it in the "
        "TOS control panel, or set DTOS_SHARE_ROOT to a folder that is already granted.",
        share_path,
        config.app_user,
    )
    return False


# The X server looks for its keymap compiler at a path baked into the binary,
# and writes the compiled keymap to a fixed output directory. Neither is
# configurable: `-xkbdir` moves where the keymap *sources* are read from and
# nothing else. Both facts were read out of the Xvfb binary itself rather than
# assumed:
#
#     '"%s%sxkbcomp" -w %d %s -xkm "%s" ... "%s%s.xkm"'
#     '/usr/bin'          the prefix the compiler path is built from
#     '/var/lib/xkb/'     the compiled keymap output directory
#
# TOS ships neither. Its dpkg database records xkb-data as installed while the
# files under /usr/share/X11/xkb are absent from the image, and x11-xkb-utils -
# which is what provides xkbcomp - is not installed at all. So the X server
# cannot compile a keymap and refuses to start:
#
#     XKB: Failed to compile keymap
#     Fatal server error: Failed to activate virtual core keyboard: 2
#
# That is a device-level blocker, not a packaging one, and it would hit any
# application that brings its own X server.
#
# The two things it needs are therefore supplied here, by the root-privileged
# preparation step, and both are declared in the package README as required.
# The symlink is created only when nothing is already at that path, so a system
# that does provide xkbcomp keeps its own.
XKBCOMP_PATH = "/usr/bin/xkbcomp"
XKB_OUTPUT_DIR = "/var/lib/xkb"


def ensure_xkb_support(config, uid, gid):
    """Give the bundled X server the two system paths it hard-codes."""
    # The compiled keymap directory. The X server runs as the application
    # account - the launcher drops privileges nowhere, but the unit starts it
    # as that user - so this has to be writable by that account, not by root.
    #
    # It is recreated on every start because on TOS /var is a symlink into
    # /tmp: the directory does not survive a reboot, and would otherwise be
    # missing on the first start after one.
    try:
        os.makedirs(XKB_OUTPUT_DIR, exist_ok=True)
        os.chown(XKB_OUTPUT_DIR, uid, gid)
        os.chmod(XKB_OUTPUT_DIR, 0o755)
        log.info("%s is ready, owned by %s", XKB_OUTPUT_DIR, config.app_user)
    except OSError as error:
        log.warning("cannot prepare %s: %s", XKB_OUTPUT_DIR, error)

    bundled = os.path.join(config.depends_bin, "xkbcomp")
    if not os.path.isfile(bundled):
        log.warning(
            "%s is missing from the bundle, so the X server cannot compile a "
            "keymap and will not start",
            bundled,
        )
        return

    if os.path.exists(XKBCOMP_PATH) or os.path.islink(XKBCOMP_PATH):
        log.info("%s already exists; leaving it alone", XKBCOMP_PATH)
        return

    try:
        os.symlink(bundled, XKBCOMP_PATH)
        log.info("linked %s -> %s for the X server", XKBCOMP_PATH, bundled)
    except OSError as error:
        # Not fatal here, but the X server will fail on its next start, so say
        # so plainly rather than letting it surface as a display error.
        log.error(
            "cannot create %s (%s). The X server will not start without it; "
            "the root filesystem may be read-only.",
            XKBCOMP_PATH,
            error,
        )


def run(config):
    """Entry point. Returns a process exit status; always 0 unless hopeless."""
    if not is_root():
        log.warning("preparation needs root to change ownership; skipping")
        return 0

    account = resolve_account(config.app_user)
    if account is None:
        # The account is created by the platform. If it is missing here, the
        # service is about to fail on its own User= directive with a clearer
        # message than anything this could add.
        log.warning(
            "the application account %r does not exist yet; nothing to prepare",
            config.app_user,
        )
        return 0

    uid, gid, group_name = account
    log.info("preparing for %s (uid %d, gid %d, group %s)", config.app_user, uid, gid, group_name)

    fix_ownership(config, uid, gid)
    ensure_share(config, uid, gid)
    ensure_xkb_support(config, uid, gid)

    # The service cannot run without somewhere to write. Reported here rather
    # than left to the traceback the launcher would otherwise produce.
    for name in WRITABLE:
        path = os.path.join(config.root, name)
        if not can_access_as(path, uid, gid):
            log.error(
                "%s is not writable by %s after preparation; the service cannot start",
                path,
                config.app_user,
            )
            # Walk the path and print what each level looks like.
            #
            # The reason is nearly always an ancestor the account cannot
            # traverse rather than the directory itself, and which one is not
            # visible from the failure. Printing the chain answers it in one
            # run instead of several: the first level whose mode lacks "other
            # execute" is the one blocking.
            probe = path
            while True:
                try:
                    status = os.stat(probe)
                    log.error(
                        "  %s  mode %04o  owner %d:%d",
                        probe,
                        status.st_mode & 0o7777,
                        status.st_uid,
                        status.st_gid,
                    )
                except OSError as error:
                    log.error("  %s  (%s)", probe, error)
                parent = os.path.dirname(probe)
                if parent == probe:
                    break
                probe = parent
            return 1

    return 0
