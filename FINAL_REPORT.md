# Brain V2 Trading Bot — Safety & Correctness Patch: Final Report

**Revision 2** — incorporates the six findings from the independent review. All six were traced against the code; **all six are confirmed**, one of them (F6) only partially, with the difference explained in §13. Every confirmed finding is fixed and covered by new tests.

Repository: `Damith122/dca_agent`, branch `main`. Deployed-ZIP vs `main`: **byte-identical** (`diff -rq` reported zero differences) — there was nothing to reconcile before editing.

No Railway deploy, no live trading, and no real order was submitted at any point in this work. All changes were verified against a `DRY_RUN=false` FakeClient test harness (mocked network) — never against the real Binance API.

> **Delivery correction:** the review was right that five modified existing test files were described in Revision 1 but never delivered. They are delivered with this revision. The complete delivery set has now been validated by applying it to a **freshly cloned, unmodified `main`** and running everything: **20/20 files pass** (see §4.1b).

---

## 1. Root cause of the two live incidents

### 1.1 The −$1.0070 loss following a +$0.0845 win

`dca_step=0`, held **3.10h** — under the then-current 4h soft max-hold cap — so a DCA add was submitted and filled. The instant it filled, `dca_step` became `1`, which immediately multiplies the soft cap by `MAX_HOLD_TIME_DCA_MULTIPLIER` (0.5): the 4h cap became a 2h cap. The **same** 3.10h hold, now measured against the *new* 2h cap, was already overdue, so Max Hold Time V2 force-closed the position on the very next tick — a few seconds after paying the DCA's entry commission. The DCA add could not possibly have helped; it only added cost immediately before a close that was already coming.

Root cause: the DCA-submission gate checked hold time against the position's **current** step's cap, never against the cap that will apply **the instant the pending DCA fills**.

Also contributing: there was no per-trade dollar loss ceiling at all — only `MAX_DAILY_LOSS_USDT`, a whole-day breaker that does nothing to cap a single trade.

### 1.2 The HTTP 429/418 rate-limit / IP-ban incident

`_place_step_order()` had no cooldown check before calling `client.place_order()`, so once a DCA trigger condition was met, every subsequent price tick re-attempted the same order — hundreds of local rejection log lines — until Binance itself returned a real 418 (IP ban, ~25 min). During the ban this process could not reach the REST API for **any** purpose, including a risk-reducing close. A DCA filled during the ban but could not be locally confirmed; on recovery, the position (now believed stale) was immediately closed via `max_hold_time`.

Root cause: no single, central, monotonic cooldown gate shared by every order-submission path; local rejections were not distinguished from ambiguous/real exchange rejections; no exchange-side protection existed for the exact window (a REST outage) where a client-side check cannot run at all.

---

## 2. Files and functions changed

| File | Change |
|---|---|
| `config.py` | Added `MAX_TRADE_NET_LOSS_USDT`, `MAX_TRADE_EXIT_BUFFER_USDT`, `PROTECTIVE_STOP_ENABLED`, `PROTECTIVE_STOP_WORKING_TYPE`, `BRAIN_HEAD_MIN_SAMPLES`, and (Revision 2) `PROTECTIVE_STOP_CLIENT_ID_PREFIX`, `PROTECTIVE_STOP_RETRY_SEC`, `PROTECTION_PENDING_MAX_SEC` — 8 new env-driven constants, all added to `__all__`. No existing value changed. |
| `exchange.py` | Added `RestClient.get_open_orders(symbol)` (signed GET `/fapi/v1/openOrders`). Nothing else touched. |
| `brain.py` | `BrainV2`: added per-head sample/class-seen counters, `_head_reliable()`, `head_readiness()`; `predict_all()` now returns the neutral prior (0.5) for noise/success/tp_hit until each head has seen both classes and ≥`BRAIN_HEAD_MIN_SAMPLES` labels; `learn_noise/success/tp_hit` now increment those counters; snapshot version 2→3 with backward-compatible `load_state()`/`from_bytes()` (a v2 snapshot loads fine — weights preserved, reliability counters reset conservatively to 0). |
| `dca2.py` | One new import, one new call site: `await reconcile_protective_stop_on_startup(client, manager)` immediately after `initialize_sync()` at startup. |
| `trading.py` | See §3 below — the bulk of the behavioral changes. |
| 3 new test files | `test_trade_loss_budget_and_dca_gates_fix.py` (12 tests), `test_protective_stop_fix.py` (16 tests), `test_protective_stop_lifecycle_fix.py` (26 tests — review findings F2–F6). |
| 5 modified existing test files | `test_dca_spacing_fix.py`, `test_dca_time_gate_fix.py`, `test_fee_net_profitability_guard_fix.py`, `test_final_sync_dca_invariants_fix.py`, `test_rest_fallback_dca_safety_fix.py` — each adds one `MAX_TRADE_NET_LOSS_USDT=0` env-isolation line (see §4.1). No assertion weakened or removed. |

### 3. `trading.py` — mechanism-by-mechanism explanation

