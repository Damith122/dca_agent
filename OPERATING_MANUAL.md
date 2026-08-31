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

**71 closed trades, 2026-08-30 12:15 → 2026-08-31 04:24 UTC.** Full
per-symbol breakdown in §13.

```
71 trades   21 wins (30%)
gross    +0.3503 USDT   ( +1.3 bps per trade)  <- indistinguishable from zero
fees      2.8008 USDT   (-10.0 bps per trade)
NET      -2.4497 USDT   ( -8.7 bps per trade)   t = -3.23   p < 0.01
```

**The strategy has no gross edge.** +1.3 bps is not distinguishable from
zero. The round trip costs 10.0 bps, which turns a zero edge into a
significant loss. This is a conclusion, not a suspicion: at n=71 with
t = −3.23 the negative expectation is established.

The practical consequence: **no configuration change fixes this.** Fewer
symbols, lower leverage, smaller size and different thresholds all scale a
zero edge. The only lever that moves net is execution cost, and the best
reachable version (post-only maker entry, which is now enabled) is
−5.7 bps — still negative.

Seven strategy families were tested and found null during this work:
Martingale DCA, tick mean-reversion, orderbook imbalance, Donchian breakout,
XGBoost on engineered features, cointegration pairs, cross-sectional
long/short. Two things survived: volatility clustering (corr(atr_pct,
|r3600|) = +0.286 — predicts move *size*, not direction, so not directly
tradable) and funding carry (5.5–6.3% annualised, delta-neutral, a different
system to this one).

What has NOT been established: whether some different strategy on this same
infrastructure could work. The infrastructure itself — recorder, heads,
execution, risk envelope, reconciliation — is sound and well tested. It is
the entry signal that has no edge.

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

### Live configuration

**Superseded by §13, which carries the final values actually set in Railway
on 2026-08-31 and the arithmetic behind each one.** The table below is the
earlier generic guidance, kept for the reasoning.

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

| `session_total` disagrees with Binance | a protective stop filled but Binance reported FINISHED with no `actualOrderId` | **fixed 2026-08-31** — the outcome is now resolved against the exchange position and the fill recovered from userTrades. Binance is always the authority; if they ever diverge again, trust the exchange |
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

---

## 13. Final live configuration (2026-08-31) and the evidence behind it

### The 71-trade result

Between 2026-08-30 12:15 and 2026-08-31 04:24 UTC the simulated-fill engine
produced **71 closed trades** across all four symbols. This is the first
sample large enough to draw a conclusion from.

```
sym      n   W   win%   net USDT     gross     fees   net bps  gross bps   t-stat
=================================================================================
SOL     16   7    44%    -0.3184    0.3140   0.6323      -5.1        4.9    -0.92
NEAR    22   7    32%    -0.7884    0.0762   0.8646      -9.1        0.9    -1.76
SUI     20   4    20%    -0.7825    0.0162   0.7992      -9.8        0.2    -1.64
ETH     13   3    23%    -0.5604   -0.0561   0.5047     -11.1       -1.1    -2.47
=================================================================================
ALL     71  21    30%    -2.4497    0.3503   2.8008      -8.7        1.3    -3.23
```

**The gross edge is +1.3 bps per trade — indistinguishable from zero.** The
round trip costs 10.0 bps. Net is −8.7 bps per trade at **t = −3.23**, which
is significant at p < 0.01.

At n=5 this was inconclusive. At n=71 it is not. The strategy does not have
an edge, and the losses are not caused by fees — fees convert a zero edge
into a negative result.

**No configuration change fixes a zero gross edge.** Fewer symbols, lower
leverage and smaller size all scale the same ~zero number. The only lever
that moves net is execution cost:

| execution | net |
|---|---|
| taker in, taker out (as measured) | −8.7 bps |
| post-only maker entry + taker exit | −5.7 bps |
| maker both sides (not reachable — exits are MARKET) | −2.7 bps |

### Symbol selection

Per-symbol differences are **not** statistically distinguishable. Welch t of
each symbol against the pooled rest:

```
SOL   -5.1 bps vs rest  -9.8 bps   t = +0.75
NEAR  -9.1 bps vs rest  -8.6 bps   t = -0.08
SUI   -9.8 bps vs rest  -8.3 bps   t = -0.21
ETH  -11.1 bps vs rest  -8.2 bps   t = -0.52
```

