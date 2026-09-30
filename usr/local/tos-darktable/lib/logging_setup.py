"""Logging in the format the other TOS applications on this platform use."""

import logging
import logging.handlers
import os
import sys

_FORMAT = "[%(asctime)s] [%(levelname)s] [%(component)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}


class _ComponentFilter(logging.Filter):
    """Give every record a component name so the format string never fails.

    Records from a logger that did not set one would otherwise raise a
    formatting KeyError at emit time and the message would be lost exactly
    when it was most needed.
    """

    def filter(self, record):
        if not hasattr(record, "component"):
            record.component = record.name
        return True


def setup_logging(level_name, log_dir=None, to_file=True):
    """Configure the root logger and return it."""
    level = _LEVELS.get((level_name or "INFO").upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    component_filter = _ComponentFilter()

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    stream.addFilter(component_filter)
    root.addHandler(stream)

    # A file handler as well as the journal: journald is the right place for
    # operational logs, but the launcher's own startup trace is what an
    # operator reads when the application will not come up, and that is much
    # easier to hand to someone as a file.
    if to_file and log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            path = os.path.join(log_dir, "launcher.log")
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(component_filter)
            root.addHandler(file_handler)
        except OSError:
            # A log file that cannot be written must not stop the application.
            root.warning("cannot write %s; logging to stderr only", log_dir)

    return root


def get_logger(component):
    return logging.getLogger(component)
