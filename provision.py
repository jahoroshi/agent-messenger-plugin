"""Configure AMessenger for a profile without a chat window.

`/amsg setup` reads the Owner Chat from the chat it was typed in. Some Hermes
gateways do not put a chat id in the event they hand to a plugin, and on those
the command cannot work at all -- the Owner is stuck with no way forward.

This module is the same job done from a shell, where the Owner Chat is an
argument instead of something read off an event. It deliberately imports
nothing from the rest of the plugin: it has to run under a bare python3 on a
host where Hermes's own packages are not importable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

AGENT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
KINDS = ("corporate", "personal")


def shipped_relay_url() -> str:
    """Read RELAY_URL out of the defaults module without importing it."""
    text = (Path(__file__).with_name("defaults.py")).read_text(encoding="utf-8")
    match = re.search(r'^RELAY_URL\s*=\s*"([^"]*)"', text, re.MULTILINE)
    return (match.group(1) if match else "").rstrip("/")


def profile_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if not home:
        raise SystemExit("HERMES_HOME is not set; run this through the Hermes profile.")
    return Path(home)


def read_env(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip()
    return values


def write_env(path: Path, updates: dict) -> None:
    """Replace or append each value, leaving every other line untouched."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    out, seen = [], set()
    for line in lines:
        name = line.partition("=")[0].strip()
        if name in updates:
            if name not in seen:
                out.append(f"{name}={updates[name]}\n")
                seen.add(name)
            continue
        out.append(line)
    if out and not out[-1].endswith("\n"):
        out.append("\n")
    for name, value in updates.items():
        if name not in seen:
            out.append(f"{name}={value}\n")
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    with handle:
        handle.writelines(out)
    os.chmod(handle.name, 0o600)
    os.replace(handle.name, path)


def publish_card(relay: str, key: str, agent: str, kind: str) -> dict:
    request = urllib.request.Request(
        f"{relay}/v1/agents/me",
        method="PUT",
        data=json.dumps({"kind": kind, "description": None}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "X-Agent": agent,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="amessenger-provision")
    parser.add_argument("--owner-chat", required=True,
                        help="platform or platform:chat_id, e.g. google_chat:spaces/AAA")
    parser.add_argument("--agent", required=True, help="globally unique Agent name")
    parser.add_argument("--kind", default="corporate", choices=KINDS)
    parser.add_argument("--key", default="", help="Owner key; default REDMINE_API_KEY")
    parser.add_argument("--relay", default="", help="relay URL; default the shipped one")
    parser.add_argument("--owner-user", default="", help="Owner id when the chat is a group")
    args = parser.parse_args(argv)

    if AGENT_NAME.fullmatch(args.agent) is None:
        print(
            f"'{args.agent}' is not a valid Agent name: 2-32 characters, lowercase "
            "letters, digits and hyphens, starting with a letter or digit.",
            file=sys.stderr,
        )
        return 2
    if not args.owner_chat.strip():
        print("--owner-chat must name the chat that should receive mail.", file=sys.stderr)
        return 2

    env_path = profile_home() / ".env"
    existing = read_env(env_path)
    key = args.key or existing.get("REDMINE_API_KEY", "")
    if not key:
        print(
            "No Owner key: pass --key, or put REDMINE_API_KEY in the profile .env.",
            file=sys.stderr,
        )
        return 2
    relay = (args.relay or existing.get("AMESSENGER_URL") or shipped_relay_url()).rstrip("/")
    if not relay:
        print("No relay address is configured; pass --relay <url>.", file=sys.stderr)
        return 2

    try:
        card = publish_card(relay, key, args.agent, args.kind)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        print(f"The relay at {relay} refused the Card ({error.code}): {detail}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(f"The relay at {relay} is unreachable: {error}", file=sys.stderr)
        return 1

    updates = {
        "AMESSENGER_URL": relay,
        "AMESSENGER_KEY": key,
        "AMESSENGER_AGENT": args.agent,
        "AMESSENGER_KIND": args.kind,
        "AMESSENGER_OWNER_CHAT": args.owner_chat.strip(),
    }
    if args.owner_user.strip():
        updates["AMESSENGER_OWNER_USER"] = args.owner_user.strip()
    write_env(env_path, updates)

    owner = (card.get("owner") or {}) if isinstance(card, dict) else {}
    print(
        f"AMessenger is configured. Agent {args.agent} ({args.kind}); "
        f"Owner {owner.get('name') or owner.get('login') or 'unknown'}; "
        f"Owner Chat {args.owner_chat}. Restart the gateway and mail will arrive there."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