Selecting the best of four needs |t| > ~2.5 after Bonferroni. Nothing is
close. Picking "the two best performers" by PnL would be selecting on noise.

The two symbols were therefore chosen on **structural** grounds that do not
depend on a small sample:

- **Quantity-step granularity at $10 notional.** SUI (step 0.1 × $0.72 =
  $0.07) and SOL (step 0.01 × $101 = $1.01) can both express a $10 order
  precisely. ETH (step 0.001 × $2,430 = $2.43) cannot — a $10 ETH order
  quantises to 0.004 = $9.72 with no room, and smaller sizes fall under the
  $5 minimum notional entirely.
- **ATR relative to the dead-market floor**, which determines whether a
  symbol trades at all. ETH ran at 19% of the floor; SUI 81%, SOL 60%.
- ETH is also worst on both net and gross PnL, so excluding it costs nothing
  even on the noisy measure.

### Position sizing arithmetic

`INITIAL_ENTRY_USDT` is **margin**; notional = margin × leverage. With
`MAX_DCA_STEPS=1` and `DCA_MULTIPLIER=1.6`, a fully-DCA'd position reaches
2.6× the initial notional.

```
LEVERAGE=2, INITIAL_ENTRY_USDT=5      -> initial notional $10
fully DCA'd                            -> $26 notional, $13 margin
account $17                            -> $4 buffer (24%)
$10 notional vs $5 exchange minimum    -> 2x headroom on both symbols
```

`MAX_ACTIVE_TRADES=1` rather than 2: two concurrent positions would halve
the margin available to each, pushing initial notional to ~$6.50 — close
enough to the $5 floor that ordinary price movement could make an order
illegal — and would leave no margin buffer at all if both DCA'd. The
portfolio-capacity gate was never observed blocking an entry in any log
window analysed, so the second slot buys little.

### Every USD-denominated threshold was set explicitly — which was a mistake

> **CORRECTION (2026-08-31).** An earlier version of this section told you to
> rescale these thresholds by hand whenever `INITIAL_ENTRY_USDT` or
> `LEVERAGE` changes, and said they "are not percentages and will not
> follow." **That was wrong.** Thirteen of them are declared through
> `_notional_scaled()` in `config.py`, which expresses each one as a fraction
> of `ENTRY_NOTIONAL_USDT` and therefore *does* follow position size
> automatically. They only stopped following because the table below was
> pushed into Railway as explicit environment variables, and an explicit
> value always wins over the derived one — that is the documented escape
> hatch, and setting it is precisely what disables the auto-scaling. The
> startup banner has been telling us so all along, printing `OVERRIDDEN`
> beside ten of the thirteen. **Do not hand-rescale these. Clear the
> override instead** (set the Railway variable to an empty string —
> `_env_raw()` treats empty and whitespace-only as absent, by design) and the
> correct value is derived. See §15.

The concern that motivated the table was real even if the remedy was not.
Position notional dropped from ~$40 to ~$10, and a threshold pinned in
absolute USDT does silently change meaning in percentage terms when notional
moves: `MAX_TRADE_NET_LOSS_USDT=0.15` would have been 150 bps of a $10
position — beyond the 2% hard stop — so the rr-stop would never have fired
and the risk/reward envelope would have been inert. Pinning every value by
hand avoided that, but only until the next size change.

These were the explicit values set against the $10 notional:

| Variable | Value | = bps of $10 |
|---|---|---|
| `MAX_TRADE_NET_LOSS_USDT` | 0.04 | 40 |
| `MIN_STOP_LOSS_USD` | 0.02 | 20 |
| `MAX_STOP_LOSS_USD` | 0.06 | 60 |
| `TARGET_PROFIT_USD` | 0.035 | 35 (matches `TAKE_PROFIT_PCT`) |
| `MIN_TARGET_PROFIT_USD` | 0.02 | 20 |
| `SMART_ORDERFLOW_EXIT_MIN_LOSS_USD` | 0.01 | 10 |
| `SMART_ORDERFLOW_EXIT_MAX_LOSS_USD` | 0.05 | 50 |
| `DCA_RESCUE_BREAKEVEN_MIN_NET_USD` | 0.01 | 10 |
| `MAX_TRADE_EXIT_BUFFER_USDT` | 0.02 | 20 |

**Superseded — see the correction above.** These are auto-scaling thresholds
that were pinned by explicit overrides. When you change `INITIAL_ENTRY_USDT`
or `LEVERAGE`, clear the overrides rather than rescaling them; the values
below are then re-derived at the new notional. The full list of thirteen, and
the two that are worth overriding on purpose, are in §15.

