"""Pure share configuration and authentication helpers.

The merged 8100 process (``chat2api.py``) uses these helpers in-process: the
share projection config parser, request validation, credential resolution,
``ShareGate`` admission and the persisted revoke-state reader/writer. The legacy
two-process server, its HMAC attribution marker and the signed revoke notice are
retired and no longer live here.
"""

import hashlib
import hmac
import itertools
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path

# The service runs this file as a script (``sys.path[0]`` is ``chat2api/``), and
# tests may load it by path; the package root is inserted here when missing so
# the bundled ``shared`` package resolves without a second copy.
_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from shared.responses_protocol import MESSAGES_FIELDS  # noqa: E402

def model_catalog_limit(path):
    """Accept only Harness's bounded catalog query, never arbitrary queries.

    The query is consumed locally; only canonical /v1/models reaches chat2api.
    Harness's Anthropic SDK appends ``beta=true`` and the catalog probe appends
    one ``limit``; both are recognized by name, never by position.
    """
    if path == "/v1/models":
        return None
    prefix = "/v1/models?"
    if not path.startswith(prefix):
        raise ValueError("not a shared catalog path")
    query = path[len(prefix):]
    if not query:
        raise ValueError("empty catalog query")
    limit = None
    seen = set()
    for part in query.split("&"):
        name, separator, value = part.partition("=")
        if not separator or not name or name in seen:
            raise ValueError("invalid catalog query")
        seen.add(name)
        if name == "beta" and value == "true":
            continue
        if name != "limit":
            raise ValueError("invalid catalog query")
        if not value.isascii() or not value.isdecimal() \
                or not 1 <= len(value) <= 4:
            raise ValueError("invalid catalog limit")
        limit = int(value)
        if not 1 <= limit <= 1000 or str(limit) != value:
            raise ValueError("invalid catalog limit")
    return limit


def messages_query(path):
    """Accept the SDK's beta query on Messages, never an arbitrary one."""
    if path in (MESSAGES_PATH, MESSAGES_PATH + "?beta=true"):
        return
    raise ValueError("not a shared Messages path")



CHAT_FIELDS = {"model", "messages", "stream", "stream_options", "temperature",
               "top_p", "max_tokens", "max_completion_tokens", "stop", "seed",
               "presence_penalty", "frequency_penalty", "tools", "tool_choice",
               "parallel_tool_calls", "response_format", "reasoning_effort", "store"}
MESSAGES_PATH = "/v1/messages"
LEGACY_KEY_MAX_CONCURRENCY = 1
LEGACY_SHARE_MAX_CONCURRENCY = 2

# A notice can only pass the timestamp window for this long, so an applied
# generation older than the window can never be needed again.
REVOKE_NOTICE_TS_WINDOW_S = 300
REVOKE_NOTICE_NONCE_CHARS = 32
REVOKE_NOTICE_SEEN_MAX = 20000
# Revoke state must survive a process restart: the replay registry and the
# per-Key applied generation are written to ONE bounded state file before the
# notice is ACKed. The file holds NO secret, NO plaintext Key and NO
# credential - only nonces (already public in the signed notice) and the stable
# key_id/generation/application time.
#
# The broker unit runs the 8100 child with ProtectSystem=strict and only
# /var/lib/um-share, /run/um-share and /var/log/um-share writable, so the
# release directory (/opt/um-share/current) is READ-ONLY. A state file under
# the release therefore cannot be created or replaced: the first revoke write
# fails, the load ACK carries no applied watermark and the management side keeps
# the security journal generation pending (a production Key delete answers 503).
# The default is the persistent state directory that StateDirectory=um-share
# already creates; CHAT2API_SHARE_REVOKE_STATE_FILE overrides it explicitly.
REVOKE_STATE_ENV = "CHAT2API_SHARE_REVOKE_STATE_FILE"
DEFAULT_REVOKE_STATE_DIR = Path("/var/lib/um-share")
DEFAULT_REVOKE_STATE_FILE = DEFAULT_REVOKE_STATE_DIR / "share-revoke.state"
# The pre-fix default lived INSIDE the release (read-only under the broker
# unit). It is never written again, and never modified or deleted: a file found
# there is adopted ONCE at the first initialization of the deployment default so
# an upgrade cannot silently drop the pending generations it still carries.
# The resolved path is the CURRENT release only; `_legacy_revoke_state_candidates`
# also lists the sibling release directories (/opt/um-share/releases/*), because an
# upgrade switches `current` to a NEW release whose own data directory is empty
# while the generation that must still be ACKed lives in the release that ran
# before the switch.
LEGACY_REVOKE_STATE_FILE = (Path(__file__).resolve().parents[1]
                            / "data" / "share-revoke.state")
REVOKE_LEGACY_ADOPTED_SUFFIX = ".legacy-adopted"
REVOKE_STATE_VERSION = 1
REVOKE_STATE_MAX_BYTES = 1024 * 1024
# Applied generations are evicted by TIME, never by insertion order. A notice
# can only pass the timestamp window for this long, so an entry older than the
# window can never be needed again; within the window the stored generation stays
# absolute.
REVOKE_STATE_GENERATIONS_TTL_S = REVOKE_NOTICE_TS_WINDOW_S
# The applied projection snapshot (key_id -> enabled plus the generation) is
# persisted beside the projection config, which in the default deployment is the
# writable /var/lib/um-share/run directory (not the read-only release). Only the
# current projection's Keys and a bounded number of recently removed Keys are
# retained, so the snapshot cannot grow with Key churn.
REVOKE_PROJECTION_VERSION = 1
REVOKE_PROJECTION_MAX_BYTES = 1024 * 1024
REVOKE_PROJECTION_KNOWN_MAX = 4096
# The sibling marker proves a trusted first initialization happened. It is tiny,
# private (0600) and carries the same schema version as the state.
REVOKE_INIT_MARKER_MAX_BYTES = 4096
_revoke_notice_seen = {}
_revoke_applied_generations = {}
# The newest APPLIED projection generation. Every revoke/rotate/limit_lower
# intent at or below it is applied because the projection rendered after the
# intent's own transaction carries the change; it is persisted so a restart ACKs
# the generation it already applied even before the next load.
_revoke_projection_generation = 0
_revoke_state_lock = threading.RLock()
_revoke_state_loaded = False
# A read-only merge found a genuinely absent state (no file, no initialization
# marker). Repeated status polls must not re-scan the release directories; the
# first real write clears this flag and initializes the state.
_revoke_state_absent = False


