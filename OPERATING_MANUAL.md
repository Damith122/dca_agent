# dca_agent — Operating Manual

Written 2026-08-30. This is the reference for running and maintaining the bot
without further code changes. It documents what the system does, which knob
governs which behaviour, how to read its logs, and what its evidence actually
shows.

---

## 1. Status, stated plainly

Read this section once before going live, then act as you see fit.

**What has been fixed and verified this cycle:**

| Component | Was | Now |
|---|---|---|
| `trend_conf` | one constant value | 21,962 distinct values, median 0.430 |
| `tp_hit_p` | saturated, IC −0.0007 | median 0.173, IC **+0.1671** vs its own label |
| `noise_p` | median 0.988, 96.5% > 0.90 | median 0.841, 37.5% > 0.90, 480/501 distinct |
| `success_p` | 0.50 fallback, starved | **still 0.50** — see below |
| DRY_RUN fills | never filled, no trade ever closed | closes trades against real prices |
| Feature recorder | 3.2h of data per 24h runtime | continuous, `dropped=0`, hourly GitHub shards |

**What the evidence says about profitability:**

The complete live trading record of this system is 5 simulated round trips
(NEARUSDT, 2026-08-30, 02:40–05:10 UTC):

```
trades 5   wins 1 (20%)
gross PnL  -0.1023 USDT   (-5.1 bps per trade, BEFORE fees)
fees        0.1968 USDT   ( 9.8 bps per trade)
NET        -0.2992 USDT   (-15.0 bps per trade)   t = -2.21
```

The strategy lost money *before* costs. Fees made it worse; they are not the
cause. Average absolute move captured was 12.7 bps against a 9.8 bps round
trip, so fees consume 77% of the typical move.

Seven strategy families were tested and found null during this work:
Martingale DCA, tick mean-reversion, orderbook imbalance, Donchian breakout,
XGBoost on engineered features, cointegration pairs, cross-sectional
long/short. Two things survived: volatility clustering (corr(atr_pct,
|r3600|) = +0.286 — predicts move *size*, not direction, so not directly
tradable) and funding carry (5.5–6.3% annualised, delta-neutral, a different
system to this one).

n = 5 is far too small to be conclusive. But it is not "unknown" either —
the little evidence there is leans negative.

---

## 2. What actually learns from what

This matters for operating decisions, and it is easy to get wrong.

**The feature recorder does not train anything.** `feature_log_*.jsonl` is a
research archive. It is written to disk, pushed to GitHub, and read only by
the offline analysis scripts (`analyze_feature_log.py`, `diagnose_live.py`).
No part of the live model reads it back. Accumulating more of it improves
*your ability to analyse later*; it does not make the bot smarter tomorrow.

**The four online heads learn per tick, from price only:**

| Head | Label | Trains on |
|---|---|---|
| `trend` | forward return over 10 ticks (~2.5s) | every tick |
| `noise` | \|forward return\| < `NOISE_LABEL_MULT` × EWMA of \|move\| | every tick |
| `tp_hit` | \|forward return\| ≥ `TP_HIT_LABEL_MULT` × EWMA of \|move\| | every tick |
| `quality` | realised reward of a closed trade | closed trades only |

**`success_p` learns ONLY from closed trades.** It is reached from exactly one
place — `_on_close_filled()` → `brain.learn_success()`. No closed trade, no
label, ever. This is why it sat at 0.50 for the entire project: under DRY_RUN
no order filled, so no position opened, so none closed. The simulated-fill
engine (§6) is what fixed that.

`success_p` reports its 0.50 fallback until a symbol has
`BRAIN_HEAD_MIN_SAMPLES` (20) closed trades **with both outcome classes
present**. Current counts: NEAR 7, and 1 / 11 / 12 across SOL, ETH, SUI.

**The heads use SGD and EWMAs, so they forget.** They track a moving target
rather than accumulating forever. More runtime keeps them current; it does
not monotonically improve them, and it cannot manufacture an edge that is not
in the data.

---

## 3. Deploy and operate

