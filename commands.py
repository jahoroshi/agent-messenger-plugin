"""Owner-only ``/amsg`` commands."""

from contextvars import ContextVar
import logging
import re

from gateway.config import Platform

from . import mirror, relay, state
from .adapter import PLATFORM_NAME, read_settings


logger = logging.getLogger("amessenger")
REFUSAL = "AMessenger commands are accepted only from the Owner in the Owner Chat."
NOT_CONNECTED = "AMessenger is not connected."
_DURATION = re.compile(r"^(\d+)([hm])$")

_SOURCE: ContextVar = ContextVar("amessenger_source", default=None)
_GATEWAY: ContextVar = ContextVar("amessenger_gateway", default=None)


def remember_source(**kwargs) -> None:
    """Stash the gateway event source for the command dispatched just after it."""
    event = kwargs.get("event")
    gateway = kwargs.get("gateway")
    source = getattr(event, "source", None)
    if source is None or gateway is None:
        _SOURCE.set(None)
        _GATEWAY.set(None)
        return None
    _SOURCE.set(source)
    _GATEWAY.set(gateway)
    return None


def owner_check(adapter, source) -> bool:
    """Return whether *source* is the configured Owner in the Owner Chat."""
    if source is None:
        return False
    try:
        settings = read_settings()
        owner_user = str(settings.get("owner_user") or "")
        same_platform = source.platform.value == adapter._owner_platform
        same_chat = str(source.chat_id) == str(adapter._owner_chat_id)
        named_owner = bool(owner_user) and str(source.user_id) == owner_user
        return same_platform and same_chat and (
            source.chat_type == "dm" or named_owner
        )
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False


def _source_details(source) -> tuple[object, object, object]:
    try:
        platform = source.platform.value
        chat_id = source.chat_id
        user_id = source.user_id
    except (AttributeError, TypeError):
        return None, None, None
    return platform, chat_id, user_id


def _log_refusal(source) -> None:
    platform, chat_id, user_id = _source_details(source)
    logger.warning(
        "[amessenger] refused command from platform=%s chat=%s user=%s",
        platform,
        chat_id,
        user_id,
    )


def _live_adapter(gateway):
    if gateway is None:
        return None
    try:
        return gateway.adapters[Platform(PLATFORM_NAME)]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def parse_interact(tokens) -> tuple[str, float | None, str] | None:
    """Parse the duration and Tool Level after an ``interact`` Channel token."""
    try:
        values = list(tokens)
    except TypeError:
        return None

    duration = None
    level = "base"
    for token in values:
        if not isinstance(token, str):
            return None
        if token == "full":
            if level == "full":
                return None
            level = "full"
            continue
        match = _DURATION.fullmatch(token)
        if match is not None:
            if duration is not None:
                return None
            number = int(match.group(1))
            if number <= 0:
                return None
            multiplier = 3600 if match.group(2) == "h" else 60
            duration = number * multiplier
            continue
        if token == "always":
            if duration is not None:
                return None
            duration = "standing"
            continue
        return None

    if duration == "standing":
        return "standing", None, level
    if duration is None:
        duration = state.DEFAULT_SINGLE_GRANT_HOURS * 3600
    return "single", duration, level


def _ambiguous_message(channels: list[dict]) -> str:
    entries = []
    for channel in channels:
        short_handle = mirror.handle(channel["id"])
        name = channel.get("name") or "unnamed"
        entries.append(f"{short_handle}… {name}")
    return (
        f"That matches {len(channels)} Channels: {', '.join(entries)}. "
        "Type more characters."
    )


async def resolve_channel(adapter, token) -> tuple[dict | None, str | None]:
    """Resolve an Owner-facing Channel id, prefix, or exact name."""
    try:
        channels = await relay.list_channels(adapter.client())
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return None, _relay_failure("list Channels", error)

    exact_ids = [channel for channel in channels if channel.get("id") == token]
    if exact_ids:
        return exact_ids[0], None

    prefixes = [
        channel
        for channel in channels
        if isinstance(channel.get("id"), str) and channel["id"].startswith(token)
    ]
    if len(prefixes) == 1:
        return prefixes[0], None
    if len(prefixes) > 1:
        return None, _ambiguous_message(prefixes)

    exact_names = [channel for channel in channels if channel.get("name") == token]
    if len(exact_names) == 1:
        return exact_names[0], None
    if len(exact_names) > 1:
        return None, _ambiguous_message(exact_names)
    return None, f"No Channel here starts with {token}."


def _relay_failure(action: str, error: Exception) -> str:
    if isinstance(error, relay.RelayRejected):
        return error.detail
    return f"Could not {action}: {error}"


def _channel_token(tokens: list[str]) -> str | None:
    if len(tokens) != 2 or not tokens[1]:
        return None
    return tokens[1]


