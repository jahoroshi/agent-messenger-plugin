from contextlib import suppress
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile

DEFAULT_SINGLE_GRANT_HOURS = 5.0  # A Grant with no duration (§6.6).
SINGLE_GRANT_IDLE_HOURS = 1.0  # A single Grant's idle limit (§6.6).
REPLY_CAP = 20  # Autonomous replies allowed per Channel (§6.4).
REPLY_WINDOW_SECONDS = 600  # Rolling reply-cap window (§6.4).
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"  # Fixed-width UTC timestamps.


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
    return {"welcomed": False, "channels": {}, "pending_approvals": {}}


def _copy_record(record: dict) -> dict:
    return {**record, "replies": [*record.get("replies", [])]}


def _copy_channels(channels: dict) -> dict:
    return {key: _copy_record(record) for key, record in channels.items()}


def _copy_pending(pending: dict) -> dict:
    return {key: {**entry} for key, entry in pending.items()}


def _copy_state(state: dict, channels=None, pending=None) -> dict:
    return {**state, "channels": _copy_channels(state["channels"] if channels is None else channels),
            "pending_approvals": _copy_pending(state["pending_approvals"] if pending is None else pending)}


def _default_channel() -> dict:
    return dict(policy="notify", level="base", grant=None, granted_at=None,
                expires_at=None, last_incoming_at=None, replies=[])


def channel(state: dict, channel_id, moment=None) -> dict:
    if moment is None:
        moment = now()
    record = state["channels"].get(channel_id)
    if record is None or _grant_ended(record, moment):
        return _default_channel()
    return _copy_record(record)


def grant(state: dict, channel_id, *, kind, level, duration_seconds, moment) -> dict:
    if kind not in {"single", "standing"} or level not in {"base", "full"}:
        raise ValueError("invalid Grant kind or Tool Level")
    granted_at = ts(moment)
    expires_at = None if kind == "standing" else ts(moment + timedelta(seconds=duration_seconds))
    record = {"policy": "interact", "level": level, "grant": kind, "granted_at": granted_at,
              "expires_at": expires_at, "last_incoming_at": granted_at, "replies": []}
    channels = {**state["channels"], channel_id: record}
    return _copy_state(state, channels=channels)


def revoke(state: dict, channel_id) -> dict:
    channels = {key: value for key, value in state["channels"].items() if key != channel_id}
    return _copy_state(state, channels=channels)


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


def _timestamps_are_valid(record: dict) -> bool:
    values = (record.get("granted_at"), record.get("expires_at"), record.get("last_incoming_at"))
    replies = record.get("replies", [])
    return (record.get("grant") in {"single", "standing"}
            and parse_ts(values[0]) is not None
            and all(value is None or parse_ts(value) is not None for value in values[1:])
            and isinstance(replies, list)
            and all(parse_ts(value) is not None for value in replies))


def _grant_ended(record: dict, moment: datetime) -> bool:
    if not _timestamps_are_valid(record):
        return True
    if record["grant"] == "standing":
        return False
    expires_at = parse_ts(record["expires_at"])
    if expires_at is None or expires_at <= moment:
        return True
    last_incoming_at = parse_ts(record.get("last_incoming_at"))
    return last_incoming_at is not None and moment - last_incoming_at >= timedelta(hours=SINGLE_GRANT_IDLE_HOURS)


def expire_grants(state: dict, moment) -> tuple[dict, list[str]]:
    ended = sorted(key for key, value in state["channels"].items() if _grant_ended(value, moment))
    channels = {key: value for key, value in state["channels"].items() if key not in ended}
    return _copy_state(state, channels=channels), ended


def set_welcomed(state: dict, value=True) -> dict:
    return {**_copy_state(state), "welcomed": value}


def add_pending_approval(state: dict, session_key, chat_id, moment) -> dict:
    pending = {**state["pending_approvals"], session_key: {"chat_id": chat_id, "created_at": ts(moment)}}
    return _copy_state(state, pending=pending)


def pop_pending_approval(state: dict) -> tuple[dict, dict | None]:
    pending = state["pending_approvals"]
    if not pending:
        return _copy_state(state), None
    session_key = min(pending, key=lambda key: (pending[key]["created_at"], key))
    entry = pending[session_key]
    remaining = {key: value for key, value in pending.items() if key != session_key}
    popped = {"session_key": session_key, "chat_id": entry["chat_id"], "created_at": entry["created_at"]}
    return _copy_state(state, pending=remaining), popped


def load(path) -> dict:
    target = Path(path)
    try:
        with target.open("r", encoding="utf-8") as source:
            document = json.load(source)
    except FileNotFoundError:
        return empty_state()
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise StateFileCorrupt(f"invalid state file: {target}") from error
    valid = (
        isinstance(document, dict)
        and isinstance(document.get("welcomed"), bool)
        and isinstance(document.get("channels"), dict)
        and isinstance(document.get("pending_approvals"), dict)
    )
    if not valid:
        raise StateFileCorrupt(f"invalid state file: {target}")
    return document


def save(path, state) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
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