**`estimate_net_pnl_usdt_executable(extra_qty=0.0, extra_entry_price=None)`** — new method, deliberately separate from the pre-existing `estimate_net_pnl_usdt()` (used by Profit Lock / max-hold diagnostics, untouched). Computes fee-net PnL using the **executable closing-side price** (best bid to close a LONG, best ask to close a SHORT — the price a real reduceOnly MARKET order would actually fill near) and the **actual accumulated commission** (`_position_fees_accum`) when reliable, falling back to a rate-based estimate otherwise. `extra_qty`/`extra_entry_price` let a caller preview the state *after* a prospective DCA add without mutating anything.

**Per-trade fee-net loss budget (item 5)** — `_manage_open_position()`, evaluated every tick immediately after Hard Stop, before Profit Lock/TP/Smart-Exit/DCA: if `estimate_net_pnl_usdt_executable() <= -(MAX_TRADE_NET_LOSS_USDT - MAX_TRADE_EXIT_BUFFER_USDT)` (default trigger: **−$0.15**), the position is closed via the existing `close_position()` path with `exit_reason_tag="max_trade_net_loss"`. Gated behind `self.position_sync_ready` — like Max Hold V2 / Smart Exit / DCA, and *unlike* Hard Stop — because it reads `total_qty`/accumulated commission, exactly the kind of locally-tracked, restart-fragile state the Live incident showed can be corrupted. Disabled entirely when `MAX_TRADE_NET_LOSS_USDT<=0`. Records the trigger PnL on `PositionState.trade_loss_budget_trigger_pnl` and in the trade log.

**DCA loss-budget gate (item 7)** — inside the DCA block, immediately before order placement: projects `estimate_net_pnl_usdt_executable(extra_qty=<next DCA's qty>, extra_entry_price=<current price>)` and withholds the add (`[dca-budget] blocked ...`, `dca_step` untouched) if the projection would already breach the same trigger. `MAX_DCA_STEPS=2` remains the hard upper bound regardless.

**Prospective post-DCA max-hold gate (item 8)** — inside the DCA block: computes `MAX_HOLD_TIME_SEC * MAX_HOLD_TIME_DCA_MULTIPLIER` (the cap that will apply the instant *any* DCA fills, since post-fill `dca_step>=1` regardless of which step this is) and withholds the add if the position is already past that threshold — even though the *current*-step threshold hasn't been reached yet. This is the direct fix for incident 1.1.

**REST cooldown gate (item 4)** — `_place_step_order()`: first check in the function, before the stale-decision guard — if `self.client.is_cooldown_active()`, the DCA attempt is withheld (logged once, then throttled) instead of submitting into a known ban/cooldown window. Defensive via `getattr` so a test harness's minimal fake client (no `is_cooldown_active`) doesn't crash.

**Exchange-native protective stop (item 6)** — new methods `_compute_protective_stop_price()`, `_place_or_replace_protective_stop()`, `_cancel_protective_stop()`, plus top-level `reconcile_protective_stop_on_startup()`:

