"""The last-known-good policy cache, and the process state the distribution tier keeps.

The cache's layout under the data home, the recorded-source identity, its read paths and
its locked compare-and-swap writes; and the per-process state -- the fetch window, the
cache-directory memo, the installed-document digest and the post-install hook list --
with :func:`reset_process_state`, which clears all of it. The two share a module because
that reset rebinds the cache-directory memo and the installed digest together, and a
global is rebound in the one module that binds it.

:mod:`kiro_crew.platform.policy_distribution` is the import path and the patch surface:
callers import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import stat
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

from kiro_crew.atomic_write import atomic_write, read_bytes_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.platform.governance import MAX_POLICY_BYTES
from kiro_crew.platform.policy_distribution_source import (
    _body_digest,
    _is_enterprise,
    _split_url,
    cache_only,
)
from kiro_crew.platform_compat import file_lock, is_link_or_junction, make_owner_only_dir

logger = logging.getLogger("kiro_crew.platform.policy_distribution")


# ── Cache layout, under the data home and on the sensitive-path keystone ──
CACHE_DIR_LEAF = "policy_cache"
_CACHE_DOC_LEAF = "policy.json"
_CACHE_META_LEAF = "policy.meta.json"
#: Lock file serialising doc+meta pair mutations. Carries no content, so it is never
#: read and never validated as a policy -- and it must not collide with either leaf.
_CACHE_LOCK_LEAF = "policy.lock"

#: When this tier last went to the network, across every caller.  See
#: :func:`_claim_fetch_slot` — ``load_security_policy`` is re-run per app callback,
#: so the tier needs its own bound independent of any one caller's discipline.
_last_fetch_attempt: float = 0.0
_FETCH_SLOT_LOCK = threading.Lock()

#: The cache directory this process has already created and tightened, or ``None``.
#: See :func:`_ensure_cache_dir`.
_cache_dir_ready: Optional[Path] = None

#: Digest of the policy document whose ceiling this process last INSTALLED.  See
#: :func:`_installed_digest` — it is what lets a 304 be judged against the ceiling in
#: effect rather than against a cache another process may have moved ahead.
_installed_body_digest: str = ""
_INSTALLED_LOCK = threading.Lock()

# ── The ceiling handoff to sandboxed children (enterprise build only) ──
#: Mirrored as ``sandbox_leaves._POLICY_HANDOFF_LEAF``; ``test_policy_handoff`` pins them.
HANDOFF_DIR_LEAF = "policy_handoff"
_HANDOFF_FILE_LEAF = "ceiling.json"
_HANDOFF_VERSION = 1
#: The body plus a small JSON wrapper; anything larger is not a handoff this module wrote.
_MAX_HANDOFF_BYTES = 2 * MAX_POLICY_BYTES + 4096
#: Digest of the document this process last made the handoff hold, or ``""``.
_handoff_digest: str = ""


# ──────────────────────────────────────────────────────────────────────────
# The last-known-good cache
# ──────────────────────────────────────────────────────────────────────────


def cache_dir() -> Path:
    """The cache directory, resolved lazily.

    Never a module-level ``config_dir()`` capture: importing this trust-root module
    must not fire the one-time data-home migration as a side effect, which is the
    same rule ``governance._policy_home_path`` follows.
    """
    return config_dir() / CACHE_DIR_LEAF


#: Query parameters that carry a signature rather than select a document, keyed by the
#: presigning scheme that defines them. Dropped from the source identity because they ROTATE:
#: whatever provisions the host re-issues the URL, and an identity that moves with the
#: credential discards the last-known-good on every rotation.
#:
#: A NAME list, not a heuristic on the value, and a small one: everything not named here stays
#: in the identity. That is the safe direction for both failure modes. An unknown presigning
#: scheme rotates the identity and costs the outage fallback until the next successful fetch —
#: bounded, and visible as a fetch. Wrongly dropping a document-SELECTING parameter costs the
#: opposite: ``?policy=team-a`` and ``?policy=team-b`` would share an identity, so a repoint to
#: a stricter policy would keep serving the looser one — indefinitely on a boot-only source,
#: which never re-fetches once a ceiling is established.
_PRESIGN_QUERY_PREFIXES = (
    # AWS SigV4 (S3 and anything behind it) and Google's copy of the same design.
    "x-amz-",
    "x-goog-",
)
#: Azure Blob SAS, which uses short opaque names rather than a namespaced prefix: signature,
#: service version, resource, permissions, and the start/expiry/protocol/identity set.
#:
#: Stripped ONLY when the query is positively identified as a SAS (see
#: :data:`_AZURE_SAS_MARKERS`), because these names are short and generic enough to be somebody
#: else's selector: ``?sp=team-a`` reads perfectly well as "policy = team-a", and collapsing it
#: with ``?sp=team-b`` is the exact failure the namespaced families cannot cause.
_AZURE_SAS_QUERY_NAMES = frozenset(
    {
        "sig",
        "sv",
        "sr",
        "sp",
        "st",
        "se",
        "spr",
        "sip",
        "si",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
        "srt",
        "ss",
    }
)

#: What makes a query positively a SAS rather than a coincidence: the signature AND the service
#: version, both of which every Azure SAS carries and neither of which a plain selector needs.
#: Requiring both keeps a lone ``?sig=…`` or ``?sv=…`` in the identity — the safe direction,
#: because an unrecognised rotation costs one fetch while a collapsed selector costs a superseded
#: ceiling staying in force.
_AZURE_SAS_MARKERS = frozenset({"sig", "sv"})


def _identity_query(query: str) -> str:
    """*query* with presigning parameters removed, order-normalised.

    Sorted so two spellings of the same selection compare equal: a URL is not required to
    preserve parameter order, and an identity that changed with it would discard the cache on a
    re-issue that merely reordered.
    """
    if not query:
        return ""
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    drop_sas = _AZURE_SAS_MARKERS <= {name.lower() for name, _ in pairs}
    kept = []
    for name, value in pairs:
        lowered = name.lower()
        if lowered.startswith(_PRESIGN_QUERY_PREFIXES):
            continue
        if drop_sas and lowered in _AZURE_SAS_QUERY_NAMES:
            continue
        kept.append((name, value))
    return urllib.parse.urlencode(sorted(kept))


def _source_digest(source: str) -> str:
    """A stable, non-reversible stand-in for a source URL.

    The cache records WHICH source a copy came from so the repoint rule can refuse a
    retired endpoint's document. That question is an equality test, and an equality test
    does not need the plaintext — which matters because the URL may BE the credential: a
    pre-signed object-store link carries its signature in the query string, and this
    module's rule is that the source is emitted nowhere. Storing it in a file made the
    cache the one place that broke that rule, and the app backend reads that file.

    Not reversible in any useful sense: what an attacker would need is the signature, and
    that is an HMAC-SHA256 whose search space a preimage attempt cannot walk. An empty
    source digests to the empty string so "no source recorded" stays distinguishable from
    "recorded a source", rather than both hashing to the same constant.

    **The identity is what SELECTS the document, not what authenticates the request.** The
    fragment and the basic-auth PASSWORD are dropped (the username is not — it names the account,
    and two tenants at one host and path are two different documents), and so are the query parameters that carry a signature (see
    :data:`_PRESIGN_QUERY_NAMES`) — because a pre-signed URL is the documented shape for the
    environment channel and its signature ROTATES by design. Hashing the whole URL made every
    rotation look like a repoint, so the cache was discarded as a retired endpoint's copy and a
    host that then hit a transient outage had no last-known-good, aborting startup under the
    fail-closed default. Nobody had to do anything for that; it was the credential doing what
    credentials do.

    Every OTHER query parameter stays in, because a query can also select the document:
    ``?policy=team-a`` and ``?policy=team-b`` are different sources, and treating them as one
    would let a repoint to a stricter policy keep serving the looser one — indefinitely on a
    boot-only source, which never re-fetches once a ceiling is established. Kept parameters are
    sorted, so a re-issue that merely reorders them is not a repoint either.

    Scheme and host are lowercased (both are case-insensitive) and the path is not (it is not).
    """
    if not source:
        return ""
    parts = _split_url(source)
    host = (parts.hostname or "").lower()
    # ``.port`` is a LAZILY parsed property, so ``_split_url``'s guard does not cover it: it
    # raises ValueError for a non-numeric or out-of-range port, and this function is on the
    # boot path. The raw netloc port text is used instead when it cannot be parsed — an
    # identity does not need the number, only stability, and two spellings of an unusable port
    # are not a repoint between them.
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        _, _, raw_port = (parts.netloc or "").rpartition(":")
        port = f":{raw_port}" if raw_port else ""
    query = _identity_query(parts.query)
    # The USERNAME stays, the password goes — the same selector/credential split the query gets.
    # With basic auth the username names the account, and two tenants at one host and path are
    # two different documents: collapsing them would leave tenant A's possibly looser ceiling in
    # force after a repoint to tenant B. The password is the rotating half, so including it would
    # reintroduce exactly the discard-on-rotation this function exists to avoid.
    user = f"{parts.username}@" if parts.username else ""
    location = f"{parts.scheme.lower()}://{user}{host}{port}{parts.path}"
    if query:
        location = f"{location}?{query}"
    return hashlib.sha256(location.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CachedPolicy:
    """A last-known-good document plus the provenance needed to reuse it."""

    body: bytes
    #: Digest of the source, not the source. See :func:`_source_digest`.
    source_digest: str
    fetched_at: float
    etag: str = ""
    last_modified: str = ""

    def age_secs(self) -> float:
        """Seconds since the fetch, floored at 0.

        A clock that stepped backwards between the write and the read would
        otherwise produce a negative age that reads as impossibly fresh — or, once
        compared against a staleness bound, as a reason to refuse to boot.
        """
        return max(0.0, time.time() - self.fetched_at)

    def meta(self) -> "CachedMeta":
        """This entry's provenance without its body, for a metadata-only rewrite."""
        return CachedMeta(
            source_digest=self.source_digest,
            fetched_at=self.fetched_at,
            etag=self.etag,
            last_modified=self.last_modified,
            digest=_body_digest(self.body),
        )