### The complete live variable set

```
ACTIVE_SYMBOLS                     = SOLUSDT,SUIUSDT
MAX_ACTIVE_TRADES                  = 1
LEVERAGE                           = 2
INITIAL_ENTRY_USDT                 = 5          -> $10 notional
USE_POST_ONLY_LIMIT                = true       -> maker entry, saves ~3 bps
MAX_DAILY_LOSS_USDT                = 1.50       -> ~9% of a $17 account
DAILY_PROFIT_TARGET_USDT           = 1.00
ENTRY_SCORE_THRESHOLD              = 0.60
MAX_TRADE_NET_LOSS_USDT            = 0.04
MIN_STOP_LOSS_USD                  = 0.02
MAX_STOP_LOSS_USD                  = 0.06
TARGET_PROFIT_USD                  = 0.035
MIN_TARGET_PROFIT_USD              = 0.02
SMART_ORDERFLOW_EXIT_MIN_LOSS_USD  = 0.01
SMART_ORDERFLOW_EXIT_MAX_LOSS_USD  = 0.05
DCA_RESCUE_BREAKEVEN_MIN_NET_USD   = 0.01
MAX_TRADE_EXIT_BUFFER_USDT         = 0.02
DRY_RUN                            = true       <- the only remaining switch
```

### Expected outcome at this configuration

At the measured −8.7 bps, with ~2.2 trades/hour on two symbols and $10
notional: **−0.46 USDT/day, about −2.7% of a $17 account per day.** Half the
account in roughly 25 days. With the post-only maker entry now enabled, the
better case is −5.7 bps → −0.30 USDT/day, −1.8%/day, half in ~39 days.

Those are projections from a measured negative edge, not forecasts of ruin —
variance is large and individual days will be positive. But the expectation
is negative and now measured with n=71.

### One trade-off to be aware of

Restricting `ACTIVE_SYMBOLS` to two coins also **halves feature-recorder
coverage** — NEAR and ETH stop being recorded. The watchlist governs both
trading and recording; there is no switch to record more symbols than are
traded. If continuous four-symbol data collection matters more than
restricting trading, keep all four in `ACTIVE_SYMBOLS` and rely on
`MAX_ACTIVE_TRADES=1` to limit exposure to one position at a time — the cost
is that trades will be spread across four symbols instead of two.

---

## 14. Long-term outlook: what this system can and cannot do on its own

Written 2026-08-31, after the first live trading day. Read this before
deciding to leave the bot running unattended for weeks.

### Will it autonomously learn and become profitable?

**No.** Not "probably not" — the mechanism is not there. Four specific
reasons, all verifiable in the code:

**1. Almost nothing in the decision is learnable.** The composite entry
score is

```
score = 0.30*brain_confidence + 0.20*trend_confidence + 0.12*volume_confirmation
      + 0.10*volatility_fit   + 0.13*momentum         + 0.10*regime_fit
      - 0.05*risk_score
```

Every weight is a hard-coded constant in `ENTRY_WEIGHTS`. So are all the
thresholds, the ATR floor, the TP/SL distances, the DCA multiplier, the
position size, the leverage and the symbol list. The only things that move
on their own are four probability estimates, and three of them
(`trend_confidence`, `tp_hit_p`, `noise_p`) feed a single term carrying
0.30 of the score. **The architecture of the decision is frozen. Only its
inputs drift.**

**2. The heads adapt, they do not optimise.** They use SGD with EWMA
scaling: they track a moving target and forget the past. That is the right
design for surviving regime drift, and it is *not* a mechanism for getting
better. A head that has converged is finished improving; it will then just
follow the market around. Adaptation is not optimisation, and neither is
profit-seeking — no head anywhere is maximising PnL.

**3. You cannot learn a signal that is not in the features.** Over 71
trades the gross edge — before any cost — was **+1.3 bps, indistinguishable
from zero**. `success_p` can become an excellent predictor of an outcome
that is itself unpredictable from these inputs, and the answer it converges
to is "about 50/50", which is the truth. Learning cannot create
information that the feature set does not contain.

**4. The learning loop is censored, and that is a hard ceiling.**
`success_p` only ever receives a label from a trade the bot **took**.
Declined setups produce nothing. So it can slowly get better at ranking
*within* the region of feature space it already enters, and it can **never**
discover that a region it rejects was profitable. This is the classic
bandit-censoring problem, and it means autonomous improvement is bounded
by the entry rule that was frozen on day one.

