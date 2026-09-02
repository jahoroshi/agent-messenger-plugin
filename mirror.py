"""Owner-visible AMessenger messages."""

import logging


logger = logging.getLogger("amessenger")


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
        logger.warning("[amessenger] Owner Chat post failed: %s", error)
        return False
    return bool(result and getattr(result, "success", False))


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
    return bool(result)


# Task T5.1 adds the three §6.5 formats and the §6.3 notice lines here.
