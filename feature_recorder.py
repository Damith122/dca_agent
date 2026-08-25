#!/usr/bin/env python3
"""
================================================================================
 FEATURE RECORDER (2026-08-24)

 WHY THIS EXISTS
 -------------------------------------------------------------------------
 Offline analysis of the first 58 live trades showed the entry model has no
 demonstrated directional edge: maximum favourable excursion exceeded maximum
 adverse excursion in only 45% of trades against a 50% coin flip, and no
 entry-time feature separated the winners. Every promising subgroup collapsed
 under a bootstrap - each 95% CI spanned both large profit and large loss.

 The blocker is the dataset, not the analysis. 58 trades is far too few, and
 worse, they are only the setups the CURRENT rule chose to take. Nothing is
 known about the ones it rejected, so any rule fitted to them inherits the
 existing selection bias.

 This module records EVERY evaluated setup - accepted and rejected alike -
 with the full feature vector, every score component, the orderflow snapshot,
 and the realised forward return over several horizons. That is the dataset
 needed to ask "does any rule beat a coin flip" without begging the question.

 DESIGN CONSTRAINTS
 -------------------------------------------------------------------------
 * Recording must never affect trading. Every public method swallows its own
   exceptions and returns quietly; the caller is not expected to guard.
 * Volume. The decision loop runs ~3.5x/s per symbol; recording all of it
   would produce ~30 MB/hour and overwhelm the whole-file GitHub sync. Samples
   are therefore taken on a fixed interval (default 10s per symbol), which is
   also honest statistically - consecutive ticks are so autocorrelated that
   denser sampling would inflate the row count without adding information.
 * Sharding. Files are rotated hourly and each completed shard is uploaded
   once, so no upload ever re-sends the whole history.
 * Forward returns are only knowable later, so a sample is buffered until its
   longest horizon elapses, with MFE/MAE tracked on every tick in between.
================================================================================
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional


def _f(value: Any, digits: int = 6) -> Optional[float]:
    """Round for compactness; None for anything non-finite or unusable. Keeps
    the JSONL small without silently turning a NaN into a real number."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return round(out, digits)