### What it CAN do on its own

| Capability | Mechanism | Real? |
|---|---|---|
| Re-calibrate labels to changing volatility | `*_LABEL_MULT` × EWMA of actual moves | Yes — proven; `noise_p` self-corrected 0.84 → 0.48 |
| Track drift in short-horizon price behaviour | SGD on the four heads | Yes, within the frozen feature set |
| Refuse to trade dead or sideways markets | ATR floor + the 0.85 SIDEWAYS off-switch | Yes — this is doing most of the risk work |
| Cap per-trade and per-day loss | `MAX_TRADE_NET_LOSS_USDT`, `MAX_DAILY_LOSS_USDT` | Yes — verified firing live |
| Recover from exchange quirks | reconciliation, PROTECTION_PENDING, algo-fill recovery | Yes — three separate incidents survived |
| **Find a new edge** | — | **No** |
| **Change how it decides** | — | **No** |
| **Size up when winning** | — | **No** |

### Expected trajectory if nothing is touched

```
as measured, taker  (71 trades)   -0.418 USDT/day   -2.47%/day   half gone in ~28 days
with maker entry (current config) -0.274 USDT/day   -1.62%/day   half gone in ~43 days
```

Individual days will be green. The expectation is not.

On the live sample the picture is unusually clean: average win +0.0274,
average loss −0.0201, so the **break-even win rate is 42.3%** and the bot
achieved **42.9%**. It is sitting precisely on the coin-flip line. That is
what "no edge" looks like from the inside, and it is why it feels
break-even rather than obviously broken.

### What degrades if left alone

- **Absolute-USD thresholds do not follow price or account size.** The nine
  values in §13 were computed for a $10 notional. Change size, leverage, or
  let the account grow, and the risk envelope silently stops matching.
- **Binance changes its API.** This project already hit `-4120` (Algo
  Service migration), `-5022` (post-only rejection) and `-4509` (TIF GTE),
  plus a `FINISHED`-without-`actualOrderId` case that cost a trade in the
  ledger. Expect more.
- **A head can re-saturate** if the market's move distribution shifts far
  enough that a label's base rate collapses toward 0 or 1.
- **Symbols get delisted, fee tiers change, funding regimes flip.**

### The human roadmap

**Tier 0 — hygiene. Do this or do not run it.**

1. **Weekly:** reconcile the bot's `session_total` against Binance trade
   history. The exchange is the authority; the bot's number is a
   reconstruction. They have already diverged once.
2. **Monthly:** run `diagnose_live.py` on the recorded shards. If any head's
   output collapses to a near-constant, or its label base rate falls outside
   roughly 5–95%, that head is dead — the label definition needs rescaling,
   exactly as `tp_hit` and `noise` did.
3. **On any `*** HIGH SEVERITY ***` line:** investigate the same day.
4. **Never raise `MAX_DCA_STEPS`.** Martingale sizing is what turns a bad
   run into a terminal one.

**Tier 1 — the gate that decides everything.**

Do not scale size, add symbols, or loosen thresholds until a feature is
shown to have a **positive out-of-sample information coefficient against
forward returns**. Parameter tuning cannot manufacture an edge; it can only
redistribute a zero one. The recorder archive plus `analyze_feature_log.py`
and `edge_requirements.py` exist precisely to run that test. If no feature
clears the bar, the correct action is to stop trading this strategy, not to
tune it further.

**Tier 2 — structural options, ranked.**

1. **Hold longer.** This is the highest-leverage change that needs no new
   alpha. Cost is fixed per round trip; move size grows with √time:

   | hold | typical \|move\| | cost | cost as % of move |
   |---|---|---|---|
   | ~8 min (current) | 17.5 bps | 7 bps | **40%** |
   | ~32 min | 35 bps | 7 bps | 20% |
   | ~2 h | 70 bps | 7 bps | 10% |
   | ~8.5 h | 140 bps | 7 bps | **5%** |

   It does not create edge — a zero edge stays zero — but it is what makes
   a *small* edge survivable. At the current 8-minute hold, 40% of every
   move is consumed by fees before the strategy is even judged.

