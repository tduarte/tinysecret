"""No page, endpoint, MCP tool or log line may ever contain a secret value."""

import logging

from starlette.routing import Mount, Route

from conftest import CANARY, PASSWORD, TOKEN, mcp_call, mcp_tool


def _set_canary(logged_in):
    r = logged_in.post("/s/LEAK_TEST", data={"csrf": logged_in.csrf, "value": CANARY},
                       follow_redirects=False)
    assert r.status_code == 303
    r = logged_in.post("/new", data={"csrf": logged_in.csrf, "name": "LEAK_NEW", "value": CANARY,
                                      "note": "n"}, follow_redirects=False)
    assert r.status_code == 303


def test_no_endpoint_returns_value(app, logged_in, caplog):
    caplog.set_level(logging.DEBUG)
    _set_canary(logged_in)
    assert (app.store.dir / "LEAK_TEST").read_text() == CANARY

    bodies = []
    # Every GET route in the web app, with a real secret name filled in.
    for route in app.web.routes:
        if isinstance(route, Route) and "GET" in route.methods:
            path = route.path.replace("{name}", "LEAK_TEST")
            bodies.append(logged_in.get(path).text)
        elif isinstance(route, Mount):
            for f in ("style.css", "app.js", "icon.svg"):
                bodies.append(logged_in.get(f"/static/{f}").text)
    # Error paths that echo user input.
    bodies.append(logged_in.post("/s/LEAK_TEST", data={"csrf": "wrong", "value": CANARY}).text)
    bodies.append(logged_in.post("/new", data={"csrf": logged_in.csrf, "name": "bad name", "value": CANARY}).text)
    bodies.append(logged_in.post("/s/LEAK_TEST", data={"csrf": logged_in.csrf, "value": CANARY + "\x00"}).text)

    # Every MCP tool, plus list endpoints.
    tools = mcp_call(logged_in, "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"list_secrets", "check_secret", "request_secret"}
    for t in tools:
        assert "value" not in t["inputSchema"].get("properties", {}), t["name"]
    bodies.append(str(tools))
    bodies.append(str(mcp_tool(logged_in, "list_secrets")))
    bodies.append(str(mcp_tool(logged_in, "check_secret", {"name": "LEAK_TEST"})))
    bodies.append(str(mcp_tool(logged_in, "request_secret", {"name": "LEAK_TEST", "note": "x"})))
    for m in ("resources/list", "prompts/list"):
        bodies.append(mcp_call(logged_in, m).text)

    for b in bodies:
        assert CANARY not in b
    assert CANARY not in caplog.text
    # the value file is untouched by request_secret
    assert (app.store.dir / "LEAK_TEST").read_text() == CANARY


def test_mcp_cannot_write_values(app, client):
    app.store.set_value("K", CANARY)
    # Unknown extra arguments must not create a write path.
    r = mcp_call(client, "tools/call", {"name": "request_secret",
                                         "arguments": {"name": "K", "value": "overwritten"}})
    assert "overwritten" not in r.text
    assert (app.store.dir / "K").read_text() == CANARY
    r = mcp_call(client, "tools/call", {"name": "set_secret", "arguments": {"name": "K", "value": "x"}})
    assert (app.store.dir / "K").read_text() == CANARY


def test_mcp_requires_bearer(client):
    assert mcp_call(client, "tools/list", token=None).status_code == 401
    assert mcp_call(client, "tools/list", token="wrong-token-wrong-token").status_code == 401
    assert mcp_call(client, "tools/list", token=TOKEN).status_code == 200


def test_mcp_request_flow(app, client):
    res = mcp_tool(client, "request_secret",
                   {"name": "NORDVPN_USER", "note": "Nord service user", "requested_by": "gluetun"})
    sc = res["structuredContent"]
    assert sc["status"] == "waiting" and sc["changed"] is True
    assert "http://nas:8790" in sc["message"]
    assert mcp_tool(client, "check_secret", {"name": "NORDVPN_USER"})["structuredContent"]["status"] == "waiting"
    assert mcp_tool(client, "check_secret", {"name": "NOPE"})["structuredContent"]["status"] == "missing"
    app.store.set_value("NORDVPN_USER", "u")
    lst = mcp_tool(client, "list_secrets")["structuredContent"]["secrets"]
    assert lst == [{**lst[0], "name": "NORDVPN_USER", "status": "set", "requested_by": "gluetun"}]
    again = mcp_tool(client, "request_secret", {"name": "NORDVPN_USER"})["structuredContent"]
    assert again["changed"] is False


def test_ui_requires_login(client):
    for path in ["/", "/new", "/s/X", "/s/X/delete"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    r = client.post("/s/X", data={"value": "v"}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/healthz").text == "ok"


def test_login_rate_limit(client, monkeypatch):
    import secret_drop.web as web

    async def no_sleep(_):
        pass

    monkeypatch.setattr(web.asyncio, "sleep", no_sleep)
    for _ in range(5):
        assert client.post("/login", data={"password": "nope"}).status_code == 401
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429  # locked even with the right password
    assert "Retry-After" in r.headers


def test_csrf_and_origin(logged_in, app):
    r = logged_in.post("/new", data={"csrf": "bad", "name": "X", "value": "v"})
    assert r.status_code == 403
    r = logged_in.post("/new", data={"csrf": logged_in.csrf, "name": "X", "value": "v"},
                       headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert not (app.store.dir / "X").exists()
    # Same-origin (what a real browser sends) is accepted.
    r = logged_in.post("/new", data={"csrf": logged_in.csrf, "name": "X", "value": "v"},
                       headers={"Origin": "http://nas:8790"}, follow_redirects=False)
    assert r.status_code == 303


def test_delete_flow(logged_in, app):
    app.store.set_value("GONE", "v")
    page = logged_in.get("/s/GONE/delete").text
    assert "Yes, delete it" in page
    assert (app.store.dir / "GONE").exists()
    r = logged_in.post("/s/GONE/delete", data={"csrf": logged_in.csrf}, follow_redirects=False)
    assert r.status_code == 303
    assert not (app.store.dir / "GONE").exists()


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
