"""exchange_flows_bot auto_tokens mode: token auto-discovery, exclusions, learned deposit addresses."""
from alerters.common.config import ChainConfig, TelegramConfig
from alerters.common.prices import PriceOracle
from alerters.common.state import State
from alerters.exchange_flows_bot.bot import ExchangeBotConfig, ExchangeFlowsAlerter

from test_alerters import BINANCE, OKX, USER, FakeRpc, Sink, book, make_log

GNOSIS = ChainConfig(
    name="gnosis", chain_id=100, rpc_urls=["http://fake"], explorer="https://gnosisscan.io",
    defillama="xdai", coingecko="xdai", confirmations=0, log_chunk=100, initial_lookback=10,
)
ALT = "0x" + "a1" * 20      # altcoin, $2
ALT2 = "0x" + "a2" * 20     # altcoin seen for the first time later
USDC = "0x" + "c1" * 20     # stable
WETH = "0x" + "e1" * 20     # major
SCAM = "0x" + "5c" * 20     # priced but low confidence
DEP = "0x" + "d0" * 20      # user's deposit address at Binance (EOA)
USER2 = "0x" + "44" * 20
ROUTER = "0x" + "77" * 20   # contract sending to an exchange
E18 = 10 ** 18


class AutoRpc(FakeRpc):
    def is_contract(self, addr):
        return addr == ROUTER


class Oracle(PriceOracle):
    def __init__(self):
        super().__init__()
        for token, sym, price, conf in [(ALT, "ALT", 2.0, 0.99), (ALT2, "NEW", 4.0, None),
                                        (USDC, "USDC", 1.0, 0.99), (WETH, "WETH", 3000.0, 0.99),
                                        (SCAM, "SCAM", 50.0, 0.2)]:
            self.set_metadata("gnosis", token, sym, 18)
            info = self.get("gnosis", token)
            info.price, info.confidence = price, conf

    def refresh(self, wanted):
        self.refreshed = set(wanted)


def auto_bot(tmp_path, logs, **kw):
    cfg = ExchangeBotConfig(
        telegram=TelegramConfig("t", "1"), chains={"gnosis": GNOSIS}, tokens=[],
        state_file=str(tmp_path / "s.json"), auto_token_chains=["gnosis"], min_usd=80_000, **kw,
    )
    sink = Sink()
    oracle = Oracle()
    rpc = AutoRpc(logs)
    bot = ExchangeFlowsAlerter(cfg, sink, State(cfg.state_file), oracle=oracle,
                               rpcs={"gnosis": rpc}, book=book())
    return bot, sink, rpc, oracle


def tx(n):
    return "0x" + f"{n:064x}"


def test_auto_reads_all_transfers_and_filters_exclusions(tmp_path):
    logs = [
        make_log(BINANCE, USER, 50_000 * E18, token=ALT, block=101, tx=tx(1)),     # $100k withdraw
        make_log(BINANCE, USER, 200_000 * E18, token=USDC, block=101, idx=1, tx=tx(2)),  # stable
        make_log(BINANCE, USER, 100 * E18, token=WETH, block=101, idx=2, tx=tx(3)),      # major
        make_log(BINANCE, USER, 10_000 * E18, token=SCAM, block=101, idx=3, tx=tx(4)),   # low conf
        make_log(USER, USER2, 10 ** 9 * E18, token=ALT, block=102, tx=tx(5)),           # not CEX
    ]
    bot, sink, rpc, oracle = auto_bot(tmp_path, logs)
    bot.run_once()
    assert rpc.calls[0][2] is None  # no token-address filter: whole chain
    assert len(sink.messages) == 1 and "ВЫВОД" in sink.messages[0] and "ALT" in sink.messages[0]
    # only CEX-related tokens get priced, the unrelated big transfer is not
    assert all(k.startswith("gnosis:") for k in oracle.refreshed)


def test_deposit_address_learned_from_sweep_then_realtime(tmp_path):
    logs = [
        # user -> deposit address (unknown yet: silent)
        make_log(USER, DEP, 60_000 * E18, token=ALT, block=101, tx=tx(1)),
        # first sweep -> learn DEP, alert with original sender found by lookback
        make_log(DEP, BINANCE, 60_000 * E18, token=ALT, block=102, tx=tx(2)),
        # next deposit of a NEW token into the learned address -> realtime alert
        make_log(USER2, DEP, 30_000 * E18, token=ALT2, block=103, tx=tx(3)),
        # its sweep -> silent (already alerted)
        make_log(DEP, BINANCE, 30_000 * E18, token=ALT2, block=104, tx=tx(4)),
        # exchange gas top-up to deposit address -> silent
        make_log(BINANCE, DEP, 90_000 * E18, token=ALT, block=105, tx=tx(5)),
    ]
    bot, sink, rpc, _ = auto_bot(tmp_path, logs)
    bot.run_once()
    assert len(sink.messages) == 2, sink.messages
    first, second = sink.messages
    assert "ЗАВОД на биржу Binance" in first and "исходный отправитель" in first and USER[2:8] in first
    assert "ЗАВОД на биржу Binance" in second and "депозитный адрес" in second and USER2[2:8] in second
    # persisted across restarts
    assert State(str(tmp_path / "s.json")).extra["deposit_addrs"]["gnosis"][DEP] == "Binance"


def test_stable_sweep_still_teaches_deposit_address(tmp_path):
    logs = [
        make_log(DEP, BINANCE, 500_000 * E18, token=USDC, block=101, tx=tx(1)),  # stable: no alert, learn
        make_log(USER, DEP, 50_000 * E18, token=ALT, block=102, tx=tx(2)),       # $100k alt -> alert
    ]
    bot, sink, _, _ = auto_bot(tmp_path, logs)
    bot.run_once()
    assert len(sink.messages) == 1 and "ALT" in sink.messages[0]


def test_contract_and_spam_are_not_learned(tmp_path):
    logs = [
        make_log(ROUTER, OKX, 100_000 * E18, token=ALT, block=101, tx=tx(1)),   # contract -> alert, no learn
        make_log(USER2, BINANCE, 10 ** 6 * E18, token=SCAM, block=101, idx=1, tx=tx(2)),  # spam: nothing
    ]
    bot, sink, _, _ = auto_bot(tmp_path, logs)
    bot.run_once()
    deps = bot.state.extra.get("deposit_addrs", {}).get("gnosis", {})
    assert ROUTER not in deps and USER2 not in deps
    assert len(sink.messages) == 1 and "OKX" in sink.messages[0]


def test_exclusion_rules(tmp_path):
    bot, *_ = auto_bot(tmp_path, [])
    ex = lambda sym, price=5.0: bot._excluded("gnosis", "0x" + "99" * 20, sym, price)  # noqa: E731
    for s in ["USDC", "sDAI", "EURe", "wstETH", "WETH", "cbBTC", "WBNB", "bSOL", "XAUT", "USDT0", "crvUSD"]:
        assert ex(s), s
    for s in ["GNO", "COW", "SAFE", "ARB", "PEPE", "ETHFI"]:
        assert not ex(s), s
    assert ex("WHATEVER", price=1.001)  # pegged by price
