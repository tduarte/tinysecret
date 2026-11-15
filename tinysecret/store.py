"""File-backed secret storage.

Each secret is one file: <dir>/<NAME>. Request metadata lives in <dir>/.meta.json.
Values are write-only from the point of view of this module: nothing here ever
reads a secret file's contents.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tinysecret.store")

NAME_RE = re.compile(r"[A-Z0-9_]{1,128}")
META_FILE = ".meta.json"
MAX_VALUE_BYTES = 64 * 1024
MAX_NOTE = 500
MAX_REQUESTED_BY = 100
FILE_MODE = 0o640


class InvalidName(ValueError):
    pass


class InvalidValue(ValueError):
    pass


def validate_name(name: object) -> str:
    """Return the name if valid, else raise InvalidName. fullmatch: no '\\n' tricks."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise InvalidName(
            "Name must be 1-128 characters: uppercase letters, digits and underscores only."
        )
    return name


def _clean_text(text: object, limit: int) -> str:
    if text is None:
        return ""
    text = " ".join(str(text).split())  # collapse whitespace / newlines
    return text[:limit]


@dataclass
class Entry:
    name: str
    status: str  # "set" or "waiting"
    note: str = ""
    requested_by: str = ""
    requested_at: float | None = None
    updated_at: float | None = None

    def public(self) -> dict:
        """Metadata only. There is no value field, by design."""
        return {
            "name": self.name,
            "status": self.status,
            "note": self.note,
            "requested_by": self.requested_by,
            "requested_at": _iso(self.requested_at),
            "updated_at": _iso(self.updated_at),
        }


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class SecretStore:
    def __init__(self, directory: str | os.PathLike, puid: int | None = None, pgid: int | None = None):
        self.dir = Path(directory)
        self.puid = puid
        self.pgid = pgid
        self._lock = threading.RLock()
        self._chown_warned = False
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---------- low-level file helpers ----------

    def _path(self, name: str) -> Path:
        validate_name(name)
        p = self.dir / name
        # Belt and braces: the regex already forbids '/', '.', etc.
        if p.parent.resolve() != self.dir.resolve():
            raise InvalidName("Invalid name.")
        return p

    def _chown(self, fd: int) -> None:
        uid = -1 if self.puid is None else self.puid
        gid = -1 if self.pgid is None else self.pgid
        if uid == -1 and gid == -1:
            return
        try:
            os.fchown(fd, uid, gid)
        except PermissionError:
            if not self._chown_warned:
                log.warning(
                    "Could not set file owner to PUID=%s PGID=%s (running as uid=%s gid=%s). "
                    "Run the container with user: \"PUID:PGID\".",
                    self.puid, self.pgid, os.getuid(), os.getgid(),
                )
                self._chown_warned = True

    def _atomic_write(self, target: Path, data: bytes) -> None:
        """Write to a temp file in the same dir, fsync, then rename over the target."""
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-")
        try:
            os.fchmod(fd, FILE_MODE)
            self._chown(fd)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        try:
            dfd = os.open(self.dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    # ---------- metadata ----------

    def _load_meta(self) -> dict:
        try:
            with open(self.dir / META_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            log.error("Could not read %s; treating as empty.", META_FILE)
            return {}
        secrets = data.get("secrets") if isinstance(data, dict) else None
        return secrets if isinstance(secrets, dict) else {}

    def _save_meta(self, meta: dict) -> None:
        body = json.dumps({"version": 1, "secrets": meta}, indent=2, sort_keys=True).encode()
        self._atomic_write(self.dir / META_FILE, body)

    # ---------- public API ----------

    def is_set(self, name: str) -> bool:
        return self._path(name).is_file()

    def list(self) -> list[Entry]:
        with self._lock:
            meta = self._load_meta()
            names = set()
            for child in self.dir.iterdir():
                if NAME_RE.fullmatch(child.name) and child.is_file():
                    names.add(child.name)
            names.update(n for n in meta if NAME_RE.fullmatch(n))
            entries = [self._entry(n, meta.get(n) or {}) for n in names]
        # Waiting first, then by name.
        entries.sort(key=lambda e: (e.status != "waiting", e.name))
        return entries

    def get(self, name: str) -> Entry | None:
        validate_name(name)
        with self._lock:
            meta = self._load_meta()
            if name not in meta and not self.is_set(name):
                return None
            return self._entry(name, meta.get(name) or {})

    def _entry(self, name: str, m: dict) -> Entry:
        return Entry(
            name=name,
            status="set" if self.is_set(name) else "waiting",
            note=str(m.get("note") or ""),
            requested_by=str(m.get("requested_by") or ""),
            requested_at=m.get("requested_at"),
            updated_at=m.get("updated_at"),
        )

    def set_value(self, name: str, value: str) -> None:
        """Store a value. Overwrites. The value is never logged."""
        path = self._path(name)
        if not isinstance(value, str):
            raise InvalidValue("Value must be text.")
        value = value.rstrip("\r\n")
        if not value:
            raise InvalidValue("Value is empty.")
        if "\x00" in value:
            raise InvalidValue("Value contains a NUL byte.")
        data = value.encode("utf-8")
        if len(data) > MAX_VALUE_BYTES:
            raise InvalidValue(f"Value is larger than {MAX_VALUE_BYTES} bytes.")
        with self._lock:
            self._atomic_write(path, data)
            meta = self._load_meta()
            m = meta.setdefault(name, {})
            m["updated_at"] = time.time()
            self._save_meta(meta)
        log.info("Secret %s saved.", name)

    def request(self, name: str, note: str = "", requested_by: str = "") -> tuple[Entry, bool]:
        """Create or refresh a 'waiting' entry. Never touches a set value.

        Returns (entry, changed).
        """
        self._path(name)
        note = _clean_text(note, MAX_NOTE)
        requested_by = _clean_text(requested_by, MAX_REQUESTED_BY)
        with self._lock:
            meta = self._load_meta()
            if self.is_set(name):
                return self._entry(name, meta.get(name) or {}), False
            m = meta.setdefault(name, {})
            m["note"] = note
            m["requested_by"] = requested_by
            m["requested_at"] = time.time()
            self._save_meta(meta)
            entry = self._entry(name, m)
        log.info("Secret %s requested by %r.", name, requested_by)
        return entry, True

    def update_note(self, name: str, note: str) -> None:
        self._path(name)
        with self._lock:
            meta = self._load_meta()
            meta.setdefault(name, {})["note"] = _clean_text(note, MAX_NOTE)
            self._save_meta(meta)

    def delete(self, name: str) -> bool:
        path = self._path(name)
        with self._lock:
            existed = False
            try:
                path.unlink()
                existed = True
            except FileNotFoundError:
                pass
            meta = self._load_meta()
            if name in meta:
                del meta[name]
                existed = True
                self._save_meta(meta)
        if existed:
            log.info("Secret %s deleted.", name)
        return existed
