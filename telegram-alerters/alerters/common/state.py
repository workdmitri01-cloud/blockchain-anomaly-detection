"""Persistent per-bot state: last scanned block per chain + recently sent alert keys."""
from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict
from pathlib import Path


class State:
    MAX_SENT = 20000

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.last_block: dict[str, int] = {}
        self.chunk: dict[str, int] = {}
        self._sent: OrderedDict[str, None] = OrderedDict()
        # Free-form per-bot storage (must be JSON-serialisable).
        self.extra: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        self.last_block = {k: int(v) for k, v in data.get("last_block", {}).items()}
        self.chunk = {k: int(v) for k, v in data.get("chunk", {}).items()}
        self._sent = OrderedDict((k, None) for k in data.get("sent", []))
        self.extra = data.get("extra", {})

    def was_sent(self, key: str) -> bool:
        return key in self._sent

    def mark_sent(self, key: str) -> None:
        self._sent[key] = None
        while len(self._sent) > self.MAX_SENT:
            self._sent.popitem(last=False)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_block": self.last_block,
            "chunk": self.chunk,
            "sent": list(self._sent),
            "extra": self.extra,
        }
        # Atomic write so a crash never leaves a half-written state file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)