@dataclass(frozen=True)
class CachedMeta:
    """The cache's provenance, without its body.

    Split out because most readers only need this half.  ``distribution_posture``
    wants an age and a source and is repolled by every open dashboard; the
    source-mismatch and staleness pre-checks want the same two before deciding
    whether the body is worth reading at all.  Reading a document that may be up to
    ``MAX_POLICY_BYTES`` to answer "how old is it" is the kind of waste that only
    shows up once a viewer is left open.
    """

    #: Digest of the source, not the source. See :func:`_source_digest`.
    source_digest: str
    fetched_at: float
    etag: str = ""
    last_modified: str = ""
    #: Digest of the document this metadata was written WITH.  See :func:`read_cache`.
    digest: str = ""

    def age_secs(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def read_cache_meta() -> Optional[CachedMeta]:
    """The cached document's provenance, or ``None`` when there is no usable cache.

    Never raises, for the reason :func:`read_cache` gives.  Still requires the
    DOCUMENT to be present — via a ``stat``, not a read — because the two files must
    agree: metadata alone has nothing to serve, and honouring it would report a
    cached ceiling that is not there.
    """
    directory = cache_dir()
    try:
        if not (directory / _CACHE_DOC_LEAF).stat().st_size:
            return None
        meta_raw = read_bytes_with_retry(directory / _CACHE_META_LEAF).decode("utf-8")
    except (OSError, UnicodeError):
        return None
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        logger.warning("policy cache metadata is not valid JSON; ignoring the cached ceiling")
        return None
    if not isinstance(meta, dict):
        return None
    fetched_at = meta.get("fetched_at")
    # A cache written before the source was digested carries the plaintext instead. Hash
    # it on read rather than discarding the entry: an upgrade must not throw away the
    # last-known-good copy, and the next write replaces the file with the digest form.
    recorded = meta.get("source_digest")
    if not isinstance(recorded, str) or not recorded:
        legacy = meta.get("source")
        recorded = _source_digest(legacy) if isinstance(legacy, str) and legacy else ""
    if not recorded or not isinstance(fetched_at, (int, float)):
        logger.warning("policy cache metadata is incomplete; ignoring the cached ceiling")
        return None
    return CachedMeta(
        source_digest=recorded,
        fetched_at=float(fetched_at),
        etag=str(meta.get("etag") or ""),
        last_modified=str(meta.get("last_modified") or ""),
        digest=str(meta.get("digest") or ""),
    )


def read_cache() -> Optional[CachedPolicy]:
    """The cached document, or ``None`` when there is no usable one.

    Never raises.  A cache is an optimisation over a fetch and a fallback for an
    outage; a corrupt one is neither, so it reads as absent and the caller fetches.
    Both files must agree: a document with no metadata has no provenance (we could
    not say what source it came from, and honouring it would let a stray file
    become the ceiling), and metadata with no document has nothing to serve.

    Reads through ``atomic_write.read_bytes_with_retry`` — the read-side twin of the
    ``replace_with_retry`` the writes go through — because the refresher thread
    publishes this cache while boot, the loader tier and the browser-polled posture
    read it. On Windows a read during a rename raises ``PermissionError``, which here
    would read as "no cache" and, on a host with an unreachable source, abort boot
    under the fail-closed default.
    """
    meta = read_cache_meta()
    if meta is None:
        return None
    try:
        body = read_bytes_with_retry(cache_dir() / _CACHE_DOC_LEAF)
    except (OSError, UnicodeError):
        return None
    if not body:
        return None
    # The two files are written separately, so overlapping writers can leave a NEW
    # document beside OLD metadata. That pair is not merely stale, it is wrong: the
    # metadata's ``source`` is what the repoint rule trusts, so a torn pair could hand a
    # repointed host the retired endpoint's document with the new endpoint's name on it.
    # The digest is written WITH the document, so a mismatch proves the tear and the
    # cache reads as absent — the caller then fetches, which is the safe answer.
    if meta.digest and _body_digest(body) != meta.digest:
        logger.warning("the policy cache document and its metadata disagree; ignoring it")
        return None
    return CachedPolicy(
        body=body,
        source_digest=meta.source_digest,
        fetched_at=meta.fetched_at,
        etag=meta.etag,
        last_modified=meta.last_modified,
    )


def write_cache(
    body: bytes,
    *,
    source: str,
    etag: str = "",
    last_modified: str = "",
    now: Optional[float] = None,
    expect_pair: Optional[Tuple[str, str]] = None,
) -> bool:
    """Persist a fetched document as the new last-known-good copy.

    Written through the shared :func:`kiro_crew.atomic_write.atomic_write` (never a
    hand-rolled temp + rename, which is how earlier writers in this repo silently
    lost the Windows rename retry) and document-before-metadata, so a process killed
    mid-write leaves either the previous consistent pair or a new document with old
    metadata — never metadata promising a document that is not there.
    ``restrict_to_owner`` also brings ``atomic_write``'s refusal to write through a
    symlinked parent, which matters more here than for an ordinary file: the parent
    is a trust root, and a link swapped in over it would redirect the write.

    The directory itself is created owner-only and INHERITABLY so, which is the
    control that actually matters — it holds the ceiling and the recorded source, so
    an agent able to write either could publish itself a ceiling with provenance.

    **Must not be called from the event loop.** ``replace_with_retry`` deliberately
    skips its Windows sharing-violation retry when it detects a running loop, so a
    write from there would lose it. Every caller today is boot (synchronous) or the
    refresher thread.

    Best-effort by design: a host that cannot write the cache is still governed by
    what it just fetched.  It simply has no outage fallback, which is worth a
    warning and not worth refusing to run.

    *expect_pair* makes the publish a **compare-and-swap**, checked inside the lock: the
    write happens only if the cache still holds the ``(body digest, source digest)`` the
    caller observed. ``("", "")`` expects no cache at all, and ``None`` (the default)
    publishes unconditionally. The pair rather than the body alone, because identical bytes
    can be published for a DIFFERENT source and overwriting that provenance with this
    call's own is exactly what the repoint rule then refuses at the next boot. It exists because a fetch is slow and a refresh is not the only
    writer: a refresher that fetched v2 over a slow link, while ``policy fetch --force``
    published v3, would otherwise write v2 over v3 and then INSTALL v2 — the ceiling
    rolling backward to a looser document, which is the one direction this tier must
    never move on its own.

    Returns whether the document was published. ``False`` means either the swap found the
    cache moved on (the caller must not install what it fetched) or the write failed.
    """
    directory = _ensure_cache_dir()
    if directory is None:
        return False
    payload = _meta_payload(
        source_digest=_source_digest(source),
        etag=etag,
        last_modified=last_modified,
        digest=_body_digest(body),
        now=now,
    )
    try:
        with _cache_write_lock(directory):
            if expect_pair is not None:
                observed = read_cache_meta()
                on_disk = _cache_pair(observed)
                # The PAIR, not the body digest alone. Identical bytes can be published
                # for a DIFFERENT source (a repoint whose document did not change), and a
                # body-only comparison would let this call overwrite that provenance with
                # its own -- recording the old source against bytes another process
                # published for the new one, which the next boot's repoint rule then
                # discards as not belonging to the source it resolves.
                if on_disk != expect_pair:
                    logger.info(
                        "another writer published a different ceiling while this fetch was "
                        "in flight; not overwriting it"
                    )
                    return False
            for path, data in (
                (directory / _CACHE_DOC_LEAF, body),
                (directory / _CACHE_META_LEAF, payload),
            ):
                _write_cache_file(path, data)
    except Exception:
        logger.warning("could not cache the fetched security policy", exc_info=True)
        return False
    return True


def touch_cache(
    meta: CachedMeta, *, etag: str = "", last_modified: str = "", now: Optional[float] = None
) -> None:
    """Record that the cached document was re-validated as current.

    A 304 proves the cached bytes ARE the published document right now, so its age
    must restart — otherwise a fleet with a stable policy and a staleness bound
    would refuse to boot for having successfully confirmed nothing changed.

    Takes the *meta* the caller already read and rewrites **only**
    ``policy.meta.json``.  A 304 is the common outcome on a stable fleet policy, and
    re-reading the document just to write the same bytes back would spend a full
    fsync'd write of up to ``MAX_POLICY_BYTES`` per interval per host to change one
    timestamp.  The document on disk is already correct, so the document-before-
    metadata ordering has nothing to preserve here.

    **Compare-and-swap, not a blind write.**  The lock serialises the two writers, but
    *meta* was read before it was taken, so a forced ``write_cache`` landing in between
    leaves this call about to stamp the OLD digest over metadata describing NEW bytes.
    Readers then see a torn pair and discard the cache — the last-known-good copy lost at
    the moment it is needed, during the outage that made the refresh fail. So the current
    metadata is re-read INSIDE the lock and the touch is skipped unless it still describes
    the document this caller validated. Skipping is right rather than merely safe: the
    writer that got there first already recorded a fresher fetch of the same source, so
    there is no age to restart.
    """
    directory = _ensure_cache_dir()
    if directory is None:
        return
    payload = _meta_payload(
        source_digest=meta.source_digest,
        etag=etag or meta.etag,
        last_modified=last_modified or meta.last_modified,
        # Carried forward, not recomputed: this rewrite does not touch the document, so
        # the digest still describes the bytes on disk.
        digest=meta.digest,
        now=now,
    )
    try:
        with _cache_write_lock(directory):
            current = read_cache_meta()
            if current is not None and (
                current.digest != meta.digest or current.source_digest != meta.source_digest
            ):
                logger.info(
                    "another writer advanced the policy cache while this 304 was in "
                    "flight; leaving its metadata alone"
                )
                return
            _write_cache_file(directory / _CACHE_META_LEAF, payload)
    except Exception:
        logger.warning("could not re-validate the cached security policy", exc_info=True)


def _meta_payload(
    *,
    source_digest: str,
    etag: str,
    last_modified: str,
    digest: str,
    now: Optional[float] = None,
) -> bytes:
    """The metadata document. Records a DIGEST of the source, never the source itself.

    See :func:`_source_digest`: the URL can BE the credential, and this file is read by
    the app backend.
    """
    return json.dumps(
        {
            "source_digest": source_digest,
            "fetched_at": float(now if now is not None else time.time()),
            "etag": etag,
            "last_modified": last_modified,
            "digest": digest,
        },
        sort_keys=True,
    ).encode("utf-8")


@contextlib.contextmanager
def _cache_write_lock(directory: Path) -> Iterator[None]:
    """Serialise every mutation of the cached doc/meta PAIR, across processes.

    The pair is two files and each is written atomically, but the pair is not: a
    forced ``write_cache`` (new document, new digest) interleaving with another
    process's 304 ``touch_cache`` (metadata only, carrying the digest it read before
    the write) persists NEW bytes against an OLD digest. Readers detect that and
    discard the cache as torn, so the cost is the last-known-good copy vanishing at
    the moment it is most needed — during the outage that made the refresh fail. Two
    writers is the ordinary case, not a corner: the gateway's refresher thread polls
    on its interval while ``kirocrew policy fetch --force`` runs from a shell.

    Exclusive, and shared with nobody: readers are NOT locked. They already
    revalidate the digest and retry, so they degrade to one transient cache miss,
    and locking them would put a boot behind a mutex that a hung refresher holds.

    Raises rather than proceeding unlocked if the lock cannot be taken. Both callers
    treat a raise as "no new cache this round", which preserves the existing good
    copy; writing unlocked could destroy it, and skipping is what a caller unable to
    write the cache at all already does.
    """
    lock_path = directory / _CACHE_LOCK_LEAF
    # Same hardening as the lock file in ``memory.py``: never follow a link to it and
    # require a lone regular inode, so an agent-planted link cannot redirect the open
    # onto a file it wants truncated. No O_TRUNC — a lock file carries no content.
    if is_link_or_junction(lock_path):
        raise OSError(f"refusing cache lock file (link or junction): {lock_path}")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"refusing cache lock file (not a lone regular inode): {lock_path}")
        with file_lock(fd, exclusive=True, required=True):
            yield
    finally:
        os.close(fd)


