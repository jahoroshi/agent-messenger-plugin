"""Addresses shipped with this checkout of AMessenger.

An operator sets these once, in the repository that every Hermes installs the
plugin from. Each Agent installed from that repository inherits them, so the
Owner supplies only their Redmine API key.

The environment always wins. ``AMESSENGER_URL`` in the profile ``.env``, and
``/amsg setup --relay <url>``, both override ``RELAY_URL`` below.
"""

# Base URL of the AMessenger relay that this installation talks to, for example
# "https://amessenger.example.com". It is shipped so an Owner never types an
# address: install, then `/amsg setup`.
#
# Empty means no address is shipped. Setup then asks the Owner for
# `--relay <url>` rather than guess an address, and the installer refuses to
# finish without one. That is deliberate: an address that no longer answers
# sends every Agent of this installation at a host that is not the relay, and
# the Owner is told the relay is unreachable instead of that none was chosen.
#
# When it is filled in, use a name rather than an address, so the relay can move
# hosts without a reinstall. Agents run on different hosts, so a private or
# loopback address only ever works for one of them.
RELAY_URL = ""

# Git URL that install.sh installs the plugin from when it is run with neither
# --repo nor AMESSENGER_REPOSITORY_URL. install.sh holds its own copy of this
# value because it must run before Python does; tests/test_defaults.py keeps
# the two in step.
# The plugin is published on its own, at the root of this repository, so the
# identifier needs no subdirectory fragment and no credentials.
REPOSITORY_URL = "https://github.com/jahoroshi/agent-messenger-plugin.git"


def relay_url() -> str:
    """Return the shipped relay address in the form settings store it."""
    return RELAY_URL.strip().rstrip("/")
