"""Owner-facing AMessenger tools."""

import logging

from . import mirror, relay, state
from .adapter import check_requirements, live_adapter


logger = logging.getLogger("amessenger")
NOT_CONNECTED = "Error: AMessenger is not connected."
RELAY_OUTAGE = "Error: the AMessenger relay did not answer. Try again."


def relay_error(error) -> str:
    """The sentence the model reads when the relay refused or did not answer."""
    if isinstance(error, relay.RelayRejected):
        return f"Error: {error.detail}"
    return RELAY_OUTAGE


def _adapter_or_error():
    adapter = live_adapter()
    if adapter is None:
        return None, NOT_CONNECTED
    return adapter, None


def _agent_line(card: dict) -> str:
    owner = card.get("owner") if isinstance(card.get("owner"), dict) else {}
    name = card.get("name") or "unknown"
    kind = card.get("kind") or "unknown"
    owner_name = owner.get("name") or owner.get("login") or "unknown"
    owner_email = owner.get("email") or "unknown"
    line = f"{name} ({kind}) — {owner_name} <{owner_email}>"
    if card.get("description"):
        line += f" — {card['description']}"
    return line


def _channel_line(channel: dict) -> str:
    channel_id = channel.get("id") or "unknown"
    name = channel.get("name") or "unnamed"
    members = channel.get("members") or []
    rendered_members = ", ".join(
        f"{member.get('agent', 'unknown')} ({member.get('state', 'unknown')})"
        for member in members
    )
    return f"{channel_id} — {name}, members: {rendered_members}"


def _pending_recipients(channel: dict, to: str | None) -> list[str]:
    members = channel.get("members") or []
    if to is not None:
        members = [member for member in members if member.get("agent") == to]
    return [
        member.get("agent", "unknown")
        for member in members
        if member.get("state") == "invited"
    ]


def _send_result(result: dict, text: str, to: str | None) -> str:
    channel = result["channel"]
    message_id = result["message"]["id"]
    channel_id = channel["id"]
    pending = _pending_recipients(channel, to)
    if pending:
        recipient = ", ".join(pending)
        return (
            f"Sent to channel {channel_id}. {recipient} has not joined yet, so their "
            "Owner must accept the Invite before it is delivered. Tell your Owner that."
        )
    return f"Sent to channel {channel_id}. Message id {message_id}."


def _manage_description(action: str) -> str:
    return (
        f"{action} Use this only in the Owner's own session; the Owner is the one "
        "who decides."
    )


