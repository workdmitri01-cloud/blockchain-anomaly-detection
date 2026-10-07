"""Bot #2: large token deposits to / withdrawals from centralized exchanges."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..common.config import (
    BaseBotConfig,
    ChainConfig,
    TelegramConfig,
    load_chains,
    load_yaml,
    parse_telegram,
    parse_tokens,
)
from ..common.evm import TRANSFER_TOPIC, chunked
from ..common.labels import AddressBook, Label
from ..common.runner import Alert, BaseAlerter, Valued
from ..common.telegram import esc, fmt_amount, fmt_usd, short


@dataclass
class ExchangeBotConfig(BaseBotConfig):
    min_usd: float = 80_000.0
    # Only these exchanges (empty = all from the address book).
    include_exchanges: list[str] = field(default_factory=list)
    exclude_exchanges: list[str] = field(default_factory=list)
    # {address: "Exchange name"} added on top of the open-source address book.
    extra_exchange_addresses: dict[str, str] = field(default_factory=dict)
    # Binance hot -> Binance cold etc.
    alert_same_exchange: bool = False
    # Binance -> OKX
    alert_exchange_to_exchange: bool = True
    # Remember large transfers into unknown addresses to detect CEX deposit addresses
    # (user -> deposit address -> exchange hot wallet sweep).
    track_deposit_addresses: bool = True
    deposit_memory_hours: float = 72
    known_addresses: dict[str, str] = field(default_factory=dict)


def load_config(path: str, dry_run: bool = False) -> ExchangeBotConfig:
    raw = load_yaml(path)
    tokens = parse_tokens(raw.get("tokens"))
    if not tokens:
        raise ValueError("exchange flows bot: 'tokens' list is empty")
    telegram = (
        TelegramConfig(bot_token="", chat_id="")
        if dry_run and not (raw.get("telegram") or {}).get("bot_token")
        else parse_telegram(raw.get("telegram"), "exchange flows bot")
    )
    ex = raw.get("exchanges") or {}
    return ExchangeBotConfig(
        telegram=telegram,
        chains=load_chains(raw.get("chains"), {t.chain for t in tokens}),
        tokens=tokens,
        state_file=raw.get("state_file", "state/exchange_flows.json"),
        poll_interval=int(raw.get("poll_interval", 20)),
        labels_file=raw.get("labels_file"),
        min_usd=float(raw.get("min_usd", 80_000)),
        include_exchanges=list(ex.get("include") or []),
        exclude_exchanges=list(ex.get("exclude") or []),
        extra_exchange_addresses={k.lower(): v for k, v in (ex.get("extra_addresses") or {}).items()},
        alert_same_exchange=bool(raw.get("alert_same_exchange", False)),
        alert_exchange_to_exchange=bool(raw.get("alert_exchange_to_exchange", True)),
        track_deposit_addresses=bool(raw.get("track_deposit_addresses", True)),
        deposit_memory_hours=float(raw.get("deposit_memory_hours", 72)),
        known_addresses={k.lower(): v for k, v in (raw.get("known_addresses") or {}).items()},
    )


class ExchangeFlowsAlerter(BaseAlerter):
    name = "exchange-flows"
    cfg: ExchangeBotConfig

    def __init__(self, *args, book: AddressBook | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.book = book if book is not None else AddressBook.from_file(self.cfg.labels_file)
        if self.cfg.include_exchanges:
            self.book.keep_entities(self.cfg.include_exchanges)
        if self.cfg.exclude_exchanges:
            self.book.remove_entities(self.cfg.exclude_exchanges)
        for addr, entity in self.cfg.extra_exchange_addresses.items():
            self.book.add(addr, entity)
        if not len(self.book):
            raise ValueError("exchange flows bot: exchange address book is empty - run scripts/update_labels.py")

    def log_filters(self, chain: ChainConfig) -> list[dict]:
        # One query returns every Transfer of every tracked token on the chain.
        tokens = [t.address for t in self.cfg.tokens_for(chain.name)]
        return [{"address": part, "topics": [TRANSFER_TOPIC]} for part in chunked(tokens, 50)]

    def _min_usd(self, chain: str, token: str) -> float:
        for t in self.cfg.tokens_for(chain):
            if t.address == token and t.min_usd is not None:
                return t.min_usd
        return self.cfg.min_usd

    # deposit-address memory ------------------------------------------------------
    def _remember(self, v: Valued) -> None:
        mem = self.state.extra.setdefault("deposit_candidates", {})
        tr = v.transfer
        mem[f"{tr.chain}:{tr.token}:{tr.to_addr}"] = [tr.from_addr, tr.tx_hash, time.time()]
        if len(mem) > 5000:  # drop the oldest entries
            for k in sorted(mem, key=lambda k: mem[k][2])[: len(mem) - 5000]:
                del mem[k]

    def _origin(self, tr) -> tuple[str, str] | None:
        mem = self.state.extra.get("deposit_candidates", {})
        hit = mem.pop(f"{tr.chain}:{tr.token}:{tr.from_addr}", None)
        if hit and time.time() - hit[2] <= self.cfg.deposit_memory_hours * 3600:
            return hit[0], hit[1]
        return None

    # formatting ---------------------------------------------------------------------
    def _who(self, chain: ChainConfig, addr: str, label: Label | None) -> str:
        link = f'<a href="{chain.address_url(addr)}">{short(addr)}</a>'
        if label:
            return f"🏦 <b>{esc(label.entity)}</b> [{esc(label.name)}] ({link})"
        if addr in self.cfg.known_addresses:
            return f"<b>{esc(self.cfg.known_addresses[addr])}</b> ({link})"
        return link

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        alerts = []
        for v in transfers:
            tr = v.transfer
            src, dst = self.book.get(tr.from_addr), self.book.get(tr.to_addr)
            threshold = self._min_usd(chain.name, tr.token)
            big = v.usd is not None and v.usd >= threshold
            if not src and not dst:
                if big and self.cfg.track_deposit_addresses:
                    self._remember(v)
                continue
            if not big:
                continue
            origin = None
            if src and dst:
                if src.entity == dst.entity:
                    if not self.cfg.alert_same_exchange:
                        continue
                    head = f"🔁 <b>Внутренний перевод {esc(src.entity)}</b>"
                elif self.cfg.alert_exchange_to_exchange:
                    head = f"🔀 <b>Перевод между биржами: {esc(src.entity)} → {esc(dst.entity)}</b>"
                else:
                    continue
            elif dst:
                head = f"📥 <b>ЗАВОД на биржу {esc(dst.entity)}</b>"
                origin = self._origin(tr) if self.cfg.track_deposit_addresses else None
            else:
                head = f"📤 <b>ВЫВОД с биржи {esc(src.entity)}</b>"
            text = (
                f"{head}\n\n"
                f"💰 <b>{fmt_amount(v.amount)} {esc(v.symbol)}</b> (~<b>{fmt_usd(v.usd)}</b>)\n"
                f"⛓ {esc(chain.name)} · блок {tr.block}\n"
                f"От: {self._who(chain, tr.from_addr, src)}\n"
                f"Кому: {self._who(chain, tr.to_addr, dst)}\n"
            )
            if origin:
                text += (
                    f"↪️ Через депозитный адрес, исходный отправитель: "
                    f"{self._who(chain, origin[0], None)} "
                    f'(<a href="{chain.tx_url(origin[1])}">tx</a>)\n'
                )
            text += f'🔗 <a href="{chain.tx_url(tr.tx_hash)}">Транзакция</a>'
            alerts.append(Alert(key=f"cex:{tr.key}", text=text))
        return alerts
