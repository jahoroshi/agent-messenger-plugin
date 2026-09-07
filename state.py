from contextlib import contextmanager, suppress
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None

BOUNDED_GRANT_HOURS = 5.0  # The longest Grant nobody human is approving (§6.6).
SINGLE_GRANT_IDLE_HOURS = 1.0  # A single Grant's idle limit (§6.6).
REPLY_CAP = 20  # Autonomous replies allowed per Channel (§6.4).
REPLY_WINDOW_SECONDS = 600  # Rolling reply-cap window (§6.4).
PENDING_MIRRORS_MAX = 200  # Bound queued Owner-facing outbound Mirrors.
DEDUPE_MAX = 200  # Processed Delivery ids retained for restart-safe dedupe.
PENDING_INVITES_MAX = DEDUPE_MAX  # Pending Invite snapshots retained for restart-safe lookup.
DEDUPE_SECONDS = 3600  # Processed Delivery retention window.
SEND_IDEMPOTENCY_SECONDS = 60  # Exact tool sends are coalesced for one minute.
SEND_IDEMPOTENCY_MAX = DEDUPE_MAX
OWNER_LOG_MAX_LINES = 2000
AUTHENTICITY_SECRET_KEY = "authenticity_secret"
AUTHENTICITY_SECRET_LENGTH = 4
_AUTHENTICITY_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{4}$")
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # Fixed-width UTC timestamps.
logger = logging.getLogger("amessenger")
_file_lock_warning_logged = False
_file_lock_warning_lock = threading.Lock()


