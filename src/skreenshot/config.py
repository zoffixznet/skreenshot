"""Configuration file: a hand-editable YAML file at
$XDG_CONFIG_HOME/skreenshot/config.yaml (default ~/.config/skreenshot/config.yaml).

Created with commented defaults on first run so there is always a file to edit.
Resolution is pure -- a parsed mapping plus an environ mapping in, resolved
values out -- so the two environment variables that predate the file
(SKREENSHOT_DIM, SKREENSHOT_LOG) still work as one-off overrides:
env var > config file > built-in default.
"""

import logging
import math
import os
from collections import namedtuple

import yaml

log = logging.getLogger("skreenshot")

DEFAULT_DIM = 140
DEFAULT_SAVE_DIR = "~/Pictures"

Config = namedtuple("Config", ["save_dir", "dim", "log_file"])

DEFAULT_CONFIG_TEXT = """\
# skreenshot configuration. Edit by hand; every key is optional and falls back
# to the built-in default if removed.

# Folder the Shift+drag "Save As" dialog opens to (~ is expanded). If it does
# not exist, the dialog falls back to your home directory.
save_dir: ~/Pictures

# Overlay dim opacity while selecting, 0-255 (higher is darker). Default 140.
# The SKREENSHOT_DIM environment variable overrides this for a single run.
dim: 140

# Append a debug log to this file (~ is expanded). Empty/null means no log file.
# The SKREENSHOT_LOG environment variable overrides this; --verbose also logs
# to stderr.
log_file:
"""


def config_path(environ=None):
    """Absolute path to the config file, honoring XDG_CONFIG_HOME."""
    env = os.environ if environ is None else environ
    base = env.get("XDG_CONFIG_HOME")
    # Per the XDG spec, a relative (non-absolute) value must be ignored.
    if not base or not os.path.isabs(base):
        home = env.get("HOME") or os.path.expanduser("~")
        base = os.path.join(home, ".config")
    return os.path.join(base, "skreenshot", "config.yaml")


def ensure_config_file(path):
    """Write the commented default config at path if it does not exist yet.

    Best effort: a failure to create it is not fatal (defaults still apply).
    """
    if os.path.exists(path):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(DEFAULT_CONFIG_TEXT)
    except OSError as exc:
        log.warning("config: could not create %s: %s", path, exc)


def _expand(path, environ):
    """Expand a leading ~/ (or bare ~) using environ's HOME, so it is testable
    without touching $HOME; delegate ~user to os.path.expanduser."""
    if path == "~" or path.startswith("~/"):
        home = environ.get("HOME") or os.path.expanduser("~")
        return home + path[1:]
    if path.startswith("~"):
        return os.path.expanduser(path)
    return path


def _parse_dim(raw):
    """Clamp a dim value to 0-255, or None if missing/invalid/non-finite."""
    if raw is None or str(raw).strip() == "":
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    try:
        return min(255, max(0, int(raw)))
    except (ValueError, TypeError, OverflowError):
        return None


def _str_value(data, key):
    """A stripped string for key, or '' when missing/blank/null."""
    raw = data.get(key)
    return str(raw).strip() if raw is not None else ""


def resolve(data, environ):
    """Resolve a Config from a parsed config mapping and an environ mapping.

    Precedence for dim and log_file: environment variable, then config file,
    then built-in default. save_dir comes from the config file (or its default)
    with ~ expanded. A non-mapping `data` (a YAML file that is a bare scalar or
    list) is treated as empty, so every value falls back to its default.
    """
    if not isinstance(data, dict):
        data = {}
    dim = _parse_dim(environ.get("SKREENSHOT_DIM"))
    if dim is None:
        dim = _parse_dim(data.get("dim"))
    if dim is None:
        dim = DEFAULT_DIM

    env_log = environ.get("SKREENSHOT_LOG")
    if env_log:
        log_file = _expand(env_log, environ)
    else:
        raw_log = _str_value(data, "log_file")
        log_file = _expand(raw_log, environ) if raw_log else None

    save_dir = _expand(_str_value(data, "save_dir") or DEFAULT_SAVE_DIR, environ)

    return Config(save_dir=save_dir, dim=dim, log_file=log_file)


def load(environ=None):
    """Locate (creating if needed) and read the config file into a Config."""
    env = os.environ if environ is None else environ
    path = config_path(env)
    ensure_config_file(path)
    data = {}
    try:
        with open(path) as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("config: could not read %s: %s; using defaults", path, exc)
        data = {}
    if not isinstance(data, dict):
        log.warning("config: %s is not a mapping; using defaults", path)
        data = {}
    return resolve(data, env)