2. **Funding carry.** The one thing that tested positive across all this
   work: 5.5–6.3% annualised, delta-neutral, direction-agnostic. It does
   not need a directional edge at all, which is exactly why it survived.
   `funding_arb.py` / `backtest_funding.py` implement it. It is a different
   system — sizing, hedging and rebalancing rather than entry timing — and
   it has never been run live.

3. **Cut cost further.** Maker exits would take the round trip from ~7 bps
   to ~4. Safe for the take-profit leg, **not** for the stop leg — a resting
   stop that does not fill is not a stop.

4. **Stop.** A legitimate outcome. Seven strategy families tested null; the
   infrastructure is genuinely good and the entry signal genuinely is not.

### The honest summary

The engineering is sound: execution, risk envelope, reconciliation,
recorder, four working model heads, tested to 48 passing files. What it
lacks is an edge, and no amount of runtime supplies one. Left alone it will
adapt to drift, defend its downside competently, and slowly bleed the fee
differential. **It is a well-built vehicle with no fuel.** The next phase
is not tuning — it is finding fuel, and the only lead this project produced
is funding carry.


---

## 15. Aggressive configuration (2026-08-31) — operator-directed

This section documents a configuration change that the evidence in §13 and
§14 argues against, made at the operator's explicit instruction after that
evidence was presented and accepted. It is written so the reasoning behind
each number is recoverable, and so reverting is a five-minute job.

### What it is trying to do

Target **≥ $0.50 net profit per day** on a ~$16.92 account. That is 2.96%
per day, which compounds to roughly 4,134,601% per year.

### What the evidence says will happen instead

The measured per-trade result over 71 round trips is **gross +1.3 bps, net
−8.7 bps**, t = −3.23, p < 0.01. The gross edge is statistically
indistinguishable from zero; the net number is the fee line.

Sizing does not create edge — it multiplies whatever expectancy exists, and
this expectancy is negative. Monte Carlo over this configuration returns:

| Configuration | P(ruin) | Median time to ruin |
|---|---|---|
| Previous live config ($10 notional, 1 slot) | 77.9% | — |
| This aggressive config (and every variant tried) | **100.0%** | **36–61 days** |

For contrast: *if* the strategy had a genuine +5 bps net edge, $21 of
notional already produces $0.50/day at **0%** ruin. The binding constraint
has never been position size. It is the sign of the edge.

### The changes

| Variable | Was | Now | Rationale |
|---|---|---|---|
| `INITIAL_ENTRY_USDT` | 5 | **3** | Largest initial margin that lets *both* slots carry a DCA rung |
| `LEVERAGE` | 2 | **20** | Entry notional $10 → $60 |
| `MAX_ACTIVE_TRADES` | 1 | **2** | Two concurrent positions across SOL/SUI |
| `TAKE_PROFIT_PCT` | 0.0035 | **0.004** | 35 → 40 bps. A hard ceiling, not a choice — see below |
| `TAKE_PROFIT_MAX_PCT` | 0.010 | **0.020** | 100 → 200 bps; this is where the expansion lives |
| `DCA_TRIGGER_PCT` | 0.002 | **0.002** | Unchanged — 40 bps base needs no widening |
| `MAX_DAILY_LOSS_USDT` | 1.50 | **2.50** | ≈14.8% of account (see below) |
| `DAILY_PROFIT_TARGET_USDT` | 1.00 | **1.00** | 2× the $0.50/day objective |
| ten `_notional_scaled` overrides | pinned | **cleared** | Re-derive at $60 notional |

`MAX_DCA_STEPS` stays at **1** and `ENTRY_SCORE_THRESHOLD` stays where it is.

### Why $3, and not more

$3 is not a round number chosen for taste. It is the largest initial margin
at which both portfolio slots can still carry their DCA rung on this account:

```
initial            margin $3.00   notional  $60.00
+ DCA #1 (x1.6)    margin $4.80   notional  $96.00
full position      margin $7.80   notional $156.00
x 2 slots          margin $15.60  notional $312.00
balance $16.92  -> free margin $1.32   (8% buffer)
```

`INITIAL_ENTRY_USDT=4` needs $20.80 in that state and is not fundable.
`INITIAL_ENTRY_USDT=8` needs $41.60 and is not fundable at any leverage.

