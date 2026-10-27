"""Bind a dashboard slot's session to the authenticated user driving it.

The persisted MCP sign-in vault is per user (``mcp_grant_principals.py``).
The only place the gateway can prove who drives a session is a dashboard
request: its signed token subject (``request["user"]``), with the owner judged
by the dashboard's own owner predicate. Every request-layer path that starts or
sends into a slot's session calls :func:`bind_slot_to_request_user` before the
session's engine process can be spawned.

What conflicts the key instead, so its session keeps sign-ins in memory only:

- an app token, an admitted crew member, an internal or unauthenticated caller;
- a slot whose session is a channel's or a cron's (``linked_session_key``):
  turns arrive there from outside the dashboard, from callers this request
  cannot speak for;
- an app's slot (``_app``) and a slot another session or a cron created
  (``_created_by``): it is not a person's own session;
- a session a Slack thread or a two-way channel mirror can drive: its other
  turns come from channel users. ``SessionManager.set_slack_link`` and
  ``set_mirror_link`` conflict the key when the link is made, and this module
  re-reads the persisted links so a restart cannot clear that.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from kiro_crew import mcp_grant_principals
from kiro_crew.mcp_grant_principals import OWNER_PRINCIPAL

logger = logging.getLogger(__name__)

__all__ = [
    "OWNER_PRINCIPAL",
    "bind_routed_slot",
    "bind_slot_to_principal",
    "bind_slot_to_request_user",
    "request_principal",
]


def request_principal(request: web.Request) -> str | None:
    """The principal of the dashboard user who made *request*, or ``None``."""
    # The middleware sets ``app`` to "" for a dashboard user; absent means it
    # never authenticated this caller, and non-empty is an app token.
    if request.get("app", None) != "":
        return None
    subject = request.get("user")
    if not isinstance(subject, str) or not subject:
        return None
    # Call-time imports: both modules import much of the dashboard.
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
    from kiro_crew.dashboard.token_auth_callers import MEMBER_CHAT_PRINCIPAL_KEY

    # A crew member's admitted request is an agent's, not a person's.
    if request.get(MEMBER_CHAT_PRINCIPAL_KEY):
        return None

    try:
        is_owner = is_owner_dashboard_request(request)
    except Exception:  # noqa: BLE001 -- an unreadable owner verdict binds no one
        logger.debug("owner verdict unreadable; MCP grant binding skipped", exc_info=True)
        return None
    return mcp_grant_principals.principal_for_subject(subject, is_owner=is_owner)


def _channel_driven(sessions: Any, session_key: str) -> bool:
    """Whether a channel can drive *session_key*: a Slack thread or a two-way mirror.

    Read off the session map, which keeps both across a restart. Unreadable is
    True: a session whose links cannot be read is not provably a person's own.
    """
    if sessions is None:
        return False
    try:
        return bool(sessions.get_slack_link(session_key)[0]) or bool(
            sessions.mirror_accepts_inbound(session_key)
        )
    except Exception:  # noqa: BLE001 -- fail closed
        logger.debug("session links unreadable; MCP grant binding conflicted", exc_info=True)
        return True


def bind_slot_to_principal(slot: Any, principal: str | None, sessions: Any) -> None:
    """Bind *slot*'s session key to *principal*, else conflict it.

    ``None`` (a caller that is not exactly one person), or a slot that is not a
    person's own session, conflicts the key rather than leaving it alone, so a
    non-person caller can never ride a vault a person bound, and a key reused by
    such a slot cannot inherit an earlier user.
    """
    # Call-time import: the slot helpers import much of the dashboard.
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    session_key = effective_session_key(slot)
    not_own_session = any(
        getattr(slot, attr, "")
        for attr in ("linked_session_key", "_app", "_created_by", "_slack_linked")
    ) or _channel_driven(sessions, session_key)
    if principal is None or not_own_session:
        mcp_grant_principals.conflict(session_key)
        return
    mcp_grant_principals.bind(session_key, principal)


def bind_slot_to_request_user(request: web.Request, slot: Any, *, person: bool = True) -> None:
    """Bind *slot*'s session key to the person who made *request*, else conflict it.

    *person* is False for a caller the route already knows is not a person (an
    attested cron).
    """
    principal = request_principal(request) if person else None
    sessions = getattr(request.app.get("state"), "sessions", None)
    bind_slot_to_principal(slot, principal, sessions)


def bind_routed_slot(request: web.Request, slot: Any) -> None:
    """The per-slot checkpoint's bind: every mutating ``/api/chat/slots/{slot}/...`` call.

    Continue, regenerate, edit-resend, rewind, agent and model switches, reload,
    resume, approvals and the rest can each start a turn or respawn the session's
    engine, so each one binds the caller first (a second user, an app or a member
    conflicts the key). Reads do not bind.
    """
    if slot is None or request.method in ("GET", "HEAD", "OPTIONS"):
        return
    bind_slot_to_request_user(request, slot)
