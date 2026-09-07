"""Posting into a Telegram Owner Chat without changing a Message.

Hermes sends every ordinary line to Telegram through a Markdown converter
(``plugins/platforms/telegram/adapter.py``: ``format_message``), which reads a
peer's ``*`` and ``_`` as markup and shows the Owner text the peer never wrote.
The Owner must read the Message exactly as it was sent, so AMessenger posts to
Telegram itself, as plain text, through the client Hermes already holds.

Only this module knows anything about Telegram.  Every other Owner Chat keeps
Hermes's own send path.
"""

import asyncio
import logging
import re


logger = logging.getLogger("amessenger")

PLATFORM = "telegram"
# Telegram measures a message in UTF-16 code units and refuses a longer one.
MAX_CHARS = 4096
# A whole line that is one AMessenger command, and nothing a peer wrote: a
# quoted body line starts with the quote, so this never matches one.
_COMMAND_LINE = re.compile(r"(?m)^(/[a-z][a-z0-9_-]*)(?=$|[ \t])")
# Room kept on a continued post for its header, counted with a two-digit
# numbering and widened by split_for_posts() when there are more posts.
_COUNT_DIGITS = 2


def utf16_length(text) -> int:
    """Return the length Telegram counts, which is not the Python length."""
    if not isinstance(text, str):
        return 0
    return len(text.encode("utf-16-le")) // 2


def is_owner_chat(platform) -> bool:
    """Return whether this Owner Chat platform is Telegram."""
    return str(platform or "").strip().casefold() == PLATFORM


def chat_id_is_reachable(chat_id) -> bool:
    """Return whether a Telegram chat id is one an incoming event can match.

    Telegram's Bot API also accepts ``@name`` for a public channel, and a post
    addressed that way arrives.  A command typed back does not: Hermes reports
    the numeric id, which never equals the name, so every Owner command would
    be refused and only the operator could see why.
    """
    value = str(chat_id or "").strip()
    if value.startswith("-"):
        value = value[1:]
    return bool(value) and value.isascii() and value.isdigit()


def normalized_chat_id(chat_id):
    """Return the chat id in the form the Bot API takes."""
    value = str(chat_id).strip()
    try:
        return int(value)
    except ValueError:
        return value


def client(owner_adapter):
    """Return Hermes's live Telegram client, or None while it has none.

    Hermes keeps the python-telegram-bot client in a private attribute in both
    0.20.5 and 0.21.0.  ``bot`` is read first so a later Hermes that publishes
    one is used instead of the private name.
    """
    for name in ("bot", "_bot"):
        candidate = getattr(owner_adapter, name, None)
        if candidate is not None and callable(getattr(candidate, "send_message", None)):
            return candidate
    return None


def _link_preview(owner_adapter) -> dict:
    """Turn the link preview off, however this Telegram client says it.

    A URL in a Message would otherwise open a card under the Mirror, showing
    the title and picture of a page the peer chose, outside the quote and
    looking like part of what AMessenger wrote. Hermes's own answer is used
    when the operator has already turned previews off; otherwise the same two
    forms are built here, newest first.
    """
    reader = getattr(owner_adapter, "_link_preview_kwargs", None)
    if callable(reader):
        try:
            options = reader()
        except Exception as error:
            # A preview is cosmetic; a Mirror is not. Never fail a post over it.
            logger.debug(
                "[amessenger] Telegram link-preview options unavailable: %s", error
            )
            options = None
        if isinstance(options, dict) and options:
            return dict(options)
    try:
        from telegram import LinkPreviewOptions
    except Exception:
        return {"disable_web_page_preview": True}
    return {"link_preview_options": LinkPreviewOptions(is_disabled=True)}


def _bot_username(owner_adapter) -> str:
    """Return the name Telegram knows this Agent's bot by, if it says."""
    reader = getattr(owner_adapter, "_current_bot_username", None)
    if not callable(reader):
        return ""
    try:
        return str(reader() or "").strip().lstrip("@")
    except Exception as error:
        logger.debug("[amessenger] Telegram bot name unavailable: %s", error)
        return ""


def address_commands(text: str, username: str) -> str:
    """Address every AMessenger command in *text* to this Agent's bot.

    Telegram gives a group's members several bots, and a bare ``/amsg`` there
    reaches whichever one the group is configured to answer. ``/amsg@name`` is
    the form Telegram routes to one bot, and Hermes drops the name again before
    the command runs.

    Only a line that begins with the command is rewritten. Everything a peer
    wrote is quoted first, so a command inside a Message is never touched.
    """
    if not username:
        return text
    return _COMMAND_LINE.sub(rf"\1@{username}", text)


def is_group(chat_id) -> bool:
    """Return whether this chat is a group, which Telegram numbers below zero."""
    return str(chat_id or "").strip().startswith("-")


def _units(text: str, separators) -> list[str]:
    """Cut text into lines, each carrying the line break that starts it.

    Joining the result gives back the text unchanged, so a post boundary placed
    between two units adds and removes nothing. The breaks are the ones the
    body was quoted by, so a unit is one line as the Owner reads it: a post
    never ends inside a quote, and a cut is never given a second one.
    """
    pieces = separators.split(text)
    units = [pieces[0]]
    for index in range(1, len(pieces), 2):
        line = pieces[index + 1] if index + 1 < len(pieces) else ""
        units.append(pieces[index] + line)
    return units


