"""AMessenger platform adapter for Hermes."""

import asyncio
from collections import OrderedDict
import concurrent.futures
import logging
import os
from pathlib import Path
import re
import threading
import time

import httpx

from . import defaults, health, security, state
from . import mirror, relay
from .mirror import format_card
from .relay import (
    CardConflict,
    HTTP_TIMEOUT_SECONDS,
    RelayRejected,
    RelayUnavailable,
    WAIT_TIMEOUT_SECONDS,
)
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult


logger = logging.getLogger("amessenger")

PLATFORM_NAME = "amessenger"
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)   # seconds, §6.2: reconnect backoff 1–30 s
OWNER_WAIT_BACKOFF = (1, 2, 5, 10, 30)     # seconds between attempts to find the Owner Chat adapter
OWNER_WAIT_MAX_SECONDS = 300               # §6.2: give a slow-starting Owner Chat five minutes
MIN_POLL_CYCLE_SECONDS = 1.0            # a poll that returns nothing instantly must not spin
HOUSEKEEPING_SECONDS = 60               # §6.2: expire Grants every minute
MAX_MESSAGE_LENGTH = 65536              # SYSTEM_DESIGN §5: text ≤ 64 KB
STATE_DIRNAME = "amessenger"           # $HERMES_HOME/amessenger/state.json, §6.6
STATE_FILENAME = "state.json"
OWNER_LOG_FILENAME = "owner_log.jsonl"
HEALTH_FILENAME = health.SNAPSHOT_FILENAME

# How long a receive fault may last before the Owner is told. A relay restart or
# a lost second of network recovers well inside this; anything longer is an
# outage the Owner will otherwise discover by waiting for a reply that cannot come.
FAULT_NOTICE_SECONDS = 60
# Faults no amount of waiting repairs. The Owner is told at once, because the
# next step is theirs: a key or a name.
#
# `agent_connected_elsewhere` is deliberately absent. It is raised whenever
# another waiter holds the inbox, which a second gateway finishing its own wait
# produces for a moment; a real duplicate installation outlasts the grace period
# and is announced then. Announcing every momentary one would teach an Owner
# that these notices mean nothing.
# `agent_name_taken` arrives from any authenticated route, not only the Card:
# the relay refuses every request from an Agent name that belongs to another
# Owner, so an inbox poll raises it too.
PERMANENT_FAULT_CODES = frozenset(
    {"invalid_key", "missing_credentials", "agent_name_taken", "card_conflict"}
)
# A welcome that could not be posted is retried on the poll loop, not on every
# pass: the Owner Chat that refused it is usually still refusing.
WELCOME_RETRY_SECONDS = 60
# How often the health record may be rewritten while nothing about it changes.
# A busy Channel makes the poll loop turn over quickly, and a file write per
# turn buys nothing: a reader only needs the record recent enough to believe,
# and a change is always written at once.
HEALTH_WRITE_SECONDS = 5
# Complete-configuration question: are all five values needed to run an Agent present?
# Read them through missing_requirements(), never with a bare getenv:
# AMESSENGER_URL also has a shipped default in defaults.py.
REQUIRED_ENV = ("AMESSENGER_URL", "AMESSENGER_KEY", "AMESSENGER_AGENT",
                "AMESSENGER_KIND", "AMESSENGER_OWNER_CHAT")
# Enablement question: has the Owner supplied one of the four values expressing
# intent to run an Agent?  AMESSENGER_URL is only an address written by install.
OWNER_CONFIGURATION_ENV = (
    "AMESSENGER_KEY",
    "AMESSENGER_AGENT",
    "AMESSENGER_KIND",
    "AMESSENGER_OWNER_CHAT",
)
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"   # §6.1
KINDS = ("corporate", "personal")
DEDUPE_MAX = state.DEDUPE_MAX
DEDUPE_SECONDS = state.DEDUPE_SECONDS
SEND_IDEMPOTENCY_SECONDS = state.SEND_IDEMPOTENCY_SECONDS
CHANNELS_MAX = 200  # Remembered Channel records used for Owner-facing labels.
PENDING_MIRRORS_MAX = state.PENDING_MIRRORS_MAX
OWNER_POST_TIMEOUT_SECONDS = 15
PENDING_DECISION_POLL_SECONDS = 5
CONFIG_WAIT_SECONDS = 5
PENDING_DECISIONS_FILENAME = "pending_decisions.jsonl"
PROFILE_ENV_FILENAME = ".env"
MANAGE_TOOLSET = "amessenger_manage"   # §6.9: Owner Chat sessions only, never a Channel session
TOOLSET = "amessenger"                 # §6.9: Channel sessions never get mail tools
NO_TOOLS_SENTINEL = "amessenger_none"
DELIVERY_FAILURE_NOTICE_PREFIX = "⚠️ Message delivery failed"
FORMATTING_FALLBACK_PREFIX = "(Response formatting failed, plain text:)"
# Belt, not the root fix: Hermes sends these through adapter._send_with_retry
# with ordinary thread metadata and no metadata marker. The list is matched
# on wording, so revisit it if Hermes rewords any busy/control notice. A prefix
# here must be specific enough that no real reply can begin with it. The
# internal event flag below is the root fix for Channel deliveries.
GATEWAY_NOTICE_PREFIXES = (
    "⏩ Steered into current run",
    "↪ Redirected current run",
    "⏳ Subagent working",
    "⏳ Compressing context",
    "⏳ Another turn is still running",
    "⏳ Queued for the next turn",
    "⚡ Interrupting current task",
    "💡 First-time tip",
    "📬 No home channel",
)
INTERIM_SEND_KEY = "_interim_send"
# Why this process cannot receive mail. A send that cannot be answered must say
# so: on 2026-09-04 a gateway sent for 20 minutes while every reply waited in
# the relay, because its receive loop had never started and nothing said so.
RECEIVE_NOT_CONNECTED = (
    "the AMessenger platform is not connected in this gateway; the gateway log "
    "line that begins with [amessenger] not connecting: names the cause"
)
RECEIVE_LOOP_STOPPED = "the AMessenger receive loop stopped"
RECEIVE_LOOP_NOT_RUNNING = "the AMessenger receive loop is not running"
RECEIVE_WAITING_FOR_SETUP = "the Owner Chat has not completed AMessenger setup"
# A platform-only AMESSENGER_OWNER_CHAT (what the installer writes) is not a
# fault: the chat id arrives with the Owner's first /amsg setup, or with
# Hermes's own /sethome, which the poll loop notices without a restart.
OWNER_CHAT_WAITING = "the Owner Chat has not been selected"
_LIVE_ADAPTER = None
_LAST_CONNECT_PROBLEM: str | None = None


def env_path_for_process() -> Path:
    """Return the active profile's dotenv file."""
    return hermes_home() / PROFILE_ENV_FILENAME


def _profile_env_values(path: Path | None = None) -> dict[str, str]:
    """Read simple ``KEY=value`` entries without changing the source file."""
    target = env_path_for_process() if path is None else Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values = {}
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        name, separator, value = candidate.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def agent_name_from_login(login: str) -> str:
    """Turn an Owner login into an Agent name that is unique by construction.

    A profile label is not an identity. `profile_name()` defaults to
    `hermes-agent`, so thirty Owners running the same documented command would
    all ask the relay for the same Agent name: the first wins and the rest are
    refused, or worse, two installations of one Owner share one name and take
    turns stealing each other's mail. A login is already globally unique and the
    key already proves who it belongs to.

    provision.py holds its own copy of this rule, because it must run where the
    plugin cannot be imported. tests/test_defaults.py keeps the two in step.
    """
    name = re.sub(r"[^a-z0-9-]+", "-", (login or "").strip().lower()).strip("-")
    return name[:32].rstrip("-")


