"""Say what is wrong with an AMessenger installation, from a shell.

This exists for the case every other surface cannot cover: the plugin does not
import, so there is no `/amsg`, no tool, and no adapter to ask. That is also the
case an Owner reports as "it does nothing", and until now the only evidence was
a gateway log line they could not read.

So this file imports nothing from the rest of the plugin, exactly as
provision.py does not. It reads files: the profile `.env`, the plugin directory,
and the health record the gateway writes. It then tries the import last, and
reports its failure as a finding rather than dying of it.

It prints no key and no Message text, because an Owner will paste its output
into a chat to ask for help.

    HERMES_HOME=~/.hermes/profiles/work python3 .../amessenger/doctor.py

Exit codes, so a script can act on it:
    0  ready
    1  a fault that needs repair
    2  it could not run at all
    3  installed, and waiting for something a human must do
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import sys
from dataclasses import replace
from pathlib import Path

READY, FAULT, CANNOT_RUN, ACTION_REQUIRED = 0, 1, 2, 3

# Named so the report can say which are present without printing any value.
SETTINGS = (
    "AMESSENGER_URL",
    "AMESSENGER_KEY",
    "AMESSENGER_AGENT",
    "AMESSENGER_KIND",
    "AMESSENGER_OWNER_CHAT",
    "AMESSENGER_OWNER_USER",
    "AMESSENGER_CA_FILE",
)
# Without every one of these Hermes refuses to register the platform at all, so
# a gateway can be running while AMessenger inside it never starts.
REQUIRED_SETTINGS = (
    "AMESSENGER_URL",
    "AMESSENGER_KEY",
    "AMESSENGER_AGENT",
    "AMESSENGER_KIND",
    "AMESSENGER_OWNER_CHAT",
)
# Values that identify rather than authenticate. Everything else is reported as
# present or missing and never shown.
SHOWABLE = {"AMESSENGER_URL", "AMESSENGER_AGENT", "AMESSENGER_KIND", "AMESSENGER_CA_FILE"}
# Copies of what the plugin ships. This file imports nothing from the package
# (see the module docstring), so it keeps its own; tests/test_doctor.py keeps
# every copy in step with the original.
DEFAULT_KIND = "corporate"
OWNER_KEY_VARIABLES = ("AMESSENGER_KEY", "REDMINE_API_KEY")
# Values the first gateway start after an install derives for itself. Missing
# one of these is a wait, never a fault, and never an operator's errand.
DERIVABLE_SETTINGS = ("AMESSENGER_AGENT", "AMESSENGER_OWNER_CHAT")


def shipped_defaults(module_dir: Path) -> dict:
    """Read the values defaults.py ships, without importing it."""
    try:
        source = (module_dir / "defaults.py").read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for literal, variable in (("RELAY_URL", "AMESSENGER_URL"), ("KIND", "AMESSENGER_KIND")):
        match = re.search(rf'^{literal}\s*=\s*"([^"]*)"', source, re.MULTILINE)
        value = (match.group(1).strip() if match else "")
        if value:
            out[variable] = value
    return out


def effective_values(values: dict, shipped: dict) -> dict:
    """What this profile actually runs with: what is written, else the default.

    A value that has a default is never reported as missing. Reporting it sends
    an Owner to write a line the plugin already knows the answer to, which is
    the whole fault this diagnostic exists to catch.
    """
    out = {name: str(value).strip() for name, value in values.items()}
    key = next(
        (out[name] for name in OWNER_KEY_VARIABLES if out.get(name, "")),
        "",
    )
    if key:
        out["AMESSENGER_KEY"] = key
    for name, value in shipped.items():
        if not out.get(name, ""):
            out[name] = value
    return out


def load_health(module_dir: Path):
    """Load health.py beside this file without importing the plugin package."""
    spec = importlib.util.spec_from_file_location(
        "amessenger_doctor_health", module_dir / "health.py"
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def profile_home(argument: str = "") -> Path | None:
    home = (argument or os.environ.get("HERMES_HOME", "")).strip()
    return Path(home) if home else None


def read_env(path: Path) -> dict:
    values = {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        name, separator, value = candidate.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name.strip()):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def settings_lines(values: dict, written: dict | None = None) -> list[str]:
    """Report each value, saying which are the plugin's own rather than typed."""
    written = values if written is None else written
    out = []
    for name in SETTINGS:
        value = str(values.get(name, "")).strip()
        defaulted = bool(value) and not str(written.get(name, "")).strip()
        note = " (the plugin's own)" if defaulted else ""
        if not value:
            out.append(f"{name}: not set")
        elif name in SHOWABLE:
            out.append(f"{name}: {value}{note}")
        elif name == "AMESSENGER_OWNER_CHAT":
            # The platform decides half the faults in this file, so it is shown.
            # The chat id is not: this output gets pasted into a chat to ask for
            # help, and naming a chat tells a reader where to try to claim it.
            platform, separator, chat_id = value.partition(":")
            out.append(
                f"{name}: {platform}"
                + (
                    " with a chat id"
                    if separator and chat_id.strip()
                    else " with no chat id yet"
                )
            )
        else:
            out.append(f"{name}: set{note}")
    return out


