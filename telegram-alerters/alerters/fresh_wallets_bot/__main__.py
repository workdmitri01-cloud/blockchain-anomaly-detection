"""python -m alerters.fresh_wallets_bot -c config/fresh_wallets.yaml [--once] [--dry-run]"""
from ..common.runner import cli_args
from ..common.state import State
from ..common.telegram import TelegramSender
from .bot import FreshWalletsAlerter, load_config


def main() -> None:
    args = cli_args("Telegram alerts for CEX withdrawals to fresh wallets", "config/fresh_wallets.yaml")
    cfg = load_config(args.config, dry_run=args.dry_run)
    bot = FreshWalletsAlerter(cfg, TelegramSender(cfg.telegram, dry_run=args.dry_run), State(cfg.state_file))
    if args.once:
        bot.run_once(max_runtime=args.max_runtime)
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
