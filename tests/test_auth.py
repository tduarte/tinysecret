"""Regression coverage for server-side sessions and login throttling."""

from tinysecret.auth import LoginLimiter, SessionStore
from tinysecret.web import COOKIE


def test_session_expiry_and_revocation(monkeypatch):
    import tinysecret.auth as auth

    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    sessions = SessionStore(ttl_seconds=10)
    token, session = sessions.create()
    assert sessions.get(token) is session
    assert sessions.get("not-a-token") is None

    now[0] = 1005.0
    assert sessions.get(token) is session  # activity extends the idle expiry
    now[0] = 1016.0
    assert sessions.get(token) is None
    assert token not in sessions._sessions

    second_token, _ = sessions.create()
    sessions.delete(second_token)
    assert sessions.get(second_token) is None


def test_sessions_have_distinct_tokens_and_csrf_values():
    sessions = SessionStore()
    first_token, first = sessions.create()
    second_token, second = sessions.create()
    assert first_token != second_token
    assert first.csrf != second.csrf
    assert sessions.get(None) is None


def test_logout_requires_csrf_and_revokes_replayed_cookie(logged_in):
    token = logged_in.cookies.get(COOKIE)
    assert token
    bad = logged_in.post("/logout", data={"csrf": "wrong"}, follow_redirects=False)
    assert bad.status_code == 403
    assert logged_in.get("/").status_code == 200

    response = logged_in.post("/logout", data={"csrf": logged_in.csrf}, follow_redirects=False)
    assert response.status_code == 303
    logged_in.cookies.set(COOKIE, token)  # replay the token after the browser cleared it
    denied = logged_in.get("/", follow_redirects=False)
    assert denied.status_code == 303
    assert denied.headers["location"] == "/login"


def test_global_login_limit_applies_across_addresses(monkeypatch):
    import tinysecret.auth as auth

    now = [1000.0]
    monkeypatch.setattr(auth.time, "time", lambda: now[0])
    limiter = LoginLimiter(max_failures=5, window=60, lockout=30, global_max=3)
    for ip in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
        limiter.failure(ip)
    assert limiter.retry_after("192.0.2.4") == 30
    now[0] = 1030.0
    assert limiter.retry_after("192.0.2.4") == 0
