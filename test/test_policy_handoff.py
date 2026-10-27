"""An enterprise gateway hands its installed ceiling to its sandboxed children.

A Kiro Crew MCP server that kiro-cli starts inside the agent sandbox enters the central
tier in an enterprise build, but the sandbox hides both the policy cache and the Kiro
sign-in from it. So the gateway writes the ceiling it installed to ``policy_handoff``,
which the sandbox seals read-only, and a sandboxed process boots from that file alone:
no fetch, no cache, no sign-in. An absent or damaged handoff refuses boot. An oss build
neither writes nor reads it.

The https transport is a fake from the shared ``transport`` fixture, so nothing here
opens a socket.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from governance_distribution_helpers import _hermetic_governance_globals  # noqa: F401
from governance_distribution_helpers import install_ceiling_fixture  # noqa: F401
from governance_distribution_helpers import transport_fixture  # noqa: F401
from governance_distribution_helpers import _body, _doc, _static_fetcher

from kiro_crew import sandbox, sandbox_launch_mark, sandbox_tiers, security
from kiro_crew.platform import governance
from kiro_crew.platform import policy_distribution as pd
from kiro_crew.platform.context import PlatformCompositionError

_REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"


@pytest.fixture(autouse=True)
def _outside_any_sandbox(_floor_monkeypatch):
    """Start every test as the gateway: unsandboxed, with a clean handoff memo.

    The suite itself may run inside a Kiro Crew sandbox, whose marker would otherwise
    make every process here a child.
    """
    _floor_monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER, raising=False)
    pd.reset_process_state()
    yield
    pd.reset_process_state()


@pytest.fixture(name="enterprise")
def enterprise_fixture(monkeypatch):
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


def _as_child(monkeypatch) -> None:
    """Become a process the sandbox launcher started, after the entry point's pop."""
    monkeypatch.setattr(sandbox_launch_mark, "_launched_in_sandbox", True)
    pd.reset_process_state()


def _handoff_file() -> Path:
    return pd.handoff_dir() / pd._HANDOFF_FILE_LEAF


def _gateway_boot(transport, marker: str = "central") -> list:
    """Boot as the gateway against a fake control plane; returns the requests it made."""
    seen: list = []
    transport(_static_fetcher(pd.FetchedPolicy(body=_body(marker)), seen), scheme="https")
    ceiling = governance.load_security_policy()
    assert ceiling is not None and ceiling.identity_issuer == marker
    return seen


def _child_transport(transport) -> list:
    """A control plane that records any request a child makes, and fails it."""
    seen: list = []

    def fetcher(request):
        seen.append(request)
        raise TimeoutError("a sandboxed child must not fetch")

    transport(fetcher, scheme="https")
    return seen


class TestTheSpellingsAgree:
    def test_the_marker_is_the_launchers(self):
        assert sandbox_launch_mark.SANDBOX_MARKER == sandbox_tiers._IN_SANDBOX_MARKER

    def test_the_leaf_is_the_sandboxs(self):
        assert pd.HANDOFF_DIR_LEAF == sandbox._POLICY_HANDOFF_LEAF


