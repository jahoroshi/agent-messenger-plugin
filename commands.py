"""Owner-only ``/amsg`` commands."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
import logging
import os
import re

from . import adapter as adapter_module
from . import mirror, relay, security, state
from .adapter import active_adapter, read_settings


logger = logging.getLogger("amessenger")
REFUSAL = "AMessenger commands are accepted only from the Owner in the Owner Chat."
MISSING_GROUP_OWNER_USER_HINT = (
    "This Owner Chat is a group and AMESSENGER_OWNER_USER is not set; set it to "
    "your platform user id and restart the gateway."
)
SETUP_TUI = (
    "AMessenger setup must be typed in the chat where the Owner wants to see mail; "
    "type `/amsg setup` there. Nothing was written."
)
SETUP_KIND_FORMS = "corporate or personal"
SETUP_NO_RELAY = (
    "No relay address is configured. Ask your administrator for it and type "
    "/amsg setup … --relay <url>."
)
SETUP_KEY_GROUP_WARNING = (
    "Warning: a key typed into a group chat is visible to everyone in this group; "
    "use `--key` only in a private chat or the TUI."
)
_DURATION = re.compile(r"^(\d+)([hm])$")

_SOURCE: ContextVar = ContextVar("amessenger_source", default=None)
_GATEWAY: ContextVar = ContextVar("amessenger_gateway", default=None)
_IN_GATEWAY_PROCESS = False


def remember_source(**kwargs) -> None:
    """Stash the gateway event source for the command dispatched just after it."""
    global _IN_GATEWAY_PROCESS
    _IN_GATEWAY_PROCESS = True
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


def in_gateway_process() -> bool:
    """Return whether Hermes has delivered a gateway dispatch hook here."""
    return _IN_GATEWAY_PROCESS


def owner_check(adapter, source) -> bool:
    """Return whether *source* is the configured Owner in the Owner Chat."""
    # A TUI/CLI process has no platform adapters or peer path into it. Its
    # console user is therefore the Owner; the hook flag is positive evidence
    # of a gateway, because the hook fires before every gateway command.
    if not in_gateway_process():
        return True
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


def missing_group_owner_user(adapter, source) -> bool:
    """Return whether this refusal is caused by an unnamed group Owner."""
    try:
        settings = read_settings()
        return (
            source.platform.value == adapter._owner_platform
            and str(source.chat_id) == str(adapter._owner_chat_id)
            and source.chat_type == "group"
            and not str(settings.get("owner_user") or "").strip()
        )
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False


def _source_details(source) -> tuple[object, object, object]:
    try:
        platform = getattr(source.platform, "value", source.platform)
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


def _setup_source_is_usable(source) -> bool:
    platform, chat_id, _user_id = _source_details(source)
    return bool(platform and str(chat_id or "").strip())


def _setup_owner_check(adapter, source) -> bool:
    """Allow first setup, and let the current Owner request a move."""
    if adapter is None:
        return _setup_source_is_usable(source)
    if adapter_module.configuration_state() != "configured":
        return _setup_source_is_usable(source)
    if owner_check(adapter, source):
        return True
    # A move starts in the new chat, so the configured group Owner identity is
    # the proof that this setup command belongs to the current Owner.
    try:
        settings = read_settings()
        platform, _chat_id, user_id = _source_details(source)
        return (
            bool(settings.get("owner_user"))
            and platform == adapter._owner_platform
            and str(user_id) == str(settings["owner_user"])
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _not_setup_message() -> str:
    missing = [
        name
        for name in adapter_module.REQUIRED_ENV
        if not os.getenv(name, "").strip()
    ]
    cause = (
        "missing " + ", ".join(missing)
        if missing
        else "the Owner Chat and Agent are not configured"
    )
    return (
        f"AMessenger is not set up yet: {cause}; type `/amsg setup` in the chat "
        "where the Owner wants to see mail."
    )


def _setup_agent_error(agent: str) -> str:
    return (
        f"Agent name `{security.safe_field(agent, fallback='')}` is invalid: it must "
        "match `[a-z0-9][a-z0-9-]{1,31}`. Pass a valid name as the first argument, "
        "for example `/amsg setup my-agent`."
    )


def _setup_argument_error() -> str:
    return (
        "Accepted forms for setup: `/amsg setup [name] [corporate|personal] "
        "[--key <key>] [--relay <url>] [--confirm]`; to confirm a requested "
        "Owner Chat move, type `/amsg setup --confirm`."
    )


def _setup_card_reply(card: dict) -> str:
    card_lines = [line.strip() for line in mirror.format_card(card).splitlines()]
    published = card_lines[0] + " " + "; ".join(card_lines[1:])
    return f"{published}; this chat is your Owner Chat; type /amsg help."


def _redact_setup_secret(text, secret: str = "") -> str:
    rendered = str(text)
    if secret:
        rendered = rendered.replace(secret, "[redacted]")
    return rendered


def _setup_reply(
    text: str,
    source,
    *,
    key_supplied: bool = False,
    secret: str = "",
) -> str:
    reply = _redact_setup_secret(text, secret)
    if key_supplied:
        try:
            chat_type = str(source.chat_type or "").casefold()
        except (AttributeError, TypeError):
            chat_type = ""
        if chat_type in {"group", "forum", "channel"}:
            reply = f"{reply} {SETUP_KEY_GROUP_WARNING}"
    return _redact_setup_secret(reply, secret)


def _setup_move_message(old_owner_chat: str, owner_chat: str) -> str:
    return (
        f"Moving the Owner Chat from {old_owner_chat} to {owner_chat} will "
        "change where AMessenger mail is shown. Nothing moved yet; type "
        "/amsg setup --confirm to confirm, or type another setup command "
        "to leave it unchanged."
    )


def _parse_setup_arguments(tokens: list[str]) -> dict | None:
    """Parse setup's two optional positionals and non-colliding flags."""
    positionals = []
    values = {"key": None, "relay": None, "confirm": False}
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--confirm":
            if values["confirm"]:
                return None
            values["confirm"] = True
            index += 1
            continue
        if token in {"--key", "--relay"}:
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                return None
            option = "key" if token == "--key" else "relay"
            if values[option] is not None:
                return None
            values[option] = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--"):
            return None
        positionals.append(token)
        if len(positionals) > 2:
            return None
        index += 1
    values["positionals"] = positionals
    return values


