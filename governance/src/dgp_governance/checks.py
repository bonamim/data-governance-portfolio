"""How a governance check reports its outcome, so ``make check`` reads as one aligned list.

Every check prints one line: a mark, the policy's name, and what it found. Details that belong to
a check (a notice, a homonym) go on indented lines under it.
"""

from __future__ import annotations

NAME_WIDTH = 18


def passed(name: str, detail: str) -> str:
    return f"✓ {name:<{NAME_WIDTH}} {detail}"


def failed(name: str, detail: str) -> str:
    return f"✗ {name:<{NAME_WIDTH}} {detail}"


def note(detail: str) -> str:
    """A line of detail under a check's result."""
    return f"    {detail}"


def plural(n: int, word: str) -> str:
    """``1 agent``, ``2 agents``."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
