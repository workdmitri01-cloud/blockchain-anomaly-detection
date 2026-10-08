"""Tron / Solana / HyperCore clients and both bots on those chains (HTTP faked)."""
from alerters.common.addr import TRON_ZERO, tron_from_hex, tron_to_hex
from alerters.common.config import ChainConfig, TelegramConfig, TokenConfig
from alerters.common.hypercore import HyperCoreClient, HyperCoreExplorer
from alerters.common.labels import AddressBook
from alerters.common.solana import SolanaClient, transfers_from_balances
from alerters.common.state import State
from alerters.common.tron import TronClient, TronExplorer
from alerters.exchange_flows_bot.bot import ExchangeBotConfig, ExchangeFlowsAlerter
from alerters.team_wallets_bot.bot import AutoDiscoveryConfig, TeamBotConfig, TeamWallet, TeamWalletsAlerter
from alerters.team_wallets_bot.discovery import DiscoveryParams, discover

from test_alerters import FakeOracle, Sink

USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
BINANCE_TRON = "TV6MuMXfmLbBqPZvBHdwFsDnQeVfnmiuSi"
USER_TRON = tron_from_hex("0x" + "22" * 20)
TEAM_TRON = tron_from_hex("0x" + "11" * 20)

MINT = "EPjFWdd5AufqSSqeM5qZNwHxXFHbr5yKSdUoS9xS5Uq"  # any 32-byte base58
BINANCE_SOL = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
USER_SOL = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class Router:
    """requests.Session stand-in: route(method, url, params/json) -> body."""

    def __init__(self, route):
        self.route = route
        self.calls = []

    def request(self, method, url, headers=None, timeout=None, params=None, json=None):
        self.calls.append((method, url, params, json))
        return Resp(self.route(method, url, params or {}, json or {}))

    def get(self, url, params=None, timeout=None, headers=None):
        return self.request("GET", url, params=params)

    def post(self, url, json=None, timeout=None, headers=None):
        return self.request("POST", url, json=json)


def chain(name, kind, **kw):
    return ChainConfig(name=name, kind=kind, rpc_urls=["http://fake"], explorer="https://x", initial_lookback=10**10, **kw)


# --- address helpers ----------------------------------------------------------------------

def test_tron_address_roundtrip():
    assert tron_to_hex(USDT_TRON) == "0xa614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert tron_from_hex("0x41a614f803b6fd780986a42c78ec9c7f77e6ded13c") == USDT_TRON
    assert TRON_ZERO == "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


def test_book_families_are_case_sensitive_for_base58():
    b = AddressBook.from_file()
    assert b.get(BINANCE_TRON).family == "tron"
    assert b.get(BINANCE_SOL).family == "solana"
    assert b.get(BINANCE_SOL.lower()) is None
    assert BINANCE_TRON in b.addresses_for("tron") and BINANCE_SOL not in b.addresses_for("tron")


# --- Tron -----------------------------------------------------------------------------------

def tron_router():
    events = [
        {"block_number": 10, "block_timestamp": 1000, "contract_address": tron_to_hex(USDT_TRON), "event_index": 0,
         "transaction_id": "aa", "result": {"from": "0x41" + tron_to_hex(USER_TRON)[2:], "to": tron_to_hex(BINANCE_TRON),
                                             "value": str(200_000 * 10**6)}},
        {"block_number": 11, "block_timestamp": 2000, "contract_address": USDT_TRON, "event_index": 1,
         "transaction_id": "bb", "result": {"from": BINANCE_TRON, "to": USER_TRON, "value": str(5 * 10**6)}},
    ]

    def route(method, url, params, body):
        if "/events" in url:
            return {"data": [e for e in events if e["block_timestamp"] >= int(params["min_block_timestamp"])], "meta": {}}
        if "/transactions/trc20" in url:
            return {"data": [{"transaction_id": "cc", "block_timestamp": 3000, "from": TEAM_TRON, "to": BINANCE_TRON,
                              "value": str(1_000 * 10**6), "token_info": {"address": USDT_TRON, "decimals": 6, "symbol": "USDT"}}]}
        if url.endswith("/wallet/getcontract"):
            return {"origin_address": TEAM_TRON, "bytecode": "60", "name": "TetherToken"} if body["value"] == USDT_TRON else {}
        if url.endswith("/wallet/triggerconstantcontract"):
            return {"constant_result": ["0" * 63 + "6"], "result": {"result": True}}
        raise AssertionError(url)

    return Router(route)