**The buffer is thin, and the failure mode matters.** Confidence-based sizing
(`SIZE_MAX_MULT=1.5`) can scale an entry to $4.50 margin and its DCA to
$7.20 — $11.70 per slot, $23.40 for two, which does not fit. Under CROSSED
margin the consequence is a **rejected order** (Binance `-2019`), not a
liquidation: the second slot's DCA fails to place and the existing position
keeps its protective stop. That is survivable and self-limiting, which is why
`SIZE_MAX_MULT` was left alone rather than clamped to 1.0 — clamping trades a
recoverable rejection for the permanent loss of confidence scaling.

**Liquidation distance.** At the full $312 notional state, maintenance margin
is roughly $1.56, so liquidation needs equity below that — about a **4.9%**
adverse move. The ATR-scaled stop, capped by the derived `MAX_STOP_LOSS_USD`
of $0.675 (112 bps), fires around **1.1%**. The stop binds first by a factor
of four — *provided it executes*. At 20× leverage the protective stop is the
only thing between this configuration and the liquidation price, so a stop
that fails to place is now an account-level event rather than a trade-level
one. The bookkeeping fix in §13 that reconciles `FINISHED` algo orders
against the exchange position matters considerably more here than it did at
2×.

### The take-profit ceiling the test suite found

Widening the target is the one change here that improves the trade's
economics rather than merely scaling it. Round-trip cost is ~7 bps and is
**fixed per round trip**, so it is 20% of a 35 bps target but only 10% of a
70 bps one. Expected move size grows as the square root of holding time while
cost does not grow at all — so wider is the right direction, *but only while
the target stays reachable*.

**The first version of this configuration set `TAKE_PROFIT_PCT` to 100 bps,
and the test suite rejected it.** `test_algo_update_and_trade_log_recovery_fix.py`
asserts that the take-profit must sit within about **5 ATR of the ATR floor**
(0.08%), because beyond that distance the position cannot travel far enough
before the loss budget or the max-hold timer ends the trade. 100 bps is
**12.5 ATR** at the floor. In the quiet `SIDEWAYS` conditions the live logs
actually show (atr% 0.17–0.25%), that target would essentially never be hit
and every trade would resolve at the stop or on max-hold. That is not a more
aggressive strategy — it is a worse one wearing an aggressive number.

Probing the invariant directly: **0.004 passes, 0.005 fails.** So

```
TAKE_PROFIT_PCT = 0.004   (40 bps = exactly 5.0 ATR at the floor)
```

is a hard ceiling imposed by the design, and that is where it sits.

**The expansion the operator asked for lives in `TAKE_PROFIT_MAX_PCT`
instead**, raised 100 → 200 bps. That is the right home for it:
`DYNAMIC_TP_ENABLED` interpolates between base and max as tick-return
volatility runs from `TP_VOL_LOW` to `TP_VOL_HIGH`, so the wide target is
applied *only when the market is actually moving far enough to reach it*,
while the 40 bps base governs the quiet conditions where reachability binds.
The base is the value used in quiet markets — which is precisely why it could
not simply be set to the number that sounded most aggressive.

`DCA_TRIGGER_PCT` was briefly widened to 0.005 alongside the 100 bps target
and reverted with it. At a 40 bps base the rung still fires once inside the TP
distance, which is the intended geometry.

### The Brain needs no reset

`TAKE_PROFIT_PCT` only **seeds** `_label_move_scale` (`trading.py:2548`),
which is an EWMA of realised `|forward_return|` decaying at 0.999 per tick.
Within roughly a thousand ticks the `tp_hit` and `noise` labels ride the
market's own move scale rather than this constant. The `tp_hit` head's
accumulated updates (2.8M+) stay valid across this change. No
`reset_head()`, no label-version bump.

### The two intentional overrides

Everything notional-scaled is left to derive itself **except** the two daily
circuit breakers, which are set explicitly on purpose:

```
MAX_DAILY_LOSS_USDT       = 2.50    (14.8% of a $16.92 account)
DAILY_PROFIT_TARGET_USDT  = 1.00    (2x the $0.50/day objective)
```

Notional is the right reference for per-**trade** geometry: a stop governs
one position, and that position's notional is fixed for its lifetime. It is
the wrong reference for a per-**day** budget, which is properly a fraction of
account equity and has nothing to do with how large any single entry happens
to be. Left derived, the daily loss cap lands at $0.75 — barely more than a
single full stop-out at $0.675 — so entries would halt after one losing trade
on most days, defeating the standing first priority that data collection must
never stop.

Note the gate is **new-entry-only**. The tick feature recorder and the
`tp_hit` / `noise` heads keep learning through a halt regardless; it is the
`success` head, which can only be labelled by a closed trade, that goes
hungry.

