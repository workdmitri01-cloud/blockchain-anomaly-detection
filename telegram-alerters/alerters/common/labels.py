"""Exchange (CEX) address book.

Built by ``scripts/update_labels.py`` from open GitHub datasets:

* duneanalytics/spellbook ``cex_evms_addresses.sql`` (curated, ~5k EVM CEX wallets)
* brianleect/etherscan-labels (Etherscan / BscScan / ... public name tags)

EVM CEX hot wallets are EOAs and usually share the same address on every EVM
chain, so the book is chain-agnostic.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .addr import is_solana, is_tron, norm
from .config import PACKAGE_ROOT

DEFAULT_LABELS_FILE = PACKAGE_ROOT / "data" / "cex_addresses.json"


@dataclass(frozen=True)
class Label:
    entity: str  # "Binance"
    name: str  # "Binance 14"
    # None = any EVM chain; otherwise "tron" / "solana" / "hypercore" (EVM address active on HyperCore)
    family: str | None = None


class AddressBook:
    def __init__(self, entries: dict[str, Label] | None = None):
        self._entries: dict[str, Label] = {norm(k): v for k, v in (entries or {}).items()}
        self._hypercore: set[str] = set()

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "AddressBook":
        path = Path(path or DEFAULT_LABELS_FILE)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = {addr: Label(entity=v[0], name=v[1], family=v[2] if len(v) > 2 else None)
                   for addr, v in data.get("addresses", {}).items()}
        book = cls(entries)
        # EVM exchange wallets seen active on Hyperliquid HyperCore (see update_labels.py --hypercore)
        book._hypercore = {norm(a) for a in data.get("hypercore", [])}
        return book

    def add(self, address: str, entity: str, name: str | None = None, family: str | None = None) -> None:
        if family is None and not address.startswith("0x"):
            family = "tron" if is_tron(address) else "solana" if is_solana(address) else None
        self._entries[norm(address)] = Label(entity=entity, name=name or entity, family=family)

    def remove_entities(self, entities: list[str]) -> None:
        drop = {e.lower() for e in entities}
        self._entries = {a: l for a, l in self._entries.items() if l.entity.lower() not in drop}

    def keep_entities(self, entities: list[str]) -> None:
        keep = {e.lower() for e in entities}
        self._entries = {a: l for a, l in self._entries.items() if l.entity.lower() in keep}

    def get(self, address: str) -> Label | None:
        return self._entries.get(norm(address))

    def __contains__(self, address: str) -> bool:
        return norm(address) in self._entries

    def addresses_for(self, kind: str) -> set[str]:
        """Exchange wallets to watch on an account-polled chain."""
        if kind == "hypercore":
            return {a for a in self._hypercore if a in self._entries} | {
                a for a, l in self._entries.items() if l.family == "hypercore"}
        if kind == "evm":
            return {a for a, l in self._entries.items() if l.family in (None, "hypercore")}
        return {a for a, l in self._entries.items() if l.family == kind}

    def __len__(self) -> int:
        return len(self._entries)
