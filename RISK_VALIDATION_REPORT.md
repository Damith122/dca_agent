# Risk admission repair and paper validation

This patch does **not** establish profitability. It is not a live profitability
release. After explicit user approval, the fixes were included in `main` for
keyless paper deployment. Exchange settings and orders were not changed.
See the deployment update below for the Railway changes and current scope.

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

Initially no variables were changed on the original `dca_agent` Railway service.
After approval, three safety variables were changed as detailed below. This
table describes the supplied **paper candidate**, not a live recommendation
or an optimized strategy; most values are set inside the paper launcher,
not written over the saved Railway variables.

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

Local: 10 pure admission tests, 14 mocked manager integration tests and one
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

## Hosted validation status

[GitHub CI run 33436469987](https://github.com/Damith122/dca_agent/actions/runs/33436469987)
passed on commit `1969d12c581880ab3fc697ea74e7c740afe8a37e` using actual
dependencies, including the 24 new tests and 97 dry-fill checks. This resolves
the local dependency limitation for those checks, not the wider legacy suite.

A separate Railway service `dca-agent-paper-validation` was created from
`codex/paper-validation-run`; the original stopped `dca_agent` service was not
changed. Its configured command is `python paper_validation.py --minutes 60`
and restart policy is `NEVER`. Only this paper service received these variables:
`DRY_RUN=true`, `USE_TESTNET=false`, `LIVE_TRADING_CONFIRMATION=false`,
`I_UNDERSTAND_THIS_IS_REAL_MONEY=no`, and empty `BINANCE_API_KEY`,
`BINANCE_API_SECRET`, `GITHUB_TOKEN`. Its launcher also clears inherited
settings and loads the documented paper profile.

The create-service API initially reported an unexpected `main` commit in build
metadata despite its configured source being the paper branch. No credentials
were supplied. A subsequent branch push selected commit `38d4443745de7e7251d282aeb8b254de29a8a5ef`
correctly. Deployment `ab63c997-574c-4a54-a296-a6bc3681dda2` then logged
`Dry-run: True`, a fresh PAPER_LIVE namespace, $15 paper cash, $10 initial
notional, DCA=0, threshold 0.75 and +$0.50/-$0.30 daily limits.

However, the new service defaulted to San Francisco (`sfo`). Binance denied
the initial public time request with HTTP 451 (restricted location). No market
forward test or trade completed. The deployment ended CRASHED with restart
policy NEVER; it is not running. No location restriction was bypassed.

The PR also corrects an old misleading mainnet warning that printed even when
DRY_RUN was true, and stops immediately on HTTP 451 rather than retrying it.
The latter has an additional offline regression, bringing the new-test total
to 25. [CI run 33437377319](https://github.com/Damith122/dca_agent/actions/runs/33437377319)
passed all 25 new tests and 97 fill checks on final repair commit `52f493c`.

## Approved paper deployment update — 2026-08-31 UTC

The user explicitly approved updating `main` and deploying the existing
`dca_agent` service for the bounded paper test. The repair was fast-forwarded
to `main`; [PR #1](https://github.com/Damith122/dca_agent/pull/1) is merged.
The production source remains `Damith122/dca_agent`, branch `main`, in its
existing region. No region, proxy or market-data endpoint was changed.

Saved Railway variables changed: `DRY_RUN=true`,
`LIVE_TRADING_CONFIRMATION=false`, `I_UNDERSTAND_THIS_IS_REAL_MONEY=no`.
All Binance API credentials and the GitHub token were left unchanged and
were not read. The launcher removes them from its own process environment.
The service configuration is staged with start command
`python -u paper_validation.py --minutes 60` and restart policy `NEVER`.
`Procfile` now uses that same keyless command, and CI runs on `main` pushes.

The first service-scoped redeploy (`be567eb4-9f90-456e-9b5b-e2b9d481cfb1`)
reused the previous commit `75c1d0d` and its old start command. The disabled
live confirmation gate stopped it before account activity. A Railway
SUCCESS status here means a clean process exit, not a running paper test.
Do not reuse historical deployment images expecting new source code.

An environment-wide staged-change deployment was not performed: the safety
reviewer rejected its scope. Only the named service's source/configuration
is being prepared. This document does not claim a completed forward test.
Current deployment IDs, runtime verification and remaining blockers are
recorded in the PR description to avoid restarting experiments for log-only
documentation edits. A 60-minute test is a plumbing check, not proof of profit.


## Fee-net validation update — 2026-09-02 UTC

The completed 24-hour keyless paper run produced 17 closed trades (9 wins,
8 losses). Simulated equity fell from $15.00 to $14.77640552: net
`-$0.22359448` (`-1.49%`). The average win was about `+$0.0190`; the
average loss was about `-$0.0494`, so one loss erased about 2.6 wins. Gross
price PnL was also negative (about `-$0.058`); approximately `$0.1657` of
simulated fees made it worse but did not create the loss. SOL contributed
about `-$0.1288` and SUI about `-$0.0948`. No position remained open and
no real order was sent.

Offline replay could not find a fixed stop, take-profit, maximum hold,
profit-lock, score threshold, EMA-structure gate or four-hour direction gate
that was fee-net positive in both the first and second chronological halves.
This rules out treating an exit-parameter change as evidence of an edge.

A fresh 12-month Binance public-funding walk-forward (1,096 settled funding
prints per symbol) also invalidated the previously attractive carry estimate.
At VIP0-style 30 bps full-cycle cost, annualised return on capital was:
BTC `+0.17%`, ETH `-0.04%`, SOL `-1.31%`, SUI `-0.57%`; the four-symbol
mean was `-0.4%` and only 1/4 symbols was profitable. Deployment
`34e45438-bde1-453c-81ba-8ed3dae2fb9c` completed this keyless calculation.

Even an optimistic 19 bps cycle (7.5 bps per spot side, assuming the BNB fee
discount, plus 2 bps per perpetual side, assuming maker fills) produced only:
BTC `+0.83%`, ETH `+0.48%`, SOL `-0.65%`, SUI `+0.16%`; mean
`+0.2%` annualised. Deployment
`47e78ddc-172c-4ef3-9cdb-246f11fed4de` completed that keyless calculation.
Those discounts/fills are assumptions, not verified account entitlements,
and the repository does not contain a production-safe two-leg spot/perpetual
executor. At $15, a 0.2% annual return is roughly $0.03 per year; it cannot
support a $0.30 daily target.

The directional bot is therefore not live-ready and must not be enabled by
raising leverage or position size. `main` is intentionally left on the
one-shot funding validation command, Railway restart policy is `NEVER`, and
the last deployment exited successfully. Railway `SUCCESS` here means the
validation process ended; it does not mean a profitable or running bot.
