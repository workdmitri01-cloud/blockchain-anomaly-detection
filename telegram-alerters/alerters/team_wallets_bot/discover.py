"""Preview auto-discovered team wallets without Telegram:

    python -m alerters.team_wallets_bot.discover -c config/team_wallets.yaml
    python -m alerters.team_wallets_bot.discover --chain ethereum --token 0x...

Prints the scoring and a ready-to-paste `wallets:` YAML block.
"""
from __future__ import annotations

import argparse
import logging

from ..common.config import load_chains
from ..common.addr import norm
from ..common.explorer import explorer_for
from ..common.labels import AddressBook
from ..common.runner import make_client
from .discovery import DiscoveryParams, discover


def main() -> None:
    ap = argparse.ArgumentParser(description="Find team wallets of a token from public on-chain data")
    ap.add_argument("-c", "--config", help="team wallets bot config (uses its tokens and auto_discovery params)")
    ap.add_argument("--chain", help="chain name from config/chains.yaml")
    ap.add_argument("--token", help="token contract address")
    ap.add_argument("--min-score", type=float)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")

    if args.config:
        from .bot import load_config

        cfg = load_config(args.config, dry_run=True)
        targets = [(t.chain, t.address, t.symbol) for t in cfg.tokens]
        chains, ad, book_file = cfg.chains, cfg.auto_discovery, cfg.labels_file
        params = DiscoveryParams(min_score=ad.min_score, min_share_pct=ad.min_share_pct,
                                 top_holders=ad.top_holders, max_depth=ad.max_depth)
    else:
        if not (args.chain and args.token):
            ap.error("give -c CONFIG or --chain and --token")
        targets = [(args.chain, norm(args.token), None)]
        chains, params, book_file = load_chains(None, {args.chain}), DiscoveryParams(), None
    if args.min_score is not None:
        params.min_score = args.min_score
    book = AddressBook.from_file(book_file)

    for chain, token, symbol in targets:
        c = chains[chain]
        client = make_client(c)
        team, low = discover(chain, token, symbol, client, explorer_for(c, client), book, params)
        print(f"\n=== {symbol or token} on {chain} ===")
        for title, items in (("TEAM (будут отслеживаться)", team), ("низкая уверенность", low)):
            print(f"-- {title}: {len(items)}")
            for cand in items:
                print(f"  {cand.address}  score={cand.score:g}  {cand.info.name if cand.info and cand.info.name else ''}")
                for r in cand.reasons:
                    print(f"      - {r}")
        if team:
            print("\n# wallets:")
            for cand in team:
                print(f'#  - name: "{cand.title}"\n#    address: "{cand.address}"\n#    chains: [{chain}]')


if __name__ == "__main__":
    main()
