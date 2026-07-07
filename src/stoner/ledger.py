"""Append-only action log: .stoner/ledger.jsonl."""

from __future__ import annotations

import json
from pathlib import Path

from .types import LedgerEntry


class Ledger:
    def __init__(self, project_root: Path):
        self.path = project_root / ".stoner" / "ledger.jsonl"

    def append(self, action: str, target: str = "", session: str = "", **detail) -> LedgerEntry:
        entry = LedgerEntry(action=action, target=target, session=session, detail=detail)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        return entry

    def tail(self, n: int = 50) -> list[LedgerEntry]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[LedgerEntry] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(LedgerEntry.model_validate(json.loads(line)))
            except (json.JSONDecodeError, ValueError):
                continue
        return out