async def _join(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return help_text
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    try:
        await relay.join(adapter.client(), channel["id"])
    except (relay.RelayRejected, relay.RelayUnavailable) as caught:
        return _relay_failure("join the Channel", caught)
    return f"Joined {mirror.label(channel)}. Messages in it will be mirrored here."


async def _interact(adapter, tokens: list[str], help_text: str) -> str:
    if len(tokens) < 2:
        return help_text
    channel, error = await resolve_channel(adapter, tokens[1])
    if error is not None:
        return error
    parsed = parse_interact(tokens[2:])
    if parsed is None:
        return help_text
    kind, duration_seconds, level = parsed
    moment = state.now()
    updated = state.grant(
        adapter.state(),
        channel["id"],
        kind=kind,
        level=level,
        duration_seconds=duration_seconds,
        moment=moment,
    )
    adapter.set_state(updated)
    record = state.channel(updated, channel["id"])
    grant_period = (
        "standing" if record["expires_at"] is None else f"until {record['expires_at']}"
    )
    return (
        f"{mirror.label(channel)} is now interact, Tool Level {level}, {grant_period}. "
        f"End it any time with `/amsg notify {mirror.handle(channel['id'])}`."
    )


async def _notify(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return help_text
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    adapter.set_state(state.revoke(adapter.state(), channel["id"]))
    return (
        f"{mirror.label(channel)} is back to notify. I will show you its Messages "
        "and do nothing else."
    )


async def _leave(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return help_text
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    try:
        await relay.leave(adapter.client(), channel["id"])
    except (relay.RelayRejected, relay.RelayUnavailable) as caught:
        return _relay_failure("leave the Channel", caught)
    adapter.set_state(state.revoke(adapter.state(), channel["id"]))
    return f"Left {mirror.label(channel)}."


def _is_invited(channel: dict, agent_name: str) -> bool:
    return any(
        member.get("agent") == agent_name and member.get("state") == "invited"
        for member in channel.get("members", [])
        if isinstance(member, dict)
    )


async def _status(adapter) -> str:
    try:
        channels = await relay.list_channels(adapter.client())
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return _relay_failure("list Channels", error)
    if not channels:
        return "No Channels yet."

    agent_name = read_settings().get("agent", "")
    lines = []
    for channel in sorted(channels, key=mirror.label):
        label = mirror.label(channel)
        if _is_invited(channel, agent_name):
            lines.append(
                f"{label} — invited. Join with /amsg join {mirror.handle(channel['id'])}"
            )
            continue
        record = state.channel(adapter.state(), channel["id"])
        if record.get("grant") in {"single", "standing"}:
            period = (
                "standing"
                if record.get("expires_at") is None
                else f"until {record['expires_at']}"
            )
            lines.append(f"{label} — interact, {record['level']}, {period}")
        else:
            lines.append(f"{label} — notify")
    return "\n".join(lines)


def _pending_label(adapter, channel_id) -> str:
    return mirror.label(adapter.known_channel(channel_id))


async def _approval(adapter, choice: str) -> str:
    updated, popped = state.pop_pending_approval(adapter.state())
    if popped is None:
        return "Nothing is waiting for your approval."
    try:
        from tools.approval import resolve_gateway_approval
    except ImportError:
        return "Approvals are unavailable in this Hermes build."

    resolved = resolve_gateway_approval(popped["session_key"], choice)
    adapter.set_state(updated)
    label = _pending_label(adapter, popped["chat_id"])
    return f"Resolved {resolved} approval(s) for Channel {label}."


def make_handler(ctx_unused=None):
    """Return the async callable registered by Hermes as ``/amsg``."""
    async def handle(raw_args: str) -> str:
        gateway = _GATEWAY.get()
        source = _SOURCE.get()
        adapter = _live_adapter(gateway)
        if adapter is None:
            _log_refusal(source)
            return NOT_CONNECTED
        if not owner_check(adapter, source):
            _log_refusal(source)
            return REFUSAL

        from . import HELP_TEXT

        tokens = (raw_args or "").split()
        if not tokens or tokens[0] == "help":
            return HELP_TEXT
        command = tokens[0]
        if command == "join":
            return await _join(adapter, tokens, HELP_TEXT)
        if command == "interact":
            return await _interact(adapter, tokens, HELP_TEXT)
        if command == "notify":
            return await _notify(adapter, tokens, HELP_TEXT)
        if command == "leave":
            return await _leave(adapter, tokens, HELP_TEXT)
        if command == "status" and len(tokens) == 1:
            return await _status(adapter)
        if command == "approve" and len(tokens) == 1:
            return await _approval(adapter, "once")
        if command == "deny" and len(tokens) == 1:
            return await _approval(adapter, "deny")
        return HELP_TEXT

    return handle
