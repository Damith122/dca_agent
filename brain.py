#!/usr/bin/env python3
"""
================================================================================
 Brain V2 - moved out of dca2.py

 This file contains ONLY the classes that were relocated out of dca2.py's
 "BRAIN V2 - probability / confidence engine" section: `BrainV2` itself and
 `RunningNormalizer`, the small online normalizer it instantiates internally
 (self.norm = RunningNormalizer(...) in BrainV2.__init__). RunningNormalizer
 is not used anywhere else in dca2.py, so it travels with BrainV2 rather than
 staying behind - splitting them would only recreate the same coupling
 through an extra import.

 Every method body below is byte-for-byte identical to the original
 dca2.py source. Nothing was renamed, fixed, or optimized.

 One structural note on the move itself (not a logic change): BrainV2's
 `predict_all` and `from_bytes` call `clamp()` / `color()` / `YELLOW`, which
 live in dca2.py's UTIL section. Importing them back from dca2.py would
 create a circular import (dca2.py imports BrainV2 from here). To keep this
 module self-contained, this file carries its own private copies of those
 three tiny, generic helpers - defined identically to dca2.py's versions.
 They are formatting/math utilities, not part of the Brain's behavior, and
 are not exported for use elsewhere.

 2026-08 STRATEGY UPGRADE - LiquidityFlowGuard (additive; BrainV2 and
 RunningNormalizer below are untouched, still byte-for-byte the original
 dca2.py code): a small, pure, stateless evaluator for the orderbook
 imbalance / aggregated-trade-flow entry conditions. It lives here because
 it is strategy/signal logic that sits directly alongside the Brain's own
 directional output, and because keeping it dependency-free (config only -
 no numpy, no sklearn, no websocket/exchange imports) makes it trivially
 unit-testable in isolation. EntryEngineV2 in trading.py applies it AFTER
 the existing technical scoring, both as a hard veto and as the extra two
 legs of a three-way multi-factor confirmation. See the class banner below.
================================================================================
"""

from __future__ import annotations

import pickle
import sys
from typing import Optional

import numpy as np
from sklearn.linear_model import SGDRegressor, SGDClassifier

from config import (
    N_FEATURES_V2,
    BRAIN2_WARMUP_UPDATES,
    BRAIN_HEAD_MIN_SAMPLES,
    # --- 2026-08 Liquidity & Flow Guard (appended config) ---------------
    ENABLE_ORDERBOOK_GUARD,
    ORDERBOOK_IMBALANCE_THRESHOLD,
    ORDERBOOK_SUPPORT_MIN,
    AGG_TRADE_DELTA_MIN,
    ORDERBOOK_GUARD_REQUIRES_DATA,
)

# ----------------------------------------------------------------------------
# Private helpers (identical copies of dca2.py's color()/YELLOW/clamp() -
# duplicated only to avoid a circular import; see module docstring above).
# ----------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def color(text: str, code: str) -> str:
    if not _USE_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


GREEN, RED, YELLOW, CYAN, GRAY, BOLD, MAGENTA, BLUE = "32", "31", "33", "36", "90", "1", "35", "34"


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ============================================================================
# LIQUIDITY & FLOW GUARD (2026-08 upgrade)
# ============================================================================
# Purely ADDITIVE strategy layer. It does not read, replace or weaken any
# existing technical check: the EMA/RSI/ATR stack, MarketRegimeEngine, the
# ENTRY_WEIGHTS composite score and its ENTRY_SCORE_THRESHOLD acceptance bar
# all run first and are completely untouched. This guard is applied AFTER
# them, in EntryEngineV2.evaluate() (trading.py), as:
#
#   (a) a HARD VETO - a blocked side can never reach should_enter=True no
#       matter how high the technical score came in, exactly like the
#       pre-existing regime / dead-market / counter-momentum hard blocks; and
#   (b) the extra two factors of a MULTI-FACTOR CONFIRMATION requirement -
#       a trade opens only when the TECHNICAL signal, the ORDERBOOK support
#       and the 10s trade-FLOW delta all point the same way.
#
# The veto rules are exactly as specified:
#     BLOCK LONG  if orderbook_imbalance < -THRESHOLD  OR  10s_delta < 0
#     BLOCK SHORT if orderbook_imbalance > +THRESHOLD  OR  10s_delta > 0
# with THRESHOLD defaulting to ORDERBOOK_IMBALANCE_THRESHOLD (0.20) and
# tightened by the caller during the post-loss cool-off window (see
# trading.py's cool-off handling - this class never reads wall-clock time,
# it is a pure function of the numbers it is handed, which is what makes it
# trivially testable).
# ============================================================================


