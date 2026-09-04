"""Addresses shipped with this checkout of AMessenger.

An operator sets these once, in the repository that every Hermes installs the
plugin from. Each Agent installed from that repository inherits them, so the
Owner supplies only their Redmine API key.

The environment always wins. ``AMESSENGER_URL`` in the profile ``.env``, and
``/amsg setup --relay <url>``, both override ``RELAY_URL`` below.
"""

# Base URL of the AMessenger relay that this installation talks to, for example
# "https://amessenger.example.com". Empty means no address is shipped: setup
# then asks the Owner for `--relay <url>` rather than guess an address.
# The relay every Agent installed from this repository talks to. It is shipped
# so an Owner never types an address: install, then `/amsg setup`.
# A public address on purpose: Agents run on different hosts, so a private or
# loopback address only ever works for one of them.
RELAY_URL = "http://34.179.158.248:8010"

# Git URL that install.sh installs the plugin from when it is run with neither
# --repo nor AMESSENGER_REPOSITORY_URL. install.sh holds its own copy of this
# value because it must run before Python does; tests/test_defaults.py keeps
# the two in step.
REPOSITORY_URL = "git@gitlab.azati.com:andrei.shelepen/agent-messenger.git"


def relay_url() -> str:
    """Return the shipped relay address in the form settings store it."""
    return RELAY_URL.strip().rstrip("/")
