"""python -m alerters.team_wallets_bot -c config/team_wallets.yaml [--once] [--dry-run]"""
from ..common.runner import cli_args
from ..common.state import State
from ..common.telegram import TelegramSender
from .bot import TeamWalletsAlerter, load_config


def main() -> None:
    args = cli_args("Telegram alerts for team wallet token transfers", "config/team_wallets.yaml")
    cfg = load_config(args.config, dry_run=args.dry_run)
    bot = TeamWalletsAlerter(cfg, TelegramSender(cfg.telegram, dry_run=args.dry_run), State(cfg.state_file))
    if args.once:
        bot.run_once(max_runtime=args.max_runtime)
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