class LiquidityFlowGuard:
    """Pure, stateless evaluator for the orderbook/flow entry conditions.

    Held as a plain object (rather than module functions) so a caller can
    hand it per-decision thresholds - the post-loss cool-off does exactly
    that - without any global mutation.
    """

    def __init__(
        self,
        enabled: bool = ENABLE_ORDERBOOK_GUARD,
        imbalance_threshold: float = ORDERBOOK_IMBALANCE_THRESHOLD,
        support_min: float = ORDERBOOK_SUPPORT_MIN,
        delta_min: float = AGG_TRADE_DELTA_MIN,
        require_data: bool = ORDERBOOK_GUARD_REQUIRES_DATA,
    ):
        self.enabled = enabled
        self.imbalance_threshold = imbalance_threshold
        self.support_min = support_min
        self.delta_min = delta_min
        self.require_data = require_data

    def evaluate(
        self,
        side: Optional[str],
        orderflow: Optional[dict],
        imbalance_threshold: Optional[float] = None,
        support_min: Optional[float] = None,
        delta_min: Optional[float] = None,
    ) -> dict:
        """Decide whether `side` ("LONG"/"SHORT") may be opened right now.

        `orderflow` is an OrderFlowTracker.snapshot() dict (see
        websocket.py) - or None, which means this process has no orderflow
        wiring at all (e.g. a focused unit test constructing EntryEngineV2
        directly). None is treated as "guard not applicable" so every
        pre-existing caller behaves exactly as it did before this upgrade.

        Returns a dict with:
            blocked          - True if this side must be vetoed
            reason           - short machine-readable reason ("" if allowed)
            book_support     - True if the book itself supports this side
            flow_aligned     - True if the 10s trade delta supports this side
            confirmed        - book_support AND flow_aligned (the two
                               non-technical legs of multi-factor
                               confirmation)
            imbalance / trade_delta / threshold - the values actually used,
                               so the caller can log an auditable decision.
        """
        thr = self.imbalance_threshold if imbalance_threshold is None else imbalance_threshold
        sup = self.support_min if support_min is None else support_min
        dmin = self.delta_min if delta_min is None else delta_min

        result = {
            "blocked": False,
            "reason": "",
            "book_support": False,
            "flow_aligned": False,
            "confirmed": False,
            "active": False,
            "imbalance": 0.0,
            "trade_delta": 0.0,
            "threshold": thr,
            "support_min": sup,
            "delta_min": dmin,
            "data_available": False,
        }

        if not self.enabled or orderflow is None or side not in ("LONG", "SHORT"):
            # Guard disabled, not wired, or nothing directional to judge -
            # confirmation legs are reported as satisfied so the caller's
            # multi-factor AND-chain degrades to the pre-upgrade behavior
            # rather than blocking every trade.
            result["book_support"] = True
            result["flow_aligned"] = True
            result["confirmed"] = True
            return result

        result["active"] = True
        data_available = bool(orderflow.get("data_available"))
        result["data_available"] = data_available
        imbalance = float(orderflow.get("imbalance", 0.0) or 0.0)
        trade_delta = float(orderflow.get("trade_delta", 0.0) or 0.0)
        result["imbalance"] = imbalance
        result["trade_delta"] = trade_delta

        if not data_available:
            # Fail-SAFE, not fail-open: a silently dead depth/trade feed
            # must never be mistaken for a neutral, permissive book. With
            # ORDERBOOK_GUARD_REQUIRES_DATA=false this degrades to the
            # pre-upgrade behavior instead (documented escape hatch).
            if self.require_data:
                result["blocked"] = True
                result["reason"] = "orderflow_data_unavailable"
                return result
            result["book_support"] = True
            result["flow_aligned"] = True
            result["confirmed"] = True
            return result

        if side == "LONG":
            if imbalance < -thr:
                result["blocked"] = True
                result["reason"] = "orderbook_imbalance_against_long"
                return result
            if trade_delta < 0:
                result["blocked"] = True
                result["reason"] = "trade_delta_against_long"
                return result
            result["book_support"] = imbalance > sup
            result["flow_aligned"] = trade_delta > dmin
        else:  # SHORT
            if imbalance > thr:
                result["blocked"] = True
                result["reason"] = "orderbook_imbalance_against_short"
                return result
            if trade_delta > 0:
                result["blocked"] = True
                result["reason"] = "trade_delta_against_short"
                return result
            result["book_support"] = imbalance < -sup
            result["flow_aligned"] = trade_delta < -dmin

        result["confirmed"] = result["book_support"] and result["flow_aligned"]
        if not result["confirmed"]:
            result["blocked"] = True
            result["reason"] = (
                "no_orderbook_support" if not result["book_support"] else "flow_delta_not_aligned"
            )
        return result

    def supports_reversal(
        self, side: Optional[str], orderflow: Optional[dict], support_min: float = 0.0,
    ) -> bool:
        """Does the resting book currently support a RECOVERY of an open
        `side` position? Used by trading.py's Safe DCA rescue rule, which
        may only add once and only when bids (for a LONG) / asks (for a
        SHORT) are genuinely backing the reversal. Returns True when the
        guard is disabled or no orderflow data is wired, so a deployment
        that turns the guard off keeps the pre-upgrade DCA behavior."""
        if not self.enabled or orderflow is None or side not in ("LONG", "SHORT"):
            return True
        if not orderflow.get("data_available"):
            return not self.require_data
        imbalance = float(orderflow.get("imbalance", 0.0) or 0.0)
        return imbalance > support_min if side == "LONG" else imbalance < -support_min


# ============================================================================
# BRAIN V2 - probability / confidence engine (replaces the old direction-only
# predictor). Runs several small online models in parallel over the SAME
# normalized feature vector and turns their outputs into a set of
# probabilities/scores that the rest of the stack consumes.
# ============================================================================


