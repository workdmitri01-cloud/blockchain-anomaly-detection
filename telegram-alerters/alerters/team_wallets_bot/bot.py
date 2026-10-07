"""Bot #1: ERC-20 transfers to / from team wallets."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..common.config import (
    BaseBotConfig,
    ChainConfig,
    load_chains,
    load_yaml,
    parse_telegram,
    parse_tokens,
    TelegramConfig,
)
from ..common.evm import TRANSFER_TOPIC, address_topic, chunked
from ..common.labels import AddressBook
from ..common.runner import Alert, BaseAlerter, Valued
from ..common.telegram import esc, fmt_amount, fmt_usd, short


@dataclass
class TeamWallet:
    address: str
    name: str
    chains: list[str] | None = None  # None = every enabled chain


@dataclass
class TeamBotConfig(BaseBotConfig):
    wallets: list[TeamWallet] = field(default_factory=list)
    # true: alert on ANY ERC-20 moving through team wallets, not only `tokens`.
    track_all_tokens: bool = False
    min_usd: float = 0.0
    # With track_all_tokens, skip tokens without a market price (airdrop spam).
    skip_unpriced: bool = True
    # Transfers between two team wallets.
    alert_internal: bool = True
    known_addresses: dict[str, str] = field(default_factory=dict)

    def wallets_for(self, chain: str) -> list[TeamWallet]:
        return [w for w in self.wallets if w.chains is None or chain in w.chains]


def load_config(path: str, dry_run: bool = False) -> TeamBotConfig:
    raw = load_yaml(path)
    tokens = parse_tokens(raw.get("tokens"))
    wallets = []
    for w in raw.get("wallets") or []:
        chains = w.get("chains") or w.get("chain")
        if isinstance(chains, str):
            chains = None if chains in ("*", "all") else [chains]
        wallets.append(TeamWallet(address=w["address"].lower(), name=w.get("name") or short(w["address"]), chains=chains))
    if not wallets:
        raise ValueError("team wallets bot: 'wallets' list is empty")
    networks = set(raw.get("networks") or [])
    networks |= {t.chain for t in tokens}
    for w in wallets:
        networks |= set(w.chains or [])
    if not networks:
        raise ValueError("team wallets bot: no networks - set 'networks' or token/wallet chains")
    telegram = (
        TelegramConfig(bot_token="", chat_id="")
        if dry_run and not (raw.get("telegram") or {}).get("bot_token")
        else parse_telegram(raw.get("telegram"), "team wallets bot")
    )
    cfg = TeamBotConfig(
        telegram=telegram,
        chains=load_chains(raw.get("chains"), networks),
        tokens=tokens,
        state_file=raw.get("state_file", "state/team_wallets.json"),
        poll_interval=int(raw.get("poll_interval", 20)),
        labels_file=raw.get("labels_file"),
        wallets=wallets,
        track_all_tokens=bool(raw.get("track_all_tokens", False)),
        min_usd=float(raw.get("min_usd", 0) or 0),
        skip_unpriced=bool(raw.get("skip_unpriced", True)),
        alert_internal=bool(raw.get("alert_internal", True)),
        known_addresses={k.lower(): v for k, v in (raw.get("known_addresses") or {}).items()},
    )
    if not cfg.track_all_tokens and not cfg.tokens:
        raise ValueError("team wallets bot: set 'tokens' or enable 'track_all_tokens'")
    return cfg


class TeamWalletsAlerter(BaseAlerter):
    name = "team-wallets"
    cfg: TeamBotConfig

    def __init__(self, *args, book: AddressBook | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.book = book if book is not None else AddressBook.from_file(self.cfg.labels_file)

    def log_filters(self, chain: ChainConfig) -> list[dict]:
        wallets = [address_topic(w.address) for w in self.cfg.wallets_for(chain.name)]
        tokens = [t.address for t in self.cfg.tokens_for(chain.name)]
        if not wallets or (not tokens and not self.cfg.track_all_tokens):
            return []
        address = None if self.cfg.track_all_tokens else tokens
        filters = []
        # Topic positions are AND-ed, so "from OR to" needs two queries.
        for part in chunked(wallets, 100):
            filters.append({"address": address, "topics": [TRANSFER_TOPIC, part]})
            filters.append({"address": address, "topics": [TRANSFER_TOPIC, None, part]})
        return filters

    def _min_usd(self, chain: str, token: str) -> float:
        for t in self.cfg.tokens_for(chain):
            if t.address == token and t.min_usd is not None:
                return t.min_usd
        return self.cfg.min_usd

    def describe(self, chain: ChainConfig, addr: str, team: dict[str, TeamWallet]) -> str:
        link = f'<a href="{chain.address_url(addr)}">{short(addr)}</a>'
        if addr in team:
            return f"👥 <b>{esc(team[addr].name)}</b> ({link})"
        if addr in self.cfg.known_addresses:
            return f"{esc(self.cfg.known_addresses[addr])} ({link})"
        label = self.book.get(addr)
        if label:
            return f"🏦 <b>{esc(label.entity)}</b> [{esc(label.name)}] ({link})"
        if addr == "0x" + "0" * 40:
            return "🪙 mint / burn (0x0)"
        return link

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        team = {w.address: w for w in self.cfg.wallets_for(chain.name)}
        configured = {t.address for t in self.cfg.tokens_for(chain.name)}
        alerts = []
        for v in transfers:
            tr = v.transfer
            src, dst = tr.from_addr in team, tr.to_addr in team
            if not (src or dst):
                continue
            if src and dst and not self.cfg.alert_internal:
                continue
            if tr.token not in configured:
                if not self.cfg.track_all_tokens:
                    continue
                if v.usd is None and self.cfg.skip_unpriced:
                    continue
            min_usd = self._min_usd(chain.name, tr.token)
            if min_usd and v.usd is not None and v.usd < min_usd:
                continue
            if src and dst:
                head = "🔁 <b>Перевод между командными кошельками</b>"
            elif src:
                head = "🔴 <b>Исходящий перевод с командного кошелька</b>"
            else:
                head = "🟢 <b>Входящий перевод на командный кошелёк</b>"
            counter = tr.to_addr if src else tr.from_addr
            cex = self.book.get(counter) if counter not in team else None
            if cex:
                head += f"\n⚠️ {'Депозит на биржу' if src else 'Вывод с биржи'} <b>{esc(cex.entity)}</b>"
            text = (
                f"{head}\n\n"
                f"💰 <b>{fmt_amount(v.amount)} {esc(v.symbol)}</b> (~{fmt_usd(v.usd)})\n"
                f"⛓ {esc(chain.name)} · блок {tr.block}\n"
                f"От: {self.describe(chain, tr.from_addr, team)}\n"
                f"Кому: {self.describe(chain, tr.to_addr, team)}\n"
                f'🔗 <a href="{chain.tx_url(tr.tx_hash)}">Транзакция</a>'
            )
            alerts.append(Alert(key=f"team:{tr.key}", text=text))
        return alerts
