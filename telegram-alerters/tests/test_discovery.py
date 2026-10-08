from alerters.common.explorer import AddressInfo, Explorer, MultiExplorer, ExplorerError
from alerters.common.state import State
from alerters.common.config import TelegramConfig, TokenConfig
from alerters.team_wallets_bot.bot import AutoDiscoveryConfig, TeamBotConfig, TeamWallet, TeamWalletsAlerter
from alerters.team_wallets_bot.discovery import ZERO, DiscoveryParams, classify, discover

from test_alerters import BINANCE, CHAIN, E18, TOKEN, FakeOracle, FakeRpc, Sink, book, make_log

DEPLOYER = "0x" + "d0" * 20
SAFE = "0x" + "5a" * 20
VESTING = "0x" + "7e" * 20
PAIR = "0x" + "9a" * 20
BENEFICIARY = "0x" + "be" * 20
WHALE = "0x" + "77" * 20
USER = "0x" + "22" * 20
FRESH = "0x" + "f1" * 20

SUPPLY = 1_000_000 * E18

INFOS = {
    DEPLOYER: AddressInfo(is_contract=False),
    SAFE: AddressInfo(is_contract=True, name="GnosisSafeProxy"),
    VESTING: AddressInfo(is_contract=True, name="TokenVesting"),
    PAIR: AddressInfo(is_contract=True, name="UniswapV2Pair"),
    BENEFICIARY: AddressInfo(is_contract=False),
    WHALE: AddressInfo(is_contract=False),
    FRESH: AddressInfo(is_contract=False),
}


class FakeExplorer(Explorer):
    def contract_creation(self, address):
        return DEPLOYER, "0x" + "c0" * 32

    def first_transfer_logs(self, token, limit=1000):
        txs = [
            (ZERO, DEPLOYER, SUPPLY),
            (DEPLOYER, SAFE, 200_000 * E18),
            (DEPLOYER, VESTING, 150_000 * E18),
            (DEPLOYER, PAIR, 100_000 * E18),
            (DEPLOYER, BINANCE, 50_000 * E18),
            (DEPLOYER, USER, 100 * E18),
            (VESTING, BENEFICIARY, 50_000 * E18),
        ]
        return [make_log(f, t, a, block=10 + i, idx=0, tx="0x%064x" % i) for i, (f, t, a) in enumerate(txs)]

    def top_holders(self, token, limit=50):
        return [(SAFE, 200_000 * E18, INFOS[SAFE]), (VESTING, 100_000 * E18, INFOS[VESTING]),
                (PAIR, 100_000 * E18, INFOS[PAIR]), (WHALE, 30_000 * E18, INFOS[WHALE])]

    def address_info(self, address):
        return INFOS.get(address, AddressInfo())


class DiscoveryRpc(FakeRpc):
    def total_supply(self, token):
        return SUPPLY

    def is_contract(self, address):
        return INFOS.get(address, AddressInfo(is_contract=False)).is_contract


def test_classify():
    assert classify(AddressInfo(True, "GnosisSafeProxy")) == "team"
    assert classify(AddressInfo(True, "UniswapV3Pool")) == "non_team"
    assert classify(AddressInfo(True, "MerkleDistributor")) == "non_team"
    assert classify(AddressInfo(False, None)) == "unknown"


def test_discover_finds_team_and_skips_defi_and_cex():
    team, low = discover("ethereum", TOKEN, "XYZ", DiscoveryRpc([]), FakeExplorer(), book(), DiscoveryParams())
    team_addrs = {c.address for c in team}
    assert team_addrs == {DEPLOYER, SAFE, VESTING}
    assert {c.address for c in low} >= {BENEFICIARY, WHALE}
    everything = team_addrs | {c.address for c in low}
    assert PAIR not in everything and BINANCE not in everything and USER not in everything
    safe = next(c for c in team if c.address == SAFE)
    assert any("Gnosis" in r for r in safe.reasons) and safe.score >= 6


def test_factory_deployer_is_dropped():
    class FactoryExplorer(FakeExplorer):
        def address_info(self, address):
            if address == DEPLOYER:
                return AddressInfo(is_contract=True, name="TokenFactory")
            return super().address_info(address)

    INFOS_BACKUP = INFOS[DEPLOYER]
    INFOS[DEPLOYER] = AddressInfo(is_contract=True, name=None)
    try:
        team, _ = discover("ethereum", TOKEN, "XYZ", DiscoveryRpc([]), FactoryExplorer(), book())
    finally:
        INFOS[DEPLOYER] = INFOS_BACKUP
    assert DEPLOYER not in {c.address for c in team}


def test_multi_explorer_falls_back():
    class Broken(Explorer):
        def contract_creation(self, address):
            raise ExplorerError("down")

    assert MultiExplorer([Broken(), FakeExplorer()]).contract_creation(TOKEN)[0] == DEPLOYER