class StateFileCorrupt(Exception):
    """The state file exists but is not a valid state document."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def ts(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("moment must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def empty_state() -> dict:
    return {
        "welcomed": False,
        "channels": {},
        "pending_approvals": {},
        "pending_invites": {},
        "pending_mirrors": [],
        "seen_deliveries": {},
        # None is the uninitialised value used by old/in-memory state. The
        # adapter replaces it with a generated value before its first post.
        AUTHENTICITY_SECRET_KEY: None,
    }


def _valid_authenticity_secret(value) -> bool:
    return isinstance(value, str) and bool(_AUTHENTICITY_SECRET_RE.fullmatch(value))


def ensure_authenticity_secret(document: dict) -> dict:
    """Return state with its stable per-gateway authenticity secret.

    A missing value is a legacy/uninitialised state and gets one new secret.
    Any present value has to retain the short, URL-safe shape so a damaged
    state file cannot silently change the mark used to identify real posts.
    """
    value = document.get(AUTHENTICITY_SECRET_KEY)
    if value is None:
        return {
            **document,
            AUTHENTICITY_SECRET_KEY: secrets.token_urlsafe(3),
        }
    if not _valid_authenticity_secret(value):
        raise StateFileCorrupt("invalid authenticity secret")
    return {**document}


def _copy_record(record: dict) -> dict:
    return {
        **record,
        "name": record.get("name"),
        "topic": record.get("topic"),
        "replies": [*record.get("replies", [])],
    }


def _copy_channels(channels: dict) -> dict:
    return {key: _copy_record(record) for key, record in channels.items()}


def _copy_pending(pending: dict) -> dict:
    return {key: {**entry} for key, entry in pending.items()}


def _copy_pending_invites(pending_invites: dict) -> dict:
    return {
        key: {**entry, "topic": entry.get("topic")}
        for key, entry in pending_invites.items()
    }


def _bound_pending_invites(pending_invites: dict) -> dict:
    if len(pending_invites) <= PENDING_INVITES_MAX:
        return pending_invites
    return dict(list(pending_invites.items())[-PENDING_INVITES_MAX:])


def _copy_state(
    state: dict,
    channels=None,
    pending=None,
    pending_invites=None,
    pending_mirrors=None,
    seen_deliveries=None,
    send_idempotency=None,
) -> dict:
    mirrors = (
        state.get("pending_mirrors", [])
        if pending_mirrors is None
        else pending_mirrors
    )
    seen = (
        state.get("seen_deliveries", {})
        if seen_deliveries is None
        else seen_deliveries
    )
    invites = (
        state.get("pending_invites", {})
        if pending_invites is None
        else pending_invites
    )
    updated = {
        **state,
        "channels": _copy_channels(state["channels"] if channels is None else channels),
        "pending_approvals": _copy_pending(
            state["pending_approvals"] if pending is None else pending
        ),
        "pending_invites": _bound_pending_invites(
            _copy_pending_invites(invites)
        ),
        "pending_mirrors": [*mirrors],
        "seen_deliveries": {**seen},
    }
    if "send_idempotency" in state or send_idempotency is not None:
        idempotency = (
            state.get("send_idempotency", {})
            if send_idempotency is None
            else send_idempotency
        )
        updated["send_idempotency"] = {
            key: {**entry} for key, entry in idempotency.items()
        }
    return updated


def _default_channel() -> dict:
    return dict(name=None, topic=None, policy="notify", level="base", grant=None, granted_at=None,
                expires_at=None, last_incoming_at=None, replies=[])


def channel(state: dict, channel_id, moment=None) -> dict:
    if moment is None:
        moment = now()
    record = state["channels"].get(channel_id)
    if record is None or _grant_ended(record, moment):
        return _default_channel()
    return _copy_record(record)


def grant(
    state: dict, channel_id, *, kind, level, duration_seconds, moment,
    bound_seconds=None,
) -> dict:
    """Write a Grant.

    ``kind`` is the Owner's intent and decides how the Grant ends early: a
    single Grant ends on ``[TASK_DONE]`` or an idle hour, a standing one does
    not. ``bound_seconds`` is a safety bound on a standing Grant, used when
    nobody human is answering the peer's approvals. The two are separate on
    purpose: writing the bound as a single Grant would silently attach both
    early-end rules to a Grant the Owner asked to keep running.
    """
    if kind not in {"single", "standing"} or level not in {"base", "full"}:
        raise ValueError("invalid Grant kind or Tool Level")
    granted_at = ts(moment)
    if kind == "single":
        expires_at = ts(moment + timedelta(seconds=duration_seconds))
    elif bound_seconds is not None:
        expires_at = ts(moment + timedelta(seconds=bound_seconds))
    else:
        expires_at = None
    existing = state["channels"].get(channel_id, _default_channel())
    record = {
        "name": existing.get("name"),
        "topic": existing.get("topic"),
        "policy": "interact",
        "level": level,
        "grant": kind,
        "granted_at": granted_at,
        "expires_at": expires_at,
        "last_incoming_at": granted_at,
        "replies": [],
    }
    channels = {**state["channels"], channel_id: record}
    return _copy_state(state, channels=channels)


def remember_channel(state: dict, channel: dict) -> dict:
    """Persist the relay-owned name and topic alongside a Channel's policy."""
    if not isinstance(channel, dict):
        return _copy_state(state)
    channel_id = channel.get("id")
    if not isinstance(channel_id, str) or not channel_id:
        return _copy_state(state)
    existing = state["channels"].get(channel_id, _default_channel())
    record = _copy_record(existing)
    if "name" in channel:
        record["name"] = channel.get("name")
    if "topic" in channel:
        record["topic"] = channel.get("topic")
    return _copy_state(
        state,
        channels={**state["channels"], channel_id: record},
    )


def revoke(state: dict, channel_id) -> dict:
    channels = {key: value for key, value in state["channels"].items() if key != channel_id}
    return _copy_state(state, channels=channels)


def drop_channel(state: dict, channel_id) -> dict:
    """Forget a Channel and any pending Invite for it."""
    channels = {
        key: value for key, value in state["channels"].items() if key != channel_id
    }
    pending_invites = {
        key: value
        for key, value in state.get("pending_invites", {}).items()
        if key != channel_id
    }
    return _copy_state(
        state,
        channels=channels,
        pending_invites=pending_invites,
    )


