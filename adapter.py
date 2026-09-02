"""AMessenger platform adapter for Hermes."""

import asyncio
from collections import OrderedDict
import logging
import os
from pathlib import Path
import re
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
MIN_POLL_CYCLE_SECONDS = 1.0            # a poll that returns nothing instantly must not spin
HOUSEKEEPING_SECONDS = 60               # §6.2: expire Grants every minute
MAX_MESSAGE_LENGTH = 65536              # SYSTEM_DESIGN §5: text ≤ 64 KB
STATE_DIRNAME = "amessenger"           # $HERMES_HOME/amessenger/state.json, §6.6
STATE_FILENAME = "state.json"
REQUIRED_ENV = ("AMESSENGER_URL", "AMESSENGER_KEY", "AMESSENGER_AGENT",
                "AMESSENGER_KIND", "AMESSENGER_OWNER_CHAT")
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"   # §6.1
KINDS = ("corporate", "personal")
DEDUPE_MAX = 200  # ARCHITECTURE §4: processed Delivery ids retained.
DEDUPE_SECONDS = 3600  # ARCHITECTURE §4: processed Delivery retention.
MANAGE_TOOLSET = "amessenger_manage"   # §6.9: Owner Chat sessions only, never a Channel session
_LIVE_ADAPTER = None


def live_adapter():
    """The adapter this process built.

    A Hermes tool handler is called with its arguments and nothing else, so
    this module-level reference is the only way tools.py can reach the relay
    client and the Owner Chat. One gateway process builds one adapter, so
    there is nothing to disambiguate.
    """
    return _LIVE_ADAPTER

