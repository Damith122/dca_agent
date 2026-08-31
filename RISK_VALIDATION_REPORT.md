# Risk admission repair and paper validation

This patch does **not** establish profitability. Do not merge it into an
auto-deploying live branch as a profitability release. No production Railway
variables, exchange settings, positions or orders were changed during this work.

## Evidence inspected

Base code: `75c1d0dc0a9c19115df6611d0a82ba0b217a0ab1`.
Railway deployment `aaa0c25d-3be3-47d1-b2b7-656076965ea8` stopped on
2026-08-31 at 19:46:25 UTC. Last status: both symbols FLAT, reported balance
$15.26. That is a historical log, not a fresh exchange account snapshot.
The deployment-list API resolved the correct deployment; Railway's status API
returned a stale July failed deployment. OAuth exposes variable names, not
current values; effective values below come from startup/runtime logs.

Startup: SOL and SUI, two simultaneous positions, $3 margin x 20 leverage =
$60 entry each, one DCA step, entry threshold 0.60. Both symbols' exchange
minimum notional was $5 in that run. Three DCA trades closed on the 15%
liquidation-distance warning. The guard remains enabled and unchanged.

The two trade JSONL files contain 79 rows: 42 negative simulated exit IDs,
one unknown exit ID, and 36 positive exit IDs. For the latter, recorded net
PnL is -$2.836131 (11 wins, 25 losses; gross -$1.702640, commissions
$1.133491). These are logged round trips, not an independent exchange income
reconciliation; funding is not included. Never train/evaluate profitability by
blindly combining all 79 rows.

The last 10 positive-ID trades (close times after 14:55 UTC on August 31)
sum to -$1.618654, including $0.543134 commissions. Removing the three
liquidation-buffer outcomes would not turn the remaining recorded trades
profitable. It would also be an invalid backtest: blocking DCA changes the
whole subsequent position and exit path.

## What changed

- **Before an exposure increase:** read a fresh, read-only V2 account snapshot.
  Count gross exposure across symbols, including unmanaged positions and local
  fills not yet visible in the snapshot. Reserve the configured liquidation
  warning distance plus 3 percentage points of headroom, a 1% maintenance
  floor and 0.2% cost reserve. Require free initial margin separately. No
  credit for positive unrealized profit or assumed long/short diversification.
- **Race protection:** serialize entry/DCA admission, reject queued stale
  decisions, and block additions while any tracked order is pending. Recheck
  position identity, state, quantity, synchronization and daily limits after
  the account read. Fail closed on unavailable/malformed data, unsupported
  margin modes or existing opening-order margin. Limit account checks to one
  per 10 seconds, across all symbols. Exits never acquire this lock.
- **Daily limits:** combine the managers' recorded daily realized PnL. The
  daily loss limit now blocks new entries even if continuous mode is enabled.
  With the exposure guard enabled, it also blocks further DCA additions.
  Existing exits remain active. Funding/unmanaged trades are not yet included
  in this daily ledger, and open risk can still overshoot the realized limit.
- **DCA disabled:** `MAX_DCA_STEPS=0` no longer divides by zero in risk scoring
  or feature construction. Valid startup range is 0..5.
- **Paper isolation:** default `DRY_RUN=true`; fresh `PAPER_LIVE_<run-id>` or
  `PAPER_TESTNET_<run-id>` state for each process. Simulated symbols share one
  cash ledger, fees debit that ledger, and simulated entry orders reserve a
  portfolio slot. No authenticated account streams/polling in paper mode.
- **Keyless launcher:** `paper_validation.py` discards inherited bot overrides,
  loads the paper profile, then forces paper mode and blank Binance/GitHub
  credentials before importing the bot. It runs for a bounded duration and
  cannot switch itself into live trading.
- **Audit utility:** `audit_trade_evidence.py` separates simulated/unknown exit
  IDs, deduplicates positive exit IDs and verifies gross minus fees equals
  logged net. It makes no prediction about changed execution.

The admission reserve is a conservative policy, **not** Binance's exact
liquidation formula. Maintenance tiers, fast price moves, manual actions,
multiple bot processes and exchange delays remain risks. It currently supports
USDT single-asset, one-way, cross-margin positions. Do not disable exchange
stops or widen loss limits to make a rejected order pass.

Account schema reference: [Binance Account Information V2](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Account-Information-V2).
V2 notional is derived from quantity, entry price and unrealized PnL, because
the documented V2 position schema does not include V3's notional field.

## Configuration changes

No Railway values were written. This table describes the supplied **paper
candidate**, not a live recommendation or an optimized strategy.