def revoke_state_path():
    """The persisted revoke state path: env override, else a writable state dir.

    The default never points inside the release directory: the broker unit keeps
    the release read-only (ProtectSystem=strict) and only /var/lib/um-share is
    writable.  A non-POSIX development checkout keeps the release-relative data
    directory, which is writable there and is never a deployment target.
    """
    override = os.environ.get(REVOKE_STATE_ENV)
    if override:
        return Path(override)
    if os.name == "posix":
        return DEFAULT_REVOKE_STATE_FILE
    return LEGACY_REVOKE_STATE_FILE


def revoke_projection_path(config_path):
    """The persisted projection snapshot path for ONE projection config.

    The snapshot is colocated with the projection config it describes; the
    default deployment passes --config
    /var/lib/um-share/run/share-projection.json, so the snapshot also lands
    under the writable state tree (never inside the read-only release).  Keying
    the name by the config path also keeps separate deployments (and test
    fixtures) from sharing a snapshot.
    """
    config = Path(config_path)
    return config.parent / (config.name + ".revoke-projection")


class RevokeStateUnavailable(ValueError):
    """A persisted revoke/projection state exists but cannot be trusted.

    Review #12: a missing file AFTER a trusted first initialization, a corrupt
    file, an unreadable file and a version mismatch all fail closed.  The
    in-memory caches are never replaced with an empty state and the existing
    file is never silently rewritten.  The message names the exact path and the
    recovery action so an operator can restore a backup or intentionally
    reinitialize.
    """


def _initialization_marker_path(path):
    """The sibling marker proving ``path`` was initialized once.

    The marker is deliberately separate from the state payload: a deleted or
    truncated state file can then be told apart from a real first start.  Both
    files are atomically replaced with the same private (0600) helper.
    """
    path = Path(path)
    return path.with_name(path.name + ".initialized")


def _read_initialization_marker(path):
    """True when the marker proves ``path`` was initialized; fail closed.

    A missing marker is the normal legacy/first-start case (False).  An
    unreadable, oversized, malformed or version-mismatched marker raises
    ``RevokeStateUnavailable``: it is evidence that initialization happened,
    so the caller must not treat the state as absent.
    """
    marker = _initialization_marker_path(path)
    try:
        raw = marker.read_bytes()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is unreadable ({exc}); restore "
            f"it from backup or remove {marker} to reinitialize") from exc
    if len(raw) > REVOKE_INIT_MARKER_MAX_BYTES:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is unexpectedly large; restore "
            f"it from backup or remove {marker} to reinitialize")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"initialization marker {marker} is corrupt; restore it from "
            f"backup or remove {marker} to reinitialize") from exc
    if (not isinstance(payload, dict)
            or payload.get("version") != REVOKE_STATE_VERSION):
        raise RevokeStateUnavailable(
            f"initialization marker {marker} has an unsupported version; "
            f"restore it from backup or remove {marker} to reinitialize")
    return True


def _write_initialization_marker(path):
    """Create the private marker AFTER the state itself was durably written."""
    marker = _initialization_marker_path(path)
    _write_private_json_atomic(
        marker, {"version": REVOKE_STATE_VERSION},
        REVOKE_INIT_MARKER_MAX_BYTES)


def _empty_revoke_state():
    return {"version": REVOKE_STATE_VERSION, "nonces": {},
            "generations": {}, "projection_generation": 0}


