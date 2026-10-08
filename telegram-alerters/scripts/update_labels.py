#!/usr/bin/env python3
"""Build data/cex_addresses.json from open-source GitHub datasets (no API keys).

Sources
  1. duneanalytics/spellbook  - curated CEX wallets for all EVM chains (primary)
  2. brianleect/etherscan-labels - public explorer name tags (ETH, BSC, Polygon,
     Arbitrum, Optimism, Avalanche, Fantom); only strict "<Exchange> <N>" /
     "<Exchange>: Hot Wallet" style names are taken to avoid token contracts,
     deployers etc.
  3. data/cex_addresses_custom.json - your own additions (optional, never overwritten)

Usage:  python scripts/update_labels.py [--out data/cex_addresses.json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alerters.common.addr import is_solana, is_tron  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RAW = "https://raw.githubusercontent.com"
DUNE_URL = f"{RAW}/duneanalytics/spellbook/main/dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/cex_evms_addresses.sql"
ETHERSCAN_LABELS = {
    chain: f"{RAW}/brianleect/etherscan-labels/main/data/{chain}/combined/combinedAllLabels.json"
    for chain in ("etherscan", "bscscan", "polygonscan", "arbiscan", "optimism", "avalanche", "ftmscan")
}
CUSTOM_FILE = ROOT / "data" / "cex_addresses_custom.json"

# Old explorer names -> current entity names used by Dune.
ALIASES = {
    "okex": "OKX",
    "okx": "OKX",
    "huobi": "HTX",
    "gate.io": "Gate.io",
    "gate": "Gate.io",
    "crypto.com": "Crypto.com",
    "kucoin": "KuCoin",
    "mexc": "MEXC",
    "bybit": "Bybit",
    "bitget": "Bitget",
    "coinbase": "Coinbase",
    "binance": "Binance",
    "kraken": "Kraken",
    "bitfinex": "Bitfinex",
    "gemini": "Gemini",
    "bitstamp": "Bitstamp",
    "upbit": "Upbit",
    "bithumb": "Bithumb",
    "bitmart": "BitMart",
    "poloniex": "Poloniex",
    "hitbtc": "HitBTC",
    "bitvavo": "Bitvavo",
    "lbank": "LBank",
    "whitebit": "WhiteBIT",
    "bingx": "BingX",
    "coinex": "CoinEx",
    "htx": "HTX",
    "robinhood": "Robinhood",
    "deribit": "Deribit",
}

# Entries that are contracts / service wallets rather than deposit/withdrawal
# wallets (token contracts, DEX routers, deployers, staking...). Transfers to
# them are not "exchange flows" and would only produce false alerts.
NON_FLOW_RE = re.compile(
    r"\b(token|tokens|deployer|router|proxy|contract|gas supplier|staking|unstaking|"
    r"mining pool|pool|bridge|factory|token sale|aggregation|forwarder)\b",
    re.I,
)

DUNE_NON_EVM = {
    fam: f"{RAW}/duneanalytics/spellbook/main/dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/{fam}/cex_{fam}_addresses.sql"
    for fam in ("tron", "solana")
}
NON_EVM_ROW_RE = re.compile(r"\(\s*'(tron|solana)'\s*,\s*'([^']+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'")
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"

ROW_RE = re.compile(r"\(\s*(0x[0-9a-fA-F]{40})\s*,\s*'([^']*)'\s*,\s*'([^']*)'")


def fetch(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.text


def from_dune() -> dict[str, list[str]]:
    rows = ROW_RE.findall(fetch(DUNE_URL))
    return {addr.lower(): [entity, name] for addr, entity, name in rows if not NON_FLOW_RE.search(name)}


def _strict_name_re(entities: list[str]) -> re.Pattern:
    names = "|".join(re.escape(e) for e in sorted(entities, key=len, reverse=True))
    return re.compile(
        rf"^(?P<ex>{names})(?:\.com|\.io)?(?:\s*\d+|:?\s*(?:hot|cold)\s*wallet\s*\d*|:\s*deposit\s*\d*|\s*\d*)$",
        re.I,
    )


def from_etherscan_labels(entities: list[str]) -> dict[str, list[str]]:
    pattern = _strict_name_re(list(ALIASES) + entities)
    canonical = {e.lower(): e for e in entities}
    canonical.update(ALIASES)
    out: dict[str, list[str]] = {}
    for chain, url in ETHERSCAN_LABELS.items():
        try:
            data = json.loads(fetch(url))
        except (requests.RequestException, ValueError) as exc:
            print(f"warn: {chain}: {exc}", file=sys.stderr)
            continue
        for addr, item in data.items():
            name = (item.get("name") or "").strip()
            m = pattern.match(name)
            if m and not NON_FLOW_RE.search(name):
                out[addr.lower()] = [canonical.get(m.group("ex").lower(), m.group("ex")), name]
    return out


def from_dune_non_evm() -> dict[str, list[str]]:
    """Tron / Solana exchange wallets (base58, validated)."""
    out: dict[str, list[str]] = {}
    for fam, url in DUNE_NON_EVM.items():
        try:
            text = fetch(url)
        except requests.RequestException as exc:
            print(f"warn: dune {fam}: {exc}", file=sys.stderr)
            continue
        for chain, addr, entity, name in NON_EVM_ROW_RE.findall(text):
            ok = is_tron(addr) if chain == "tron" else is_solana(addr) and not addr.startswith("0x")
            if ok and not NON_FLOW_RE.search(name):
                out[addr] = [entity, name, chain]
    return out


def hypercore_active(addresses: list[str]) -> list[str]:
    """EVM exchange wallets that hold spot balances on Hyperliquid HyperCore (~7 min for 4k addresses)."""
    import time

    active, last = [], 0.0
    for i, addr in enumerate(addresses):
        time.sleep(max(0.0, 0.11 - (time.time() - last)))
        last = time.time()
        try:
            resp = requests.post(HYPERLIQUID_INFO, json={"type": "spotClearinghouseState", "user": addr}, timeout=20)
            if resp.status_code == 429:
                time.sleep(10)
                continue
            if any(float(b.get("total") or 0) > 0 for b in (resp.json() or {}).get("balances", [])):
                active.append(addr)
        except (requests.RequestException, ValueError):
            continue
        if i % 500 == 0:
            print(f"hypercore probe {i}/{len(addresses)}: {len(active)} active", file=sys.stderr)
    return active


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "cex_addresses.json"))
    ap.add_argument("--hypercore", action="store_true",
                    help="probe which EVM exchange wallets are active on Hyperliquid HyperCore (slow)")
    args = ap.parse_args()

    dune = from_dune()
    entities = sorted({v[0] for v in dune.values()})
    etherscan = from_etherscan_labels(entities)
    custom = json.loads(CUSTOM_FILE.read_text()) if CUSTOM_FILE.exists() else {}

    merged: dict[str, list[str]] = {}
    merged.update({a: v for a, v in etherscan.items()})
    merged.update(dune)  # Dune is curated -> wins over explorer tags
    non_evm = from_dune_non_evm()
    merged.update(non_evm)
    for addr, v in custom.items():  # custom wins over everything
        key = addr.lower() if addr.startswith("0x") else addr
        merged[key] = v if isinstance(v, list) else [v, v]

    previous = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else {}
    if args.hypercore:
        hypercore = hypercore_active([a for a in merged if a.startswith("0x")])
    else:
        hypercore = [a for a in previous.get("hypercore", []) if a in merged]

    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "sources": [DUNE_URL, *DUNE_NON_EVM.values(), *ETHERSCAN_LABELS.values(), "data/cex_addresses_custom.json"],
        "counts": {"dune": len(dune), "dune_tron_solana": len(non_evm), "etherscan_labels": len(etherscan),
                   "custom": len(custom), "hypercore_active": len(hypercore), "total": len(merged)},
        "addresses": dict(sorted(merged.items())),
        "hypercore": sorted(hypercore),
    }
    Path(args.out).write_text(json.dumps(result, indent=0, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result["counts"]))


if __name__ == "__main__":
    main()