def _write_cache_file(path: Path, data: bytes) -> None:
    atomic_write(
        path,
        data,
        fsync=True,
        restrict_to_owner=True,
        # The directory's inheritable owner-only mode is the real control; a per-file
        # tighten is defence in depth and must not discard a ceiling we fetched.
        restrict_on_error="warn",
    )


def _ensure_cache_dir() -> Optional[Path]:
    """The cache directory, created owner-only on first use.  ``None`` if it cannot be.

    Created and tightened at most once per process.  The tighten is what matters and
    it matters at CREATION: re-applying it on every write costs a ``chmod`` on POSIX
    and, on Windows, a DACL write — which, with a 304 being the common poll outcome,
    would be filesystem work per refresh interval (and an unbounded SMB round-trip on
    a network-homed data home) to re-assert a DACL that has not changed.
    """
    global _cache_dir_ready
    directory = cache_dir()
    if _cache_dir_ready == directory:
        return directory
    try:
        make_owner_only_dir(directory)
    except Exception:
        logger.warning("could not create the policy cache directory %s", directory, exc_info=True)
        return None
    # Keyed on the PATH, not a bare bool: a test (and a re-homed process) moves the
    # data home, and a bool would skip the tighten for the new directory.
    _cache_dir_ready = directory
    return directory


