"""MCP tools. None of them accepts or returns a secret value."""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from .store import Entry, InvalidName, SecretStore, validate_name

INSTRUCTIONS = """\
tinysecret holds secrets (API keys, passwords) as files that other containers mount.
You can never read or write a secret's value, and you must never ask the user to paste a
secret into the chat. Instead:
1. call request_secret with an UPPER_SNAKE_CASE name, a short note saying what it is for,
   and requested_by (the app or project that needs it);
2. tell the user to open the tinysecret page on their phone and fill it in;
3. call check_secret (or list_secrets) to confirm it is "set" before continuing.
Containers read a secret from the file /secrets/<NAME> (mount the folder read-only).
"""


class SecretInfo(BaseModel):
    """Metadata about one secret. There is deliberately no value field."""

    name: str
    status: Literal["set", "waiting", "missing"]
    note: str = ""
    requested_by: str = ""
    requested_at: str | None = None
    updated_at: str | None = None
    path: str


class SecretList(BaseModel):
    secrets: list[SecretInfo]


class RequestResult(SecretInfo):
    changed: bool
    message: str


def _check(name: str) -> str:
    try:
        return validate_name(name)
    except InvalidName as e:
        raise ToolError(str(e)) from None


def _info(e: Entry) -> SecretInfo:
    return SecretInfo(**e.public(), path=f"/secrets/{e.name}")


def build_mcp(store: SecretStore, public_url: str = "") -> MCPServer:
    mcp = MCPServer(name="tinysecret", instructions=INSTRUCTIONS)
    where = f"Ask the user to open {public_url} and fill it in." if public_url \
        else "Ask the user to open the tinysecret page and fill it in."

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    def list_secrets() -> SecretList:
        """List all secrets with status ("set" or "waiting"), note and requested_by. Never includes values."""
        return SecretList(secrets=[_info(e) for e in store.list()])

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True))
    def check_secret(name: str) -> SecretInfo:
        """Check one secret. status is "set", "waiting" (requested, not filled in yet) or "missing". Never includes the value."""
        _check(name)
        entry = store.get(name)
        if entry is None:
            return SecretInfo(name=name, status="missing", path=f"/secrets/{name}")
        return _info(entry)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def request_secret(name: str, note: str = "", requested_by: str = "") -> RequestResult:
        """Ask the user for a secret. Creates a "waiting" entry shown at the top of the
        tinysecret page. Never overwrites a value that is already set.

        name: UPPER_SNAKE_CASE, letters/digits/underscores only (e.g. NORDVPN_USER).
        note: what it is and where the user can find it (e.g. "NordVPN service username, Nord dashboard > Manual setup").
        requested_by: the app or project asking (e.g. "gluetun").
        """
        _check(name)
        entry, changed = store.request(name, note, requested_by)
        msg = f"Waiting for the user. {where}" if changed else "Already set. Nothing changed."
        return RequestResult(**_info(entry).model_dump(), changed=changed, message=msg)

    return mcp