async def amessenger_agents(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    query = args.get("query")
    try:
        cards = await relay.list_agents(adapter.client(), query)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    if not cards:
        return (
            f"No Agents match '{query}'."
            if query is not None
            else "The Directory is empty."
        )
    return "\n".join(_agent_line(card) for card in cards)


async def amessenger_channels(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    try:
        channels = await relay.list_channels(adapter.client())
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    if not channels:
        return "No Channels yet."
    return "\n".join(_channel_line(channel) for channel in channels)


async def amessenger_send(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    to = args.get("to")
    channel_id = args.get("channel_id")
    if (to is None) == (channel_id is None):
        return "Error: provide exactly one of 'to' and 'channel_id'."
    text = args.get("text", "")
    try:
        result = await relay.send_message(
            adapter.client(), to=to, channel_id=channel_id, text=text
        )
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)

    adapter.remember_channel(result["channel"])
    # The reply cap does not apply: it counts autonomous replies inside a
    # Channel session, not a Message the Owner asked the Agent to send.
    posted = await mirror.mirror(
        adapter.owner_adapter,
        adapter._owner_platform,
        adapter._owner_chat_id,
        mirror.outgoing(result["channel"], text),
    )
    if not posted:
        logger.warning(
            "[amessenger] outgoing Mirror failed for tool send; Message was accepted"
        )
    return _send_result(result, text, to)


def _status_line(delivery: dict) -> str:
    return (
        f"Pending: {delivery.get('agent', 'unknown')} — "
        f"attempts {delivery.get('attempts', 0)}"
    )


async def amessenger_status(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    message_id = args.get("message_id")
    try:
        status_result = await relay.message_status(adapter.client(), message_id)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        if isinstance(error, relay.RelayRejected) and error.status == 404:
            return "That Message is gone: every recipient acked it, or it expired."
        return relay_error(error)

    deliveries = status_result.get("deliveries") or []
    delivered = sum(delivery.get("state") == "acked" for delivery in deliveries)
    lines = [f"{delivered} of {len(deliveries)} delivered"]
    lines.extend(
        _status_line(delivery)
        for delivery in deliveries
        if delivery.get("state") != "acked"
    )
    return "\n".join(lines)


async def amessenger_create_channel(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    name = args.get("name")
    invite = args.get("invite") or []
    try:
        channel = await relay.create_channel(adapter.client(), name, invite)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    adapter.remember_channel(channel)
    invited = [
        member.get("agent", "unknown")
        for member in channel.get("members", [])
        if member.get("state") == "invited"
    ]
    invited_names = set(invite)
    joined = [
        member.get("agent", "unknown")
        for member in channel.get("members", [])
        if member.get("state") == "member"
        and member.get("agent") in invited_names
    ]
    return (
        f"Created channel {channel['id']}.\n"
        f"Invited: {', '.join(invited) if invited else 'none'}.\n"
        f"Joined immediately: {', '.join(joined) if joined else 'none'}."
    )


async def amessenger_invite(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    channel_id = args.get("channel_id")
    agent = args.get("agent")
    try:
        channel = await relay.invite(adapter.client(), channel_id, agent)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    adapter.remember_channel(channel)
    return f"Invited {agent} to channel {channel_id}."


async def amessenger_leave(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    channel_id = args.get("channel_id")
    try:
        await relay.leave(adapter.client(), channel_id)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    adapter.set_state(state.revoke(adapter.state(), channel_id))
    return f"Left channel {channel_id}."


async def amessenger_remove_member(args: dict, **_) -> str:
    adapter, error = _adapter_or_error()
    if error:
        return error
    channel_id = args.get("channel_id")
    agent = args.get("agent")
    try:
        await relay.remove_member(adapter.client(), channel_id, agent)
    except (relay.RelayRejected, relay.RelayUnavailable) as error:
        return relay_error(error)
    return f"Removed {agent} from channel {channel_id}."


_SCHEMAS = {
    "amessenger": {
        "amessenger_agents": {
            "name": "amessenger_agents",
            "description": (
                "Find an Agent in the Directory that belongs to a person. When a "
                "person has more than one Agent, ask the Owner which Agent to use; "
                "do not choose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional name, Owner login, or Owner name to search for.",
                    }
                },
            },
        },
        "amessenger_channels": {
            "name": "amessenger_channels",
            "description": (
                "List the Agent's own Channels. The result shows each full Channel "
                "id; other AMessenger tools take a full id, while the short handle "
                "in the Owner Chat is only for the Owner to type."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        "amessenger_send": {
            "name": "amessenger_send",
            "description": (
                "Send a Message to exactly one Agent or Channel. Sending to an Agent "
                "creates the Channel, Invite, and queued Message when no matching "
                "Channel exists, so the Owner never has to create one first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Agent name to address."},
                    "channel_id": {"type": "string", "description": "Full Channel id to address."},
                    "text": {"type": "string", "description": "Message text."},
                },
                "required": ["text"],
            },
        },
        "amessenger_status": {
            "name": "amessenger_status",
            "description": "Check delivery status for a Message while the relay still has it.",
            "parameters": {
                "type": "object",
                "properties": {"message_id": {"type": "string", "description": "Message id."}},
                "required": ["message_id"],
            },
        },
    },
    "amessenger_manage": {
        "amessenger_create_channel": {
            "name": "amessenger_create_channel",
            "description": _manage_description(
                "Create a Channel with the supplied name and Invite entries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Human label for the Channel."},
                    "invite": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent names to Invite.",
                    },
                },
                "required": ["name", "invite"],
            },
        },
        "amessenger_invite": {
            "name": "amessenger_invite",
            "description": _manage_description(
                "Invite an Agent to a Channel you created."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Full Channel id."},
                    "agent": {"type": "string", "description": "Agent name to Invite."},
                },
                "required": ["channel_id", "agent"],
            },
        },
        "amessenger_leave": {
            "name": "amessenger_leave",
            "description": _manage_description(
                "Leave a Channel. Leaving your created Channel closes it."
            ),
            "parameters": {
                "type": "object",
                "properties": {"channel_id": {"type": "string", "description": "Full Channel id."}},
                "required": ["channel_id"],
            },
        },
        "amessenger_remove_member": {
            "name": "amessenger_remove_member",
            "description": _manage_description(
                "Remove an Agent from a Channel you created."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "channel_id": {"type": "string", "description": "Full Channel id."},
                    "agent": {"type": "string", "description": "Member Agent name to remove."},
                },
                "required": ["channel_id", "agent"],
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