def reset_fetch_window() -> None:
    """Forget when this tier last fetched, so the next call may go out again.

    Called by :func:`reset_process_state`.  Deliberately does NOT clear the
    installed-document digest: forgetting when we last fetched does not un-install the
    ceiling that is running.
    """
    global _last_fetch_attempt
    with _FETCH_SLOT_LOCK:
        _last_fetch_attempt = 0.0


def reset_process_state() -> None:
    """Clear every process-global this module keeps.  Test helper.

    All four are invisible to a test that does not know they exist, and each leaks a
    different way, so they are reset together rather than one at a time:

    * the fetch window would suppress a later test's fetch;
    * the installed-document digest would make a later test's 304 compare against a
      document some earlier test installed, so a poll adopts a cache it should have
      reported as unchanged;
    * the cache-directory memo holds a path from a previous test's data home, so a
      write would skip the owner-only tighten for the new one;
    * a post-install hook registered by one test would run on every later test's refresh,
      reaching into whatever subsystem it was written for.
    """
    global _cache_dir_ready, _installed_body_digest, _handoff_digest
    reset_fetch_window()
    with _INSTALLED_LOCK:
        _installed_body_digest = ""
        _handoff_digest = ""
    _cache_dir_ready = None
    _POST_INSTALL_HOOKS.clear()


