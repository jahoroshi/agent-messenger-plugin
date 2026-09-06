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
import ssl
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


def certificate_context(ca_file: str):
    """Trust the authority that signed the relay's certificate, when named.

    A relay on an internal network is behind a private CA, and nothing on this
    host trusts it yet. Returning None keeps urllib's own default, which is the
    public authorities.
    """
    if not str(ca_file or "").strip():
        return None
    return ssl.create_default_context(cafile=str(ca_file).strip())


def owner_login(relay: str, key: str, ca_file: str = "") -> str:
    """Ask the relay who this key belongs to, before any Agent exists."""
    request = urllib.request.Request(
        f"{relay}/v1/whoami", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(
        request, timeout=30, context=certificate_context(ca_file)
    ) as response:
        return (json.loads(response.read().decode("utf-8")) or {}).get("login") or ""


def agent_name_from_login(login: str) -> str:
    """Turn a Redmine login into a valid, and already unique, Agent name."""
    name = re.sub(r"[^a-z0-9-]+", "-", login.strip().lower()).strip("-")
    return name[:32].rstrip("-")


def publish_card(relay: str, key: str, agent: str, kind: str, ca_file: str = "") -> dict:
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
    with urllib.request.urlopen(
        request, timeout=30, context=certificate_context(ca_file)
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="amessenger-provision")
    parser.add_argument("--owner-chat", required=True,
                        help="platform, or platform:chat_id. `google_chat` alone uses this Hermes home channel, so no chat id is needed.")
    parser.add_argument("--agent", default="",
                        help="Agent name; default is the Owner's login, already unique")
    parser.add_argument("--kind", default="corporate", choices=KINDS)
    parser.add_argument("--key", default="", help="Owner key; default REDMINE_API_KEY")
    parser.add_argument("--relay", default="", help="relay URL; default the shipped one")
    parser.add_argument("--owner-user", default="", help="Owner id when the chat is a group")
    parser.add_argument("--ca-file", default="",
                        help="PEM file of the authority that signed the relay certificate")
    args = parser.parse_args(argv)

    agent = args.agent.strip()
    if AGENT_NAME.fullmatch(agent) is None and agent:
        print(
            "AMessenger provisioning failed.\n"
            f"Agent: {agent}\n"
            "Reason: an Agent name must have 2–32 lower-case letters, digits, "
            "or hyphens.\n\n"
            "Try again:\n"
            "amessenger-provision --owner-chat <platform:chat-id> "
            "--agent <agent-name>",
            file=sys.stderr,
        )
        return 2
    if not args.owner_chat.strip():
        print(
            "AMessenger provisioning failed.\n"
            "Reason: --owner-chat must name the Owner Chat.\n\n"
            "Try again:\n"
            "amessenger-provision --owner-chat <platform:chat-id>",
            file=sys.stderr,
        )
        return 2

    env_path = profile_home() / ".env"
    existing = read_env(env_path)
    key = args.key or existing.get("REDMINE_API_KEY", "")
    if not key:
        print(
            "AMessenger provisioning failed.\n"
            "Reason: no Owner key was found.\n"
            "Pass --key or put REDMINE_API_KEY in the profile .env.",
            file=sys.stderr,
        )
        return 2
    ca_file = (args.ca_file or existing.get("AMESSENGER_CA_FILE") or "").strip()
    if ca_file:
        try:
            certificate_context(ca_file)
        except (OSError, ssl.SSLError) as error:
            print(
                "AMessenger provisioning failed.\n"
                f"Reason: the relay CA file could not be used: {error}",
                file=sys.stderr,
            )
            return 2
    relay = (args.relay or existing.get("AMESSENGER_URL") or shipped_relay_url()).rstrip("/")
    if not relay:
        print(
            "AMessenger provisioning failed.\n"
            "Reason: no relay address is configured.\n\n"
            "Try again:\n"
            "amessenger-provision --owner-chat <platform:chat-id> --relay <url>",
            file=sys.stderr,
        )
        return 2

    if not agent:
        # Nobody should have to invent a name for each of thirty Owners: the
        # Redmine login is unique already and the key proves who it belongs to.
        try:
            agent = agent_name_from_login(owner_login(relay, key, ca_file))
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as error:
            print(
                "AMessenger provisioning failed.\n"
                f"Relay: {relay}\n"
                f"Reason: the Owner identity could not be read: {error}",
                file=sys.stderr,
            )
            return 1
        if AGENT_NAME.fullmatch(agent) is None:
            print(
                "AMessenger provisioning failed.\n"
                "Reason: an Agent name could not be derived from the Owner login.\n\n"
                "Try again:\n"
                "amessenger-provision --owner-chat <platform:chat-id> "
                "--agent <agent-name>",
                file=sys.stderr,
            )
            return 2

    try:
        card = publish_card(relay, key, agent, args.kind, ca_file)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        print(
            "AMessenger provisioning failed.\n"
            f"Relay: {relay}\n"
            f"Reason: the relay refused the Card: {error.code} — {detail}",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, OSError) as error:
        print(
            "AMessenger provisioning failed.\n"
            f"Relay: {relay}\n"
            f"Reason: the relay is unreachable: {error}",
            file=sys.stderr,
        )
        return 1

    updates = {
        "AMESSENGER_URL": relay,
        "AMESSENGER_KEY": key,
        "AMESSENGER_AGENT": agent,
        "AMESSENGER_KIND": args.kind,
        "AMESSENGER_OWNER_CHAT": args.owner_chat.strip(),
    }
    if args.owner_user.strip():
        updates["AMESSENGER_OWNER_USER"] = args.owner_user.strip()
    if ca_file:
        updates["AMESSENGER_CA_FILE"] = ca_file
    write_env(env_path, updates)

    owner = (card.get("owner") or {}) if isinstance(card, dict) else {}
    print(
        "AMessenger is configured.\n"
        f"Agent: {agent}\n"
        f"Kind: {args.kind}\n"
        f"Owner: {owner.get('name') or owner.get('login') or 'unknown'}\n"
        f"Owner Chat: {args.owner_chat}\n\n"
        "The gateway restarts next and reports whether Messages arrive."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
