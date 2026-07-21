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
================================================================================
"""

from __future__ import annotations

import pickle
import sys
from typing import Optional

import numpy as np
from sklearn.linear_model import SGDRegressor, SGDClassifier

from config import N_FEATURES_V2, BRAIN2_WARMUP_UPDATES

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
# BRAIN V2 - probability / confidence engine (replaces the old direction-only
# predictor). Runs several small online models in parallel over the SAME
# normalized feature vector and turns their outputs into a set of
# probabilities/scores that the rest of the stack consumes.
# ============================================================================


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
        self.noise_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )
        self.success_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )
        self.tp_hit_model = SGDClassifier(
            loss="log_loss", penalty="l2", alpha=1e-5,
            learning_rate="optimal", warm_start=True,
        )

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
        self._trend_scale = 0.0015

    # -- prediction -----------------------------------------------------------

    def predict_all(self, x: np.ndarray) -> dict:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            x = np.nan_to_num(x)
        xn = self.norm.normalize(x).reshape(1, -1)

        trend_pred = float(self.trend_model.predict(xn)[0]) if self.trend_fitted else 0.0
        quality_pred = float(self.quality_model.predict(xn)[0]) if self.quality_fitted else 0.0
        noise_prob = float(self.noise_model.predict_proba(xn)[0][1]) if self.noise_fitted else 0.5
        success_prob = float(self.success_model.predict_proba(xn)[0][1]) if self.success_fitted else 0.5
        tp_hit_prob = float(self.tp_hit_model.predict_proba(xn)[0][1]) if self.tp_hit_fitted else 0.5

        self.last_trend_pred = trend_pred
        self.last_noise_prob = noise_prob
        self.last_success_prob = success_prob
        self.last_tp_hit_prob = tp_hit_prob
        self.last_quality_pred = quality_pred

        trend_confidence = clamp(abs(trend_pred) / max(self._trend_scale, 1e-6), 0.0, 1.0)
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

    def learn_success(self, x: np.ndarray, was_success: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.success_fitted else None
        self.success_model.partial_fit(xn, [1 if was_success else 0], classes=classes)
        self.success_fitted = True

    def learn_tp_hit(self, x: np.ndarray, tp_was_hit: bool) -> None:
        x = np.asarray(x, dtype=float)
        if not np.all(np.isfinite(x)):
            return
        xn = self.norm.normalize(x).reshape(1, -1)
        classes = np.array([0, 1]) if not self.tp_hit_fitted else None
        self.tp_hit_model.partial_fit(xn, [1 if tp_was_hit else 0], classes=classes)
        self.tp_hit_fitted = True

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

    def to_state(self) -> dict:
        return {
            "version": 2,
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
            "norm": self.norm.state(),
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
        self.norm.load(state["norm"])

    def to_bytes(self) -> bytes:
        return pickle.dumps(self.to_state(), protocol=pickle.HIGHEST_PROTOCOL)

    @classmethod
    def from_bytes(cls, data: bytes, n_features: int, warmup_updates: int) -> "BrainV2":
        """Falls back to a fresh (cold) brain on any corruption, version
        mismatch, or feature-shape mismatch - a bad/stale snapshot must
        never prevent the bot from starting."""
        brain = cls(n_features, warmup_updates)
        try:
            state = pickle.loads(data)
            if state.get("version") != 2 or state.get("n_features") != n_features:
                print(color(
                    f"[brain] snapshot incompatible (version={state.get('version')}, "
                    f"n_features={state.get('n_features')}, expected {n_features}) - "
                    f"starting a fresh Brain V2.", YELLOW,
                ))
                return brain
            brain.load_state(state)
        except Exception as e:  # noqa: BLE001 - corrupted/incompatible snapshot must not crash startup
            print(color(f"[brain] failed to deserialize snapshot ({e}), starting fresh.", YELLOW))
            return cls(n_features, warmup_updates)
        return brain