def _read_revoke_state(path):
    """Read and strictly validate ONE revoke state file.

    A missing file is reported as ``FileNotFoundError`` so the loader can CREATE
    the first state file with its schema version.  An unreadable, oversized,
    malformed or version-mismatched file raises ``ValueError`` so the caller
    fails closed instead of silently forgetting a previous revoke.
    """
    raw = path.read_bytes()
    if len(raw) > REVOKE_STATE_MAX_BYTES:
        raise ValueError("revoke state is unexpectedly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("revoke state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("revoke state is not an object")
    if payload.get("version") != REVOKE_STATE_VERSION:
        raise ValueError("revoke state version is unsupported")
    nonces = payload.get("nonces")
    generations = payload.get("generations")
    if not isinstance(nonces, dict) or not isinstance(generations, dict):
        raise ValueError("revoke state shape is invalid")
    # The applied projection generation is the ACK watermark for the
    # revoke/rotate/limit_lower intents that were applied BEFORE this state
    # file existed.  Old files predate it and read as 0.
    projection_generation = payload.get("projection_generation", 0)
    if (isinstance(projection_generation, bool)
            or not isinstance(projection_generation, int)
            or projection_generation < 0):
        raise ValueError("revoke state projection generation is invalid")
    clean_nonces = {}
    for nonce, ts in nonces.items():
        if (not isinstance(nonce, str) or not nonce
                or isinstance(ts, bool) or not isinstance(ts, int)):
            raise ValueError("revoke state nonce entry is invalid")
        clean_nonces[nonce] = ts
    clean_generations = {}
    for key_id, entry in generations.items():
        if not isinstance(key_id, str) or not key_id \
                or not isinstance(entry, dict):
            raise ValueError("revoke state generation entry is invalid")
        generation = entry.get("generation")
        applied_at = entry.get("applied_at")
        # ``updated_at`` is the eviction clock (review #14); old state files
        # predate it and fall back to ``applied_at``.
        updated_at = entry.get("updated_at", applied_at)
        if (isinstance(generation, bool) or not isinstance(generation, int)
                or generation < 0
                or isinstance(applied_at, bool)
                or not isinstance(applied_at, (int, float))
                or applied_at < 0
                or isinstance(updated_at, bool)
                or not isinstance(updated_at, (int, float))
                or updated_at < 0):
            raise ValueError("revoke state generation entry is invalid")
        clean_generations[key_id] = {
            "generation": generation, "applied_at": applied_at,
            "updated_at": updated_at}
    return {"version": REVOKE_STATE_VERSION, "nonces": clean_nonces,
            "generations": clean_generations,
            "projection_generation": projection_generation}


def _legacy_revoke_state_candidates(path, primary=None):
    """The pre-fix release-relative state files, when they are DIFFERENT files.

    The legacy files are a migration SOURCE only: they are read once at the first
    initialization of the deployment default, and they are never modified or deleted
    (the release may be read-only and the operator owns those files).

    The CURRENT release is ``<prefix>/releases/<v>`` and the pre-fix state lived in
    its own ``data/share-revoke.state``.  After a version switch that file belongs
    to the OLD release, so the siblings of the current release are searched too;
    otherwise the pending generation of the release that ran before the switch would
    never be adopted.  ``primary`` is a test seam; the module constant is used
    when it is omitted.
    """
    primary = Path(LEGACY_REVOKE_STATE_FILE if primary is None else primary)
    candidates = []
    try:
        if Path(path).resolve() == primary.resolve():
            return candidates
    except OSError:
        return candidates
    candidates.append(primary)
    # primary is <release>/data/share-revoke.state.  The release root's parent
    # holds the sibling release directories when `current` is a symlink into
    # <prefix>/releases/<v>; when `current` is a plain directory the releases
    # live one level deeper under <prefix>/releases/*.
    release_root = primary.parent.parent
    releases = [release_root.parent, release_root.parent / "releases"]
    siblings = []
    for root in releases:
        try:
            siblings.extend(sorted(root.glob("*/data/share-revoke.state")))
        except OSError:
            continue
    seen = set()
    try:
        seen.add(primary.resolve())
    except OSError:
        pass
    for sibling in siblings:
        try:
            if not sibling.is_file():
                continue
            resolved = sibling.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        candidates.append(sibling)
    return candidates


def _adopts_legacy_revoke_state(path):
    """True only for the deployment default; an override owns its own state.

    Adopting into an arbitrary override could resurrect one deployment's revoke
    watermarks in another.  The documented default (or an override naming it) is
    the ONE upgrade target, and the code reports an ignored legacy file for any
    other path instead of reading it silently.
    """
    try:
        return Path(path) == DEFAULT_REVOKE_STATE_FILE
    except TypeError:
        return False


def _merge_revoke_states(states):
    """Merge several adopted legacy states into ONE conservative state.

    The nonce registry keeps the newest timestamp seen for each nonce.  A per-Key
    applied generation keeps the NEWEST generation; when two files carry the same
    generation the newest application time wins, so the admission fence never moves
    backwards.  The applied projection watermark keeps the newest generation.
    """
    nonces = {}
    for state in states:
        for nonce, ts in state["nonces"].items():
            if ts > nonces.get(nonce, -1):
                nonces[nonce] = ts
    generations = {}
    for state in states:
        for key_id, entry in state["generations"].items():
            current = generations.get(key_id)
            if current is None or (
                    entry["generation"],
                    entry.get("applied_at", 0)) > (
                        current["generation"],
                        current.get("applied_at", 0)):
                generations[key_id] = entry
    projection_generation = max(
        (state.get("projection_generation", 0) for state in states), default=0)
    return {"version": REVOKE_STATE_VERSION, "nonces": nonces,
            "generations": generations,
            "projection_generation": projection_generation}


def _legacy_adoption_marker_path(path):
    """The sibling marker proving the pre-fix file was adopted ONCE.

    It keeps an intentional reinitialization (state and initialization marker
    removed) from resurrecting a stale release file on the next start.
    """
    path = Path(path)
    return path.with_name(path.name + REVOKE_LEGACY_ADOPTED_SUFFIX)


def _read_legacy_revoke_state(legacy):
    """Read the pre-fix state for ONE-TIME adoption; None when absent.

    A corrupt legacy file fails closed instead of being skipped: it may hold the
    only copy of a pending generation, so the operator must resolve it
    explicitly (restore it from backup or remove it to start empty).
    """
    try:
        return _read_revoke_state(legacy)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"legacy revoke state {legacy} is unreadable or corrupt ({exc}); "
            f"restore it from backup, or remove it to start empty") from exc


def _read_adopted_legacy_revoke_state(candidates):
    """Read and MERGE every present pre-fix state; None when none is present.

    A corrupt candidate fails closed instead of being skipped: it may hold the
    only copy of a pending generation, so the operator must resolve it
    explicitly (restore it from backup or remove it to start empty).
    """
    states = []
    adopted = []
    for legacy in candidates:
        state = _read_legacy_revoke_state(legacy)
        if state is not None:
            states.append(state)
            adopted.append(legacy)
    if not states:
        return None, []
    return _merge_revoke_states(states), adopted


def _report_ignored_legacy_revoke_state(path, candidates, reason):
    """Say ONCE that present pre-fix files are not the active state.

    The files are never modified or deleted, so no pending generation disappears
    silently: the operator can copy them deliberately or remove them.
    """
    for legacy in candidates:
        try:
            present = legacy.exists()
        except OSError:
            present = False
        if present:
            print(f"revoke state: the legacy file {legacy} is ignored "
                  f"({reason}); the active state is {path} and the legacy "
                  f"file is left untouched", file=sys.stderr, flush=True)


