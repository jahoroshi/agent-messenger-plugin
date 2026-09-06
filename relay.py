"""Small HTTP client for the AMessenger relay."""

from . import mirror, security

import httpx


HTTP_TIMEOUT_SECONDS = 10       # every relay call except the long poll
WAIT_TIMEOUT_SECONDS = 25       # §6.2: GET /v1/inbox/wait?timeout=25
CHANNEL_GONE = "That Channel no longer exists on the relay."
MALFORMED_SEND = (
    "Error: the relay returned an unusable send response, so no Message was sent; "
    "check relay health and retry."
)


class RelayRejected(Exception):
    """The relay refused the request (4xx)."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(detail)


class RelayUnavailable(Exception):
    """The relay could not be reached or answered something unusable."""


class CardConflict(RelayRejected):
    """This Agent name belongs to another Owner (409 on the Card route)."""


def build_client(url, key, agent, transport=None) -> httpx.AsyncClient:
    """Build the shared authenticated relay client."""
    return httpx.AsyncClient(
        base_url=url,
        transport=transport,
        trust_env=False,
        timeout=HTTP_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {key}",
            "X-Agent": agent,
        },
    )


def _client_key(client: httpx.AsyncClient) -> str:
    authorization = client.headers.get("Authorization", "")
    prefix = "Bearer "
    return authorization[len(prefix):] if authorization.startswith(prefix) else ""


def _redacted(client: httpx.AsyncClient, text: str) -> str:
    key = _client_key(client)
    return text.replace(key, "[redacted]") if key else text


def _rejection(client, response) -> RelayRejected:
    """Build the 4xx error from the relay's {"error", "detail"} envelope."""
    code = "http_error"
    detail = f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        if isinstance(payload.get("error"), str) and payload["error"].strip():
            code = payload["error"]
        if isinstance(payload.get("detail"), str) and payload["detail"].strip():
            detail = payload["detail"]
    return RelayRejected(response.status_code, code, _redacted(client, detail))


async def request(
    client: httpx.AsyncClient,
    method,
    path,
    *,
    json=None,
    params=None,
    timeout=None,
) -> httpx.Response:
    """Send one relay request and map transport and HTTP failures."""
    request_kwargs = {"json": json, "params": params}
    if timeout is not None:
        request_kwargs["timeout"] = timeout
    try:
        response = await client.request(method, path, **request_kwargs)
    except httpx.HTTPError as error:
        message = _redacted(client, str(error))
        raise RelayUnavailable(message) from error

    if 400 <= response.status_code < 500:
        raise _rejection(client, response)
    if not 200 <= response.status_code < 300:
        raise RelayUnavailable(f"HTTP {response.status_code}")
    return response


def body(response: httpx.Response) -> dict | list:
    """Decode a relay JSON object or array."""
    try:
        payload = response.json()
    except (TypeError, ValueError) as error:
        raise RelayUnavailable("response body is not JSON") from error
    if not isinstance(payload, (dict, list)):
        raise RelayUnavailable("response body is not a JSON object or array")
    return payload


def _object(response: httpx.Response) -> dict:
    payload = body(response)
    if not isinstance(payload, dict):
        raise RelayUnavailable("response body is not a JSON object")
    return payload


def _array(response: httpx.Response) -> list:
    payload = body(response)
    if not isinstance(payload, list):
        raise RelayUnavailable("response body is not a JSON array")
    return payload


async def publish_card(client, kind, description) -> dict:
    payload = {"kind": kind}
    if description:
        payload["description"] = description
    try:
        response = await request(client, "PUT", "/v1/agents/me", json=payload)
    except RelayRejected as error:
        if error.status == 409:
            raise CardConflict(error.status, error.code, error.detail) from error
        raise
    return _object(response)


async def wait(client, timeout_seconds) -> list[dict]:
    timeout = httpx.Timeout(
        HTTP_TIMEOUT_SECONDS,
        read=timeout_seconds + 10,
    )
    response = await request(
        client,
        "GET",
        "/v1/inbox/wait",
        params={"timeout": timeout_seconds},
        timeout=timeout,
    )
    payload = body(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("deliveries"), list):
        raise RelayUnavailable("malformed deliveries response")
    return payload["deliveries"]