def _cache_pair(meta: Optional["CachedMeta"]) -> Tuple[str, str]:
    """The cache's identity for a compare-and-swap: ``(body digest, source digest)``.

    Both halves, because either can move independently: a new document under the same
    source, or the SAME bytes recorded against a new source after a repoint. A swap that
    compared only the body would silently overwrite the second case's provenance.
    ``("", "")`` means "no cache", which is a state a caller can legitimately expect.
    """
    if meta is None:
        return ("", "")
    return (meta.digest, meta.source_digest)


def _claim_fetch_slot(window_secs: int) -> bool:
    """Reserve the right to go to the network, or refuse because someone just did.

    Records the ATTEMPT, not the success: a source that is down would otherwise be
    retried by every caller, and the per-app-call path would turn a network outage
    into one timeout per app call.

    The first claim in a process always succeeds, so boot is never held back by this.
    Explicit refresh (``refresh_now``, and therefore the CLI and the background
    poller) does not come through here — an operator asking for a fetch gets one.
    """
    global _last_fetch_attempt
    now = time.time()
    with _FETCH_SLOT_LOCK:
        # A clock that stepped backwards would otherwise park the window in the
        # future and block fetching until real time caught up.
        if _last_fetch_attempt and 0 <= now - _last_fetch_attempt < window_secs:
            return False
        _last_fetch_attempt = now
        return True


