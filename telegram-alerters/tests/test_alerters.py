import json

import pytest

from alerters.common.config import ChainConfig, TelegramConfig, TokenConfig, _substitute_env
from alerters.common.evm import TRANSFER_TOPIC, EvmRpc, RangeTooLarge, address_topic, decode_transfer
from alerters.common.labels import AddressBook
from alerters.common.prices import PriceOracle
from alerters.common.state import State
from alerters.exchange_flows_bot.bot import ExchangeBotConfig, ExchangeFlowsAlerter
from alerters.team_wallets_bot.bot import TeamBotConfig, TeamWallet, TeamWalletsAlerter

TOKEN = "0x" + "aa" * 20
OTHER_TOKEN = "0x" + "ab" * 20
TEAM = "0x" + "11" * 20
TEAM2 = "0x" + "12" * 20
BINANCE = "0x28c6c06298d514db089934071355e5743bf21d60"
BINANCE_COLD = "0xf977814e90da44bfa03b6295a0616a897441acec"
OKX = "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b"
USER = "0x" + "22" * 20
DEPOSIT_ADDR = "0x" + "33" * 20

CHAIN = ChainConfig(
    name="ethereum", chain_id=1, rpc_urls=["http://fake"], explorer="https://etherscan.io",
    defillama="ethereum", coingecko="ethereum", confirmations=0, log_chunk=100, initial_lookback=10,
)


def make_log(frm, to, amount, token=TOKEN, block=105, idx=0, tx="0x" + "ee" * 32):
    return {
        "address": token,
        "topics": [TRANSFER_TOPIC, address_topic(frm), address_topic(to)],
        "data": hex(amount),
        "transactionHash": tx,
        "logIndex": hex(idx),
        "blockNumber": hex(block),
    }


class FakeRpc:
    def __init__(self, logs, head=110):
        self.logs = logs
        self.head = head
        self.calls = []

    def block_number(self):
        return self.head

    def get_logs(self, start, end, address=None, topics=None, chunk=100):
        self.calls.append((start, end, address, topics))
        out = []
        for lg in self.logs:
            b = int(lg["blockNumber"], 16)
            if not start <= b <= end:
                continue
            if address and lg["address"] not in address:
                continue
            ok = True
            for i, t in enumerate(topics or []):
                if t is None:
                    continue
                allowed = t if isinstance(t, list) else [t]
                if lg["topics"][i] not in allowed:
                    ok = False
            if ok:
                out.append(lg)
        return out, chunk

    def erc20_metadata(self, token):
        return "SPAM", 18


class FakeOracle(PriceOracle):
    def __init__(self, prices):
        super().__init__()
        for (token, sym, price) in prices:
            self.set_metadata("ethereum", token, sym, 18)
            self.get("ethereum", token).price = price

    def refresh(self, wanted):
        pass


class Sink:
    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)


def book():
    b = AddressBook()
    b.add(BINANCE, "Binance", "Binance 14")
    b.add(BINANCE_COLD, "Binance", "Binance 8")
    b.add(OKX, "OKX", "OKX 7")
    return b


E18 = 10 ** 18


def exchange_bot(tmp_path, logs, price=2.0, **kw):
    cfg = ExchangeBotConfig(
        telegram=TelegramConfig("t", "1"), chains={"ethereum": CHAIN},
        tokens=[TokenConfig("ethereum", TOKEN)], state_file=str(tmp_path / "s.json"), **kw,
    )
    sink = Sink()
    bot = ExchangeFlowsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([(TOKEN, "XYZ", price)]),
                               rpcs={"ethereum": FakeRpc(logs)}, book=book())
    return bot, sink


def team_bot(tmp_path, logs, **kw):
    cfg = TeamBotConfig(
        telegram=TelegramConfig("t", "1"), chains={"ethereum": CHAIN},
        tokens=[TokenConfig("ethereum", TOKEN)], state_file=str(tmp_path / "s.json"),
        wallets=[TeamWallet(TEAM, "Treasury"), TeamWallet(TEAM2, "MM")], **kw,
    )
    sink = Sink()
    bot = TeamWalletsAlerter(cfg, sink, State(cfg.state_file),
                             oracle=FakeOracle([(TOKEN, "XYZ", 2.0), (OTHER_TOKEN, "OTH", None)]),
                             rpcs={"ethereum": FakeRpc(logs)}, book=book())
    return bot, sink


# --- common ---------------------------------------------------------------------------

def test_decode_transfer_skips_erc721():
    lg = make_log(USER, TEAM, 5)
    tr = decode_transfer("ethereum", lg)
    assert tr.from_addr == USER and tr.to_addr == TEAM and tr.raw_amount == 5
    lg["topics"].append("0x" + "00" * 32)
    assert decode_transfer("ethereum", lg) is None