async def ack(client, delivery_ids) -> int:
    response = await request(
        client,
        "POST",
        "/v1/inbox/ack",
        json={"delivery_ids": delivery_ids},
    )
    payload = _object(response)
    count = payload.get("acked")
    # bool is an int in Python; a JSON true must not pass as a count.
    if not isinstance(count, int) or isinstance(count, bool):
        raise RelayUnavailable("malformed ack response")
    return count


async def send_message(
    client,
    *,
    channel_id=None,
    to=None,
    text,
    meta=None,
) -> dict:
    if (channel_id is None) == (to is None):
        raise ValueError("exactly one of channel_id and to is required")
    payload = {"text": text}
    if channel_id is not None:
        payload["channel_id"] = channel_id
    else:
        payload["to"] = to
    if meta is not None:
        payload["meta"] = meta
    response = await request(client, "POST", "/v1/messages", json=payload)
    return _object(response)


async def message_status(client, message_id) -> dict:
    response = await request(client, "GET", f"/v1/messages/{message_id}")
    return _object(response)


async def list_agents(client, q=None) -> list[dict]:
    params = {"q": q} if q is not None else None
    response = await request(client, "GET", "/v1/agents", params=params)
    return _array(response)


async def list_channels(client) -> list[dict]:
    response = await request(client, "GET", "/v1/channels")
    channels = _array(response)
    if not all(
        isinstance(channel, dict)
        and isinstance(channel.get("id"), str)
        and bool(channel["id"])
        for channel in channels
    ):
        raise RelayUnavailable("malformed channels response")
    return channels


def _ambiguous_channel_message(channels: list[dict]) -> str:
    entries = [mirror.label(channel) for channel in channels]
    return (
        f"That name matches {len(channels)} Channels.\n"
        f"Channels: {', '.join(entries)}\n"
        "Type more of the Channel name."
    )


def resolve_channel(
    channels: list[dict], token: str
) -> tuple[dict | None, str | None]:
    """Resolve a Channel name, unique name prefix, or exact topic.

    The caller supplies the already-fetched Channels so this remains a pure
    resolver shared by the Owner command path and the Owner-only tools.
    """
    exact_names = [channel for channel in channels if channel.get("name") == token]
    if len(exact_names) == 1:
        return exact_names[0], None
    if len(exact_names) > 1:
        return None, _ambiguous_channel_message(exact_names)

    prefixes = [
        channel
        for channel in channels
        if isinstance(channel.get("name"), str)
        and isinstance(token, str)
        and channel["name"].casefold().startswith(token.casefold())
    ]
    if len(prefixes) == 1:
        return prefixes[0], None
    if len(prefixes) > 1:
        return None, _ambiguous_channel_message(prefixes)

    exact_topics = [channel for channel in channels if channel.get("topic") == token]
    if len(exact_topics) == 1:
        return exact_topics[0], None
    if len(exact_topics) > 1:
        return None, _ambiguous_channel_message(exact_topics)

    safe_token = security.safe_field(token, fallback="")
    return None, f"No Channel here is named {safe_token}."


async def create_channel(client, topic, invite, text=None) -> dict:
    payload = {"invite": invite}
    if topic:
        payload["topic"] = topic
    if text is not None:
        payload["text"] = text
    response = await request(
        client,
        "POST",
        "/v1/channels",
        json=payload,
    )
    return _object(response)


async def invite(client, channel_id, agent, text=None) -> dict:
    payload = {"agent": agent}
    if text is not None:
        payload["text"] = text
    response = await request(
        client,
        "POST",
        f"/v1/channels/{channel_id}/invite",
        json=payload,
    )
    return _object(response)


async def join(client, channel_id) -> dict:
    response = await request(client, "POST", f"/v1/channels/{channel_id}/join")
    return _object(response)


async def leave(client, channel_id) -> None:
    await request(client, "POST", f"/v1/channels/{channel_id}/leave")
    return None


async def rename(client, channel_id, name) -> dict:
    """Give a Channel a new name. The relay allows only the Creator."""
    return await request(
        client, "POST", f"/v1/channels/{channel_id}/rename", json={"name": name}
    )


async def remove_member(client, channel_id, agent) -> None:
    await request(
        client,
        "DELETE",
        f"/v1/channels/{channel_id}/members/{agent}",
    )
    return None
