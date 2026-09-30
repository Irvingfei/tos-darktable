"""darktable for TOS - application package header.

The version literal here is one of the locations synchronised by
tools/bump_version.py from the VERSION file at the repository root; it is
reported by the /health endpoint so an operator can tell which build is
actually running without querying dpkg.
"""

__version__ = "1.0.14"

APP_ID = "tos-darktable"
SERVICE_NAME = "tos-darktable.service"