def _stored_setup_value(profile_values: dict[str, str], *names: str) -> str:
    for name in names:
        value = profile_values.get(name, "").strip()
        if value:
            return value
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _setup_relay_error(url: str, error: Exception, secret: str = "") -> str:
    safe_url = _redact_setup_secret(url, secret)
    if isinstance(error, relay.CardConflict) or (
        isinstance(error, relay.RelayRejected) and error.status == 409
    ):
        return (
            f"AMessenger setup failed: Agent name is already taken by another Owner "
            f"at {safe_url}; choose another name as the first argument and type "
            "`/amsg setup <agent-name> [corporate|personal]`."
        )
    if isinstance(error, relay.RelayRejected):
        if error.status in {401, 403}:
            return (
                f"AMessenger setup failed: the relay at {safe_url} rejected the key; put "
                "a valid Redmine API key in REDMINE_API_KEY or AMESSENGER_KEY in the "
                "profile .env, then type `/amsg setup` again."
            )
        return (
            f"AMessenger setup failed: the relay at {safe_url} rejected the request "
            f"({_redact_setup_secret(error.detail, secret)}); fix that cause and type "
            "/amsg setup again."
        )
    return (
        f"AMessenger setup failed: the relay at {safe_url} is unreachable "
        f"({_redact_setup_secret(error, secret)}); check AMESSENGER_URL or the relay "
        "health, then type `/amsg setup` again."
    )