- **Host:** Railway, project `discerning-integrity`, service `dca_agent`,
  environment `production`.
- **Source:** GitHub `Damith122/dca_agent`, branch **`main`**. Railway
  redeploys automatically on push to `main`.
- **Entrypoint:** `main.py` → `dca2.run_forever()`.
- **State:** `brain_LIVE_<SYMBOL>.pkl`, `dca_state_*.json`,
  `trades_log_*.csv/.jsonl`, `performance_stats_*` — synced to the
  `brain-state` branch via `GITHUB_TOKEN`, so they survive container
  restarts. The container filesystem is ephemeral; anything not synced is
  lost on redeploy.

Setting any Railway variable triggers a redeploy unless you skip deploys.
A redeploy costs ~2 minutes of adaptive-label warm-up (§7) and nothing else —
brain state is restored from GitHub.

---

## 4. Going live with real money

Two gates must both be satisfied, by design, whenever `USE_TESTNET=false`:

```
I_UNDERSTAND_THIS_IS_REAL_MONEY = yes      (exact string "yes")
LIVE_TRADING_CONFIRMATION       = true
```

Missing or wrong, the process refuses to start. That is intentional; do not
remove it.

**API keys come from Railway variables only** — `BINANCE_API_KEY` /
`BINANCE_API_SECRET`. Never hardcode them in a file; the repo is on GitHub.
On the Binance key, enable Futures trading and **disable withdrawals**. IP-
restrict it to Railway's egress if you can.

### The switch

```
DRY_RUN = false
```

That is the only change required. Everything else is already configured for
live. Confirm from the boot banner that the `*** DRY RUN MODE ***` line is
**gone** — if it still prints, no real order will ever be sent.

### Recommended live configuration

Sized so a losing run is survivable rather than sudden. At the measured
−15 bps per trade and ~1 trade/hour, this is a slow bleed you can watch.

| Variable | Suggested | Why |
|---|---|---|
| `INITIAL_ENTRY_USDT` | `5` | smallest that clears Binance min-notional on all four symbols |
| `LEVERAGE` | `3` | currently 20; at 20x a 5% adverse move is a liquidation |
| `MAX_ACTIVE_TRADES` | `1` | one position across the whole watchlist |
| `MAX_DCA_STEPS` | `1` | already 1 — do not raise it; martingale sizing is what makes losses non-linear |
| `MAX_DAILY_LOSS_USDT` | a number you would shrug at | hard stop for the day |
| `MAX_TRADE_NET_LOSS_USDT` | `0.15`–`0.50` | per-trade ceiling; arms the rr-stop |
| `USE_POST_ONLY_LIMIT` | `true` | maker entry at 0.02% instead of taker 0.05% — saves 3 bps per round trip |

**Keep the recorder on** (`FEATURE_RECORDER_ENABLED=true`) — it is
independent of trading and costs nothing.

---

## 5. Variable reference — the ones that matter

Everything below is read from the environment at import, with the code
default in `config.py`. Names not listed here are documented inline in
`config.py`, which carries the reasoning for every value.

### Entry gating — in the order they fire

An entry must survive **all** of these, in this sequence:

1. **`FEATURE_RECORDER_ENABLED`** — irrelevant to trading, listed only so you
   know it is not a gate.
2. **Portfolio slot** — `MAX_ACTIVE_TRADES`. Blocks if another symbol holds
   the slot.
3. **Cool-off** — `COOL_OFF_PERIOD_MINUTES` (15). Armed after any fee-net
   losing close. Blocks new entries entirely, then keeps tightened orderbook
   thresholds for an equally long window.
4. **`TRADE_COOLDOWN_SEC`** (60) — minimum gap between trade actions.
5. **Dead-market floor** — `LOW_VOLATILITY_ATR_PCT_THRESHOLD` (0.0008).
   **This is currently the binding gate: 87% of ticks fall below it.**
   A hard block, evaluated before the score.
6. **Counter-momentum block** (SIDEWAYS only) — blocks a proposed side that
   fights a meaningful move.
