"""Which dashboard requests bind a slot's session to a user for persisted MCP sign-ins."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import mcp_grant_principals as principals
from kiro_crew.dashboard.chat_utils_slots import effective_session_key
from kiro_crew.dashboard.mcp_grant_binding import bind_slot_to_request_user, request_principal
from kiro_crew.dashboard.token_auth_callers import MEMBER_CHAT_PRINCIPAL_KEY


class _Request(dict):
    """The parts of an aiohttp request the binding reads."""

    def __init__(self, owner_id: str = "", sessions=None, **claims):
        super().__init__(claims)
        self.app = {"state": SimpleNamespace(owner_id=owner_id, sessions=sessions)}


def _slot(key: str = "chat-1-1700000000", **extra):
    return SimpleNamespace(key=key, linked_session_key="", _app="", _created_by="", **extra)


@pytest.fixture(autouse=True)
def _fresh_bindings():
    principals.reset_for_tests()
    yield
    principals.reset_for_tests()


@pytest.mark.parametrize(
    ("owner_id", "claims", "expected"),
    [
        # A single-user local gateway: the machine-local bootstrap subject is the owner.
        ("", {"user": "local-app", "app": ""}, "owner"),
        ("U-OWNER", {"user": "U-OWNER", "app": ""}, "owner"),
        # Any other signed subject gets its own namespace.
        ("U-OWNER", {"user": "42", "app": ""}, "user:42"),
        ("", {"user": "someone@example.com", "app": ""}, "user:someone@example.com"),
        # App tokens, an unauthenticated caller and an empty subject bind no one.
        ("", {"user": "local-app", "app": "design-critique"}, None),
        ("", {"user": "local-app"}, None),
        ("", {"app": ""}, None),
        ("", {"user": "", "app": ""}, None),
        # A crew member's admitted request is an agent's, even on an owner subject.
        ("", {"user": "local-app", "app": "", MEMBER_CHAT_PRINCIPAL_KEY: "member:ada"}, None),
    ],
)
def test_request_principal(owner_id, claims, expected):
    assert request_principal(_Request(owner_id, **claims)) == expected


def test_two_users_on_one_slot_conflict_it():
    slot = _slot()
    key = effective_session_key(slot)  # the key the session's engine is spawned for
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    bind_slot_to_request_user(_Request(user="42", app=""), slot)
    assert principals.principal_for(key) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"linked_session_key": "slack:1700000000.0001"},
        {"_app": "design-critique"},
        {"_created_by": "chat-parent"},
    ],
)
def test_channel_app_and_spawned_slots_conflict_their_key(extra):
    slot = SimpleNamespace(
        **{"key": "chat-2", "linked_session_key": "", "_app": "", "_created_by": "", **extra}
    )
    key = effective_session_key(slot)
    # Even a key an earlier person bound (a reused slot key) cannot be inherited.
    principals.bind(key, "owner")
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) is None
    bind_slot_to_request_user(_Request(user="local-app", app=""), _slot("chat-2"))
    assert principals.principal_for(key) is None


@pytest.mark.parametrize(
    ("claims", "person"),
    [
        ({"user": "local-app", "app": "design-critique"}, True),
        ({"user": "local-app", "app": "", MEMBER_CHAT_PRINCIPAL_KEY: "member:ada"}, True),
        ({"user": "local-app", "app": ""}, False),  # an attested cron
    ],
)
def test_a_non_person_caller_on_a_persons_slot_conflicts_it(claims, person):
    slot = _slot()
    key = effective_session_key(slot)
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) == "owner"
    bind_slot_to_request_user(_Request(**claims), slot, person=person)
    assert principals.principal_for(key) is None
    # The person returning does not undo it.
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(key) is None


class _Sessions:
    """The session-map link reads the binding consults."""

    def __init__(self, slack: str = "", inbound: bool = False, broken: bool = False):
        self._slack, self._inbound, self._broken = slack, inbound, broken

    def get_slack_link(self, key):
        if self._broken:
            raise OSError("unreadable")
        return (self._slack or None, None)

    def mirror_accepts_inbound(self, key):
        return self._inbound


@pytest.mark.parametrize(
    "sessions",
    [_Sessions(slack="1700000000.0001"), _Sessions(inbound=True), _Sessions(broken=True)],
)
def test_a_channel_driven_session_conflicts_even_for_the_owner(sessions):
    """A persisted Slack thread or two-way mirror survives a restart; so does the conflict."""
    slot = _slot()
    bind_slot_to_request_user(_Request(user="local-app", app="", sessions=sessions), slot)
    assert principals.principal_for(effective_session_key(slot)) is None


def test_a_slack_linked_slot_conflicts():
    slot = _slot(_slack_linked=True)
    bind_slot_to_request_user(_Request(user="local-app", app=""), slot)
    assert principals.principal_for(effective_session_key(slot)) is None


def test_an_unlinked_session_binds_the_owner():
    slot = _slot()
    bind_slot_to_request_user(_Request(user="local-app", app="", sessions=_Sessions()), slot)
    assert principals.principal_for(effective_session_key(slot)) == "owner"


def test_linking_a_session_to_a_channel_conflicts_its_key():
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.session_parts import links

    class _Map:
        def set_slack_link(self, *a):
            pass

        def set_mirror_link(self, *a, **k):
            pass

    manager = SimpleNamespace(_session_map=_Map())
    principals.bind("dashboard:a", "owner")
    links.set_slack_link(manager, "dashboard:a", "1700000000.0001", "C1")
    assert principals.principal_for("dashboard:a") is None

    principals.bind("dashboard:b", "owner")
    link = ChannelLink(channel_type="telegram", channel_id="42")
    links.set_mirror_link(manager, "dashboard:b", link)  # outbound only
    assert principals.principal_for("dashboard:b") == "owner"
    links.set_mirror_link(manager, "dashboard:b", link, accepts_inbound=True)
    assert principals.principal_for("dashboard:b") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_second_user_on_any_slot_route_conflicts_it(method):
    """Continue, regenerate, edit-resend, reload ... all pass the per-slot checkpoint."""
    from kiro_crew.dashboard.mcp_grant_binding import bind_routed_slot

    slot = _slot()
    key = effective_session_key(slot)
    owner = _Request(user="local-app", app="")
    owner.method = "POST"
    bind_routed_slot(owner, slot)
    assert principals.principal_for(key) == "owner"
    other = _Request("U-OWNER", user="42", app="")
    other.method = method
    bind_routed_slot(other, slot)
    assert principals.principal_for(key) is None


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_read_on_a_slot_route_binds_nothing(method):
    from kiro_crew.dashboard.mcp_grant_binding import bind_routed_slot

    slot = _slot()
    reader = _Request("U-OWNER", user="42", app="")
    reader.method = method
    bind_routed_slot(reader, slot)
    assert principals.principal_for(effective_session_key(slot)) is None
    principals.bind(effective_session_key(slot), "owner")
    bind_routed_slot(reader, slot)
    assert principals.principal_for(effective_session_key(slot)) == "owner"


def test_the_owner_focus_prefetch_binds_the_owner():
    from kiro_crew.dashboard.mcp_grant_binding import bind_slot_to_principal

    slot = _slot()
    bind_slot_to_principal(slot, principals.OWNER_PRINCIPAL, _Sessions())
    assert principals.principal_for(effective_session_key(slot)) == "owner"
    linked = _slot("chat-9")
    bind_slot_to_principal(linked, principals.OWNER_PRINCIPAL, _Sessions(slack="1.2"))
    assert principals.principal_for(effective_session_key(linked)) is None