def _write_private_json_atomic(path, payload, max_bytes):
    """Atomically replace ONE private JSON file; failure leaves the old one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=True,
                       separators=(",", ":")).encode("ascii")
    if len(data) > max_bytes:
        raise ValueError("state payload is unexpectedly large")
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _write_revoke_state_payload(path, nonces, generations,
                                projection_generation=0):
    """Atomically replace ONE revoke state file with the given payload."""
    _write_private_json_atomic(path, {
        "version": REVOKE_STATE_VERSION,
        "nonces": nonces,
        "generations": generations,
        "projection_generation": projection_generation,
    }, REVOKE_STATE_MAX_BYTES)


def _write_revoke_state_locked():
    """Atomically replace the state file; a failed write leaves the old one."""
    _write_revoke_state_payload(
        revoke_state_path(), _revoke_notice_seen, _revoke_applied_generations,
        _revoke_projection_generation)


def _read_projection_snapshot(path):
    """Read and strictly validate ONE projection snapshot file.

    A missing snapshot is the normal first start (None).  An unreadable,
    oversized, malformed or version-mismatched file raises ``ValueError`` so the
    caller records a fault and keeps the config baseline instead of trusting a
    half-written snapshot.
    """
    raw = path.read_bytes()
    if len(raw) > REVOKE_PROJECTION_MAX_BYTES:
        raise ValueError("revoke projection state is unexpectedly large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("revoke projection state is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("revoke projection state is not an object")
    if payload.get("version") != REVOKE_PROJECTION_VERSION:
        raise ValueError("revoke projection state version is unsupported")
    generation = payload.get("generation")
    keys = payload.get("keys")
    if (isinstance(generation, bool) or not isinstance(generation, int)
            or generation < 0 or not isinstance(keys, dict)):
        raise ValueError("revoke projection state shape is invalid")
    clean_keys = {}
    for key_id, enabled in keys.items():
        if not isinstance(key_id, str) or not key_id \
                or not isinstance(enabled, bool):
            raise ValueError("revoke projection state key entry is invalid")
        clean_keys[key_id] = enabled
    return {"generation": generation, "keys": clean_keys}


def _write_projection_snapshot(path, generation, known_enabled):
    _write_private_json_atomic(path, {
        "version": REVOKE_PROJECTION_VERSION,
        "generation": generation,
        "keys": dict(known_enabled),
    }, REVOKE_PROJECTION_MAX_BYTES)


def _prune_revoke_generations_locked(now=None):
    """Drop applied generations whose notice window has fully expired.

    A notice outside ``REVOKE_NOTICE_TS_WINDOW_S`` is rejected before it can
    reach this state, so an entry older than that window can never be needed
    again.  Within the window every stored generation stays absolute.
    """
    now = time.time() if now is None else now
    cutoff = now - REVOKE_STATE_GENERATIONS_TTL_S
    for key_id, entry in list(_revoke_applied_generations.items()):
        updated_at = (entry.get("updated_at", entry.get("applied_at", 0))
                      if isinstance(entry, dict) else 0)
        if isinstance(updated_at, bool) \
                or not isinstance(updated_at, (int, float)):
            updated_at = 0
        if updated_at < cutoff:
            _revoke_applied_generations.pop(key_id, None)


def _bounded_projection_known(current_keys, old_known, limit):
    """Current projection Keys plus the most recently removed ones.

    Every Key present in the current projection stays known: a later
    disable/delete/re-add must still be detected.  Removed Keys are retained
    only up to the remaining budget, newest first, so Key churn cannot grow the
    set without bound.
    """
    current = OrderedDict()
    for key_id in current_keys:
        if key_id not in current:
            current[key_id] = None
    budget = max(0, limit - len(current))
    removed = [key_id for key_id in old_known if key_id not in current]
    kept = removed[-budget:] if budget else []
    known = OrderedDict((key_id, None) for key_id in kept)
    known.update(current)
    return known


def _load_revoke_state_locked(create=True):
    """Load the persisted state into the in-process caches exactly once.

    Review #12: the FIRST trusted initialization creates the state file and its
    sibling marker.  After that, a missing state file, a corrupt file, an
    unreadable file or a version mismatch fails closed: the in-memory caches are
    left untouched (never replaced with an empty state) and the error names the
    path plus the recovery action.  A legacy state file without a marker (written
    before this marker existed) is adopted and marked, so an existing deployment is
    never locked out.  A failed first initialization is not marked, so a later
    start retries it instead of locking the installation forever.

    ``create=False`` is the read-only startup/ACK merge: a genuinely absent
    state (no file AND no initialization marker) is left for the first real
    write instead of being created by a status read.  Everything else (an
    initialized-but-missing file, a corrupt file, a version mismatch) still
    fails closed.

    Upgrade path: the pre-fix default lived inside the release, which the broker
    unit mounts read-only.  The deployment default therefore adopts every
    release-relative file (the current release plus its sibling release
    directories) ONCE at its first initialization, merging them; a sibling
    adoption marker keeps an intentional reinitialization from resurrecting them,
    and the legacy files themselves are never modified or deleted.  An explicit
    override for another location owns its own state and never adopts; present
    legacy files are reported as ignored there so no pending generation
    disappears silently.
    """
    global _revoke_state_loaded, _revoke_projection_generation, \
        _revoke_state_absent
    if _revoke_state_loaded or (not create and _revoke_state_absent):
        return
    path = revoke_state_path()
    marker = _initialization_marker_path(path)
    legacy_candidates = _legacy_revoke_state_candidates(path)
    adopts_legacy = (bool(legacy_candidates)
                     and _adopts_legacy_revoke_state(path))
    try:
        state = _read_revoke_state(path)
    except FileNotFoundError:
        if _read_initialization_marker(path):
            raise RevokeStateUnavailable(
                f"revoke state {path} is missing after initialization; "
                f"restore it from backup, or remove {marker} to reinitialize")
        if not create:
            # First start / read-only merge: there is no state to merge yet.
            _revoke_state_absent = True
            return
        adopted = None
        adopted_from = []
        if adopts_legacy and not _legacy_adoption_marker_path(path).exists():
            adopted, adopted_from = _read_adopted_legacy_revoke_state(
                legacy_candidates)
        # First start: write the state and THEN its marker.  A failed creation
        # fails closed; it is never treated as an empty state, and without the
        # marker the next start retries the first initialization.
        try:
            if adopted is None:
                _write_revoke_state_locked()
            else:
                _write_revoke_state_payload(
                    path, adopted["nonces"], adopted["generations"],
                    adopted.get("projection_generation", 0))
            _write_initialization_marker(path)
        except (OSError, ValueError) as exc:
            raise RevokeStateUnavailable(
                f"revoke state {path} could not be initialized ({exc}); fix "
                f"the data directory permissions and retry") from exc
        if adopted is not None:
            try:
                _write_private_json_atomic(
                    _legacy_adoption_marker_path(path),
                    {"version": REVOKE_STATE_VERSION},
                    REVOKE_INIT_MARKER_MAX_BYTES)
            except (OSError, ValueError) as exc:
                # The adopted state is already durable and marked; only the
                # one-time guard is missing.  Never fail a valid start here.
                print(f"revoke state: adopted {adopted_from} but could not "
                      f"write its adoption marker ({exc})", file=sys.stderr,
                      flush=True)
            print(f"revoke state adopted from {adopted_from} into {path}",
                  file=sys.stderr, flush=True)
            state = adopted
        else:
            if legacy_candidates:
                _report_ignored_legacy_revoke_state(
                    path, legacy_candidates,
                    "already adopted" if adopts_legacy
                    else "the path is not the deployment default")
            state = _empty_revoke_state()
    except (OSError, ValueError) as exc:
        raise RevokeStateUnavailable(
            f"revoke state {path} is unreadable or corrupt ({exc}); restore "
            f"it from backup, or remove {path} and {marker} to "
            f"reinitialize") from exc
    else:
        if not _read_initialization_marker(path):
            # A state file from before the marker existed: adopt it, then mark
            # it.  If the marker cannot be written the state is NOT accepted.
            try:
                _write_initialization_marker(path)
            except (OSError, ValueError) as exc:
                raise RevokeStateUnavailable(
                    f"revoke state {path} was adopted but its marker could "
                    f"not be written ({exc}); fix the data directory "
                    f"permissions and retry") from exc
        if legacy_candidates:
            # The configured state is authoritative after the one-time upgrade
            # adoption.  Say so instead of silently ignoring the old files; the
            # operator may delete them once the new state is confirmed.
            _report_ignored_legacy_revoke_state(
                path, legacy_candidates, "the configured state already exists")
    latest = max(state["nonces"].values(), default=0)
    horizon = latest - 2 * REVOKE_NOTICE_TS_WINDOW_S
    _revoke_notice_seen.clear()
    _revoke_notice_seen.update({
        nonce: ts for nonce, ts in state["nonces"].items() if ts >= horizon})
    _revoke_applied_generations.clear()
    _revoke_applied_generations.update(state["generations"])
    _revoke_projection_generation = state.get("projection_generation", 0)
    _prune_revoke_generations_locked()
    _revoke_state_absent = False
    _revoke_state_loaded = True


def load_revoke_state(create=True):
    """Return the persisted revoke state (nonces and per-Key generations).

    ``create=False`` merges an existing state without initializing one: a
    genuinely absent state is left absent for the first real write, while a
    missing-after-initialization or corrupt state still fails closed.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked(create=create)
        return {
            "nonces": dict(_revoke_notice_seen),
            "generations": {
                key_id: (dict(entry) if isinstance(entry, dict) else entry)
                for key_id, entry in _revoke_applied_generations.items()},
        }


