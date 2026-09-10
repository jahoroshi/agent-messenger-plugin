"""What the first gateway start after an install works out for itself.

Hermes runs no plugin code when it installs a plugin: `hermes plugins install`
clones the files, prints `after-install.md` and stops. There is no post-install
hook to ask for. So the first gateway start is the earliest moment this plugin
can act, and it is where the values an Owner should never have to type become
the profile's own lines. See docs/adr/0008.

Precedence, highest first:

1. an argument to `/amsg setup`
2. an ``AMESSENGER_`` line already in the profile ``.env``
3. a value derived here: the Agent named after the key's Owner, the Owner Chat
   taken from the one Hermes home channel
4. a value shipped in ``defaults.py``: the relay address, the Kind, and the
   Owner key read from ``REDMINE_API_KEY``

Nothing here guesses. Two candidate home channels, or none, means the profile
waits for a human, and the reason is said once rather than every turn.
"""

import logging
import os
import re

from . import adapter as adapter_module
from . import defaults, relay, security
from gateway.config import Platform


logger = logging.getLogger("amessenger")

# Platforms an Owner Chat can live on. A Channel never reaches an Owner, and a
# console cannot receive Messages, so neither is ever a candidate.
OWNER_CAPABLE_PLATFORMS = ("telegram", "google_chat", "irc")
# The five lines a configured profile holds.
PROFILE_VARIABLES = (
    "AMESSENGER_URL",
    "AMESSENGER_KEY",
    "AMESSENGER_AGENT",
    "AMESSENGER_KIND",
    "AMESSENGER_OWNER_CHAT",
)
# An Owner reads both of these. They name what the Owner can reach: a value in
# the profile, or a command typed in a chat. Never a shell only an operator has.
NO_OWNER_KEY = (
    "AMessenger is waiting for the Owner key.\n"
    "Put the Redmine API key in REDMINE_API_KEY in the profile .env, "
    "or type /amsg setup --key <key> in a private chat with this Agent."
)
NO_OWNER_CHAT = (
    "AMessenger is waiting for its Owner Chat.\n"
    "Open the chat where Messages must arrive and type /amsg setup there."
)


def _home_channel_id(runner, platform) -> str:
    try:
        config = getattr(runner, "config", None)
        getter = getattr(config, "get_home_channel", None)
        if not callable(getter):
            return ""
        return str(getattr(getter(platform), "chat_id", "") or "")
    except Exception as error:
        # A platform Hermes cannot answer for is simply not a candidate.
        logger.warning(
            "[amessenger] could not read the %s home channel: %s", platform, error
        )
        return ""


def default_owner_chat(runner) -> str:
    """The Owner Chat platform, when Hermes leaves exactly one candidate.

    One home channel is the Owner already saying where they read this Agent.
    Two is a choice only they can make, and none is nothing to take, so both
    answer "" and wait.
    """
    candidates = []
    for name in OWNER_CAPABLE_PLATFORMS:
        try:
            platform = Platform(name)
        except ValueError:
            continue
        if adapter_module.owner_chat_platform_is_disabled(runner, platform, name):
            continue
        if _home_channel_id(runner, platform):
            candidates.append(name)
    return candidates[0] if len(candidates) == 1 else ""


def resolve(values: dict, runner) -> tuple[dict, str]:
    """The lines to write for this profile, or the reason it must wait.

    ``values`` is what the profile ``.env`` already holds. Every line in it is
    kept exactly as it is: an Owner who wrote a value is never overruled by a
    default. ``AMESSENGER_AGENT`` is absent from the result when it still has
    to be derived, which needs the relay and so happens in ``first_run``.
    """
    stored = {name: str(values.get(name, "") or "").strip() for name in PROFILE_VARIABLES}

    key = stored["AMESSENGER_KEY"] or adapter_module.owner_key()
    if not key:
        return {}, NO_OWNER_KEY

    owner_chat = stored["AMESSENGER_OWNER_CHAT"] or default_owner_chat(runner)
    if not owner_chat:
        return {}, NO_OWNER_CHAT

    resolved = {
        "AMESSENGER_URL": stored["AMESSENGER_URL"] or adapter_module.relay_url(),
        "AMESSENGER_KEY": key,
        "AMESSENGER_KIND": stored["AMESSENGER_KIND"] or defaults.KIND,
        "AMESSENGER_OWNER_CHAT": owner_chat,
    }
    if stored["AMESSENGER_AGENT"]:
        resolved["AMESSENGER_AGENT"] = stored["AMESSENGER_AGENT"]
    return resolved, ""