def _installed_digest() -> str:
    """The digest of the document this process last installed, or ``""``.

    Process-local on purpose. It answers "is the ceiling in effect here the one those
    bytes describe", which no shared file can: the cache is written by several
    processes (gatewayd's reload, an app backend's boot, ``kirocrew policy fetch``)
    while only this one installed a ceiling.
    """
    with _INSTALLED_LOCK:
        return _installed_body_digest


def _record_installed(body: bytes) -> None:
    """Record *body* as the ceiling this process now runs under, and hand it off.

    The handoff is published here because every install path ends here once its ceiling
    is the one that will govern. It raises ``PlatformCompositionError`` when the handoff
    cannot be made to agree (see :func:`publish_handoff`), which at boot is a refusal.
    """
    global _installed_body_digest
    with _INSTALLED_LOCK:
        _installed_body_digest = _body_digest(body)
    publish_handoff(body)


# ──────────────────────────────────────────────────────────────────────────
# The ceiling handoff
# ──────────────────────────────────────────────────────────────────────────
#
# In an enterprise build every process enters the central tier, but a process inside the
# agent sandbox can reach neither the policy cache nor the Kiro sign-in: both are hidden
# from the agent, and the Kiro Crew MCP servers kiro-cli starts share the agent's view.
# So the gateway hands its installed ceiling over: the document's bytes, written to a leaf
# the sandbox seals read-only (``sandbox_leaves._CREW_READONLY_LEAVES``) and pre-creates
# empty, so nothing inside a sandbox can write or create it. A sandboxed child reads it
# and never fetches (``policy_distribution_boot._load_handoff``). The file carries only
# the document: no source, no sign-in, no credential.
#
# Readable inside the sandbox, as ``security_policy.json`` is: a child that enforces a
# ceiling must read it, and the agent's own file tools are still refused it
# (``security.paths_lists``).


