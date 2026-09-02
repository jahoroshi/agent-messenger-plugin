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

from . import security, state
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
REQUIRED_ENV = ("AMESSENGER_URL", "AMESSENGER_KEY", "AMESSENGER_AGENT",
                "AMESSENGER_KIND", "AMESSENGER_OWNER_CHAT")
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"   # §6.1
KINDS = ("corporate", "personal")
DEDUPE_MAX = state.DEDUPE_MAX
DEDUPE_SECONDS = state.DEDUPE_SECONDS
SEND_IDEMPOTENCY_SECONDS = state.SEND_IDEMPOTENCY_SECONDS
CHANNELS_MAX = 200  # Remembered Channel records used for Owner-facing labels.
PENDING_MIRRORS_MAX = state.PENDING_MIRRORS_MAX
OWNER_POST_TIMEOUT_SECONDS = 15
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
)
INTERIM_SEND_KEY = "_interim_send"
_LIVE_ADAPTER = None


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


def read_settings() -> dict:
    """Read AMessenger settings from the environment at call time."""
    return {
        "url": os.getenv("AMESSENGER_URL", "").rstrip("/"),
        "key": os.getenv("AMESSENGER_KEY", ""),
        "agent": os.getenv("AMESSENGER_AGENT", ""),
        "kind": os.getenv("AMESSENGER_KIND", ""),
        "description": os.getenv("AMESSENGER_DESCRIPTION", ""),
        "owner_chat": os.getenv("AMESSENGER_OWNER_CHAT", ""),
        "owner_user": os.getenv("AMESSENGER_OWNER_USER", ""),
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


def check_requirements() -> bool:
    """Return whether all required environment values are non-blank."""
    return all(os.getenv(name, "").strip() for name in REQUIRED_ENV)


def validate_config(config) -> bool:
    """Validate environment configuration; config.extra is intentionally ignored."""
    return check_requirements()


def is_connected(config) -> bool:
    """Report whether the platform has the minimum environment configuration."""
    return check_requirements()


def env_enablement() -> dict | None:
    """Return the values needed to seed a minimally configured platform."""
    if not check_requirements():
        return None
    settings = read_settings()
    return {
        "url": settings["url"],
        "agent": settings["agent"],
        "kind": settings["kind"],
    }


class AMessengerAdapter(BasePlatformAdapter):
    def __init__(self, config: PlatformConfig, help_text: str = "") -> None:
        super().__init__(config=config, platform=Platform(PLATFORM_NAME))
        self._help_text = help_text
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

    @property
    def authorization_is_upstream(self) -> bool:
        # The relay only delivers Messages from Channels this Agent has joined,
        # so the sender is already authorized upstream (§6.4, ADR-0003).
        return True

    def configuration_problem(self) -> str | None:
        """Return the first reason this Agent cannot use AMessenger mail."""
        settings = read_settings()
        missing = [name for name in REQUIRED_ENV if not os.getenv(name, "").strip()]
        if missing:
            return "missing " + ", ".join(missing)

        agent = settings["agent"]
        if re.fullmatch(AGENT_NAME_PATTERN, agent) is None:
            return (
                "AMESSENGER_AGENT must be 2–32 characters using lowercase letters, "
                "digits, and hyphens, starting with a lowercase letter or digit"
            )
        if settings["kind"] not in KINDS:
            return "AMESSENGER_KIND must be one of: corporate, personal"

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
                return (
                    "set AMESSENGER_OWNER_CHAT=<platform>:<chat id> or configure "
                    "a home channel"
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
        global _LIVE_ADAPTER
        _LIVE_ADAPTER = None
        self._running = False
        problem = self.configuration_problem()
        if problem:
            logger.error("[amessenger] not connecting: %s", problem)
            return False
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._poll_task = asyncio.create_task(self.run_poll_loop())
        self._housekeeping_task = asyncio.create_task(self.run_housekeeping_loop())
        self._mark_connected()
        _LIVE_ADAPTER = self
        logger.info(
            "[amessenger] connected Agent %s with Owner Chat %s:%s",
            self._settings["agent"],
            self._owner_platform,
            self._owner_chat_id,
        )
        return True

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
        channel_id = channel["id"]
        self._channels.pop(channel_id, None)
        self._channels[channel_id] = dict(channel)
        while len(self._channels) > CHANNELS_MAX:
            self._channels.popitem(last=False)

    def known_channel(self, channel_id: str) -> dict:
        """Return a remembered Channel, or a renderable unnamed fallback."""
        channel = self._channels.get(channel_id)
        return dict(channel) if channel is not None else {"id": channel_id, "name": None}

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
        self.remember_channel(channel)
        sender_card = delivery.get("sender_card")
        kind = message["kind"]
        logger.info("[amessenger] Delivery %s kind=%s", delivery_id, kind)

        if self.already_processed(delivery_id):
            logger.debug("[amessenger] Delivery %s already processed", delivery_id)
            return True

        if kind == "invite":
            text = mirror.invite(channel, message["text"])
            processed = await self._mirror_delivery(delivery, text)
        elif kind in {"joined", "left", "closed", "removed"}:
            text = mirror.notice(channel, message["text"])
            processed = await self._mirror_delivery(delivery, text)
            if processed and kind in {"closed", "removed"}:
                self.update_state(
                    lambda document: state.revoke(document, channel["id"])
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
            chat_name=channel.get("name") or channel["id"],
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
                if flush_at_start:
                    await self.flush_pending_mirrors()
                    flush_at_start = False
                if self._card is None:
                    self._card = await self.publish_card()
                    await self.after_publish()
                started = monotonic()
                deliveries = await self.poll_once()
                if not deliveries and monotonic() - started < MIN_POLL_CYCLE_SECONDS:
                    await sleep(MIN_POLL_CYCLE_SECONDS)
                index = 0
            except asyncio.CancelledError:
                raise
            except CardConflict as error:
                logger.error("[amessenger] stopping: %s", error)
                self._running = False
                self._mark_disconnected()
                return
            except (RelayRejected, RelayUnavailable) as error:
                wait = RECONNECT_BACKOFF[min(index, len(RECONNECT_BACKOFF) - 1)]
                logger.warning(
                    "[amessenger] relay unavailable (%s); retrying in %ss",
                    error,
                    wait,
                )
                self._card = None
                index += 1
                await sleep(wait)
            except Exception:
                wait = RECONNECT_BACKOFF[min(index, len(RECONNECT_BACKOFF) - 1)]
                logger.exception(
                    "[amessenger] poll pass failed; continuing in %ss",
                    wait,
                )
                self._card = None
                index += 1
                await sleep(wait)

    async def after_publish(self) -> None:
        if self.state()["welcomed"]:
            return
        # Leave the mark itself to the single Owner-post seam. Ending the
        # explanatory sentence here makes that seam turn it into the one
        # concrete mark the Owner should remember, without putting the mark in
        # the log or any model-visible transcript.
        text = (
            self._help_text
            + "\n\n"
            + format_card(self._card)
            + "\n\nReal AMessenger lines end with"
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
        self.update_state(lambda document: state.set_welcomed(document, True))
        logger.info("[amessenger] welcome delivered to Owner Chat %s", self._owner_chat_id)

    async def run_housekeeping_loop(self) -> None:
        while self._running:
            try:
                await sleep(HOUSEKEEPING_SECONDS)
                await self.housekeeping_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[amessenger] housekeeping pass failed; continuing")

    async def housekeeping_once(self) -> None:
        """Expire single Grants and tell the Owner about each one (§6.6)."""
        await self.flush_pending_mirrors()
        moment = state.now()
        ended = []
        expired_approvals = []
        timeout_seconds = 300
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
            nonlocal ended, expired_approvals
            updated, ended = state.expire_grants(document, moment)
            updated, expired_approvals = state.expire_pending_approvals(
                updated, moment, timeout_seconds
            )
            return updated

        self.update_state(expire)
        if expired_approvals:
            logger.info(
                "[amessenger] expired %d pending approval(s); Hermes already denied them",
                len(expired_approvals),
            )
        if not ended:
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
        self._mark_disconnected()

    async def _forward_exec_approval(
        self,
        owner,
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
            self.known_channel(chat_id), command, description, mirror.handle(chat_id)
        )
        # Approval cards use the same marked/logged Owner-Chat path as Mirrors.
        # Approval cards are also retried by the approval request itself; do
        # not leave a stale queued card that could outlive its request.
        if await self.mirror_or_queue(
            card, queue_on_failure=False, note_transcript=False
        ):
            self.update_state(
                lambda document: state.add_pending_approval(
                    document, session_key, chat_id, state.now()
                )
            )
            return SendResult(success=True)

        error = "approval request was not delivered to the Owner Chat"
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
                error="approval request was not delivered to the Owner Chat",
            )
        # Nothing in this method may post into the Channel: the peer must never
        # learn that an approval was asked for, let alone answer it.
        forwarded = await self._forward_exec_approval(
            owner,
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
        if text.startswith(DELIVERY_FAILURE_NOTICE_PREFIX):
            logger.warning(
                "[amessenger] dropping Hermes delivery-failure notice; it is not mail"
            )
            return SendResult(success=True, message_id=None)

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
                return SendResult(
                    success=False,
                    error=str(error),
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
        owner = await self.wait_for_owner_adapter()
        if owner is None:
            return False
        marked_text = f"{text} {mirror.authenticity_mark(self.state()[state.AUTHENTICITY_SECRET_KEY])}"
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
        return posted

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
        return {"name": chat_id, "type": "dm"}
