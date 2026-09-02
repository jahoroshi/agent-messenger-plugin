"""The amessenger-hermes plugin package."""

from pathlib import Path

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent
HELP_TEXT = (_HERE / "help.md").read_text(encoding="utf-8")   # §6.8: written once, loaded at import
SKILL_PATH = _HERE / "SKILL.md"                               # registered with ctx.register_skill in T8.1

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


def register(ctx) -> None:
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
        args_hint="join|interact|notify|leave|status|approve|deny|help",
    )
    ctx.register_hook("pre_gateway_dispatch", commands.remember_source)