- A `STOP_MARKET`, `closePosition=true` order, sized from the *same* loss-budget inputs as item 5, placed on Binance itself immediately after every confirmed entry/DCA fill (`_on_entry_filled`) — this protects the position even when this process cannot reach the REST API at all (the exact 418-ban scenario).
- `closePosition=true` was chosen deliberately over a fixed reduceOnly quantity: per Binance USD-M Futures docs it always closes the **entire** current position, can never reverse/increase it, and needs no quantity bookkeeping across DCA adds — a fixed-qty stop would go stale (under- or over-close) the instant a DCA changes `total_qty`.
- **Replace sequence and accepted risk**: Binance has no atomic "amend stopPrice" for this order type — moving it requires cancel + place-new as two separate requests, leaving a real (normally sub-second, REST-latency-bound) window with *no* protective stop resting. Place-then-cancel was rejected: two `closePosition=true` STOP_MARKET orders on the same side/symbol resting simultaneously in One-Way Mode risk Binance rejecting the second as a duplicate conditional order, which would silently leave the *stale* stop as the only one resting — worse than a short gap with none. This gap is mitigated (not eliminated) by the client-side budget check in item 5 running independently every tick; the exchange-native stop exists specifically for when that client-side check *cannot* run (REST outage/ban, process restart).
- If placement fails, the position enters `PROTECTION_PENDING` (`PositionState.protection_pending`): new DCA is blocked (does not touch TP/Hard Stop/Profit Lock/Smart Exit/Max Hold/the net-loss budget itself), with a high-severity log, rather than continuing silently unprotected.
- `_cancel_protective_stop()` is called before every close path finalizes (`close_position()`'s early "exchange already flat" branch, and `_on_close_filled()`) so no protective order is ever left orphaned after TP/Profit-Lock/Smart-Exit/Hard-Stop/manual/max-hold closes.
- `reconcile_protective_stop_on_startup()` runs once, right after `initialize_sync()`, and queries Binance's own open orders: adopts a single matching resting stop, de-duplicates (cancels extras) if more than one is somehow resting, places a fresh one if none is found for a genuinely OPEN position, or enters `PROTECTION_PENDING` with a high-severity log if the fetch itself fails — never silently assumes protection.

**Entry-quality audit (item 10)** — see §5 below.

**No secrets in logs**: verified no code path added by this patch prints API keys/secrets, listen keys, or signed query strings; the existing convention of logging only prices/qty/order IDs/reasons was preserved throughout.

---

## 4. Test commands and complete results

Every `test_*.py` file in the repo is a standalone script (module-level `asyncio.run(...)`, not pytest-compatible — confirmed by running the original, unmodified `test_fill_race_fix.py` both ways: passes directly, fails under `pytest` with "async def functions are not natively supported"). Run each directly:

```bash
for f in test_*.py; do python3 "$f"; done
```

**Result: 20/20 files pass, 0 failures, 0 skips.**

| File | Status | Covers |
|---|---|---|
| `test_dca_resync_race_fix.py` | PASS | pre-existing |
| `test_dca_risk_debug_throttle_fix.py` | PASS | pre-existing |
| `test_dca_spacing_fix.py` | PASS | pre-existing (env-isolated from the new loss budget — see below) |
| `test_dca_time_gate_fix.py` | PASS | pre-existing (env-isolated) |
| `test_entry_quality_guard_fix.py` | PASS | item 10 |
| `test_fee_net_profitability_guard_fix.py` | PASS | pre-existing (env-isolated) |
| `test_fill_race_fix.py` | PASS | pre-existing |
| `test_final_sync_dca_invariants_fix.py` | PASS | pre-existing (env-isolated) |
| `test_github_sync_branch.py` | PASS | pre-existing |
| `test_new_features.py` | PASS | pre-existing |
| `test_position_sync_ready_fix.py` | PASS | pre-existing — this is the test that caught the position_sync_ready regression described below |
| `test_protective_stop_fix.py` | **PASS (new, 16 tests)** | item 6, full |
| `test_protective_stop_lifecycle_fix.py` | **PASS (new, 26 tests)** | review findings F2–F6 |
| `test_rest_cooldown_and_session_fix.py` | PASS | item 4 |
| `test_rest_cooldown_startup_and_pollers_fix.py` | PASS | item 4 |
| `test_rest_fallback_dca_safety_fix.py` | PASS | pre-existing (env-isolated) |
| `test_restart_accounting_fix.py` | PASS | pre-existing |
| `test_startup_state_safety_fix.py` | PASS | item 4 |
| `test_trade_loss_budget_and_dca_gates_fix.py` | **PASS (new, 12 tests)** | items 5, 7, 8 |
| `test_websocket_route_migration_fix.py` | PASS | item 3 |

### 4.1 A regression caught and fixed during this work

The first version of the item-5 gate was **not** gated behind `self.position_sync_ready`. `test_position_sync_ready_fix.py` failed because it explicitly encodes the codebase's own invariant: only simple, deterministic, non-qty-dependent exits (Hard Stop) may act while sync is unconfirmed; qty/fee-dependent "discretionary" exits (DCA, Max Hold V2, Smart Exit — and now the loss budget) must wait. Fixing this (adding the `position_sync_ready` gate, matching the existing pattern exactly) resolved that failure and 5 further cascading failures in tests whose fixtures triggered the new gate at scenarios/sizes they hadn't been written to anticipate (`test_dca_spacing_fix.py`, `test_dca_time_gate_fix.py`, `test_fee_net_profitability_guard_fix.py`, `test_final_sync_dca_invariants_fix.py`, `test_rest_fallback_dca_safety_fix.py`). Those 5 files test mechanisms orthogonal to the new budget (DCA spacing, DCA/max-hold interaction, 2/2-exhaustion + daily targets, sync/DCA invariants, REST-fallback DCA safety); each now sets `MAX_TRADE_NET_LOSS_USDT=0` in its own env block to isolate what it's actually testing — the same test-isolation pattern the suite already used for `SMART_EXIT_ENABLED`/`MAX_HOLD_TIME_ENABLED`. No assertion in any of the 5 files was weakened or removed.

### 4.1b Delivery-set validation (added in Revision 2)

Because Revision 1 shipped an incomplete file set, the delivery is now validated the same way the reviewer validated it — by applying **only the delivered files** to a **freshly cloned, unmodified `main`** and running the whole suite there:

```bash
git clone https://github.com/Damith122/dca_agent.git verify_clean
cp config.py exchange.py brain.py dca2.py trading.py verify_clean/
cp test_dca_spacing_fix.py test_dca_time_gate_fix.py test_fee_net_profitability_guard_fix.py \
   test_final_sync_dca_invariants_fix.py test_rest_fallback_dca_safety_fix.py verify_clean/
cp test_protective_stop_fix.py test_trade_loss_budget_and_dca_gates_fix.py \
   test_protective_stop_lifecycle_fix.py verify_clean/
cd verify_clean && for f in test_*.py; do python3 "$f"; done
```

**Result on the clean repo: PASS=20 FAIL=0 TOTAL=20.** A file-list diff against the working directory confirms no other file differs.

### 4.2 An important interaction documented by the new tests

At the **currently configured production sizing** ($4 initial margin × 20x leverage = $80 notional, ~0.2% ATR-based DCA trigger distance), round-trip taker fees on the very first DCA-eligible move already approach the $0.20 budget on their own (fees ≈ $0.08 + a ~$0.16 move at the 0.2% trigger distance ≈ $0.24 total), so **item 5 (the per-trade budget) fires at or before the very first DCA trigger is reached** — item 7's DCA-specific gate is real, correctly implemented, and unit-tested in isolation, but is rarely the mechanism that actually intervenes at this size. `test_loss_budget_preempts_dca_at_production_defaults` asserts this explicitly so a future sizing change that silently breaks the interaction is caught. This is not a bug — it's the direct, working consequence of adding a tight per-trade budget on top of an existing tiny-account position size, and is exactly why §6 below separately flags a sizing recommendation for approval.

---

## 5. Entry-quality audit (item 10) — findings

| Suspected issue | Verdict | Action |
|---|---|---|
| `TP_HIT_LOOKAHEAD_CANDLES` unused | **Confirmed** | Left unwired deliberately — documented in `config.py`; the `tp_hit` head's real signal comes from tick-based `LABEL_HORIZON_TICKS`, refined later by side-aware success/quality heads at actual close. Rewiring it would be a real pipeline change with no evidence of harm; out of scope for a minimal-change safety pass. |
| `LABEL_HORIZON_TICKS` tick-based not candle-based | **Confirmed, not a defect** | Same as above — intentional design, documented, not changed. |
| TP label uses `abs(forward_return)>=TAKE_PROFIT_PCT` (direction-agnostic) | **Confirmed, not a defect** | Deliberately measures "was *a* tradeable move available", not "was the trade a winner" — that judgment is made by the side-aware success/quality heads. Not changed. |
| `update_count` counts raw ticks, not meaningful samples | **Confirmed defect** | Fixed: separate per-head sample counters (`noise_samples`/`success_samples`/`tp_hit_samples`), distinct from `update_count` (unchanged, still gates the overall `is_ready()`). |
| Classifier heads saturate to 0/1 from tiny/one-class samples | **Confirmed defect** (live evidence: `entry_success_prob=1.0` from ≤2 trades) | Fixed: `predict_all()` returns the neutral prior (0.5) until a head has seen both classes and ≥`BRAIN_HEAD_MIN_SAMPLES` (20) labels. |
| `quality_pred` computed but ignored | **Confirmed** | Now included in `EntryEngineV2`'s logged components (`quality_pred`) for visibility; the composite score formula itself was not restructured (out of scope — that would be a strategy change, not a correctness fix). |
| Brain/trend double-counted in composite score | **Confirmed, not a defect** | Reviewed the weighting; it's a defensible design choice (trend appears both directly and via confidence), not a logic error. Not changed. |
| TP probability near-zero not influencing decision | **Addressed indirectly** | Once the reliability gate above prevents near-zero/near-one saturation from tiny samples, this concern is substantially mitigated; the decision path itself (which components feed the composite score) was not restructured. |
| CSV `entry_confidence` ≠ final entry score | **Confirmed defect** | Fixed: `PositionState.entry_composite_score`/`entry_score_threshold` now record the *actual* accepted score/threshold, logged separately in the trade CSV (`entry_composite_score`, `entry_score_threshold`) alongside the pre-existing `entry_confidence` (left as-is — it's a real, separately-meaningful sub-component, not deleted). |

Additional guarantees added: one concise, rate-limited entry-decision log line per evaluated candidate (`[entry-debug]`/`[entry-accepted]`) now includes side, composite score, threshold, regime, directional/trend components, Brain confidence, success/TP-hit probability, risk score, quality prediction, and a `brain_ready=[...]` summary reading `head_readiness()` (READY/WARMING_UP per component) plus the exact accept/reject reason. Brain snapshot version bumped 2→3 with backward-compatible loading — a v2 snapshot on disk loads without error, model weights are fully preserved, only the new reliability counters start at 0 (conservative, never silently discarded).

---

## 6. Baseline vs. patched evaluation

No historical tick-level dataset was provided or found in the repository sufficient to replay the exact two live incidents deterministically end-to-end (the attached Railway log is prose/log-line format, not structured replay data). Accordingly:

- **No profitability claim is made.** This patch is a safety/correctness patch, not a strategy change, and its numeric loss ceiling ("~$0.20 targeted") is explicitly a target, not a guarantee (slippage, gaps, delayed fills, and exchange outages can still produce a worse realized result).
- What **is** verified deterministically: the exact code paths implicated in both incidents (post-DCA max-hold timing; unbounded REST retry storms) are now covered by passing regression tests reproducing the same numeric conditions from the incident report (3.10h hold / 4h→2h cap; repeated-tick DCA submission during cooldown), and the new per-trade budget / exchange-native stop are unit-verified to trigger, disable, and gate correctly per §4.

---

## 7. Remaining risks

1. **Cancel-then-replace gap on the protective stop** (see §3): a sub-second window with no resting exchange-side stop exists every time a DCA changes the position's economics. Mitigated, not eliminated, by the independent client-side budget check.
2. **At current sizing, item 5 (not item 7) is almost always the mechanism that intervenes** (§4.2) — DCA is effectively rarely reached before the budget closes the trade. This is expected given the numbers, but means the DCA recovery mechanism largely can't do its job at this size/budget combination; see the sizing recommendation below.
3. **The $0.20 target is not a guarantee** — under a real gap, extreme slippage, or an exchange outage that also blocks the protective stop from filling at the intended price (STOP_MARKET can still slip past its trigger in a fast market), realized loss can exceed the target.
4. **Entry-quality fixes address reliability/logging, not strategy performance** — no threshold was retuned, no frequency was increased; actual win-rate/expectancy improvement (if any) is unproven without a live/replay dataset.
5. **Liquidation guard formula confirmed correct (see §8) but its numeric buffer was not re-derived from live account numbers** — see the recommendation below.

---

## 8. Liquidation guard audit (item 13)

Found in `dca2.py`'s `position_risk_poller()` (not in `trading.py`, where earlier `LIQUIDATION_WARNING_BUFFER_PCT` usage in `RiskEngine.score()` is a *separate*, secondary soft input to the risk score — not the hard guard). The hard guard: every poll cycle, Binance's own authoritative `liquidationPrice` and `markPrice` are fetched; a sanity band (`LIQUIDATION_SANITY_MIN_RATIO`–`LIQUIDATION_SANITY_MAX_RATIO`, 0.2×–5×) filters out implausible Cross-Margin over-collateralization artifacts; if plausible, `distance_pct = |mark - liq| / mark` is computed, and if `distance_pct <= LIQUIDATION_WARNING_BUFFER_PCT` (default 0.15) while a position is OPEN, the bot emergency-closes it ahead of a forced liquidation.

**Math verified correct**: the formula is a standard fractional-distance-to-liquidation calculation, correctly directional (works the same for LONG/SHORT since it's an absolute difference), correctly guards against Cross-Margin's well-known behavior of reporting a liquidation price calculated against the *whole account balance* rather than just this position (hence the sanity band, which is why the guard doesn't fire spuriously at normal 20x isolated-style distances of ~5%). **No bug found; nothing changed.**

**Recommended value — for approval, not applied**: a scientifically-grounded new number requires the actual live Cross-Margin wallet balance and current total exposure (not fully available to this session — the account is referenced elsewhere in the codebase as a "~$33 account," but no live balance/positionRisk snapshot was supplied). Recommendation: **keep the existing 0.15 (15%) default** unless/until the user can supply a current wallet-balance snapshot; a formal re-derivation from live numbers can follow as a fast, isolated follow-up once that data is available. Do not narrow it without that data — narrowing a liquidation buffer with stale assumptions is a directly dangerous change.

---

## 9. Railway environment variables

**New (added by this patch, all optional — sensible defaults, no action required to deploy):**

| Variable | Default | Purpose |
|---|---|---|
| `MAX_TRADE_NET_LOSS_USDT` | `0.20` | Per-trade fee-net loss budget (item 5). Set `0` to disable. |
| `MAX_TRADE_EXIT_BUFFER_USDT` | `0.05` | Exit buffer subtracted from the budget to compute the actual trigger (−$0.15 by default). |
| `PROTECTIVE_STOP_ENABLED` | `true` | Enables the exchange-native STOP_MARKET protective stop (item 6). |
| `PROTECTIVE_STOP_WORKING_TYPE` | `MARK_PRICE` | Trigger price source for the protective stop. |
| `BRAIN_HEAD_MIN_SAMPLES` | `20` | Minimum labeled samples (both classes seen) before a Brain classifier head's probability is treated as reliable. |

**Unchanged**: every other existing Railway env var (leverage, initial entry size, DCA multiplier/steps/distance, TP/Profit-Lock/Smart-Exit/Hard-Stop/Max-Hold values, `MAX_DAILY_LOSS_USDT`, `LIQUIDATION_WARNING_BUFFER_PCT`, entry thresholds, etc.) — none were modified.

**To remove**: none.

**Separately-listed recommendations (NOT auto-applied — Railway ENV decisions requiring explicit approval):**

1. **Smaller-position / larger-budget profile**: given the §4.2 finding, if the intent is for DCA to actually participate in recovery (rather than the $0.20 budget essentially pre-empting it every time), consider either raising `MAX_TRADE_NET_LOSS_USDT` (e.g., toward $0.40–$0.50) to give the existing $80-notional/2-step DCA room to work, or reducing `INITIAL_ENTRY_USDT`/leverage so round-trip fees are a smaller fraction of a $0.20 budget. No specific new value is being asserted as "correct" — this is a trade-off between tighter loss protection and DCA's ability to function, and the right number depends on risk tolerance the user should set explicitly.
2. **`LIQUIDATION_WARNING_BUFFER_PCT`**: keep at `0.15` pending a live wallet-balance-informed re-derivation (§8).

---

## 10. State / schema migration impact

- **Trade log CSV**: `TRADE_LOG_FIELDS` gained 3 columns (`entry_composite_score`, `entry_score_threshold`, `loss_budget_trigger_est_net_pnl`). The existing `_migrate_csv_schema_if_needed()` mechanism (already in the codebase, unmodified) rewrites the on-disk header/blank-fills automatically the next time the bot starts — no manual action needed, no data loss.
- **DCA-state JSON snapshot**: 4 new fields (`protective_stop_order_id`, `protective_stop_price`, `protection_pending`, `protection_pending_reason`) added to `_dca_state_snapshot()`/`_flat_dca_state_snapshot()`. Loaded via `.get(..., default)` — an old snapshot missing these fields loads fine, defaults to "no protective stop tracked / not pending," and `reconcile_protective_stop_on_startup()` will discover/adopt or place a fresh one on the next startup.
- **Brain snapshot**: version 2→3. A v2 snapshot loads without error (`from_bytes()` accepts versions `(2, 3)`); model weights are fully preserved; only the new reliability counters start at 0/no-classes-seen (conservative — heads simply take up to `BRAIN_HEAD_MIN_SAMPLES` fresh labels to be trusted again after an upgrade, they are never wiped).
- **No file was deleted, reset, or overwritten as part of this work** — trade logs, DCA-state, Brain snapshots, and performance/stats files are all additive/backward-compatible changes only.

## 11. Rollback procedure

1. `git revert` (or restore from this session's diff) the 5 changed files (`config.py`, `exchange.py`, `brain.py`, `dca2.py`, `trading.py`) back to the `main` baseline used at the start of this session.
2. Remove the 5 new Railway env vars (§9) — harmless to leave them set (they're simply unread by the old code) or remove them for cleanliness.
3. No data migration is needed to roll back: old code reads the DCA-state JSON/Brain snapshot fine since it simply ignores the newer fields it doesn't know about (they were added additively, never replacing/removing any existing field).
4. The 2 new test files can be deleted or left in place (inert without the corresponding `trading.py` changes — they would fail against reverted code, which is expected and fine to ignore post-rollback).

---

## 12. Controlled live validation checklist (item 16)

**This report does NOT recommend live deployment yet.** All changes are deterministically tested; no live-market or historical-replay evaluation has been performed (§6), and the remaining risks in §7 should be read and accepted first. If/when the user chooses to validate live with one minimum-size trade, here is what to expect in the logs as proof each mechanism is active:

| Mechanism | Expected log line(s) | Confirms |
|---|---|---|
| Protective stop placed on entry | `[protective-stop] PLACED {SELL/BUY} closePosition=true stopPrice=... orderId=...` right after the entry fill line | item 6 armed immediately |
| Protective stop replaced after DCA | `[protective-stop] cancelled orderId=... (replacing before: ...)` followed by a new `PLACED` line | cancel-then-replace working |
| Protective stop reconciled on restart | `[protective-stop] startup reconciliation adopted existing orderId=...` (or `placing one now` if none found) | item 6 restart-safe |
| Per-trade budget triggers | `[trade-loss-budget] TRIGGERED: estimated fee-net pnl $... <= trigger $-0.15 ...` followed by `EMERGENCY CLOSE` | item 5 active |
| DCA withheld by budget | `[dca-budget] blocked step=... projected_net_loss=...` | item 7 active |
| DCA withheld by prospective timeout | `[dca-blocked-post-step-timeout] ...` | item 8 active (incident 1.1 fix) |
| REST cooldown respected | `[order-cooldown-block] ...` instead of a repeated order attempt | item 4 active (incident 1.2 fix) |
| PROTECTION_PENDING correctly blocks only DCA | `[protective-stop] *** HIGH SEVERITY *** PROTECTION_PENDING ...` with no new DCA order, while Hard Stop/other exits remain reachable | item 6 fail-safe |
| Brain reliability gating | `brain_ready=[...]` block in `[entry-debug]`/`[entry-accepted]` showing READY/WARMING_UP per head | item 10 active |

Recommended validation sequence: (1) confirm `PROTECTIVE_STOP_ENABLED=true` and `MAX_TRADE_NET_LOSS_USDT=0.20` (or the approved value) are set in Railway; (2) let exactly one minimum-size trade open; (3) confirm the `[protective-stop] PLACED` line appears within the same log burst as the entry fill; (4) if a DCA triggers, confirm either a normal DCA fill or a `[dca-budget] blocked`/`[dca-blocked-post-step-timeout]` line — never a silent no-op; (5) confirm the trade closes via exactly one of TP/Hard-Stop/Profit-Lock/Smart-Exit/`max_trade_net_loss`/`max_hold_time`, and that the protective stop is cancelled (`[protective-stop] cancelled orderId=... (position closed ...)`) in the same close sequence; (6) inspect the trade-log CSV row for the new `entry_composite_score`/`entry_score_threshold`/`loss_budget_trigger_est_net_pnl` columns to confirm the schema migration succeeded.

---

## 13. Independent review findings — verdicts and corrections (Revision 2)

Every finding was traced in code and reproduced (or refuted) with a focused test before any change was made. Verdict summary: **F1 confirmed, F2 confirmed, F3 confirmed, F4 confirmed, F5 confirmed, F6 partially confirmed.**

### F1 — Modified test files were not delivered. **CONFIRMED.**

Evidence: the five files genuinely differ from `main` in my working directory, but were absent from the delivery.

```
$ for f in test_*.py; do diff -q repo_main/$f deployed_zip/.../$f; done
MODIFIED: test_dca_spacing_fix.py
MODIFIED: test_dca_time_gate_fix.py
MODIFIED: test_fee_net_profitability_guard_fix.py
MODIFIED: test_final_sync_dca_invariants_fix.py
MODIFIED: test_rest_fallback_dca_safety_fix.py
```

The reviewer's 14-pass/5-fail result was the correct and expected consequence. **Correction:** all five are delivered with this revision, and §4.1b adds a clean-repo validation step so this class of gap cannot recur silently.

### F2 — Protective-stop FILLED event unhandled. **CONFIRMED — and the most serious of the six.**

Evidence: `_place_or_replace_protective_stop()` set `protective_stop_order_id`/`_price` but never touched `_order_index`; `handle_order_update()` routes only when `order_id in self._order_index` (trading.py, the `if order_id not in self._order_index:` branch). `_try_recover_close_fill()` matched only `pending_role == "close"` with `pending_order_id`, which a protective stop never is.

Reproduction (matches the reviewer's byte-for-byte):

```
protective_stop_order_id = 7000
_order_index.get(7000)   = None
[fill-trace] ... reason=untracked_order_id ... buffered
position.status              = OPEN      <-- exchange had CLOSED it
protective_stop_order_id     = 7000
buffered in _unmatched_fills = True
```

Consequence: the exchange closed the position, the bot kept believing it was open, kept managing and potentially DCA-ing a position that no longer existed, and never logged the trade, never updated PnL/daily counters, never reset to FLAT.

**Correction.** The order is now registered via `_register_order_and_replay(order_id, "protective_stop")` — the same mechanism ordinary entry/DCA/close orders use — and `handle_order_update()` gained a `role == "protective_stop"` branch that clears local stop tracking (the order triggered; there is nothing left to cancel), sets `_pending_exit_reason = "protective_stop"`, and routes to the **same `_on_close_filled()`** every other close uses. This gives realized PnL, commission, trade CSV/JSON logging, daily counters, Brain learning, and the reset to FLAT through exactly one code path. Coverage added:

- exactly-once processing (guaranteed by the existing `_order_index.pop()`), verified against duplicate WebSocket **and** REST redelivery;
- a fill arriving *before* registration is replayed from the unmatched-fill buffer (a stop can trigger the instant it is accepted);
- restart recovery: `_try_recover_close_fill()` now also matches the persisted `protective_stop_order_id`, and `initialize_sync()` re-registers a restored protective stop into `_order_index`;
- an adopted stop from startup reconciliation is registered too.

**A second bug was found while writing these tests and fixed:** the new restart-recovery branch made duplicate finalization possible, because the on-disk snapshot is rewritten asynchronously (`save_flat_dca_state` via `create_task`) after a trade finalizes, so a duplicate fill arriving in that window matched a stale snapshot and finalized the same trade twice. `_try_recover_close_fill()` now returns early unless `self.position.status` is one of `OPEN`/`DCA_PENDING`/`CLOSING` — the synchronous, authoritative signal that the trade is still live. `test_f2_duplicate_fill_events_are_idempotent` is the test that caught it.

### F3 — No retry or bounded fail-safe for `PROTECTION_PENDING`. **CONFIRMED.**

Evidence: the only two callers of `_place_or_replace_protective_stop()` were `_on_entry_filled()` and startup reconciliation. Since `PROTECTION_PENDING` itself blocks new DCA, no further fill could occur — so a position that failed to arm stayed unprotected for the entire life of the trade, with no retry and no bound. The reviewer's trace is exactly right.

**Correction.** New `_protective_stop_sweep()`, called once per tick from `_manage_open_position()` immediately after Hard Stop:

1. retries an unconfirmed **cancel** (see F5);
2. retries **placement** every `PROTECTIVE_STOP_RETRY_SEC` (default 30s) while `PROTECTION_PENDING`;
3. if protection still cannot be armed after `PROTECTION_PENDING_MAX_SEC` (default 300s), performs a **bounded, risk-reducing fail-safe close** with `exit_reason=protection_unavailable`.

Retry-storm safety is explicit: every branch is time-throttled, and **all of them return early while a REST cooldown is armed**, so this adds zero REST traffic during a ban. `protection_pending_since` starts once and is persisted, so repeated failures cannot keep resetting the clock and a restart cannot reset it to zero. Tests cover: retry succeeds and clears the state; 50 rapid ticks produce at most one retry; zero placement attempts during cooldown; fail-safe fires and submits exactly one reduceOnly close; `PROTECTION_PENDING_MAX_SEC<=0` disables the fail-safe.

### F4 — Protective-stop ownership ambiguous. **CONFIRMED.**

Evidence: `grep -n "newClientOrderId\|clientOrderId" trading.py exchange.py` returned **nothing** — no client order ID was set anywhere. Reconciliation matched purely on `type == "STOP_MARKET"` + close side + `closePosition == "true"`, and it both **adopts** and **cancels** what it matches. A user's manual stop on the same symbol/side satisfies all three conditions.

**Correction.** Every bot-placed protective stop now carries `newClientOrderId` beginning with `PROTECTIVE_STOP_CLIENT_ID_PREFIX` (default `bv2ps`, kept within Binance's `^[\.A-Za-z0-9_:/-]{1,36}$` charset and 36-char limit). `_is_own_protective_stop()` is the single ownership predicate, and reconciliation now partitions open orders into owned vs foreign — foreign orders are counted, logged, and **left completely untouched**. Tests include the exact scenario requested: a bot-owned stop and an unrelated manual `STOP_MARKET` resting simultaneously → the bot-owned one is adopted and wired for fill routing, the manual one is never adopted and never cancelled; plus a dedupe test where two bot-owned duplicates and one manual order rest together and only the duplicate bot-owned order is cancelled.

### F5 — Cancel failure cleared local tracking too early. **CONFIRMED.**

Evidence: the old `_cancel_protective_stop()` set `protective_stop_order_id = None` and `protective_stop_price = None` **before** the `try:` block, so every outcome — success, `-2011`, network timeout, API rejection — cleared tracking identically. A failed cancel therefore left a real resting order on Binance that the bot could no longer name. The reviewer is also right that `reconcile_protective_stop_on_startup()` runs only once at startup, so the comment's promised "later reconciliation" did not exist in-session.

**Correction.** `_cancel_protective_stop()` now returns `bool` and clears tracking **only on proof**: the cancel succeeded, or Binance answered `-2011` ("Unknown order sent", which proves the order is gone). On any indeterminate outcome the id is retained and `protective_stop_cancel_pending` is set. Follow-through:

- the per-tick sweep retries the cancel until it resolves;
- `_place_or_replace_protective_stop()` **refuses to place a replacement** over an unconfirmed cancel, so two `closePosition=true` stops can never rest simultaneously;
- a cancel attempted during REST cooldown is deferred without a REST call and the id retained;
- and critically, when the position goes **FLAT** the `PositionState` is discarded — so an unconfirmed cancel at close time is handed to a new manager-level `_orphan_protective_stop_ids` registry, swept by `_sweep_orphan_protective_stops()` from `on_price_tick()` (which runs regardless of position state) until Binance accepts the cancel or proves the order gone. An orphaned conditional order is most dangerous exactly when the bot believes it is flat, since it could later trigger against a *new* position.

All six requested cases are tested: success; `-2011`/already-gone; timeout; REST cooldown; API-rejection-while-still-open; and no-orphan/no-duplicate.

### F6 — Cooldown scope. **PARTIALLY CONFIRMED — the log-spam half is real, the ban-escalation half is not.**

The reviewer is correct that the *explicit* gate existed only in `_place_step_order()`, and that close and protective-order paths re-attempted on every market tick. But the premise that this could "race into another Binance 418" is **not** correct, and I want to be precise rather than agree by default. `exchange.py::RestClient._request()` blocks **every** request centrally while a cooldown is armed:

```python
if self._cooldown_until_ts and time.time() < self._cooldown_until_ts:
    raise BinanceApiError(429, {"code": -1003, "msg": f"local REST cooldown active ..."})
```

This raises **before** any network I/O — so no close, protective-stop, poller, or DCA request can reach Binance during a cooldown, and none of them can extend a ban. `test_f6_request_layer_is_the_central_choke_point` demonstrates this across all five request paths (order / positionRisk / balance / cancel / openOrders), and asserts `client.session is None` throughout — proving nothing touched the network layer.

What *was* genuinely wrong is the local churn: during the 25-minute ban, every tick ran `_fetch_exchange_position()` (one synthetic-429 error log), flipped status `OPEN → CLOSING`, failed to place the order (a second, alarming `POSITION MAY STILL BE OPEN, check manually!` log), then flipped status back to `OPEN` — hundreds of times, with per-tick state thrash.

**Correction.** The same `is_cooldown_active` gate now guards `close_position()` and protective-stop placement/cancel. This is **behavior-preserving by construction** — since `_request()` refuses the call anyway, the gate cannot suppress a close that would otherwise have succeeded — and `test_f6_close_resumes_immediately_when_cooldown_clears` asserts the close proceeds on the very next call once cooldown clears. `test_f6_close_position_blocked_during_cooldown_without_thrash` runs 25 ticks during a ban and asserts zero orders submitted, unchanged status, no alarming spam, and ≤2 throttled log lines.

On serialization at cooldown expiry: the pre-existing `wait_out_cooldown_silently(jitter_max=3.0)` is the coordination mechanism, and it already covers the three pollers. `test_f6_concurrent_expiry_does_not_stampede` runs six concurrent waiters and asserts they all resume only after expiry and are jittered apart (measured spread ≈0.87s), so they do not stampede back into Binance on the same tick. Given the central `_request()` block plus jittered resume, I did **not** add a single-flight lock: it would add a new concurrency primitive to the hot path for no reachable failure mode I could demonstrate. If you would like belt-and-braces serialization anyway, say so and I will add it as an isolated change.

---

## 14. Additional Railway environment variables (Revision 2)

All optional, all with safe defaults — no action is required to deploy.

| Variable | Default | Purpose |
|---|---|---|
| `PROTECTIVE_STOP_CLIENT_ID_PREFIX` | `bv2ps` | Ownership tag for bot-placed protective stops (F4). Only orders whose `clientOrderId` starts with this are ever adopted/cancelled. Change only if it could collide with another system's IDs on the same account. |
| `PROTECTIVE_STOP_RETRY_SEC` | `30` | Throttle for protective-stop placement/cancel retries (F3/F5). |
| `PROTECTION_PENDING_MAX_SEC` | `300` | How long a position may stay unprotected before the bounded fail-safe close fires (F3). Set `0` to disable the fail-safe (retries continue; **not recommended**). |

**Unchanged:** everything else, including all five variables from Revision 1. **To remove:** none.

### State/schema impact of Revision 2

The DCA-state snapshot gains `protective_stop_client_order_id`, `protective_stop_cancel_pending`, and `protection_pending_since`. All are read with `.get(..., default)`, so a snapshot written by Revision 1 (or by pre-patch code) loads without error. Rollback is unchanged: older code simply ignores fields it does not know about.

### New exit reason

`protection_unavailable` — the bounded fail-safe close (F3). It joins `protective_stop`, which now appears in the trade log when the exchange-native stop is what closed the trade (F2). Both are worth watching for in the live-validation checklist in §12.