def reconcile_channels(state: dict, listed_ids) -> tuple[dict, list[str]]:
    """Drop Channels absent from one successful relay listing.

    The caller decides whether a listing is trustworthy.  This function only
    applies the authoritative set it is given and returns the dropped Channel
    ids so an active Grant can be explained to the Owner.
    """
    listed = {channel_id for channel_id in listed_ids if isinstance(channel_id, str)}
    dropped = sorted(
        channel_id
        for channel_id in state["channels"]
        if channel_id not in listed
    )
    channels = {
        key: value
        for key, value in state["channels"].items()
        if key in listed
    }
    pending_invites = {
        key: value
        for key, value in state.get("pending_invites", {}).items()
        if key in listed
    }
    return (
        _copy_state(
            state,
            channels=channels,
            pending_invites=pending_invites,
        ),
        dropped,
    )


def _pending_invite_snapshot(channel: dict) -> dict:
    if not isinstance(channel, dict):
        raise ValueError("pending Invite Channel must be an object")
    channel_id = channel.get("id")
    name = channel.get("name")
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("pending Invite Channel id must be a non-empty string")
    if name is not None and not isinstance(name, str):
        raise ValueError("pending Invite Channel name must be a string or null")
    topic = channel.get("topic")
    if topic is not None and not isinstance(topic, str):
        raise ValueError("pending Invite Channel topic must be a string or null")
    return {"id": channel_id, "name": name, "topic": topic}


def _pending_invite_is_valid(key, entry) -> bool:
    return (
        isinstance(key, str)
        and bool(key)
        and isinstance(entry, dict)
        and entry.get("id") == key
        and "name" in entry
        and (entry["name"] is None or isinstance(entry["name"], str))
        and (entry.get("topic") is None or isinstance(entry.get("topic"), str))
    )


def remember_pending_invite(state: dict, channel: dict) -> dict:
    """Remember the id and name needed to resolve a newly posted Invite."""
    snapshot = _pending_invite_snapshot(channel)
    channel_id = snapshot["id"]
    pending_invites = {**state.get("pending_invites", {})}
    pending_invites.pop(channel_id, None)
    pending_invites[channel_id] = snapshot
    return _copy_state(
        state,
        pending_invites=_bound_pending_invites(pending_invites),
    )


def drop_pending_invite(state: dict, channel_id: str) -> dict:
    """Forget a pending Invite without mutating the input state."""
    pending_invites = {
        key: value
        for key, value in state.get("pending_invites", {}).items()
        if key != channel_id
    }
    return _copy_state(state, pending_invites=pending_invites)


def note_incoming(state: dict, channel_id, moment) -> dict:
    if channel_id not in state["channels"]:
        return _copy_state(state)
    channels = {key: ({**_copy_record(value), "last_incoming_at": ts(moment)}
                      if key == channel_id else value)
                for key, value in state["channels"].items()}
    return _copy_state(state, channels=channels)


def _replies_in_window(record: dict, moment: datetime) -> list[str]:
    cutoff = moment - timedelta(seconds=REPLY_WINDOW_SECONDS)
    return [value for value in record.get("replies", [])
            if (parsed := parse_ts(value)) is not None and parsed >= cutoff]


def note_reply(state: dict, channel_id, moment) -> dict:
    if channel_id not in state["channels"]:
        return _copy_state(state)
    replies = [*_replies_in_window(state["channels"][channel_id], moment), ts(moment)]
    channels = {key: ({**_copy_record(value), "replies": replies}
                      if key == channel_id else value)
                for key, value in state["channels"].items()}
    return _copy_state(state, channels=channels)


def cap_reached(state: dict, channel_id, moment) -> bool:
    record = state["channels"].get(channel_id)
    return record is not None and len(_replies_in_window(record, moment)) >= REPLY_CAP


def forget_old_deliveries(state: dict, moment: datetime) -> dict:
    """Drop expired Delivery ids and retain only the newest bounded set."""
    cutoff = moment - timedelta(seconds=DEDUPE_SECONDS)
    current = {}
    for delivery_id, seen_at in state.get("seen_deliveries", {}).items():
        parsed = parse_ts(seen_at)
        if parsed is not None and parsed >= cutoff:
            current[delivery_id] = seen_at
    if len(current) > DEDUPE_MAX:
        current = dict(list(current.items())[-DEDUPE_MAX:])
    return _copy_state(state, seen_deliveries=current)