7. **Composite score ≥ threshold** — see below.
8. **Orderbook / flow guard** — `ENABLE_ORDERBOOK_GUARD`,
   `ORDERBOOK_IMBALANCE_THRESHOLD`.

### The composite entry score

```
score = 0.30*brain_confidence + 0.20*trend_confidence + 0.12*volume_confirmation
      + 0.10*volatility_fit   + 0.13*momentum         + 0.10*regime_fit
      - 0.05*risk_score
```

where

```
brain_confidence = (0.35*trend_conf + 0.35*success_p + 0.30*tp_hit_p)
                 * (1 - 0.5*noise_p) * (1 - 0.4*risk_score)
```

Note `success_p` is pinned at 0.5, contributing a fixed 0.175 to the inner
blend and capping `brain_confidence` around 0.62 even at best.

**Thresholds are regime-dependent:**

| Regime | Variable | Live value |
|---|---|---|
| SIDEWAYS | `SIDEWAYS_ENTRY_SCORE_THRESHOLD` | **0.85** |
| everything else | `ENTRY_SCORE_THRESHOLD` | **0.60** |

The 0.85 is deliberately an **off-switch**, not a filter: it sits above the
regime's structural ceiling of 0.84, so no SIDEWAYS entry can qualify. It was
set on 2026-08-23 after six SIDEWAYS trades went 0W/6L with an average
maximum-favourable-excursion of 0.020% — three of them never ticked in our
favour at all. Disabling SIDEWAYS turned −0.3080 into +0.1602 over that
window. Lowering it re-enables a population of trades known to be near-random.

Observed score range in live SIDEWAYS conditions: median 0.303, max 0.616.

### Exits

| Variable | Default | Effect |
|---|---|---|
| `TAKE_PROFIT_PCT` | 0.0035 | base TP; expanded up to `TAKE_PROFIT_MAX_PCT` in vol/trend |
| `HARD_STOP_PCT` | 0.02 | catastrophic stop |
| `MAX_TRADE_NET_LOSS_USDT` | — | fee-net per-trade loss ceiling; drives the `rr-stop` |
| `MAX_HOLD_TIME_*` | on | time-based exit |
| `PROTECTIVE_STOP_ENABLED` | on | exchange-native STOP_MARKET via the Algo API |

### Model labels (changed this cycle — see §7)

| Variable | Default | Effect |
|---|---|---|
| `TP_HIT_LABEL_ADAPTIVE` | `true` | threshold scales to the horizon's own moves |
| `TP_HIT_LABEL_MULT` | `1.2` | → ~20% base rate |
| `NOISE_LABEL_ADAPTIVE` | `true` | band scales to the horizon's own moves |
| `NOISE_LABEL_MULT` | `0.5` | → ~73% base rate; **0.26 would give ~50%** |
| `*_LABEL_MIN_SAMPLES` | `500` | warm-up before the adaptive form engages |

### Simulated fills (DRY_RUN only — inert when `DRY_RUN=false`)

| Variable | Default | Effect |
|---|---|---|
| `DRY_FILL_ENABLED` | `true` | fill DRY_RUN orders against real prices |
| `DRY_FILL_SLIPPAGE_BPS` | `1.0` | adverse slippage per fill |
| `DRY_FILL_TAKER_FEE_PCT` | `0.0005` | commission per fill |
| `DRY_FILL_MIN_DELAY_SEC` | `0.0` | extra latency beyond "a later tick" |

### Data retention

| Variable | Default | Effect |
|---|---|---|
| `FEATURE_RECORDER_ENABLED` | — | master switch for recording |
| `FEATURE_LOG_RETENTION_ENABLED` | `true` | delete local shards after upload |
| `FEATURE_LOG_RETAIN_LOCAL_HOURS` | `6.0` | keep uploaded shards this long |
| `FEATURE_LOG_MAX_LOCAL_MB` | `256.0` | hard disk cap |

GitHub copies are never touched by retention.

---

## 6. Simulated fills — how DRY_RUN produces real labels

