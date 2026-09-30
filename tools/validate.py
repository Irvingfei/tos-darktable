#!/usr/bin/env python3
"""Pre-submission validator for the TOS 7 darktable package.

Mirrors the automated validation the TOS Developer Platform performs (guide
chapter 16.1) plus the manual review checklist (16.2), so that a failure is
found here instead of after a submission has been rejected. The platform
rejects on the first problem it finds and the queue is days long, so the value
of this file is entirely in what it catches before that.

Checks:

config.ini      valid JSON, no comments, no single quotes, no trailing commas,
                no BOM, all required fields, type/open_path exclusivity,
                ${ip} placeholder for external open, lowercase keys, id
                character set, category whitelist and the three-category
                limit, low_version >= 7.0, no reserved field names
app.lang        all 14 languages, name/auth/descript non-empty, field length
                limits, UTF-8 without BOM, LF line endings
icon            SVG, viewBox present, size and element limits, no scripting
                constructs, path exactly matching config.ini
systemd         non-root User/Group matching config.ini, correct ExecStart,
                multi-user.target; namespace sandboxing refused (see below)
DEBIAN/control  Package/Version match config.ini, Debian architecture name,
                no dependency on a runtime TOS does not ship
lifecycle       present, LF, bash shebang, set -e; the application user is not
                created (guide 10.3 forbids it); no network installation;
                postrm does not delete the photograph share
layout          DEBIAN at the package root, metadata under usr/local/<appid>/,
                nothing duplicated at the root
line endings    no CRLF anywhere in a text payload
ports           no reference to a TOS reserved port
secrets         no obvious hardcoded credential
version         every derived version literal matches VERSION

Usage:
    python tools/validate.py [--json]
Exit code 0 means every check passed.
"""

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_ID = "tos-darktable"
SYSTEM_ID = "tos-darktable"
PACKAGE = "tos-darktable"
SHARE_NAME = "darktable-photos"
LISTEN_PORT = 9312

APP_ROOT = os.path.join(REPO_ROOT, "usr", "local", APP_ID)
DEBIAN_DIR = os.path.join(REPO_ROOT, "DEBIAN")

REQUIRED_LANGUAGES = [
    "zh-cn", "zh-hk", "en-us", "fr-fr", "de-de", "it-it", "es-es",
    "hu-hu", "ja-jp", "ko-kr", "pl-pl", "ru-ru", "tr-tr", "pt-pt",
]

ALLOWED_CATEGORIES = {
    "Audio_Video_Entertainment",
    "Photography_Video",
    "Backup_Sync",
    "Development_Tools",
    "Utilities",
    "Web_Services",
    "Security",
    "Download",
    "Driver",
    "Artificial_Intelligence",
}

RESERVED_CONFIG_FIELDS = {
    "host_network", "container_runtime", "sandbox", "auto_update",
    "upstream_url", "license", "min_memory", "min_cpu", "min_disk",
}

REQUIRED_CONFIG_FIELDS = [
    "id", "icon", "publisher", "exec", "version", "recommend", "beta",
    "low_version", "category", "platform", "application_type", "user",
]

RESERVED_PORTS = {
    22: "SSH", 80: "TOS Web HTTP", 443: "TOS HTTPS", 445: "SMB",
    3306: "MySQL", 5050: "TOS Daemon", 5432: "PostgreSQL",
    6379: "Redis", 8181: "TOS Nginx", 8443: "TOS HTTPS UI",
}

LINE_ENDING_EXTENSIONS = (
    ".sh", ".py", ".ini", ".lang", ".service", ".conf", ".env",
    ".js", ".css", ".html", ".json", ".md", ".txt",
)

