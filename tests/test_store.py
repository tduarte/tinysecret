import json
import os
import stat

import pytest

from secret_drop import store as store_mod
from secret_drop.store import InvalidValue, SecretStore


@pytest.fixture
def store(tmp_path):
    return SecretStore(tmp_path / "s", puid=os.getuid(), pgid=os.getgid())


def test_write_exact_bytes_mode_and_owner(store):
    store.set_value("TOKEN", "abc 123\n")
    p = store.dir / "TOKEN"
    assert p.read_bytes() == b"abc 123"  # no trailing newline
    st = p.stat()
    assert stat.S_IMODE(st.st_mode) == 0o640
    assert (st.st_uid, st.st_gid) == (os.getuid(), os.getgid())
    meta = json.loads((store.dir / ".meta.json").read_text())
    assert "TOKEN" in meta["secrets"]
    assert "abc" not in (store.dir / ".meta.json").read_text()
    assert stat.S_IMODE((store.dir / ".meta.json").stat().st_mode) == 0o640


def test_overwrite_and_no_temp_leftovers(store):
    store.set_value("K", "one")
    store.set_value("K", "two")
    assert (store.dir / "K").read_bytes() == b"two"
    assert sorted(os.listdir(store.dir)) == [".meta.json", "K"]


def test_atomic_failure_keeps_old_value(store, monkeypatch):
    store.set_value("K", "old")

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(store_mod.os, "replace", boom)
    with pytest.raises(OSError):
        store.set_value("K", "new")
    monkeypatch.undo()
    assert (store.dir / "K").read_bytes() == b"old"
    assert not [f for f in os.listdir(store.dir) if f.startswith(".tmp-")]


def test_write_goes_through_rename(store, monkeypatch):
    calls = []
    real = os.replace

    def spy(src, dst):
        calls.append((os.path.basename(src), os.path.basename(dst)))
        # the temp file is complete before it is renamed into place
        if os.path.basename(dst) == "K":
            with open(src, "rb") as fh:
                assert fh.read() == b"value"
        real(src, dst)

    monkeypatch.setattr(store_mod.os, "replace", spy)
    store.set_value("K", "value")
    assert calls[0][0].startswith(".tmp-") and calls[0][1] == "K"


@pytest.mark.parametrize("bad", ["", "\n", "\r\n", "a\x00b", "x" * (64 * 1024 + 1)])
def test_invalid_values(store, bad):
    with pytest.raises(InvalidValue):
        store.set_value("K", bad)
    assert not (store.dir / "K").exists()


def test_request_does_not_overwrite_set_value(store):
    store.set_value("K", "keep-me")
    entry, changed = store.request("K", "new note", "bot")
    assert not changed and entry.status == "set"
    assert (store.dir / "K").read_bytes() == b"keep-me"


def test_request_then_set_then_delete(store):
    e, changed = store.request("NEW", "for vpn", "gluetun")
    assert changed and e.status == "waiting" and e.requested_by == "gluetun"
    assert [x.name for x in store.list()] == ["NEW"]
    store.set_value("NEW", "v")
    assert store.get("NEW").status == "set"
    assert store.get("NEW").note == "for vpn"
    assert store.delete("NEW")
    assert store.get("NEW") is None


def test_waiting_listed_first(store):
    store.set_value("AAA", "v")
    store.request("ZZZ")
    assert [e.status for e in store.list()] == ["waiting", "set"]


def test_ignores_junk_files(store):
    (store.dir / "lowercase.txt").write_text("x")
    (store.dir / ".tmp-abc").write_text("x")
    store.set_value("OK", "v")
    assert [e.name for e in store.list()] == ["OK"]