`dry_fills.py` fills DRY_RUN orders against the real prices already being
streamed, so `success_p` gets labels with nothing at risk. Two rules keep it
honest:

1. **Never fills on the submitting tick.** Enforced on the decision-tick
   *counter*, not elapsed time — a submit and a resolve inside one tick are
   microseconds apart, which a clock-based rule would wave through. Deciding
   and executing on the same observed price is lookahead, and it is the usual
   way a paper harness invents an edge.
2. **Charges the cost.** Adverse slippage in both directions plus commission.

Fills are delivered by synthesising a Binance `ORDER_TRADE_UPDATE` and
dispatching it through `handle_order_update()` — the same entry point the live
websocket uses — so fees, realized PnL, close-dedup, the trade log and Brain
reinforcement all run the production path. There is no second implementation
to drift.

**Two known fidelity gaps**, in opposite directions:

- Entries are placed post-only (maker, 0.02%) but the simulator charges the
  taker rate (0.05%). **Fees are overstated by ~3 bps per round trip** —
  conservative.
- A real GTX order can rest unfilled and be cancelled; the simulator always
  fills it. **Trade count is optimistic.**

---

## 7. Reading the logs

### `[status]` — every 20s per symbol

```
[status] price=1.8815 status=FLAT side=None dca_step=0/1 avg_entry=None qty=0.0
         balance=500.00 USDT trades=15 session_pnl=-0.4806 regime=WEAK_TREND
         atr%=0.042 brain=[READY] confidence=0.12 success_p=0.50 tp_hit_p=0.10
         risk=0.13 github_sync=[on, last_push=30s ago]
```

`trades` and `session_pnl` are cumulative from persisted state, not this run.

### `[entry-debug]` — throttled, the decision trace

Carries every score component, both label base rates, all four head
probabilities including `noise_p`, the threshold in force, and the reason:

```
decision=dead_market_blocked                 → ATR floor; score never consulted
decision=sideways_counter_momentum_blocked   → side fights the move
decision=score 0.4172 below threshold 0.8500 → reached scoring, fell short
```

`brain_ready=[trend=READY success=UNRELIABLE(7) tp_hit=READY(1.6M) noise=READY(97k)]`
— the number in brackets is that head's label count. `success` is the one to
watch; it needs 20.

### `[dry-fill]` / `ENTRY FILLED` / `POSITION CLOSED` / `[brain] reinforced`

The full round-trip chain. `POSITION CLOSED` carries
`PnL=... (raw=..., fees=... [actual])` — `raw` is gross, `PnL` is net.

### `[feature-log:SYMBOL]`

```
taken=1948 finalised=1594 written=1583 pending=354 dropped=0 shard=...jsonl
```

`dropped` must stay 0. `finalised` lags `taken` by one hour because the r3600
horizon needs a full hour to close — that is normal, not a stall.

---

## 8. Symptom → knob

| Symptom | Cause to check | Knob |
|---|---|---|
| No trades at all | `dead_market_blocked` dominating `[entry-debug]` | `LOW_VOLATILITY_ATR_PCT_THRESHOLD` (0.0008) |
| No trades, all SIDEWAYS | 0.85 off-switch | `SIDEWAYS_ENTRY_SCORE_THRESHOLD` — see §5 warning |
| No trades, `decision=score ... below` | score short of the bar | `ENTRY_SCORE_THRESHOLD` |
| Trades, but all tiny losses | fees > edge | raise `TAKE_PROFIT_PCT`; ensure `USE_POST_ONLY_LIMIT=true` |
| `success_p` stuck at 0.50 | fewer than 20 closed trades, or one class only | nothing — it needs trades |
| A head reads a constant | label saturated | check its `*_LABEL_MULT`; a base rate near 0 or 1 is the tell |
| Disk full | retention off | `FEATURE_LOG_RETENTION_ENABLED`, `FEATURE_LOG_MAX_LOCAL_MB` |
| Restart loop on boot | import error, or a mainnet gate unsatisfied | read the traceback in Railway deploy logs |
| One symbol not listed | testnet has a smaller symbol list | it is excluded automatically; `SymbolNotListed` is caught per-symbol |

