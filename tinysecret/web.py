"""Mobile-first web UI. Plain HTML forms; values only ever travel browser -> server."""

from __future__ import annotations

import asyncio
import time
from html import escape
from urllib.parse import quote, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .auth import LoginLimiter, Session, SessionStore, constant_time_equals
from .store import MAX_NOTE, Entry, InvalidName, InvalidValue, SecretStore, validate_name

COOKIE = "tinysecret_session"
STATIC_VERSION = "1"

MESSAGES = {
    "saved": "Saved.",
    "deleted": "Deleted.",
    "requested": "Added as waiting.",
}


def esc(s: object) -> str:
    return escape(str(s), quote=True)


def _ago(ts: float | None) -> str:
    if not ts:
        return ""
    d = max(0, int(time.time() - ts))
    if d < 60:
        return "just now"
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= secs:
            return f"{d // secs}{unit} ago"
    return ""


def page(title: str, body: str, *, status: int = 200, logged_in: bool = True, csrf: str = "") -> HTMLResponse:
    logout = (
        f'<form method="post" action="/logout" class="logout">'
        f'<input type="hidden" name="csrf" value="{esc(csrf)}">'
        f'<button type="submit" class="link">Log out</button></form>'
        if logged_in else ""
    )
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="tinysecret">
<meta name="referrer" content="same-origin">
<title>{esc(title)} · tinysecret</title>
<link rel="stylesheet" href="/static/style.css?v={STATIC_VERSION}">
<link rel="icon" href="/static/icon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/icon.png">
</head>
<body>
<header><a href="/" class="brand">tinysecret</a>{logout}</header>
<main>
{body}
</main>
<script src="/static/app.js?v={STATIC_VERSION}" defer></script>
</body>
</html>"""
    return HTMLResponse(html, status_code=status)


class WebUI:
    def __init__(self, store: SecretStore, ui_password: str, *, cookie_secure: bool = False):
        self.store = store
        self.password = ui_password
        self.cookie_secure = cookie_secure
        self.sessions = SessionStore()
        self.limiter = LoginLimiter()

    # ---------- helpers ----------

    def _session(self, request: Request) -> Session | None:
        return self.sessions.get(request.cookies.get(COOKIE))

    @staticmethod
    def _same_origin(request: Request) -> bool:
        """Reject requests whose Origin/Referer names another site.

        A missing or "null" origin is let through: the CSRF token and the
        SameSite=Strict cookie are the main defence; this is an extra check.
        """
        origin = request.headers.get("origin") or request.headers.get("referer")
        if not origin or origin == "null":
            return True
        return urlsplit(origin).netloc == request.headers.get("host")

    async def _form(self, request: Request) -> dict[str, str]:
        form = await request.form(max_files=0, max_fields=10, max_part_size=128 * 1024)
        return {k: v for k, v in form.items() if isinstance(v, str)}

    async def _authed_post(self, request: Request) -> tuple[Session | None, dict[str, str], Response | None]:
        s = self._session(request)
        if s is None:
            return None, {}, RedirectResponse("/login", status_code=303)
        if not self._same_origin(request):
            return None, {}, PlainTextResponse("Cross-origin request refused.", status_code=403)
        form = await self._form(request)
        if not constant_time_equals(form.get("csrf", ""), s.csrf):
            return None, {}, PlainTextResponse("Form expired. Go back and reload the page.", status_code=403)
        return s, form, None

    # ---------- login ----------

    def login_page(self, error: str = "", status: int = 200) -> HTMLResponse:
        err = f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
        body = f"""
<h1>Log in</h1>
{err}
<form method="post" action="/login" class="stack">
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
  <button type="submit" class="primary">Log in</button>