def handoff_dir() -> Path:
    """The handoff directory, resolved lazily for the reason :func:`cache_dir` gives."""
    return config_dir() / HANDOFF_DIR_LEAF


def hands_off() -> bool:
    """Does this process publish the handoff? An unsandboxed enterprise process does."""
    from kiro_crew import sandbox_launch_mark

    return _is_enterprise() and not cache_only() and not sandbox_launch_mark.launched_in_sandbox()


def takes_handoff() -> bool:
    """Must this process take its ceiling from the handoff and never fetch?

    An enterprise process the sandbox launcher started. A cache-only child (an app
    backend) keeps its own mode, which the gateway chose for it.
    """
    from kiro_crew import sandbox_launch_mark

    return _is_enterprise() and not cache_only() and sandbox_launch_mark.launched_in_sandbox()


def read_handoff() -> Tuple[Optional[bytes], str]:
    """The handed-off document, or ``(None, why)``. Never raises.

    Refuses a link, a non-regular file, an oversize file, a wrapper of another version,
    and a document whose bytes do not match the digest written with them.
    """
    path = handoff_dir() / _HANDOFF_FILE_LEAF
    try:
        if is_link_or_junction(path):
            return None, "the handoff is a link"
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            return None, "the handoff is not a regular file"
        if st.st_size > _MAX_HANDOFF_BYTES:
            return None, "the handoff is too large"
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return None, "the gateway has not handed a ceiling over"
    except OSError as exc:
        return None, f"the handoff could not be read ({type(exc).__name__})"
    body, why = _parse_handoff(raw)
    return body, why


def _parse_handoff(raw: bytes) -> Tuple[Optional[bytes], str]:
    try:
        wrapper = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "the handoff is not valid JSON"
    if not isinstance(wrapper, dict) or wrapper.get("version") != _HANDOFF_VERSION:
        return None, "the handoff has an unknown format"
    policy, digest = wrapper.get("policy"), wrapper.get("sha256")
    if not isinstance(policy, str) or not isinstance(digest, str):
        return None, "the handoff has an unknown format"
    body = policy.encode("utf-8")
    if _body_digest(body) != digest:
        return None, "the handoff document does not match its digest"
    return body, ""


def _handoff_payload(body: bytes) -> bytes:
    return json.dumps(
        {
            "version": _HANDOFF_VERSION,
            "sha256": _body_digest(body),
            "policy": body.decode("utf-8"),
        },
        sort_keys=True,
    ).encode("utf-8")


