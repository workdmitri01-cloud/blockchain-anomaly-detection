# Telegram-алертеры ончейн-транзакций

Два **независимых** Telegram-бота для EVM-сетей (Ethereum, BSC, Arbitrum, Base, Optimism, Polygon, Avalanche):

| | Бот №1 `team_wallets_bot` | Бот №2 `exchange_flows_bot` |
|---|---|---|
| Что ловит | переводы токенов **с/на командные кошельки** (кошельки находит **сам**) | **заводы/выводы на биржи (CEX)** от **$80k** |
| Telegram | свой токен бота + свой чат | свой токен бота + свой чат |
| Конфиг | `config/team_wallets.yaml` | `config/exchange_flows.yaml` |
| Состояние | `state/team_wallets.json` | `state/exchange_flows.json` |
| Процесс / контейнер / workflow | `team-wallets-bot` / `alerter-team-wallets.yml` | `exchange-flows-bot` / `alerter-exchange-flows.yml` |

Общий у них только код-библиотека `alerters/common`. Боты не знают друг о друге: падение, рестарт или отключение одного никак не влияет на другой.

## Бесплатные источники данных

| Что | Источник | Ключ |
|---|---|---|
| Транзакции | `eth_getLogs` через публичные RPC (publicnode, llamarpc, drpc, официальные RPC сетей) с ротацией и авто-подбором диапазона блоков | не нужен (можно добавить свои Alchemy / Infura / QuickNode — используются первыми) |
| Цены, decimals, symbol | [DefiLlama coins API](https://coins.llama.fi) — один батч-запрос на все токены | не нужен |
| Поиск командных кошельков | [Blockscout](https://eth.blockscout.com) API (деплоер, первые переводы, топ-холдеры, имена контрактов) + Etherscan V2 / Routescan; для **BSC** — [NodeReal MegaNode](https://nodereal.io/meganode) (данные BSCTrace) | ETH/Base/OP/Arbitrum/Polygon/Avalanche — не нужен; **BSC — бесплатный ключ NodeReal** (`NODEREAL_API_KEY`) |
| Резерв цен | CoinGecko `simple/token_price` | не нужен (опц. demo-ключ) |
| Адреса бирж | GitHub: [duneanalytics/spellbook `cex_evms_addresses.sql`](https://github.com/duneanalytics/spellbook/blob/main/dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/cex_evms_addresses.sql) (курируемый список ~4.3k кошельков 300+ бирж) + [brianleect/etherscan-labels](https://github.com/brianleect/etherscan-labels) | не нужен |
| Хостинг | GitHub Actions cron **или** любой VPS / Docker | — |

### Почему это эффективно

* **Один запрос на сеть за цикл.** Бот №2 делает `eth_getLogs(address=[все ваши токены], topic0=Transfer)` — получает все переводы всех отслеживаемых токенов разом и фильтрует биржи локально по базе адресов (в памяти, O(1)).
* **Бот №1 фильтрует на стороне ноды** по `topic1/topic2 = командные кошельки` — нода возвращает только нужные переводы, трафик минимальный.
* Цены кэшируются на 5 минут, метаданные токенов — навсегда (в state).
* Подтверждения (`confirmations`) защищают от реоргов; дедупликация по `tx:logIndex` гарантирует, что алерт не придёт дважды; если Telegram недоступен — блок не помечается обработанным и алерт уйдёт при следующем запуске.

## Быстрый старт (локально)

```bash
cd telegram-alerters
pip install -r requirements.txt
cp config/team_wallets.example.yaml   config/team_wallets.yaml     # указать токены (кошельки найдутся сами)
cp config/exchange_flows.example.yaml config/exchange_flows.yaml   # заполнить токены
cp .env.example .env                                               # токены ботов и chat_id

# проверка без отправки в Telegram — алерты печатаются в консоль
python -m alerters.team_wallets_bot   -c config/team_wallets.yaml   --dry-run --once
python -m alerters.exchange_flows_bot -c config/exchange_flows.yaml --dry-run --once

# боевой режим (каждый бот — отдельный процесс)
set -a; . ./.env; set +a
python -m alerters.team_wallets_bot   -c config/team_wallets.yaml
python -m alerters.exchange_flows_bot -c config/exchange_flows.yaml
```

Создайте **двух** ботов в [@BotFather](https://t.me/BotFather), добавьте каждого в свой чат/канал (в канал — админом). `chat_id` можно узнать, переслав сообщение из чата боту [@userinfobot](https://t.me/userinfobot) или через `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Автопоиск командных кошельков (бот №1)

Достаточно указать **только адреса токенов** — кошельки команды бот найдёт сам.

1. **При старте и раз в сутки** для каждого токена строится список кандидатов с баллами:

   | Сигнал | Баллы |
   |---|---|
   | Деплоер контракта токена | +4 |
   | Получил токены при минте | +4 |
   | Получил крупную долю (≥0.5% supply) в начальном распределении от деплоера/минтера | +3 |
   | То же, на 2-м уровне (команда → vesting → кошелёк) | +2 |
   | Контракт команды по имени/тегу: Safe-мультисиг, Vesting, Timelock, Treasury, DAO | +2 |
   | Публичный тег обозревателя с названием проекта | +2 |
   | Топ-холдер ≥0.5% supply (+1, если всё ещё держит аллокацию) | +1…+2 |

   Кандидаты с баллом ≥ `min_score` (по умолчанию 3) начинают отслеживаться, в чат приходит сводка с причинами.
   Исключаются: биржи (база адресов CEX), DEX-пулы и роутеры, мосты, стейкинг, airdrop-дистрибьюторы, burn-адреса, контракты-фабрики/лаунчпады.

2. **В реальном времени («следование за деньгами»)**: если с командного кошелька уходит крупный перевод
   (≥ `follow_min_usd` или ≥ `follow_min_supply_pct` supply) на новый адрес, который не биржа и не DeFi-контракт, —
   адрес автоматически становится командным (до `follow_max_depth` шагов). Так ловятся новые кошельки, на которые команда
   перекладывает токены перед продажей.

Проверить, что найдёт бот, до запуска:
```bash
python -m alerters.team_wallets_bot.discover --chain ethereum --token 0xВАШ_ТОКЕН
python -m alerters.team_wallets_bot.discover -c config/team_wallets.yaml
```
Лишние адреса (инвесторы, OTC-покупатели) — в `exclude_wallets`; известные вручную — в `wallets` (их имена важнее).
Найденные кошельки хранятся в `state/team_wallets.json`.

### BSC

С конца 2025 года BscScan API закрыт, а бесплатный тариф Etherscan V2 не покрывает BSC (а также Base, Optimism, Avalanche).
Официальная замена от BNB Chain — BSCTrace на инфраструктуре **NodeReal MegaNode**, у неё есть бесплатный тариф:

1. Зарегистрируйтесь на <https://nodereal.io/meganode> (вход через GitHub / Google) и создайте API key.
2. Положите его в `NODEREAL_API_KEY` (`.env` или GitHub Secret).

Бот возьмёт оттуда создателя контракта (`nr_getContractCreationTransaction`) и первые переводы токена (архивный `eth_getLogs`).
Топ-холдеры на BSC не используются. Неверифицированные контракты распознаются прямым вызовом через RPC:
`token0()` → DEX-пул (исключается), `getThreshold()` → Safe-мультисиг (командный). Мониторинг переводов
(оба бота) на BSC работает и без ключа — через публичные RPC.

## Деплой

### Вариант A — Docker на любом сервере (рекомендуется, реальное время ~20–40 с)

```bash
docker compose up -d --build        # поднимает два отдельных контейнера
docker compose logs -f exchange-flows-bot
```
Бесплатно подойдёт Oracle Cloud Always Free / любой уже имеющийся сервер.

### Вариант B — GitHub Actions (полностью бесплатно, задержка 5–15 мин)

Workflow-ы уже лежат в `.github/workflows/`:

* `alerter-team-wallets.yml` — бот №1
* `alerter-exchange-flows.yml` — бот №2
* `alerters-update-labels.yml` — еженедельное обновление базы адресов бирж из GitHub
* `alerters-tests.yml` — тесты

Включение (Settings → Secrets and variables → Actions):

| Бот | Variables | Secrets |
|---|---|---|
| №1 | `TEAM_ALERTER_ENABLED=true` | `TEAM_BOT_TOKEN`, `TEAM_CHAT_ID`, `TEAM_CONFIG_YAML` (содержимое `team_wallets.yaml`) |
| №2 | `EXCHANGE_ALERTER_ENABLED=true` | `EXCHANGE_BOT_TOKEN`, `EXCHANGE_CHAT_ID`, `EXCHANGE_CONFIG_YAML` |
| общие, опц. | | `ETH_RPC_URL`, `COINGECKO_API_KEY`, `ETHERSCAN_API_KEY`, `NODEREAL_API_KEY` (нужен для автопоиска на BSC) |

Состояние (последний блок, отправленные алерты) переносится между запусками через `actions/cache`.
Ограничения: cron в GitHub Actions срабатывает не чаще раза в 5 минут и часто с задержкой; для **публичного** репозитория минуты бесплатны без лимита, для **приватного** — 2000 мин/мес (два бота каждые 5 минут ≈ 17k мин/мес — для приватного репо используйте вариант A или увеличьте интервал cron до `*/30`).

## Формат алертов

Бот №1:
```
🔴 Исходящий перевод с командного кошелька
⚠️ Депозит на биржу Binance

💰 1.25M XYZ (~$312,500)
⛓ ethereum · блок 21034567
От: 👥 Treasury (0x1234…abcd)
Кому: 🏦 Binance [Binance 14] (0x28c6…1d60)
🔗 Транзакция
```

Бот №2:
```
📥 ЗАВОД на биржу Binance

💰 500,000 XYZ (~$125,000)
⛓ ethereum · блок 21034570
От: 0x9f2c…77aa
Кому: 🏦 Binance [Binance 14] (0x28c6…1d60)
↪️ Через депозитный адрес, исходный отправитель: 0x5e1a…0b3c (tx)
🔗 Транзакция
```
Также: `📤 ВЫВОД с биржи`, `🔀 Перевод между биржами Binance → OKX`; внутренние переводы одной биржи (hot→cold) по умолчанию не шлются.

## Как определяются биржи и депозитные адреса

* База `data/cex_addresses.json` собирается скриптом `scripts/update_labels.py` из открытых GitHub-датасетов и обновляется workflow-ом раз в неделю. Из неё автоматически исключаются контракты-токены, DEX-роутеры, деплоеры, стейкинг-контракты — они дают ложные срабатывания.
* Свои адреса бирж: `data/cex_addresses_custom.json` (`{"0x...": ["Binance", "Binance hot 99"]}`) или `exchanges.extra_addresses` в конфиге.
* У каждого пользователя биржи свой **депозитный адрес**, их нет в публичных базах. Деньги с него биржа «свипает» на hot-wallet — этот свип бот и ловит как завод. Если до этого бот видел крупный перевод *на* этот депозитный адрес, в алерте будет указан исходный отправитель (`track_deposit_addresses`).

## Расширение

* Новая EVM-сеть — добавьте блок в `config/chains.yaml` (chain_id, RPC, explorer, slug DefiLlama).
* Свои API: любой JSON-RPC URL (Alchemy/Infura/QuickNode/своя нода) в `chains.<сеть>.rpc_urls` конфига бота — он будет основным, публичные останутся резервом.
* Не-EVM сети (Solana, Tron, TON) требуют отдельного источника транзакций — архитектура (`BaseAlerter.fetch_transfers`) позволяет добавить их адаптером.

## Тесты

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```