def test_tron_tokens_mode_decodes_events():
    c = TronClient(session=tron_router(), api_key="k")
    c.min_interval = 0
    trs, cur = c.poll("tron", {"ts": 0}, [USDT_TRON], set(), "tokens", 60)
    assert [(t.from_addr, t.to_addr, t.token) for t in trs][0] == (USER_TRON, BINANCE_TRON, USDT_TRON)
    assert cur == {"ts": 2000}


def test_exchange_bot_on_tron(tmp_path):
    cfg = ExchangeBotConfig(telegram=TelegramConfig("t", "1"), chains={"tron": chain("tron", "tron")},
                            tokens=[TokenConfig("tron", USDT_TRON)], state_file=str(tmp_path / "s.json"), min_usd=80_000)
    client = TronClient(session=tron_router(), api_key="k")
    client.min_interval = 0
    sink = Sink()
    bot = ExchangeFlowsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([]), rpcs={"tron": client})
    bot.oracle.set_metadata("tron", USDT_TRON, "USDT", 6)
    bot.oracle.get("tron", USDT_TRON).price = 1.0
    bot.run_once()
    assert len(sink.messages) == 1 and "ЗАВОД на биржу Binance" in sink.messages[0] and "200,000 USDT" in sink.messages[0]
    bot.run_once()  # cursor re-reads the last ms -> no duplicate alert
    assert len(sink.messages) == 1


def test_team_bot_on_tron_accounts_mode(tmp_path):
    cfg = TeamBotConfig(telegram=TelegramConfig("t", "1"), chains={"tron": chain("tron", "tron")},
                        tokens=[TokenConfig("tron", USDT_TRON)], state_file=str(tmp_path / "s.json"),
                        wallets=[TeamWallet(TEAM_TRON, "Treasury")])
    client = TronClient(session=tron_router(), api_key="k")
    client.min_interval = 0
    sink = Sink()
    bot = TeamWalletsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([]), rpcs={"tron": client})
    bot.oracle.get("tron", USDT_TRON).price = 1.0
    bot.run_once()
    assert len(sink.messages) == 1
    assert "Депозит на биржу <b>Binance" in sink.messages[0] and "1,000.00 USDT" in sink.messages[0]


def test_tron_discovery_creator():
    c = TronClient(session=tron_router(), api_key="k")
    c.min_interval = 0
    assert TronExplorer(c).contract_creation(USDT_TRON) == (TEAM_TRON, "")


# --- Solana -----------------------------------------------------------------------------------

def bal(owner, amount, idx):
    return {"accountIndex": idx, "mint": MINT, "owner": owner, "uiTokenAmount": {"amount": str(amount), "decimals": 6}}


def test_solana_balance_diff_to_transfers():
    meta = {"preTokenBalances": [bal(USER_SOL, 500, 1), bal(BINANCE_SOL, 0, 2)],
            "postTokenBalances": [bal(USER_SOL, 100, 1), bal(BINANCE_SOL, 400, 2)]}
    (tr,) = transfers_from_balances("solana", "sig1", 7, meta, {BINANCE_SOL})
    assert (tr.from_addr, tr.to_addr, tr.raw_amount, tr.decimals) == (USER_SOL, BINANCE_SOL, 400, 6)


def solana_rpc(handlers):
    class S:
        def post(self, url, json, timeout):
            return Resp({"jsonrpc": "2.0", "id": json["id"], "result": handlers[json["method"]](json["params"])})
    return S()