async def _setup(adapter, tokens: list[str], source) -> str:
    if not in_gateway_process() or not _setup_source_is_usable(source):
        return SETUP_TUI
    parsed = _parse_setup_arguments(tokens)
    if parsed is None:
        return _setup_argument_error()

    pending = getattr(adapter, "_pending_setup", None) if adapter is not None else None
    if parsed["confirm"]:
        if (
            parsed["positionals"]
            or parsed["key"] is not None
            or parsed["relay"] is not None
        ):
            return _setup_argument_error()
        if not isinstance(pending, dict):
            return (
                "AMessenger setup has no Owner Chat move waiting for confirmation; "
                "type /amsg setup in the chat you want to use, or type /amsg setup "
                "without `--confirm` to start setup."
            )
        values = dict(pending["values"])
        clear = tuple(pending.get("clear", ()))
        key_supplied = bool(pending.get("key_supplied"))
        key = str(values.get("AMESSENGER_KEY") or "")
    else:
        profile = adapter_module.profile_name()
        positionals = parsed["positionals"]
        agent = positionals[0] if positionals else profile
        kind = positionals[1] if len(positionals) == 2 else "corporate"
        if re.fullmatch(adapter_module.AGENT_NAME_PATTERN, agent) is None:
            return _setup_agent_error(agent)
        if kind not in adapter_module.KINDS:
            return (
                f"Agent kind {security.safe_field(kind)} is invalid; use "
                f"{SETUP_KIND_FORMS} as the second argument and type /amsg setup again."
            )
        platform, chat_id, user_id = _source_details(source)
        try:
            profile_values = adapter_module._profile_env_values()
        except OSError as error:
            return _setup_reply(
                f"AMessenger setup failed: the profile .env could not be read ({error}); "
                "fix its permissions, then type /amsg setup again.",
                source,
                key_supplied=parsed["key"] is not None,
            )

        key_supplied = parsed["key"] is not None
        key = parsed["key"].strip() if key_supplied else _stored_setup_value(
            profile_values,
            "REDMINE_API_KEY",
            "AMESSENGER_KEY",
        )
        relay_url = (
            parsed["relay"].strip()
            if parsed["relay"] is not None
            else _stored_setup_value(profile_values, "AMESSENGER_URL")
        )
        if not relay_url:
            return _setup_reply(
                SETUP_NO_RELAY,
                source,
                key_supplied=key_supplied,
                secret=key if key_supplied else "",
            )
        if not key:
            return _setup_reply(
                "AMessenger setup failed: no key was found; put your Redmine API key "
                "in REDMINE_API_KEY in the profile .env (or AMESSENGER_KEY), then "
                "type /amsg setup again.",
                source,
                key_supplied=key_supplied,
            )
        url = relay_url.rstrip("/")
        owner_chat = f"{platform}:{chat_id}"
        values = {
            "AMESSENGER_URL": url,
            "AMESSENGER_KEY": key,
            "AMESSENGER_AGENT": agent,
            "AMESSENGER_KIND": kind,
            "AMESSENGER_OWNER_CHAT": owner_chat,
        }
        clear = ()
        if str(getattr(source, "chat_type", "") or "").casefold() in {
            "group", "forum", "channel"
        }:
            owner_user = str(user_id or "").strip()
            if not owner_user:
                return _setup_reply(
                    "AMessenger setup failed: this group event has no Owner user id; "
                    "type /amsg setup from a group message that includes your user id.",
                    source,
                    key_supplied=key_supplied,
                    secret=key if key_supplied else "",
                )
            values["AMESSENGER_OWNER_USER"] = owner_user
        else:
            clear = ("AMESSENGER_OWNER_USER",)

        old_owner_chat = str(read_settings().get("owner_chat") or "").strip()
        if (
            old_owner_chat
            and old_owner_chat != owner_chat
            and adapter_module.configuration_state() == "configured"
        ):
            if adapter is None:
                return SETUP_TUI
            if (
                isinstance(pending, dict)
                and not positionals
                and parsed["key"] is None
                and parsed["relay"] is None
            ):
                return _setup_reply(
                    _setup_move_message(old_owner_chat, owner_chat),
                    source,
                    key_supplied=bool(pending.get("key_supplied")),
                    secret=(
                        str(pending["values"].get("AMESSENGER_KEY") or "")
                        if pending.get("key_supplied")
                        else ""
                    ),
                )
            adapter._pending_setup = {
                "values": values,
                "clear": clear,
                "key_supplied": key_supplied,
            }
            return _setup_reply(
                _setup_move_message(old_owner_chat, owner_chat),
                source,
                key_supplied=key_supplied,
                secret=key if key_supplied else "",
            )

    if adapter is None:
        return SETUP_TUI
    env_values = dict(values)
    try:
        adapter_module.update_profile_env(env_values, clear=clear)
    except Exception as error:
        return _setup_reply(
            f"AMessenger setup failed: the profile .env is not writable ({error}); "
            "fix its permissions or disk space, then type /amsg setup again.",
            source,
            key_supplied=key_supplied,
            secret=key if key_supplied else "",
        )
    for name, value in env_values.items():
        os.environ[name] = value
    for name in clear:
        os.environ[name] = ""
    adapter._pending_setup = None
    try:
        await adapter.reload_configuration()
    except Exception as error:
        return _setup_reply(
            "AMessenger setup failed while applying the new configuration "
            f"({_redact_setup_secret(error, key if key_supplied else '')}); "
            "fix that cause and type /amsg setup again.",
            source,
            key_supplied=key_supplied,
            secret=key if key_supplied else "",
        )
    url = str(env_values.get("AMESSENGER_URL") or read_settings().get("url") or "")
    try:
        card = await adapter.publish_card()
    except Exception as error:
        return _setup_reply(
            _setup_relay_error(url, error, key if key_supplied else ""),
            source,
            key_supplied=key_supplied,
            secret=key if key_supplied else "",
        )
    return _setup_reply(
        _setup_card_reply(card),
        source,
        key_supplied=key_supplied,
        secret=key if key_supplied else "",
    )


