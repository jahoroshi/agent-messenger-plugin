"""One health report, so every surface answers the same question the same way.

Before this module there were four different answers to "is mail working?".
`/amsg status` asked the relay first and returned nothing useful when the relay
was down. The tools asked `receive_problem()`. The installer asked nothing and
printed that mail would arrive. Hermes's own platform status said `connected`
from the moment the background tasks were created, which is before the Card is
published and long before any inbox poll has succeeded.

The rule this module exists to keep is: **one component's success may never
clear another component's failure.** A relay that answers proves nothing about
the Owner Chat, and an Owner Chat that accepts a line proves nothing about the
relay. So the parts are recorded apart and only the summary joins them.

Nothing here performs I/O against the relay or the Owner Chat. The adapter,
which is the only thing that knows what happened, hands the facts over; this
module decides what they add up to and how it is written down.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

SNAPSHOT_FILENAME = "health.json"

# Summary outcomes. These are AMessenger's own words for its own state; they are
# not Hermes platform states and must not be handed to Hermes as one.
WAITING_FOR_SETUP = "waiting_for_setup"
STARTING = "starting"
READY = "ready"
DEGRADED = "degraded"
STOPPED = "stopped"
UNKNOWN = "unknown"

# Component values, kept as words rather than booleans: "unknown" is a real and
# common answer, and a boolean would force it to be read as a failure.
RESOLVED, WAITING = "resolved", "waiting"
POLLING, OK, FAILING = "polling", "ok", "failing"

# The Owner Chat adapter may legitimately take five minutes to start
# (OWNER_WAIT_MAX_SECONDS), so a record older than that is old enough to be
# meaningless but not so tight that a slow start looks like a death.
SNAPSHOT_STALE_SECONDS = 360

# A record written by a process that is no longer the gateway says nothing about
# the gateway that runs now.
STALE_RECORD = "the gateway has not updated its health record"
NO_RECORD = "no gateway has written a health record for this profile"


@dataclass(frozen=True)
class Report:
    """What is known about one installation's ability to carry mail.

    Every field is either a word from the sets above or a sanitized string. No
    field may carry a key, a chat id, or the text of a Message: this record is
    written to a file an operator reads and is printed by the installer.
    """

    summary: str = UNKNOWN
    fault: str = ""
    fault_code: str = ""
    fault_since: str = ""
    agent: str = ""
    profile: str = ""
    revision: str = ""
    owner_chat: str = UNKNOWN
    receiver: str = UNKNOWN
    last_poll_at: str = ""
    receive_fault: str = ""
    posting: str = UNKNOWN
    last_post_at: str = ""
    post_fault: str = ""
    pending_mirrors: int = 0
    mirrors_lost: int = 0
    housekeeping: str = UNKNOWN
    last_housekeeping_at: str = ""
    housekeeping_fault: str = ""

    def is_ready(self) -> bool:
        return self.summary == READY

    def to_dict(self) -> dict:
        return asdict(self)


def from_dict(values) -> Report:
    """Build a Report from a file that another version may have written."""
    if not isinstance(values, dict):
        return Report()
    known = {field: values[field] for field in Report.__dataclass_fields__ if field in values}
    counts = ("pending_mirrors", "mirrors_lost")
    for name in counts:
        if name in known and not isinstance(known[name], int):
            known.pop(name)
    for name, value in list(known.items()):
        if name not in counts and not isinstance(value, str):
            known.pop(name)
    return Report(**known)


def summarize(report: Report) -> Report:
    """Decide the one word, from the component facts and nothing else.

    Order matters, and it is the order of what an Owner can act on. Waiting for
    setup comes first because every other component is meaningless until the
    Owner Chat exists. Ready comes last because it is the only claim that has to
    be earned: one completed inbox poll and one delivered Owner line.
    """
    if report.receiver == STOPPED:
        summary, code, fault = STOPPED, "receiver_stopped", report.receive_fault
    elif report.owner_chat == WAITING:
        summary, code, fault = WAITING_FOR_SETUP, "waiting_for_setup", report.receive_fault
    elif report.receive_fault:
        summary, code, fault = DEGRADED, report.fault_code or "receive_fault", report.receive_fault
    elif report.posting == FAILING:
        # Mail arrives and the Owner never sees it. Nothing else is wrong, and
        # that is exactly why this has to be its own failure: a healthy relay
        # would otherwise report a healthy installation.
        summary, code, fault = DEGRADED, "owner_chat_unusable", report.post_fault
    elif report.housekeeping == FAILING:
        summary, code, fault = DEGRADED, "housekeeping_failing", report.housekeeping_fault
    elif report.receiver == POLLING and report.posting == OK:
        summary, code, fault = READY, "", ""
    elif report.receiver in {POLLING, STARTING} or report.owner_chat == RESOLVED:
        summary, code, fault = STARTING, "", ""
    else:
        summary, code, fault = UNKNOWN, "", ""
    return replace(report, summary=summary, fault_code=code, fault=fault)


def lines(report: Report) -> list[str]:
    """The report as an Owner or an operator reads it, one fact per line."""
    out = [f"Summary: {report.summary}"]
    if report.agent:
        out.append(f"Agent: {report.agent}")
    if report.profile:
        out.append(f"Profile: {report.profile}")
    if report.fault:
        out.append(f"Reason: {report.fault}")
    out.append(f"Owner Chat: {report.owner_chat}")
    receiver = report.receiver
    if report.last_poll_at:
        receiver = f"{receiver}, last inbox poll {report.last_poll_at}"
    out.append(f"Receiver: {receiver}")
    posting = report.posting
    if report.last_post_at:
        posting = f"{posting}, last Owner line {report.last_post_at}"
    out.append(f"Owner posting: {posting}")
    if report.pending_mirrors:
        out.append(f"Owner lines waiting: {report.pending_mirrors}")
    if report.mirrors_lost:
        out.append(f"Owner lines lost: {report.mirrors_lost}")
    if report.housekeeping == FAILING:
        out.append(f"Background maintenance: {report.housekeeping_fault or FAILING}")
    return out


def render(report: Report) -> str:
    return "\n".join(lines(report))


def installed_revision(module_dir: Path | None = None) -> str:
    """Name the code that is actually running, not the code someone meant to run.

    Every declared version has said `0.1.0` since the first release, so it
    identifies nothing. Hermes installs the plugin with a Git clone, so the
    checked-out commit is the one honest identifier available. It is read out of
    the files rather than by running Git: this is called from a gateway, and a
    subprocess in a health check is a new way to hang.
    """
    module_dir = Path(module_dir or Path(__file__).resolve().parent)
    version = ""
    with _ignored():
        for line in (module_dir / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip() == "version":
                version = value.strip()
                break
    revision = _git_revision(module_dir)
    if version and revision:
        return f"{version}+{revision}"
    return version or revision


def _git_revision(module_dir: Path) -> str:
    for root in (module_dir, module_dir.parent):
        git = root / ".git"
        head = ""
        with _ignored():
            head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head:
            continue
        if head.startswith("ref:"):
            reference = head.split(":", 1)[1].strip()
            with _ignored():
                return (git / reference).read_text(encoding="utf-8").strip()[:12]
            with _ignored():
                for line in (git / "packed-refs").read_text(encoding="utf-8").splitlines():
                    sha, _, name = line.partition(" ")
                    if name.strip() == reference:
                        return sha[:12]
        else:
            return head[:12]
    return ""


def snapshot_path(home: Path) -> Path:
    return Path(home) / SNAPSHOT_FILENAME


def write_snapshot(path: Path, report: Report, *, moment: datetime, pid: int) -> None:
    """Save the report where a process that is not this one can read it.

    Written whole and renamed into place: a reader that finds half a file has no
    way to tell it from a healthy installation with fewer facts, and would say
    "unknown" where it should say what is wrong.
    """
    document = {"written_at": _ts(moment), "pid": int(pid), **report.to_dict()}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False
    )
    try:
        with handle:
            json.dump(document, handle, sort_keys=True)
            handle.write("\n")
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except BaseException:
        with _ignored():
            os.unlink(handle.name)
        raise


def read_snapshot(path: Path, *, moment: datetime | None = None) -> Report:
    """Read what the gateway last wrote, refusing to repeat a stale claim.

    A record nobody has updated is not evidence of health; a gateway that has
    been killed leaves its last happy record behind. Age is therefore checked
    before the content is believed, and an old record reports `unknown` with the
    reason, never `ready`.
    """
    moment = moment or datetime.now(timezone.utc)
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Report(summary=UNKNOWN, fault_code="no_record", fault=NO_RECORD)
    except (OSError, ValueError):
        return Report(summary=UNKNOWN, fault_code="unreadable_record",
                      fault="the gateway's health record could not be read")
    report = from_dict(document)
    written_at = _parse_ts(document.get("written_at") if isinstance(document, dict) else None)
    if written_at is None or moment - written_at > timedelta(seconds=SNAPSHOT_STALE_SECONDS):
        return replace(
            report,
            summary=UNKNOWN,
            fault_code="stale_record",
            fault=STALE_RECORD,
        )
    return report


# The same fixed-width UTC stamp the rest of the plugin writes, repeated rather
# than imported: this module has to stay readable by a diagnostic that runs when
# the plugin cannot be imported at all.
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_TS_FORMATS = (_TS_FORMAT, "%Y-%m-%dT%H:%M:%SZ")


def _ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime(_TS_FORMAT)


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    for pattern in _TS_FORMATS:
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


class _ignored:
    """Suppress a cleanup failure so it cannot replace the real exception."""

    def __enter__(self):
        return self

    def __exit__(self, *_exception) -> bool:
        return True