def publish_handoff(body: bytes) -> None:
    """Make the handoff hold *body*, the ceiling this process just installed.

    No-op unless :func:`hands_off`, and once per document per process. Skipped when the
    cache holds a DIFFERENT document: another process has published a newer ceiling and
    hands that one off after its own install, so writing this one would roll the
    handoff back. Checked and written under the cache's write lock for that reason.

    Raises ``PlatformCompositionError`` when the write fails AND a different document
    stays in place, because sandboxed children would then run under a ceiling this host
    has replaced. A failed write that leaves no handoff is only logged: children
    then find none and refuse to start, which is the safe direction.
    """
    global _handoff_digest
    if not hands_off():
        return
    digest = _body_digest(body)
    with _INSTALLED_LOCK:
        if _handoff_digest == digest:
            return
    with _handoff_lock():
        if _cache_moved_past(digest):
            logger.info("the policy cache moved past this ceiling; leaving the handoff alone")
            return
        try:
            current, _ = read_handoff()
            if current is None or _body_digest(current) != digest:
                make_owner_only_dir(handoff_dir())
                _write_cache_file(handoff_dir() / _HANDOFF_FILE_LEAF, _handoff_payload(body))
        except Exception:
            logger.warning(
                "could not hand the installed ceiling to sandboxed children", exc_info=True
            )
            _withdraw_handoff(keep_digest=digest)
            return
    with _INSTALLED_LOCK:
        _handoff_digest = digest


def restore_handoff(previous: Optional[bytes], *, published: bytes) -> None:
    """Undo :func:`publish_handoff` of *published* after the install it preceded failed.

    Puts *previous* back, or removes the handoff when there was none. Under the same
    lock as the publish, and only while the handoff still holds *published* and the
    cache has not moved to a third document: a process that published that one hands
    it off itself, and a rollback here would regress it. Never raises: a refused
    refresh keeps running.
    """
    global _handoff_digest
    if not hands_off():
        return
    with _INSTALLED_LOCK:
        _handoff_digest = ""  # whatever happens below, the next install re-checks the file
    keep = _body_digest(previous) if previous is not None else ""
    try:
        with _handoff_lock():
            current, _ = read_handoff()
            if current != published:
                return
            # A third document only: a cache still holding the refused one (its own
            # rollback failed) must not keep that one handed off.
            meta = read_cache_meta()
            if meta is not None and meta.digest not in ("", keep, _body_digest(published)):
                return
            try:
                if previous is None:
                    (handoff_dir() / _HANDOFF_FILE_LEAF).unlink(missing_ok=True)
                else:
                    _write_cache_file(
                        handoff_dir() / _HANDOFF_FILE_LEAF, _handoff_payload(previous)
                    )
            except Exception:
                logger.warning("could not restore the previous ceiling handoff", exc_info=True)
                _withdraw_handoff(keep_digest=keep)
    except Exception:
        logger.error("sandboxed children may run under a ceiling this host refused", exc_info=True)


@contextlib.contextmanager
def _handoff_lock() -> Iterator[None]:
    """The cache's write lock, which every handoff mutation is made under.

    The same lock as the cache, so "the cache holds X" and "the handoff holds X" are
    judged and changed as one step across processes. Without a cache directory there is
    no lock to take, and nothing to race: no other process has published a ceiling.
    """
    directory = _ensure_cache_dir()
    if directory is None:
        yield
        return
    with _cache_write_lock(directory):
        yield


def _cache_moved_past(digest: str) -> bool:
    """Does the cache hold a document other than the one with *digest*?"""
    meta = read_cache_meta()
    return meta is not None and bool(meta.digest) and meta.digest != digest


def _withdraw_handoff(*, keep_digest: str) -> None:
    """Remove a handoff that does not hold *keep_digest*, or raise if it survives.

    Called with :func:`_handoff_lock` held.
    """
    from kiro_crew.platform.context import PlatformCompositionError

    path = handoff_dir() / _HANDOFF_FILE_LEAF
    current, _ = read_handoff()
    if current is not None and _body_digest(current) == keep_digest:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    if os.path.lexists(path):
        remaining, _ = read_handoff()
        if remaining is None or _body_digest(remaining) != keep_digest:
            raise PlatformCompositionError(
                "the installed ceiling could not be handed to sandboxed children and the "
                f"previous handoff could not be removed. Check that {handoff_dir()} is "
                "writable by the gateway."
            )


#: Callables run after a refresh INSTALLS a new ceiling, so a control that was
#: materialised from the old one can be re-derived. Registered by the layer that owns the
#: control -- this module must not learn about tailnet, dashboards or anything above it.
_POST_INSTALL_HOOKS: list[Callable[[], None]] = []
