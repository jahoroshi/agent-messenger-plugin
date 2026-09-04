"""Owner-facing AMessenger tools."""

from contextlib import asynccontextmanager
import logging

from . import adapter as adapter_module
from . import mirror, relay, security, state
from .adapter import active_adapter, check_requirements


logger = logging.getLogger("amessenger")
RELAY_OUTAGE = (
    "Error: the AMessenger relay at AMESSENGER_URL did not answer; check "
    "AMESSENGER_URL and relay health, then retry."
)
SEND_IDEMPOTENCY_SECONDS = state.SEND_IDEMPOTENCY_SECONDS
_MALFORMED_SEND = relay.MALFORMED_SEND


def relay_error(error) -> str:
    """The sentence the model reads when the relay refused or did not answer."""
    if isinstance(error, relay.RelayRejected):
        detail = security.safe_field(error.detail, fallback=RELAY_OUTAGE)
        return (
            "Error: the relay rejected the request: "
            f"{detail}; fix the named Channel, Agent, or permission, then retry."
        )
    return RELAY_OUTAGE


def _configuration_error() -> str | None:
    if check_requirements():
        return None
    missing = adapter_module.missing_requirements()
    if not missing:
        # This also keeps a monkeypatched check_requirements failure actionable.
        missing = list(adapter_module.REQUIRED_ENV)
    return f"Error: AMessenger is not configured: set {', '.join(missing)}."


@asynccontextmanager
async def relay_client(adapter):
    """A client for the loop this tool is running on.

    Hermes dispatches tool handlers on a worker event loop, not the gateway loop,
    and an httpx.AsyncClient cannot cross that boundary — its pool primitives are
    bound to the loop that built it. Building one per call is cheap next to the
    request itself.
    """
    if adapter is None:
        client = adapter_module.build_relay_client()
    else:
        client = adapter_module.build_relay_client(transport=adapter._transport)
    try:
        yield client
    finally:
        await client.aclose()


def _agent_line(card: dict) -> str:
    owner = card.get("owner") if isinstance(card.get("owner"), dict) else {}
    name = security.safe_field(card.get("name"))
    kind = security.safe_field(card.get("kind"))
    owner_name = security.safe_field(owner.get("name") or owner.get("login"))
    line = f"{name} ({kind}) — {owner_name}"
    if owner.get("email"):
        line += f" <{security.safe_field(owner.get('email'))}>"
    description = security.safe_field(card.get("description"), fallback="")
    if description:
        line += f" — {description}"
    return line


def _channel_line(channel: dict) -> str:
    members = channel.get("members") or []
    rendered_members = ", ".join(
        f"{security.safe_field(member.get('agent'))} "
        f"({security.safe_field(member.get('state'))})"
        for member in members
        if isinstance(member, dict)
    )
    return f"{mirror.label(channel)}, members: {rendered_members}"


def _recipient_members(result: dict, to: str | None) -> list[dict]:
    channel = result.get("channel") or {}
    message = result.get("message") or {}
    sender = message.get("sender")
    if not isinstance(sender, str) or not sender:
        sender = adapter_module.read_settings().get("agent")
    members = channel.get("members") or []
    recipients = []
    for member in members:
        if not isinstance(member, dict):
            continue
        if to is not None and member.get("agent") != to:
            continue
        if to is None and member.get("agent") == sender:
            continue
        if member.get("state") in {"member", "invited"}:
            recipients.append(member)
    return recipients