# ---------------------------------------------------------------------------
# Shared classifier hyperparameters (2026-08-21 saturation fix). Kept in one
# place so the three log-loss heads can never drift apart again - it was
# exactly such a drift (regressors given eta0=0.01, classifiers left on the
# alpha-derived "optimal" schedule) that produced the divergence.
# ---------------------------------------------------------------------------
_CLASSIFIER_KW = dict(
    loss="log_loss", penalty="l2", alpha=1e-5,
    learning_rate="constant", eta0=0.01, warm_start=True,
)

# 2026-08-22, revised against live data. The first value (5.0) was calibrated
# on synthetic features, where a healthy head settled near |coef| 0.6 and a
# diverged one ran to 14+. Real market features are far less well-behaved: a
# head rebuilt at 05:17 and relearning correctly under the corrected schedule
# reached 5.5 within ten minutes, and was destroyed by its own screen on the
# next restart - while emitting a perfectly healthy spread of probabilities
# (0.0001 / 0.5852 / 0.9006 across symbols, not the pinned extremes that
# define divergence).
#
# Left at 5.0 this becomes self-defeating: any restart during early relearning
# wipes the head, so it can never accumulate enough samples to become
# reliable.
#
# The screen no longer needs to be sensitive. Every snapshot predating the
# fix is now caught definitively by the missing base-rate-window field, which
# is an exact test rather than a heuristic. All this threshold has to catch is
# NEW runaway under the corrected schedule, so it is set well clear of what a
# healthy head reaches. Observed diverged peaks under the old schedule ran to
# 52, 111, 133 and 387, so genuine divergence remains comfortably visible.
SATURATED_COEF_ABS = 100.0


class RunningNormalizer:
    """Welford online mean/variance normalizer, one instance per model head
    (kept separate from the feature vector itself so features stay in their
    natural, somewhat-interpretable units for logging/regime logic, while
    each model still gets a properly normalized input)."""

    def __init__(self, n_features: int):
        self.n_features = n_features
        self._n_seen = 0
        self._mean = np.zeros(n_features, dtype=float)
        self._m2 = np.zeros(n_features, dtype=float)

    def update(self, x: np.ndarray) -> None:
        self._n_seen += 1
        delta = x - self._mean
        self._mean += delta / self._n_seen
        delta2 = x - self._mean
        self._m2 += delta * delta2

    def normalize(self, x: np.ndarray) -> np.ndarray:
        if self._n_seen < 2:
            return x
        variance = self._m2 / max(self._n_seen - 1, 1)
        std = np.sqrt(variance)
        std = np.where(std < 1e-8, 1.0, std)
        return (x - self._mean) / std

    def state(self) -> dict:
        return {"n_features": self.n_features, "_n_seen": self._n_seen, "_mean": self._mean, "_m2": self._m2}

    def load(self, state: dict) -> None:
        self._n_seen = state["_n_seen"]
        self._mean = state["_mean"]
        self._m2 = state["_m2"]