def make_bot(tmp_path, logs, **ad):
    cfg = TeamBotConfig(
        telegram=TelegramConfig("t", "1"), chains={"ethereum": CHAIN},
        tokens=[TokenConfig("ethereum", TOKEN)], state_file=str(tmp_path / "s.json"),
        auto_discovery=AutoDiscoveryConfig(**ad),
    )
    sink = Sink()
    bot = TeamWalletsAlerter(cfg, sink, State(cfg.state_file), oracle=FakeOracle([(TOKEN, "XYZ", 2.0)]),
                             rpcs={"ethereum": DiscoveryRpc(logs)}, book=book(),
                             explorers={"ethereum": FakeExplorer()})
    return bot, sink


def test_bot_discovers_and_alerts_without_manual_wallets(tmp_path):
    logs = [make_log(SAFE, BINANCE, 60_000 * E18, block=105)]
    bot, sink = make_bot(tmp_path, logs)
    bot.run_once()
    assert "Автопоиск" in sink.messages[0] and "добавлено" in sink.messages[0].lower()
    assert "Исходящий" in sink.messages[1] and "Депозит на биржу <b>Binance" in sink.messages[1]
    # discovery is cached: no second summary on the next cycle
    bot.run_once()
    assert sum("Автопоиск" in m for m in sink.messages) == 1


def test_exclude_wallets(tmp_path):
    bot, _ = make_bot(tmp_path, [])
    bot.cfg.exclude_wallets = {SAFE}
    bot.run_once()
    assert SAFE not in bot.team_wallets("ethereum") and DEPLOYER in bot.team_wallets("ethereum")


def test_follow_the_money(tmp_path):
    logs = [
        make_log(SAFE, FRESH, 60_000 * E18, block=103, idx=0),   # $120k -> FRESH becomes team
        make_log(SAFE, PAIR, 60_000 * E18, block=103, idx=1),    # sale into DEX pool -> not followed
        make_log(SAFE, USER, 10 * E18, block=103, idx=2),        # small -> not followed
    ]
    bot, sink = make_bot(tmp_path, logs, notify=False, follow_min_usd=50_000)
    bot.run_once()
    wallets = bot.team_wallets("ethereum")
    assert FRESH in wallets and wallets[FRESH].depth == 1
    assert PAIR not in wallets and USER not in wallets
    assert any("автоматически добавлен" in m for m in sink.messages)

    # next block: FRESH dumps to Binance -> alert as team outflow
    bot.rpcs["ethereum"].logs.append(make_log(FRESH, BINANCE, 60_000 * E18, block=112, tx="0x" + "12" * 32))
    bot.rpcs["ethereum"].head = 115
    bot.run_once()
    assert "Депозит на биржу" in sink.messages[-1] and "🤖" in sink.messages[-1]


def test_follow_depth_limit(tmp_path):
    bot, _ = make_bot(tmp_path, [], notify=False, follow_max_depth=1)
    bot.state.extra["auto_wallets"] = {"ethereum": {FRESH: {"name": "x", "depth": 1}}}
    bot.state.extra["discovery_at"] = {f"ethereum:{TOKEN}": 9e18}
    bot.rpcs["ethereum"].logs = [make_log(FRESH, WHALE, 60_000 * E18, block=105)]
    bot.run_once()
    assert WHALE not in bot.team_wallets("ethereum")


# --- BSC without a paid explorer ------------------------------------------------------

from alerters.common.evm import TRANSFER_TOPIC as _T
from alerters.common.explorer import NodeRealExplorer


class FakeJsonRpc:
    """requests.Session stand-in answering JSON-RPC by method name."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.methods = []

    def post(self, url, json, timeout):
        self.methods.append(json["method"])
        result = self.handlers[json["method"]](json["params"])

        class R:
            status_code = 200

            def json(self_inner):
                return {"jsonrpc": "2.0", "id": json["id"], "result": result}

        return R()


def test_nodereal_creation_and_early_logs():
    early = make_log(ZERO, DEPLOYER, SUPPLY, block=1000)
    session = FakeJsonRpc({
        # documented response nests the object in another result wrapper
        "nr_getContractCreationTransaction": lambda p: {"result": {"from": DEPLOYER, "hash": "0xabc", "blockNumber": "0x3e8"}},
        "eth_blockNumber": lambda p: hex(1500),
        "eth_getLogs": lambda p: [early] if int(p[0]["fromBlock"], 16) <= 1000 <= int(p[0]["toBlock"], 16) else [],
    })
    nr = NodeRealExplorer("https://bsc-mainnet.nodereal.io/v1/key", chunk=300, session=session)
    assert nr.contract_creation(TOKEN) == (DEPLOYER, "0xabc")
    logs = nr.first_transfer_logs(TOKEN)
    assert logs == [early]
    assert session.methods.count("nr_getContractCreationTransaction") == 1  # cached


def test_unverified_pair_detected_by_fingerprint():
    class NoNames(FakeExplorer):
        def top_holders(self, token, limit=50):
            return []

        def address_info(self, address):
            return AddressInfo(is_contract=INFOS.get(address, AddressInfo()).is_contract)

    class ProbeRpc(DiscoveryRpc):
        def fingerprint(self, address):
            return {PAIR: "AMM pair / pool", SAFE: "Safe multisig"}.get(address)

    team, low = discover("bsc", TOKEN, "XYZ", ProbeRpc([]), NoNames(), book())
    addrs = {c.address for c in team} | {c.address for c in low}
    assert PAIR not in addrs
    assert SAFE in {c.address for c in team}
