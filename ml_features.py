"""Feature engineering for the ML model.

Every feature is computed from bars up to and including bar i, and the trade
it informs opens at bar i+1's open - the same convention breakout.py uses.
That single rule is what keeps a backtest honest, so it is worth stating
plainly: nothing here may look at bar i+1 or later.

The features are deliberately ordinary - RSI, ATR, EMA distance, volume
z-scores, channel position, realized-volatility ratios. Exotic features are
not what separates a working model from a broken one; leakage and validation
are, and those live in train_ml_model.py.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rma(x: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing, the average RSI and ATR are defined against."""
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = _rma(d.clip(lower=0.0), n)
    dn = _rma((-d).clip(lower=0.0), n)
    rs = up / dn.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def build(df: pd.DataFrame) -> pd.DataFrame:
    """OHLCV frame in, feature frame out. Index is preserved."""
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    f = pd.DataFrame(index=df.index)

    # --- momentum over several scales -------------------------------------
    for n in (1, 2, 3, 6, 12, 24, 48):
        f[f"ret_{n}"] = c.pct_change(n)

    # --- trend location ---------------------------------------------------
    ema_f, ema_s = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    f["ema_spread"] = (ema_f - ema_s) / c
    f["ema_fast_dist"] = (c - ema_f) / c
    f["ema_slow_dist"] = (c - ema_s) / c
    sma50 = c.rolling(50).mean()
    f["sma50_dist"] = (c - sma50) / c
    f["ema_spread_chg"] = f["ema_spread"].diff(3)

    # --- oscillators ------------------------------------------------------
    f["rsi_14"] = rsi(c, 14) / 100.0
    f["rsi_7"] = rsi(c, 7) / 100.0
    f["rsi_chg"] = f["rsi_14"].diff(3)

    # --- volatility -------------------------------------------------------
    a = atr(df, 14)
    f["atr_pct"] = a / c
    f["atr_ratio"] = a / a.rolling(100).mean()
    r1 = c.pct_change()
    f["rv_12"] = r1.rolling(12).std()
    f["rv_48"] = r1.rolling(48).std()
    f["rv_ratio"] = f["rv_12"] / f["rv_48"]
    f["ret_skew_24"] = r1.rolling(24).skew()
    f["ret_kurt_24"] = r1.rolling(24).kurt()

    # --- channel position -------------------------------------------------
    for n in (20, 50):
        hi = h.rolling(n).max()
        lo = l.rolling(n).min()
        f[f"donch_pos_{n}"] = (c - lo) / (hi - lo).replace(0.0, np.nan)
    sma20 = c.rolling(20).mean()
    sd20 = c.rolling(20).std()
    f["bb_pos"] = (c - sma20) / (2.0 * sd20).replace(0.0, np.nan)
    f["bb_width"] = (4.0 * sd20) / c

    # --- volume -----------------------------------------------------------
    vmean = v.rolling(20).mean()
    vstd = v.rolling(20).std()
    f["vol_z"] = (v - vmean) / vstd.replace(0.0, np.nan)
    f["vol_ratio"] = v / vmean.replace(0.0, np.nan)
    f["vol_trend"] = vmean / v.rolling(100).mean().replace(0.0, np.nan)

    # --- candle shape -----------------------------------------------------
    rng = (h - l).replace(0.0, np.nan)
    f["body_frac"] = (c - df["open"]) / rng
    f["upper_wick"] = (h - np.maximum(c, df["open"])) / rng
    f["lower_wick"] = (np.minimum(c, df["open"]) - l) / rng

    # --- session ----------------------------------------------------------
    idx = pd.to_datetime(df.index, unit="s", utc=True)
    f["hour_sin"] = np.sin(2 * np.pi * idx.hour / 24)
    f["hour_cos"] = np.cos(2 * np.pi * idx.hour / 24)
    f["dow"] = idx.dayofweek.astype(float)

    return f.replace([np.inf, -np.inf], np.nan)


FEATURE_NAMES = None      # filled on first build() call by the trainer