async def _resolve_channel(adapter, token) -> tuple[dict | None, str | None]:
    """Resolve a tool Channel name, prefix, or topic against this Agent's Channels."""
    try:
        async with relay_client(adapter) as client:
            channels = await relay.list_channels(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return None, relay_error(error)
    pending_invites = _read_shared_state(adapter).get("pending_invites", {})
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
    document = _read_shared_state(adapter)
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


def _read_shared_state(adapter) -> dict:
    if adapter is None:
        return adapter_module.read_state_file()
    return state.load(adapter.state_path())


def _update_shared_state(adapter, change) -> dict:
    if adapter is None:
        return adapter_module.update_state_file(change)
    return adapter.update_state(change)


def _drop_forgotten_channel(adapter, channel_id: str) -> None:
    _update_shared_state(adapter, lambda document: state.drop_channel(document, channel_id))
    if adapter is not None:
        adapter._channels.pop(channel_id, None)


def _already_sent_answer(channel: dict, record: dict) -> str:
    return (
        f"Already sent that exact Message to channel {mirror.label(channel)} a moment ago. "
        f"Message id {record['message_id']}; nothing to do."
    )


def _usable_send_result(result) -> bool:
    return (
        isinstance(result, dict)
        and isinstance(result.get("channel"), dict)
        and isinstance(result["channel"].get("id"), str)
        and bool(result["channel"]["id"])
        and isinstance(result.get("message"), dict)
        and isinstance(result["message"].get("id"), str)
        and bool(result["message"]["id"])
    )


def _receive_warning(problem: str | None, *, no_owner_copy: bool = False) -> str:
    """The sentence that stops a send from looking answered when it cannot be."""
    if not problem:
        return ""
    lead = (
        "Your Owner Chat received no copy, and replies cannot arrive here"
        if no_owner_copy
        else "WARNING: replies cannot arrive here"
    )
    return (
        f" {lead}: {problem}. Tell your Owner that; do not try to fix it yourself."
    )


def _send_result(
    result: dict,
    text: str,
    to: str | None,
    *,
    owner_copy_queued: bool = False,
    receive_problem: str | None = None,
) -> str:
    channel = result["channel"]
    message_id = security.safe_field(result["message"].get("id"))
    answer = f"Sent to channel {mirror.label(channel)}. Message id {message_id}."
    answer += " When you report this, name the Agent, not its Owner."
    recipients = _recipient_members(result, to)
    delivered = [
        security.safe_field(member.get("agent"))
        for member in recipients
        if member.get("state") == "member"
    ]
    pending = [
        member.get("agent", "unknown")
        for member in recipients
        if member.get("state") == "invited"
    ]
    # Name the Agent, never the person: this is the Agents' messenger, and a
    # result that reads "delivered to andrei-work" invites the model to report
    # it as "sent to Andrei".
    outcomes = [
        f"delivered to agent {agent}'s inbox" for agent in delivered
    ] + [
        f"waiting for the Owner of agent {security.safe_field(agent)} to join"
        for agent in pending
    ]
    if outcomes:
        answer += " " + "; ".join(outcomes) + "."
    if pending:
        recipient = ", ".join(security.safe_field(agent) for agent in pending)
        answer += (
            f" Agent {recipient} has not joined yet, so its "
            "Owner must accept the Invite before it is delivered. Tell your Owner that."
        )
    if receive_problem:
        # In a gateway with no receive loop the queued copy is never drained,
        # so it must not be reported as on its way.
        answer += _receive_warning(receive_problem, no_owner_copy=owner_copy_queued)
    elif owner_copy_queued:
        answer += " Your Owner Chat copy is queued for the gateway to post."
    return answer


def _manage_description(action: str) -> str:
    return (
        f"{action} Use this only in the Owner's own session; the Owner is the one "
        "who decides."
    )


async def amessenger_agents(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    query = args.get("query")
    try:
        async with relay_client(adapter) as client:
            cards = await relay.list_agents(client, query)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    if not cards:
        return (
            f"No Agents match '{query}'; change the query or publish the missing "
            "Agent's Card."
            if query is not None
            else "The Directory is empty; publish an Agent Card before searching."
        )
    return "\n".join(_agent_line(card) for card in cards)


async def amessenger_channels(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    try:
        async with relay_client(adapter) as client:
            channels = await relay.list_channels(client)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    if not channels:
        return "No Channels yet; create one with amessenger_create_channel or send to an Agent."
    return "\n".join(_channel_line(channel) for channel in channels)


async def amessenger_send(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    to = args.get("to")
    channel_name = args.get("channel")
    if (to is None) == (channel_name is None):
        return "Error: provide exactly one of 'to' and 'channel'."
    channel_id = None
    text = args.get("text", "")
    redacted = security.redact_outbound(text)
    if channel_name is not None:
        channel, resolution_error = await _resolve_channel(adapter, channel_name)
        if resolution_error is not None:
            return resolution_error
        channel_id = channel["id"]
        key = state.send_idempotency_key(channel_id, text)
        previous = state.recent_send(
            _read_shared_state(adapter), key, state.now()
        )
        if previous is not None:
            return _already_sent_answer(channel, previous)

    # Asked once, before the send, so both the result and the queued Owner
    # copy describe the same state.
    unanswerable = adapter_module.receive_problem()
    count_reply = False
    reply_moment = None
    if adapter is not None and channel_id is not None:
        # A tool send into an active Grant uses the same bounded reply window as
        # an autonomous Channel reply. No Grant means there is no window to count.
        record = state.channel(adapter.state(), channel_id, state.now())
        count_reply = record["grant"] is not None
    elif channel_id is not None:
        reply_moment = state.now()
        record = state.channel(
            adapter_module.read_state_file(), channel_id, reply_moment
        )
        count_reply = record["grant"] is not None

    if adapter is not None:
        delivery = await adapter.deliver_to_channel(
            channel_id,
            text,
            count_reply=count_reply,
            to=to,
        )
        if not delivery.success:
            error = delivery.raw_response
            if (
                channel_id is not None
                and isinstance(error, relay.RelayRejected)
                and error.status == 404
            ):
                _drop_forgotten_channel(adapter, channel_id)
                return relay.CHANNEL_GONE
            if isinstance(error, (relay.RelayRejected, relay.RelayUnavailable)):
                return relay_error(error)
            if delivery.error == relay.MALFORMED_SEND:
                return relay.MALFORMED_SEND
            return RELAY_OUTAGE
        result = delivery.raw_response
        owner_copy_queued = False
    else:
        send_arguments = {"text": redacted}
        if to is None:
            send_arguments["channel_id"] = channel_id
        else:
            send_arguments["to"] = to
        try:
            async with relay_client(None) as client:
                result = await relay.send_message(client, **send_arguments)
        except (relay.RelayRejected, relay.RelayUnavailable) as error:
            if (
                channel_id is not None
                and isinstance(error, relay.RelayRejected)
                and error.status == 404
            ):
                _drop_forgotten_channel(adapter, channel_id)
                return relay.CHANNEL_GONE
            return relay_error(error)
        if not _usable_send_result(result):
            return _MALFORMED_SEND
        owner_line = mirror.outgoing(result["channel"], redacted)
        adapter_module.update_state_file(
            lambda document: state.queue_mirror(document, owner_line)
        )
        if unanswerable:
            # If a gateway ever drains this queue, the Owner also learns why
            # the reply to this Message never came. Queued once while it is
            # pending, so a broken gateway does not fill the queue with it.
            notice = mirror.receive_problem_notice(unanswerable)
            adapter_module.update_state_file(
                lambda document: (
                    document
                    if any(
                        state.mirror_entry_text(entry) == notice
                        for entry in document.get("pending_mirrors", [])
                    )
                    else state.queue_mirror(document, notice)
                )
            )
        if count_reply:
            adapter_module.update_state_file(
                lambda document: state.note_reply(
                    document, channel_id, reply_moment
                )
            )
        owner_copy_queued = True

    if not _usable_send_result(result):
        return _MALFORMED_SEND
    result_channel = result["channel"]
    result_message = result["message"]
    if isinstance(result_channel, dict) and isinstance(result_message, dict):
        sent_channel_id = result_channel["id"]
        key = state.send_idempotency_key(sent_channel_id, text)
        _update_shared_state(
            adapter,
            lambda document: state.remember_send(
                document, key, result_message["id"], state.now()
            ),
        )
    return _send_result(
        result,
        text,
        to,
        owner_copy_queued=owner_copy_queued,
        receive_problem=unanswerable,
    )


def _status_line(delivery: dict) -> str:
    agent = security.safe_field(delivery.get("agent"))
    attempts = security.safe_field(str(delivery.get("attempts", 0)))
    return f"{agent} ({attempts} attempts)"


async def amessenger_status(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    channel_name = args.get("channel")
    if channel_name is not None:
        channel, resolution_error = await _resolve_channel(adapter, channel_name)
        if resolution_error is not None:
            return resolution_error
        channel_id = channel["id"]
    message_id = args.get("message_id")
    try:
        async with relay_client(adapter) as client:
            status_result = await relay.message_status(client, message_id)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        if isinstance(error, relay.RelayRejected) and error.status == 404:
            return (
                "That Message is gone because every recipient acked it or it expired; "
                "nothing to do."
            )
        return relay_error(error)

    deliveries = status_result.get("deliveries") or []
    if not deliveries:
        return (
            "The Message has been delivered to everyone; no recipients are still "
            "waiting."
        )
    waiting = ", ".join(_status_line(delivery) for delivery in deliveries)
    return (
        f"Still waiting: {waiting}. Delivered recipients no longer appear here; "
        "when every recipient has acked, the Message is deleted and this reports "
        "that it is gone."
    )


async def amessenger_create_channel(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    topic = args.get("topic")
    # Accept the pre-name-generation spelling in direct callers while keeping
    # the registered tool schema truthful: the relay now calls it a topic.
    if topic is None:
        topic = args.get("name")
    invite = args.get("invite") or []
    text = args.get("text")
    try:
        async with relay_client(adapter) as client:
            channel = await relay.create_channel(client, topic, invite, text)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    if adapter is not None:
        adapter.remember_channel(channel)
    invited = [
        security.safe_field(member.get("agent"))
        for member in channel.get("members", [])
        if isinstance(member, dict) and member.get("state") == "invited"
    ]
    invited_names = set(invite)
    joined = [
        security.safe_field(member.get("agent"))
        for member in channel.get("members", [])
        if isinstance(member, dict) and member.get("state") == "member"
        and member.get("agent") in invited_names
    ]
    return (
        f"Created channel {mirror.label(channel)}.\n"
        f"Invited: {', '.join(invited) if invited else 'none'}.\n"
        f"Joined immediately: {', '.join(joined) if joined else 'none'}."
        + _receive_warning(adapter_module.receive_problem())
    )


async def amessenger_invite(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    channel_name = args.get("channel")
    agent = args.get("agent")
    channel, resolution_error = await _resolve_channel(adapter, channel_name)
    if resolution_error is not None:
        return resolution_error
    channel_id = channel["id"]
    text = args.get("text")
    try:
        async with relay_client(adapter) as client:
            channel = await relay.invite(client, channel_id, agent, text)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        if isinstance(error, relay.RelayRejected) and error.status == 404:
            _drop_forgotten_channel(adapter, channel_id)
            return relay.CHANNEL_GONE
        return relay_error(error)
    if adapter is not None:
        adapter.remember_channel(channel)
    target = next(
        (
            member
            for member in channel.get("members", [])
            if isinstance(member, dict) and member.get("agent") == agent
        ),
        None,
    )
    answer = f"Invited {agent} to channel {mirror.label(channel)}."
    if isinstance(target, dict) and target.get("state") == "member":
        answer += f" delivered to {security.safe_field(agent)}'s inbox."
    else:
        answer += (
            f" waiting for the Owner of agent {security.safe_field(agent)} to join. "
            f"Agent {security.safe_field(agent)} has not joined yet, so its Owner must "
            "accept the Invite before it is delivered. Tell your Owner that."
        )
    return answer + _receive_warning(adapter_module.receive_problem())


async def amessenger_leave(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    channel_name = args.get("channel")
    channel, resolution_error = await _resolve_channel(adapter, channel_name)
    if resolution_error is not None:
        return resolution_error
    channel_id = channel["id"]
    try:
        async with relay_client(adapter) as client:
            await relay.leave(client, channel_id)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        if isinstance(error, relay.RelayRejected) and error.status == 404:
            _drop_forgotten_channel(adapter, channel_id)
            return relay.CHANNEL_GONE
        return relay_error(error)
    if adapter is not None:
        adapter.update_state(lambda document: state.revoke(document, channel_id))
    else:
        adapter_module.update_state_file(
            lambda document: state.revoke(document, channel_id)
        )
    return f"Left channel {mirror.label(channel)}."


async def amessenger_remove_member(args: dict, **_) -> str:
    adapter = active_adapter()
    error = _configuration_error()
    if error:
        return error
    channel_name = args.get("channel")
    agent = args.get("agent")
    channel, resolution_error = await _resolve_channel(adapter, channel_name)
    if resolution_error is not None:
        return resolution_error
    channel_id = channel["id"]
    try:
        async with relay_client(adapter) as client:
            await relay.remove_member(client, channel_id, agent)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        if isinstance(error, relay.RelayRejected) and error.status == 404:
            _drop_forgotten_channel(adapter, channel_id)
            return relay.CHANNEL_GONE
        return relay_error(error)
    return f"Removed {agent} from channel {mirror.label(channel)}."


_SCHEMAS = {
    "amessenger": {
        "amessenger_agents": {
            "name": "amessenger_agents",
            "description": (
                "List every Agent in the Directory; an optional query narrows it by name or "
                "Owner. When a person has more than one Agent, ask the Owner which Agent "
                "to use; do not choose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Optional query to filter by Agent name or Owner; omit it "
                            "to list every Agent in the Directory."
                        ),
                    }
                },
            },
        },
        "amessenger_channels": {
            "name": "amessenger_channels",
            "description": (
                "List the Agent's own Channels by their human-facing names "
                "and optional topics."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        "amessenger_send": {
            "name": "amessenger_send",
            "description": (
                "Send a Message to exactly one Agent or Channel. Sending to an Agent "
                "creates the Channel, Invite, and queued Message when no matching "
                "Channel exists, so the Owner never has to create one first. The "
                "channel argument is a Channel name, unique case-insensitive "
                "name prefix, or exact topic; relay ids are internal and rejected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Agent name to address."},
                    "channel": {
                        "type": "string",
                        "description": (
                            "Channel name, unique case-insensitive name prefix, or "
                            "exact topic to address; do not use the internal id."
                        ),
                    },
                    "text": {"type": "string", "description": "Message text."},
                },
                "required": ["text"],
            },
        },
        "amessenger_status": {
            "name": "amessenger_status",
            "description": (
                "Check which recipients are still waiting for a Message while the "
                "relay still has it. Delivered recipients disappear from the result; "
                "when no rows remain, the Message was delivered to everyone, and a "
                "404 means it is gone. If supplied, channel is a Channel name, "
                "unique case-insensitive name prefix, or exact topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "description": "Message id."},
                    "channel": {
                        "type": "string",
                        "description": (
                            "Optional Channel name, unique case-insensitive name "
                            "prefix, or exact topic."
                        ),
                    },
                },
                "required": ["message_id"],
            },
        },
    },
    "amessenger_manage": {
        "amessenger_create_channel": {
            "name": "amessenger_create_channel",
            "description": _manage_description(
                "Create a Channel with an optional topic and Invite entries. Pass "
                "the first Message as text: it is what the invited Owner reads "
                "when deciding whether to accept."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Optional human topic for the generated Channel name.",
                    },
                    "invite": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent names to Invite.",
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "The first Message/request shown to invited Owners "
                            "before they accept."
                        ),
                    },
                },
                "required": ["invite"],
            },
        },
        "amessenger_invite": {
            "name": "amessenger_invite",
            "description": _manage_description(
                "Invite an Agent to a Channel you created. The channel argument "
                "accepts a Channel name, unique case-insensitive name prefix, or "
                "exact topic. Relay ids are internal and rejected. "
                "An Invite into an existing Channel arrives with nothing in it, "
                "so the invited Owner has only the Channel name to judge; include "
                "a short note in text so they can decide."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": (
                            "Channel name, unique case-insensitive name prefix, or "
                            "exact topic; do not use the internal id."
                        ),
                    },
                    "agent": {"type": "string", "description": "Agent name to Invite."},
                    "text": {
                        "type": "string",
                        "description": (
                            "Optional short note explaining what the invite is for; "
                            "the invited Owner reads it before accepting."
                        ),
                    },
                },
                "required": ["channel", "agent"],
            },
        },
        "amessenger_leave": {
            "name": "amessenger_leave",
            "description": _manage_description(
                "Leave a Channel. Leaving your created Channel closes it. The "
                "channel argument accepts a Channel name, unique case-insensitive "
                "name prefix, or exact topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": (
                            "Channel name, unique case-insensitive name prefix, or "
                            "exact topic."
                        ),
                    }
                },
                "required": ["channel"],
            },
        },
        "amessenger_remove_member": {
            "name": "amessenger_remove_member",
            "description": _manage_description(
                "Remove an Agent from a Channel you created. The channel argument "
                "accepts a Channel name, unique case-insensitive name prefix, or "
                "exact topic."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": (
                            "Channel name, unique case-insensitive name prefix, or "
                            "exact topic."
                        ),
                    },
                    "agent": {"type": "string", "description": "Member Agent name to remove."},
                },
                "required": ["channel", "agent"],
            },
        },
    },
}


_HANDLERS = {
    "amessenger_agents": amessenger_agents,
    "amessenger_channels": amessenger_channels,
    "amessenger_send": amessenger_send,
    "amessenger_status": amessenger_status,
    "amessenger_create_channel": amessenger_create_channel,
    "amessenger_invite": amessenger_invite,
    "amessenger_leave": amessenger_leave,
    "amessenger_remove_member": amessenger_remove_member,
}


def register_tools(ctx) -> None:
    for toolset, schemas in _SCHEMAS.items():
        for name, schema in schemas.items():
            ctx.register_tool(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=_HANDLERS[name],
                check_fn=check_requirements,
                is_async=True,
                description=schema["description"],
                emoji="📨",
            )
