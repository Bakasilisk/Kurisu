import json
import os
import tempfile


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