### Derived values at $60 notional

All thirteen, with the overrides cleared:

| Threshold | Value | bps of notional |
|---|---|---|
| `MAX_STOP_LOSS_USD` | $0.6750 | 112.5 |
| `MAX_TRADE_NET_LOSS_USDT` | $0.6750 | 112.5 |
| `TARGET_PROFIT_USD` | $0.6750 | 112.5 |
| `PROFIT_LOCK_ACTIVATION_USDT` | $0.3615 | 60.2 |
| `MIN_TARGET_PROFIT_USD` | $0.2625 | 43.8 |
| `SMART_ORDERFLOW_EXIT_MAX_LOSS_USD` | $0.1500 | 25.0 |
| `SL_MIN_USD` | $0.0900 | 15.0 |
| `SMART_ORDERFLOW_EXIT_MIN_LOSS_USD` | $0.0750 | 12.5 |
| `MAX_TRADE_EXIT_BUFFER_USDT` | $0.0750 | 12.5 |
| `MIN_NET_PROFIT_USDT` | $0.0375 | 6.2 |
| `DCA_RESCUE_BREAKEVEN_MIN_NET_USD` | $0.0300 | 5.0 |
| `MAX_DAILY_LOSS_USDT` | *overridden* $2.50 | — |
| `DAILY_PROFIT_TARGET_USDT` | *overridden* $1.00 | — |

`MIN_STOP_LOSS_USD` is a **dead value** — imported by `trading.py` but never
read for any decision. The working stop floor is `SL_MIN_USD`. Setting it has
no effect; it can be deleted from Railway.

### What was deliberately not changed

**`ENTRY_SCORE_THRESHOLD`.** Size and frequency multiply rather than add.
Raising both would compound the expected loss, and the entry gate is the last
filter still rejecting trades. Lowering it is the single fastest available
route to ruin. It is the remaining lever, and it is left in the operator's
hands deliberately.

**`MAX_DCA_STEPS`.** Two steps do not fit the margin arithmetic above at two
slots.

**`SIZE_MAX_MULT`.** See the rejected-order reasoning above.

### How to revert

Set these seven Railway variables and redeploy (`DCA_TRIGGER_PCT` never
moved):

```
INITIAL_ENTRY_USDT       = 5
LEVERAGE                 = 2
MAX_ACTIVE_TRADES        = 1
TAKE_PROFIT_PCT          = 0.0035
TAKE_PROFIT_MAX_PCT      = 0.010
MAX_DAILY_LOSS_USDT      = 1.50
DAILY_PROFIT_TARGET_USDT = 1.00
```

Every notional-scaled threshold follows automatically. Do **not** re-pin them
by hand — that is the mistake corrected at the top of §13.

### Test suite result for this change

48 of 53 files pass. The 5 failures are the long-standing pre-existing ones
documented in §10 (`dca_resync_race`, `dca_spacing`, `dca_time_gate`,
`fee_net_profitability_guard`, `trade_loss_budget_and_dca_gates`) — verified
identical against the previous `config.py`, so this change introduces no new
failures.

Two tests were amended, both because they pinned a *deployment* value into an
assertion about something else entirely — the same class of mistake as the
hand-pinned thresholds corrected in §13:

- `test_multi_coin_architecture.py` asserted `MAX_ACTIVE_TRADES == 1`. It now
  asserts the cap is a positive integer no larger than the watchlist, which is
  the actual invariant.
- `test_dry_fills.py` hardcoded `qty=0.2` while proving "PnL is booked net of
  commission" — a claim that must hold at any quantity. It now reads the
  filled quantity.

### What to watch in the first 48 hours

1. **The startup banner.** It must read `$60.00 = $3.0 x 20x` and
   `11/13 derived`. Any `OVERRIDDEN` line other than the two daily breakers
   means a stale Railway variable survived and is pinning a threshold while
   the rest of the geometry moved.
2. **`-2019` margin rejections.** Expected occasionally on a second-slot DCA;
   harmless. Frequent rejections on *initial* entries mean the account can no
   longer fund the configuration and `INITIAL_ENTRY_USDT` must come down.
3. **Protective-stop placement.** Every opened position must show its stop
   placed. At 20× a missing stop is the account, not the trade.
4. **The daily-loss halt.** If it fires most days, the loss rate is running
   ahead of the ruin model and the honest read is that the 36–61 day median
   is optimistic.