</form>"""
        return page("Log in", body, status=status, logged_in=False)

    async def login_get(self, request: Request) -> Response:
        if self._session(request):
            return RedirectResponse("/", status_code=303)
        return self.login_page()

    async def login_post(self, request: Request) -> Response:
        ip = request.client.host if request.client else "unknown"
        wait = self.limiter.retry_after(ip)
        if wait:
            r = self.login_page(f"Too many failed attempts. Try again in {wait // 60 + 1} min.", 429)
            r.headers["Retry-After"] = str(wait)
            return r
        if not self._same_origin(request):
            return PlainTextResponse("Cross-origin request refused.", status_code=403)
        form = await self._form(request)
        if constant_time_equals(form.get("password", ""), self.password):
            self.limiter.success(ip)
            token, _ = self.sessions.create()
            r = RedirectResponse("/", status_code=303)
            r.set_cookie(COOKIE, token, max_age=self.sessions.ttl, httponly=True,
                         samesite="strict", secure=self.cookie_secure, path="/")
            return r
        self.limiter.failure(ip)
        await asyncio.sleep(1)
        return self.login_page("Wrong password.", 401)

    async def logout(self, request: Request) -> Response:
        s, _, err = await self._authed_post(request)
        if err:
            return err
        self.sessions.delete(request.cookies.get(COOKIE))
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE, path="/")
        return r

    # ---------- pages ----------

    @staticmethod
    def _row(e: Entry) -> str:
        meta = []
        if e.requested_by:
            meta.append(f'<span class="by">for {esc(e.requested_by)}</span>')
        when = _ago(e.requested_at if e.status == "waiting" else e.updated_at)
        if when:
            meta.append(f"<span>{esc(when)}</span>")
        note = f'<p class="note">{esc(e.note)}</p>' if e.note else ""
        return f"""
<li><a class="card {e.status}" href="/s/{quote(e.name)}">
  <span class="row"><code class="name">{esc(e.name)}</code><span class="pill {e.status}">{esc(e.status)}</span></span>
  {note}
  <span class="meta">{' · '.join(meta)}</span>
</a></li>"""

    async def index(self, request: Request) -> Response:
        s = self._session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        entries = self.store.list()
        waiting = [e for e in entries if e.status == "waiting"]
        done = [e for e in entries if e.status == "set"]
        msg = MESSAGES.get(request.query_params.get("msg", ""), "")
        flash = f'<p class="flash" role="status">{esc(msg)}</p>' if msg else ""
        parts = [flash]
        if waiting:
            parts.append(f'<h2>Waiting <span class="count">{len(waiting)}</span></h2>')
            parts.append('<ul class="list">' + "".join(self._row(e) for e in waiting) + "</ul>")
        parts.append('<a class="button primary block" href="/new">+ Add secret</a>')
        if done:
            parts.append(f'<h2>Set <span class="count">{len(done)}</span></h2>')
            parts.append('<ul class="list">' + "".join(self._row(e) for e in done) + "</ul>")
        if not entries:
            parts.append('<p class="empty">No secrets yet. Add one, or let your assistant request one.</p>')
        return page("Secrets", "\n".join(parts), csrf=s.csrf)

    def _name_or_404(self, request: Request) -> str | None:
        try:
            return validate_name(request.path_params["name"])
        except InvalidName:
            return None

    def edit_page(self, s: Session, e: Entry, error: str = "", status: int = 200) -> HTMLResponse:
        err = f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
        details = []
        if e.note:
            details.append(f'<p class="note">{esc(e.note)}</p>')
        if e.requested_by:
            details.append(f'<p class="meta">Requested by {esc(e.requested_by)}</p>')
        state = ("This secret is <strong>set</strong>. Saving replaces the current value."
                 if e.status == "set" else "Waiting for a value.")
        body = f"""
<p><a href="/" class="back">‹ All secrets</a></p>
<h1><code>{esc(e.name)}</code></h1>
<p><span class="pill {e.status}">{esc(e.status)}</span></p>
{''.join(details)}
<p class="hint">{state}</p>
{err}
<form method="post" action="/s/{quote(e.name)}" class="stack">
  <input type="hidden" name="csrf" value="{esc(s.csrf)}">
  <label for="value">New value</label>
  <input id="value" name="value" type="password" required autocomplete="off"
         autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true">
  <label class="check"><input type="checkbox" data-reveal="value"> Show what I type</label>
  <button type="submit" class="primary">Save</button>
</form>
<p class="danger-zone"><a href="/s/{quote(e.name)}/delete" class="button danger block">Delete secret</a></p>"""
        return page(e.name, body, status=status, csrf=s.csrf)

    async def edit_get(self, request: Request) -> Response:
        s = self._session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        name = self._name_or_404(request)
        e = self.store.get(name) if name else None
        if e is None:
            return page("Not found", '<h1>Not found</h1><p><a href="/">‹ All secrets</a></p>',
                        status=404, csrf=s.csrf)
        return self.edit_page(s, e)

    async def edit_post(self, request: Request) -> Response:
        s, form, err = await self._authed_post(request)
        if err:
            return err
        name = self._name_or_404(request)
        if name is None:
            return PlainTextResponse("Invalid name.", status_code=400)
        try:
            self.store.set_value(name, form.get("value", ""))
        except InvalidValue as ex:
            e = self.store.get(name) or Entry(name=name, status="waiting")
            return self.edit_page(s, e, str(ex), 400)
        return RedirectResponse("/?msg=saved", status_code=303)

    async def delete_get(self, request: Request) -> Response:
        s = self._session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        name = self._name_or_404(request)
        if name is None or self.store.get(name) is None:
            return RedirectResponse("/", status_code=303)
        body = f"""
