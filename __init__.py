"""The amessenger-hermes plugin package."""

from pathlib import Path

__version__ = "0.1.0"

_HERE = Path(__file__).resolve().parent
HELP_TEXT = (_HERE / "help.md").read_text(encoding="utf-8")   # §6.8: written once, loaded at import
SKILL_PATH = _HERE / "SKILL.md"                               # registered with ctx.register_skill in T8.1
