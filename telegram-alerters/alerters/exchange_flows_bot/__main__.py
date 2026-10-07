"""python -m alerters.exchange_flows_bot -c config/exchange_flows.yaml [--once] [--dry-run]"""
from ..common.runner import cli_args
from ..common.state import State
from ..common.telegram import TelegramSender
from .bot import ExchangeFlowsAlerter, load_config


def main() -> None:
    args = cli_args("Telegram alerts for large CEX token deposits / withdrawals", "config/exchange_flows.yaml")
    cfg = load_config(args.config, dry_run=args.dry_run)
    bot = ExchangeFlowsAlerter(cfg, TelegramSender(cfg.telegram, dry_run=args.dry_run), State(cfg.state_file))
    if args.once:
        bot.run_once(max_runtime=args.max_runtime)
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