class TestTheGatewayHandsOff:
    def test_boot_writes_the_installed_document(self, enterprise, transport):
        _gateway_boot(transport)

        body, why = pd.read_handoff()
        assert why == ""
        assert json.loads(body)["identity"]["issuer"] == "central"

    def test_the_handoff_carries_only_the_document(self, enterprise, transport):
        """No source, no header, no sign-in: only what the child must enforce."""
        _gateway_boot(transport)

        wrapper = json.loads(_handoff_file().read_bytes())
        assert sorted(wrapper) == ["policy", "sha256", "version"]
        assert pd.ENTERPRISE_POLICY_URL not in _handoff_file().read_text(encoding="utf-8")

    def test_an_oss_gateway_writes_nothing(self, transport, monkeypatch):
        monkeypatch.setenv(pd.POLICY_URL_ENV, "https://policy.example/p.json")
        _gateway_boot(transport)

        assert not _handoff_file().exists()

    def test_a_refresh_hands_off_the_new_document(self, transport, install_ceiling, enterprise):
        _gateway_boot(transport, "first")
        install_ceiling(governance.parse_policy(_doc("first")))
        transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"))), scheme="https")

        assert pd.refresh_now().status == pd.REFRESH_APPLIED
        body, _ = pd.read_handoff()
        assert json.loads(body)["identity"]["issuer"] == "pushed"

    def test_a_refused_install_puts_the_previous_handoff_back(
        self, transport, install_ceiling, enterprise, monkeypatch
    ):
        _gateway_boot(transport, "first")
        install_ceiling(governance.parse_policy(_doc("first")))
        transport(_static_fetcher(pd.FetchedPolicy(body=_body("pushed"))), scheme="https")

        def refuse(ceiling):
            raise PlatformCompositionError("does not compose")

        monkeypatch.setattr(pd, "apply_ceiling", refuse)

        assert pd.refresh_now().status == pd.REFRESH_REJECTED
        body, _ = pd.read_handoff()
        assert json.loads(body)["identity"]["issuer"] == "first"

    def test_a_rollback_leaves_a_newer_publication_alone(self, enterprise):
        """Another process publishing C between this refresh's publish and its rollback wins."""
        pd.write_cache(_body("pushed"), source=pd.ENTERPRISE_POLICY_URL)
        pd.publish_handoff(_body("pushed"))
        pd.write_cache(_body("newer"), source=pd.ENTERPRISE_POLICY_URL)

        pd.restore_handoff(_body("first"), published=_body("pushed"))

        body, _ = pd.read_handoff()
        assert json.loads(body)["identity"]["issuer"] != "first"

    def test_a_rollback_still_runs_when_the_cache_kept_the_refused_document(self, enterprise):
        """The cache rollback failed and still holds the refused B: the handoff goes back to A."""
        pd.write_cache(_body("pushed"), source=pd.ENTERPRISE_POLICY_URL)
        pd.publish_handoff(_body("pushed"))

        pd.restore_handoff(_body("first"), published=_body("pushed"))

        body, _ = pd.read_handoff()
        assert json.loads(body)["identity"]["issuer"] == "first"

    def test_a_rollback_with_no_previous_copy_removes_the_handoff(self, enterprise):
        pd.publish_handoff(_body("pushed"))

        pd.restore_handoff(None, published=_body("pushed"))

        assert not _handoff_file().exists()

    def test_a_cache_that_moved_on_is_not_rolled_back(self, enterprise):
        """A process holding an older ceiling leaves a newer process's handoff alone."""
        pd.write_cache(_body("newer"), source=pd.ENTERPRISE_POLICY_URL)
        pd.publish_handoff(_body("newer"))
        pd.reset_process_state()

        pd.publish_handoff(_body("older"))

        body, _ = pd.read_handoff()
        assert json.loads(body)["identity"]["issuer"] == "newer"

    def test_a_failed_write_withdraws_a_stale_handoff(self, enterprise, monkeypatch):
        pd.publish_handoff(_body("stale"))
        pd.reset_process_state()

        def fail(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(pd, "_write_cache_file", fail)
        pd.publish_handoff(_body("current"))

        assert not _handoff_file().exists()

    def test_a_stale_handoff_that_cannot_be_removed_refuses(self, enterprise, monkeypatch):
        pd.publish_handoff(_body("stale"))
        pd.reset_process_state()

        def fail(*args, **kwargs):
            raise OSError("read-only file system")

        monkeypatch.setattr(pd, "_write_cache_file", fail)
        monkeypatch.setattr(Path, "unlink", fail)

        with pytest.raises(PlatformCompositionError, match="could not be removed"):
            pd.publish_handoff(_body("current"))


class TestTheChildEnforcesIt:
    def test_the_child_boots_from_the_handoff_without_fetching(
        self, enterprise, transport, monkeypatch
    ):
        _gateway_boot(transport)
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        ceiling = governance.load_security_policy()

        assert ceiling is not None
        assert ceiling.identity_issuer == "central"
        assert ceiling.tier == governance.TIER_CENTRAL
        assert seen == []

    def test_the_child_ignores_the_cache(self, enterprise, transport, monkeypatch):
        """The cache is hidden in the sandbox; even where it reads, the handoff decides."""
        _gateway_boot(transport, "handed")
        pd.write_cache(_body("cached-only"), source=pd.ENTERPRISE_POLICY_URL)
        _as_child(monkeypatch)
        _child_transport(transport)

        assert governance.load_security_policy().identity_issuer == "handed"

    def test_the_child_does_not_rewrite_the_handoff(self, enterprise, transport, monkeypatch):
        _gateway_boot(transport)
        before = _handoff_file().stat().st_mtime_ns
        _as_child(monkeypatch)
        _child_transport(transport)

        governance.load_security_policy()

        assert _handoff_file().stat().st_mtime_ns == before

    def test_a_child_refresh_never_fetches(self, enterprise, transport, monkeypatch):
        _gateway_boot(transport)
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        outcome = pd.refresh_now(force=True)

        assert outcome.status == pd.REFRESH_REJECTED
        assert seen == []

    def test_the_marker_noted_before_the_pop_still_counts(self, monkeypatch):
        monkeypatch.setenv(sandbox_launch_mark.SANDBOX_MARKER, "1")
        sandbox_launch_mark.note()
        monkeypatch.delenv(sandbox_launch_mark.SANDBOX_MARKER)

        assert sandbox_launch_mark.launched_in_sandbox() is True

    @pytest.mark.parametrize("entry", ["mcp_entry.py", "cli.py"])
    def test_each_entry_point_notes_the_marker_before_dropping_it(self, entry):
        text = (_REPO_SRC / entry).read_text(encoding="utf-8")
        noted = text.index("sandbox_launch_mark.note()")
        dropped = text.index('os.environ.pop("KIROCREW_SANDBOX_ACTIVE", None)')
        assert noted < dropped


class TestTheChildFailsClosed:
    def _expect_refusal(self, transport, match: str) -> None:
        seen = _child_transport(transport)
        with pytest.raises(PlatformCompositionError, match=match):
            governance.load_security_policy()
        assert seen == []

    def test_no_handoff_refuses(self, enterprise, transport, monkeypatch):
        _as_child(monkeypatch)
        self._expect_refusal(transport, "has not handed a ceiling over")

    def test_an_empty_precreated_directory_refuses(self, enterprise, transport, monkeypatch):
        pd.handoff_dir().mkdir(parents=True)
        _as_child(monkeypatch)
        self._expect_refusal(transport, "has not handed a ceiling over")

    def test_a_document_that_does_not_match_its_digest_refuses(
        self, enterprise, transport, monkeypatch
    ):
        _gateway_boot(transport)
        wrapper = json.loads(_handoff_file().read_bytes())
        wrapper["policy"] = json.dumps(_doc("looser"))
        _handoff_file().write_text(json.dumps(wrapper), encoding="utf-8")
        _as_child(monkeypatch)

        self._expect_refusal(transport, "does not match its digest")

    def test_a_handoff_that_is_not_json_refuses(self, enterprise, transport, monkeypatch):
        pd.handoff_dir().mkdir(parents=True)
        _handoff_file().write_bytes(b"\x00not json")
        _as_child(monkeypatch)
        self._expect_refusal(transport, "not valid JSON")

    def test_an_unknown_format_refuses(self, enterprise, transport, monkeypatch):
        pd.handoff_dir().mkdir(parents=True)
        _handoff_file().write_text(json.dumps({"version": 2}), encoding="utf-8")
        _as_child(monkeypatch)
        self._expect_refusal(transport, "unknown format")

    def test_a_valid_wrapper_around_an_invalid_policy_refuses(
        self, enterprise, transport, monkeypatch
    ):
        pd.handoff_dir().mkdir(parents=True)
        _handoff_file().write_bytes(pd._handoff_payload(b'{"version": 999}'))
        _as_child(monkeypatch)
        self._expect_refusal(transport, "handoff is unusable")

    @pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
    def test_a_linked_handoff_refuses(self, enterprise, transport, monkeypatch, tmp_path):
        planted = tmp_path / "planted.json"
        planted.write_bytes(pd._handoff_payload(_body("planted")))
        pd.handoff_dir().mkdir(parents=True)
        _handoff_file().symlink_to(planted)
        _as_child(monkeypatch)
        self._expect_refusal(transport, "is a link")


class TestOssIsUnchanged:
    def test_a_sandboxed_oss_process_neither_reads_nor_requires_it(self, transport, monkeypatch):
        _as_child(monkeypatch)
        seen = _child_transport(transport)

        assert governance.load_security_policy() is None
        assert seen == []
        assert pd.takes_handoff() is False

    def test_a_sandboxed_oss_process_ignores_a_planted_handoff(self, monkeypatch):
        pd.handoff_dir().mkdir(parents=True)
        _handoff_file().write_bytes(pd._handoff_payload(_body("planted")))
        _as_child(monkeypatch)

        assert governance.load_security_policy() is None

    def test_an_app_backend_keeps_cache_only_mode(self, enterprise, monkeypatch):
        """The gateway chose cache-only for an app backend; the handoff does not override it."""
        monkeypatch.setenv(pd.POLICY_CACHE_ONLY_ENV, "1")
        _as_child(monkeypatch)

        assert pd.takes_handoff() is False
        assert pd.hands_off() is False


class TestTheSandboxKeepsItReadOnly:
    """The handoff is sealed read-only and pre-created; the vault and the cache stay hidden."""

    @pytest.mark.parametrize("listed", ["_STANDARD_DIRS", "_CC_DIRS", "_STRICT_DIRS"])
    def test_the_vault_the_sign_in_and_the_cache_stay_hidden(self, listed):
        hidden = set(getattr(sandbox, listed))
        for leaf in (".vault", "policy_cache", "kas"):
            assert f".kiro/crew/{leaf}" in hidden, leaf
        assert ".kiro/crew/policy_handoff" not in hidden

    def test_the_leaf_is_sealed_read_only_and_precreated(self):
        assert "policy_handoff" in sandbox._CREW_READONLY_LEAVES
        assert "policy_handoff" in sandbox._CREW_PRECREATE_READONLY_DIR_LEAVES
        assert "policy_handoff" in sandbox._CREW_NOFOLLOW_READONLY_DIR_LEAVES
        assert "policy_handoff" not in sandbox._CREW_HIDDEN_LEAVES

    def test_no_foreign_harness_child_may_read_it(self):
        assert "policy_handoff" in sandbox._CREW_CHILD_WITHHELD_LEAVES

    def test_the_agents_file_tools_are_refused_it(self, tmp_path, monkeypatch):
        target = str(_handoff_file())
        assert security.is_sensitive_path(target)
        assert security.is_sensitive_write_path(target)