def test_env_substitution(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    assert _substitute_env({"a": ["${FOO}", "${MISSING:-x}", "${MISSING}"]}) == {"a": ["bar", "x", ""]}


def test_state_roundtrip(tmp_path):
    s = State(tmp_path / "st.json")
    s.last_block["ethereum"] = 5
    s.mark_sent("k")
    s.save()
    s2 = State(tmp_path / "st.json")
    assert s2.last_block == {"ethereum": 5} and s2.was_sent("k")


def test_get_logs_halves_chunk_on_range_error():
    rpc = EvmRpc(["http://fake"])
    seen = []

    def call(method, params):
        f = params[0]
        size = int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
        seen.append(size)
        if size > 25:
            raise RangeTooLarge("block range too large")
        return []

    rpc.call = call
    _, chunk = rpc.get_logs(1, 100, chunk=100)
    assert chunk == 25 and seen[:3] == [100, 50, 25]


def test_labels_file_loading(tmp_path):
    p = tmp_path / "l.json"
    p.write_text(json.dumps({"addresses": {BINANCE.upper().replace("0X", "0x"): ["Binance", "Binance 14"]}}))
    b = AddressBook.from_file(p)
    assert b.get(BINANCE).entity == "Binance"


def test_bundled_labels_file_has_major_exchanges():
    b = AddressBook.from_file()
    assert b.get(BINANCE).entity == "Binance"
    assert len(b) > 3000


# --- exchange flows bot ------------------------------------------------------------------

def test_exchange_deposit_and_withdrawal(tmp_path):
    logs = [
        make_log(USER, BINANCE, 50_000 * E18, idx=0),          # $100k deposit
        make_log(OKX, USER, 45_000 * E18, idx=1),              # $90k withdrawal
        make_log(USER, BINANCE, 10_000 * E18, idx=2),          # $20k - below threshold
        make_log(BINANCE, BINANCE_COLD, 90_000 * E18, idx=3),  # internal - skipped
        make_log(BINANCE, OKX, 90_000 * E18, idx=4),           # CEX -> CEX
    ]
    bot, sink = exchange_bot(tmp_path, logs, min_usd=80_000)
    bot.run_once()
    assert len(sink.messages) == 3
    assert "ЗАВОД на биржу Binance" in sink.messages[0] and "$100,000" in sink.messages[0]
    assert "ВЫВОД с биржи OKX" in sink.messages[1]
    assert "Binance → OKX" in sink.messages[2]
    assert bot.state.last_block["ethereum"] == 110


def test_exchange_deposit_address_origin(tmp_path):
    logs = [
        make_log(USER, DEPOSIT_ADDR, 50_000 * E18, block=103, idx=0, tx="0x" + "01" * 32),
        make_log(DEPOSIT_ADDR, BINANCE, 50_000 * E18, block=104, idx=0, tx="0x" + "02" * 32),
    ]
    bot, sink = exchange_bot(tmp_path, logs)
    bot.run_once()
    assert len(sink.messages) == 1
    assert "исходный отправитель" in sink.messages[0] and USER[:6] in sink.messages[0]


def test_exchange_no_duplicates_and_resume(tmp_path):
    logs = [make_log(USER, BINANCE, 50_000 * E18, block=105)]
    bot, sink = exchange_bot(tmp_path, logs)
    bot.run_once()
    bot.state.last_block["ethereum"] = 100  # simulate re-scan of the same range
    bot.run_once()
    assert len(sink.messages) == 1


def test_exchange_unpriced_token_ignored(tmp_path):
    bot, sink = exchange_bot(tmp_path, [make_log(USER, BINANCE, 10 ** 30)], price=None)
    bot.run_once()
    assert sink.messages == []


def test_exchange_failed_send_does_not_advance(tmp_path):
    bot, _ = exchange_bot(tmp_path, [make_log(USER, BINANCE, 50_000 * E18)])

    def boom(text):
        raise RuntimeError("telegram down")

    bot.sender.send = boom
    bot.run_once(max_runtime=1)
    assert "ethereum" not in bot.state.last_block


# --- team wallets bot -----------------------------------------------------------------

def test_team_in_out_and_cex_tag(tmp_path):
    logs = [
        make_log(TEAM, BINANCE, 1_000 * E18, idx=0),
        make_log(USER, TEAM, 5 * E18, idx=1),
        make_log(TEAM, TEAM2, 7 * E18, idx=2),
        make_log(USER, USER, 7 * E18, idx=3),  # unrelated
    ]
    bot, sink = team_bot(tmp_path, logs)
    bot.run_once()
    assert len(sink.messages) == 3
    assert "Исходящий" in sink.messages[0] and "Депозит на биржу <b>Binance" in sink.messages[0]
    assert "Входящий" in sink.messages[1]
    assert "между командными" in sink.messages[2]


def test_team_filters_query_by_wallet_topics(tmp_path):
    bot, _ = team_bot(tmp_path, [])
    filters = bot.log_filters(CHAIN)
    assert filters[0]["topics"] == [TRANSFER_TOPIC, [address_topic(TEAM), address_topic(TEAM2)]]
    assert filters[1]["topics"][1] is None and filters[0]["address"] == [TOKEN]


def test_team_track_all_skips_unpriced_spam(tmp_path):
    logs = [make_log(USER, TEAM, 5 * E18, token=OTHER_TOKEN)]
    bot, sink = team_bot(tmp_path, logs, track_all_tokens=True)
    assert bot.log_filters(CHAIN)[0]["address"] is None
    bot.run_once()
    assert sink.messages == []


def test_team_min_usd(tmp_path):
    bot, sink = team_bot(tmp_path, [make_log(TEAM, USER, 1 * E18)], min_usd=100)
    bot.run_once()
    assert sink.messages == []


@pytest.mark.parametrize("alert_internal,expected", [(True, 1), (False, 0)])
def test_team_internal_toggle(tmp_path, alert_internal, expected):
    bot, sink = team_bot(tmp_path, [make_log(TEAM, TEAM2, 1 * E18)], alert_internal=alert_internal)
    bot.run_once()
    assert len(sink.messages) == expected