def _line_of(unit: str, separators) -> str:
    """Return a unit without the line break it carries."""
    found = separators.match(unit)
    return unit[found.end() :] if found else unit


def _cut(text: str, budget: int) -> tuple[str, str]:
    """Split one line at the last position that fits, never inside a character."""
    used = 0
    for position, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if used + width > budget:
            # A budget too small for one character would make no progress.
            position = max(position, 1)
            return text[:position], text[position:]
        used += width
    return text, ""


def _header_room(continuation_header: str, digits: int) -> int:
    return utf16_length(continuation_header) + len(" (/)") + 2 * digits + 1


def _fragments(text: str, limit: int, reserve: int, quote: str, separators) -> list[str]:
    """Pack the text into post-sized fragments, adding only the body quote."""
    fragments: list[str] = []
    current = ""
    pending = _units(text, separators)
    index = 0
    while index < len(pending):
        # A continued post pays for its header. One character is the floor:
        # below it a cut would take back as much as it gave and never finish.
        whole = limit if not fragments else max(limit - reserve, 1)
        unit = pending[index]
        if not unit:
            index += 1
            continue
        if utf16_length(unit) <= whole - utf16_length(current):
            current += unit
            index += 1
            continue
        if current:
            fragments.append(current)
            current = ""
            continue
        # A single line too long for a post of its own: cut the line, and give
        # the rest the quote its line already carries.  Without it a peer could
        # place a forged AMessenger header at the cut and have the next post
        # open with a line that looks like one AMessenger wrote.  A post with
        # no room for more than the quote would take it back and never finish,
        # so there the cut goes on unquoted.
        line = _line_of(unit, separators)
        head, tail = _cut(unit, whole)
        fragments.append(head)
        repeats = line.startswith(quote) and whole > utf16_length(quote)
        pending[index] = (quote if repeats else "") + tail
    if current:
        fragments.append(current)
    return fragments


def split_for_posts(
    text: str,
    continuation_header: str,
    quote: str,
    separators,
    limit: int = MAX_CHARS,
) -> list[str]:
    """Split one Owner-facing text into the posts Telegram will accept.

    Nothing is trimmed, normalized or rewritten.  A continued post opens with
    ``continuation_header``, which says the Owner is still reading the same
    Mirror, and every continued body line keeps its quote.
    """
    if utf16_length(text) <= limit:
        return [text]

    digits = _COUNT_DIGITS
    while True:
        reserve = _header_room(continuation_header, digits)
        fragments = _fragments(text, limit, reserve, quote, separators)
        needed = len(str(len(fragments)))
        if needed <= digits:
            break
        digits = needed

    total = len(fragments)
    posts = [fragments[0]]
    for number, fragment in enumerate(fragments[1:], start=2):
        posts.append(f"{continuation_header} ({number}/{total})\n{fragment}")
    return posts


# One Mirror can need several posts, and a second Mirror posting between them
# would leave the Owner reading two Messages as one. Every post of a text goes
# out under this lock, in one gateway process with one Owner Chat.
_POSTING = asyncio.Lock()


def thread_kwargs(thread_id) -> dict:
    """Address a post to the forum topic it answers, when it answers one."""
    value = str(thread_id or "").strip()
    if not value.isascii() or not value.isdigit():
        return {}
    return {"message_thread_id": int(value)}


async def deliver(
    owner_adapter,
    chat_id,
    text: str,
    *,
    continuation_header: str,
    quote: str,
    separators,
    thread_id=None,
) -> tuple[int, int]:
    """Post an Owner-facing text into Telegram, exactly as it was composed.

    Returns how many posts arrived out of how many the text needed, because a
    caller that would write the text again has to know that part of it is
    already in the Owner Chat.

    A failure is reported rather than retried through Hermes's own send path:
    that path would deliver a different Message, and a Message the Owner cannot
    trust is worse than one that arrives late (ADR-0007).
    """
    bot = client(owner_adapter)
    if bot is None:
        logger.warning(
            "[amessenger] the Telegram Owner Chat has no running client; nothing was posted"
        )
        return 0, 1

    target = normalized_chat_id(chat_id)
    options = {**_link_preview(owner_adapter), **thread_kwargs(thread_id)}
    if is_group(chat_id):
        text = address_commands(text, _bot_username(owner_adapter))
    pieces = split_for_posts(text, continuation_header, quote, separators)
    delivered = 0
    async with _POSTING:
        for piece in pieces:
            try:
                await bot.send_message(
                    chat_id=target, text=piece, parse_mode=None, **options
                )
            except Exception as error:
                # An Owner Chat failure must not kill the poll loop.
                logger.warning(
                    "[amessenger] Telegram Owner Chat post failed: %s",
                    error,
                    exc_info=True,
                )
                return delivered, len(pieces)
            delivered += 1
    return delivered, len(pieces)