def load_revoke_generation(key_id):
    """The persisted applied revoke generation for ONE Key, or None."""
    with _revoke_state_lock:
        _load_revoke_state_locked()
        entry = _revoke_applied_generations.get(key_id)
        return dict(entry) if entry is not None else None


def record_revoke_nonce(nonce, ts):
    """Remember ONE verified notice nonce; False when it was already seen.

    The check and the write happen under ONE lock and the state file is replaced
    atomically, so a replay after a process restart is still refused.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked()
        if nonce in _revoke_notice_seen:
            return False
        previous = dict(_revoke_notice_seen)
        _revoke_notice_seen[nonce] = ts
        latest = max(_revoke_notice_seen.values(), default=ts)
        horizon = latest - 2 * REVOKE_NOTICE_TS_WINDOW_S
        for old in [item for item, seen_ts in _revoke_notice_seen.items()
                    if seen_ts < horizon]:
            _revoke_notice_seen.pop(old, None)
        while len(_revoke_notice_seen) > REVOKE_NOTICE_SEEN_MAX:
            _revoke_notice_seen.pop(next(iter(_revoke_notice_seen)))
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_notice_seen.clear()
            _revoke_notice_seen.update(previous)
            raise
        return True


def record_revoke_generation(key_id, generation, applied_at):
    """Persist ONE applied revoke generation for a Key.

    The entry is kept across restarts until its notice window expires; within
    the window the stored generation stays absolute, so a late older revoke can
    never be treated as new.  ``updated_at`` is the eviction clock and is
    deliberately separate from ``applied_at``, which a re-enable preserves as
    the admission boundary.  A newer request's attribution timestamp
    (strictly after ``applied_at``) can still be admitted, so a Key that is
    later re-enabled does not stay blocked forever.
    """
    with _revoke_state_lock:
        _load_revoke_state_locked()
        previous = dict(_revoke_applied_generations)
        now = time.time()
        _revoke_applied_generations[key_id] = {
            "generation": generation, "applied_at": applied_at,
            "updated_at": now}
        _prune_revoke_generations_locked(now)
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_applied_generations.clear()
            _revoke_applied_generations.update(previous)
            raise


def record_projection_generation(generation):
    """Persist the newest APPLIED projection generation as the ACK watermark.

    The management store records a revoke/rotate/limit_lower intent in the SAME
    transaction that changes the Key and bumps ``config_generation``; the
    projection rendered after that commit therefore CARRIES the intent at that
    generation.  Applying projection generation N is the evidence that every
    security intent at or below N is applied - including ``rotate`` and
    ``limit_lower``, which have no per-Key revoke entry because they leave the
    Key enabled.  The watermark is monotonic: a repeated or older generation is
    a no-op, and a failed write leaves the previous value.

    Only an APPLIED generation is recorded (the caller passes the generation it
    installed or adopted from the persisted projection snapshot), so this never
    turns "no generation" into a confirmation.
    """
    if isinstance(generation, bool) or not isinstance(generation, int) \
            or generation < 0:
        raise ValueError("projection generation must be a non-negative integer")
    global _revoke_projection_generation
    with _revoke_state_lock:
        _load_revoke_state_locked()
        if generation <= _revoke_projection_generation:
            return False
        previous = _revoke_projection_generation
        _revoke_projection_generation = generation
        try:
            _write_revoke_state_locked()
        except (OSError, ValueError):
            _revoke_projection_generation = previous
            raise
        return True


def applied_security_generation():
    """The newest APPLIED security generation across every Key, as ONE int.

    The value MERGES the persisted revoke state (loaded on demand, so a restart
    that has not seen a notice yet still reports the generations it already
    applied) with the applied projection watermark written by
    ``record_projection_generation``.  A per-Key entry written by
    ``record_revoke_generation`` (generation/applied_at/updated_at) and a legacy
    plain int are both accepted.  The result is always an integer watermark (0
    when nothing was applied): a per-Key dict must NEVER reach ``max`` - two of
    them raise TypeError, which the instance channel would answer as
    ``internal_error`` instead of the load ACK.  A malformed entry is ignored
    rather than crashing the ACK, and an unreadable state file falls back to
    the in-memory snapshot (fail-closed: the ACK then reports only what this
    process already confirmed, never an invented watermark).
    """
    with _revoke_state_lock:
        try:
            _load_revoke_state_locked(create=False)
        except (OSError, ValueError):
            pass
        newest = _revoke_projection_generation
        for entry in list(_revoke_applied_generations.values()):
            generation = (entry.get("generation")
                          if isinstance(entry, dict) else entry)
            if isinstance(generation, bool) or not isinstance(generation, int) \
                    or generation < 0:
                continue
            newest = max(newest, generation)
        return newest


def reset_revoke_state_cache():
    """Forget the in-process cache (tests); the state file is untouched."""
    global _revoke_state_loaded, _revoke_projection_generation, \
        _revoke_state_absent
    with _revoke_state_lock:
        _revoke_state_loaded = False
        _revoke_state_absent = False
        _revoke_projection_generation = 0
        _revoke_notice_seen.clear()
        _revoke_applied_generations.clear()



_INFLIGHT_SEQUENCE = itertools.count(1)


class _InflightCtx:
    """One live share request's client-socket handle (R8-A7).

    ``terminate`` closes the socket ONCE (shutdown then close, both
    guarded); the owning handler observes the closure and unwinds through
    its own ``finally`` — the counter decrement happens exactly there, so
    termination and release never double-account.  A revoke always closes the
    client socket and the upstream connection, and never sends a second HTTP
    status after the response started."""

    def __init__(self, connection):
        self._connection = connection
        self._closed = False
        self._response_started = False
        self._forwarded = False
        self._socket_closed = False
        # D18-3: the live upstream connection this request is blocked on.
        # A revoke closes it so a blocked ``upstream.getresponse()`` (or a
        # streaming read) unwinds immediately instead of holding the 8100
        # in-flight slot until the upstream silence budget expires.
        self._upstream = None
        self._upstream_closed = False
        # One lock serializes attach/revoke/terminate/close so a revoke that
        # races the upstream creation cannot leave a live connection behind.
        self._state_lock = threading.Lock()
        self.sequence = next(_INFLIGHT_SEQUENCE)

    @property
    def terminated(self):
        """True once a revoke terminated this live request.

        The relay checks it immediately before forwarding upstream: a request
        that 8100 admitted before the revoke but had not yet sent must not
        register a new ticket or reach the UM after the notice.
        """
        return self._closed

    @property
    def forwarded(self):
        """True once the request reached the 8100 gateway."""
        return self._forwarded

    @property
    def socket_closed(self):
        return self._socket_closed

    @property
    def upstream_closed(self):
        """True once this request's upstream connection was closed."""
        return self._upstream_closed

    def attach_upstream(self, upstream):
        """Attach the live upstream connection for this request.

        Returns True when the connection was attached; False when a revoke
        already terminated this context (the connection is closed immediately
        so a request that raced the revoke never leaves it live).
        """
        with self._state_lock:
            if self._closed:
                close_now = True
            else:
                self._upstream = upstream
                close_now = False
        if close_now:
            try:
                upstream.close()
            except OSError:
                pass
            with self._state_lock:
                self._upstream_closed = True
            return False
        return True

    def close_upstream(self):
        """Close the attached upstream connection ONCE; True when closed.

        ``HTTPConnection.close`` closes the socket; a reader blocked in
        ``getresponse()``/``read1`` observes the closure and unwinds instead
        of holding the 8100 in-flight slot until the silence budget expires.
        """
        with self._state_lock:
            upstream = self._upstream
            self._upstream = None
            if upstream is None:
                return False
            self._upstream_closed = True
        try:
            upstream.close()
        except OSError:
            pass
        return True

    def mark_response_started(self):
        """Record that response headers went out (no second status after)."""
        self._response_started = True

    def mark_forwarded(self):
        """Record that the request reached the 8100 gateway."""
        self._forwarded = True

    def revoke(self):
        """Terminate for a revoke; return True when this context was live.

        A revoke closes BOTH the client socket and the upstream connection, so
        a blocked ``getresponse()`` unwinds immediately, and never sends a
        second HTTP status after the response started.
        """
        with self._state_lock:
            if self._closed:
                return False
            self._closed = True
        self._close_socket()
        self.close_upstream()
        return True

    def terminate(self):
        """Original cancel: close the socket and upstream once, never send a
        status."""
        with self._state_lock:
            if self._closed:
                return False
            self._closed = True
        self._close_socket()
        self.close_upstream()
        return True

    def close_socket(self):
        """Close the client socket once (used by the released-count rule)."""
        if self._socket_closed:
            return False
        self._socket_closed = True
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._connection.close()
        except OSError:
            pass
        return True

    def _close_socket(self):
        return self.close_socket()