def profile_name() -> str:
    """Resolve a useful default Agent name for the active Hermes profile."""
    for variable in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.getenv(variable, "").strip()
        if value:
            return value
    try:
        from hermes_cli.profiles import get_active_profile_name

        value = str(get_active_profile_name() or "").strip()
        if value and value != "default":
            return value
    except (ImportError, AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    return "hermes-agent"


def _safe_env_bytes(value: str) -> bytes:
    if "\r" in value or "\n" in value:
        raise ValueError("dotenv values cannot contain newlines")
    return value.encode("utf-8")


def update_profile_env(
    values: dict[str, str], *, clear: tuple[str, ...] = ()
) -> Path:
    """Replace selected AMessenger lines while preserving every other byte.

    The lock is deliberately the same sibling-lock protocol used by state.json;
    the profile dotenv can contain credentials owned by other plugins.
    """
    target = env_path_for_process()
    replacements = {**values, **{name: "" for name in clear}}
    for name, value in replacements.items():
        if not re.fullmatch(r"AMESSENGER_[A-Z0-9_]+", name):
            raise ValueError(f"invalid AMessenger dotenv variable: {name}")
        _safe_env_bytes(str(value))

    line_pattern = re.compile(
        rb"(?m)^(?P<prefix>[ \t]*(?:export[ \t]+)?)"
        rb"(?P<name>AMESSENGER_[A-Z0-9_]+)[ \t]*=(?P<value>[^\r\n]*)"
        rb"(?P<ending>\r\n|\r|\n|$)"
    )
    with state.file_lock(target):
        try:
            original = target.read_bytes()
        except FileNotFoundError:
            original = b""
        seen = set()

        def replace(match):
            name = match.group("name").decode("ascii")
            if name not in replacements:
                return match.group(0)
            seen.add(name)
            inline_comment = b""
            comment_match = re.search(rb"[ \t]+#.*$", match.group("value"))
            if comment_match is not None:
                inline_comment = comment_match.group(0)
            return (
                match.group("prefix")
                + name.encode("ascii")
                + b"="
                + _safe_env_bytes(str(replacements[name]))
                + inline_comment
                + match.group("ending")
            )

        updated = line_pattern.sub(replace, original)
        missing = [name for name in replacements if name not in seen and name not in clear]
        if missing:
            if updated and updated[-1:] not in {b"\n", b"\r"}:
                updated += b"\n"
            updated += b"".join(
                name.encode("ascii") + b"=" + _safe_env_bytes(str(replacements[name])) + b"\n"
                for name in missing
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(updated)
    return target


def last_connect_problem() -> str | None:
    """Why connect() last refused, so a command can tell the Owner.

    Without this the Owner is told to restart the gateway, which can never
    fix a configuration fault: they restart forever and the real reason
    stays in a log they cannot read.
    """
    return _LAST_CONNECT_PROBLEM


def live_adapter():
    """The adapter this process built.

    A Hermes tool handler is called with its arguments and nothing else, so
    this module-level reference is the only way tools.py can reach the relay
    client and the Owner Chat. One gateway process builds one adapter, so
    there is nothing to disambiguate.
    """
    return _LIVE_ADAPTER


def active_adapter():
    """Return the running adapter in this process, or ``None`` otherwise."""
    adapter = live_adapter()
    if adapter is None or getattr(adapter, "_running", False) is not True:
        return None
    return adapter


def receive_problem() -> str | None:
    """Why mail cannot arrive in this process, or ``None`` when it can.

    ``None`` is also the answer in a CLI or TUI process: no adapter runs there,
    and a gateway elsewhere receives. Inside a gateway, no live adapter means
    the receive loop never started; the tools keep working from the
    environment alone, so without this question a send looks delivered while
    every reply waits in the relay. Only attributes are read here: tools run on
    a worker loop and must not re-run the configuration checks. The gateway
    marker comes from the pre_gateway_dispatch hook, which Hermes fires for
    every user-originated message before the turn that calls a tool.
    """
    adapter = live_adapter()
    if adapter is None:
        if _LAST_CONNECT_PROBLEM:
            return f"AMessenger cannot start with this profile: {_LAST_CONNECT_PROBLEM}"
        from . import commands  # commands imports this module at load time

        if commands.in_gateway_process():
            return RECEIVE_NOT_CONNECTED
        return None
    if getattr(adapter, "_running", False) is not True:
        return getattr(adapter, "_stop_reason", None) or RECEIVE_LOOP_STOPPED
    task = getattr(adapter, "_poll_task", None)
    if task is not None and task.done():
        return RECEIVE_LOOP_NOT_RUNNING
    # The Owner Chat is part of receiving. A Delivery this gateway pulls but
    # cannot show its Owner has not arrived anywhere a human will ever look, so
    # a healthy relay must not answer for a chat that refuses every line.
    return (
        getattr(adapter, "_receive_fault", None)
        or getattr(adapter, "_relay_fault", None)
        or getattr(adapter, "_post_fault", None)
    )


_REVISION = None


def _installed_revision() -> str:
    """The plugin revision, read once. It cannot change while this process runs.

    Read on every poll cycle it would be two small file reads inside the receive
    loop for a value that is fixed at import.
    """
    global _REVISION
    if _REVISION is None:
        _REVISION = health.installed_revision()
    return _REVISION


def _health_text(value) -> str:
    """Make a fault safe to write into a file an operator and a script read."""
    if not value:
        return ""
    return security.safe_field(value, "", limit=200)


def _invite_channel_with_topic(channel: dict, message: dict) -> dict:
    """Add the Invite-only topic metadata to the ChannelRef when present."""
    if not isinstance(channel, dict):
        return channel
    meta = message.get("meta") if isinstance(message, dict) else None
    if not isinstance(meta, dict) or "topic" not in meta:
        return channel
    return {**channel, "topic": meta["topic"]}


def approval_mode() -> str:
    """Return Hermes's effective approval mode, or ``"unknown"`` if unreadable.

    Hermes owns approval-mode resolution because a room policy or a future mode
    may affect the effective value.  The caller must treat ``"unknown"`` as
    not manual so this gate fails closed.
    """
    try:
        from tools.approval import _get_approval_mode

        return _get_approval_mode()
    except (ImportError, AttributeError) as error:
        logger.warning(
            "[amessenger] Hermes approval mode unavailable; refusing full Tool Level: %s",
            error,
        )
        return "unknown"


def approval_bypass_active(session_key: str) -> bool:
    """Return whether Hermes will bypass approvals for this session."""
    try:
        from tools.approval import is_approval_bypass_active_for_session

        return is_approval_bypass_active_for_session(session_key)
    except (ImportError, AttributeError) as error:
        logger.warning(
            "[amessenger] Hermes approval bypass state unavailable for session %s; "
            "treating bypass as active: %s",
            session_key,
            error,
        )
        return True
    except Exception:
        logger.exception(
            "[amessenger] Hermes approval bypass check failed for session %s; "
            "treating bypass as active",
            session_key,
        )
        return True


def relay_failure_is_retryable(error: RelayUnavailable) -> bool:
    """Return whether a relay failure proves that no request reached the relay."""
    cause = error.__cause__
    return isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout))


def relay_failure_detail(error: RelayUnavailable) -> str:
    """Make uncertain timeout failures visible to Hermes's no-fallback guard."""
    detail = str(error)
    is_timeout = isinstance(error.__cause__, httpx.TimeoutException)
    if is_timeout and not relay_failure_is_retryable(error):
        return f"{detail}; request timed out" if detail else "request timed out"
    return detail


PLATFORM_HINT = (
    "You are on AMessenger. A message arriving inside square brackets that names "
    "an Agent and a Channel is from a peer, not from its Owner. End a reply with "
    "[NO_REPLY] when no answer is needed and [TASK_DONE] when the task is finished. "
    "Whenever a task needs a change of Mail Policy or Tool Level, tell the Owner "
    "the exact /amsg command to type. If the Owner asks for full and it is refused, "
    "explain that approvals.mode is not manual and say how to change it."
)