| Variable | Last observed run / old default | Paper candidate |
|---|---|---|
| DRY_RUN | false | true, forced by launcher |
| USE_TESTNET | false | false: real market data, simulated orders |
| LIVE_TRADING_CONFIRMATION | enabled for live | false, forced |
| I_UNDERSTAND_THIS_IS_REAL_MONEY | enabled for live | no, forced |
| INITIAL_ENTRY_USDT | 3 | 2 |
| LEVERAGE | 20 | 5 ($10 initial notional) |
| MAX_ACTIVE_TRADES | 2 | 1 |
| MAX_DCA_STEPS | 1 | 0 |
| ENTRY_SCORE_THRESHOLD | 0.60 | 0.75; not proven profitable |
| CONTINUOUS_24_7_TRADING | old default true | false; also new default |
| DAILY_PROFIT_TARGET_USDT | 1.00 | 0.50, a halt threshold, not expected income |
| MAX_DAILY_LOSS_USDT | 2.50 | 0.30 |
| USE_POST_ONLY_LIMIT | true in observed maker fills | false; paper simulator models market fills |
| DRY_FILL_SLIPPAGE_BPS | old default 1 | 2 per fill |
| SL_ATR_MULT | runtime override not fully known | 2.5 |
| PROFIT_LOCK_SLIPPAGE_ATR_MULT | runtime override not fully known | 0.25 |
| PAPER_START_BALANCE_USDT | hardcoded paper $500 | new, 15 |
| EXPOSURE_GUARD_ENABLED | absent | new, true by default |
| EXPOSURE_HEADROOM_PCT | absent | new, 0.03 |
| EXPOSURE_MAINTENANCE_FLOOR_PCT | absent | new, 0.01 |
| EXPOSURE_COST_RESERVE_PCT | absent | new, 0.002 |
| EXPOSURE_RECHECK_SEC | absent | new, 10 |

Dollar overrides explicitly cleared in the paper profile:
`MAX_STOP_LOSS_USD`, `MIN_STOP_LOSS_USD`, `TARGET_PROFIT_USD`,
`MIN_TARGET_PROFIT_USD`, `MAX_TRADE_NET_LOSS_USDT`,
`MAX_TRADE_EXIT_BUFFER_USDT`, `SMART_ORDERFLOW_EXIT_MIN_LOSS_USD`,
`SMART_ORDERFLOW_EXIT_MAX_LOSS_USD`, `DCA_RESCUE_BREAKEVEN_MIN_NET_USD`.
Blank values invoke their existing defaults/scaling; consult the startup
banner for the resulting values. `MIN_STOP_LOSS_USD` is an existing legacy
setting; this patch does not change stop formulas. All inherited bot settings
are ignored by the dedicated launcher, so omitted settings use code defaults.
Paper runs do not upload to GitHub. Collect local records before destroying
their container; each restart intentionally begins a new experiment.

## Validation and remaining work

Local: 10 pure admission tests, 13 mocked manager integration tests and one
launcher-isolation test pass. The simulated-fill regression has 97 passing
checks. Syntax compilation and whitespace checks pass.

The local environment lacks `aiohttp`, and package installation was blocked.
Integration/legacy checks used an explicitly network-disabled import shim;
they are **not** validation of the real HTTP transport. The added GitHub CI
workflow installs the actual pinned-range dependencies and runs the new tests
plus the dry-fill regression without keys.

A 17-script baseline comparison found existing failures in DCA resync/spacing,
loss-budget fixtures and environment/transport checks; not a clean full suite.
Existing failures are not represented as passes. This patch only updates three
structural assertions for the moved submission function, added eighth signed
read method and explicitly live missing-key boot check. It does not relax their
behavioral safety assertions. Live-release readiness remains unproven.

Reproduce:

```sh
pip install -r requirements.txt
python -m unittest -v test_exposure_guard.py test_admission_integration.py test_paper_launcher.py
EXPOSURE_GUARD_ENABLED=false python test_dry_fills.py
python paper_validation.py --minutes 60
python audit_trade_evidence.py /path/to/trades_log_LIVE_SOLUSDT.jsonl /path/to/trades_log_LIVE_SUIUSDT.jsonl --since 2026-08-31T14:55:00Z
```

Before considering real funds: resolve baseline test failures, collect several
independent market periods with frozen settings, and evaluate complete equity
including open positions, fees, funding and adverse execution costs. Use
chronological holdout periods; never choose thresholds on the evaluation data.
Require positive results after costs with uncertainty/drawdown estimates, not
one profitable day. A 60-minute paper run validates plumbing only. There is no
minimum trade-count shortcut that can guarantee a profitable next month.