<h1>Delete <code>{esc(name)}</code>?</h1>
<p>The file <code>/secrets/{esc(name)}</code> will be removed. Apps that read it will stop working.</p>
<form method="post" action="/s/{quote(name)}/delete" class="stack">
  <input type="hidden" name="csrf" value="{esc(s.csrf)}">
  <button type="submit" class="danger">Yes, delete it</button>
  <a href="/s/{quote(name)}" class="button block">Cancel</a>
</form>"""
        return page("Delete", body, csrf=s.csrf)

    async def delete_post(self, request: Request) -> Response:
        s, form, err = await self._authed_post(request)
        if err:
            return err
        name = self._name_or_404(request)
        if name is None:
            return PlainTextResponse("Invalid name.", status_code=400)
        self.store.delete(name)
        return RedirectResponse("/?msg=deleted", status_code=303)

    def new_page(self, s: Session, name: str = "", note: str = "", error: str = "", status: int = 200) -> HTMLResponse:
        err = f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
        body = f"""
<p><a href="/" class="back">‹ All secrets</a></p>
<h1>Add secret</h1>
{err}
<form method="post" action="/new" class="stack">
  <input type="hidden" name="csrf" value="{esc(s.csrf)}">
  <label for="name">Name</label>
  <input id="name" name="name" value="{esc(name)}" required maxlength="128" pattern="[A-Z0-9_]+"
         autocapitalize="characters" autocorrect="off" spellcheck="false" autocomplete="off"
         placeholder="NORDVPN_USER" data-upper>
  <p class="hint">Capital letters, digits and _ only. Becomes the file <code>/secrets/NAME</code>.</p>
  <label for="value">Value <span class="opt">(optional)</span></label>
  <input id="value" name="value" type="password" autocomplete="off"
         autocapitalize="off" autocorrect="off" spellcheck="false" data-1p-ignore data-lpignore="true">
  <label class="check"><input type="checkbox" data-reveal="value"> Show what I type</label>
  <p class="hint">Leave empty to add it as “waiting” and fill it in later.</p>
  <label for="note">Note <span class="opt">(optional)</span></label>
  <input id="note" name="note" value="{esc(note)}" maxlength="{MAX_NOTE}" autocomplete="off">
  <button type="submit" class="primary">Add</button>
</form>"""
        return page("Add secret", body, status=status, csrf=s.csrf)

    async def new_get(self, request: Request) -> Response:
        s = self._session(request)
        if s is None:
            return RedirectResponse("/login", status_code=303)
        return self.new_page(s)

    async def new_post(self, request: Request) -> Response:
        s, form, err = await self._authed_post(request)
        if err:
            return err
        name = form.get("name", "").strip()
        note = form.get("note", "")
        value = form.get("value", "")
        try:
            validate_name(name)
            if value.strip("\r\n"):
                self.store.set_value(name, value)
                if note:
                    self.store.update_note(name, note)
                return RedirectResponse("/?msg=saved", status_code=303)
            if self.store.is_set(name):
                return self.new_page(s, name, note, f"{name} is already set. Open it to replace the value.", 409)
            self.store.request(name, note, "")
            return RedirectResponse("/?msg=requested", status_code=303)
        except (InvalidName, InvalidValue) as ex:
            return self.new_page(s, name, note, str(ex), 400)

    async def healthz(self, request: Request) -> Response:
        return PlainTextResponse("ok")

    def routes(self, static_dir: str) -> list:
        return [
            Route("/", self.index, methods=["GET"]),
            Route("/login", self.login_get, methods=["GET"]),
            Route("/login", self.login_post, methods=["POST"]),
            Route("/logout", self.logout, methods=["POST"]),
            Route("/new", self.new_get, methods=["GET"]),
            Route("/new", self.new_post, methods=["POST"]),
            Route("/s/{name}", self.edit_get, methods=["GET"]),
            Route("/s/{name}", self.edit_post, methods=["POST"]),
            Route("/s/{name}/delete", self.delete_get, methods=["GET"]),
            Route("/s/{name}/delete", self.delete_post, methods=["POST"]),
            Route("/healthz", self.healthz, methods=["GET"]),
            Mount("/static", StaticFiles(directory=static_dir), name="static"),
        ]
