import re

import pytest
from starlette.testclient import TestClient

from secret_drop.main import create_app

PASSWORD = "correct horse battery"
TOKEN = "t" * 32
CANARY = "CANARY-VALUE-7f3a9c-do-not-leak"


@pytest.fixture
def app(tmp_path):
    return create_app(secrets_dir=str(tmp_path / "secrets"), ui_password=PASSWORD,
                      mcp_token=TOKEN, public_url="http://nas:8790")


@pytest.fixture
def client(app):
    with TestClient(app, base_url="http://nas:8790") as c:
        yield c


def csrf_of(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


@pytest.fixture
def logged_in(client):
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    client.csrf = csrf_of(client.get("/").text)
    return client


def mcp_call(client, method, params=None, token=TOKEN, id_=1):
    headers = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    return client.post("/mcp", json=body, headers=headers)


def mcp_tool(client, name, args=None):
    r = mcp_call(client, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]