def import_check(module_dir: Path) -> tuple[str, str]:
    """Try the import last, and report its failure instead of dying of it.

    A scanner rejection, a mangled file and a syntax error all end here, and
    each makes every other AMessenger surface silent at once.

    A missing module that is not AMessenger's own is a different thing: this
    diagnostic runs under whatever python3 is on PATH, which is usually not the
    one Hermes runs in, and Hermes's own packages are not importable there. That
    is not a fault in the installation and must not be reported as one.
    """
    parent = str(module_dir.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        importlib.import_module(module_dir.name)
    except ModuleNotFoundError as error:
        missing = str(getattr(error, "name", "") or "")
        if missing != module_dir.name and not missing.startswith(f"{module_dir.name}."):
            return "not_checked", missing or "a module this python does not have"
        return "failed", f"{type(error).__name__}: {error}"
    except BaseException as error:  # noqa: BLE001 - the report is the product
        return "failed", f"{type(error).__name__}: {error}"
    finally:
        if inserted and sys.path and sys.path[0] == parent:
            sys.path.pop(0)
    return "ok", ""


def report(module_dir: Path, home: Path | None) -> tuple[list[str], int]:
    health = load_health(module_dir)
    lines = ["AMessenger diagnostic"]
    if health is not None:
        lines.append(f"Plugin: {module_dir}")
        lines.append(f"Revision: {health.installed_revision(module_dir) or 'unknown'}")
    else:
        lines.append(f"Plugin: {module_dir}")
        lines.append("Revision: unknown; health.py could not be read")

    if home is None:
        lines.append(
            "Profile: unknown\n"
            "Reason: HERMES_HOME is not set.\n\n"
            "Run it again with the profile:\n"
            "HERMES_HOME=~/.hermes/profiles/<profile> python3 "
            f"{module_dir / 'doctor.py'}"
        )
        return lines, CANNOT_RUN

    lines.append(f"Profile: {home}")
    written = read_env(home / ".env")
    values = effective_values(written, shipped_defaults(module_dir))
    lines.extend(settings_lines(values, written))

    status, detail = import_check(module_dir)
    if status == "failed":
        lines.append(
            "Import: failed\n"
            f"Reason: {detail}\n"
            "Nothing in AMessenger runs while this fails: no /amsg, no tools, "
            "and no receiving."
        )
    elif status == "not_checked":
        lines.append(
            "Import: not checked here\n"
            f"Reason: this python has no {detail}, so it is not the one Hermes "
            "runs in.\n"
            "The gateway's own health record below is the evidence that it loads."
        )
    else:
        lines.append("Import: ok")

    if health is None:
        return lines, FAULT

    record = health.read_snapshot(home / "amessenger" / health.SNAPSHOT_FILENAME)
    if record.fault_code == "no_record":
        # "No record" and "no gateway" read the same and are not the same. A
        # profile with some AMessenger settings and not others is refused by
        # Hermes at registration, so the gateway runs and AMessenger in it never
        # starts. The profile is right here, so say which values are missing.
        missing = [
            name
            for name in REQUIRED_SETTINGS
            if not str(values.get(name, "")).strip()
        ]
        if missing and set(missing) <= set(DERIVABLE_SETTINGS):
            # Nothing here needs a hand: the first start names the Agent after
            # the key's Owner and takes the Owner Chat from the home channel.
            lines.append(
                "Configuration: complete on the next gateway start\n"
                "Reason: "
                + ", ".join(missing)
                + " are derived then, from the key this profile already holds."
            )
        elif missing and len(missing) < len(REQUIRED_SETTINGS):
            record = replace(
                record,
                fault_code="half_configured",
                fault=health.no_record_reason(missing),
            )
    lines.append(health.render(record))
    if status == "failed":
        return lines, FAULT
    if record.summary == health.READY:
        return lines, READY
    if record.summary == health.DEGRADED:
        return lines, FAULT
    # `stopped` is written by a clean shutdown, so it means stopped on purpose;
    # a gateway that died leaves its previous record behind to go stale instead.
    # Either way the next step is a person's, which is what 3 says.
    return lines, ACTION_REQUIRED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="amessenger-doctor")
    parser.add_argument(
        "--profile-home",
        default="",
        help="the Hermes profile directory; default $HERMES_HOME",
    )
    args = parser.parse_args(argv)
    module_dir = Path(__file__).resolve().parent
    lines, code = report(module_dir, profile_home(args.profile_home))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