PLATFORM_HINT = (
    "You are on AMessenger. A message arriving inside square brackets that names "
    "an Agent and a Channel is from a peer, not from its Owner. End a reply with "
    "[NO_REPLY] when no answer is needed and [TASK_DONE] when the task is finished. "
    "Whenever a task needs a change of Mail Policy or Tool Level, tell the Owner "
    "the exact /amsg command to type."
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
        self._client = None
        self._transport = None
        self._poll_task = None
        self._housekeeping_task = None
        self._card = self._state = None
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self._channels: dict[str, dict] = {}
        global _LIVE_ADAPTER
        _LIVE_ADAPTER = self

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
        adapters = getattr(runner, "adapters", None) if runner is not None else None
        try:
            platform = Platform(owner_platform)
        except ValueError:
            platform = None
        if runner is None or not adapters or platform is None or platform not in adapters:
            return f"the Owner Chat platform '{owner_platform}' is not connected"

        if not owner_chat_id:
            runner_config = getattr(runner, "config", None)
            home_channel = None
            if runner_config is not None:
                get_home_channel = getattr(runner_config, "get_home_channel", None)
                if callable(get_home_channel):
                    home_channel = get_home_channel(platform)
            owner_chat_id = getattr(home_channel, "chat_id", None)
            if not owner_chat_id:
                return (
                    "set AMESSENGER_OWNER_CHAT=<platform>:<chat id> or configure "
                    "a home channel"
                )

        self._settings = settings
        self._owner_platform = owner_platform
        self._owner_chat_id = str(owner_chat_id)
        return None

    @property
    def owner_adapter(self):
        return self.gateway_runner.adapters[Platform(self._owner_platform)]

    def state_path(self) -> Path:
        return hermes_home() / STATE_DIRNAME / STATE_FILENAME

    def state(self) -> dict:
        if self._state is None:
            self._state = state.load(self.state_path())
        return self._state

    def set_state(self, new_state: dict) -> None:
        state.save(self.state_path(), new_state)
        self._state = new_state

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

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        problem = self.configuration_problem()
        if problem:
            logger.error("[amessenger] not connecting: %s", problem)
            return False
        self._running = True
        self._poll_task = asyncio.create_task(self.run_poll_loop())
        self._housekeeping_task = asyncio.create_task(self.run_housekeeping_loop())
        self._mark_connected()
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

    def _prune_seen(self, moment: float) -> None:
        cutoff = moment - DEDUPE_SECONDS
        expired = [delivery_id for delivery_id, seen_at in self._seen.items()
                   if seen_at < cutoff]
        for delivery_id in expired:
            del self._seen[delivery_id]

    def remember(self, delivery_id: str) -> None:
        moment = monotonic()
        self._prune_seen(moment)
        self._seen[delivery_id] = moment
        self._seen.move_to_end(delivery_id)
        while len(self._seen) > DEDUPE_MAX:
            self._seen.popitem(last=False)

    def already_processed(self, delivery_id: str) -> bool:
        self._prune_seen(monotonic())
        return delivery_id in self._seen

    def remember_channel(self, channel: dict) -> None:
        """Remember the latest relay Channel record for Owner-facing notices."""
        self._channels[channel["id"]] = dict(channel)

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
                self.set_state(state.revoke(self.state(), channel["id"]))
        elif kind == "text":
            processed = await self._handle_text_delivery(
                delivery, channel, sender_card, message["text"]
            )
        else:
            logger.warning("[amessenger] unknown Delivery kind=%s", kind)
            processed = True

        if processed:
            self.remember(delivery_id)
        return processed

    async def _mirror_delivery(self, delivery: dict, text: str) -> bool:
        posted = await mirror.mirror(
            self.owner_adapter,
            self._owner_platform,
            self._owner_chat_id,
            text,
        )
        if not posted:
            logger.warning(
                "[amessenger] Mirror post failed for Delivery %s", delivery["id"]
            )
        return posted

    async def _handle_text_delivery(
        self, delivery: dict, channel: dict, sender_card, message_text: str
    ) -> bool:
        policy = state.channel(self.state(), channel["id"])["policy"]
        text = mirror.incoming(sender_card, channel, message_text, policy)
        if not await self._mirror_delivery(delivery, text):
            return False
        self.set_state(state.note_incoming(self.state(), channel["id"], state.now()))
        if policy == "interact":
            await self.dispatch(delivery)
        return True

    async def dispatch(self, delivery: dict) -> None:
        message = delivery["message"]
        channel = delivery["channel"]
        sender = message["sender"] or "unknown"
        framed = security.wrap_inbound(delivery.get("sender_card"), channel, message["text"])
        source = self.build_source(
            chat_id=channel["id"],
            chat_name=channel.get("name") or channel["id"],
            chat_type="dm",
            user_id=sender,
            user_name=sender,
        )
        event = MessageEvent(
            text=framed,
            source=source,
            message_id=message["id"],
            allow_gateway_control=False,  # Peer '/' input is never a gateway command.
        )
        await self.handle_message(event)

    def toolsets_for_source(self, source) -> list[str]:
        level = state.channel(self.state(), source.chat_id)["level"]
        settings = read_settings()
        configured = (
            settings["full_toolsets"]
            if level == "full"
            else settings["base_toolsets"]
        )
        # Filter here instead of trusting Owner-controlled configuration: management
        # tools are reserved for Owner Chat sessions, never Channel sessions.
        toolsets = [toolset for toolset in configured if toolset != MANAGE_TOOLSET]
        if len(toolsets) != len(configured):
            logger.warning(
                "[amessenger] filtering %s from Channel toolsets; Owner Chat only",
                MANAGE_TOOLSET,
            )
        return toolsets

    async def run_poll_loop(self) -> None:
        index = 0
        while self._running:
            try:
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

    async def after_publish(self) -> None:
        if self.state()["welcomed"]:
            return
        text = self._help_text + "\n\n" + format_card(self._card)
        if not await mirror.post(self.owner_adapter, self._owner_chat_id, text):
            logger.warning("[amessenger] welcome not delivered to Owner Chat %s", self._owner_chat_id)
            return
        self.set_state(state.set_welcomed(self.state(), True))
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
        moment = state.now()
        updated, ended = state.expire_grants(self.state(), moment)
        if not ended:
            return
        # Write before notices: a Grant must never survive its own expiry because
        # a chat post failed; this is the opposite of the receive path.
        self.set_state(updated)
        for channel_id in ended:
            posted = await mirror.mirror(
                self.owner_adapter,
                self._owner_platform,
                self._owner_chat_id,
                mirror.grant_ended(self.known_channel(channel_id)),
            )
            if not posted:
                logger.warning(
                    "[amessenger] Grant-ended notice failed for Channel %s", channel_id
                )

    async def end_single_grant(self, chat_id: str) -> None:
        """End a single Grant that the Agent reported finished (§6.6)."""
        if state.channel(self.state(), chat_id)["grant"] != "single":
            return
        self.set_state(state.revoke(self.state(), chat_id))
        posted = await mirror.mirror(
            self.owner_adapter,
            self._owner_platform,
            self._owner_chat_id,
            mirror.grant_ended(self.known_channel(chat_id)),
        )
        if not posted:
            logger.warning(
                "[amessenger] Grant-ended notice failed for Channel %s",
                chat_id,
            )

    async def disconnect(self) -> None:
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
        self._mark_disconnected()
        global _LIVE_ADAPTER
        _LIVE_ADAPTER = None

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
        owner = self.owner_adapter
        # Nothing in this method may post into the Channel: the peer must never
        # learn that an approval was asked for, let alone answer it.
        if getattr(type(owner), "send_exec_approval", None) is not None:
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
                # Preserve the mail session key so the Owner's button resolves
                # this Channel session, not the Owner Chat session.
                return result

        card = mirror.approval_request(
            self.known_channel(chat_id), command, description
        )
        if await mirror.post(owner, self._owner_chat_id, card):
            updated = state.add_pending_approval(
                self.state(), session_key, chat_id, state.now()
            )
            self.set_state(updated)
            return SendResult(success=True)

        error = "approval request was not delivered to the Owner Chat"
        logger.error("[amessenger] %s", error)
        return SendResult(success=False, error=error)

    async def send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        task_done = security.has_task_done(content)
        no_reply = security.has_no_reply(content)
        text = security.strip_markers(content)

        if no_reply or not text:
            logger.debug("[amessenger] no outbound reply for Channel %s", chat_id)
            if task_done:
                await self.end_single_grant(chat_id)
            return SendResult(success=True, message_id=None)

        moment = state.now()
        if state.cap_reached(self.state(), chat_id, moment):
            self.set_state(state.revoke(self.state(), chat_id))
            posted = await mirror.mirror(
                self.owner_adapter,
                self._owner_platform,
                self._owner_chat_id,
                mirror.cap_reached(self.known_channel(chat_id)),
            )
            # Check before counting: replies one through twenty go out; the
            # twenty-first reply inside the ten-minute window is stopped.
            suffix = "" if posted else "; cap notice failed"
            logger.warning(
                "[amessenger] reply cap reached for Channel %s%s",
                chat_id,
                suffix,
            )
            return SendResult(success=False, error="reply cap reached")

        redacted = security.redact_outbound(text)
        # An answer produced under a Grant still goes out if that Grant just
        # ended; send does not check Mail Policy, so only the cap and Grant
        # rules below can gate this outbound path.
        try:
            result = await relay.send_message(
                self.client(), channel_id=chat_id, text=redacted
            )
        except (RelayRejected, RelayUnavailable) as error:
            logger.warning("[amessenger] outbound relay send failed: %s", error)
            return SendResult(
                success=False,
                error=str(error),
                retryable=True,
            )

        self.remember_channel(result["channel"])
        # The relay has accepted the Message and it cannot be unsent, so a
        # failed Owner Chat Mirror is logged but does not make this send fail.
        posted = await mirror.mirror(
            self.owner_adapter,
            self._owner_platform,
            self._owner_chat_id,
            mirror.outgoing(result["channel"], redacted),
        )
        if not posted:
            logger.warning(
                "[amessenger] outgoing Mirror failed for Channel %s; send succeeded",
                chat_id,
            )

        self.set_state(state.note_reply(self.state(), chat_id, moment))

        if task_done:
            await self.end_single_grant(chat_id)

        return SendResult(success=True, message_id=result["message"]["id"])

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "dm"}