async def agent_named_after_the_owner(adapter, url: str, key: str) -> tuple[str, str]:
    """Derive the Agent name from the key's Owner, or say why it cannot.

    Chat setup once used the Hermes profile label instead, whose ordinary
    default is `hermes-agent`, so two fresh installations following the
    documented two-step route asked the relay for the same globally unique
    name. There is no safe local guess: a name that is not derived from the
    authenticated Owner is refused rather than invented.
    """
    client = relay.build_client(
        url,
        key,
        "",
        transport=getattr(adapter, "_transport", None),
        ca_file=adapter_module.read_settings()["ca_file"],
    )
    try:
        login = await relay.owner_login(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return "", (
            "AMessenger setup did not run.\n"
            "Reason: the Owner identity could not be read from the relay: "
            f"{security.safe_field(str(error), limit=security.DIAGNOSTIC_LIMIT)}\n\n"
            "Check the relay and the key, or name the Agent yourself:\n"
            "/amsg setup <agent-name>"
        )
    except relay.UntrustedRelayCertificate as error:
        return "", (
            "AMessenger setup did not run.\n"
            f"Reason: {security.safe_field(str(error), limit=security.DIAGNOSTIC_LIMIT)}"
        )
    finally:
        await client.aclose()

    agent = adapter_module.agent_name_from_login(login)
    if re.fullmatch(adapter_module.AGENT_NAME_PATTERN, agent) is None:
        # A login of one character, or one made only of punctuation, normalizes
        # to something no Agent name may be. Saying so beats publishing a Card
        # under a name the Owner never chose and cannot recognize.
        return "", (
            "AMessenger setup did not run.\n"
            "Reason: an Agent name could not be derived from the Owner login.\n\n"
            "Name the Agent yourself:\n"
            "/amsg setup <agent-name>"
        )
    return agent, ""


def _say_once(adapter, reason: str) -> None:
    """Say why the profile is waiting, once per reason, not once per turn."""
    if getattr(adapter, "_bootstrap_reason", None) == reason:
        return
    adapter._bootstrap_reason = reason
    logger.warning("[amessenger] %s", reason.replace("\n", " "))


async def first_run(adapter) -> bool:
    """Configure this profile from its defaults, publish the Card, say hello.

    Answers whether the profile is now configured. False is an ordinary state,
    not a fault: the poll loop calls this again on its next turn, so a relay
    that is not up yet, or an Owner Chat not chosen yet, costs nothing but a
    wait.
    """
    try:
        stored = adapter_module._profile_env_values()
    except OSError as error:
        _say_once(adapter, f"the profile .env could not be read: {error}")
        return False

    values, reason = resolve(stored, getattr(adapter, "gateway_runner", None))
    if reason:
        _say_once(adapter, reason)
        return False

    if "AMESSENGER_AGENT" not in values:
        agent, problem = await agent_named_after_the_owner(
            adapter, values["AMESSENGER_URL"], values["AMESSENGER_KEY"]
        )
        if not agent:
            _say_once(adapter, problem)
            return False
        values = {**values, "AMESSENGER_AGENT": agent}

    adapter_module.update_profile_env(values)
    for name, value in values.items():
        os.environ[name] = value
    adapter._bootstrap_reason = None
    logger.info(
        "[amessenger] installed profile configured from its defaults: "
        "Agent %s, Owner Chat %s",
        values["AMESSENGER_AGENT"],
        values["AMESSENGER_OWNER_CHAT"],
    )
    await adapter.reload_configuration()
    try:
        await adapter.publish_card()
        await adapter.deliver_welcome(fresh_card=True)
    except Exception as error:
        # The profile is configured either way. The poll loop publishes on its
        # next turn, so a relay that answered whoami and then went away costs
        # one line here rather than a start that fails.
        logger.warning("[amessenger] the Card was not published yet: %s", error)
    return True
