"""``capabilities.publish`` lists ``regions``, ``audience`` and ``accounts``.

RFC ``docs/request-for-change/rfc-crew-enterprise-edition.md``, appendix rows
"Deploy regions", "Publish audiences" and "Deploy accounts". A policy names the
regions and cloud accounts a deploy may write to and who may read what is
published; each is asked before anything is uploaded. In an enterprise build a
list the remote policy leaves out is off, even when it names ``publish``. The
``oss`` edition with no list behaves as before.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew import publish_governance as pg
from kiro_crew import publish_sync
from kiro_crew.deploy import engine
from kiro_crew.deploy import handlers as deploy_handlers
from kiro_crew.deploy import handlers_sites
from kiro_crew.platform import governance
from kiro_crew.platform.governance import SCOPE_CATALOG, parse_policy, resolve

_SCOPE = "capabilities.publish"
_ACCOUNT = "123456789012"


def _ceiling(publish: dict | None = None, tier: str = "") -> governance.GovernanceCeiling:
    doc: dict = {"version": 1, "boot": {}}
    if publish is not None:
        doc["capabilities"] = {"publish": publish}
    ceiling = parse_policy(doc)
    return dataclasses.replace(ceiling, tier=tier) if tier else ceiling


def _publish_on(**lists: dict) -> dict:
    scopes = {"destinations": {"mode": "allow", "allow": ["deploy-web-aws"]}, **lists}
    return {"enabled": True, "scopes": scopes}


@pytest.fixture
def enterprise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kiro_crew.edition.EDITION", "enterprise")


# ── The keys ────────────────────────────────────────────────────────────────


def test_the_three_lists_are_catalog_keys() -> None:
    matchers = SCOPE_CATALOG[_SCOPE].scope_matchers
    assert {matchers[k] for k in ("regions", "audience", "accounts")} == {"identifier"}


def test_a_named_list_bounds_its_items() -> None:
    ceiling = _ceiling(
        _publish_on(
            regions={"mode": "allow", "allow": ["us-*"]},
            audience={"mode": "allow", "allow": ["private"]},
            accounts={"mode": "deny", "deny": [_ACCOUNT]},
        )
    )
    assert resolve(ceiling, None, _SCOPE, "regions:us-west-2").permitted
    assert not resolve(ceiling, None, _SCOPE, "regions:eu-west-1").permitted
    assert resolve(ceiling, None, _SCOPE, "audience:private").permitted
    assert not resolve(ceiling, None, _SCOPE, "audience:public").permitted
    assert not resolve(ceiling, None, _SCOPE, f"accounts:{_ACCOUNT}").permitted
    assert resolve(ceiling, None, _SCOPE, "accounts:210987654321").permitted


@pytest.mark.parametrize("publish", [None, _publish_on()])
def test_oss_without_the_lists_permits_as_before(publish: dict | None) -> None:
    for item in ("regions:eu-west-1", "audience:public", f"accounts:{_ACCOUNT}"):
        assert resolve(_ceiling(publish), None, _SCOPE, item).permitted, item


def test_enterprise_named_publish_keeps_unnamed_lists_off(enterprise: None) -> None:
    ceiling = _ceiling(_publish_on(regions={"mode": "allow", "allow": ["us-east-1"]}))
    assert resolve(ceiling, None, _SCOPE, "destinations:deploy-web-aws").permitted
    assert resolve(ceiling, None, _SCOPE, "regions:us-east-1").permitted
    assert not resolve(ceiling, None, _SCOPE, "audience:public").permitted
    assert not resolve(ceiling, None, _SCOPE, f"accounts:{_ACCOUNT}").permitted


def test_enterprise_local_tier_cannot_supply_a_list_the_remote_left_out(
    enterprise: None,
) -> None:
    central = _ceiling(_publish_on(), tier=governance.TIER_CENTRAL)
    home = _ceiling(
        _publish_on(
            regions={"mode": "deny", "deny": []},
            audience={"mode": "deny", "deny": []},
        ),
        tier=governance.TIER_HOME,
    )
    composed = governance.compose_tier_ladder(central, home)
    assert not resolve(composed, None, _SCOPE, "regions:us-east-1").permitted
    assert not resolve(composed, None, _SCOPE, "audience:public").permitted


def test_enterprise_remote_may_open_a_list(enterprise: None) -> None:
    central = _ceiling(
        _publish_on(audience={"mode": "allow", "allow": ["public"]}),
        tier=governance.TIER_CENTRAL,
    )
    composed = governance.compose_tier_ladder(central)
    assert resolve(composed, None, _SCOPE, "audience:public").permitted
    assert not resolve(composed, None, _SCOPE, "regions:us-east-1").permitted


# ── The deploy decision ─────────────────────────────────────────────────────


def _answer(denied_items: set[str]):
    asked: list[str] = []

    def _permits(scope, item, **_kw):
        asked.append(item)
        return SimpleNamespace(
            permitted=item not in denied_items, reason="nope", rule="r", layer="policy"
        )

    return asked, _permits


def _patch(monkeypatch, denied: set[str], governed: bool) -> list[str]:
    asked, permits = _answer(denied)
    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", permits)
    monkeypatch.setattr(pg, "publish_list_governed", lambda _s, _n: governed)
    return asked


def test_deploy_asks_audience_and_region_but_reads_no_account_when_unbounded(
    monkeypatch,
) -> None:
    asked = _patch(monkeypatch, set(), governed=False)

    def _no_lookup() -> str:  # pragma: no cover - reached only on regression
        raise AssertionError("account read with no accounts list")

    assert pg.deploy_target_denied_reason("s", "us-east-1", _no_lookup) is None
    assert asked == ["audience:public", "regions:us-east-1"]


def test_deploy_denied_region_stops_before_the_account(monkeypatch) -> None:
    asked = _patch(monkeypatch, {"regions:eu-west-1"}, governed=True)
    reason = pg.deploy_target_denied_reason("s", "eu-west-1", lambda: _ACCOUNT)
    assert reason and "regions" in reason
    assert "accounts:" + _ACCOUNT not in asked


def test_deploy_asks_the_account_when_bounded(monkeypatch) -> None:
    asked = _patch(monkeypatch, {f"accounts:{_ACCOUNT}"}, governed=True)
    reason = pg.deploy_target_denied_reason("s", "us-east-1", lambda: _ACCOUNT)
    assert reason and "accounts" in reason
    assert asked[-1] == f"accounts:{_ACCOUNT}"


@pytest.mark.parametrize("lookup", [lambda: "", lambda: (_ for _ in ()).throw(OSError())])
def test_deploy_unreadable_account_denies(monkeypatch, lookup) -> None:
    _patch(monkeypatch, set(), governed=True)
    assert pg.deploy_target_denied_reason("s", "us-east-1", lookup)


def test_governance_error_denies(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise RuntimeError("x")

    monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _boom)
    assert pg.publish_list_denied_reason("s", "deploy-web-aws", "regions", "us-east-1")


def test_list_probe_reads_both_levels(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.resolve_active_scope", lambda _s: None
    )
    ctx = SimpleNamespace(governance=_ceiling(_publish_on(accounts={"mode": "deny", "deny": []})))
    monkeypatch.setattr("kiro_crew.platform.context.current_context", lambda: ctx)
    assert pg.publish_list_governed("s", "accounts")
    assert not pg.publish_list_governed("s", "regions")
    ctx.governance = None
    assert not pg.publish_list_governed("s", "accounts")


@pytest.mark.parametrize(
    ("rc", "out", "want"),
    [(0, f"{_ACCOUNT}\n", _ACCOUNT), (0, "not-an-id", ""), (255, _ACCOUNT, "")],
)
def test_deploy_account_comes_from_the_credentials(monkeypatch, rc, out, want) -> None:
    calls: list[list[str]] = []

    def _run(args, profile, timeout=30, **_kw):
        calls.append(args)
        return rc, out, ""

    monkeypatch.setattr(engine, "run_aws", _run)
    assert handlers_sites._deploy_account("dev") == want
    assert calls[0][:2] == ["sts", "get-caller-identity"]


def test_do_deploy_refuses_before_staging(monkeypatch) -> None:
    async def _profile(_p):
        return "dev", "eu-west-1"

    seen: dict = {}

    def _deny(session_key, region, _account_of):
        seen.update(session_key=session_key, region=region)
        return "regions 'eu-west-1' is not permitted"

    monkeypatch.setattr(deploy_handlers, "_resolve_profile", _profile)
    monkeypatch.setattr(deploy_handlers, "deploy_target_denied_reason", _deny)
    monkeypatch.setattr(handlers_sites.os, "name", "posix")
    status, body = asyncio.run(
        deploy_handlers._do_deploy({"site_id": "s", "artifact_slug": "a"}, session_key="k")
    )
    assert status == 403
    assert body["code"] == "publish_destination_disabled"
    assert seen == {"session_key": "k", "region": "eu-west-1"}


# ── Artifact publish audience and the unchecked cloud destination ───────────


def test_personal_drive_refused_while_regions_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.platform.governance_profiles.governance_permits",
        lambda *_a, **_k: SimpleNamespace(permitted=True),
    )
    monkeypatch.setattr(pg, "_read_allowed_destinations", lambda: ([], None))
    request = SimpleNamespace(headers={"X-Session-Key": "s"})
    monkeypatch.setattr(pg, "publish_list_governed", lambda _s, name: name == "regions")
    assert "regions" in (pg.publish_denied_reason(request, "personal-drive") or "")
    assert pg.publish_denied_reason(request, "deploy-web-aws") is None
    monkeypatch.setattr(pg, "publish_list_governed", lambda _s, _n: False)
    assert pg.publish_denied_reason(request, "personal-drive") is None


@pytest.mark.asyncio
async def test_artifact_publish_refuses_a_denied_audience(tmp_path, monkeypatch) -> None:
    from test_artifact_sharing_handlers import _request

    from kiro_crew import artifacts as art_mod
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(art_handlers, "_publish_governance_denied", lambda *_a: None)
    asked: list[tuple] = []

    def _audience(_req, provider, visibility):
        asked.append((provider, visibility))
        return "audience 'public' is not permitted"

    monkeypatch.setattr(art_handlers, "_publish_audience_denied", _audience)
    dispatched = AsyncMock()
    monkeypatch.setattr(publish_sync, "publish", dispatched)
    monkeypatch.setattr(publish_sync, "update_sharing", dispatched)
    art = store.create(name="Doc", content="x", kind="html")

    for handler in (art_handlers.api_artifact_publish, art_handlers.api_artifact_update_sharing):
        resp = await handler(_request(match={"slug": art.slug}, body={"visibility": "PUBLIC"}))
        assert resp.status == 403
    assert asked == [("default", "PUBLIC"), ("default", "PUBLIC")]
    dispatched.assert_not_awaited()


def test_audience_is_asked_lower_case(monkeypatch) -> None:
    from kiro_crew.dashboard.handlers.artifacts_parts import route_plumbing

    seen: list[tuple] = []
    monkeypatch.setattr(
        route_plumbing, "publish_list_denied_reason", lambda *a: seen.append(a) or None
    )
    request = SimpleNamespace(headers={"X-Session-Key": "k"})
    assert route_plumbing._publish_audience_denied(request, "default", "SHARED") is None
    assert seen == [("k", "default", "audience", "shared")]


@pytest.mark.asyncio
async def test_republish_asks_the_current_audience_too(tmp_path, monkeypatch) -> None:
    """New bytes reach the old audience first, so a narrower request is not enough."""
    from test_artifact_sharing_handlers import _request

    from kiro_crew import artifacts as art_mod
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(art_handlers, "_publish_governance_denied", lambda *_a: None)
    monkeypatch.setattr(
        art_handlers,
        "_publish_audience_denied",
        lambda _r, _p, v: "no" if v.upper() == "PUBLIC" else None,
    )
    dispatched = AsyncMock()
    monkeypatch.setattr(publish_sync, "publish", dispatched)
    art = store.create(name="Doc", content="x", kind="html")
    from kiro_crew.artifact_store.model import ArtifactPublication

    store.set_publication(
        art.slug, ArtifactPublication(artifact_id="e", view_url="u", visibility="PUBLIC")
    )
    resp = await art_handlers.api_artifact_publish(
        _request(match={"slug": art.slug}, body={"visibility": "PRIVATE"})
    )
    assert resp.status == 403
    dispatched.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_sync_push_asks_the_current_audience(tmp_path, monkeypatch) -> None:
    """A snapshot on an auto-sync publication pushes nothing to a denied audience."""
    from test_artifact_sharing_handlers import _request

    from kiro_crew import artifacts as art_mod
    from kiro_crew.artifact_store.model import ArtifactPublication
    from kiro_crew.artifacts import ArtifactStore
    from kiro_crew.dashboard.handlers import artifacts as art_handlers

    store = ArtifactStore(root=tmp_path / "artifacts")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_handlers, "_is_restricted_session", lambda *_a: False)
    monkeypatch.setattr(art_handlers, "_publish_governance_denied", lambda *_a: None)
    asked: list[str] = []

    def _audience(_r, _p, visibility):
        asked.append(visibility)
        return "no"

    monkeypatch.setattr(art_handlers, "_publish_audience_denied", _audience)
    push = AsyncMock()
    monkeypatch.setattr(publish_sync, "push_version_by_slug", push)
    art = store.create(name="P", content="v1", kind="html")
    store.set_publication(
        art.slug,
        ArtifactPublication(artifact_id="e", view_url="u", visibility="SHARED", auto_sync=True),
    )
    resp = await art_handlers.api_artifact_update(
        _request(body={"content": "v2", "snapshot": True}, match={"slug": art.slug})
    )
    assert resp.status == 200
    assert asked == ["SHARED"]
    push.assert_not_awaited()
