"""Bot #3: withdrawals from centralized exchanges to FRESH wallets.

A fresh wallet is an EOA (not a contract) with at most ``max_nonce`` outgoing
transactions at the moment of the check (default 0 - never sent anything).
Large withdrawals to such wallets often mean a new wallet created for
accumulation / a team or insider moving funds off the exchange.

Independent from the other bots: own Telegram bot/chat, config and state.
Token selection, exclusions (stables, ETH/BTC/BNB/SOL + wrappers, gold), the
anti-spam price-confidence check and the CEX address book are shared with the
exchange flows bot (``auto_tokens`` / ``tokens`` in the config).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..common.config import ChainConfig
from ..common.labels import AddressBook
from ..common.runner import Alert, Valued
from ..common.telegram import esc, fmt_amount, fmt_usd, where
from ..exchange_flows_bot.bot import ExchangeBotConfig, ExchangeFlowsAlerter
from ..exchange_flows_bot.bot import load_config as _load_exchange_config


@dataclass
class FreshBotConfig(ExchangeBotConfig):
    max_nonce: int = 0   # outgoing txs still counted as "fresh"


def load_config(path: str, dry_run: bool = False) -> FreshBotConfig:
    return _load_exchange_config(
        path, dry_run, cls=FreshBotConfig, bot_name="fresh wallets bot",
        defaults={"min_usd": 30_000, "state_file": "state/fresh_wallets.json"},
        extra=lambda raw: {"max_nonce": int((raw.get("fresh") or {}).get("max_nonce", 0))},
    )


class FreshWalletsAlerter(ExchangeFlowsAlerter):
    name = "fresh-wallets"
    cfg: FreshBotConfig

    def __init__(self, *args, book: AddressBook | None = None, **kwargs):
        super().__init__(*args, book=book, **kwargs)
        for chain in self.cfg.chains.values():
            if chain.kind != "evm":
                raise ValueError(f"fresh wallets bot: only EVM chains are supported, got '{chain.name}'")

    def _nonce_if_fresh(self, chain: ChainConfig, addr: str) -> int | None:
        rpc = self.rpcs[chain.name]
        try:
            nonce = int(rpc.call("eth_getTransactionCount", [addr, "latest"]), 16)
        except Exception:  # noqa: BLE001
            return None
        if nonce > self.cfg.max_nonce:
            return None
        if rpc.is_contract(addr) is not False:  # contract or RPC error
            return None
        return nonce

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        alerts = []
        auto = self._auto(chain)
        deps = self._deposits(chain.name)
        for v in transfers:
            tr = v.transfer
            src, dst = self.book.get(tr.from_addr), self.book.get(tr.to_addr)
            trusted = self._trusted_price(chain.name, tr.token, v) if auto else v.usd is not None
            if not trusted:
                continue
            # learn deposit addresses (sweep EOA -> exchange) so they are never taken for fresh wallets
            if dst and not src:
                if tr.from_addr not in deps and self.cfg.learn_deposit_addresses \
                        and self._is_eoa(chain, tr.from_addr):
                    deps[tr.from_addr] = dst.entity
                continue
            if not src or dst or tr.to_addr in deps:
                continue  # only exchange -> outside world
            if v.usd < self._min_usd(chain.name, tr.token):
                continue
            if auto and self._excluded(chain.name, tr.token, v.symbol, v.usd / v.amount if v.amount else None):
                continue
            nonce = self._nonce_if_fresh(chain, tr.to_addr)
            if nonce is None:
                continue
            txs = "ни одной исходящей tx" if nonce == 0 else f"{nonce} исходящих tx"
            text = (
                f"🆕 <b>ВЫВОД НА ФРЕШ-КОШЕЛЁК</b>\n\n"
                f"💰 <b>{fmt_amount(v.amount)} {esc(v.symbol)}</b> (~<b>{fmt_usd(v.usd)}</b>)\n"
                f"{where(chain, tr)}\n"
                f"С биржи: {self._who(chain, tr.from_addr, src)}\n"
                f"Кому: {self._who(chain, tr.to_addr, None)} ({txs})\n"
                f'🔗 <a href="{chain.tx_url(tr.tx_hash)}">Транзакция</a>'
            )
            alerts.append(Alert(key=f"fresh:{tr.key}", text=text))
        return alerts
