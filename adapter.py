"""AMessenger platform adapter for Hermes."""

import asyncio
import logging
import os
from pathlib import Path
import re
import time

import httpx

from . import state
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult


logger = logging.getLogger("amessenger")

PLATFORM_NAME = "amessenger"
RECONNECT_BACKOFF = (1, 2, 5, 10, 30)   # seconds, §6.2: reconnect backoff 1–30 s
WAIT_TIMEOUT_SECONDS = 25               # §6.2: GET /v1/inbox/wait?timeout=25
MIN_POLL_CYCLE_SECONDS = 1.0            # a poll that returns nothing instantly must not spin
HOUSEKEEPING_SECONDS = 60               # §6.2: expire Grants every minute
HTTP_TIMEOUT_SECONDS = 10               # every relay call except the long poll
MAX_MESSAGE_LENGTH = 65536              # SYSTEM_DESIGN §5: text ≤ 64 KB
STATE_DIRNAME = "amessenger"           # $HERMES_HOME/amessenger/state.json, §6.6
STATE_FILENAME = "state.json"
REQUIRED_ENV = ("AMESSENGER_URL", "AMESSENGER_KEY", "AMESSENGER_AGENT",
                "AMESSENGER_KIND", "AMESSENGER_OWNER_CHAT")
AGENT_NAME_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"   # §6.1
KINDS = ("corporate", "personal")

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
def format_card(card: dict | None) -> str:
    if card is None: return "Your Card is published."
    owner = card.get("owner") or {}
    lines = ["Your Card is published:",
             f"  Agent: {card.get('name') or 'unknown'} ({card.get('kind') or 'unknown'})",
             f"  Owner: {owner.get('name') or owner.get('login') or 'unknown'} <{owner.get('email') or 'unknown'}>"]
    if card.get("description"): lines.append(f"  About: {card['description']}")
    return "\n".join(lines)
class CardConflict(Exception):
    """The relay rejected this Agent's Card because its identity conflicts."""

class RelayUnavailable(Exception):
    """The relay could not complete a request."""


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


def _safe_error(message, key: str) -> str:
    text = str(message)
    return text.replace(key, "[redacted]") if key else text


def _response_detail(response: httpx.Response, fallback: str, key: str) -> str:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return fallback
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail.strip():
            return _safe_error(detail, key)
    return fallback


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
    def state_path(self) -> Path: return hermes_home() / STATE_DIRNAME / STATE_FILENAME
    def state(self) -> dict:
        if self._state is None: self._state = state.load(self.state_path())
        return self._state
    def set_state(self, new_state: dict) -> None:
        state.save(self.state_path(), new_state)
        self._state = new_state

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            settings = self._settings or read_settings()
            self._client = httpx.AsyncClient(
                base_url=settings["url"],
                transport=self._transport,
                timeout=HTTP_TIMEOUT_SECONDS,
                trust_env=False,
                headers={
                    "Authorization": "Bearer " + settings["key"],
                    "X-Agent": settings["agent"],
                },
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
        body = {"kind": settings["kind"]}
        if settings["description"]:
            body["description"] = settings["description"]
        try:
            response = await self.client().put("/v1/agents/me", json=body)
        except httpx.HTTPError as error:
            raise RelayUnavailable(_safe_error(error, settings["key"])) from error
        if response.status_code == 409:
            detail = _response_detail(response, "HTTP 409", settings["key"])
            raise CardConflict(detail)
        if response.status_code != 200:
            raise RelayUnavailable(f"HTTP {response.status_code}")
        try:
            card = response.json()
        except (TypeError, ValueError) as error:
            raise RelayUnavailable("malformed Card response") from error
        if not isinstance(card, dict):
            raise RelayUnavailable("malformed Card response")
        self._card = card
        return card

    async def fetch_deliveries(self) -> list[dict]:
        settings = self._settings or read_settings()
        timeout = httpx.Timeout(
            HTTP_TIMEOUT_SECONDS, read=WAIT_TIMEOUT_SECONDS + HTTP_TIMEOUT_SECONDS
        )
        try:
            response = await self.client().get(
                "/v1/inbox/wait",
                params={"timeout": WAIT_TIMEOUT_SECONDS},
                timeout=timeout,
            )
        except httpx.HTTPError as error:
            raise RelayUnavailable(_safe_error(error, settings["key"])) from error
        if response.status_code != 200:
            raise RelayUnavailable(f"HTTP {response.status_code}")
        try:
            payload = response.json()
            deliveries = payload["deliveries"]
        except (KeyError, TypeError, ValueError) as error:
            raise RelayUnavailable("malformed deliveries response") from error
        if not isinstance(deliveries, list):
            raise RelayUnavailable("malformed deliveries response")
        return deliveries

    async def poll_once(self) -> list[dict]:
        # Task T5.1 adds the Mirror, dispatch and Ack here.
        return await self.fetch_deliveries()

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
            except RelayUnavailable as error:
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
        result = await self.owner_adapter.send(self._owner_chat_id, text)
        if not result or not getattr(result, "success", False):
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
        # Task T7.3 expires Grants and posts the notices.
        return None

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
                    pass
                setattr(self, attribute, None)
        client = self._client
        self._client = None
        if client is not None:
            await client.aclose()
        self._mark_disconnected()

    async def send(
        self,
        chat_id,
        content,
        reply_to=None,
        metadata=None,
    ) -> SendResult:
        # Task T6.3 replaces this whole placeholder body with outbound mail.
        logger.error("[amessenger] send arrives in task T6.3")
        return SendResult(success=False, error="AMessenger send arrives in task T6.3")

    async def get_chat_info(self, chat_id: str) -> dict:
        return {"name": chat_id, "type": "dm"}
