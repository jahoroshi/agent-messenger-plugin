"""Owner-visible AMessenger messages."""

import logging


logger = logging.getLogger("amessenger")
HANDLE_LENGTH = 6  # §6.5: Owners type short Channel handles, not full ids.


def handle(channel_id: str) -> str:
    """Return the short handle shown to the Owner for a Channel."""
    return channel_id[:HANDLE_LENGTH]


def label(channel: dict) -> str:
    """Return a Channel's Owner-facing name and short handle."""
    channel_id = channel["id"]
    short_handle = handle(channel_id)
    suffix = f"({short_handle}…)"
    name = channel.get("name")
    return f"{name} {suffix}" if name else suffix


def _sender_details(sender_card: dict | None) -> tuple[str, str, str]:
    card = sender_card if isinstance(sender_card, dict) else {}
    owner = card.get("owner") if isinstance(card.get("owner"), dict) else {}
    name = card.get("name") or "unknown"
    owner_name = owner.get("name") or owner.get("login") or "unknown"
    kind = card.get("kind") or "unknown"
    return name, owner_name, kind


def incoming(sender_card, channel, text, policy) -> str:
    """Format an incoming Message for the Owner Chat."""
    sender_name, owner_name, kind = _sender_details(sender_card)
    header = (
        f"📨 AMessenger · from {sender_name} ({owner_name}, {kind}) · "
        f"channel {label(channel)}"
    )
    rendered = f"{header}\n{text}"
    if policy == "notify":
        rendered += (
            f"\n— notify mode. To let me answer on my own: /amsg interact "
            f"{handle(channel['id'])} 1h   (add \"full\" for all tools)"
        )
    return rendered


def outgoing(channel, text) -> str:
    """Format an outgoing Message for the Owner Chat."""
    return f"📤 AMessenger · to channel {label(channel)}\n{text}"


def grant_ended(channel: dict) -> str:
    """Format the notice for a single Grant ended by the Agent."""
    return f"🔕 AMessenger · Grant for {label(channel)} ended, back to notify."


def cap_reached(channel: dict) -> str:
    """Format the notice for a Channel that reached its reply cap."""
    return f"🔕 AMessenger · Cap reached, channel {label(channel)} back to notify."


def invite(channel, text) -> str:
    """Format an Invite, including its first Message, for the Owner Chat."""
    return (
        f"🔔 AMessenger · {channel['creator']} invites you to channel {label(channel)}. "
        f"First message:\n{text}\n"
        f"— Join: /amsg join {handle(channel['id'])}     Ignore: do nothing"
    )


def notice(channel, text) -> str:
    """Format a relay-written Channel notice for the Owner Chat."""
    return f"🔔 AMessenger · channel {label(channel)}: {text}"


def format_card(card: dict | None) -> str:
    if card is None:
        return "Your Card is published."

    owner = card.get("owner") or {}
    lines = [
        "Your Card is published:",
        f"  Agent: {card.get('name') or 'unknown'} ({card.get('kind') or 'unknown'})",
        "  Owner: "
        f"{owner.get('name') or owner.get('login') or 'unknown'} "
        f"<{owner.get('email') or 'unknown'}>",
    ]
    if card.get("description"):
        lines.append(f"  About: {card['description']}")
    return "\n".join(lines)


async def post(owner_adapter, chat_id: str, text: str) -> bool:
    """Post text in the Owner Chat and report whether Hermes accepted it."""
    try:
        result = await owner_adapter.send(chat_id, text)
    except Exception as error:
        # An Owner Chat adapter failure must not kill the poll loop.
        # A failed post means "do not ack"; the caller handles that.
        return False
    succeeded = bool(result and getattr(result, "success", False))
    return succeeded


def note(platform: str, chat_id: str, text: str) -> bool:
    """Append an Owner-visible text to the Owner Chat transcript."""
    try:
        from gateway import mirror

        result = mirror.mirror_to_session(
            platform,
            chat_id,
            text,
            source_label="amessenger",
            role="assistant",
        )
    except (ImportError, AttributeError) as error:
        logger.warning("[amessenger] Owner Chat transcript unavailable: %s", error)
        return False
    if not result:
        logger.warning("[amessenger] Owner Chat transcript note failed")
    return bool(result)


async def mirror(owner_adapter, platform, chat_id, text) -> bool:
    """Post a Mirror, then append the same text to the Owner transcript."""
    if not await post(owner_adapter, chat_id, text):
        return False
    note(platform, chat_id, text)
    return True