def test_exchange_bot_on_solana(tmp_path):
    meta = {"preTokenBalances": [bal(USER_SOL, 200_000 * 10**6, 1)],
            "postTokenBalances": [bal(USER_SOL, 0, 1), bal(BINANCE_SOL, 200_000 * 10**6, 2)]}
    handlers = {
        "getTokenAccountsByOwner": lambda p: {"value": [{"pubkey": "ATA_" + p[0][:4]}]},
        "getSignaturesForAddress": lambda p: [{"signature": "sig1", "blockTime": 2**40, "err": None}],
        "getTransaction": lambda p: {"slot": 99, "meta": meta},
    }
    cfg = ExchangeBotConfig(telegram=TelegramConfig("t", "1"), chains={"solana": chain("solana", "solana")},
                            tokens=[TokenConfig("solana", MINT)], state_file=str(tmp_path / "s.json"))
    sink = Sink()
    book = AddressBook()
    book.add(BINANCE_SOL, "Binance", "Binance 1")
    client = SolanaClient(["http://fake"], session=solana_rpc(handlers))
    bot = ExchangeFlowsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([]), rpcs={"solana": client}, book=book)
    bot.oracle.get("solana", MINT).price = 1.0
    bot.run_once()
    assert len(sink.messages) == 1 and "ЗАВОД на биржу Binance" in sink.messages[0] and "слот 99" in sink.messages[0]
    # next poll asks only for signatures newer than sig1
    handlers["getSignaturesForAddress"] = lambda p: [] if p[1].get("until") == "sig1" else 1 / 0
    bot.run_once()
    assert len(sink.messages) == 1


# --- HyperCore ----------------------------------------------------------------------------

HC_TEAM = "0x" + "11" * 20
HC_BINANCE = "0x" + "bb" * 20
HC_DEPLOYER = "0x" + "d0" * 20


def hc_router():
    def route(method, url, params, body):
        t = body["type"]
        if t == "userNonFundingLedgerUpdates":
            return [{"time": 5000, "hash": "0xh1", "delta": {"type": "spotTransfer", "token": "PURR", "amount": "1000000",
                                                          "usdcValue": "150000", "user": HC_TEAM, "destination": HC_BINANCE}},
                    {"time": 5001, "hash": "0xh2", "delta": {"type": "deposit", "usdc": "5"}}]
        if t == "spotMeta":
            return {"tokens": [{"name": "PURR", "tokenId": "0xpurr"}]}
        if t == "tokenDetails":
            return {"deployer": HC_DEPLOYER, "totalSupply": "1000000000",
                    "genesis": {"userBalances": [[HC_TEAM, "300000000"], ["0x" + "99" * 20, "1"]]},
                    "nonCirculatingUserBalances": [[HC_TEAM, "250000000"]]}
        raise AssertionError(t)

    return Router(route)


def test_team_bot_on_hypercore(tmp_path):
    cfg = TeamBotConfig(telegram=TelegramConfig("t", "1"), chains={"hypercore": chain("hypercore", "hypercore")},
                        tokens=[TokenConfig("hypercore", "PURR")], state_file=str(tmp_path / "s.json"),
                        wallets=[TeamWallet(HC_TEAM, "Treasury")])
    client = HyperCoreClient(session=hc_router())
    client.min_interval = 0
    sink = Sink()
    book = AddressBook()
    book.add(HC_BINANCE, "Binance", "Binance HL")
    bot = TeamWalletsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([]), rpcs={"hypercore": client}, book=book)
    bot.run_once()
    assert len(sink.messages) == 1
    msg = sink.messages[0]
    assert "1.00M PURR" in msg and "$150,000" in msg and "Депозит на биржу <b>Binance" in msg and "блок" not in msg
    assert bot.state.extra["cursor"]["hypercore"][HC_TEAM] >= 5001


def test_hypercore_discovery_uses_genesis_and_deployer():
    client = HyperCoreClient(session=hc_router())
    client.min_interval = 0
    team, low = discover("hypercore", "PURR", "PURR", client, HyperCoreExplorer(client), AddressBook(), DiscoveryParams())
    found = {c.address: c for c in team}
    assert HC_DEPLOYER in found and HC_TEAM in found
    assert any("минт" in r for r in found[HC_TEAM].reasons)
    assert "0x" + "99" * 20 not in found  # dust genesis balance is not an allocation