def _validate_digest(value):
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("Invalid digest")
    int(value, 16)


def load_config(path, auth_mode=None):
    """Load and validate the share authorization file.

    ``auth_mode`` is the DEPLOYMENT mode fixed at instance start (R35-2);
    the deployment path always passes it explicitly (the guard's
    ``--auth-mode``) and it is never mutated after construction:

    * ``"projection"``: SQLite is the single authority; the legacy
      ``{"clients": ...}`` digest format is rejected at EVERY config read
      (a runtime swap of the derived file to legacy clients can never be
      consumed as authorization), and the projection branch delegates FULL
      parsing to the ONE shared parser ``parse_projection_payload`` (R31-6:
      no consumer more lenient than the control-end ACK).
    * ``"legacy"``: the explicit not-yet-migrated digest config; a
      projection file is rejected so the mode - not the file - decides the
      format.
    * ``None``: historical auto-detection for direct offline callers
      (migration previews and offline tests); the deployment never relies
      on it.

    Legacy entries get ``key_id == name`` (no stable UUID in the old format),
    the legacy effective limits (per-key max_concurrency 1, global
    share_max_concurrency 2 = the old hardcodes) and generation 0. Projection
    entries carry max_concurrency and the payload may carry
    share_max_concurrency; schema 1 projections keep the legacy defaults for
    missing fields, schema 2 uses the real values. Unknown projection schema
    versions are rejected, never downgraded to the legacy/open interpretation.

    Returns a normalized dict with ``entries``
    (key_id, name, secret_hash, secret_version, enabled, max_concurrency),
    ``config_generation``, ``share_max_concurrency`` and ``schema_version``.
    """
    if auth_mode not in (None, "projection", "legacy"):
        raise ValueError("Invalid auth mode")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Invalid sharing configuration")

    projection_markers = {"schema_version", "config_generation", "data"}
    if "clients" in data:
        if auth_mode == "projection":
            # R35-2: a legacy swap of the derived file is refused under
            # projection mode at EVERY auth read - never downgraded.
            raise ValueError("Legacy client configuration is not allowed "
                             "in projection mode")
        # Formats are mutually exclusive. A mixed object that carries unknown
        # schema/data together with clients must never be downgraded to legacy.
        if projection_markers & set(data.keys()):
            raise ValueError("Mixed sharing configuration format")
        if set(data.keys()) != {"clients"}:
            raise ValueError("Legacy configuration has unknown fields")
        legacy = data.get("clients")
        if not isinstance(legacy, dict):
            raise ValueError("Invalid sharing configuration")
        entries = []
        for name, digest in legacy.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Invalid client name")
            _validate_digest(digest)
            entries.append({"key_id": name, "name": name, "secret_hash": digest,
                            "secret_version": 1, "enabled": True,
                            "max_concurrency": LEGACY_KEY_MAX_CONCURRENCY})
        return {"schema_version": 1, "config_generation": 0, "legacy": True,
                "share_max_concurrency": LEGACY_SHARE_MAX_CONCURRENCY,
                "entries": entries}

    if projection_markers & set(data.keys()):
        if auth_mode == "legacy":
            raise ValueError("Projection configuration is not allowed in "
                             "legacy mode")
        # R35-2: the projection branch reuses the ONE shared pure parser (the
        # same verdict as the writer pre-check and the control-end ACK); a
        # missing protocol module fails CLOSED instead of parsing locally.
        try:
            from share.broker_protocol import (ProtocolError,
                                               parse_projection_payload)
        except ImportError as exc:
            raise ValueError("Shared projection parser unavailable") from exc
        try:
            parsed = parse_projection_payload(data)
        except ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        # The parser normalizes entries (max_concurrency always present) and
        # fills the legacy limits for missing fields, so schema 1 keeps the
        # old behavior and schema 2 carries the real values through.
        entries = [{"key_id": item["key_id"], "name": item["name"],
                    "secret_hash": item["secret_hash"],
                    "secret_version": item["secret_version"],
                    "enabled": item["enabled"],
                    "max_concurrency": item["max_concurrency"]}
                   for item in parsed["data"]]
        return {"schema_version": parsed["schema_version"],
                "config_generation": parsed["config_generation"],
                "share_max_concurrency": parsed["share_max_concurrency"],
                "legacy": False, "entries": entries}
    raise ValueError("Unsupported sharing configuration")


