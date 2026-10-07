"""Shared polling loop: block ranges -> eth_getLogs -> Transfer -> bot.handle() -> Telegram."""
from __future__ import annotations

import argparse
import logging
import signal
import time
from dataclasses import dataclass

from .config import BaseBotConfig, ChainConfig
from .evm import EvmRpc, Transfer, decode_transfer
from .prices import PriceOracle
from .state import State
from .telegram import TelegramSender

log = logging.getLogger(__name__)


@dataclass
class Alert:
    key: str
    text: str


@dataclass
class Valued:
    transfer: Transfer
    symbol: str
    amount: float
    usd: float | None


class BaseAlerter:
    """Subclasses implement :meth:`log_filters` and :meth:`handle`."""

    name = "alerter"

    def __init__(self, cfg: BaseBotConfig, sender: TelegramSender, state: State,
                 oracle: PriceOracle | None = None, rpcs: dict[str, EvmRpc] | None = None):
        self.cfg = cfg
        self.sender = sender
        self.state = state
        self.oracle = oracle or PriceOracle()
        self.rpcs = rpcs or {name: EvmRpc(c.rpc_urls) for name, c in cfg.chains.items()}
        self._stop = False
        for t in cfg.tokens:
            self.oracle.set_metadata(t.chain, t.address, t.symbol, t.decimals)
            if t.price_usd is not None:
                self.oracle.set_fixed_price(t.chain, t.address, t.price_usd)
        # Token metadata read from chain is cached in the state file.
        for key, (sym, dec) in self.state.extra.get("meta", {}).items():
            chain, token = key.split(":", 1)
            self.oracle.set_metadata(chain, token, sym, dec)

    # --- to be implemented by bots -------------------------------------------------
    def log_filters(self, chain: ChainConfig) -> list[dict]:
        """List of {"address": ..., "topics": [...]} eth_getLogs filters."""
        raise NotImplementedError

    def handle(self, chain: ChainConfig, transfers: list[Valued]) -> list[Alert]:
        raise NotImplementedError

    def before_scan(self) -> None:
        """Hook called at the start of every polling cycle (e.g. periodic discovery)."""

    def tracked_token_keys(self, chain: ChainConfig, transfers: list[Transfer]) -> set[str]:
        """Tokens that need price/metadata before handle(); default: all seen."""
        return {t.token for t in transfers}

    # --- machinery -------------------------------------------------------------------
    def value(self, chain: ChainConfig, transfers: list[Transfer]) -> list[Valued]:
        tokens = self.tracked_token_keys(chain, transfers)
        if not tokens:
            return []
        self.oracle.refresh({f"{chain.name}:{t}": (chain.defillama, chain.coingecko) for t in tokens})
        out = []
        for tr in transfers:
            if tr.token not in tokens:
                continue
            info = self.oracle.get(chain.name, tr.token)
            if info.decimals is None:
                sym, dec = self.rpcs[chain.name].erc20_metadata(tr.token)
                self.oracle.set_metadata(chain.name, tr.token, sym, dec if dec is not None else 18)
                self.state.extra.setdefault("meta", {})[f"{chain.name}:{tr.token}"] = [info.symbol, info.decimals]
            amount = tr.raw_amount / (10 ** info.decimals)
            usd = amount * info.price if info.price else None
            out.append(Valued(tr, info.symbol or tr.token[:8], amount, usd))
        return out

    def fetch_transfers(self, chain: ChainConfig, start: int, end: int) -> list[Transfer]:
        rpc = self.rpcs[chain.name]
        chunk = self.state.chunk.get(chain.name, chain.log_chunk)
        seen: dict[str, Transfer] = {}
        for flt in self.log_filters(chain):
            logs, chunk = rpc.get_logs(start, end, address=flt.get("address"), topics=flt.get("topics"), chunk=chunk)
            for entry in logs:
                tr = decode_transfer(chain.name, entry)
                if tr and tr.raw_amount > 0:
                    seen[tr.key] = tr
        self.state.chunk[chain.name] = chunk
        return sorted(seen.values(), key=lambda t: (t.block, t.log_index))

    def scan_chain(self, chain: ChainConfig) -> bool:
        """Scan the next block range. Returns True if the chain is caught up."""
        rpc = self.rpcs[chain.name]
        head = rpc.block_number() - chain.confirmations
        last = self.state.last_block.get(chain.name)
        if last is None:
            last = head - chain.initial_lookback
            log.info("[%s] %s: first run, starting from block %d", self.name, chain.name, last + 1)
        if head <= last:
            return True
        end = min(head, last + chain.max_blocks_per_run)
        transfers = self.fetch_transfers(chain, last + 1, end)
        alerts = self.handle(chain, self.value(chain, transfers)) if transfers else []
        for alert in alerts:
            if self.state.was_sent(alert.key):
                continue
            self.sender.send(alert.text)
            self.state.mark_sent(alert.key)
            self.state.save()
        self.state.last_block[chain.name] = end
        self.state.save()
        log.info("[%s] %s: blocks %d-%d, %d transfers, %d alerts",
                 self.name, chain.name, last + 1, end, len(transfers), len(alerts))
        return end >= head

    def run_once(self, max_runtime: float = 240) -> None:
        """Catch every chain up to head (bounded by max_runtime). Used by cron / GitHub Actions."""
        deadline = time.time() + max_runtime
        try:
            self.before_scan()
        except Exception:  # noqa: BLE001
            log.exception("[%s] before_scan failed", self.name)
        pending = list(self.cfg.chains.values())
        while pending and time.time() < deadline and not self._stop:
            still = []
            for chain in pending:
                try:
                    if not self.scan_chain(chain):
                        still.append(chain)
                except Exception:  # keep other chains running
                    log.exception("[%s] %s: scan failed", self.name, chain.name)
            pending = still

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "_stop", True))
        log.info("[%s] started, chains: %s", self.name, ", ".join(self.cfg.chains))
        while not self._stop:
            started = time.time()
            self.run_once(max_runtime=max(60, self.cfg.poll_interval * 5))
            sleep = self.cfg.poll_interval - (time.time() - started)
            while sleep > 0 and not self._stop:
                time.sleep(min(sleep, 1))
                sleep -= 1


def cli_args(description: str, default_config: str) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("-c", "--config", default=default_config, help="path to YAML config")
    p.add_argument("--once", action="store_true", help="catch up once and exit (cron / GitHub Actions)")
    p.add_argument("--max-runtime", type=float, default=240, help="seconds limit for --once")
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending to Telegram")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return args