class BrainV2:
    """Multi-head online model:

      - trend model      (SGDRegressor)  -> signed forward-return estimate;
                                             trend_confidence = clamp(|pred|/scale)
      - noise model       (SGDClassifier) -> P(this tick's move is noise, i.e.
                                             forward move stays inside the
                                             typical volatility band)
      - success model     (SGDClassifier) -> P(a trade opened here ends net
                                             profitable after fees)
      - tp_hit model       (SGDClassifier) -> P(price reaches the dynamic TP
                                             distance within TP_HIT_LOOKAHEAD
                                             candles, before the hard stop)
      - quality model      (SGDRegressor)  -> predicts the composite REWARD
                                             (see RewardCalculator) a trade
                                             opened here would earn - this is
                                             what "good trading behavior"
                                             actually trains against, not
                                             raw PnL.

    confidence_score / risk_score / hold_probability / exit_probability are
    DERIVED (in ConfidenceEngine) from these five heads plus the heuristic
    RiskEngine output - they are not separate models, since they are
    algebraic combinations of the others by design (keeps the learned
    model count small and each one well-identified, which matters a lot
    for a low-sample online learner).
    """

    def __init__(self, n_features: int = N_FEATURES_V2, warmup_updates: int = BRAIN2_WARMUP_UPDATES):
        self.n_features = n_features
        self.warmup_updates = warmup_updates

        self.trend_model = SGDRegressor(
            loss="squared_error", penalty="l2", alpha=1e-5,
            learning_rate="invscaling", eta0=0.01, power_t=0.25, warm_start=True,
        )
        self.quality_model = SGDRegressor(
            loss="huber", penalty="l2", alpha=1e-5,
            learning_rate="invscaling", eta0=0.01, power_t=0.25, warm_start=True,
        )
        # 2026-08-21 saturation fix. All three classifiers previously used
        # learning_rate="optimal" with no eta0. For log_loss, sklearn derives
        # that schedule from alpha alone:
        #
        #     typw = sqrt(1/sqrt(alpha));  eta0 = typw;  t0 = 1/(eta0*alpha)
        #     eta(t) = 1 / (alpha * (t + t0))
        #
        # At alpha=1e-5 that is eta(1) = 17.78 - 1778x the 0.01 the two
        # REGRESSORS above were explicitly given, and still 0.099 after a
        # million updates. On normalized features that diverges almost
        # immediately: weights grow without bound and predict_proba collapses
        # onto 0.0/1.0. The live tp_hit head was emitting probabilities around
        # 1e-200 for exactly this reason.
        #
        # The tell that it was divergence rather than confidence: measured on
        # synthetic data with a known signal, the "optimal" schedule scored
        # AUC 0.614 with an IDENTICAL outcome rate in its top and bottom
        # deciles (0.0050 vs 0.0050) - its extreme probabilities carried no
        # information at all. A constant eta0=0.01 scored AUC 0.703 and
        # separated those deciles 0.0000 vs 0.0300.
        #
        # Matching the regressors' eta0 keeps every head on one schedule.
        self.noise_model = SGDClassifier(**_CLASSIFIER_KW)
        self.success_model = SGDClassifier(**_CLASSIFIER_KW)
        self.tp_hit_model = SGDClassifier(**_CLASSIFIER_KW)

        self.norm = RunningNormalizer(n_features)

        self.trend_fitted = False
        self.quality_fitted = False
        self.noise_fitted = False
        self.success_fitted = False
        self.tp_hit_fitted = False

        self.update_count = 0
        self.last_trend_pred: Optional[float] = None
        self.last_noise_prob: Optional[float] = None
        self.last_success_prob: Optional[float] = None
        self.last_tp_hit_prob: Optional[float] = None
        self.last_quality_pred: Optional[float] = None

        # scale used to squash trend_model's raw regression output into a
        # 0..1 "trend_confidence" - set from observed prediction magnitude,
        # starts at a sane prior and adapts slowly.
        # Scale for trend_confidence. This must track the size of the
        # PREDICTIONS, not the size of realised returns - see predict_all().
        self._trend_scale = 0.0015
        self._pred_scale = 0.0015
        self._pred_scale_samples = 0

        # 2026-08 entry-quality audit fix (confirmed defect #6/#9 - see
        # config.py's BRAIN_HEAD_MIN_SAMPLES comment for the full root
        # cause): per-head sample counters and observed-class sets,
        # SEPARATE from update_count (which only reflects the trend head's
        # own tick-driven learning and is not a meaningful proxy for how
        # reliable success/tp_hit/noise are). Used by predict_all() below
        # to report a neutral 0.5 instead of a possibly one-class-saturated
        # predict_proba() output until each classifier head has genuinely
        # seen both outcome classes and at least BRAIN_HEAD_MIN_SAMPLES
        # labeled samples.
        self.noise_samples = 0
        self.success_samples = 0
        self.tp_hit_samples = 0
        # Base-rate window (2026-08-22). positives/labeled_samples must be
        # measured over the SAME span of labels or the ratio is meaningless,
        # so they are incremented together and persisted together. This is
        # deliberately NOT tp_hit_samples: that counter predates positive
        # tracking, and reusing it mixed 27.6M historical samples with only
        # the positives seen since - understating the live ETH base rate by
        # ~250x (2.1e-06 against ~5e-04 on every other symbol).
        self.tp_hit_positives = 0
        self.tp_hit_labeled_samples = 0
        # Rolling positive rate. The cumulative ratio cannot forget: after
        # three million samples under one label definition it needs three
        # million more to reflect a new one. An EWMA converges in roughly
        # 1/alpha samples instead, so a label change is absorbed in hours
        # rather than never. See tp_hit_base_rate().
        self.tp_hit_rate_ewma = 0.0
        self.tp_hit_rate_samples = 0
        self._noise_classes_seen: set = set()
        self._success_classes_seen: set = set()
        self._tp_hit_classes_seen: set = set()

    # -- head reliability (2026-08 entry-quality audit fix) -------------------

    def _head_reliable(self, samples: int, classes_seen: set) -> bool:
        """True once a classifier head has seen BOTH outcome classes and at
        least BRAIN_HEAD_MIN_SAMPLES labeled samples - see config.py's
        BRAIN_HEAD_MIN_SAMPLES comment for why "fitted" alone (flips True
        after a single partial_fit call) is not a safe reliability signal
        for a low-sample online SGDClassifier."""
        return samples >= BRAIN_HEAD_MIN_SAMPLES and len(classes_seen) >= 2

    def head_readiness(self) -> dict:
        """READY / WARMING_UP summary per model head, for logging only (see
        trading.py's entry-decision log) - does not affect predict_all()'s
        own gating, which is computed independently each call from the same
        underlying counters."""
        def state(fitted: bool, reliable: bool) -> str:
            if not fitted:
                return "WARMING_UP"
            return "READY" if reliable else "UNRELIABLE"

        return {
            "trend": "READY" if self.is_ready() else "WARMING_UP",
            "noise": state(self.noise_fitted, self._head_reliable(self.noise_samples, self._noise_classes_seen)),
            "success": state(self.success_fitted, self._head_reliable(self.success_samples, self._success_classes_seen)),
            "tp_hit": state(self.tp_hit_fitted, self._head_reliable(self.tp_hit_samples, self._tp_hit_classes_seen)),
            "quality": "READY" if self.quality_fitted else "WARMING_UP",
            "update_count": self.update_count,
            "noise_samples": self.noise_samples,
            "success_samples": self.success_samples,
            "tp_hit_samples": self.tp_hit_samples,
        }

    # -- prediction -----------------------------------------------------------

    def predict_all(self, x: np.ndarray) -> dict:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            x = np.nan_to_num(x)
        xn = self.norm.normalize(x).reshape(1, -1)

        trend_pred = float(self.trend_model.predict(xn)[0]) if self.trend_fitted else 0.0
        quality_pred = float(self.quality_model.predict(xn)[0]) if self.quality_fitted else 0.0

        # 2026-08 entry-quality audit fix: each classifier head's reported
        # probability falls back to the neutral prior (0.5) - identical to
        # the existing "not yet fitted" default - until that head has both
        # observed both outcome classes AND accumulated BRAIN_HEAD_MIN_SAMPLES
        # labeled samples. A "fitted" head that hasn't cleared this bar is
        # exactly the one-class/tiny-sample saturation scenario the attached
        # live evidence showed (entry_success_prob=1.0 from ~1 completed
        # trade) - this does not change what partial_fit() learns, only
        # whether predict_all() treats the resulting predict_proba() as
        # trustworthy enough to report.
        noise_reliable = self._head_reliable(self.noise_samples, self._noise_classes_seen)
        success_reliable = self._head_reliable(self.success_samples, self._success_classes_seen)
        tp_hit_reliable = self._head_reliable(self.tp_hit_samples, self._tp_hit_classes_seen)

        noise_prob = (
            float(self.noise_model.predict_proba(xn)[0][1])
            if (self.noise_fitted and noise_reliable) else 0.5
        )
        success_prob = (
            float(self.success_model.predict_proba(xn)[0][1])
            if (self.success_fitted and success_reliable) else 0.5
        )
        tp_hit_prob = (
            float(self.tp_hit_model.predict_proba(xn)[0][1])
            if (self.tp_hit_fitted and tp_hit_reliable) else 0.5
        )

        self.last_trend_pred = trend_pred
        self.last_noise_prob = noise_prob
        self.last_success_prob = success_prob
        self.last_tp_hit_prob = tp_hit_prob
        self.last_quality_pred = quality_pred

        # 2026-08-28: this divided |prediction| by an EWMA of realised
        # per-tick |return|. Those are different quantities in different
        # units of typical magnitude, and the realised EWMA converges to
        # about 0.7 bps while the regressor emits predictions comfortably
        # above that - so the ratio exceeded 1 on essentially every tick and
        # clamped. Live evidence: trend_confidence was 1.000000 in all
        # 84,744 recorded rows over 64 hours, one distinct value.
        #
        # Confidence has to be relative to the prediction's OWN
        # distribution: "is this forecast large compared with the forecasts
        # this model usually makes". That is a ratio of like to like, and it
        # can actually vary.
        self._pred_scale = (0.99 * self._pred_scale
                            + 0.01 * max(abs(trend_pred), 1e-9))
        self._pred_scale_samples += 1
        if self._pred_scale_samples < 200:
            # Until the scale has settled, a confidence would be arbitrary.
            trend_confidence = 0.5
        else:
            trend_confidence = clamp(
                abs(trend_pred) / max(self._pred_scale * 2.0, 1e-12), 0.0, 1.0)
        trend_direction = "LONG" if trend_pred > 0 else ("SHORT" if trend_pred < 0 else None)

        return {
            "trend_pred": trend_pred,
            "trend_confidence": trend_confidence,
            "trend_direction": trend_direction,
            "noise_probability": noise_prob,
            "success_probability": success_prob,
            "tp_hit_probability": tp_hit_prob,
            "quality_pred": quality_pred,
        }

    # -- online learning --------------------------------------------------------

    def learn_trend(self, x: np.ndarray, forward_return: float) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)) or not np.isfinite(forward_return):
            return
        self.norm.update(x)
        xn = self.norm.normalize(x).reshape(1, -1)
        self.trend_model.partial_fit(xn, [float(forward_return)])
        self.trend_fitted = True
        # slowly adapt the confidence-squash scale toward observed |return| typical size
        self._trend_scale = 0.98 * self._trend_scale + 0.02 * max(abs(forward_return), 1e-6)
        self.update_count += 1

    def learn_noise(self, x: np.ndarray, is_noise: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.noise_fitted else None
        self.noise_model.partial_fit(xn, [1 if is_noise else 0], classes=classes)
        self.noise_fitted = True
        self.noise_samples += 1
        self._noise_classes_seen.add(1 if is_noise else 0)

    def learn_success(self, x: np.ndarray, was_success: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.success_fitted else None
        self.success_model.partial_fit(xn, [1 if was_success else 0], classes=classes)
        self.success_fitted = True
        self.success_samples += 1
        self._success_classes_seen.add(1 if was_success else 0)

    def learn_tp_hit(self, x: np.ndarray, tp_was_hit: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.tp_hit_fitted else None
        self.tp_hit_model.partial_fit(xn, [1 if tp_was_hit else 0], classes=classes)
        self.tp_hit_fitted = True
        self.tp_hit_samples += 1
        self.tp_hit_labeled_samples += 1
        hit = 1.0 if tp_was_hit else 0.0
        if self.tp_hit_rate_samples == 0:
            self.tp_hit_rate_ewma = hit
        else:
            self.tp_hit_rate_ewma = ((1.0 - self._TP_RATE_ALPHA)
                                     * self.tp_hit_rate_ewma
                                     + self._TP_RATE_ALPHA * hit)
        self.tp_hit_rate_samples += 1
        if tp_was_hit:
            self.tp_hit_positives += 1
        self._tp_hit_classes_seen.add(1 if tp_was_hit else 0)

    def learn_quality(self, x: np.ndarray, reward: float) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)) or not np.isfinite(reward):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        self.quality_model.partial_fit(xn, [float(reward)])
        self.quality_fitted = True

    def is_ready(self) -> bool:
        return self.trend_fitted and self.update_count >= self.warmup_updates

    # -- persistence ------------------------------------------------------------

    # -- saturation detection & head reset (2026-08-21) ---------------------
    #
    # Correcting the learning rate above only governs FUTURE updates. Every
    # brain.pkl already in circulation carries weights that diverged under
    # the old schedule, and log-loss gradients are bounded, so a diverged head
    # would take millions of corrective samples to crawl back - it cannot
    # recover in practice. Such a head has to be retrained from scratch.
    #
    # These heads are cheap to rebuild: they learn once per tick, so a reset
    # head is back to a reliable state within minutes of live data, whereas
    # leaving it in place keeps feeding the strategy a meaningless number.

    _CLASSIFIER_HEADS = ("noise", "success", "tp_hit")

    # Bumped whenever the noise LABEL definition changes, so a snapshot
    # trained under the old one can be recognised and rebuilt.
    _NOISE_LABEL_VERSION = 2

    def _head_is_saturated(self, name: str) -> bool:
        model = getattr(self, f"{name}_model", None)
        coef = getattr(model, "coef_", None)
        if coef is None:
            return False
        try:
            peak = float(np.max(np.abs(coef)))
        except (TypeError, ValueError):
            return False
        return np.isfinite(peak) and peak >= SATURATED_COEF_ABS

    def reset_head(self, name: str) -> None:
        """Rebuild one classifier head from zero, clearing its weights and
        every counter that gates its reliability, so it must earn READY again
        rather than inheriting the old head's standing."""
        setattr(self, f"{name}_model", SGDClassifier(**_CLASSIFIER_KW))
        setattr(self, f"{name}_fitted", False)
        setattr(self, f"{name}_samples", 0)
        setattr(self, f"_{name}_classes_seen", set())
        if name == "tp_hit":
            self.tp_hit_positives = 0
            self.tp_hit_labeled_samples = 0
            self.tp_hit_rate_ewma = 0.0
            self.tp_hit_rate_samples = 0

    def reset_saturated_heads(self) -> list:
        """Reset every classifier head whose weights diverged. Returns the
        names reset, so startup can report it rather than silently discarding
        learned state."""
        reset = []
        for name in self._CLASSIFIER_HEADS:
            if self._head_is_saturated(name):
                peak = float(np.max(np.abs(getattr(self, f"{name}_model").coef_)))
                self.reset_head(name)
                reset.append((name, peak))
        return reset

    _TP_RATE_ALPHA = 1e-4          # converges in ~10k samples
    _TP_RATE_WARMUP = 20_000       # two time constants before trusting it

    def tp_hit_base_rate(self) -> float:
        """Observed positive rate of the tp_hit label, over a ROLLING window.

        Any threshold on the head's probability has to be relative to this
        rate rather than an absolute floor.

        It is rolling because the label definition changed on 2026-08-28.
        The old label asked whether price moved TAKE_PROFIT_PCT within
        LABEL_HORIZON_TICKS - about 35 bps in 2.5 seconds - which was true
        0.003% of the time. The new one scales the threshold to the move
        typical at that horizon and is true around 25% of the time. A
        cumulative ratio mixes the two forever: three million samples of the
        old definition would need three million of the new one to be
        forgotten, so the reported rate stayed near 0.04% while the head
        emitted probabilities between 0.09 and 0.68. Any veto computed
        against that mixture is a no-op.

        The EWMA converges in roughly 1/alpha samples, so a future label
        change is absorbed in hours. The cumulative ratio is still returned
        during warm-up, when the EWMA has not settled."""
        if self.tp_hit_rate_samples >= self._TP_RATE_WARMUP:
            return float(self.tp_hit_rate_ewma)
        if self.tp_hit_labeled_samples <= 0:
            return 0.0
        return self.tp_hit_positives / float(self.tp_hit_labeled_samples)

    def to_state(self) -> dict:
        return {
            # 2026-08 entry-quality audit fix: version bumped 2 -> 3 to
            # carry the new per-head sample counters/classes-seen sets
            # (see __init__). from_bytes() below still ACCEPTS a version-2
            # snapshot (migrates it - see load_state()'s .get() defaults)
            # rather than rejecting it, so an existing live brain.pkl is
            # never silently wiped by this change - only a genuinely
            # unreadable/corrupt/wrong-n_features snapshot falls back to a
            # fresh Brain, exactly as before.
            "version": 4,
            "n_features": self.n_features,
            "warmup_updates": self.warmup_updates,
            "trend_model": self.trend_model, "quality_model": self.quality_model,
            "noise_model": self.noise_model, "success_model": self.success_model,
            "tp_hit_model": self.tp_hit_model,
            "trend_fitted": self.trend_fitted, "quality_fitted": self.quality_fitted,
            "noise_fitted": self.noise_fitted, "success_fitted": self.success_fitted,
            "tp_hit_fitted": self.tp_hit_fitted,
            "update_count": self.update_count,
            "_trend_scale": self._trend_scale,
            "_pred_scale": self._pred_scale,
            "_pred_scale_samples": self._pred_scale_samples,
            "norm": self.norm.state(),
            "noise_samples": self.noise_samples,
            "success_samples": self.success_samples,
            "tp_hit_samples": self.tp_hit_samples,
            "tp_hit_positives": self.tp_hit_positives,
            "tp_hit_labeled_samples": self.tp_hit_labeled_samples,
            "tp_hit_rate_ewma": self.tp_hit_rate_ewma,
            "tp_hit_rate_samples": self.tp_hit_rate_samples,
            # Which noise LABEL the weights were trained under. 1 = the
            # half-a-one-minute-ATR band, which was True almost always and
            # produced a head pinned near 1.0; 2 = the horizon-matched band.
            # A head trained under 1 cannot be corrected by further updates
            # at any realistic rate - the two labels disagree on most
            # samples - so it is rebuilt on load. See from_bytes().
            "noise_label_version": self._NOISE_LABEL_VERSION,
            "noise_classes_seen": sorted(self._noise_classes_seen),
            "success_classes_seen": sorted(self._success_classes_seen),
            "tp_hit_classes_seen": sorted(self._tp_hit_classes_seen),
        }

    def load_state(self, state: dict) -> None:
        self.trend_model = state["trend_model"]
        self.quality_model = state["quality_model"]
        self.noise_model = state["noise_model"]
        self.success_model = state["success_model"]
        self.tp_hit_model = state["tp_hit_model"]
        self.trend_fitted = state["trend_fitted"]
        self.quality_fitted = state["quality_fitted"]
        self.noise_fitted = state["noise_fitted"]
        self.success_fitted = state["success_fitted"]
        self.tp_hit_fitted = state["tp_hit_fitted"]
        self.update_count = state["update_count"]
        self._trend_scale = state.get("_trend_scale", 0.0015)
        self._pred_scale = state.get("_pred_scale", 0.0015)
        self._pred_scale_samples = int(state.get("_pred_scale_samples", 0))
        self.norm.load(state["norm"])
        # 2026-08 entry-quality audit fix - migration-safe: a version-2
        # snapshot (or any snapshot predating this fix) simply has none of
        # these keys, so every counter/class-set below defaults to "no
        # confirmed-reliable samples yet" - the CONSERVATIVE direction
        # (predict_all() reports neutral 0.5 for that head until enough
        # NEW samples accumulate post-upgrade). The underlying learned
        # model weights themselves (trend/quality/noise/success/tp_hit
        # models above) are restored unchanged either way - this never
        # resets/discards learned state, it only resets the reliability
        # bookkeeping for classifier heads whose sample provenance predates
        # this fix and was never tracked.
        self.noise_samples = int(state.get("noise_samples", 0))
        self.success_samples = int(state.get("success_samples", 0))
        self.tp_hit_samples = int(state.get("tp_hit_samples", 0))
        self.tp_hit_positives = int(state.get("tp_hit_positives", 0))
        self.tp_hit_rate_ewma = float(state.get("tp_hit_rate_ewma", 0.0))
        self.tp_hit_rate_samples = int(state.get("tp_hit_rate_samples", 0))
        self.tp_hit_labeled_samples = int(state.get("tp_hit_labeled_samples", 0))
        self._noise_classes_seen = set(state.get("noise_classes_seen", []))
        self._success_classes_seen = set(state.get("success_classes_seen", []))
        self._tp_hit_classes_seen = set(state.get("tp_hit_classes_seen", []))

    def to_bytes(self) -> bytes:
        return pickle.dumps(self.to_state(), protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def from_bytes(cls, data: bytes, n_features: int, warmup_updates: int) -> "BrainV2":
        """Falls back to a fresh (cold) brain on any corruption, version
        mismatch, or feature-shape mismatch - a bad/stale snapshot must
        never prevent the bot from starting.

        2026-08 entry-quality audit fix (this version check only - every
        other line of load behavior is unchanged): accepts BOTH version 2
        (the pre-existing on-disk/GitHub format - every currently deployed
        brain.pkl) and version 3 (this fix's new format, adding per-head
        sample counters) so upgrading this code never rejects/wipes an
        existing live-learned Brain snapshot. See load_state() for exactly
        what a migrated version-2 snapshot defaults to.
        """
        brain = cls(n_features, warmup_updates)
        try:
            state = pickle.loads(data)
            if state.get("version") not in (2, 3, 4) or state.get("n_features") != n_features:
                print(color(
                    f"[brain] snapshot incompatible (version={state.get('version')}, "
                    f"n_features={state.get('n_features')}, expected {n_features}) - "
                    f"starting a fresh Brain V2.", YELLOW,
                ))
                return brain
            brain.load_state(state)
            # 2026-08-21 (second pass, from live data): ANY snapshot written
            # before version 4 had its classifier heads trained under the
            # divergent "optimal" schedule, so none of their weights can be
            # trusted - regardless of where |coef| happens to sit right now.
            # A head that drifted only as far as 4.9 is not healthy, it is
            # merely less far gone, and the |coef| screen below would wave it
            # through.
            #
            # It also fixes a counter-window bug that screen created. A head
            # that escaped the reset kept its restored tp_hit_samples (27.6M
            # on the live ETH brain) while tp_hit_positives, absent from the
            # older format, defaulted to 0. Base rate is positives/samples,
            # so measuring the two over different windows pinned it at
            # exactly 0.0 and silently disabled the veto for that symbol -
            # and it could not recover, since new positives would be diluted
            # by 27.6M historical samples that were never counted.
            #
            # Resetting all three together puts weights and both counters on
            # one consistent window.
            # Keyed on the STATE, not the format version. The first attempt
            # tested `version < 4` and missed the case that actually mattered:
            # a snapshot re-saved by the previous build is legitimately
            # version 4, yet still carries counters measured over mismatched
            # windows. The presence of the base-rate window field is the
            # honest signal - a snapshot without it has positives counted over
            # an unknown span, so the ratio cannot be trusted whatever the
            # version says.
            #
            # The tp_hit weights go with it. A head from before that field
            # existed trained at least partly under the divergent schedule,
            # and the |coef| screen cannot tell "healthy" from "drifted only a
            # little" - the live ETH head sat under the threshold twice while
            # reporting probabilities an order of magnitude below its peers.
            # For an input to a safety veto, rebuilding is the honest default.
            if "tp_hit_labeled_samples" not in state:
                for head_name in brain._CLASSIFIER_HEADS:
                    brain.reset_head(head_name)
                print(color(
                    f"[brain] snapshot (version {state.get('version')}) predates "
                    f"base-rate window tracking: rebuilt the noise/success/tp_hit "
                    f"heads. Their positive counts were measured over an unknown "
                    f"span of labels, so weights and both counters restart "
                    f"together on one consistent window.", YELLOW,
                ))
            # 2026-08-29: the noise label changed. It used to ask whether
            # |forward_return| over LABEL_HORIZON_TICKS (about 2.5 seconds)
            # stayed inside half a 14-period ATR measured on ONE-MINUTE
            # candles - roughly 6 bps against a median 2.5s move of 0.8 bps,
            # so it was True almost always. The head learned to answer "yes"
            # and was right; solved back from live logs its output had a
            # median of 0.988. Because noise_p enters confidence as
            # (1 - 0.5*noise_p), that halved brain_confidence on every tick
            # and held the composite entry score below its threshold, so the
            # bot could not open a position at all.
            #
            # The old and new labels disagree on most samples, so continued
            # training cannot correct these weights at any realistic rate -
            # the same reasoning that governs the saturation rebuild below.
            # The head relearns from live ticks within minutes and reports a
            # neutral 0.5 until it is reliable again, which is honest; the
            # restored weights were not.
            if int(state.get("noise_label_version", 1)) < cls._NOISE_LABEL_VERSION:
                brain.reset_head("noise")
                print(color(
                    f"[brain] noise head rebuilt: this snapshot was trained under "
                    f"the old ATR-band noise label (version "
                    f"{state.get('noise_label_version', 1)}), which was true for "
                    f"almost every sample and pinned the head near 1.0 - halving "
                    f"brain_confidence on every tick. It restarts under the "
                    f"horizon-matched label and reports 0.5 until reliable.",
                    YELLOW,
                ))
            # 2026-08-21: a snapshot written under the old "optimal" learning
            # rate carries diverged classifier weights. Correcting the
            # schedule does not repair them, so any head still saturated is
            # rebuilt here - loudly, because this discards learned state.
            for head_name, peak in brain.reset_saturated_heads():
                print(color(
                    f"[brain] {head_name} head reset: |coef| peaked at {peak:.1f} "
                    f"(>= {SATURATED_COEF_ABS}), which means it diverged under the "
                    f"old learning rate rather than learned anything. Its "
                    f"probabilities carried no information, so it starts fresh "
                    f"under the corrected schedule and reports a neutral 0.5 "
                    f"until it is reliable again.", YELLOW,
                ))
            if state.get("version") == 2:
                print(color(
                    "[brain] migrated version-2 snapshot to version-3 (added per-head "
                    "sample-reliability counters, all starting at 0/no-classes-seen for "
                    "success/tp_hit/noise - existing learned model weights unchanged; "
                    "those heads will report neutral 0.5 until enough new samples "
                    "accumulate under the new reliability gate).", YELLOW,
                ))
        except Exception as e:  # noqa: BLE001 - corrupted/incompatible snapshot must not crash startup
            print(color(f"[brain] failed to deserialize snapshot ({e}), starting fresh.", YELLOW))
            return cls(n_features, warmup_updates)
        return brain