def valid_chat_request(data):
    """Shape-only admission for one shared Chat Completions body."""
    if not isinstance(data, dict) or set(data) - CHAT_FIELDS:
        return False
    if not isinstance(data.get("model"), str) or not data["model"]:
        return False
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    if not isinstance(data.get("stream", False), bool):
        return False
    return "store" not in data or data["store"] is False


def valid_messages_request(data):
    """Decide whether an Anthropic Messages body may enter the shared relay.

    Shape-only admission, never a rewrite: an accepted body is forwarded
    unchanged, so this must not add, drop or normalize fields.

    * Unknown fields are refused (the share exposes Messages, not the whole
      Anthropic surface: files/batches/count_tokens stay unshared).
    * ``max_tokens`` is REQUIRED and must be a positive integer - Messages has
      no default, and the share must not invent one: filling it in would
      silently change the caller's request and its cost.
    * ``stream`` is optional but must be a bool when present.

    ``bool`` is an ``int`` subclass in Python, so it is excluded explicitly:
    ``max_tokens: true`` is not a positive integer.
    """
    if not isinstance(data, dict) or set(data) - MESSAGES_FIELDS:
        return False
    if not isinstance(data.get("model"), str) or not data["model"]:
        return False
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    max_tokens = data.get("max_tokens")
    if (not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
            or max_tokens <= 0):
        return False
    return isinstance(data.get("stream", False), bool)


# The two accepted credential shapes and their shared bound. The bound applies to
# the raw header, exactly as the original Bearer-only rule did: a credential
# header longer than this is refused before any digest work.
BEARER_PREFIX = "Bearer "
API_KEY_HEADER = "X-Api-Key"
MAX_CREDENTIAL_HEADER = 256