**After any redeploy**, both adaptive labels fall back to their fixed
definitions for ~500 samples (~2 minutes) while `_label_move_scale` re-warms.
Brief distortion in `noise_p` / `tp_hit_p` right after a restart is expected.

You should **not** see `[brain] noise head rebuilt` after the first
post-upgrade boot. Snapshots now carry `noise_label_version: 2`; if it
reappears, the snapshot is being written by older code.

---

## 9. Emergency procedures

**Stop trading immediately, keep collecting data:**
set `DRY_RUN=true`. Recorder and learning continue; no order reaches Binance.

**Stop everything:** pause or delete the Railway service. Any open position
stays open on Binance — close it manually in the Binance UI.

**Position stuck / local state disagrees with the exchange:** the periodic
`initialize_sync()` reconciles against Binance every `POSITION_RISK_POLL_SEC`
and self-heals. If it does not, restart the service; state is restored from
the `brain-state` branch.

**Model behaving oddly:** delete `brain_LIVE_<SYMBOL>.pkl` from the
`brain-state` branch. The head rebuilds from zero and reports a neutral 0.5
until reliable again. You lose learned state; you do not break anything.

---

## 10. Test suite

`python3 test_<name>.py` — each file is standalone, no pytest required.

**47 of 52 test files pass.** The 5 failures are long-standing and predate
this cycle's work:

```
test_dca_resync_race_fix.py
test_dca_spacing_fix.py
test_dca_time_gate_fix.py
test_fee_net_profitability_guard_fix.py
test_trade_loss_budget_and_dca_gates_fix.py
```

These are stale fixtures, not live defects: they assert DCA and stop
behaviour from before later safety gates were added. `test_dca_spacing_fix.py`
is confirmed to fail because the orderbook-supported-DCA guard withholds the
add its fixture does not supply book data for
(`DCA_REQUIRE_ORDERBOOK_SUPPORT`). The other four assert similar pre-gate
behaviour; I did not individually root-cause each. They have failed
throughout this work and no change made here introduced them.

Fast checks worth running after any edit:

```
python3 test_module_integrity.py      # every module-scope name resolves
python3 test_feature_recorder_fix.py  # every referenced name in trading.py resolves
python3 test_dry_fills.py             # the simulated-fill contract
python3 test_noise_label_fix.py       # the noise label and its head rebuild
python3 test_brain_head_health_fix.py # head reliability gating
```

`test_module_integrity.py` exists because a patch once anchored on a function
that lived in a different file, `str.replace` silently no-oped, and `ast.parse`
was happy — shipping a `NameError` that only fired on a command-line flag.

---

## 11. Offline analysis

Requires `pip install -r requirements-research.txt`. None of these run on
Railway; run them locally against shards pulled from the `brain-state` branch.

| Script | Purpose |
|---|---|
| `diagnose_live.py <shard-dir>` | head health, feature ICs, calibration |
| `analyze_feature_log.py` | forward-return analysis by horizon |
| `edge_requirements.py` | closed-form IC / horizon / Sharpe requirements |
| `risk_simulator.py` | drawdown and ruin probability |
| `backtest_*.py` | the seven strategy families, all null |
| `funding_arb.py`, `backtest_funding.py` | the delta-neutral carry strategy |

`diagnose_live.py` is the one to reach for first: it reads the shards and
reports whether each head is varying, what its base rate is, and its
information coefficient against its own label.

---

## 12. If you want to change direction later

The one measured positive result was **funding carry**: short the
highest-funding perpetuals against a hedge, collect the funding, stay
delta-neutral. 5.5–6.3% annualised on capital in backtest, with the caveat
that spot fees are double futures fees (0.10%/side vs 0.05%) which is what
kills most naive carry constructions.

`funding_arb.py` and `backtest_funding.py` implement and test it.
`fetch_funding_universe.py` downloads the data. It is a fundamentally
different system to this one — position sizing, rebalancing, and hedging
rather than entry timing — and it has not been run live.
