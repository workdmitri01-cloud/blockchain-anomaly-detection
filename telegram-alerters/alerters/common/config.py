"""YAML config loading with ``${ENV_VAR}`` substitution and chain defaults."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAINS_FILE = PACKAGE_ROOT / "config" / "chains.yaml"

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _substitute_env(value: Any) -> Any:
    """Recursively replace ``${VAR}`` / ``${VAR:-default}`` in strings."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    return value


def load_yaml(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return _substitute_env(data)


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    rpc_urls: list[str]
    explorer: str
    defillama: str
    coingecko: str | None = None
    confirmations: int = 2
    log_chunk: int = 2000
    initial_lookback: int = 50
    max_blocks_per_run: int = 20000
    # Explorers for team-wallet auto-discovery.
    blockscout: str | None = None
    etherscan_api: str | None = None

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer.rstrip('/')}/tx/{tx_hash}"

    def address_url(self, address: str) -> str:
        return f"{self.explorer.rstrip('/')}/address/{address}"


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    thread_id: int | None = None
    disable_preview: bool = True


@dataclass
class TokenConfig:
    chain: str
    address: str
    symbol: str | None = None
    decimals: int | None = None
    # Fixed price (e.g. 1.0 for stablecoins or a token without a market).
    price_usd: float | None = None
    # Per-token override of the bot-wide USD threshold.
    min_usd: float | None = None


@dataclass
class BaseBotConfig:
    telegram: TelegramConfig
    chains: dict[str, ChainConfig]
    tokens: list[TokenConfig] = field(default_factory=list)
    state_file: str = "state.json"
    poll_interval: int = 20
    labels_file: str | None = None

    def tokens_for(self, chain: str) -> list[TokenConfig]:
        return [t for t in self.tokens if t.chain == chain]


def load_chains(overrides: dict | None, used: set[str], path: Path = DEFAULT_CHAINS_FILE) -> dict[str, ChainConfig]:
    """Load default chain definitions and apply per-bot overrides.

    Only chains actually referenced by the bot (``used``) are returned, so a bot
    watching only Ethereum never touches BSC RPCs.
    """
    defaults = load_yaml(path)
    overrides = overrides or {}
    chains: dict[str, ChainConfig] = {}
    for name in sorted(used):
        base = dict(defaults.get(name) or {})
        extra = dict(overrides.get(name) or {})
        if not base and not extra:
            raise ValueError(f"Unknown chain '{name}': add it to config/chains.yaml or the bot config 'chains' section")
        # Custom RPCs (e.g. paid/keyed Alchemy, Infura, own node) go first,
        # public ones stay as a fallback.
        rpc = [u for u in (extra.pop("rpc_urls", None) or []) if u]
        rpc += [u for u in base.get("rpc_urls", []) if u and u not in rpc]
        base.update(extra)
        base["rpc_urls"] = rpc
        if not rpc:
            raise ValueError(f"Chain '{name}' has no RPC URLs")
        chains[name] = ChainConfig(name=name, **base)
    return chains


def parse_telegram(raw: dict, prefix: str) -> TelegramConfig:
    raw = raw or {}
    token = raw.get("bot_token") or ""
    chat = str(raw.get("chat_id") or "")
    if not token or not chat:
        raise ValueError(
            f"Telegram bot_token/chat_id are empty for {prefix}: set them in the config or env variables"
        )
    thread = raw.get("thread_id")
    return TelegramConfig(
        bot_token=token,
        chat_id=chat,
        thread_id=int(thread) if thread not in (None, "") else None,
        disable_preview=bool(raw.get("disable_preview", True)),
    )


def parse_tokens(raw: list | None) -> list[TokenConfig]:
    tokens = []
    for item in raw or []:
        tokens.append(
            TokenConfig(
                chain=item["chain"],
                address=item["address"].lower(),
                symbol=item.get("symbol"),
                decimals=item.get("decimals"),
                price_usd=item.get("price_usd"),
                min_usd=item.get("min_usd"),
            )
        )
    return tokens