# Each pattern captures the value and requires it to look like a *literal*.
# The negated character class excludes '$', '(', ')' and '/', which is what
# separates a real credential from the two things that look like one:
#   PASSWORD="$(head -c 32 /dev/urandom | base64 | ...)"   a shell expansion
#   SECRET="$ROOT/data/access.txt"                         a file path
# Both are correct code, and a detector that flagged them would be switched off
# by the first person it inconvenienced - which is worse than not having it.
SECRET_PATTERNS = [
    (re.compile(r"""(?:password|passwd|pwd)\s*[:=]\s*['"]([^'"$()/]{4,})['"]""", re.IGNORECASE),
     "hardcoded password"),
    (re.compile(r"""api[_-]?key\s*[:=]\s*['"]([^'"$()/]{8,})['"]""", re.IGNORECASE),
     "hardcoded API key"),
    (re.compile(r"""(?:secret|token)\s*[:=]\s*['"]([^'"$()/]{8,})['"]""", re.IGNORECASE),
     "hardcoded secret"),
    (re.compile(r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"), "embedded private key"),
]

# Directories that are build products or third-party code. Scanning them is
# both slow and pointless: they are not authored here, and a false positive in
# a vendored file would block a legitimate release.
SCAN_SKIP_DIRS = {".git", "__pycache__", "build", "dist", "vendor", "depends", "app", "inspect"}

NETWORK_INSTALL_COMMANDS = (
    "apt install", "apt-get install", "aptitude install",
    "pip install", "pip3 install", "npm install", "yarn install",
    "dnf install", "yum install",
)

COMMAND_WRAPPERS = ("sudo", "command", "env", "timeout", "nohup", "if", "then", "else", "do", "!")


class Report(object):
    def __init__(self):
        self.errors = []
        self.warnings = []
        self.passed = []

    def error(self, check, detail=""):
        self.errors.append({"check": check, "detail": detail})

    def warn(self, check, detail=""):
        self.warnings.append({"check": check, "detail": detail})

    def ok(self, check):
        self.passed.append(check)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def _iter_repo_files(skip_dirs=SCAN_SKIP_DIRS):
    for base, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [item for item in dirs if item not in skip_dirs]
        for name in files:
            yield os.path.join(base, name)


# --------------------------------------------------------------------------- #
# config.ini
# --------------------------------------------------------------------------- #


def check_config_ini(report):
    path = os.path.join(APP_ROOT, "config.ini")
    if not os.path.isfile(path):
        report.error("config.ini present", "missing at usr/local/%s/config.ini" % APP_ID)
        return None
    report.ok("config.ini present")

    raw = read_bytes(path)
    if raw.startswith(b"\xef\xbb\xbf"):
        report.error("config.ini encoding", "UTF-8 BOM detected")
    else:
        report.ok("config.ini has no BOM")

    if b"\r\n" in raw:
        report.error("config.ini line endings", "CRLF detected, must be LF")
    else:
        report.ok("config.ini uses LF")

    text = raw.decode("utf-8", "replace")

    without_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)
    if "//" in without_strings or "/*" in without_strings:
        report.error("config.ini has no comments", "// or /* */ found outside strings")
    else:
        report.ok("config.ini has no comments")

    if re.search(r"'[^']*'\s*:", text):
        report.error("config.ini uses double quotes", "single quoted key found")
    else:
        report.ok("config.ini uses double quoted keys")

    if re.search(r",\s*[}\]]", text):
        report.error("config.ini has no trailing commas", "trailing comma before } or ]")
    else:
        report.ok("config.ini has no trailing commas")

    if re.search(r"[“”‘’]", text):
        report.error("config.ini uses ASCII quotes", "full-width quote detected")
    else:
        report.ok("config.ini uses ASCII quotes only")

    try:
        config = json.loads(text)
    except ValueError as error:
        report.error("config.ini is valid JSON", str(error))
        return None
    report.ok("config.ini is valid JSON")

    for field in REQUIRED_CONFIG_FIELDS:
        if field not in config:
            report.error("config.ini required field", "missing: %s" % field)
        elif config[field] in (None, ""):
            report.error("config.ini required field", "empty: %s" % field)
    report.ok("config.ini required fields checked")

    for field in RESERVED_CONFIG_FIELDS:
        if field in config:
            report.error("config.ini reserved fields", "reserved field used: %s" % field)
    report.ok("config.ini avoids reserved fields")

    upper = [key for key in config if key != key.lower()]
    if upper:
        report.error("config.ini lowercase keys", "uppercase keys: %s" % ", ".join(upper))
    else:
        report.ok("config.ini keys are lowercase")

    if config.get("id") != APP_ID:
        report.error("config.ini id", "expected %s, found %s" % (APP_ID, config.get("id")))
    else:
        report.ok("config.ini id matches the package id")

    if config.get("system_id") != SYSTEM_ID:
        report.error("config.ini system_id", "expected %s, found %s" % (SYSTEM_ID, config.get("system_id")))
    else:
        report.ok("config.ini system_id matches the service unit")

    if config.get("package") != PACKAGE:
        report.error("config.ini package", "expected %s, found %s" % (PACKAGE, config.get("package")))
    else:
        report.ok("config.ini package matches DEBIAN/control")

    if config.get("application_type") != "deb":
        report.error("config.ini application_type", "expected deb")
    else:
        report.ok("config.ini application_type is deb")

    # Subtype. This application is WebUI External Open: it opens its own port in
    # a new tab rather than being embedded in the desktop, because a VNC stream
    # needs a WebSocket and the platform's embedded route is documented for
    # plain HTTP. The two subtypes are mutually exclusive by definition.
    if "type" in config and "open_path" in config:
        report.error(
            "config.ini subtype exclusivity",
            "type and open_path must not both be present",
        )
    elif config.get("open_path") is True:
        report.ok("config.ini subtype is external open (open_path, no type)")
        path_value = config.get("path") or ""
        if "${ip}" not in path_value:
            report.error(
                "config.ini path placeholder",
                "an external-open path must use ${ip}, found %r" % path_value,
            )
        elif not path_value.startswith("http://") and not path_value.startswith("https://"):
            report.error("config.ini path scheme", "expected an http:// URL, found %r" % path_value)
        else:
            report.ok("config.ini path uses the ${ip} placeholder")
        webui = os.path.join(APP_ROOT, "webui.bz2")
        source = os.path.join(REPO_ROOT, "webui", "index.html")
        if os.path.isfile(webui) or os.path.isfile(source):
            report.ok("webui payload is available for the external-open subtype")
        else:
            report.error(
                "config.ini external-open payload",
                "webui.bz2 or a webui/index.html source tree is required",
            )
    else:
        report.error(
            "config.ini subtype",
            "expected open_path true; found type=%r open_path=%r"
            % (config.get("type"), config.get("open_path")),
        )

    if config.get("type") == "iframe":
        report.error("config.ini subtype", "this application is not an iframe application")

    # The nginx route and the listener must agree with each other.
    nginx_conf = os.path.join(APP_ROOT, "nginx", "%s.conf" % APP_ID)
    if not os.path.isfile(nginx_conf):
        report.warn("nginx route", "no nginx/%s.conf shipped" % APP_ID)
    else:
        body = read_bytes(nginx_conf).decode("utf-8", "replace")
        if "proxy_pass http://127.0.0.1:%d/" % LISTEN_PORT not in body:
            report.error(
                "nginx route",
                "proxy_pass does not target 127.0.0.1:%d" % LISTEN_PORT,
            )
        else:
            report.ok("nginx route targets the declared port")
        # Without these two headers the WebSocket handshake never completes
        # through the platform's nginx, and the page hangs on "connecting"
        # while the plain HTTP health endpoint answers normally.
        if "Upgrade" not in body or "Connection" not in body:
            report.error(
                "nginx route",
                "a WebSocket upgrade needs proxy_set_header Upgrade and Connection",
            )
        else:
            report.ok("nginx route forwards the WebSocket upgrade")

    categories = config.get("category") or []
    if not isinstance(categories, list) or not categories:
        report.error("config.ini category", "at least one category is required")
    else:
        if len(categories) > 3:
            report.error("config.ini category limit", "at most 3, found %d" % len(categories))
        else:
            report.ok("config.ini category count is within the limit")
        unknown = [item for item in categories if item not in ALLOWED_CATEGORIES]
        if unknown:
            report.error("config.ini category whitelist", "unknown: %s" % ", ".join(unknown))
        else:
            report.ok("config.ini categories are all official")

    if config.get("recommend") is not False:
        report.error("config.ini recommend flag", "developers must submit with recommend=false")
    else:
        report.ok("config.ini recommend is false")

    if config.get("platform") not in ("x86_64", "aarch64"):
        report.error("config.ini platform", "must be x86_64 or aarch64")
    else:
        report.ok("config.ini platform is valid")

    low = str(config.get("low_version") or "")
    match = re.match(r"^(\d+)(?:\.(\d+))?", low)
    if not match or int(match.group(1)) < 7:
        report.error("config.ini low_version", "must be TOS 7.0 or higher, found %r" % low)
    else:
        report.ok("config.ini low_version is TOS 7.0+")

    if str(config.get("user") or "").lower() in ("root", ""):
        report.error("config.ini user", "running as root is a one-vote veto")
    else:
        report.ok("config.ini declares a non-root runtime user")

    icon = str(config.get("icon") or "")
    expected = "/images/icons/%s.svg" % APP_ID
    if icon != expected:
        report.error("config.ini icon path", "expected %s, found %s" % (expected, icon))
    elif not os.path.isfile(os.path.join(APP_ROOT, "images", "icons", "%s.svg" % APP_ID)):
        report.error("config.ini icon file", "icon file not found at %s" % icon)
    else:
        report.ok("config.ini icon path matches the packaged file")

    if not re.match(r"^[a-z][a-z0-9-]{0,49}$", str(config.get("id") or "")):
        report.error("config.ini id format", "lowercase letters, digits and hyphens, starting with a letter")
    else:
        report.ok("config.ini id format is valid")

    return config


# --------------------------------------------------------------------------- #
# app.lang
# --------------------------------------------------------------------------- #


def check_lang(report):
    path = os.path.join(APP_ROOT, "%s.lang" % APP_ID)
    if not os.path.isfile(path):
        report.error("app.lang present", "missing %s.lang" % APP_ID)
        return

    raw = read_bytes(path)
    if raw.startswith(b"\xef\xbb\xbf"):
        report.error("app.lang encoding", "UTF-8 BOM detected")
    else:
        report.ok("app.lang has no BOM")

    if b"\r\n" in raw:
        report.error("app.lang line endings", "CRLF detected, must be LF")
    else:
        report.ok("app.lang uses LF")

    sections = {}
    current = None
    for line in raw.decode("utf-8", "replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^\[([a-z]{2}-[a-z]{2})\]$", stripped)
        if match:
            current = match.group(1)
            sections[current] = {}
            continue
        if current and "=" in stripped:
            key, _, value = stripped.partition("=")
            sections[current][key.strip()] = value.strip().strip('"')

    missing = [tag for tag in REQUIRED_LANGUAGES if tag not in sections]
    if missing:
        report.error("app.lang completeness", "missing languages: %s" % ", ".join(missing))
    else:
        report.ok("app.lang declares all 14 languages")

    problems = []
    limits = {"name": 64, "auth": 128, "descript": 512, "important": 512, "release_note": 2048}
    for tag in REQUIRED_LANGUAGES:
        block = sections.get(tag)
        if not block:
            continue
        for field in ("name", "auth", "descript"):
            if not block.get(field):
                problems.append("%s.%s is empty" % (tag, field))
        for field, limit in limits.items():
            value = block.get(field) or ""
            if len(value) > limit:
                problems.append("%s.%s is %d characters (limit %d)" % (tag, field, len(value), limit))
            if "<" in value.replace("</br>", ""):
                problems.append("%s.%s contains HTML other than </br>" % (tag, field))

    if problems:
        for item in problems:
            report.error("app.lang field rules", item)
    else:
        report.ok("app.lang fields are non-empty and within their limits")


# --------------------------------------------------------------------------- #
# icon
# --------------------------------------------------------------------------- #

FORBIDDEN_ICON_TAGS = ("<script", "<foreignObject", "<iframe", "<object", "<embed")


def check_icon(report):
    path = os.path.join(APP_ROOT, "images", "icons", "%s.svg" % APP_ID)
    if not os.path.isfile(path):
        report.error("icon present", "missing %s" % path)
        return

    raw = read_bytes(path)
    text = raw.decode("utf-8", "replace")

    if "<svg" not in text:
        report.error("icon is SVG", "no <svg> element")
    else:
        report.ok("icon is SVG")

    if not re.search(r'viewBox\s*=\s*"[^"]+"', text):
        report.error("icon viewBox", "viewBox attribute is missing")
    else:
        report.ok("icon declares a viewBox")

    size = len(raw)
    if size > 50 * 1024:
        report.error("icon size", "%d bytes exceeds the 50 KB limit" % size)
    else:
        report.ok("icon is within the size limit (%d bytes)" % size)

    starts = text.count("<")
    if starts > 50:
        report.error("icon element count", "%d start tags exceeds the limit of 50" % starts)
    else:
        report.ok("icon start-tag count is within the limit")

    for forbidden in FORBIDDEN_ICON_TAGS:
        if forbidden.lower() in text.lower():
            report.error("icon scripting", "contains %s" % forbidden)

    if re.search(r'\son[a-z]+\s*=', text, re.IGNORECASE):
        report.error("icon scripting", "contains an on* event attribute")

    if re.search(r'(href|xlink:href)\s*=\s*"(javascript|vbscript|file|ftp|about|blob):', text, re.IGNORECASE):
        report.error("icon scripting", "contains a disallowed href scheme")

    if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
        report.error("icon entities", "DOCTYPE or ENTITY directives are not allowed")

    if "<image" in text and "base64" in text:
        report.warn("icon vector quality", "embedded raster data found in the SVG")

    # A full-bleed opaque rectangle is what an icon with no transparent
    # background looks like, and transparency is a stated review criterion.
    if re.search(r'<rect[^>]*width\s*=\s*"(512|100%)"[^>]*height', text) and 'fill="#' in text:
        report.warn("icon transparency", "a full-size filled rect may mean an opaque background")

    if not any(item.startswith("icon ") for item in report.errors and [] or []):
        report.ok("icon has no scripting constructs")


# --------------------------------------------------------------------------- #
# systemd unit
# --------------------------------------------------------------------------- #


def _active_directives(text):
    """Return the uncommented lines. A commented directive is documentation."""
    active = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        active.append(stripped)
    return active


def check_systemd(report, config):
    path = os.path.join(APP_ROOT, "init.d", "%s.service" % SYSTEM_ID)
    if not os.path.isfile(path):
        report.error("service unit present", "missing init.d/%s.service" % SYSTEM_ID)
        return

    text = read_bytes(path).decode("utf-8", "replace")
    active = "\n".join(_active_directives(text))

    user = re.search(r"^User\s*=\s*(.+)$", text, re.MULTILINE)
    group = re.search(r"^Group\s*=\s*(.+)$", text, re.MULTILINE)

    if not user or user.group(1).strip() == "root":
        report.error("service runs as non-root", "User is missing or root")
    else:
        report.ok("service declares a non-root User")

    if not group:
        report.warn("service Group", "Group directive is missing")
    else:
        report.ok("service declares a Group")

    if user and config and user.group(1).strip() != config.get("user"):
        report.error(
            "service user matches config.ini",
            "service User=%s but config.ini user=%s" % (user.group(1).strip(), config.get("user")),
        )
    else:
        report.ok("service user matches config.ini")

    for directive in ("StartLimitBurst", "StartLimitIntervalSec", "NoNewPrivileges",
                      "ExecStart", "WantedBy=multi-user.target"):
        if directive not in active:
            report.warn("service hardening", "%s is missing" % directive)
        else:
            report.ok("service declares %s" % directive)

    # Namespace-based sandboxing is refused here, and refusing it is the point.
    # On TOS, /var and several other standard paths are symlinks into /tmp, and
    # systemd fails while preparing /run/systemd/unit-root for any directive
    # that needs a mount namespace; the unit then exits with status=226
    # (NAMESPACE) before the program is ever reached. Chapter 12.7 of the guide
    # lists these as required hardening, and following it produces a service
    # that cannot start. PrivateTmp would additionally break X11 outright,
    # because the display socket has to be reachable at the shared
    # /tmp/.X11-unix path, which is a fixed X11 protocol location.
    for banned in ("PrivateTmp", "ProtectSystem", "ProtectHome", "ReadWritePaths"):
        for directive in _active_directives(text):
            if re.match(r"%s\s*=" % re.escape(banned), directive):
                report.error(
                    "service avoids namespace sandboxing",
                    "%s needs a mount namespace and fails on TOS with status=226/NAMESPACE" % banned,
                )
                break
    report.ok("service avoids namespace based sandboxing")

    match = re.search(r"^ExecStart\s*=\s*(.+)$", text, re.MULTILINE)
    if match:
        target = match.group(1).strip().split()[0]
        expected = "/usr/local/%s/bin/darktable-server" % APP_ID
        if target != expected:
            report.error("service ExecStart path", "expected %s, found %s" % (expected, target))
        elif not os.path.isfile(os.path.join(APP_ROOT, "bin", "darktable-server")):
            report.error("service ExecStart target", "the executable is not packaged")
        else:
            report.ok("service ExecStart points at the packaged executable")


# --------------------------------------------------------------------------- #
# DEBIAN/control and lifecycle scripts
# --------------------------------------------------------------------------- #


def check_control(report, config):
    path = os.path.join(DEBIAN_DIR, "control")
    if not os.path.isfile(path):
        report.error("DEBIAN/control present", "missing")
        return

    text = read_bytes(path).decode("utf-8", "replace")
    fields = {}
    for line in text.splitlines():
        if not line or line.startswith(" ") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()

    for required in ("Package", "Version", "Architecture", "Maintainer", "Description"):
        if required not in fields:
            report.error("DEBIAN/control fields", "missing: %s" % required)
    report.ok("DEBIAN/control declares the required fields")

    if fields.get("Package") != PACKAGE:
        report.error(
            "DEBIAN/control Package matches config.ini",
            "control=%s config.ini=%s" % (fields.get("Package"), PACKAGE),
        )
    else:
        report.ok("DEBIAN/control Package matches config.ini")

    if config and fields.get("Version") != config.get("version"):
        report.error(
            "DEBIAN/control Version matches config.ini",
            "control=%s config.ini=%s" % (fields.get("Version"), config.get("version")),
        )
    else:
        report.ok("DEBIAN/control Version matches config.ini")

    if fields.get("Architecture") not in ("amd64", "arm64"):
        report.error(
            "DEBIAN/control Architecture",
            "must be a Debian architecture name, found %r" % fields.get("Architecture"),
        )
    else:
        report.ok("DEBIAN/control Architecture is a Debian architecture name")

    depends = fields.get("Depends") or ""
    if "python3" not in depends:
        report.error("DEBIAN/control Depends", "python3 must be declared")
    else:
        report.ok("DEBIAN/control declares python3")
    for forbidden in ("nodejs", "default-jre", "golang", "openjdk"):
        if forbidden in depends:
            report.error(
                "DEBIAN/control Depends",
                "%s is not preinstalled on TOS and must not be a hard dependency" % forbidden,
            )
    report.ok("DEBIAN/control avoids runtimes TOS does not ship")


def find_network_installations(text):
    """Return network-installation commands a script actually *runs*.

    A plain substring scan cannot tell a command from a mention of one, so it
    flags the comment that explains the ban and the echo that tells the
    operator what to run - both of which a correct script contains.
    """
    hits = []
    for raw_line in text.splitlines():
        line = re.sub(r"(^|\s)#.*$", "", raw_line)
        if not line.strip():
            continue
        for fragment in re.split(r"&&|\|\||;", line):
            tokens = fragment.strip().split()
            index = 0
            while index < len(tokens) and tokens[index] in COMMAND_WRAPPERS:
                index += 1
                if index < len(tokens) and re.match(r"^\d+[smhd]?$", tokens[index]):
                    index += 1
            joined = " ".join(tokens[index:])
            for banned in NETWORK_INSTALL_COMMANDS:
                if joined.startswith(banned):
                    hits.append(banned)
        if re.search(r"\bcurl\b[^|]*\|\s*(?:ba|z|k)?sh\b", line):
            hits.append("curl | shell")
    return hits


def check_lifecycle(report):
    for name in ("preinst", "postinst", "prerm", "postrm"):
        path = os.path.join(DEBIAN_DIR, name)
        if not os.path.isfile(path):
            report.error("lifecycle script %s" % name, "missing")
            continue

        raw = read_bytes(path)
        text = raw.decode("utf-8", "replace")

        if b"\r\n" in raw:
            report.error("lifecycle script %s" % name, "CRLF detected, must be LF")
        if not text.startswith("#!/bin/bash"):
            report.warn("lifecycle script %s" % name, "missing a bash shebang")
        if "set -e" not in text.replace("#!/bin/bash", ""):
            report.warn("lifecycle script %s" % name, "does not use 'set -e'")

        for banned in find_network_installations(text):
            report.error("lifecycle script %s" % name, "network installation is prohibited: %s" % banned)

        report.ok("lifecycle script %s inspected" % name)

    # The application user belongs to the platform. Guide 10.3 is explicit that
    # the platform creates it and that lifecycle scripts MUST NOT, because a
    # script creating it produces an account with attributes the platform did
    # not choose and then does not own.
    for name in ("preinst", "postinst"):
        path = os.path.join(DEBIAN_DIR, name)
        if not os.path.isfile(path):
            continue
        text = read_bytes(path).decode("utf-8", "replace")
        for pattern in (r"^\s*useradd\b", r"^\s*adduser\b", r"^\s*groupadd\b"):
            if re.search(pattern, text, re.MULTILINE):
                report.error(
                    "application user is platform-created",
                    "%s creates the application account; guide 10.3 forbids it" % name,
                )
    report.ok("lifecycle scripts leave the application account to the platform")

    # Uninstalling must not destroy the user's photographs.
    postrm = os.path.join(DEBIAN_DIR, "postrm")
    if os.path.isfile(postrm):
        text = read_bytes(postrm).decode("utf-8", "replace")
        offender = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if (stripped.startswith("rm ") or " rm " in stripped) and (
                SHARE_NAME in stripped or "/Volume" in stripped
            ):
                offender = stripped
        if offender:
            report.error(
                "uninstall safety",
                "postrm deletes the photograph share: %s" % offender[:70],
            )
        else:
            report.ok("postrm leaves the photograph share in place")

    # And the app-owned paths must actually be cleaned.
    if os.path.isfile(postrm):
        text = read_bytes(postrm).decode("utf-8", "replace")
        if "/data" not in text or "/logs" not in text:
            report.warn("uninstall completeness", "postrm does not remove data/ and logs/")


# --------------------------------------------------------------------------- #
# layout, line endings, ports, secrets
# --------------------------------------------------------------------------- #


def check_layout(report):
    if not os.path.isdir(DEBIAN_DIR):
        report.error("package layout", "DEBIAN is missing at the package root")
        return
    report.ok("DEBIAN is at the package root")

    metadata = [
        ("config.ini", os.path.join(APP_ROOT, "config.ini")),
        ("%s.lang" % APP_ID, os.path.join(APP_ROOT, "%s.lang" % APP_ID)),
        ("images/icons/%s.svg" % APP_ID, os.path.join(APP_ROOT, "images", "icons", "%s.svg" % APP_ID)),
    ]
    for label, path in metadata:
        if not os.path.isfile(path):
            report.error("metadata under usr/local/<appid>", "missing %s" % label)
    report.ok("metadata lives under usr/local/%s/" % APP_ID)

    # Metadata beside DEBIAN/ would break the platform's parser, which reads it
    # out of data.tar rather than from the archive root.
    for stray in ("config.ini", "%s.lang" % APP_ID, "images"):
        if os.path.exists(os.path.join(REPO_ROOT, stray)):
            report.error(
                "metadata is not at the deb root",
                "%s exists beside DEBIAN/ and would break metadata parsing" % stray,
            )
    report.ok("no metadata duplicated at the deb root")

    if not os.path.isfile(os.path.join(APP_ROOT, "bin", "darktable-server")):
        report.error("launcher present", "missing bin/darktable-server")
    else:
        report.ok("launcher entry point present")

    lib_dir = os.path.join(APP_ROOT, "lib")
    if not os.path.isdir(lib_dir):
        report.error("launcher modules", "lib/ is missing")
    else:
        modules = [name for name in os.listdir(lib_dir) if name.endswith(".py")]
        if len(modules) < 5:
            report.warn("launcher modules", "only %d module(s) found" % len(modules))
        else:
            report.ok("launcher modules packaged (%d files)" % len(modules))


def check_line_endings(report):
    offenders = []
    for path in _iter_repo_files():
        if not path.endswith(LINE_ENDING_EXTENSIONS):
            continue
        try:
            if b"\r\n" in read_bytes(path):
                offenders.append(os.path.relpath(path, REPO_ROOT))
        except OSError:
            continue
    if offenders:
        for item in offenders[:20]:
            report.error("LF line endings", "CRLF in %s" % item)
    else:
        report.ok("all text payload files use LF line endings")


def check_ports(report):
    scanned = []
    for relative in (
        os.path.join("usr", "local", APP_ID, "%s.env" % APP_ID),
        os.path.join("usr", "local", APP_ID, "init.d", "%s.service" % SYSTEM_ID),
        os.path.join("usr", "local", APP_ID, "nginx", "%s.conf" % APP_ID),
        os.path.join("DEBIAN", "postinst"),
        os.path.join("DEBIAN", "preinst"),
    ):
        path = os.path.join(REPO_ROOT, relative)
        if not os.path.isfile(path):
            continue
        text = re.sub(r"#.*", "", read_bytes(path).decode("utf-8", "replace"))
        for match in re.finditer(r"\b(\d{2,5})\b", text):
            value = int(match.group(1))
            if value in RESERVED_PORTS:
                report.error(
                    "reserved port usage",
                    "%s references reserved port %d (%s)" % (relative, value, RESERVED_PORTS[value]),
                )
        scanned.append(relative)
    if scanned:
        report.ok("no TOS reserved port referenced (%d file(s) scanned)" % len(scanned))

    if LISTEN_PORT not in range(8000, 20000):
        report.error("declared port", "%d is outside the recommended 8000-19999 range" % LISTEN_PORT)
    else:
        report.ok("declared port %d is inside the recommended range" % LISTEN_PORT)


def check_secrets(report):
    hits = []
    for path in _iter_repo_files():
        if path.endswith((".md", ".lang", ".json", ".svg")):
            continue
        try:
            raw = read_bytes(path)
        except OSError:
            continue
        if b"\x00" in raw[:1024]:
            continue
        text = raw.decode("utf-8", "replace")
        for pattern, label in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append("%s: %s" % (os.path.relpath(path, REPO_ROOT), label))
    if hits:
        for item in hits:
            report.error("no hardcoded credentials", item)
    else:
        report.ok("no hardcoded credential pattern found")


def check_version_consistency(report):
    tools_dir = os.path.dirname(os.path.abspath(__file__))
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    try:
        import bump_version
    except ImportError as error:
        report.error("version single source", "tools/bump_version.py is not importable: %s" % error)
        return

    version, drift = bump_version.check(quiet=True)
    if drift:
        for label, value in drift:
            report.error("version consistency", "%s carries %r but VERSION is %r" % (label, value, version))
    else:
        report.ok("version literals all read %s (VERSION)" % version)


# --------------------------------------------------------------------------- #

def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate the TOS 7 package for darktable")
    parser.add_argument("--json", action="store_true", help="emit machine readable JSON")
    args = parser.parse_args(argv)

    report = Report()
    config = check_config_ini(report)
    check_lang(report)
    check_icon(report)
    check_systemd(report, config)
    check_control(report, config)
    check_lifecycle(report)
    check_layout(report)
    check_line_endings(report)
    check_ports(report)
    check_secrets(report)
    check_version_consistency(report)

    if args.json:
        print(json.dumps(
            {"errors": report.errors, "warnings": report.warnings, "passed": len(report.passed)},
            ensure_ascii=False, indent=2,
        ))
    else:
        print("TOS 7 package validation - %s" % APP_ID)
        print("=" * 62)
        for item in report.errors:
            print("  [ERROR] %s: %s" % (item["check"], item["detail"]))
        for item in report.warnings:
            print("  [WARN ] %s: %s" % (item["check"], item["detail"]))
        print("-" * 62)
        print("passed checks : %d" % len(report.passed))
        print("warnings      : %d" % len(report.warnings))
        print("errors        : %d" % len(report.errors))
        if report.errors:
            print("\nRESULT: FAILED - fix the errors above before submitting.")
        else:
            print("\nRESULT: PASSED")

    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