class FeatureRecorder:
    """Records evaluated setups and their realised forward returns.

    One instance per symbol. `observe()` is called on every decision cycle;
    it updates the excursion trackers on pending samples and takes a new
    sample when the interval has elapsed.
    """

    def __init__(
        self,
        symbol: str,
        local_path: str,
        *,
        enabled: bool = False,
        interval_sec: float = 10.0,
        horizons_sec: Optional[List[float]] = None,
        shard_sec: float = 3600.0,
        max_pending: int = 2000,
    ) -> None:
        self.symbol = symbol
        self.local_path = local_path
        self.enabled = bool(enabled)
        self.interval_sec = max(0.5, float(interval_sec))
        self.horizons = sorted({float(h) for h in (horizons_sec or [5, 15, 30, 60, 300]) if h > 0})
        self.max_horizon = self.horizons[-1] if self.horizons else 60.0
        self.shard_sec = max(60.0, float(shard_sec))
        self.max_pending = max(10, int(max_pending))

        self._pending: Deque[Dict[str, Any]] = deque()
        self._ready: List[Dict[str, Any]] = []
        self._last_sample_ts = 0.0
        self._shard_start_ts: Optional[float] = None
        self._shard_path: Optional[str] = None

        # counters, surfaced through stats() for the periodic log line
        self.samples_taken = 0
        self.samples_finalised = 0
        self.samples_dropped = 0
        self.rows_written = 0

    # -- shard naming ------------------------------------------------------
    #
    # A completed shard is never rewritten, so each one is uploaded exactly
    # once. The alternative - one growing file - would re-upload the entire
    # history on every sync and scale badly within hours.

    def _shard_for(self, ts: float) -> str:
        bucket = int(ts // self.shard_sec) * int(self.shard_sec)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(bucket))
        root, ext = os.path.splitext(self.local_path)
        return f"{root}_{stamp}{ext or '.jsonl'}"

    def current_shard_path(self, now: Optional[float] = None) -> str:
        return self._shard_for(now if now is not None else time.time())

    def completed_shards(self) -> List[str]:
        """Shard files on disk that are no longer being appended to, oldest
        first. These are what the sync loop uploads."""
        root, ext = os.path.splitext(self.local_path)
        directory = os.path.dirname(root) or "."
        prefix = os.path.basename(root) + "_"
        active = os.path.basename(self.current_shard_path())
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        out = [
            os.path.join(directory, n)
            for n in sorted(names)
            if n.startswith(prefix) and n.endswith(ext or ".jsonl") and n != active
        ]
        return out

    # -- recording ---------------------------------------------------------

    def observe(
        self,
        now: float,
        price: float,
        *,
        features: Any = None,
        regime: Any = None,
        conf: Any = None,
        decision: Any = None,
        orderflow: Optional[dict] = None,
        brain_readiness: Optional[dict] = None,
        extra: Optional[dict] = None,
    ) -> None:
        """Single entry point, called once per decision cycle. Never raises."""
        if not self.enabled:
            return
        try:
            self._track(now, price)
            if (now - self._last_sample_ts) >= self.interval_sec:
                self._last_sample_ts = now
                self._sample(now, price, features, regime, conf,
                             decision, orderflow, brain_readiness, extra)
        except Exception:  # noqa: BLE001 - recording must never disturb trading
            pass

    def annotate_latest(self, now: float, **fields: Any) -> None:
        """Attach decision context to the sample taken on THIS tick.

        observe() runs before the FLAT gate so market state is captured
        whatever the position is doing, but the engine only produces a
        decision when flat. This fills those fields in afterwards, and only
        when a sample was actually taken this tick - so a decision is never
        stapled onto an older row. Never raises.
        """
        if not self.enabled or not self._pending:
            return
        try:
            latest = self._pending[-1]
            if abs(latest["t"] - now) > 1e-6:
                return          # no sample taken this tick; nothing to annotate
            row = latest["row"]
            for k, v in fields.items():
                if v is not None:
                    row[k] = v
        except Exception:  # noqa: BLE001 - recording must never disturb trading
            pass

    def decision_fields(self, decision: Any, orderflow: Optional[dict] = None,
                        volume_z: Any = None) -> dict:
        """Flatten a decision + orderflow snapshot into row fields."""
        comp = getattr(decision, "components", None) or {}
        of = orderflow or {}
        return {
            "enter": bool(getattr(decision, "should_enter", False)),
            "side": getattr(decision, "side", None),
            "score": _f(getattr(decision, "score", None), 5),
            "thr": _f(comp.get("threshold"), 4),
            "reason": comp.get("rejection_reason"),
            "c_vol": _f(comp.get("volume_confirmation"), 5),
            "c_volfit": _f(comp.get("volatility_fit"), 5),
            "c_mom": _f(comp.get("momentum"), 5),
            "c_mom_raw": _f(comp.get("momentum_raw"), 8),
            "c_mom_mag": _f(comp.get("momentum_magnitude"), 5),
            "c_mom_align": comp.get("momentum_aligned"),
            "c_regfit": _f(comp.get("regime_fit"), 5),
            "exhausted": comp.get("momentum_exhausted"),
            "cm_blocked": comp.get("sideways_counter_momentum_blocked"),
            "of_ok": of.get("data_available"),
            "of_imb": _f(of.get("imbalance"), 5),
            "of_delta": _f(of.get("trade_delta"), 3),
            "of_support": of.get("book_support"),
            "of_aligned": of.get("flow_aligned"),
            "of_blocked": of.get("blocked"),
            "vol_z": _f(volume_z, 5),
            "decided": True,
        }

    def _track(self, now: float, price: float) -> None:
        """Update excursions and horizon returns on every pending sample, then
        finalise the ones whose longest horizon has elapsed.

        Order matters. Finalising first would drop the maturing sample before
        this tick could record its LONGEST horizon, so that column would have
        been null on every row - the dataset would have looked complete while
        silently missing the horizon most likely to matter.
        """
        if price is None or price <= 0:
            return

        for s in self._pending:
            entry = s["px"]
            move = (price - entry) / entry
            if move > s["mfe_long"]:
                s["mfe_long"] = move
            if move < s["mae_long"]:
                s["mae_long"] = move
            age = now - s["t"]
            for h in self.horizons:
                key = f"r{int(h)}"
                if key not in s["fwd"] and age >= h:
                    s["fwd"][key] = move

        while self._pending:
            s = self._pending[0]
            if (now - s["t"]) < self.max_horizon:
                break
            self._pending.popleft()
            self._finalise(s, now)

    def _sample(self, now, price, features, regime, conf,
                decision, orderflow, brain_readiness, extra) -> None:
        if price is None or price <= 0:
            return
        if len(self._pending) >= self.max_pending:
            # Should not happen at the default interval/horizon, but a stalled
            # feed must not grow this unboundedly.
            self._pending.popleft()
            self.samples_dropped += 1

        comp = getattr(decision, "components", None) or {}
        of = orderflow or {}
        row: Dict[str, Any] = {
            "sym": self.symbol,
            "ts": round(now, 3),
            "px": _f(price, 8),
            # --- what the engine decided (the label we are trying to beat) ---
            "enter": bool(getattr(decision, "should_enter", False)),
            "side": getattr(decision, "side", None),
            "score": _f(getattr(decision, "score", None), 5),
            "thr": _f(comp.get("threshold"), 4),
            "reason": comp.get("rejection_reason"),
            # --- regime ---
            "regime": getattr(regime, "regime", None),
            "atr_pct": _f(getattr(regime, "atr_pct", None)),
            "atr_ratio": _f(getattr(regime, "atr_ratio", None), 4),
            "slope": _f(getattr(regime, "trend_slope", None)),
            # --- brain / confidence ---
            "conf": _f(getattr(conf, "confidence_score", None), 5),
            "trend_conf": _f(getattr(conf, "trend_confidence", None), 5),
            "trend_dir": getattr(conf, "trend_direction", None),
            "success_p": _f(getattr(conf, "success_probability", None), 6),
            "tp_hit_p": _f(getattr(conf, "tp_hit_probability", None), 9),
            "noise_p": _f(getattr(conf, "noise_probability", None), 6),
            "risk": _f(getattr(conf, "risk_score", None), 5),
            "quality": _f(getattr(conf, "quality_pred", None), 5),
            # --- score components, exactly as the engine weighted them ---
            "c_vol": _f(comp.get("volume_confirmation"), 5),
            "c_volfit": _f(comp.get("volatility_fit"), 5),
            "c_mom": _f(comp.get("momentum"), 5),
            "c_mom_raw": _f(comp.get("momentum_raw"), 8),
            "c_mom_mag": _f(comp.get("momentum_magnitude"), 5),
            "c_mom_align": comp.get("momentum_aligned"),
            "c_regfit": _f(comp.get("regime_fit"), 5),
            "exhausted": comp.get("momentum_exhausted"),
            "cm_blocked": comp.get("sideways_counter_momentum_blocked"),
            # --- orderflow: the tick-level inputs a kline backtest cannot see ---
            "of_ok": of.get("data_available"),
            "of_imb": _f(of.get("imbalance"), 5),
            "of_delta": _f(of.get("trade_delta"), 3),
            "of_support": of.get("book_support"),
            "of_aligned": of.get("flow_aligned"),
            "of_blocked": of.get("blocked"),
            # --- head readiness, so unreliable rows can be excluded later ---
            "rdy": {k: v for k, v in (brain_readiness or {}).items()} or None,
            # False until annotate_latest() supplies a decision; a sample
            # taken while a position is open has market state but no
            # entry decision, and the two must be distinguishable.
            "decided": False,
            "pos_status": None,
        }
        if features is not None:
            try:
                row["f"] = [_f(v, 6) for v in list(features)]
            except (TypeError, ValueError):
                pass
        if extra:
            row.update(extra)

        self._pending.append({
            "t": now, "px": float(price), "row": row,
            "fwd": {}, "mfe_long": 0.0, "mae_long": 0.0,
        })
        self.samples_taken += 1

    def _finalise(self, s: Dict[str, Any], now: float) -> None:
        """Attach realised outcomes and queue the row for writing.

        Forward returns are signed from a LONG perspective. A short's return
        is just the negation, so storing one direction keeps the file smaller
        and avoids baking the engine's side choice into the label - which
        matters, because the side choice is exactly what is under suspicion.
        """
        row = s["row"]
        for h in self.horizons:
            key = f"r{int(h)}"
            row[key] = _f(s["fwd"].get(key), 8)
        row["mfe_long"] = _f(s["mfe_long"], 8)
        row["mae_long"] = _f(s["mae_long"], 8)
        # Convenience label: did price move further up than down over the
        # window? The 45%-vs-50% figure that motivated this module.
        row["up_won"] = bool(abs(s["mfe_long"]) > abs(s["mae_long"]))
        self._ready.append(row)
        self.samples_finalised += 1

    # -- persistence -------------------------------------------------------

    def flush(self, now: Optional[float] = None) -> int:
        """Append finalised rows to the current shard. Returns rows written."""
        if not self.enabled or not self._ready:
            return 0
        now = now if now is not None else time.time()
        path = self.current_shard_path(now)
        rows, self._ready = self._ready, []
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001 - a disk problem must not stop trading
            return 0
        self.rows_written += len(rows)
        self._shard_path = path
        return len(rows)

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "taken": self.samples_taken,
            "finalised": self.samples_finalised,
            "written": self.rows_written,
            "pending": len(self._pending),
            "dropped": self.samples_dropped,
            "shard": os.path.basename(self._shard_path) if self._shard_path else None,
        }
