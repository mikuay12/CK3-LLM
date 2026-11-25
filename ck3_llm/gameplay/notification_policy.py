"""Atomic notification preferences shared by the UI and the running reader."""

from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading


from ck3_llm.paths import PROJECT_ROOT


DEFAULT_POLICY_PATH = PROJECT_ROOT / "notification_policy.json"
DEFAULT_POLICY = {"version": 1, "disabled_groups": [], "disabled_types": []}


@contextmanager
def _file_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def validate_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ValueError("Unknown notification preference fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Unsupported notification preference version")
    result = {"version": 1}
    for field in ("disabled_groups", "disabled_types"):
        values = value.get(field, [])
        if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
            raise ValueError(f"{field} must be an array of nonempty identifiers")
        result[field] = sorted(set(values))
    return result


class NotificationPolicy:
    def __init__(self, path=DEFAULT_POLICY_PATH):
        self.path = Path(path)
        self.value = deepcopy(DEFAULT_POLICY)
        self.revision = "missing"
        self.error = None
        self._lock = threading.RLock()
        self.reload()

    def reload(self):
        with self._lock:
            try:
                raw = self.path.read_bytes()
                revision = hashlib.sha256(raw).hexdigest()
                if revision == self.revision and self.error is None:
                    return
                value = validate_policy(json.loads(raw.decode("utf-8-sig")))
            except FileNotFoundError:
                value, revision = deepcopy(DEFAULT_POLICY), "missing"
            except ValueError as error:
                self.revision = revision
                self.error = str(error)
                return
            except OSError as error:
                self.error = str(error)
                return
            self.value, self.revision, self.error = value, revision, None

    def snapshot(self):
        self.reload()
        with self._lock:
            return {"policy": deepcopy(self.value), "revision": self.revision, "error": self.error}

    def allows(self, event):
        with self._lock:
            return (event.get("group") not in self.value["disabled_groups"]
                    and event.get("notification_id") not in self.value["disabled_types"])

    def save(self, value, *, revision):
        value = validate_policy(value)
        with self._lock, _file_lock(self.path):
            self.reload()
            if revision != self.revision:
                raise FileExistsError("Notification preferences changed. Reload before saving")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self.value = value
            self.revision = hashlib.sha256(raw).hexdigest()
            self.error = None
            return self.snapshot()