@asynccontextmanager
async def relay_client(adapter):
    """Use the adapter client in a gateway and a process-local client in TUI/CLI."""
    if adapter is None:
        client = adapter_module.build_relay_client()
        close_client = True
    else:
        client = adapter.client()
        close_client = False
    try:
        yield client
    finally:
        if close_client:
            await client.aclose()


def _read_state(adapter) -> dict:
    return adapter.state() if adapter is not None else adapter_module.read_state_file()


def _update_state(adapter, change) -> dict:
    if adapter is not None:
        return adapter.update_state(change)
    return adapter_module.update_state_file(change)


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


async def resolve_channel(adapter, token) -> tuple[dict | None, str | None]:
    """Resolve an Owner-facing Channel name, prefix, or exact topic."""
    try:
        async with relay_client(adapter) as client:
            channels = await relay.list_channels(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return None, _relay_failure("list Channels", error)

    pending_invites = _read_state(adapter).get("pending_invites", {})
    combined = []
    seen_ids = set()
    for channel in [*channels, *pending_invites.values()]:
        if not isinstance(channel, dict):
            continue
        channel_id = channel.get("id")
        if channel_id in seen_ids:
            continue
        seen_ids.add(channel_id)
        combined.append(channel)
    channel, error = relay.resolve_channel(combined, token)
    if channel is not None or not (error or "").startswith("No Channel here is named "):
        return channel, error

    current_ids = {channel.get("id") for channel in channels}
    stale = []
    document = _read_state(adapter)
    pending_ids = set(document.get("pending_invites", {}))
    for channel_id in document.get("channels", {}):
        if channel_id in current_ids or channel_id in pending_ids:
            continue
        if adapter is not None:
            stale.append(adapter.known_channel(channel_id))
        else:
            record = document.get("channels", {}).get(channel_id, {})
            stale.append(
                {
                    "id": channel_id,
                    "name": record.get("name"),
                    "topic": record.get("topic"),
                }
            )
    forgotten, _forgotten_error = relay.resolve_channel(stale, token)
    if forgotten is None:
        return channel, error
    _drop_forgotten_channel(adapter, forgotten["id"])
    return None, relay.CHANNEL_GONE


def _relay_failure(action: str, error: Exception) -> str:
    if isinstance(error, relay.RelayRejected):
        return (
            f"Could not {action}: the relay rejected the request ({error.detail}); "
            "fix the named Channel, Agent, or permission, then retry."
        )
    return (
        f"Could not {action}: the relay did not answer ({error}); check "
        "AMESSENGER_URL and relay health, then retry."
    )


def _drop_forgotten_channel(adapter, channel_id: str) -> None:
    _update_state(adapter, lambda document: state.drop_channel(document, channel_id))
    if adapter is not None:
        adapter._channels.pop(channel_id, None)


def _channel_token(tokens: list[str]) -> str | None:
    if len(tokens) != 2 or not tokens[1]:
        return None
    return tokens[1]


def _log_argument_error() -> str:
    return (
        "Expected `/amsg log [n]`, where n is a positive number from 1 to "
        f"{state.OWNER_LOG_MAX_LINES}."
    )


def _log_limit(tokens: list[str]) -> int | None:
    if len(tokens) == 1:
        return 20
    if len(tokens) != 2 or re.fullmatch(r"[0-9]+", tokens[1]) is None:
        return None
    if len(tokens[1]) > len(str(state.OWNER_LOG_MAX_LINES)):
        return None
    value = int(tokens[1])
    if value <= 0 or value > state.OWNER_LOG_MAX_LINES:
        return None
    return value


async def _log(tokens: list[str]) -> str:
    limit = _log_limit(tokens)
    if limit is None:
        return _log_argument_error()

    try:
        entries = state.read_owner_log(adapter_module.owner_log_path_for_process(), limit)
    except OSError as error:
        logger.warning("[amessenger] could not read Owner log: %s", error)
        return (
            "The Owner log could not be read because owner_log.jsonl is unavailable; "
            "fix its permissions or disk space, then run /amsg log again."
        )
    if not entries:
        return "The Owner log is empty; nothing to do."

    try:
        document = adapter_module.read_state_file()
        if document.get(state.AUTHENTICITY_SECRET_KEY) is None:
            document = adapter_module.update_state_file(
                state.ensure_authenticity_secret
            )
        mark = mirror.authenticity_mark(
            document[state.AUTHENTICITY_SECRET_KEY]
        )
    except (OSError, state.StateFileCorrupt, KeyError) as error:
        logger.warning("[amessenger] could not load Owner log mark: %s", error)
        return (
            "The Owner log could not be shown because state.json's authenticity mark "
            "is unavailable; fix state.json, then run /amsg log again."
        )
    return "\n".join(f"{entry['text']} {mark}" for entry in entries)


async def _join(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return _argument_error("join")
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    pending_invite = channel["id"] in _read_state(adapter).get("pending_invites", {})
    agent_name = read_settings().get("agent", "")
    if any(
        isinstance(member, dict)
        and member.get("agent") == agent_name
        and member.get("state") == "member"
        for member in channel.get("members", [])
    ):
        if pending_invite:
            _update_state(
                adapter,
                lambda document: state.drop_pending_invite(
                    document, channel["id"]
                ),
            )
        return f"{mirror.label(channel)} is already a member."
    try:
        async with relay_client(adapter) as client:
            await relay.join(client, channel["id"])
    except (relay.RelayRejected, relay.RelayUnavailable) as caught:
        if isinstance(caught, relay.RelayRejected) and caught.status == 404:
            _update_state(
                adapter,
                lambda document: state.drop_channel(document, channel["id"]),
            )
            if pending_invite:
                return "That Invite was withdrawn or the Channel was closed."
            return relay.CHANNEL_GONE
        return _relay_failure("join the Channel", caught)
    if pending_invite:
        _update_state(
            adapter,
            lambda document: state.drop_pending_invite(document, channel["id"]),
        )
    return f"Joined {mirror.label(channel)}. Messages in it will be mirrored here."


async def _interact(adapter, tokens: list[str], help_text: str) -> str:
    if len(tokens) < 2:
        return _argument_error("interact")
    if parse_interact(tokens[2:]) is None:
        return _argument_error("interact")
    channel, error = await resolve_channel(adapter, tokens[1])
    if error is not None:
        return error
    if adapter is not None:
        adapter.remember_channel(channel)
    parsed = parse_interact(tokens[2:])
    kind, duration_seconds, level = parsed
    approval_mode = None
    if level == "full":
        approval_mode = adapter_module.approval_mode()
        if approval_mode != "manual":
            logger.warning(
                "[amessenger] refused full Tool Level for Channel %s: "
                "approvals.mode=%r is not manual",
                channel["id"],
                approval_mode,
            )
            level = "base"
    moment = state.now()
    updated = _update_state(
        adapter,
        lambda document: state.grant(
            document,
            channel["id"],
            kind=kind,
            level=level,
            duration_seconds=duration_seconds,
            moment=moment,
        )
    )
    record = state.channel(updated, channel["id"])
    grant_period = (
        "standing" if record["expires_at"] is None else f"until {record['expires_at']}"
    )
    if approval_mode is not None and approval_mode != "manual":
        return (
            f"{mirror.label(channel)} is now interact, Tool Level base, {grant_period}.\n"
            f"I refused the full Tool Level: this gateway has approvals.mode "
            f"'{approval_mode}', so a dangerous command from a peer would be approved "
            "by a model instead of by you. To use full, set approvals.mode: manual "
            "in config.yaml and restart the gateway, then grant it again.\n"
            f"End it any time with /amsg notify {mirror.channel_name(channel)}."
        )
    return (
        f"{mirror.label(channel)} is now interact, Tool Level {level}, {grant_period}. "
        f"End it any time with `/amsg notify {mirror.channel_name(channel)}`."
    )


async def _notify(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return _argument_error("notify")
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    record = state.channel(_read_state(adapter), channel["id"], state.now())
    if record["policy"] == "notify":
        return f"{mirror.label(channel)} is already notify."
    _update_state(adapter, lambda document: state.revoke(document, channel["id"]))
    return (
        f"{mirror.label(channel)} is back to notify. I will show you its Messages "
        "and do nothing else."
    )


async def _leave(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return _argument_error("leave")
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    try:
        async with relay_client(adapter) as client:
            await relay.leave(client, channel["id"])
    except (relay.RelayRejected, relay.RelayUnavailable) as caught:
        if isinstance(caught, relay.RelayRejected) and caught.status == 404:
            _drop_forgotten_channel(adapter, channel["id"])
            return relay.CHANNEL_GONE
        return _relay_failure("leave the Channel", caught)
    _update_state(adapter, lambda document: state.revoke(document, channel["id"]))
    return f"Left {mirror.label(channel)}."


def _is_invited(channel: dict, agent_name: str) -> bool:
    return any(
        member.get("agent") == agent_name and member.get("state") == "invited"
        for member in channel.get("members", [])
        if isinstance(member, dict)
    )


def _argument_error(command: str) -> str:
    accepted = {
        "join": "<ch>",
        "interact": "1h, 5h, Nh, Nm, always, full",
        "notify": "<ch>",
        "leave": "<ch>",
        "status": "no arguments",
        "approve": "[name]",
        "deny": "[name]",
        "help": "no arguments",
    }
    return f"Accepted forms for {command}: {accepted[command]}."


async def _status(adapter) -> str:
    try:
        async with relay_client(adapter) as client:
            channels = await relay.list_channels(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return _relay_failure("list Channels", error)
    if not channels:
        return "No Channels yet; nothing to do."

    agent_name = read_settings().get("agent", "")
    lines = []
    for channel in sorted(channels, key=mirror.label):
        label = mirror.label(channel)
        if _is_invited(channel, agent_name):
            lines.append(
                f"{label} — invited. Join with /amsg join {mirror.channel_name(channel)}"
            )
            continue
        record = state.channel(_read_state(adapter), channel["id"])
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


def _pending_label(adapter, channel_id, entry=None) -> str:
    if adapter is not None:
        channel = _pending_channel(adapter, channel_id, entry)
        return mirror.label(channel)
    channel = _pending_channel(adapter, channel_id, entry)
    return mirror.label(channel)


def _pending_channel(adapter, channel_id, entry=None) -> dict:
    if adapter is not None:
        channel = adapter.known_channel(channel_id)
    else:
        record = _read_state(adapter).get("channels", {}).get(channel_id, {})
        channel = {
            "id": channel_id,
            "name": record.get("name"),
            "topic": record.get("topic"),
        }
    if isinstance(entry, dict):
        if channel.get("name") is None and "name" in entry:
            channel["name"] = entry.get("name")
        if channel.get("topic") is None and "topic" in entry:
            channel["topic"] = entry.get("topic")
    return channel


def _pending_entries(adapter) -> list[tuple[str, dict]]:
    pending = _read_state(adapter)["pending_approvals"]
    return sorted(
        pending.items(),
        key=lambda item: (item[1]["created_at"], item[0]),
    )


def _waiting_approvals_message(
    adapter, choice: str, entries: list[tuple[str, dict]]
) -> str:
    descriptions = []
    for _session_key, entry in entries:
        channel_id = entry["chat_id"]
        channel = _pending_channel(adapter, channel_id, entry)
        descriptions.append(f"`{mirror.label(channel)}`")
    example_name = mirror.channel_name(
        _pending_channel(adapter, entries[0][1]["chat_id"], entries[0][1])
    )
    return (
        f"There are {len(entries)} waiting: {', '.join(descriptions)}. "
        f"Say which, for example `/amsg {choice} {example_name}`."
    )


async def _approval(adapter, choice: str, approval_name: str | None = None) -> str:
    entries = _pending_entries(adapter)
    if not entries:
        return "Nothing is waiting for your approval."

    if approval_name is None:
        if len(entries) != 1:
            return _waiting_approvals_message(adapter, choice, entries)
        selected = entries[0]
    else:
        pending_channels = []
        for session_key, entry in entries:
            channel = _pending_channel(adapter, entry["chat_id"], entry)
            pending_channels.append({**channel, "_session_key": session_key})
        selected_channel, resolution_error = relay.resolve_channel(
            pending_channels, approval_name
        )
        matches = []
        if selected_channel is not None:
            session_key = selected_channel.get("_session_key")
            matches = [item for item in entries if item[0] == session_key]
        elif resolution_error is not None and not resolution_error.startswith(
            "No Channel here is named "
        ):
            return resolution_error
        if not matches:
            return (
                f"No pending approval matches `{approval_name}`; use a Channel name shown by "
                "/amsg approve or /amsg deny."
            )
        if len(matches) != 1:
            return _waiting_approvals_message(adapter, choice, matches)
        selected = matches[0]

    session_key, entry = selected
    if adapter is None:
        try:
            adapter_module.append_pending_decision_file(entry["chat_id"], choice)
        except (OSError, ValueError, RuntimeError) as error:
            logger.warning("[amessenger] could not record approval decision: %s", error)
            command = "approve" if choice == "once" else "deny"
            return (
                "Could not record the approval decision because "
                "pending_decisions.jsonl could not be written; fix its permissions "
                f"or disk space, then run /amsg {command} again."
            )
        return "recorded; the gateway applies it within half a minute"

    try:
        from tools.approval import resolve_gateway_approval
    except ImportError:
        return (
            "Approvals are unavailable because this Hermes build lacks "
            "tools.approval; upgrade Hermes, then run /amsg approve or /amsg deny again."
        )

    popped = None

    def pop_approval(document):
        nonlocal popped
        updated, popped = state.pop_pending_approval(
            document, session_key=session_key
        )
        return updated

    adapter.update_state(pop_approval)
    if popped is None:
        return "Nothing is waiting for your approval."
    resolved = resolve_gateway_approval(popped["session_key"], choice)
    label = _pending_label(adapter, popped["chat_id"], popped)
    return f"Resolved {resolved} approval(s) for Channel {label}."


def make_handler():
    """Return the async callable registered by Hermes as ``/amsg``."""
    async def handle(raw_args: str) -> str:
        source = _SOURCE.get()
        adapter = active_adapter()
        tokens = (raw_args or "").split()
        command = tokens[0] if tokens else ""
        if command == "setup":
            if not in_gateway_process():
                return await _setup(None, tokens, source)
            if not _setup_owner_check(adapter, source):
                _log_refusal(source)
                return REFUSAL
            return await _setup(adapter, tokens, source)

        if adapter_module.configuration_state() != "configured":
            return _not_setup_message()
        if not owner_check(adapter, source):
            _log_refusal(source)
            if missing_group_owner_user(adapter, source):
                return f"{REFUSAL}\n{MISSING_GROUP_OWNER_USER_HINT}"
            return REFUSAL

        from . import HELP_TEXT

        if not tokens or tokens[0] == "help":
            if not tokens or len(tokens) == 1:
                return HELP_TEXT
            return _argument_error("help")
        command = tokens[0]
        if command == "join":
            return await _join(adapter, tokens, HELP_TEXT)
        if command == "interact":
            return await _interact(adapter, tokens, HELP_TEXT)
        if command == "notify":
            return await _notify(adapter, tokens, HELP_TEXT)
        if command == "leave":
            return await _leave(adapter, tokens, HELP_TEXT)
        if command == "log":
            return await _log(tokens)
        if command == "status":
            if len(tokens) != 1:
                return _argument_error("status")
            return await _status(adapter)
        if command in {"approve", "deny"}:
            if len(tokens) not in {1, 2}:
                return _argument_error(command)
            approval_name = tokens[1] if len(tokens) == 2 else None
            choice = "once" if command == "approve" else "deny"
            return await _approval(adapter, choice, approval_name)
        return HELP_TEXT

    return handle
