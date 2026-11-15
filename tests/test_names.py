import pytest

from tinysecret.store import InvalidName, SecretStore, validate_name


@pytest.mark.parametrize("name", ["A", "NORDVPN_USER", "API_KEY_2", "_X", "9LIVES", "A" * 128])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", [
    "", "lower", "Mixed", "WITH-DASH", "WITH SPACE", "DOT.TXT", "../ETC", "A/B", "/ABS",
    "..", ".META", ".meta.json", "A\nB", "NAME\n", "A\x00", "ÄBC", "A" * 129, None, 123,
])
def test_invalid_names(name):
    with pytest.raises(InvalidName):
        validate_name(name)


def test_store_rejects_path_tricks(tmp_path):
    store = SecretStore(tmp_path / "s")
    for bad in ["../X", "X/../../Y", ".meta.json", "x"]:
        with pytest.raises(InvalidName):
            store.set_value(bad, "v")
        with pytest.raises(InvalidName):
            store.request(bad)
        with pytest.raises(InvalidName):
            store.delete(bad)
    assert not (tmp_path / "X").exists()


def test_web_rejects_bad_name(logged_in):
    r = logged_in.post("/new", data={"csrf": logged_in.csrf, "name": "../evil", "value": "x"})
    assert r.status_code == 400
    r = logged_in.post("/s/lower", data={"csrf": logged_in.csrf, "value": "x"})
    assert r.status_code == 400


def test_mcp_rejects_bad_name(client):
    from conftest import mcp_tool
    res = mcp_tool(client, "request_secret", {"name": "../../etc/passwd", "note": "x"})
    assert res["isError"] is True
    assert "uppercase" in res["content"][0]["text"]