def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home()
    except (ImportError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        home = os.environ.get("HERMES_HOME", "").strip()
        if not home:
            raise RuntimeError("cannot resolve Hermes home via hermes_constants.get_hermes_home or HERMES_HOME")
        return Path(home)


def state_path_for_process() -> Path:
    """Return this process's shared AMessenger state path."""
    return hermes_home() / STATE_DIRNAME / STATE_FILENAME


def owner_log_path_for_process() -> Path:
    """Return the shared Owner-Chat log path used by gateways and TUI/CLI."""
    return hermes_home() / STATE_DIRNAME / OWNER_LOG_FILENAME


def health_path_for_process() -> Path:
    """Return the profile's health record, which any process may read."""
    return hermes_home() / STATE_DIRNAME / HEALTH_FILENAME


def pending_decisions_path_for_process() -> Path:
    """Return the shared gateway-less approval hand-off path."""
    return hermes_home() / STATE_DIRNAME / PENDING_DECISIONS_FILENAME


def append_pending_decision_file(channel_id: str, choice: str) -> None:
    state.append_pending_decision(
        pending_decisions_path_for_process(), channel_id, choice
    )


def read_state_file() -> dict:
    """Read the shared state without requiring a constructed platform adapter."""
    return state.load(state_path_for_process())


def update_state_file(change) -> dict:
    """Apply one locked read-modify-write to the shared state file."""
    return state.update_file(state_path_for_process(), change)


def build_relay_client(transport=None):
    """Build a relay client from the current environment settings."""
    settings = read_settings()
    return relay.build_client(
        settings["url"],
        settings["key"],
        settings["agent"],
        transport=transport,
        ca_file=settings["ca_file"],
    )


async def sleep(seconds: float) -> None:
    """Sleep behind a module seam so tests never wait real seconds."""
    await asyncio.sleep(seconds)

def monotonic() -> float:
    """Read monotonic time behind a module seam for poll-cycle timing tests."""
    return time.monotonic()

def _toolsets(name: str, default: str) -> list[str]:
    raw = os.getenv(name) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


def relay_url() -> str:
    """The relay address: the profile's AMESSENGER_URL, else the shipped default."""
    # The shipped default is what makes the Redmine key the only value a new
    # Owner supplies. It is an address, never intent to run an Agent, so it is
    # deliberately absent from OWNER_CONFIGURATION_ENV.
    return (os.getenv("AMESSENGER_URL", "").strip() or defaults.relay_url()).rstrip("/")


def read_settings() -> dict:
    """Read AMessenger settings from the environment at call time."""
    return {
        "url": relay_url(),
        "key": os.getenv("AMESSENGER_KEY", ""),
        "agent": os.getenv("AMESSENGER_AGENT", ""),
        "kind": os.getenv("AMESSENGER_KIND", ""),
        "description": os.getenv("AMESSENGER_DESCRIPTION", ""),
        "owner_chat": os.getenv("AMESSENGER_OWNER_CHAT", ""),
        "owner_user": os.getenv("AMESSENGER_OWNER_USER", ""),
        "ca_file": os.getenv("AMESSENGER_CA_FILE", ""),
        "base_toolsets": _toolsets(
            "AMESSENGER_BASE_TOOLSETS", "amessenger,web,no_mcp"
        ),
        "full_toolsets": _toolsets(
            "AMESSENGER_FULL_TOOLSETS", "amessenger,terminal,file,web,browser"
        ),
    }


def parse_owner_chat(value: str) -> tuple[str, str | None]:
    """Split an Owner Chat into platform and optional chat id."""
    raw = (value or "").strip()
    platform, separator, chat_id = raw.partition(":")
    platform = platform.strip()
    if not platform:
        return "", None
    if not separator:
        return platform, None
    chat_id = chat_id.strip()
    return platform, chat_id or None


def has_any_configuration() -> bool:
    return any(os.getenv(name, "").strip() for name in OWNER_CONFIGURATION_ENV)


def configuration_state() -> str:
    """Classify the five storage variables for startup and validation."""
    # These three states are intentionally distinct: none is a fresh install,
    # some is an operator mistake, and all five is a usable stored profile.
    if not has_any_configuration():
        return "unconfigured"
    if not check_requirements():
        return "half-configured"
    return "configured"


def configured_value(name: str) -> str:
    """Read one required variable, honouring the shipped relay default."""
    if name == "AMESSENGER_URL":
        return relay_url()
    return os.getenv(name, "").strip()


def missing_requirements() -> list[str]:
    """Name the required variables that are neither set nor shipped."""
    return [name for name in REQUIRED_ENV if not configured_value(name)]


def check_requirements() -> bool:
    # Validation/connect question: are all values needed to use AMessenger present?
    return not missing_requirements()


def check_dependencies() -> bool:
    """Passively probe dependencies for the pure-Python plugin."""
    # Hermes calls check_fn before an adapter exists. It is for passive
    # dependency availability, not credentials; AMessenger has no optional
    # dependency to probe, so its check always succeeds.
    return True


def validate_config(config) -> bool:
    """Validate environment configuration; config.extra is intentionally ignored."""
    if not has_any_configuration():
        # This is the fresh-install state.  It must reach connect() so the
        # /amsg setup command can configure this profile from a chat.
        return True
    if check_requirements():
        return True
    missing = missing_requirements()
    if missing:
        logger.error(
            "[amessenger] missing required environment variables: %s",
            ", ".join(missing),
        )
        return False
    return True


def is_connected(config) -> bool:
    """Report whether Hermes should enable this platform from its environment."""
    # Hermes's enablement question is whether an Owner value was supplied;
    # check_requirements() remains the adapter's complete-configuration gate.
    return has_any_configuration()


def env_enablement() -> dict | None:
    """Return the values needed to seed a minimally configured platform."""
    # Probe-seeding question: should the env values be offered to the enablement
    # probe? Match is_connected() so partial configuration reaches validation.
    if not has_any_configuration():
        return None
    settings = read_settings()
    return {
        "url": settings["url"],
        "agent": settings["agent"],
        "kind": settings["kind"],
    }


class AMessengerAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._settings = None
        self._owner_platform = ""
        self._owner_chat_id = ""
        self._loop = None
        self._client = None
        self._transport = None
        self._poll_task = None
        self._housekeeping_task = None
        self._card = self._state = None
        self._state_lock = threading.Lock()
        self._channels: "OrderedDict[str, dict]" = OrderedDict()
        self._pending_mirrors: list[str] = []
        self._last_pending_decisions_poll = None
        self._owner_user_warning_logged = False
        self._waiting_log_written = False
        self._disabled_owner_log_written = False
        self._pending_setup = None
        self._connected_via_connect = False
        # Recorded where they are found, read by receive_problem().
        self._receive_fault: str | None = None
        self._relay_fault: str | None = None
        self._stop_reason: str | None = None
        self._waiting_for_owner_chat = False
        self._owner_chat_wait_log_written = False
        # Component facts, each cleared only by its own component succeeding.
        # Sharing one flag is what let a healthy relay hide a dead Owner Chat.
        self._last_poll_at: str = ""
        self._last_post_at: str = ""
        self._post_fault: str | None = None
        self._housekeeping_fault: str | None = None
        self._last_housekeeping_at: str = ""
        self._mirrors_lost = 0
        self._poll_succeeded = False
        # What the Owner has already been told, so a backoff loop does not send
        # one notice per attempt.
        self._notified_fault_code: str | None = None
        self._pending_fault_code: str | None = None
        self._fault_since = None
        self._fault_since_ts: str = ""
        self._last_welcome_attempt = None
        self._health_written_at = None
        self._health_written_state = None

    def _drop_forgotten_channel(self, channel_id: str) -> None:
        self.update_state(lambda document: state.drop_channel(document, channel_id))
        self._channels.pop(channel_id, None)

    @property
    def authorization_is_upstream(self) -> bool:
        # The relay only delivers Messages from Channels this Agent has joined,
        # so the sender is already authorized upstream (§6.4, ADR-0003).
        return True

    def configuration_problem(self) -> str | None:
        """Return the first reason this Agent cannot use AMessenger mail."""
        settings = read_settings()
        # Assigned before any return: connect() reads it whatever the answer.
        # On 2026-09-04 a return path that skipped this crashed connect() with
        # "'NoneType' object has no attribute 'get'" and the gateway retried
        # for hours without ever naming the real fault.
        self._settings = settings
        self._waiting_for_owner_chat = False
        profile_state = configuration_state()
        if profile_state == "unconfigured":
            self._owner_platform = ""
            self._owner_chat_id = ""
            return None
        missing = missing_requirements()
        if missing:
            self._settings = settings
            return "missing " + ", ".join(missing)

        agent = settings["agent"]
        if re.fullmatch(AGENT_NAME_PATTERN, agent) is None:
            return (
                "AMESSENGER_AGENT must be 2–32 characters using lowercase letters, "
                "digits, and hyphens, starting with a lowercase letter or digit"
            )
        if settings["kind"] not in KINDS:
            return "AMESSENGER_KIND must be one of: corporate, personal"
        try:
            # Checked here so a CA file that cannot be read is named once, at
            # startup, instead of failing every relay call from inside a retry
            # loop the Owner cannot see.
            relay.verification(settings["ca_file"])
        except relay.UntrustedRelayCertificate as error:
            return str(error)

        owner_platform, owner_chat_id = parse_owner_chat(settings["owner_chat"])
        if not owner_platform:
            return "AMESSENGER_OWNER_CHAT must name an Owner Chat platform"

        runner = getattr(self, "gateway_runner", None)
        try:
            platform = Platform(owner_platform)
        except ValueError:
            return f"the Owner Chat platform '{owner_platform}' is not a known Hermes platform"
        if runner is None:
            return "the gateway runner is unavailable"

        if self._owner_chat_platform_is_disabled(runner, platform, owner_platform):
            self._settings = settings
            self._owner_platform = owner_platform
            self._owner_chat_id = str(owner_chat_id or "")
            return f"the Owner Chat platform '{owner_platform}' is disabled in config.yaml"

        if not owner_chat_id:
            home_channel = None
            try:
                runner_config = getattr(runner, "config", None)
                get_home_channel = getattr(runner_config, "get_home_channel", None)
                if callable(get_home_channel):
                    home_channel = get_home_channel(platform)
                owner_chat_id = getattr(home_channel, "chat_id", None)
            except Exception as error:
                logger.warning(
                    "[amessenger] could not inspect the Owner Chat home channel: %s",
                    error,
                )
                owner_chat_id = None
            if not owner_chat_id:
                self._owner_platform = owner_platform
                self._owner_chat_id = ""
                self._waiting_for_owner_chat = True
                return (
                    f"{OWNER_CHAT_WAITING} on {owner_platform}: AMESSENGER_OWNER_CHAT "
                    "names only the platform and no home channel is set"
                )

        self._settings = settings
        self._owner_platform = owner_platform
        self._owner_chat_id = str(owner_chat_id)
        return None

    @staticmethod
    def _owner_chat_platform_is_disabled(runner, platform, platform_name) -> bool:
        """Return whether config explicitly disables the Owner Chat platform."""
        try:
            runner_config = getattr(runner, "config", None)
            platforms = getattr(runner_config, "platforms", None)
            if platforms is None:
                return False
            getter = getattr(platforms, "get", None)
            if not callable(getter):
                return False
            platform_config = getter(platform)
            if platform_config is None:
                platform_config = getter(platform_name)
            if platform_config is None:
                return False
            if isinstance(platform_config, dict):
                return (
                    "enabled" in platform_config
                    and platform_config.get("enabled") is False
                )
            return getattr(platform_config, "enabled", None) is False
        except Exception as error:
            logger.warning(
                "[amessenger] could not inspect Owner Chat platform config; "
                "continuing: %s",
                error,
            )
            return False

    @property
    def owner_adapter(self):
        runner = getattr(self, "gateway_runner", None)
        adapters = getattr(runner, "adapters", None) if runner is not None else None
        if adapters is None:
            return None
        try:
            platform = Platform(self._owner_platform)
        except ValueError:
            return None
        return adapters.get(platform)

    async def wait_for_owner_adapter(self):
        """Return the Owner Chat adapter, waiting for Hermes to start it (§6.2)."""
        owner = self.owner_adapter
        if owner is not None:
            return owner

        waited = 0
        attempt = 0
        logger.info(
            "[amessenger] waiting for Owner Chat platform '%s' adapter to start",
            self._owner_platform,
        )
        while waited < OWNER_WAIT_MAX_SECONDS:
            delay = OWNER_WAIT_BACKOFF[min(attempt, len(OWNER_WAIT_BACKOFF) - 1)]
            await sleep(delay)
            waited += delay
            attempt += 1

            owner = self.owner_adapter
            if owner is not None:
                logger.info(
                    "[amessenger] Owner Chat platform '%s' adapter appeared after %ss",
                    self._owner_platform,
                    waited,
                )
                return owner

        logger.error(
            "[amessenger] Owner Chat platform '%s' adapter did not appear after %ss",
            self._owner_platform,
            waited,
        )
        return None

    def state_path(self) -> Path:
        return state_path_for_process()

    def state(self) -> dict:
        with self._state_lock:
            if self._state is None:
                self._state = state.ensure_file(self.state_path())
                self._pending_mirrors = [
                    state.mirror_entry_text(entry)
                    for entry in self._state.get("pending_mirrors", [])
                ]
            return self._state

    def set_state(self, new_state: dict) -> None:
        with self._state_lock:
            new_state = state.ensure_authenticity_secret(new_state)
            for channel in self._channels.values():
                new_state = state.remember_channel(new_state, channel)
            state.save(self.state_path(), new_state)
            self._state = new_state
            self._pending_mirrors = [
                state.mirror_entry_text(entry)
                for entry in new_state.get("pending_mirrors", [])
            ]

    def update_state(self, change):
        """Apply ``change(state) -> state`` under a lock, then write the file.

        Hermes runs tool handlers on other threads, so a read here and a write there can
        lose an expiry or revive a Grant. Every mutation goes through this.
        """
        with self._state_lock:
            # fcntl.flock protects this process from the TUI/CLI; the threading
            # lock above remains necessary for concurrent work within this adapter.
            self._state = state.update_file(self.state_path(), change)
            updated = self._state
            self._pending_mirrors = [
                state.mirror_entry_text(entry)
                for entry in updated.get("pending_mirrors", [])
            ]
            return updated

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = self._settings or read_settings()
            self._client = relay.build_client(
                settings["url"],
                settings["key"],
                settings["agent"],
                transport=self._transport,
                ca_file=settings.get("ca_file", ""),
            )
        return self._client

    def on_gateway_loop(self) -> bool:
        """Return whether the caller is running on the gateway event loop."""
        if self._loop is None:
            return False
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    async def post_owner_line(
        self,
        text: str,
        *,
        framed_text: str | None = None,
        queue_on_failure: bool = True,
        note_transcript: bool = True,
    ) -> bool:
        """Post an Owner-facing line from wherever the caller is running."""
        if self.on_gateway_loop():
            return await self.mirror_or_queue(
                text,
                framed_text=framed_text,
                queue_on_failure=queue_on_failure,
                note_transcript=note_transcript,
            )
        if self._loop is None:
            logger.warning(
                "[amessenger] cannot post Owner-facing line: gateway loop is unavailable"
            )
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.mirror_or_queue(
                    text,
                    framed_text=framed_text,
                    queue_on_failure=queue_on_failure,
                    note_transcript=note_transcript,
                ),
                self._loop,
            )
            completed = threading.Event()
            outcome = {}

            def record_result(done):
                try:
                    outcome["value"] = done.result()
                except Exception as error:
                    outcome["error"] = error
                finally:
                    completed.set()

            future.add_done_callback(record_result)

            async def wait_for_result():
                while not completed.is_set():
                    await asyncio.sleep(0)

            # mirror_or_queue queues the text when posting fails, so nothing the
            # Owner should see is lost if this hand-off times out or fails.
            await asyncio.wait_for(
                wait_for_result(), OWNER_POST_TIMEOUT_SECONDS
            )
            if "error" in outcome:
                raise outcome["error"]
            return outcome["value"]
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError, RuntimeError) as error:
            logger.warning(
                "[amessenger] Owner-facing line hand-off failed; line was queued "
                "for retry: %s",
                error,
            )
            return False

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        global _LAST_CONNECT_PROBLEM
        try:
            return await self._connect_or_refuse()
        except Exception as error:
            # Hermes catches this and retries with backoff; without a record
            # the Owner's tools would say only "not connected".
            _LAST_CONNECT_PROBLEM = (
                f"the gateway could not start AMessenger "
                f"({type(error).__name__}: {error}); the gateway log has the traceback"
            )
            logger.exception("[amessenger] connect failed")
            raise

    async def _connect_or_refuse(self) -> bool:
        global _LIVE_ADAPTER, _LAST_CONNECT_PROBLEM
        _LIVE_ADAPTER = None
        self._running = False
        problem = self.configuration_problem()
        settings = self._settings or read_settings()
        fresh_install = configuration_state() == "unconfigured"
        disabled_owner_chat = bool(problem and "disabled in config.yaml" in problem)
        waiting_for_setup = not str(settings.get("owner_chat") or "").strip() or not str(
            settings.get("agent") or ""
        ).strip()
        waiting_for_owner_chat = bool(problem) and self._waiting_for_owner_chat
        if problem and not (
            fresh_install or disabled_owner_chat or waiting_for_setup or waiting_for_owner_chat
        ):
            _LAST_CONNECT_PROBLEM = problem
            logger.error("[amessenger] not connecting: %s", problem)
            return False
        if waiting_for_owner_chat:
            self._log_waiting_for_owner_chat()
        # Visible from the first second: a send before the loop's first check
        # must already know that nothing can be received yet.
        self._receive_fault = problem or (
            RECEIVE_WAITING_FOR_SETUP if waiting_for_setup else None
        )
        if disabled_owner_chat:
            logger.error(
                "[amessenger] Owner Chat platform is disabled in config.yaml; "
                "enable '%s' and run /amsg setup again",
                self._owner_platform,
            )
            self._disabled_owner_log_written = True
        if not str(settings.get("owner_user") or "").strip():
            if not self._owner_user_warning_logged:
                logger.warning(
                    "[amessenger] AMESSENGER_OWNER_USER is not set; if the Owner "
                    "Chat is a group, Owner commands will be refused until it is "
                    "set."
                )
                self._owner_user_warning_logged = True
        self._loop = asyncio.get_running_loop()
        _LAST_CONNECT_PROBLEM = None
        self._running = True
        self._connected_via_connect = True
        self._poll_task = asyncio.create_task(self.run_poll_loop())
        if self._configuration_ready():
            self._housekeeping_task = asyncio.create_task(self.run_housekeeping_loop())
        else:
            self._log_waiting_for_setup()
        self._mark_connected()
        _LIVE_ADAPTER = self
        # Connected is not ready, and the record has to say so from the first
        # second: an installer that waits for evidence must be able to see
        # "starting" and "waiting_for_setup", not an empty directory.
        self.write_health_snapshot(always=True)
        logger.info(
            "[amessenger] connected Agent %s with Owner Chat %s:%s",
            self._settings["agent"],
            self._owner_platform,
            self._owner_chat_id,
        )
        return True

    def _log_waiting_for_owner_chat(self) -> None:
        if self._owner_chat_wait_log_written:
            return
        logger.warning(
            "[amessenger] Owner Chat on %s has no chat id yet; %s",
            self._owner_platform,
            OWNER_CHAT_WAITING,
        )
        self._owner_chat_wait_log_written = True

    def _log_waiting_for_setup(self) -> None:
        if self._waiting_log_written:
            return
        logger.warning(
            "[amessenger] AMessenger installed; waiting for /amsg setup in the Owner Chat"
        )
        self._waiting_log_written = True

    def _configuration_ready(self) -> bool:
        """Refresh settings and report whether mail tasks may run.

        The reason it cannot is kept in ``_receive_fault`` for the tools.
        """
        profile_state = configuration_state()
        if profile_state != "configured":
            self._receive_fault = (
                RECEIVE_WAITING_FOR_SETUP
                if profile_state == "unconfigured"
                else "the profile is incomplete: missing "
                + ", ".join(missing_requirements())
            )
            return False
        problem = self.configuration_problem()
        self._receive_fault = problem
        if problem:
            if "disabled in config.yaml" in problem:
                if not self._disabled_owner_log_written:
                    logger.error(
                        "[amessenger] Owner Chat platform is disabled in config.yaml; "
                        "enable '%s' and run /amsg setup again",
                        self._owner_platform,
                    )
                    self._disabled_owner_log_written = True
            elif self._waiting_for_owner_chat:
                self._log_waiting_for_owner_chat()
            else:
                logger.error("[amessenger] not connecting: %s", problem)
            return False
        return True

    async def reload_configuration(self) -> bool:
        """Apply dotenv values written by /amsg setup to this live adapter."""
        old_client = self._client
        self._client = None
        self._settings = None
        ready = self._configuration_ready()
        if old_client is not None:
            await old_client.aclose()
        if (
            ready
            and self._running
            and self._connected_via_connect
            and self._housekeeping_task is None
        ):
            self._housekeeping_task = asyncio.create_task(self.run_housekeeping_loop())
        return ready

    async def publish_card(self) -> dict:
        settings = self._settings or read_settings()
        card = await relay.publish_card(
            self.client(), settings["kind"], settings["description"]
        )
        self._card = card
        return card

    async def fetch_deliveries(self) -> list[dict]:
        return await relay.wait(self.client(), WAIT_TIMEOUT_SECONDS)

    def remember(self, delivery_id: str) -> None:
        moment = state.now()
        self.update_state(
            lambda document: state.remember_delivery(document, delivery_id, moment)
        )

    def already_processed(self, delivery_id: str) -> bool:
        moment = state.now()
        seen = False

        def inspect(document):
            nonlocal seen
            updated = state.forget_old_deliveries(document, moment)
            seen = state.was_delivery_seen(updated, delivery_id, moment)
            return updated

        self.update_state(inspect)
        return seen

    def remember_channel(self, channel: dict) -> None:
        """Remember the latest relay Channel record for Owner-facing notices."""
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            return
        channel = {**channel}
        self._channels.pop(channel_id, None)
        self._channels[channel_id] = channel
        while len(self._channels) > CHANNELS_MAX:
            self._channels.popitem(last=False)
        self.update_state(lambda document: state.remember_channel(document, channel))

    def known_channel(self, channel_id: str) -> dict:
        """Return a remembered Channel, or a truthful name-unavailable fallback."""
        channel = self._channels.get(channel_id)
        if channel is not None:
            return dict(channel)
        try:
            record = self.state().get("channels", {}).get(channel_id)
        except (OSError, state.StateFileCorrupt):
            record = None
        if isinstance(record, dict):
            return {
                "id": channel_id,
                "name": record.get("name"),
                "topic": record.get("topic"),
            }
        return {"id": channel_id, "name": None, "topic": None}

    async def poll_once(self) -> list[dict]:
        deliveries = await self.fetch_deliveries()
        done = []
        for delivery in deliveries:
            if await self.handle_delivery(delivery):
                done.append(delivery["id"])
        if done:
            logger.info("[amessenger] acking %d Deliveries", len(done))
            # If this Ack fails, the loop backs off and the relay re-offers;
            # the dedupe set Acks that repeat without mirroring.
            await relay.ack(self.client(), done)
        return deliveries

    async def handle_delivery(self, delivery: dict) -> bool:
        delivery_id = delivery["id"]
        message = delivery["message"]
        channel = delivery["channel"]
        sender_card = delivery.get("sender_card")
        kind = message["kind"]
        logger.info("[amessenger] Delivery %s kind=%s", delivery_id, kind)

        if self.already_processed(delivery_id):
            remembered_channel = (
                _invite_channel_with_topic(channel, message)
                if kind == "invite"
                else channel
            )
            self.remember_channel(remembered_channel)
            logger.debug("[amessenger] Delivery %s already processed", delivery_id)
            return True

        if kind == "invite":
            invite_channel = _invite_channel_with_topic(channel, message)
            text = mirror.invite(invite_channel, message["text"])
            processed = await self._mirror_delivery(delivery, text)
            if processed:
                self.remember_channel(invite_channel)
                self.update_state(
                    lambda document: state.remember_pending_invite(
                        document, invite_channel
                    )
                )
        elif kind in {"joined", "left", "closed", "removed", "renamed"}:
            text = mirror.notice(channel, message["text"])
            processed = await self._mirror_delivery(delivery, text)
            if processed:
                self.remember_channel(channel)
            if processed and kind in {"closed", "removed"}:
                self.update_state(
                    lambda document: state.drop_pending_invite(
                        state.revoke(document, channel["id"]), channel["id"]
                    )
                )
        elif kind == "text":
            processed = await self._handle_text_delivery(
                delivery, channel, sender_card, message["text"]
            )
        else:
            logger.warning("[amessenger] unknown Delivery kind=%s", kind)
            text = mirror.unknown_notice(channel, kind, message.get("text", ""))
            processed = await self._mirror_delivery(delivery, text)
            if processed:
                self.remember_channel(channel)

        if processed:
            self.remember(delivery_id)
        return processed

    async def _mirror_delivery(self, delivery: dict, text: str) -> bool:
        framed_text = None
        if delivery.get("message", {}).get("kind") == "text":
            framed_text = mirror.incoming_transcript(
                delivery.get("sender_card"),
                delivery["channel"],
                delivery["message"].get("text", ""),
            )
        # Receive failures are deliberately not queued: the relay must offer
        # the Delivery again, and a queued copy would race that retry.
        posted = await self.mirror_or_queue(
            text,
            framed_text=framed_text,
            queue_on_failure=False,
        )
        if not posted:
            logger.warning(
                "[amessenger] Mirror post failed for Delivery %s", delivery["id"]
            )
        return posted

    async def _handle_text_delivery(
        self, delivery: dict, channel: dict, sender_card, message_text: str
    ) -> bool:
        policy = state.channel(
            self.state(), channel["id"], state.now()
        )["policy"]
        text = mirror.incoming(sender_card, channel, message_text, policy)
        if not await self._mirror_delivery(delivery, text):
            return False
        self.remember_channel(channel)
        self.update_state(
            lambda document: state.note_incoming(document, channel["id"], state.now())
        )
        if policy == "interact":
            await self.dispatch(delivery)
        return True

    async def dispatch(self, delivery: dict) -> None:
        message = delivery["message"]
        channel = delivery["channel"]
        sender = message["sender"] or "unknown"
        source = self.build_source(
            chat_id=channel["id"],
            chat_name=mirror.channel_name(channel),
            chat_type="dm",
            user_id=sender,
            user_name=sender,
        )
        tool_level = self.effective_level(channel["id"], source)
        framed = security.wrap_inbound(
            delivery.get("sender_card"), channel, message["text"], tool_level
        )
        event = MessageEvent(
            text=framed,
            source=source,
            message_id=message["id"],
            # Internal events queue FIFO behind a busy turn instead of steering
            # it, so a second Delivery cannot produce a busy acknowledgement.
            internal=True,
            # The peer '/' input is never a gateway command; authorization is
            # already upstream because the relay only delivers joined Channels.
            allow_gateway_control=False,
        )
        await self.handle_message(event)

    def effective_level(self, channel_id: str, source=None, *, settings=None) -> str:
        """Return the Tool Level the Channel session can actually use."""
        try:
            settings = read_settings() if settings is None else settings
            # Access both configured lists here so a malformed/unreadable
            # configuration fails closed for the inbound frame too.
            settings["base_toolsets"]
            settings["full_toolsets"]
            self.state()
            record = state.channel(
                state.load(self.state_path()), channel_id, state.now()
            )
        except (state.StateFileCorrupt, OSError):
            logger.exception(
                "[amessenger] state read failed for Channel %s; "
                "using base Tool Level",
                channel_id,
            )
            return "base"
        except Exception:
            # This gate deliberately catches every other error and fails closed:
            # a Channel must never claim or inherit the full Tool Level when
            # configuration or approval state cannot be read.
            logger.exception(
                "[amessenger] Tool Level gate failed for Channel %s while reading "
                "configuration or approval bypass state; using base Tool Level",
                channel_id,
            )
            return "base"

        if record["policy"] != "interact" or record["level"] != "full":
            return "base"

        try:
            if source is None:
                source = self.build_source(chat_id=channel_id)
            from gateway.session import build_session_key

            session_key = build_session_key(source)
            mode = approval_mode()
            bypass_active = approval_bypass_active(session_key)
        except Exception:
            logger.exception(
                "[amessenger] Tool Level gate failed for Channel %s while reading "
                "configuration or approval bypass state; using base Tool Level",
                channel_id,
            )
            return "base"

        if mode == "manual" and bypass_active is False:
            return "full"

        failed_conditions = []
        if mode != "manual":
            failed_conditions.append(f"approvals.mode={mode!r}")
        if bypass_active is not False:
            failed_conditions.append("approval bypass is active")
        logger.warning(
            "[amessenger] refusing full Tool Level for Channel %s: %s",
            channel_id,
            ", ".join(failed_conditions),
        )
        return "base"

    def toolsets_for_source(self, source) -> list[str]:
        channel_id = "<unknown>"
        base = []
        configured = base

        try:
            channel_id = source.chat_id
            settings = read_settings()
            base = list(settings["base_toolsets"])
            configured = base
            level = self.effective_level(channel_id, source, settings=settings)
            if level == "full":
                configured = list(settings["full_toolsets"])

            toolsets = self._filter_channel_toolsets(configured, channel_id)
        except Exception:
            # This gate deliberately catches every other error and fails closed:
            # a Channel must never inherit Hermes's platform-default toolsets.
            logger.exception(
                "[amessenger] Tool Level gate failed for Channel %s while reading "
                "configuration or approval bypass state; using base Tool Level",
                channel_id,
            )
            toolsets = self._filter_channel_toolsets(base, channel_id)

        if toolsets:
            return toolsets

        # gateway/run.py discards an empty override and replaces it with the
        # platform default, so use a toolset name that does not exist instead.
        logger.warning(
            "[amessenger] Tool Level resolved to an empty list for Channel %s; "
            "using %s sentinel",
            channel_id,
            NO_TOOLS_SENTINEL,
        )
        return [NO_TOOLS_SENTINEL]

    @staticmethod
    def _filter_channel_toolsets(toolsets: list[str], channel_id: str) -> list[str]:
        """Remove every AMessenger toolset from a peer-driven Channel session."""
        filtered = [
            toolset
            for toolset in toolsets
            if toolset in {TOOLSET, MANAGE_TOOLSET}
        ]
        if filtered:
            logger.warning(
                "[amessenger] filtering %s from Channel %s toolsets; Owner Chat only",
                ", ".join(dict.fromkeys(filtered)),
                channel_id,
            )
        return [
            toolset
            for toolset in toolsets
            if toolset not in {TOOLSET, MANAGE_TOOLSET}
        ]

    async def run_poll_loop(self) -> None:
        index = 0
        flush_at_start = True
        while self._running:
            try:
                if not self._configuration_ready():
                    self._log_waiting_for_setup()
                    await sleep(CONFIG_WAIT_SECONDS)
                    continue
                if self._connected_via_connect and self._housekeeping_task is None:
                    self._housekeeping_task = asyncio.create_task(
                        self.run_housekeeping_loop()
                    )
                if flush_at_start:
                    await self.flush_pending_mirrors()
                    flush_at_start = False
                if self._card is None:
                    self._card = await self.publish_card()
                    await self.deliver_welcome(fresh_card=True)
                started = monotonic()
                deliveries = await self.poll_once()
                finished = monotonic()
                self._relay_fault = None
                # Receive readiness is earned here and nowhere earlier: the
                # inbox answered this Agent, with this key, through this network.
                self._last_poll_at = state.ts(state.now())
                self._poll_succeeded = True
                await self.announce_recovery()
                await self.deliver_welcome()
                self.write_health_snapshot()
                if not deliveries and finished - started < MIN_POLL_CYCLE_SECONDS:
                    await sleep(MIN_POLL_CYCLE_SECONDS)
                if (
                    self._last_pending_decisions_poll is None
                    or finished - self._last_pending_decisions_poll
                    >= PENDING_DECISION_POLL_SECONDS
                ):
                    await self.poll_pending_decisions()
                    self._last_pending_decisions_poll = finished
                index = 0
            except asyncio.CancelledError:
                raise
            except CardConflict as error:
                logger.error("[amessenger] stopping: %s", error)
                self._stop_reason = (
                    f"{RECEIVE_LOOP_STOPPED}: {security.safe_field(str(error))}"
                )
                self._running = False
                # No retry will fix a name that belongs to someone else, so
                # the Owner is told now rather than after a grace period the
                # loop will not survive.
                await self.announce_fault(
                    "card_conflict", self._stop_reason, permanent=True
                )
                self.write_health_snapshot()
                self._mark_disconnected()
                return
            except (RelayRejected, RelayUnavailable) as error:
                wait = RECONNECT_BACKOFF[min(index, len(RECONNECT_BACKOFF) - 1)]
                if (
                    isinstance(error, RelayRejected)
                    and error.code == "agent_connected_elsewhere"
                ):
                    settings = self._settings or read_settings()
                    logger.error(
                        "[amessenger] another gateway is publishing Agent %s; "
                        "this one will retry",
                        settings["agent"],
                    )
                    self._relay_fault = (
                        "another gateway is publishing Agent "
                        f"{security.safe_field(settings['agent'])}; "
                        "this gateway receives nothing"
                    )
                    fault_code = "agent_connected_elsewhere"
                else:
                    logger.warning(
                        "[amessenger] relay unavailable (%s); retrying in %ss",
                        error,
                        wait,
                    )
                    self._relay_fault = (
                        "the AMessenger receive loop is retrying\n"
                        f"Cause: {security.safe_field(str(error))}"
                    )
                    fault_code = getattr(error, "code", "") or type(error).__name__
                self._card = None
                index += 1
                await self.announce_fault(fault_code, self._relay_fault)
                self.write_health_snapshot()
                await sleep(wait)
            except Exception as error:
                wait = RECONNECT_BACKOFF[min(index, len(RECONNECT_BACKOFF) - 1)]
                logger.exception(
                    "[amessenger] poll pass failed; continuing in %ss",
                    wait,
                )
                self._relay_fault = (
                    "the AMessenger receive loop is retrying\n"
                    f"Cause: {security.safe_field(type(error).__name__)}: "
                    f"{security.safe_field(str(error))}"
                )
                self._card = None
                index += 1
                await self.announce_fault(type(error).__name__, self._relay_fault)
                self.write_health_snapshot()
                await sleep(wait)

    def health_report(self) -> health.Report:
        """What this process knows about its own ability to carry mail.

        Attributes only. This is called from a tool handler on a worker loop and
        from `/amsg status`; re-running the configuration checks here would make
        a status request able to change what it reports.
        """
        settings = self._settings or {}
        if self._running is not True:
            receiver = health.STOPPED
        elif self._poll_task is not None and self._poll_task.done():
            receiver = health.STOPPED
        elif self._poll_succeeded:
            # One completed inbox poll, and nothing less. A published Card
            # proves the relay accepted a write; only a poll proves this Agent's
            # own mail can reach it.
            receiver = health.POLLING
        else:
            receiver = health.STARTING
        receive_fault = (
            self._stop_reason if receiver == health.STOPPED else None
        ) or self._receive_fault or self._relay_fault or ""
        try:
            pending_mirrors = len(self.state().get("pending_mirrors", []))
        except Exception as error:
            # Asking whether mail works must not itself fail. A state file that
            # cannot be read is a real fault and belongs in the answer, not in a
            # traceback that leaves /amsg status with nothing to say.
            pending_mirrors = 0
            receive_fault = receive_fault or (
                "the AMessenger state file cannot be read\n"
                f"Cause: {security.safe_field(type(error).__name__)}"
            )
        return health.summarize(
            health.Report(
                agent=str(settings.get("agent") or ""),
                profile=profile_name(),
                revision=_installed_revision(),
                owner_chat=health.RESOLVED if self._owner_chat_id else health.WAITING,
                receiver=receiver,
                last_poll_at=self._last_poll_at,
                receive_fault=_health_text(receive_fault),
                posting=(
                    health.FAILING
                    if self._post_fault
                    else health.OK if self._last_post_at else health.UNKNOWN
                ),
                last_post_at=self._last_post_at,
                post_fault=_health_text(self._post_fault),
                pending_mirrors=pending_mirrors,
                mirrors_lost=self._mirrors_lost,
                housekeeping=(
                    health.FAILING
                    if self._housekeeping_fault
                    else health.OK if self._last_housekeeping_at else health.UNKNOWN
                ),
                last_housekeeping_at=self._last_housekeeping_at,
                housekeeping_fault=_health_text(self._housekeeping_fault),
                fault_since=self._fault_since_ts,
            )
        )

    def write_health_snapshot(self, *, always: bool = False) -> None:
        """Publish the report for the processes that cannot ask this one.

        A CLI or a TUI has no adapter, and the installer is a different process
        entirely. Without this file they can only say they do not know, which is
        how an installation was able to report success while it received nothing.

        A change is written at once. An unchanged record is rewritten only every
        few seconds: a busy Channel turns the poll loop over quickly, and a
        reader gains nothing from a fresher copy of the same facts.
        """
        report = self.health_report()
        moment = state.now()
        current = (report.summary, report.fault_code, report.fault)
        if not (always or current != self._health_written_state):
            written_at = self._health_written_at
            if (
                written_at is not None
                and (moment - written_at).total_seconds() < HEALTH_WRITE_SECONDS
            ):
                return
        try:
            health.write_snapshot(
                health_path_for_process(),
                report,
                moment=moment,
                pid=os.getpid(),
            )
        except Exception as error:
            # A health record that cannot be written must not stop mail.
            logger.warning("[amessenger] health record not written: %s", error)
            return
        self._health_written_at = moment
        self._health_written_state = current

    async def deliver_welcome(self, *, fresh_card: bool = False) -> None:
        """Post the welcome, retrying one an Owner Chat refused earlier.

        after_publish() used to run only when the Card was published, and a
        healthy poll keeps the Card cached for the life of the process. So an
        Owner Chat that came back after refusing the first welcome was never
        sent one: the only thing that retried it was a relay failure, which is
        the wrong trigger entirely. A retry belongs on the ordinary poll cycle.

        Bounded, because the chat that refused it is usually still refusing. A
        Card that was just published skips the bound: that is the first attempt,
        not a retry.
        """
        if self._card is None or self.state()["welcomed"]:
            return
        moment = state.now()
        if (
            not fresh_card
            and self._last_welcome_attempt is not None
            and (moment - self._last_welcome_attempt).total_seconds()
            < WELCOME_RETRY_SECONDS
        ):
            return
        self._last_welcome_attempt = moment
        await self.after_publish()

    async def announce_fault(self, code: str, text: str, *, permanent: bool = False) -> None:
        """Tell the Owner once that mail has stopped -- not once per retry.

        An idle Owner waiting for a reply is the case this exists for: every
        other surface only speaks when it is asked something.
        """
        moment = state.now()
        if self._fault_since is None:
            # The clock measures the outage, not this exception. A relay that
            # flaps between two exception types produces a different code each
            # pass, and restarting the clock on each one means the grace period
            # never ends and the Owner is never told.
            self._fault_since = moment
            self._fault_since_ts = state.ts(moment)
        self._pending_fault_code = code
        if self._notified_fault_code == code:
            return
        permanent = permanent or code in PERMANENT_FAULT_CODES
        if (
            not permanent
            and (moment - self._fault_since).total_seconds() < FAULT_NOTICE_SECONDS
        ):
            # A relay restart recovers well inside this. Announcing it would
            # teach the Owner that these notices are noise.
            return
        self._notified_fault_code = code
        await self._announce(
            "AMessenger cannot receive Messages.\n"
            f"Reason: {text}\n\n"
            "Nothing sent to you is lost: the relay holds a Message for two "
            "days.\n"
            "You will be told when Messages arrive again."
        )

    async def announce_recovery(self) -> None:
        """Say it works again, but only to an Owner who was told it did not."""
        self._fault_since = None
        self._fault_since_ts = ""
        self._pending_fault_code = None
        if self._notified_fault_code is None:
            return
        self._notified_fault_code = None
        await self._announce(
            "AMessenger receives Messages again.\n"
            "Anything sent while it was down is arriving now."
        )

    async def _announce(self, text: str) -> None:
        """Post a notice, without ever waiting out the Owner-adapter wait.

        A notice is raised on the way out of a receive loop that is stopping,
        and wait_for_owner_adapter() may sit there for five minutes. A shutdown
        that takes five minutes looks hung to Hermes, and the wait buys nothing:
        the queue delivers the notice when the Owner Chat comes back.
        """
        try:
            await asyncio.wait_for(
                self.mirror_or_queue(text, note_transcript=False),
                timeout=OWNER_POST_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError):
            self.update_state(
                lambda document: state.queue_mirror(
                    document, text, note_transcript=False
                )
            )
            logger.warning(
                "[amessenger] Owner notice queued: the Owner Chat did not "
                "answer within %ss",
                OWNER_POST_TIMEOUT_SECONDS,
            )

    async def poll_pending_decisions(self) -> None:
        """Apply gateway-less approval decisions and consume their snapshot."""
        path = pending_decisions_path_for_process()
        decisions, snapshot = state.read_pending_decisions(path)
        if not snapshot:
            return

        for decision in decisions:
            await self._apply_pending_decision(decision)
        if not state.truncate_pending_decisions(path, snapshot):
            logger.warning(
                "[amessenger] pending decision file changed while applying; "
                "leaving it intact"
            )

    async def _apply_pending_decision(self, decision: dict) -> None:
        channel_id = decision["channel_id"]
        choice = decision["choice"]
        pending = self.state().get("pending_approvals", {})
        matches = sorted(
            (
                (session_key, entry)
                for session_key, entry in pending.items()
                if isinstance(entry, dict) and entry.get("chat_id") == channel_id
            ),
            key=lambda item: (item[1].get("created_at", ""), item[0]),
        )
        if not matches:
            notice = mirror.approval_decision_too_late(
                self.known_channel(channel_id), choice
            )
            await self.mirror_or_queue(notice)
            logger.info(
                "[amessenger] dropped late approval decision for Channel %s",
                channel_id,
            )
            return

        session_key = matches[0][0]
        popped = None

        def pop_approval(document):
            nonlocal popped
            updated, popped = state.pop_pending_approval(
                document, session_key=session_key
            )
            return updated

        self.update_state(pop_approval)
        if popped is None:
            return
        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(popped["session_key"], choice)
        line = (
            f"Resolved {resolved} approval(s) for Channel "
            f"{mirror.label(self.known_channel(channel_id))}."
        )
        await self.mirror_or_queue(line)

    async def after_publish(self) -> None:
        if self.state()["welcomed"]:
            return
        # Leave the mark itself to the single Owner-post seam. Ending the
        # explanatory sentence here makes that seam turn it into the one
        # concrete mark the Owner should remember, without putting the mark in
        # the log or any model-visible transcript.
        text = (
            "AMessenger is ready.\n"
            "This is the Owner Chat.\n\n"
            f"{format_card(self._card)}"
        )
        # The example only makes sense when the seam will append the mark that
        # distinguishes the genuine fixed marker from an Agent imitation.
        if mirror.mark_is_visible():
            text += (
                "\n\nA real Mirror starts with a fixed marker line such as "
                "📨 AMessenger · Incoming.\n"
                "Real AMessenger lines end with"
            )
        # A welcome is retried by the startup loop, not persisted as a second
        # pending copy; otherwise the failed attempt and the retry can both
        # appear when the Owner adapter comes back.
        if not await self.mirror_or_queue(
            text, queue_on_failure=False, note_transcript=False
        ):
            logger.warning(
                "[amessenger] welcome not delivered to Owner Chat %s",
                self._owner_chat_id,
            )
            return
        if not await self.mirror_or_queue(
            "To see AMessenger commands:\n/amsg help",
            queue_on_failure=False,
            note_transcript=False,
        ):
            logger.warning(
                "[amessenger] welcome help prompt not delivered to Owner Chat %s",
                self._owner_chat_id,
            )
            return
        self.update_state(lambda document: state.set_welcomed(document, True))
        logger.info("[amessenger] welcome delivered to Owner Chat %s", self._owner_chat_id)

    async def run_housekeeping_loop(self) -> None:
        while self._running:
            try:
                await sleep(HOUSEKEEPING_SECONDS)
                await self.housekeeping_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Reported as its own component. Housekeeping is what ends an
                # expired Grant, so a loop that only logs its failures leaves an
                # Agent acting on the Owner's behalf after the permission ran out.
                self._housekeeping_fault = (
                    "background maintenance is failing\n"
                    f"Cause: {security.safe_field(type(error).__name__)}"
                )
                logger.exception("[amessenger] housekeeping pass failed; continuing")
            else:
                self._last_housekeeping_at = state.ts(state.now())
                self._housekeeping_fault = None

    async def housekeeping_once(self) -> None:
        """Expire Grants and reconcile state with the relay (§6.6)."""
        await self.flush_pending_mirrors()
        moment = state.now()
        ended = []
        forgotten_grants = []
        expired_approvals = []
        timeout_seconds = 300
        listed_ids = None
        try:
            listed_channels = await relay.list_channels(self.client())
            listed_ids = {channel["id"] for channel in listed_channels}
        except (RelayRejected, RelayUnavailable) as error:
            logger.warning(
                "[amessenger] Channel reconciliation skipped because the relay "
                "listing failed: %s",
                error,
            )
        try:
            from tools.approval import _get_approval_config

            timeout_seconds = int(_get_approval_config().get("timeout", 300))
            if timeout_seconds < 0:
                timeout_seconds = 300
        except (
            ImportError,
            AttributeError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            timeout_seconds = 300

        def expire(document):
            nonlocal ended, expired_approvals, forgotten_grants
            updated, ended = state.expire_grants(document, moment)
            updated, expired_approvals = state.expire_pending_approvals(
                updated, moment, timeout_seconds
            )
            if listed_ids is not None:
                grant_ids = {
                    channel_id
                    for channel_id, record in updated.get("channels", {}).items()
                    if isinstance(record, dict)
                    and record.get("grant") in {"single", "standing"}
                }
                updated, forgotten = state.reconcile_channels(updated, listed_ids)
                forgotten_grants = [
                    channel_id
                    for channel_id in forgotten
                    if channel_id not in ended and channel_id in grant_ids
                ]
            return updated

        self.update_state(expire)
        forgotten_channel_records = {
            channel_id: self.known_channel(channel_id)
            for channel_id in forgotten_grants
        }
        for channel_id in forgotten_grants:
            self._channels.pop(channel_id, None)
        if expired_approvals:
            logger.info(
                "[amessenger] expired %d pending approval(s); Hermes already denied them",
                len(expired_approvals),
            )
        if not ended and not forgotten_grants:
            return
        # Write before notices: a Grant must never survive its own expiry because
        # a chat post failed; this is the opposite of the receive path.
        for channel_id in ended:
            posted = await self.mirror_or_queue(
                mirror.grant_ended(self.known_channel(channel_id)),
            )
            if not posted:
                logger.warning(
                    "[amessenger] Grant-ended notice failed for Channel %s", channel_id
                )
        for channel_id in forgotten_grants:
            posted = await self.mirror_or_queue(
                mirror.grant_ended_channel_gone(
                    forgotten_channel_records[channel_id]
                ),
            )
            if not posted:
                logger.warning(
                    "[amessenger] forgotten-Grant notice failed for Channel %s",
                    channel_id,
                )

    async def end_single_grant(self, chat_id: str) -> None:
        """End a single Grant that the Agent reported finished (§6.6)."""
        revoked = False

        def revoke_single(document):
            nonlocal revoked
            if state.channel(document, chat_id)["grant"] != "single":
                return document
            revoked = True
            return state.revoke(document, chat_id)

        self.update_state(revoke_single)
        if not revoked:
            return
        posted = await self.mirror_or_queue(
            mirror.grant_ended(self.known_channel(chat_id)),
        )
        if not posted:
            logger.warning(
                "[amessenger] Grant-ended notice failed for Channel %s",
                chat_id,
            )

    async def disconnect(self) -> None:
        global _LIVE_ADAPTER
        _LIVE_ADAPTER = None
        self._running = False
        for attribute in ("_poll_task", "_housekeeping_task"):
            task = getattr(self, attribute)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    # A cancelled task is the expected outcome during disconnect.
                    setattr(self, attribute, None)
                    continue
                setattr(self, attribute, None)
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
        self._loop = None
        # A clean shutdown must leave a record that says stopped. Leaving the
        # last healthy record behind would let an operator check call a gateway
        # that was deliberately stopped "ready" until the record went stale.
        self.write_health_snapshot(always=True)
        self._mark_disconnected()

    async def _forward_exec_approval(
        self,
        owner,
        chat_id,
        command,
        session_key,
        description,
        allow_permanent,
        allow_session,
        smart_denied,
    ) -> SendResult | None:
        """Try Hermes's interactive approval API, returning None for fallback."""
        if getattr(type(owner), "send_exec_approval", None) is None:
            return None
        try:
            result = await owner.send_exec_approval(
                self._owner_chat_id,
                command,
                session_key,
                description=description,
                # The metadata describes a Channel thread, not the Owner Chat.
                metadata=None,
                allow_permanent=allow_permanent,
                allow_session=allow_session,
                smart_denied=smart_denied,
            )
        except (RuntimeError, TypeError, AttributeError) as error:
            logger.warning(
                "[amessenger] Owner Chat approval buttons unavailable; "
                "using a text card: %s",
                error,
            )
        else:
            if result and getattr(result, "success", False):
                # Preserve the mail session key so the Owner's button resolves
                # this Channel session, not the Owner Chat session.
                # Hermes owns the native card's rendering, but the plugin still
                # owns the durable Owner log.  Record the same wording used by
                # the text-card fallback without trying to post or mark it.
                self._record_owner_log(
                    mirror.approval_request(
                        self.known_channel(chat_id),
                        command,
                        description,
                    )
                )
                return result
            logger.warning(
                "[amessenger] Owner Chat approval forwarding failed: %s; "
                "using a text card",
                getattr(result, "error", result),
            )
        return None

    async def _post_exec_approval_card(
        self, owner, chat_id, command, session_key, description
    ) -> SendResult:
        """Post and record the text-card approval fallback."""
        card = mirror.approval_request(
            self.known_channel(chat_id), command, description
        )
        # Approval cards use the same marked/logged Owner-Chat path as Mirrors.
        # Approval cards are also retried by the approval request itself; do
        # not leave a stale queued card that could outlive its request.
        if await self.mirror_or_queue(
            card, queue_on_failure=False, note_transcript=False
        ):
            self.update_state(
                lambda document: state.add_pending_approval(
                    document,
                    session_key,
                    chat_id,
                    state.now(),
                    channel=self.known_channel(chat_id),
                )
            )
            return SendResult(success=True)

        error = (
            "approval request was not delivered to the Owner Chat because its "
            "adapter is unavailable; start the Owner Chat platform and retry."
        )
        logger.error("[amessenger] %s", error)
        return SendResult(success=False, error=error)

    async def send_exec_approval(
        self,
        chat_id,
        command,
        session_key,
        description="dangerous command",
        metadata=None,
        allow_permanent=True,
        allow_session=True,
        smart_denied=False,
    ) -> SendResult:
        owner = await self.wait_for_owner_adapter()
        if owner is None:
            return SendResult(
                success=False,
                error=(
                    "approval request was not delivered to the Owner Chat because "
                    "its adapter is unavailable; start the Owner Chat platform and retry."
                ),
            )
        # Nothing in this method may post into the Channel: the peer must never
        # learn that an approval was asked for, let alone answer it.
        forwarded = await self._forward_exec_approval(
            owner,
            chat_id,
            command,
            session_key,
            description,
            allow_permanent,
            allow_session,
            smart_denied,
        )
        if forwarded is not None:
            return forwarded
        return await self._post_exec_approval_card(
            owner, chat_id, command, session_key, description
        )

    def _keep_cap_record(self, channel_id: str) -> None:
        """Switch a capped Channel to notify without discarding its window."""
        def keep_record(document):
            record = document["channels"].get(channel_id)
            if record is None:
                return document
            channels = {
                **document["channels"],
                channel_id: {**record, "policy": "notify"},
            }
            return {**document, "channels": channels}

        self.update_state(keep_record)

    async def _cap_result(self, channel_id: str | None) -> SendResult:
        """Switch a capped Channel to notify and report the deliberate drop."""
        self._keep_cap_record(channel_id)
        posted = await self.mirror_or_queue(
            mirror.cap_reached(self.known_channel(channel_id)),
        )
        suffix = "" if posted else "; cap notice failed"
        logger.warning(
            "[amessenger] reply cap reached for Channel %s%s",
            channel_id,
            suffix,
        )
        return SendResult(success=True, message_id=None)

    async def deliver_to_channel(
        self,
        channel_id: str | None,
        text: str,
        *,
        count_reply: bool,
        to: str | None = None,
    ) -> SendResult:
        """Redact, send to the relay, Mirror to the Owner, and count a reply.

        This is the only path by which a Message reaches a peer. ``to`` is used
        only by the Owner-side tool; the relay resolves it to a Channel as part
        of the same POST.
        """
        guarded = self._send_guard(channel_id, text, None)
        if guarded is not None:
            return guarded

        moment = state.now() if count_reply else None
        if count_reply and state.cap_reached(self.state(), channel_id, moment):
            return await self._cap_result(channel_id)

        redacted = security.redact_outbound(text)
        if self.on_gateway_loop():
            client = self.client()
            close_client = False
        else:
            settings = self._settings or read_settings()
            client = relay.build_client(
                settings["url"],
                settings["key"],
                settings["agent"],
                transport=self._transport,
            )
            close_client = True
        try:
            try:
                # This is the only path by which a Message reaches a peer.
                send_arguments = {"text": redacted}
                if to is None:
                    send_arguments["channel_id"] = channel_id
                else:
                    send_arguments["to"] = to
                result = await relay.send_message(client, **send_arguments)
            except RelayRejected as error:
                logger.warning("[amessenger] outbound relay send failed: %s", error)
                if error.status == 404 and channel_id is not None:
                    self._drop_forgotten_channel(channel_id)
                return SendResult(
                    success=False,
                    error=(relay.CHANNEL_GONE if error.status == 404 else str(error)),
                    raw_response=error,
                    retryable=False,
                )
            except RelayUnavailable as error:
                logger.warning("[amessenger] outbound relay send failed: %s", error)
                return SendResult(
                    success=False,
                    error=relay_failure_detail(error),
                    raw_response=error,
                    retryable=relay_failure_is_retryable(error),
                )
        finally:
            if close_client:
                await client.aclose()

        if not (
            isinstance(result, dict)
            and isinstance(result.get("channel"), dict)
            and isinstance(result["channel"].get("id"), str)
            and bool(result["channel"]["id"])
            and isinstance(result.get("message"), dict)
            and isinstance(result["message"].get("id"), str)
            and bool(result["message"]["id"])
        ):
            return SendResult(
                success=False,
                error=relay.MALFORMED_SEND,
                raw_response=result,
                retryable=False,
            )

        self.remember_channel(result["channel"])
        outgoing_line = mirror.outgoing(result["channel"], redacted)
        if self._loop is None or self.on_gateway_loop():
            posted = await self.mirror_or_queue(outgoing_line)
        else:
            # Tool calls may run on a worker loop; post_owner_line hands the
            # Mirror to the gateway loop, where it goes through mirror_or_queue.
            posted = await self.post_owner_line(outgoing_line)
        if not posted:
            logger.warning(
                "[amessenger] outgoing Mirror failed for Channel %s; send succeeded",
                channel_id or result["channel"]["id"],
            )

        if count_reply:
            self.update_state(
                lambda document: state.note_reply(document, channel_id, moment)
            )

        return SendResult(
            success=True,
            message_id=result["message"]["id"],
            raw_response=result,
        )

    def _send_guard(self, chat_id, content, metadata) -> SendResult | None:
        """Handle gateway-generated sends that must never become peer mail."""
        if isinstance(content, str) and content.startswith(
            DELIVERY_FAILURE_NOTICE_PREFIX
        ):
            logger.warning(
                "[amessenger] dropping Hermes delivery-failure notice; it is not mail"
            )
            return SendResult(success=True, message_id=None)

        if isinstance(content, str) and content.startswith(GATEWAY_NOTICE_PREFIXES):
            logger.warning(
                "[amessenger] dropping Hermes control notice; it is not mail"
            )
            return SendResult(success=True, message_id=None)

        if isinstance(content, str) and content.startswith(FORMATTING_FALLBACK_PREFIX):
            logger.warning("[amessenger] dropping gateway re-send; it is not mail")
            return SendResult(success=True, message_id=None)

        if isinstance(metadata, dict) and metadata.get(INTERIM_SEND_KEY):
            logger.debug("[amessenger] dropping interim send for Channel %s", chat_id)
            return SendResult(success=True, message_id=None)
        return None

    async def send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        guarded = self._send_guard(chat_id, content, metadata)
        if guarded is not None:
            return guarded

        task_done = security.has_task_done(content)
        no_reply = security.is_only_no_reply(content)
        text = security.strip_markers(content)
        moment = state.now()

        if state.cap_reached(self.state(), chat_id, moment):
            return await self._cap_result(chat_id)

        if no_reply or not text:
            logger.debug("[amessenger] no outbound reply for Channel %s", chat_id)
            if no_reply:
                self.update_state(
                    lambda document: state.note_reply(document, chat_id, moment)
                )
            if task_done:
                await self.end_single_grant(chat_id)
            return SendResult(success=True, message_id=None)

        result = await self.deliver_to_channel(
            chat_id,
            text,
            count_reply=True,
        )
        if result.success and task_done and result.message_id is not None:
            await self.end_single_grant(chat_id)
        return result

    def owner_log_path(self) -> Path:
        return owner_log_path_for_process()

    def _record_owner_log(self, text: str) -> None:
        """Best-effort append of the unmarked copy received by the Owner."""
        try:
            state.append_owner_log(self.owner_log_path(), text)
        except Exception as error:
            # The Owner-Chat post is the product; a log filesystem failure is
            # observable but must never turn a successful post into a failure.
            logger.warning("[amessenger] Owner log append failed: %s", error)

    async def _post_owner_line_now(
        self,
        text: str,
        *,
        framed_text: str | None = None,
        note_transcript: bool = True,
    ) -> bool:
        """Post one line, marking only the chat copy and logging it unmarked."""
        if not self._owner_chat_id:
            # No Owner Chat yet (waiting for /amsg setup or /sethome): the
            # caller queues the line for the chat that will claim it. Not a
            # posting fault -- nothing has been asked of a chat yet.
            return False
        owner = await self.wait_for_owner_adapter()
        if owner is None:
            self.record_post_failure("the Owner Chat platform did not start")
            return False
        marked_text = mirror.with_mark(
            text, self.state()[state.AUTHENTICITY_SECRET_KEY]
        )
        posted = await mirror.mirror(
            owner,
            self._owner_platform,
            self._owner_chat_id,
            marked_text,
            framed_text=framed_text,
            transcript_text=text if note_transcript else None,
            note_transcript=note_transcript,
        )
        if posted:
            self._record_owner_log(text)
            self.record_post_success()
        else:
            self.record_post_failure("the Owner Chat refused the line")
        return posted

    def record_post_success(self) -> None:
        """One Owner line arrived; only this clears an Owner-posting fault."""
        self._last_post_at = state.ts(state.now())
        self._post_fault = None

    def record_post_failure(self, reason: str) -> None:
        """The Owner Chat could not be written to.

        Kept apart from the relay fault on purpose. A relay poll that succeeds
        says nothing about whether a human can see anything, and clearing this
        on an unrelated success is exactly how a dead Owner Chat looked healthy.
        """
        self._post_fault = (
            "the Owner Chat cannot be written to\n"
            f"Cause: {security.safe_field(reason)}"
        )

    async def mirror_or_queue(
        self,
        text: str,
        *,
        framed_text: str | None = None,
        queue_on_failure: bool = True,
        note_transcript: bool = True,
    ) -> bool:
        """Post an Owner line, optionally persisting it for a later retry."""
        posted = await self._post_owner_line_now(
            text,
            framed_text=framed_text,
            note_transcript=note_transcript,
        )
        if posted:
            return True

        if not queue_on_failure:
            return False

        dropped = False

        def queue(document):
            nonlocal dropped
            dropped = len(document.get("pending_mirrors", [])) >= PENDING_MIRRORS_MAX
            return state.queue_mirror(
                document,
                text,
                framed_text=framed_text,
                note_transcript=note_transcript,
            )

        self.update_state(queue)
        logger.warning("[amessenger] Owner-facing line queued for retry")
        if dropped:
            # Counted, not only logged: a queue that overflows silently is a
            # Message the Owner will never see and never be told about.
            self._mirrors_lost += 1
            logger.warning(
                "[amessenger] dropped 1 oldest queued Owner-facing line; "
                "1 line lost"
            )
        return False

    async def flush_pending_mirrors(self) -> None:
        """Retry the Owner-facing lines the Owner Chat refused earlier."""
        pending = self.state().get("pending_mirrors", [])
        pending_texts = [state.mirror_entry_text(entry) for entry in pending]
        if self._pending_mirrors != pending_texts:
            if (
                len(self._pending_mirrors) >= len(pending_texts)
                and self._pending_mirrors[: len(pending_texts)] == pending_texts
            ):
                for text in self._pending_mirrors[len(pending_texts) :]:
                    self.update_state(
                        lambda document, text=text: state.queue_mirror(document, text)
                    )
            else:
                self._pending_mirrors = [*pending_texts]

        pending = self.state().get("pending_mirrors", [])
        if not pending:
            return

        owner = await self.wait_for_owner_adapter()
        if owner is None:
            return

        delivered = 0
        while self.state().get("pending_mirrors", []):
            entry = self.state()["pending_mirrors"][0]
            text = state.mirror_entry_text(entry)
            framed_text = state.mirror_entry_transcript(entry)
            if not await self._post_owner_line_now(
                text,
                framed_text=framed_text,
                note_transcript=state.mirror_entry_should_note(entry),
            ):
                break

            def remove_delivered(document):
                pending_lines = document.get("pending_mirrors", [])
                if pending_lines and pending_lines[0] == entry:
                    updated, _popped = state.pop_mirror(document)
                    return updated
                return document

            self.update_state(remove_delivered)
            delivered += 1

        if delivered:
            logger.info("[amessenger] delivered %d queued Owner-facing lines", delivered)

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": mirror.channel_name(self.known_channel(chat_id)), "type": "dm"}
