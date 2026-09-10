"""Owner-only ``/amsg`` commands."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
import dataclasses
import logging
import os
import re

from . import adapter as adapter_module
from . import bootstrap, defaults, health, mirror, relay, security, state, telegram
from .adapter import active_adapter, read_settings


logger = logging.getLogger("amessenger")
REFUSAL = "AMessenger commands are accepted only from the Owner in the Owner Chat."
MISSING_GROUP_OWNER_USER_HINT = (
    "AMessenger cannot identify the Owner in this group Owner Chat.\n"
    "Reason: AMESSENGER_OWNER_USER is not set.\n"
    "Set it to the Owner's platform id, then restart the gateway."
)
SETUP_TUI = (
    "AMessenger setup did not run.\n"
    "This console cannot receive Messages.\n"
    "Nothing was written.\n\n"
    "Use the Telegram, Google Chat, or IRC Owner Chat where Messages should arrive:\n"
    "/amsg setup"
)
SETUP_KIND_FORMS = "corporate or personal"
NO_OWNER_CHAT_YET = (
    "AMessenger cannot receive Messages.\n"
    "Reason: the Owner Chat has not been selected.\n"
    "No other AMessenger command works until then.\n\n"
    "In the Owner Chat, use one of:\n"
    "/amsg setup\n"
    "/sethome"
)
SETUP_NO_RELAY = (
    "AMessenger setup did not run.\n"
    "Reason: no relay address is configured.\n"
    "Ask your administrator for the relay URL.\n\n"
    "Then run:\n"
    "/amsg setup --relay <url>"
)
SETUP_GATEWAY_SOURCE = (
    "AMessenger setup did not run.\n"
    "Reason: this event has no usable platform or chat id.\n"
    "The Owner Chat could not be identified.\n"
    "Nothing was written.\n\n"
    "Send a real chat message containing:\n"
    "/amsg setup"
)
SETUP_KEY_GROUP_WARNING = (
    "Security warning: everyone in this group can see the key you typed.\n"
    "Use --key only in a private Owner Chat with this Agent."
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


def _setup_gateway_adapter_message() -> str:
    missing = adapter_module.missing_requirements()
    if missing:
        if adapter_module.bootstrap_pending():
            # Everything still missing is derived on the first start after the
            # install. Naming a shell command for this state is what sent an
            # Owner to an operator for a profile that already had what it needed.
            return (
                "AMessenger is not running in this gateway yet.\n"
                "Reason: the plugin was installed after this gateway started.\n"
                "It configures itself on the next start: the Agent is named "
                "after the Owner of the key this profile already holds, and "
                "Messages arrive in the home channel.\n\n"
                "Restart the gateway."
            )
        if not adapter_module.owner_key():
            return bootstrap.NO_OWNER_KEY
        safe_missing = security.safe_field(", ".join(missing))
        # Not "/amsg setup". Hermes refuses to register the platform for a
        # profile in this state, so there is no adapter for setup to write
        # through, and telling the Owner to type it is a loop they cannot leave.
        # The remedy is a shell one, and an interrupted install lands here.
        return (
            "AMessenger is not running in this gateway.\n"
            f"Reason: the profile is missing {safe_missing}.\n"
            "Hermes drops the AMessenger platform for a profile in this state, "
            "so no chat command can repair it.\n\n"
            "Ask the operator to finish the installation on this host:\n"
            "bash install.sh -p <profile> --relay <url> --owner-chat <platform>"
        )
    problem = adapter_module.last_connect_problem()
    if problem:
        # Naming the fault is the whole point: "restart the gateway" sends an
        # Owner into a loop a restart can never end.
        return (
            "AMessenger cannot start with this Hermes profile.\n"
            f"Reason: {security.safe_field(problem, limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Fix that value, then restart the gateway.\n"
            "Restarting without the fix will not help.\n\n"
            "Then run:\n"
            "/amsg setup"
        )
    problem = adapter_module.receive_problem()
    if problem:
        # The loop stopped after a successful connect (for example the Agent
        # name now belongs to another Owner); the stop reason is the cause.
        return (
            "AMessenger cannot receive Messages in this gateway.\n"
            f"Reason: {security.safe_field(problem, limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Fix that cause, then restart the gateway."
        )
    return (
        "AMessenger is running, but its adapter is unavailable.\n"
        "No reason was reported.\n"
        "Restart the gateway.\n"
        "If this repeats, ask the operator to inspect the line beginning:\n"
        "[amessenger] not connecting:"
    )


def _may_claim_owner_chat(adapter, source) -> bool:
    """A platform-only Owner Chat is claimed by a private chat on that platform.

    This is a trust boundary: the chat id captured here becomes the Owner
    Chat. A group can never claim it unless AMESSENGER_OWNER_USER already
    names the sender. owner_check itself is untouched.
    """
    try:
        settings = read_settings()
        owner_user = str(settings.get("owner_user") or "").strip()
        same_platform = source.platform.value == adapter._owner_platform
        named_owner = bool(owner_user) and str(source.user_id) == owner_user
        return same_platform and (source.chat_type == "dm" or named_owner)
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False


def _setup_owner_check(adapter, source) -> bool:
    """Allow first setup, and let the current Owner request a move."""
    if adapter is None:
        return _setup_source_is_usable(source)
    if adapter_module.configuration_state() != "configured":
        return _setup_source_is_usable(source)
    if getattr(adapter, "_waiting_for_owner_chat", False):
        return _may_claim_owner_chat(adapter, source)
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
    missing = adapter_module.missing_requirements()
    cause = (
        "missing " + ", ".join(missing)
        if missing
        else "the Owner Chat and Agent are not configured"
    )
    return (
        "AMessenger is not set up.\n"
        f"Reason: {security.safe_field(cause)}\n\n"
        "In the intended Owner Chat, run:\n"
        "/amsg setup"
    )


def _setup_agent_error(agent: str) -> str:
    return (
        "AMessenger setup did not run.\n"
        f"Agent: {security.safe_field(agent, fallback='')}\n"
        "Reason: the Agent name must match [a-z0-9][a-z0-9-]{1,31}.\n\n"
        "For example:\n"
        "/amsg setup my-agent"
    )


def _setup_argument_error() -> str:
    return (
        "That setup command is not valid.\n\n"
        "Use:\n"
        "/amsg setup [name] [corporate|personal] [--key <key>] [--relay <url>]\n\n"
        "To confirm an Owner Chat move:\n"
        "/amsg setup --confirm"
    )


def _setup_card_reply(card: dict) -> str:
    return (
        "AMessenger setup is complete.\n"
        "Owner Chat: this chat\n\n"
        f"{mirror.format_card(card)}\n\n"
        "For help:\n"
        "/amsg help"
    )


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
            reply = f"{reply}\n\n{SETUP_KEY_GROUP_WARNING}"
    return _redact_setup_secret(reply, secret)


def _setup_move_message(old_owner_chat: str, owner_chat: str) -> str:
    return (
        "The Owner Chat has not moved.\n"
        f"Current Owner Chat: {security.safe_field(old_owner_chat, limit=security.DIAGNOSTIC_LIMIT)}\n"
        f"Requested Owner Chat: {security.safe_field(owner_chat, limit=security.DIAGNOSTIC_LIMIT)}\n\n"
        "To confirm the move:\n"
        "/amsg setup --confirm\n"
        "Do not confirm to leave the Owner Chat unchanged."
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


def _setup_relay_error(
    agent: str, url: str, error: Exception, secret: str = ""
) -> str:
    safe_agent = security.safe_field(agent, fallback="")
    safe_url = security.safe_field(_redact_setup_secret(url, secret), limit=security.DIAGNOSTIC_LIMIT)
    if isinstance(error, relay.CardConflict) or (
        isinstance(error, relay.RelayRejected) and error.status == 409
    ):
        return (
            "AMessenger setup did not finish.\n"
            f"Agent: {safe_agent}\n"
            f"Relay: {safe_url}\n"
            "Reason: another Owner already uses this Agent name.\n\n"
            "Choose another Agent name:\n"
            "/amsg setup <agent-name> [corporate|personal]"
        )
    if isinstance(error, relay.RelayRejected):
        if error.status in {401, 403}:
            return (
                "AMessenger setup did not finish.\n"
                f"Relay: {safe_url}\n"
                "Reason: the relay rejected the Owner key.\n"
                "Put a valid Redmine API key in REDMINE_API_KEY or AMESSENGER_KEY.\n\n"
                "Then run:\n"
                "/amsg setup"
            )
        safe_detail = security.safe_field(
            _redact_setup_secret(error.detail, secret)
        )
        return (
            "AMessenger setup did not finish.\n"
            f"Relay: {safe_url}\n"
            f"Reason: {safe_detail}\n"
            "Fix this cause.\n\n"
            "Then run:\n"
            "/amsg setup"
        )
    safe_error = security.safe_field(_redact_setup_secret(error, secret), limit=security.DIAGNOSTIC_LIMIT)
    return (
        "AMessenger setup did not finish.\n"
        f"Relay: {safe_url}\n"
        f"Reason: the relay is unreachable: {safe_error}\n\n"
        "For a container, check the outbound proxy and NO_PROXY.\n"
        "A bare IP is normally proxied; host.docker.internal is normally exempt.\n"
        "Check relay health or set a reachable relay:\n"
        "/amsg relay <url>\n\n"
        "Then run:\n"
        "/amsg setup"
    )


async def _relay(adapter, tokens: list[str]) -> str:
    """Show or move the relay address this Agent talks to.

    Owner-only by construction: the dispatcher runs the Owner check before
    reaching here. Pointing an Agent at another relay redirects every Message
    it sends and receives, so it is a trust decision, never a Tool.
    """
    if len(tokens) == 1:
        current = adapter_module.relay_url()
        shipped = defaults.relay_url()
        source = "shipped with the plugin" if current == shipped else "set for this profile"
        return (
            "Relay address\n"
            f"URL: {security.safe_field(current, limit=security.DIAGNOSTIC_LIMIT)}\n"
            f"Source: {source}\n\n"
            "To move this Agent:\n"
            "/amsg relay <url>"
        )
    if len(tokens) != 2:
        return (
            "That relay command is not valid.\n\n"
            "To show the current relay:\n"
            "/amsg relay\n\n"
            "To move this Agent:\n"
            "/amsg relay <url>"
        )
    url = tokens[1].strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return (
            "Nothing was changed.\n"
            f"Value: {security.safe_field(tokens[1])}\n"
            "Reason: a relay address must start with http:// or https://."
        )

    previous = adapter_module.relay_url()
    if url == previous:
        return (
            "Nothing changed.\n"
            f"Relay: {security.safe_field(url, limit=security.DIAGNOSTIC_LIMIT)}\n"
            "This Agent already uses that relay."
        )
    try:
        adapter_module.update_profile_env({"AMESSENGER_URL": url})
    except Exception as error:
        return (
            "The relay address was not saved.\n"
            "Reason: the profile .env is not writable: "
            f"{security.safe_field(str(error), limit=security.DIAGNOSTIC_LIMIT)}\n"
            f"Current relay: {security.safe_field(previous, limit=security.DIAGNOSTIC_LIMIT)}"
        )
    os.environ["AMESSENGER_URL"] = url
    try:
        await adapter.reload_configuration()
        card = await adapter.publish_card()
    except Exception as error:
        return (
            "The relay address was saved, but the Card was not published.\n"
            f"Relay: {security.safe_field(url, limit=security.DIAGNOSTIC_LIMIT)}\n"
            f"Reason: {security.safe_field(str(error), limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Check that the relay is reachable.\n\n"
            "To inspect the saved address:\n"
            "/amsg relay"
        )
    published = mirror.format_card(card) if card else "The Card is not published yet."
    return (
        "Relay moved.\n"
        f"Previous relay: {security.safe_field(previous, limit=security.DIAGNOSTIC_LIMIT)}\n"
        f"Current relay: {security.safe_field(url, limit=security.DIAGNOSTIC_LIMIT)}\n\n"
        f"{published}"
    )


async def _setup(adapter, tokens: list[str], source) -> str:
    if not in_gateway_process():
        return SETUP_TUI
    if not _setup_source_is_usable(source):
        return SETUP_GATEWAY_SOURCE
    if adapter is None:
        return _setup_gateway_adapter_message()
    parsed = _parse_setup_arguments(tokens)
    if parsed is None:
        return _setup_argument_error()

    pending = getattr(adapter, "_pending_setup", None) if adapter is not None else None
    claim = None
    if parsed["confirm"]:
        if (
            parsed["positionals"]
            or parsed["key"] is not None
            or parsed["relay"] is not None
        ):
            return _setup_argument_error()
        if not isinstance(pending, dict):
            return (
                "No Owner Chat move is waiting for confirmation.\n\n"
                "To start setup in this chat:\n"
                "/amsg setup"
            )
        values = dict(pending["values"])
        clear = tuple(pending.get("clear", ()))
        key_supplied = bool(pending.get("key_supplied"))
        key = str(values.get("AMESSENGER_KEY") or "")
    else:
        positionals = parsed["positionals"]
        stored = read_settings()
        configured = adapter_module.configuration_state() == "configured"
        # A configured profile keeps its Agent name and Kind: the installer
        # named the Agent after the Owner, and setup only adds the chat. An
        # empty name here means "derive it", which needs the key and cannot
        # happen until both are resolved below.
        agent = positionals[0] if positionals else (
            stored["agent"] if configured and stored.get("agent") else ""
        )
        kind = positionals[1] if len(positionals) == 2 else (
            stored["kind"] if configured and not positionals and stored.get("kind")
            else "corporate"
        )
        if agent and re.fullmatch(adapter_module.AGENT_NAME_PATTERN, agent) is None:
            return _setup_agent_error(agent)
        if kind not in adapter_module.KINDS:
            return (
                "AMessenger setup did not run.\n"
                f"Kind: {security.safe_field(kind)}\n"
                f"Reason: Kind must be {SETUP_KIND_FORMS}.\n\n"
                "Try again:\n"
                f"/amsg setup {security.safe_field(agent)} corporate"
            )
        platform, chat_id, user_id = _source_details(source)
        try:
            profile_values = adapter_module._profile_env_values()
        except OSError as error:
            return _setup_reply(
                "AMessenger setup did not run.\n"
                "Reason: the profile .env could not be read: "
                f"{security.safe_field(_redact_setup_secret(error, parsed['key'] or ''), limit=security.DIAGNOSTIC_LIMIT)}\n"
                "Fix access to the file.\n\n"
                "Then run:\n"
                "/amsg setup",
                source,
                key_supplied=parsed["key"] is not None,
                secret=parsed["key"] or "",
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
            or adapter_module.relay_url()
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
                "AMessenger setup did not run.\n"
                "Reason: no Owner key was found.\n"
                "Add the Redmine API key to REDMINE_API_KEY or AMESSENGER_KEY "
                "in the profile .env.\n\n"
                "Then run:\n"
                "/amsg setup",
                source,
                key_supplied=key_supplied,
            )
        url = relay_url.rstrip("/")
        if not agent:
            agent, refusal = await bootstrap.agent_named_after_the_owner(adapter, url, key)
            if refusal:
                return _setup_reply(
                    refusal,
                    source,
                    key_supplied=key_supplied,
                    secret=key if key_supplied else "",
                )
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
                    "AMessenger setup did not run.\n"
                    "Reason: this group event does not identify the Owner.\n"
                    "Send a new group message containing:\n"
                    "/amsg setup",
                    source,
                    key_supplied=key_supplied,
                    secret=key if key_supplied else "",
                )
            values["AMESSENGER_OWNER_USER"] = owner_user
        else:
            clear = ("AMESSENGER_OWNER_USER",)

        old_owner_chat = str(read_settings().get("owner_chat") or "").strip()
        old_platform, old_chat_id = adapter_module.parse_owner_chat(old_owner_chat)
        # Adding the chat id to a platform-only Owner Chat is the first setup,
        # not a move; nothing was shown anywhere before.
        claiming = bool(old_owner_chat) and not old_chat_id and old_platform == platform
        if claiming:
            claim = (platform, chat_id, user_id)
        if (
            old_owner_chat
            and old_owner_chat != owner_chat
            and not claiming
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

    env_values = dict(values)
    try:
        adapter_module.update_profile_env(env_values, clear=clear)
    except Exception as error:
        return _setup_reply(
            "AMessenger setup did not run.\n"
            "Reason: the profile .env is not writable: "
            f"{security.safe_field(_redact_setup_secret(error, key if key_supplied else ''), limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Fix file access or disk space.\n\n"
            "Then run:\n"
            "/amsg setup",
            source,
            key_supplied=key_supplied,
            secret=key if key_supplied else "",
        )
    for name, value in env_values.items():
        os.environ[name] = value
    for name in clear:
        os.environ[name] = ""
    if claim is not None:
        logger.warning(
            "[amessenger] Owner Chat claimed: platform=%s chat=%s user=%s",
            *claim,
        )
    adapter._pending_setup = None
    try:
        await adapter.reload_configuration()
    except Exception as error:
        return _setup_reply(
            "AMessenger setup was saved but could not be applied.\n"
            "Reason: "
            f"{security.safe_field(_redact_setup_secret(error, key if key_supplied else ''), limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Fix this cause.\n\n"
            "Then run:\n"
            "/amsg setup",
            source,
            key_supplied=key_supplied,
            secret=key if key_supplied else "",
        )
    url = str(env_values.get("AMESSENGER_URL") or read_settings().get("url") or "")
    try:
        card = await adapter.publish_card()
    except Exception as error:
        return _setup_reply(
            _setup_relay_error(
                env_values.get("AMESSENGER_AGENT", ""),
                url,
                error,
                key if key_supplied else "",
            ),
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
    # Asking for interact is asking for work to happen, so the Tool Level that
    # can do work is the default. `base` is the deliberate step down.
    level = None
    for token in values:
        if not isinstance(token, str):
            return None
        if token in ("full", "base"):
            if level is not None:
                return None
            level = token
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

    if level is None:
        level = "full"
    if duration is None or duration == "standing":
        return "standing", None, level
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
            f"Could not {action}.\n"
            "Reason: the relay rejected the request: "
            f"{security.safe_field(error.detail, limit=security.DIAGNOSTIC_LIMIT)}\n"
            "Check the named Channel, Agent, or Grant, then retry."
        )
    return (
        f"Could not {action}.\n"
        f"Reason: the relay did not answer: {security.safe_field(str(error), limit=security.DIAGNOSTIC_LIMIT)}\n"
        "Check AMESSENGER_URL and relay health, then retry."
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
        "That log command is not valid.\n"
        f"n must be a number from 1 to {state.OWNER_LOG_MAX_LINES}.\n\n"
        "Use:\n"
        "/amsg log [n]"
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
            "Saved Owner Chat lines could not be read.\n"
            "Reason: owner_log.jsonl is unavailable.\n"
            "Fix file access or disk space.\n\n"
            "Then run:\n"
            "/amsg log"
        )
    if not entries:
        return "No saved Owner Chat lines were found.\nNothing to do."

    try:
        document = adapter_module.read_state_file()
        if document.get(state.AUTHENTICITY_SECRET_KEY) is None:
            document = adapter_module.update_state_file(
                state.ensure_authenticity_secret
            )
        secret = document[state.AUTHENTICITY_SECRET_KEY]
    except (OSError, state.StateFileCorrupt, KeyError) as error:
        logger.warning("[amessenger] could not load Owner log mark: %s", error)
        return (
            "Saved Owner Chat lines could not be shown.\n"
            "Reason: the authenticity mark in state.json is unavailable.\n"
            "Fix state.json.\n\n"
            "Then run:\n"
            "/amsg log"
        )
    return "\n\n".join(
        mirror.with_mark(entry["text"], secret) for entry in entries
    )


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
        return (
            "Nothing changed.\n"
            f"Channel: {mirror.label(channel)}\n"
            "This Agent is already a Member."
        )
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
                return (
                    "Could not join the Channel.\n"
                    "The Invite was withdrawn or the Channel was closed."
                )
            return relay.CHANNEL_GONE
        return _relay_failure("join the Channel", caught)
    if pending_invite:
        _update_state(
            adapter,
            lambda document: state.drop_pending_invite(document, channel["id"]),
        )
    return (
        "Joined the Channel.\n"
        f"Channel: {mirror.label(channel)}\n"
        "Messages will be mirrored in this Owner Chat."
    )


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
    # A full Grant is never refused now. When nobody human answers the peer's
    # approvals, it is bounded instead, so the Owner gets the tools they asked
    # for and the exposure still ends on its own.
    bounded = ""
    bound_seconds = None
    if level == "full":
        block = adapter_module.unbounded_full_block(adapter, channel["id"])
        if block:
            cap = adapter_module.STANDING_FULL_MAX_SECONDS
            if kind == "standing":
                # Bound it, but leave it standing. Rewriting it as a single
                # Grant would also end it at the peer's first [TASK_DONE] and
                # after an idle hour, neither of which the Owner asked for.
                bound_seconds, bounded = cap, block
            elif duration_seconds > cap:
                duration_seconds, bounded = cap, block
            if bounded:
                logger.warning(
                    "[amessenger] bounded the full Grant for Channel %s to %ds: %s",
                    channel["id"],
                    cap,
                    "approvals.mode is not manual" if bounded == "mode"
                    else "an approval bypass is active",
                )
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
            bound_seconds=bound_seconds,
        )
    )
    record = state.channel(updated, channel["id"])
    grant_period = (
        "standing"
        if record["expires_at"] is None
        else f"until {mirror.human_time(record['expires_at'])}"
    )
    name = mirror.channel_name(channel)
    head = (
        "The Grant started.\n"
        f"Channel: {mirror.label(channel)}\n"
        "Mail Policy: interact\n"
        f"Tool Level: {level}\n"
        f"Grant: {grant_period}\n"
    )
    body = ""
    if bounded == "mode":
        body = (
            "\nYou asked for a Grant with no end.\n"
            "approvals.mode is not manual, so a model\n"
            "approves dangerous commands, not you.\n"
            "A Grant like that runs five hours.\n\n"
            "For a Grant with no end: set approvals.mode\n"
            "to manual, restart the gateway, grant again.\n"
        )
    elif bounded == "bypass":
        body = (
            "\nAn approval bypass is on. Commands from the\n"
            "peer Agent run with no check, and nobody is\n"
            "asked, not even a model. You chose this.\n"
            "A Grant like that runs five hours.\n"
        )
    elif level == "base":
        body = (
            "\nThe Agent answers and reads the web.\n"
            "No terminal, no files, no MCP.\n\n"
            "For full Tool Level:\n"
            f"/amsg interact {name}\n"
        )
    return f"{head}{body}\nTo end the Grant:\n/amsg notify {name}"


async def _notify(adapter, tokens: list[str], help_text: str) -> str:
    token = _channel_token(tokens)
    if token is None:
        return _argument_error("notify")
    channel, error = await resolve_channel(adapter, token)
    if error is not None:
        return error
    record = state.channel(_read_state(adapter), channel["id"], state.now())
    if record["policy"] == "notify":
        return (
            "Nothing changed.\n"
            f"Channel: {mirror.label(channel)}\n"
            "Mail Policy: notify"
        )
    _update_state(adapter, lambda document: state.revoke(document, channel["id"]))
    return (
        "The Grant ended.\n"
        f"Channel: {mirror.label(channel)}\n"
        "Mail Policy: notify\n"
        "The Agent will show each Mirror and do nothing else."
    )


async def _rename(adapter, tokens: list[str]) -> str:
    """Give a Channel a name the Owner chose. The relay allows only the Creator."""
    if len(tokens) != 3:
        return (
            "That rename command is not valid.\n"
            "A Channel name uses lower-case letters, digits, and hyphens.\n\n"
            "Use:\n"
            "/amsg rename <channel-name> <new-name>"
        )
    channel, error = await resolve_channel(adapter, tokens[1])
    if error is not None:
        return error
    new_name = tokens[2].strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,159}", new_name):
        return (
            "Nothing was changed.\n"
            f"Channel name: {security.safe_field(tokens[2])}\n"
            "Reason: a Channel name uses lower-case letters, digits, and hyphens."
        )
    old_label = mirror.label(channel)
    try:
        async with relay_client(adapter) as client:
            renamed = await relay.rename(client, channel["id"], new_name)
    except (relay.RelayRejected, relay.RelayUnavailable) as caught:
        if isinstance(caught, relay.RelayRejected):
            if caught.status == 404:
                _drop_forgotten_channel(adapter, channel["id"])
                return relay.CHANNEL_GONE
            if caught.status == 403:
                return (
                    "The Channel was not renamed.\n"
                    "Reason: only the Creator may rename a Channel.\n"
                    "Ask the Creator to run:\n"
                    f"/amsg rename {mirror.channel_name(channel)} {new_name}"
                )
            if caught.status == 409:
                return (
                    "The Channel was not renamed.\n"
                    f"Channel name: {security.safe_field(new_name)}\n"
                    "Reason: another Channel already uses that name.\n"
                    "Choose a different name."
                )
        return _relay_failure("rename the Channel", caught)
    # A TUI has no adapter; the state file is the shared record either way.
    _update_state(adapter, lambda document: state.remember_channel(document, renamed))
    if adapter is not None:
        adapter.remember_channel(renamed)
    return (
        "The Channel was renamed.\n"
        f"Previous Channel: {old_label}\n"
        f"Current Channel: {mirror.label(renamed)}\n"
        "Every Member was told.\n"
        "Use the current Channel name from now on."
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
    return f"Left the Channel.\nChannel: {mirror.label(channel)}"


def _is_invited(channel: dict, agent_name: str) -> bool:
    return any(
        member.get("agent") == agent_name and member.get("state") == "invited"
        for member in channel.get("members", [])
        if isinstance(member, dict)
    )


def _argument_error(command: str) -> str:
    accepted = {
        "join": "<channel-name>",
        "interact": "<channel-name> [Nh|Nm|always] [full]",
        "notify": "<channel-name>",
        "leave": "<channel-name>",
        "status": "",
        "approve": "[channel-name]",
        "deny": "[channel-name]",
        "help": "",
    }
    syntax = f"/amsg {command} {accepted[command]}".rstrip()
    result = f"That {command} command is not valid.\n\nUse:\n{syntax}"
    if command == "interact":
        result += (
            "\n\nFor example:\n"
            "/amsg interact deal-42 1h\n"
            "/amsg interact deal-42 always full"
        )
    return result


def _explained(report):
    """Say why no gateway could have written a record, when the profile says so.

    A profile with some AMessenger settings and not others is refused by Hermes
    at registration, so the gateway runs and AMessenger in it never starts.
    "No record" then reads exactly like a stopped gateway, and the remedy --
    which values are missing -- is one call away.
    """
    if report.fault_code != "no_record":
        return report
    missing = adapter_module.missing_requirements()
    if not missing or len(missing) == len(adapter_module.REQUIRED_ENV):
        return report
    return dataclasses.replace(
        report,
        fault_code="half_configured",
        fault=health.no_record_reason(missing),
    )


def local_health_block() -> str:
    """What this installation knows about itself, before anything is asked.

    Local first, and on purpose. `/amsg status` used to open with a relay call
    and return its error, so the one moment an Owner most needs to know whether
    their own gateway is receiving was the one moment the command refused to
    say. Every fact here is already in this process or in the profile.
    """
    adapter = active_adapter()
    if adapter is not None:
        return "AMessenger status\n" + health.render(adapter.health_report())
    report = _explained(health.read_snapshot(adapter_module.health_path_for_process()))
    if in_gateway_process():
        # A gateway with no live adapter: the record is this profile's own, and
        # its absence is the answer.
        return "AMessenger status\n" + health.render(report)
    # A console cannot receive. Saying "available" here would answer for a
    # gateway this process has never met.
    return (
        "AMessenger status\n"
        "This console does not receive Messages; a gateway does.\n"
        "The gateway last reported:\n"
        + health.render(report)
    )


async def _status(adapter) -> str:
    blocks = [local_health_block()]
    try:
        async with relay_client(adapter) as client:
            channels = await relay.list_channels(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        blocks.append(_relay_failure("list Channels", error))
        return "\n\n".join(blocks)
    if not channels:
        blocks.append("No Channels yet.\nNothing to do.")
        return "\n\n".join(blocks)

    agent_name = read_settings().get("agent", "")
    for channel in sorted(channels, key=mirror.label):
        label = mirror.label(channel)
        if _is_invited(channel, agent_name):
            blocks.append(
                f"Channel: {label}\n"
                "Invite: waiting\n"
                "To join:\n"
                f"/amsg join {mirror.channel_name(channel)}"
            )
            continue
        record = state.channel(_read_state(adapter), channel["id"])
        if record.get("grant") in {"single", "standing"}:
            period = (
                "standing"
                if record.get("expires_at") is None
                else f"until {mirror.human_time(record['expires_at'])}"
            )
            blocks.append(
                f"Channel: {label}\n"
                "Mail Policy: interact\n"
                f"Tool Level: {record['level']}\n"
                f"Grant: {period}"
            )
        else:
            blocks.append(f"Channel: {label}\nMail Policy: notify")
    return "\n\n".join(blocks)


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
        descriptions.append(mirror.label(channel))
    example_name = mirror.channel_name(
        _pending_channel(adapter, entries[0][1]["chat_id"], entries[0][1])
    )
    command = "approve" if choice == "once" else "deny"
    return (
        f"{len(entries)} approvals are waiting.\n"
        f"Channels: {', '.join(descriptions)}\n\n"
        "Choose one, for example:\n"
        f"/amsg {command} {example_name}"
    )


async def _approval(adapter, choice: str, approval_name: str | None = None) -> str:
    entries = _pending_entries(adapter)
    if not entries:
        return "No approval is waiting.\nNothing to do."

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
                "No pending approval matches this Channel.\n"
                f"Channel: {security.safe_field(approval_name)}\n\n"
                "Show the waiting approvals with one of:\n"
                "/amsg approve\n"
                "/amsg deny"
            )
        if len(matches) != 1:
            return _waiting_approvals_message(adapter, choice, matches)
        selected = matches[0]

    session_key, entry = selected
    channel_name = mirror.channel_name(
        _pending_channel(adapter, entry["chat_id"], entry)
    )
    if channel_name == mirror.UNKNOWN_CHANNEL_NAME:
        channel_name = "<channel-name>"
    if adapter is None:
        try:
            adapter_module.append_pending_decision_file(entry["chat_id"], choice)
        except (OSError, ValueError, RuntimeError) as error:
            logger.warning("[amessenger] could not record approval decision: %s", error)
            command = "approve" if choice == "once" else "deny"
            return (
                "The approval decision was not recorded.\n"
                "Reason: pending_decisions.jsonl could not be written.\n"
                "Fix file access or disk space.\n\n"
                "Then run:\n"
                f"/amsg {command} {channel_name}"
            )
        return (
            "The approval decision was recorded.\n"
            "The gateway will apply it within 30 seconds."
        )

    try:
        from tools.approval import resolve_gateway_approval
    except ImportError:
        return (
            "Approvals are unavailable.\n"
            "Reason: this Hermes build lacks tools.approval.\n"
            "Upgrade Hermes.\n\n"
            "Then use one of:\n"
            f"/amsg approve {channel_name}\n"
            f"/amsg deny {channel_name}"
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
        return "No approval is waiting.\nNothing to do."
    resolved = resolve_gateway_approval(popped["session_key"], choice)
    label = _pending_label(adapter, popped["chat_id"], popped)
    decision = "allow once" if choice == "once" else "refuse"
    if not resolved:
        # The prompt had already ended.  Saying the decision was applied would
        # tell the Owner a command was allowed or refused when none was.
        return (
            "The approval decision arrived too late.\n"
            f"Channel: {label}\n"
            f"Decision: {decision}\n"
            "No approval was waiting, so nothing was applied."
        )
    return (
        "The approval decision was applied.\n"
        f"Channel: {label}\n"
        f"Decision: {decision}\n"
        f"Approvals resolved: {resolved}"
    )


async def answer_in_owner_chat(adapter, source, answer):
    """Post a command's answer where Hermes would change it on the way.

    Hermes reads an answer as Markdown before it reaches Telegram, and
    ``/amsg log`` answers with Messages a peer wrote. Posting it here keeps
    every character the Owner is meant to read. Returning the answer instead
    hands it back to Hermes, which is what every other Owner Chat wants and
    what a failed post falls back to, so the Owner always sees something.
    """
    if not answer or adapter is None or source is None:
        return answer
    owner_platform = getattr(adapter, "_owner_platform", "")
    if not telegram.is_owner_chat(owner_platform):
        return answer
    owner_chat_id = str(getattr(adapter, "_owner_chat_id", "") or "")
    if not owner_chat_id:
        return answer
    try:
        asked_here = (
            source.platform.value == owner_platform
            and str(source.chat_id) == owner_chat_id
        )
    except AttributeError:
        return answer
    if not asked_here:
        # A refusal answers the chat that asked, which is not the Owner Chat.
        # A chat id alone does not say which platform it belongs to.
        return answer
    owner = adapter.owner_adapter
    if owner is None:
        return answer
    delivered, _total = await mirror.deliver(
        owner,
        owner_chat_id,
        answer,
        platform=owner_platform,
        thread_id=getattr(source, "thread_id", None),
    )
    # Handing back an answer already half posted would repeat it, and repeat it
    # through the Markdown path this exists to avoid.
    return None if delivered else answer


def make_handler():
    """Return the async callable registered by Hermes as ``/amsg``."""
    async def handle(raw_args: str) -> str | None:
        source = _SOURCE.get()
        adapter = active_adapter()
        return await answer_in_owner_chat(
            adapter, source, await answer(raw_args, adapter, source)
        )

    async def answer(raw_args: str, adapter, source) -> str:
        tokens = (raw_args or "").split()
        command = tokens[0] if tokens else ""
        if command == "setup":
            if not in_gateway_process():
                return await _setup(None, tokens, source)
            if not _setup_source_is_usable(source):
                return await _setup(adapter, tokens, source)
            if not _setup_owner_check(adapter, source):
                _log_refusal(source)
                return REFUSAL
            return await _setup(adapter, tokens, source)

        if command == "relay" and adapter_module.configuration_state() != "configured":
            # The address is what setup needs; refusing to change it until setup
            # has succeeded leaves an Owner with a broken relay and no way back.
            if in_gateway_process() and not _setup_source_is_usable(source):
                return SETUP_GATEWAY_SOURCE
            return await _relay(adapter, tokens)
        if adapter_module.configuration_state() != "configured":
            return _not_setup_message()
        if in_gateway_process() and adapter is None:
            # The profile is complete but nothing is running for it. The
            # Owner-identity refusal would blame the wrong thing: name the
            # real fault, the same way setup does.
            return _setup_gateway_adapter_message()
        if getattr(adapter, "_waiting_for_owner_chat", False) and _may_claim_owner_chat(
            adapter, source
        ):
            # There is no Owner Chat to check against yet. Only a chat that
            # could claim it is told so; anyone else gets the plain refusal,
            # because coaching a stranger on how to claim is worse.
            return NO_OWNER_CHAT_YET
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
        if command == "rename":
            return await _rename(adapter, tokens)
        if command == "relay":
            return await _relay(adapter, tokens)
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
