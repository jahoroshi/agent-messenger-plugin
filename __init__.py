"""The amessenger-hermes plugin package."""

import asyncio
import logging
import os
from pathlib import Path

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent
HELP_TEXT = (_HERE / "help.md").read_text(encoding="utf-8")   # §6.8: written once, loaded at import
SKILL_PATH = _HERE / "SKILL.md"

from .adapter import (
    AMessengerAdapter,
    MAX_MESSAGE_LENGTH,
    PLATFORM_HINT,
    PLATFORM_NAME,
    REQUIRED_ENV,
    check_requirements,
    env_enablement,
    is_connected,
    validate_config,
)
from . import adapter as adapter_module
from . import mirror


logger = logging.getLogger("amessenger")
HOME_CHANNEL_SENTINEL = "__amessenger_home_channel_disabled__"


def _platform_name(value) -> str:
    return str(getattr(value, "value", value) or "")


def _owner_platform() -> str:
    settings = adapter_module.read_settings()
    platform, _chat_id = adapter_module.parse_owner_chat(settings.get("owner_chat", ""))
    return platform


def transform_llm_output(
    response_text=None, session_id=None, model=None, platform=None, **_
):
    """Rewrite Mirror-shaped lines in replies addressed to the Owner Chat."""
    del session_id, model
    adapter = adapter_module.live_adapter()
    if adapter is None or getattr(adapter, "_running", False) is not True:
        return None
    if _platform_name(platform) != _owner_platform():
        return None
    return mirror.rewrite_forged_lines(response_text)


STREAM_FALLBACK_WARNING = (
    "⚠ (agent wrote, not a Mirror) The interrupted reply contained a "
    "Mirror-like line and could not be rewritten."
)


def _consume_stream_warning_result(future) -> None:
    try:
        future.result()
    except Exception as error:
        logger.warning("[amessenger] interrupted-stream warning failed: %s", error)


def on_stream_end(
    final_text=None,
    finished=True,
    error=None,
    surface=None,
    session_id=None,
    turn_id=None,
    **_,
):
    """Schedule the warning for a forged line in an interrupted Owner stream."""
    del error, session_id, turn_id
    if finished or not mirror.detects_mirror_format(final_text):
        return None
    adapter = adapter_module.live_adapter()
    if adapter is None or getattr(adapter, "_running", False) is not True:
        return None
    if _platform_name(surface) != _owner_platform():
        return None
    loop = getattr(adapter, "_loop", None)
    if loop is None or not loop.is_running():
        logger.warning(
            "[amessenger] cannot schedule interrupted-stream warning without a gateway loop"
        )
        return None
    coroutine = adapter.mirror_or_queue(STREAM_FALLBACK_WARNING)
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception as scheduling_error:
        coroutine.close()
        logger.warning(
            "[amessenger] interrupted-stream warning could not be queued: %s",
            scheduling_error,
        )
        return None
    future.add_done_callback(_consume_stream_warning_result)
    return None


def register(ctx) -> None:
    # Hermes treats a non-empty AMESSENGER_HOME_CHANNEL as proof that a home
    # channel exists, which suppresses its one-time /sethome notice in Channel
    # sessions. This sentinel is deliberately neither a Channel id nor the
    # Owner Chat id. It is inert because this entry leaves cron_deliver_env_var
    # unset, so Hermes cron does not recognize amessenger as a deliver= target
    # and never reads this variable for delivery. It would become unsafe only
    # if Hermes later registered amessenger as a cron delivery platform or
    # otherwise used this fallback variable as a real delivery destination.
    os.environ.setdefault("AMESSENGER_HOME_CHANNEL", HOME_CHANNEL_SENTINEL)
    ctx.register_platform(
        name=PLATFORM_NAME, label="AMessenger",
        adapter_factory=lambda cfg: AMessengerAdapter(cfg, help_text=HELP_TEXT),
        check_fn=check_requirements, validate_config=validate_config,
        is_connected=is_connected, required_env=list(REQUIRED_ENV),
        install_hint="Set the AMESSENGER_* variables in $HERMES_HOME/.env",
        env_enablement_fn=env_enablement, platform_hint=PLATFORM_HINT,
        emoji="📨", max_message_length=MAX_MESSAGE_LENGTH, pii_safe=False,
    )
    from . import commands

    ctx.register_command(
        "amsg",
        commands.make_handler(),
        description="AMessenger: join Channels and set the Mail Policy",
        args_hint="join|interact|notify|leave|log|status|approve|deny|help",
    )
    ctx.register_hook("pre_gateway_dispatch", commands.remember_source)
    ctx.register_hook("transform_llm_output", transform_llm_output)
    ctx.register_hook("on_stream_end", on_stream_end)
    from . import tools

    if hasattr(ctx, "register_tool"):
        tools.register_tools(ctx)
    if hasattr(ctx, "register_skill"):
        ctx.register_skill(
            "amessenger", SKILL_PATH,
            description="Send and receive Messages with other Hermes Agents through AMessenger.",
        )
