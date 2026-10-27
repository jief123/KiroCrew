"""Which authenticated user a session's persisted MCP sign-ins belong to.

``agent.kas_persist_mcp_sign_in`` keeps a remote MCP server's OAuth grant on
disk. The engine's ``_kiro/secret/*`` requests name no session and no user, and
one engine process can host several sessions, so the vault cannot tell from a
request whose grant it is. This module is the answer the gateway CAN prove: the
authenticated dashboard caller whose turn first bound a session key, recorded
before that key's engine process is spawned.

The rule is fail closed in every direction:

- A key nothing bound (a channel, cron, app, background, pooled or subagent
  session, or any key after a gateway restart until its first dashboard turn)
  has no principal, so its process declares no storage and keeps every grant
  in the engine's memory.
- A key two different callers have bound is CONFLICTED for the rest of this
  process: it has no principal, and a process already serving it stops reading
  or writing its vault (``SecretStorageServer`` re-checks on every request).
- The binding is never moved to the latest caller, so one user's turn in
  another user's session cannot point that session at the second user's vault.

- A caller that is not one provable person (an app, a crew member, a cron)
  or a slot that is not a person's own conflicts the key too (:func:`conflict`).

In memory only, and bounded: past :data:`MAX_BINDINGS` the oldest single-user
binding is dropped, which leaves that key unbound -- the same state a restart
leaves. A conflict is dropped only when nothing else is left to drop.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict

__all__ = [
    "MAX_BINDINGS",
    "OWNER_PRINCIPAL",
    "bind",
    "conflict",
    "is_bound_to",
    "principal_dir_name",
    "principal_for",
    "principal_for_subject",
    "reset_for_tests",
]

#: The gateway owner, whichever owner subject authenticated (the configured
#: owner id, or a machine-local bootstrap subject when none is configured).
OWNER_PRINCIPAL = "owner"
#: Most session keys remembered at once.
MAX_BINDINGS = 4096

_CONFLICT = object()
_lock = threading.Lock()
_bindings: OrderedDict[str, object] = OrderedDict()


def principal_for_subject(subject: object, *, is_owner: bool) -> str | None:
    """The principal for an authenticated dashboard subject, or ``None``.

    *is_owner* is the caller's verdict from the dashboard's owner predicate.
    Every owner subject maps to :data:`OWNER_PRINCIPAL`; any other signed
    subject gets its own ``user:<subject>`` namespace. An empty or non-string
    subject is unknown.
    """
    if is_owner:
        return OWNER_PRINCIPAL
    if not isinstance(subject, str) or not subject:
        return None
    return f"user:{subject}"


def bind(session_key: object, principal: object) -> None:
    """Record that *principal* drove *session_key*. A second principal conflicts it."""
    if not isinstance(session_key, str) or not session_key:
        return
    if not isinstance(principal, str) or not principal:
        return
    with _lock:
        current = _bindings.get(session_key)
        if current is None:
            if not _make_room(MAX_BINDINGS - 1):
                # Every remembered key is a conflict: leave this one unbound.
                return
            _bindings[session_key] = principal
        elif current != principal:
            _bindings[session_key] = _CONFLICT
        _bindings.move_to_end(session_key)


def conflict(session_key: object) -> None:
    """Mark *session_key* as driven by a caller that is not one provable person.

    For a non-person caller (an app, a crew member, a cron) or a slot whose
    session is not a person's own: the key never persists a sign-in again in
    this process, so such a caller cannot ride a person's vault, and a key
    reused after its slot is gone cannot inherit the earlier user.
    """
    if not isinstance(session_key, str) or not session_key:
        return
    with _lock:
        if session_key not in _bindings:
            # Over the cap with nothing but conflicts, the oldest conflict goes:
            # that key is left unbound, which persists nothing either.
            if not _make_room(MAX_BINDINGS - 1):
                _bindings.popitem(last=False)
        _bindings[session_key] = _CONFLICT
        _bindings.move_to_end(session_key)


def _make_room(limit: int) -> bool:
    """Drop the oldest single-user bindings until at most *limit* remain. Caller holds the lock.

    A conflict is never dropped while a binding can go instead: dropping it would
    let the next caller bind the key cleanly. Returns False when only conflicts
    are left over *limit*.
    """
    while len(_bindings) > limit:
        oldest = next((k for k, v in _bindings.items() if v is not _CONFLICT), None)
        if oldest is None:
            return False
        del _bindings[oldest]
    return True


def principal_for(session_key: object) -> str | None:
    """The one principal bound to *session_key*; ``None`` when unbound or conflicted."""
    if not isinstance(session_key, str) or not session_key:
        return None
    with _lock:
        found = _bindings.get(session_key)
    return found if isinstance(found, str) else None


def is_bound_to(session_key: object, principal: object) -> bool:
    """Whether *session_key* is still bound to exactly *principal*."""
    return (
        isinstance(principal, str) and bool(principal) and principal_for(session_key) == principal
    )


def principal_dir_name(principal: str) -> str:
    """The vault directory name for *principal*: a digest, never the subject itself."""
    return hashlib.sha256(principal.encode("utf-8")).hexdigest()


def reset_for_tests() -> None:
    """Forget every binding. Tests only."""
    with _lock:
        _bindings.clear()
