import json
import os
import tempfile


def data_path(filename: str) -> str:
    """Absolute path to `filename` in the repo root, given a module living in cogs/."""
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), filename)


def load_json(path: str) -> dict:
    """Load a JSON object from disk, tolerating a missing or corrupt file."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def save_json_atomic(path: str, data: dict) -> None:
    """Write a JSON object to disk atomically, so a crash mid-write can't corrupt it."""
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def backfill_defaults(conf: dict, defaults: dict) -> dict:
    """Fill in any key missing from `conf` using the value from `defaults`, recursing
    into nested dicts so a config persisted by an older schema (missing a whole nested
    section, or just a key within one) still ends up with every current default key.
    Mutates `conf` in place and returns it."""
    for key, value in defaults.items():
        if key not in conf:
            conf[key] = value
        elif isinstance(conf[key], dict) and isinstance(value, dict):
            backfill_defaults(conf[key], value)
    return conf