def resolve_share_credential(headers, allow_api_key):
    """Resolve the ONE share credential from the accepted header shapes.

    ``Authorization: Bearer <key>`` is the original contract and stays valid on
    every shared route (Claude Code's ``ANTHROPIC_AUTH_TOKEN``, OpenAI-compatible
    clients). ``X-Api-Key: <key>`` is the Anthropic SDK's default ``api_key``
    shape; it is accepted on Messages and model discovery (``allow_api_key``).
    Chat Completions keeps its Bearer-only credential contract.

    Both shapes yield the SAME credential string, so the caller's digest lookup,
    enabled check, concurrency budget and ledger entry stay single - this adds a
    second header shape, never a second authorization path.

    Fail-closed, returning None (never a partial credential) when: a same-name
    header repeats, the credential is empty, the raw header exceeds
    ``MAX_CREDENTIAL_HEADER``, the Bearer prefix is malformed, or both shapes are
    present with DIFFERENT values. Two different credentials in one request are
    never resolved to either one of them.
    """
    auth = headers.get_all("Authorization", [])
    api_keys = headers.get_all(API_KEY_HEADER, []) if allow_api_key else []
    if len(auth) > 1 or len(api_keys) > 1:
        return None
    bearer = None
    if auth:
        header = auth[0]
        if not header.startswith(BEARER_PREFIX) or len(header) > MAX_CREDENTIAL_HEADER:
            return None
        bearer = header[len(BEARER_PREFIX):]
        if not bearer:
            return None
    api_key = None
    if api_keys:
        api_key = api_keys[0]
        if not api_key or len(api_key) > MAX_CREDENTIAL_HEADER:
            return None
    if bearer is None:
        return api_key
    if api_key is None:
        return bearer
    # Both shapes present: they must name the same key. ``compare_digest`` keeps
    # the comparison constant-time, like the digest lookup below; bytes avoid its
    # TypeError on non-ASCII values (those simply never match a real key).
    if not hmac.compare_digest(bearer.encode(), api_key.encode()):
        return None
    return bearer


class ShareGate:
    """In-process share-key authentication and admission gate.

    The merged 8100 process authenticates the share credential here and then
    serves the request through the normal chat2api handler. There is no second
    listener, no loopback forward and no HMAC marker: the key_id installed on
    the request is the one this gate authenticated.
    """

    def __init__(self, config_path, auth_mode=None):
        self.config_path = Path(config_path)
        self.auth_mode = auth_mode
        self.guard = threading.Lock()
        self.state = "running"      # running | draining | stopped
        self.active = {}
        self.history = {}
        self.inflight = {}
        self._cfg = load_config(self.config_path, auth_mode=auth_mode)
        self.share_max_concurrency = self._cfg["share_max_concurrency"]
        self.key_limits = {e["key_id"]: e["max_concurrency"]
                           for e in self._cfg["entries"]}
        self.config_generation = self._cfg["config_generation"]

    def snapshot(self):
        with self.guard:
            return {
                "state": self.state,
                "config_generation": self.config_generation,
                "share_max_concurrency": self.share_max_concurrency,
                "active": dict(self.active),
                "keys": len(self.key_limits),
            }

    def set_state(self, state):
        with self.guard:
            self.state = state

    def authenticate(self, headers, allow_api_key):
        """Resolve one enabled share entry, or None (fail-closed)."""
        credential = resolve_share_credential(headers, allow_api_key)
        if credential is None:
            return None
        try:
            cfg = load_config(self.config_path, auth_mode=self.auth_mode)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        digest = hashlib.sha256(credential.encode()).hexdigest()
        entry = next((e for e in cfg["entries"]
                      if e["enabled"]
                      and hmac.compare_digest(e["secret_hash"], digest)), None)
        if entry is None:
            return None
        with self.guard:
            self._cfg = cfg
            self.share_max_concurrency = cfg["share_max_concurrency"]
            self.key_limits = {e["key_id"]: e["max_concurrency"]
                               for e in cfg["entries"]}
            self.config_generation = cfg["config_generation"]
        return entry, cfg

    def acquire(self, key_id, generation=None, secret_version=None,
                reason_out=None):
        with self.guard:
            if self.state != "running":
                if reason_out is not None:
                    reason_out["reason"] = "state_not_accepting"
                    reason_out["detail"] = (
                        "the share is draining or stopped and is not "
                        "accepting new requests")
                return False
            cfg = self._cfg
            if generation is not None or secret_version is not None:
                try:
                    fresh = load_config(self.config_path,
                                        auth_mode=self.auth_mode)
                except (OSError, ValueError, KeyError, TypeError):
                    return False
                if generation is not None                         and fresh["config_generation"] < generation:
                    return False
                entry = next((e for e in fresh["entries"]
                              if e["key_id"] == key_id), None)
                if entry is None or not entry["enabled"]:
                    return False
                if secret_version is not None                         and entry["secret_version"] != secret_version:
                    return False
                key_limit = entry["max_concurrency"]
                share_max = fresh["share_max_concurrency"]
                cfg = fresh
            else:
                key_limit = self.key_limits.get(
                    key_id, LEGACY_KEY_MAX_CONCURRENCY)
                share_max = self.share_max_concurrency
            now = time.monotonic()
            self.history = {k: v for k, v in self.history.items()
                            if v and v[-1] > now - 60}
            recent = self.history.setdefault(key_id, deque())
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if self.active.get(key_id, 0) >= key_limit:
                if reason_out is not None:
                    reason_out["reason"] = "per_key_concurrency"
                    reason_out["detail"] = (
                        "per-key concurrency limit reached "
                        "(max %d in-flight requests for this key)" % key_limit)
                return False
            if sum(self.active.values()) >= share_max:
                if reason_out is not None:
                    reason_out["reason"] = "share_concurrency"
                    reason_out["detail"] = (
                        "global share concurrency limit reached "
                        "(max %d in-flight requests)" % share_max)
                return False
            if len(recent) >= 20:
                if reason_out is not None:
                    reason_out["reason"] = "key_rate_limit"
                    reason_out["detail"] = (
                        "per-key rate limit reached "
                        "(max 20 requests per minute)")
                return False
            recent.append(now)
            self.active[key_id] = self.active.get(key_id, 0) + 1
            return True

    def release(self, key_id):
        with self.guard:
            count = self.active.get(key_id, 0)
            if count <= 1:
                self.active.pop(key_id, None)
            else:
                self.active[key_id] = count - 1

    def register_inflight(self, key_id, ctx):
        with self.guard:
            self.inflight.setdefault(key_id, set()).add(ctx)

    def unregister_inflight(self, key_id, ctx):
        with self.guard:
            ctxs = self.inflight.get(key_id)
            if ctxs is not None:
                ctxs.discard(ctx)
                if not ctxs:
                    self.inflight.pop(key_id, None)

    def terminate_key(self, key_id):
        """Close every live request held by one revoked key."""
        with self.guard:
            ctxs = self.inflight.pop(key_id, set())
            terminated = 0
            for ctx in ctxs:
                terminate = getattr(ctx, "terminate", None)
                if terminate is not None and terminate():
                    terminated += 1
            return terminated
