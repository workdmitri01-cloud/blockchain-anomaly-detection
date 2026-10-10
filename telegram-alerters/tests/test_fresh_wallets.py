"""fresh_wallets_bot: withdrawals from exchanges to fresh EOAs."""
from alerters.common.config import TelegramConfig
from alerters.common.state import State
from alerters.fresh_wallets_bot.bot import FreshBotConfig, FreshWalletsAlerter, load_config

from test_alerters import BINANCE, OKX, Sink, book, make_log
from test_auto_tokens import ALT, DEP, E18, GNOSIS, ROUTER, SCAM, USDC, AutoRpc, Oracle, tx

FRESH = "0x" + "f0" * 20
OLD = "0x" + "0d" * 20     # EOA with history
USED1 = "0x" + "f1" * 20   # 1 outgoing tx


class FreshRpc(AutoRpc):
    def call(self, method, params):
        assert method == "eth_getTransactionCount"
        return {FRESH: "0x0", USED1: "0x1", ROUTER: "0x1"}.get(params[0], "0x2a")


def fresh_bot(tmp_path, logs, **kw):
    cfg = FreshBotConfig(
        telegram=TelegramConfig("t", "1"), chains={"gnosis": GNOSIS}, tokens=[],
        state_file=str(tmp_path / "f.json"), auto_token_chains=["gnosis"], min_usd=30_000, **kw,
    )
    sink = Sink()
    bot = FreshWalletsAlerter(cfg, sink, State(cfg.state_file), oracle=Oracle(),
                              rpcs={"gnosis": FreshRpc(logs)}, book=book())
    return bot, sink


def test_only_withdrawals_to_fresh_eoas(tmp_path):
    logs = [
        make_log(BINANCE, FRESH, 20_000 * E18, token=ALT, block=101, tx=tx(1)),    # $40k -> fresh: ALERT
        make_log(BINANCE, OLD, 500_000 * E18, token=ALT, block=101, idx=1, tx=tx(2)),  # old wallet
        make_log(BINANCE, USED1, 50_000 * E18, token=ALT, block=101, idx=2, tx=tx(3)),  # nonce 1 > 0
        make_log(BINANCE, ROUTER, 50_000 * E18, token=ALT, block=101, idx=3, tx=tx(4)),  # contract
        make_log(BINANCE, FRESH, 5_000 * E18, token=ALT, block=102, tx=tx(5)),     # $10k < $30k
        make_log(BINANCE, FRESH, 500_000 * E18, token=USDC, block=102, idx=1, tx=tx(6)),  # stable
        make_log(BINANCE, FRESH, 10_000 * E18, token=SCAM, block=102, idx=2, tx=tx(7)),   # spam
        make_log(FRESH, BINANCE, 1 * E18, token=ALT, block=103, tx=tx(8)),         # deposit: not ours
        make_log(BINANCE, OKX, 90_000 * E18, token=ALT, block=103, idx=1, tx=tx(9)),  # cex -> cex
    ]
    bot, sink = fresh_bot(tmp_path, logs)
    bot.run_once()
    assert len(sink.messages) == 1, sink.messages
    msg = sink.messages[0]
    assert "ФРЕШ" in msg and "Binance" in msg and "ни одной исходящей" in msg and "$40,000" in msg


def test_max_nonce_and_deposit_addresses_skipped(tmp_path):
    logs = [
        make_log(DEP, BINANCE, 1_000 * E18, token=ALT, block=101, tx=tx(1)),      # sweep -> DEP learned
        make_log(BINANCE, DEP, 50_000 * E18, token=ALT, block=102, tx=tx(2)),     # to deposit addr: skip
        make_log(BINANCE, USED1, 50_000 * E18, token=ALT, block=103, tx=tx(3)),   # nonce 1 <= 1: ALERT
    ]
    bot, sink = fresh_bot(tmp_path, logs, max_nonce=1)
    bot.run_once()
    assert len(sink.messages) == 1 and "1 исходящих" in sink.messages[0]


def test_example_config_loads(monkeypatch):
    monkeypatch.setenv("FRESH_BOT_TOKEN", "x")
    monkeypatch.setenv("FRESH_CHAT_ID", "1")
    cfg = load_config("config/fresh_wallets.gnosis.example.yaml")
    assert cfg.auto_token_chains == ["gnosis"] and cfg.min_usd == 30_000 and cfg.max_nonce == 0
    assert cfg.state_file == "state/fresh_wallets.json"
