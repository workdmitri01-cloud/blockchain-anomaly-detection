"""Bot #2: large token deposits to / withdrawals from centralized exchanges.

Two ways to choose tokens per chain:

* ``tokens:`` list - one eth_getLogs per chain filtered by token address (classic mode).
* ``auto_tokens.chains`` - every Transfer on the chain is fetched and filtered locally
  by exchange addresses, so new tokens are picked up automatically. Stablecoins,
  majors (ETH/BTC/BNB/SOL and their wrappers / LSTs), gold and unpriced / low-confidence
  tokens are excluded. On these chains CEX deposit addresses are learned from sweeps
  (EOA -> exchange hot wallet) and stored permanently in the state, so the next deposit
  to such an address is alerted in real time with the real sender.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..common.addr import norm
from ..common.config import (
    BaseBotConfig,
    ChainConfig,
    TelegramConfig,
    load_chains,
    load_yaml,
    parse_telegram,
    parse_tokens,
)
from ..common.evm import TRANSFER_TOPIC, address_topic, chunked
from ..common.labels import AddressBook, Label
from ..common.runner import Alert, BaseAlerter, Valued
from ..common.telegram import esc, fmt_amount, fmt_usd, short, special_label, where

# --- auto-token exclusion ------------------------------------------------------------
DEFAULT_EXCLUDE_SYMBOLS = {
    # stablecoins / savings wrappers
    "USDT", "USDC", "USDC.E", "DAI", "XDAI", "WXDAI", "SDAI", "USDS", "SUSDS", "FRAX", "LUSD",
    "GHO", "USDE", "SUSDE", "BUSD", "TUSD", "USDP", "PYUSD", "FDUSD", "USD1", "CRVUSD", "MIM",
    "EURE", "EURC", "EURC.E", "AGEUR", "EURA", "EUROC", "GBPE", "BRLA", "BREAD", "USDT0",
    # majors and their wrappers / liquid staking
    "ETH", "WETH", "STETH", "WSTETH", "RETH", "CBETH", "WEETH", "EETH", "EZETH", "OSETH",
    "BTC", "WBTC", "TBTC", "CBBTC", "LBTC", "BTCB", "SOLVBTC",
    "BNB", "WBNB", "SOL", "WSOL", "JITOSOL", "MSOL",
    # gold
    "PAXG", "XAUT", "XAU",
}
# 0-5 letter prefix + ETH/BTC/BNB/SOL (+ bridged ".e"): WETH, wstETH, cbBTC, aWETH, WBNB, bSOL ...
_MAJOR_RE = re.compile(r"^[A-Z]{0,5}(ETH|BTC|BNB|SOL)(\.E)?$")
# fiat-pegged by name: USDx, EURx, GBP, CHF, ... DAI-family
_PEGGED_RE = re.compile(r"(USD|EUR|GBP|CHF|JPY|BRL|XAU|DAI)")


@dataclass
class ExchangeBotConfig(BaseBotConfig):
    min_usd: float = 80_000.0
    # Only these exchanges (empty = all from the address book).
    include_exchanges: list[str] = field(default_factory=list)
    exclude_exchanges: list[str] = field(default_factory=list)
    # {address: "Exchange name"} added on top of the open-source address book.
    extra_exchange_addresses: dict[str, str] = field(default_factory=dict)
    # EVM-format exchange accounts on Hyperliquid HyperCore to poll.
    hypercore_exchange_addresses: dict[str, str] = field(default_factory=dict)
    # Binance hot -> Binance cold etc.
    alert_same_exchange: bool = False
    # Binance -> OKX
    alert_exchange_to_exchange: bool = True
    # Remember large transfers into unknown addresses to detect CEX deposit addresses
    # (user -> deposit address -> exchange hot wallet sweep).
    track_deposit_addresses: bool = True
    deposit_memory_hours: float = 72
    known_addresses: dict[str, str] = field(default_factory=dict)
    # --- auto-token chains ---
    auto_token_chains: list[str] = field(default_factory=list)
    exclude_symbols: set[str] = field(default_factory=lambda: set(DEFAULT_EXCLUDE_SYMBOLS))
    exclude_addresses: set[str] = field(default_factory=set)
    exclude_majors: bool = True
    exclude_pegged: bool = True          # price within 2% of $1 or fiat-like symbol
    min_price_confidence: float = 0.8    # DefiLlama confidence for auto tokens
    learn_deposit_addresses: bool = True
    deposit_lookback_blocks: int = 5000  # where to look for the original sender at first sweep


def load_config(path: str, dry_run: bool = False, cls=None, bot_name: str = "exchange flows bot",
                defaults: dict | None = None, extra=None) -> ExchangeBotConfig:
    """``cls`` / ``defaults`` / ``extra(raw) -> dict`` let other bots reuse this loader."""
    raw = load_yaml(path)
    d = {"min_usd": 80_000, "state_file": "state/exchange_flows.json", **(defaults or {})}
    tokens = parse_tokens(raw.get("tokens"))
    auto = raw.get("auto_tokens") or {}
    auto_chains = [str(c) for c in (auto.get("chains") or [])]
    if not tokens and not auto_chains:
        raise ValueError(f"{bot_name}: 'tokens' is empty and no 'auto_tokens.chains' set")
    telegram = (
        TelegramConfig(bot_token="", chat_id="")
        if dry_run and not (raw.get("telegram") or {}).get("bot_token")
        else parse_telegram(raw.get("telegram"), bot_name)
    )
    ex = raw.get("exchanges") or {}
    exclude_symbols = set(DEFAULT_EXCLUDE_SYMBOLS) if auto.get("default_excludes", True) else set()
    exclude_symbols |= {str(s).upper() for s in (auto.get("exclude_symbols") or [])}
    exclude_symbols -= {str(s).upper() for s in (auto.get("allow_symbols") or [])}
    return (cls or ExchangeBotConfig)(
        telegram=telegram,
        chains=load_chains(raw.get("chains"), {t.chain for t in tokens} | set(auto_chains)),
        tokens=tokens,
        state_file=raw.get("state_file", d["state_file"]),
        poll_interval=int(raw.get("poll_interval", 20)),
        labels_file=raw.get("labels_file"),
        min_usd=float(raw.get("min_usd", d["min_usd"])),
        include_exchanges=list(ex.get("include") or []),
        exclude_exchanges=list(ex.get("exclude") or []),
        extra_exchange_addresses={norm(k): v for k, v in (ex.get("extra_addresses") or {}).items()},
        hypercore_exchange_addresses={norm(k): v for k, v in (ex.get("hypercore_addresses") or {}).items()},
        alert_same_exchange=bool(raw.get("alert_same_exchange", False)),
        alert_exchange_to_exchange=bool(raw.get("alert_exchange_to_exchange", True)),
        track_deposit_addresses=bool(raw.get("track_deposit_addresses", True)),
        deposit_memory_hours=float(raw.get("deposit_memory_hours", 72)),
        known_addresses={norm(k): v for k, v in (raw.get("known_addresses") or {}).items()},
        auto_token_chains=auto_chains,
        exclude_symbols=exclude_symbols,
        exclude_addresses={norm(a) for a in (auto.get("exclude_addresses") or [])},
        exclude_majors=bool(auto.get("exclude_majors", True)),
        exclude_pegged=bool(auto.get("exclude_stablecoins", True)),
        min_price_confidence=float(auto.get("min_price_confidence", 0.8)),
        learn_deposit_addresses=bool(raw.get("learn_deposit_addresses", True)),
        deposit_lookback_blocks=int(raw.get("deposit_lookback_blocks", 5000)),
        **(extra(raw) if extra else {}),
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
        for addr, entity in self.cfg.hypercore_exchange_addresses.items():
            self.book.add(addr, entity, family="hypercore")
        if not len(self.book):
            raise ValueError("exchange flows bot: exchange address book is empty - run scripts/update_labels.py")
        self._eoa: dict[str, bool] = {}

    def _auto(self, chain: ChainConfig) -> bool:
        return chain.kind == "evm" and chain.name in self.cfg.auto_token_chains

    def poll_mode(self, chain: ChainConfig) -> str:
        # Tron: every transfer of the tracked TRC-20s, exchanges matched locally (like EVM).
        # Solana / HyperCore have no such cheap query -> poll the exchange wallets themselves.
        return "tokens" if chain.kind == "tron" else "accounts"

    def watch_addresses(self, chain: ChainConfig) -> set[str]:
        return self.book.addresses_for(chain.kind)

    def log_filters(self, chain: ChainConfig) -> list[dict]:
        if self._auto(chain):
            # Every ERC-20 Transfer on the chain; exchanges / deposit addresses are matched locally.
            return [{"address": None, "topics": [TRANSFER_TOPIC]}]
        # One query returns every Transfer of every tracked token on the chain.
        tokens = [t.address for t in self.cfg.tokens_for(chain.name)]
        return [{"address": part, "topics": [TRANSFER_TOPIC]} for part in chunked(tokens, 50)]

    def tracked_token_keys(self, chain: ChainConfig, transfers) -> set[str]:
        if not self._auto(chain):
            return super().tracked_token_keys(chain, transfers)
        deps = set(self._deposits(chain.name))
        # addresses that sweep to an exchange in this very batch may be learned during handle()
        deps |= {t.from_addr for t in transfers if t.to_addr in self.book and t.from_addr not in self.book}
        return {
            t.token for t in transfers
            if t.from_addr in self.book or t.to_addr in self.book or t.to_addr in deps
        }

    def _min_usd(self, chain: str, token: str) -> float:
        for t in self.cfg.tokens_for(chain):
            if t.address == token and t.min_usd is not None:
                return t.min_usd
        return self.cfg.min_usd

    # auto-token filters ---------------------------------------------------------------
    def _excluded(self, chain: str, token: str, symbol: str | None, price: float | None) -> bool:
        if token in self.cfg.exclude_addresses:
            return True
        if any(t.address == token for t in self.cfg.tokens_for(chain)):
            return False  # explicitly listed by the user
        sym = (symbol or "").upper().strip()
        if sym in self.cfg.exclude_symbols:
            return True
        if self.cfg.exclude_majors and _MAJOR_RE.match(sym):
            return True
        if self.cfg.exclude_pegged and (_PEGGED_RE.search(sym) or (price and 0.98 <= price <= 1.02)):
            return True
        return False

    def _trusted_price(self, chain: str, token: str, v: Valued) -> bool:
        if v.usd is None:
            return False
        info = self.oracle.get(chain, token)
        return info.confidence is None or info.confidence >= self.cfg.min_price_confidence

    # learned deposit addresses (persistent) ----------------------------------------------
    def _deposits(self, chain: str) -> dict[str, str]:
        return self.state.extra.setdefault("deposit_addrs", {}).setdefault(chain, {})

    def _is_eoa(self, chain: ChainConfig, addr: str) -> bool:
        if addr not in self._eoa:
            res = self.rpcs[chain.name].is_contract(addr)
            if res is None:
                return False  # RPC error: don't learn, don't cache
            self._eoa[addr] = not res
        return self._eoa[addr]

    def _lookback_origin(self, chain: ChainConfig, tr) -> tuple[str, str] | None:
        """Who sent this token to the deposit address before its first sweep."""
        try:
            logs, _ = self.rpcs[chain.name].get_logs(
                max(0, tr.block - self.cfg.deposit_lookback_blocks), tr.block,
                address=[tr.token], topics=[TRANSFER_TOPIC, None, address_topic(tr.from_addr)],
                chunk=self.cfg.deposit_lookback_blocks + 1)
        except Exception:  # noqa: BLE001 - best effort
            return None
        best: dict[str, list] = {}
        for lg in logs:
            sender = "0x" + lg["topics"][1][-40:].lower()
            if sender in self.book:
                continue
            amt = int(lg.get("data") or "0x0", 16)
            cur = best.setdefault(sender, [0, lg["transactionHash"]])
            cur[0] += amt
        if not best:
            return None
        sender = max(best, key=lambda s: best[s][0])
        return sender, best[sender][1]

    # deposit-address memory (classic mode) -------------------------------------------
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
        special = special_label(chain, addr)
        if special and not label:
            return special
        link = f'<a href="{chain.address_url(addr)}">{short(addr)}</a>'
        if label:
            return f"🏦 <b>{esc(label.entity)}</b> [{esc(label.name)}] ({link})"
        if addr in self.cfg.known_addresses:
            return f"<b>{esc(self.cfg.known_addresses[addr])}</b> ({link})"
        return link

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        alerts = []
        auto = self._auto(chain)
        deps = self._deposits(chain.name) if auto else {}
        for v in transfers:
            tr = v.transfer
            src, dst = self.book.get(tr.from_addr), self.book.get(tr.to_addr)
            trusted = self._trusted_price(chain.name, tr.token, v) if auto else True
            dep_dst: Label | None = None

            if auto:
                if not src and not dst:
                    entity = deps.get(tr.to_addr)
                    if not entity or tr.from_addr in deps:
                        continue
                    dep_dst = Label(entity=entity, name="депозитный адрес")
                elif dst and not src and tr.from_addr in deps:
                    continue  # sweep from a known deposit address: alerted at deposit time
                elif src and not dst and tr.to_addr in deps:
                    continue  # exchange tops up gas on its deposit address
                if not trusted:
                    continue  # unpriced / thin-pool token: likely spam
            learned = False
            if (auto and dst and not src and self.cfg.learn_deposit_addresses
                    and self._is_eoa(chain, tr.from_addr)):
                deps[tr.from_addr] = dst.entity  # new deposit address, remembered permanently
                learned = True

            threshold = self._min_usd(chain.name, tr.token)
            big = v.usd is not None and v.usd >= threshold
            if not src and not dst and not dep_dst:
                if big and self.cfg.track_deposit_addresses:
                    self._remember(v)
                continue
            if not big:
                continue
            if auto:
                price = v.usd / v.amount if v.amount else None
                if self._excluded(chain.name, tr.token, v.symbol, price):
                    continue
            origin = None
            if dep_dst:
                head = f"📥 <b>ЗАВОД на биржу {esc(dep_dst.entity)}</b>"
                dst = dep_dst
            elif src and dst:
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
                if origin is None and learned:
                    origin = self._lookback_origin(chain, tr)
            else:
                head = f"📤 <b>ВЫВОД с биржи {esc(src.entity)}</b>"
            text = (
                f"{head}\n\n"
                f"💰 <b>{fmt_amount(v.amount)} {esc(v.symbol)}</b> (~<b>{fmt_usd(v.usd)}</b>)\n"
                f"{where(chain, tr)}\n"
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