def remember_delivery(state: dict, delivery_id: str, moment: datetime) -> dict:
    """Remember a Delivery id with a timestamp, without mutating *state*."""
    updated = forget_old_deliveries(state, moment)
    seen = {**updated["seen_deliveries"]}
    seen.pop(delivery_id, None)
    seen[delivery_id] = ts(moment)
    if len(seen) > DEDUPE_MAX:
        seen = dict(list(seen.items())[-DEDUPE_MAX:])
    return _copy_state(updated, seen_deliveries=seen)


def was_delivery_seen(state: dict, delivery_id: str, moment: datetime) -> bool:
    """Return whether *delivery_id* is still inside the dedupe window."""
    seen_at = state.get("seen_deliveries", {}).get(delivery_id)
    parsed = parse_ts(seen_at)
    if parsed is None:
        return False
    return parsed >= moment - timedelta(seconds=DEDUPE_SECONDS)


def send_idempotency_key(channel_id: str, text: str) -> str:
    """Hash a resolved Channel and body without storing the body in state."""
    encoded = json.dumps(
        [channel_id, text], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _copy_send_idempotency(entries: dict) -> dict:
    return {
        key: {**entry}
        for key, entry in entries.items()
        if isinstance(key, str) and isinstance(entry, dict)
    }


def _bound_send_idempotency(entries: dict) -> dict:
    if len(entries) <= SEND_IDEMPOTENCY_MAX:
        return entries
    ordered = sorted(
        entries.items(),
        key=lambda item: (parse_ts(item[1]["sent_at"]), item[0]),
    )
    return dict(ordered[-SEND_IDEMPOTENCY_MAX:])


def forget_old_sends(state: dict, moment: datetime) -> dict:
    """Prune expired exact-send records and retain the newest bounded set."""
    entries = state.get("send_idempotency", {})
    cutoff = moment - timedelta(seconds=SEND_IDEMPOTENCY_SECONDS)
    current = {}
    for key, entry in entries.items():
        sent_at = parse_ts(entry.get("sent_at")) if isinstance(entry, dict) else None
        message_id = entry.get("message_id") if isinstance(entry, dict) else None
        if (
            isinstance(key, str)
            and isinstance(message_id, str)
            and sent_at is not None
            and sent_at >= cutoff
        ):
            current[key] = {"message_id": message_id, "sent_at": entry["sent_at"]}
    current = _bound_send_idempotency(current)
    return _copy_state(state, send_idempotency=current)


def recent_send(state: dict, key: str, moment: datetime) -> dict | None:
    """Return a recent exact-send record, if one exists."""
    entry = state.get("send_idempotency", {}).get(key)
    if not isinstance(entry, dict):
        return None
    sent_at = parse_ts(entry.get("sent_at"))
    message_id = entry.get("message_id")
    if (
        sent_at is None
        or not isinstance(message_id, str)
        or sent_at < moment - timedelta(seconds=SEND_IDEMPOTENCY_SECONDS)
    ):
        return None
    return {"message_id": message_id, "sent_at": entry["sent_at"]}


def remember_send(
    state: dict, key: str, message_id: str, moment: datetime
) -> dict:
    """Remember a successful exact send without retaining its body."""
    updated = forget_old_sends(state, moment)
    entries = {**updated.get("send_idempotency", {})}
    entries.pop(key, None)
    entries[key] = {"message_id": message_id, "sent_at": ts(moment)}
    entries = _bound_send_idempotency(entries)
    return _copy_state(updated, send_idempotency=entries)


def _timestamps_are_valid(record: dict) -> bool:
    values = (record.get("granted_at"), record.get("expires_at"), record.get("last_incoming_at"))
    replies = record.get("replies", [])
    return (record.get("grant") in {"single", "standing"}
            and parse_ts(values[0]) is not None
            and all(value is None or parse_ts(value) is not None for value in values[1:])
            and isinstance(replies, list)
            and all(parse_ts(value) is not None for value in replies))


def _grant_ended(record: dict, moment: datetime) -> bool:
    # A remembered Channel can exist without a Grant.  Its name and topic are
    # still useful metadata and must not be mistaken for an expired Grant.
    if record.get("grant") is None:
        return False
    if not _timestamps_are_valid(record):
        return True
    if record["grant"] == "standing":
        # A standing Grant has no idle limit and no [TASK_DONE] end. It ends
        # only at a safety bound, when it carries one.
        if record.get("expires_at") is None:
            return False
        expires_at = parse_ts(record["expires_at"])
        return expires_at is None or expires_at <= moment
    expires_at = parse_ts(record["expires_at"])
    if expires_at is None or expires_at <= moment:
        return True
    last_incoming_at = parse_ts(record.get("last_incoming_at"))
    return last_incoming_at is not None and moment - last_incoming_at >= timedelta(hours=SINGLE_GRANT_IDLE_HOURS)


def expire_grants(state: dict, moment) -> tuple[dict, list[str]]:
    ended = sorted(
        key
        for key, value in state["channels"].items()
        if value.get("grant") in {"single", "standing"}
        and _grant_ended(value, moment)
    )
    channels = {key: value for key, value in state["channels"].items() if key not in ended}
    return _copy_state(state, channels=channels), ended


def set_welcomed(state: dict, value=True) -> dict:
    return {**_copy_state(state), "welcomed": value}


def add_pending_approval(
    state: dict, session_key, chat_id, moment, channel: dict | None = None
) -> dict:
    entry = {"chat_id": chat_id, "created_at": ts(moment)}
    if isinstance(channel, dict):
        entry["name"] = channel.get("name")
        entry["topic"] = channel.get("topic")
    pending = {**state["pending_approvals"], session_key: entry}
    return _copy_state(state, pending=pending)


def _pending_mirror_entry(
    text: str, framed_text: str | None = None, note_transcript: bool = True
):
    if framed_text is None and note_transcript:
        return text
    entry = {"text": text, "note_transcript": note_transcript}
    if framed_text is not None:
        entry["framed_text"] = framed_text
    return entry


def mirror_entry_text(entry) -> str:
    """Return the human-facing text from a queued Mirror entry."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return entry["text"]
    return ""


def mirror_entry_transcript(entry) -> str | None:
    """Return an optional model-safe transcript copy from a queue entry."""
    if isinstance(entry, dict) and isinstance(entry.get("framed_text"), str):
        return entry["framed_text"]
    return None


def mirror_entry_should_note(entry) -> bool:
    if isinstance(entry, dict):
        return entry.get("note_transcript", True) is True
    return True


def queue_mirror(
    state: dict,
    text: str,
    *,
    framed_text: str | None = None,
    note_transcript: bool = True,
) -> dict:
    """Append an Owner-facing line, retaining only the newest queued lines."""
    pending = [
        *state.get("pending_mirrors", []),
        _pending_mirror_entry(text, framed_text, note_transcript),
    ]
    if len(pending) > PENDING_MIRRORS_MAX:
        pending = pending[-PENDING_MIRRORS_MAX:]
    return _copy_state(state, pending_mirrors=pending)


def pop_mirror(state: dict) -> tuple[dict, str | None]:
    """Remove and return the oldest queued Owner-facing line, if any."""
    pending = state.get("pending_mirrors", [])
    if not pending:
        return _copy_state(state), None
    return _copy_state(state, pending_mirrors=pending[1:]), pending[0]


def pop_pending_approval(state: dict, session_key=None) -> tuple[dict, dict | None]:
    pending = state["pending_approvals"]
    if not pending:
        return _copy_state(state), None
    if session_key is None:
        session_key = min(pending, key=lambda key: (pending[key]["created_at"], key))
    if session_key not in pending:
        return _copy_state(state), None
    entry = pending[session_key]
    remaining = {key: value for key, value in pending.items() if key != session_key}
    popped = {"session_key": session_key, "chat_id": entry["chat_id"], "created_at": entry["created_at"]}
    return _copy_state(state, pending=remaining), popped


def expire_pending_approvals(
    state: dict, moment: datetime, timeout_seconds: int
) -> tuple[dict, list[str]]:
    """Drop pending approval records that have outlived Hermes's wait window."""
    cutoff = moment - timedelta(seconds=timeout_seconds)
    pending = state["pending_approvals"]
    expired = []
    for key, entry in pending.items():
        created_at = parse_ts(entry.get("created_at"))
        if created_at is None or created_at < cutoff:
            expired.append(key)
    expired.sort()
    if not expired:
        return _copy_state(state), []
    remaining = {key: value for key, value in pending.items() if key not in expired}
    return _copy_state(state, pending=remaining), expired


def load(path) -> dict:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as source:
            document = json.load(source)
    except FileNotFoundError:
        return empty_state()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StateFileCorrupt(f"invalid state file: {target}") from error
    pending_mirrors = document.get("pending_mirrors", []) if isinstance(document, dict) else None
    seen_deliveries = document.get("seen_deliveries", {}) if isinstance(document, dict) else None
    pending_invites = document.get("pending_invites", {}) if isinstance(document, dict) else None
    send_idempotency = document.get("send_idempotency", {}) if isinstance(document, dict) else None
    pending_approvals = document.get("pending_approvals", {}) if isinstance(document, dict) else None
    valid = (
        isinstance(document, dict)
        and isinstance(document.get("welcomed"), bool)
        and isinstance(document.get("channels"), dict)
        and all(isinstance(record, dict) for record in document["channels"].values())
        and isinstance(pending_approvals, dict)
        and isinstance(pending_invites, dict)
        and all(
            _pending_invite_is_valid(channel_id, entry)
            for channel_id, entry in pending_invites.items()
        )
        and isinstance(pending_mirrors, list)
        and all(
            (
                isinstance(value, str)
                or (
                    isinstance(value, dict)
                    and isinstance(value.get("text"), str)
                    and (
                        "framed_text" not in value
                        or isinstance(value.get("framed_text"), str)
                    )
                    and isinstance(value.get("note_transcript", True), bool)
                )
            )
            for value in pending_mirrors
        )
        and isinstance(seen_deliveries, dict)
        and all(
            isinstance(delivery_id, str) and isinstance(seen_at, str)
            for delivery_id, seen_at in seen_deliveries.items()
        )
        and isinstance(send_idempotency, dict)
        and all(
            isinstance(key, str)
            and isinstance(entry, dict)
            and isinstance(entry.get("message_id"), str)
            and isinstance(entry.get("sent_at"), str)
            for key, entry in send_idempotency.items()
        )
    )
    if not valid:
        raise StateFileCorrupt(f"invalid state file: {target}")
    authenticity_secret = document.get(AUTHENTICITY_SECRET_KEY)
    if authenticity_secret is not None and not _valid_authenticity_secret(
        authenticity_secret
    ):
        raise StateFileCorrupt(f"invalid state file: {target}")
    loaded = {
        **document,
        "channels": _copy_channels(document["channels"]),
        "pending_approvals": _copy_pending(pending_approvals),
        "pending_invites": _bound_pending_invites(
            _copy_pending_invites(pending_invites)
        ),
        "pending_mirrors": [*pending_mirrors],
        "seen_deliveries": {**seen_deliveries},
        AUTHENTICITY_SECRET_KEY: authenticity_secret,
    }
    if "send_idempotency" in document:
        loaded["send_idempotency"] = _copy_send_idempotency(send_idempotency)
    return loaded


def save(path, state) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if "pending_invites" in state:
        state = {
            **state,
            "pending_invites": _bound_pending_invites(
                _copy_pending_invites(state["pending_invites"])
            ),
        }
    state = forget_old_sends(state, now()) if "send_idempotency" in state else state
    temporary_name = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(state, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, target)
    except Exception:
        if temporary_name is not None:
            with suppress(OSError):
                os.unlink(temporary_name)
        raise


@contextmanager
def file_lock(path):
    """Hold an exclusive advisory lock on the sibling ``<path>.lock`` file."""
    global _file_lock_warning_logged

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{target}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        if fcntl is None:
            with _file_lock_warning_lock:
                if not _file_lock_warning_logged:
                    logger.warning(
                        "[amessenger] fcntl is unavailable; state file locking is disabled"
                    )
                    _file_lock_warning_logged = True
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_file(path) -> dict:
    """Initialise a state file's authenticity secret under the file lock."""
    target = Path(path)
    with file_lock(target):
        document = load(target)
        updated = ensure_authenticity_secret(document)
        if updated != document:
            save(target, updated)
        return updated


def update_file(path, change) -> dict:
    """Load, change, and save a state file while holding its process lock."""
    with file_lock(path):
        document = ensure_authenticity_secret(load(path))
        updated = change(document)
        updated = ensure_authenticity_secret(updated)
        if "send_idempotency" in updated:
            updated = forget_old_sends(updated, now())
        save(path, updated)
        return updated


def append_owner_log(path, text: str, moment: datetime | None = None) -> None:
    """Append one unmarked Owner-Chat line and retain at most 2000 records."""
    if not isinstance(text, str):
        raise TypeError("Owner log text must be a string")
    target = Path(path)
    if moment is None:
        moment = now()
    record = json.dumps(
        {"timestamp": ts(moment), "text": text},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"

    # The sibling lock is the same fcntl discipline used for state.json. Keep
    # malformed old lines during rotation; readers skip them after a crash.
    with file_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a+", encoding="utf-8") as log_file:
            log_file.seek(0)
            lines = log_file.readlines()
            if len(lines) >= OWNER_LOG_MAX_LINES:
                log_file.seek(0)
                log_file.truncate()
                log_file.writelines(lines[-(OWNER_LOG_MAX_LINES - 1) :])
            log_file.seek(0, os.SEEK_END)
            log_file.write(record)
            log_file.flush()


def append_pending_decision(
    path, channel_id: str, choice: str, moment: datetime | None = None
) -> None:
    """Append one gateway-less approval decision under the shared file lock."""
    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("pending decision Channel id must be a non-empty string")
    if choice not in {"once", "deny"}:
        raise ValueError("pending decision choice must be once or deny")
    if moment is None:
        moment = now()
    record = json.dumps(
        {"channel_id": channel_id, "choice": choice, "at": ts(moment)},
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"

    target = Path(path)
    with file_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a+", encoding="utf-8") as decision_file:
            decision_file.seek(0)
            existing = decision_file.read()
            decision_file.seek(0, os.SEEK_END)
            # A process may have crashed halfway through a JSONL record. Keep
            # that malformed record skippable, but separate the next decision
            # so it remains parseable and is not lost during the next drain.
            if existing and not existing.endswith("\n"):
                decision_file.write("\n")
            decision_file.write(record)
            decision_file.flush()


def read_pending_decisions(path) -> tuple[list[dict], str]:
    """Read valid decisions and return them with the exact file snapshot."""
    target = Path(path)
    try:
        with file_lock(target):
            try:
                snapshot = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                return [], ""
    except (OSError, UnicodeDecodeError):
        return [], ""

    decisions = []
    for line in snapshot.splitlines():
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not (
            isinstance(value, dict)
            and isinstance(value.get("channel_id"), str)
            and bool(value["channel_id"])
            and value.get("choice") in {"once", "deny"}
            and isinstance(value.get("at"), str)
        ):
            continue
        decisions.append(value)
    return decisions, snapshot


def truncate_pending_decisions(path, consumed_snapshot: str) -> bool:
    """Remove a consumed snapshot while preserving decisions appended later."""
    if not consumed_snapshot:
        return True
    target = Path(path)
    with file_lock(target):
        try:
            current = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        except (OSError, UnicodeDecodeError):
            return False
        # Writers append under the same sibling lock. If the prefix is still
        # present, dropping exactly that prefix cannot erase a later decision.
        if not current.startswith(consumed_snapshot):
            return False
        remaining = current[len(consumed_snapshot) :]
        with target.open("w", encoding="utf-8") as decision_file:
            decision_file.write(remaining)
            decision_file.flush()
    return True


def read_owner_log(path, limit: int) -> list[dict]:
    """Read the newest valid Owner log records, skipping malformed lines."""
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as log_file:
            lines = log_file.readlines()
    except FileNotFoundError:
        return []

    records = []
    for line in lines:
        try:
            value = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not (
            isinstance(value, dict)
            and isinstance(value.get("timestamp"), str)
            and isinstance(value.get("text"), str)
        ):
            continue
        records.append(value)
    return records[-limit:]
