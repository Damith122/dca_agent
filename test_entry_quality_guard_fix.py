"""Focused offline tests for the clean-Live entry-quality correction.

No network call or real order is sent. The tests pin down the exact failure
seen in the clean Live sample: a strong upward five-candle move must not help
or authorize a SIDEWAYS SHORT, while an aligned SHORT remains eligible under
the existing score/threshold system.
"""
import os

os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")
os.environ.setdefault("USE_TESTNET", "true")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("SIDEWAYS_ENTRY_MOMENTUM_ALIGNMENT_ENABLED", "true")
os.environ.setdefault("SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO", "0.50")
os.environ.setdefault("TRADE_LOG_JSON_PATH", "/tmp/test_entry_quality_trades.jsonl")
os.environ.setdefault("TRADE_LOG_CSV_PATH", "/tmp/test_entry_quality_trades.csv")
os.environ.setdefault("STATS_JSON_PATH", "/tmp/test_entry_quality_stats.json")
os.environ.setdefault("STATS_CSV_PATH", "/tmp/test_entry_quality_stats.csv")
os.environ.setdefault("BRAIN_LOCAL_PATH", "/tmp/test_entry_quality_brain.pkl")
os.environ.setdefault("DCA_STATE_PATH", "/tmp/test_entry_quality_dca_state.json")

import asyncio
import io
import sys
import time

import dca2 as bot
import trading


class Capture:
    def __enter__(self):
        self._buf = io.StringIO()
        self._stdout = sys.stdout
        sys.stdout = self._buf
        return self

    def __exit__(self, *exc):
        sys.stdout = self._stdout

    @property
    def text(self):
        return self._buf.getvalue()


def clean_live_like_conf(side="SHORT"):
    # Mirrors the clean Live entry snapshot closely enough to recreate the
    # old failure: moderate composite confidence, saturated trend direction,
    # low risk, and enough volume/momentum for the old abs(momentum) score to
    # cross the 0.60 SIDEWAYS threshold.
    return trading.ConfidenceReading(
        confidence_score=0.34,
        trend_confidence=1.0,
        trend_direction=side,
        success_probability=1.0,
        tp_hit_probability=0.0,
        noise_probability=0.5,
        risk_score=0.125,
    )


def sideways_regime():
    return trading.RegimeReading(
        regime=trading.REGIME_SIDEWAYS,
        atr_pct=0.00030,
        atr_ratio=1.0,
    )


async def test_counter_momentum_short_is_blocked():
    engine = trading.EntryEngineV2()
    conf = clean_live_like_conf("SHORT")
    upward_move = trading.ENTRY_MOMENTUM_SATURATION_PCT

    decision = engine.evaluate(
        conf, sideways_regime(), volume_z=2.0,
        momentum=upward_move, features=[0.0] * 34,
    )

    # Before the fix, abs(momentum) contributed the full 0.13 weight. Prove
    # this setup genuinely would have crossed the old threshold rather than
    # merely being a low-score entry that was already blocked elsewhere.
    legacy_score = decision.score + trading.ENTRY_WEIGHTS["momentum"]
    assert legacy_score >= trading.SIDEWAYS_ENTRY_SCORE_THRESHOLD
    assert decision.should_enter is False
    assert decision.components["momentum_aligned"] is False
    assert decision.components["sideways_counter_momentum_blocked"] is True
    assert decision.components["momentum"] == 0.0
    print("PASS: strong upward momentum cannot authorize a SIDEWAYS SHORT")


async def test_aligned_short_remains_eligible_and_logs_exact_tick():
    engine = trading.EntryEngineV2()
    # Suppress the periodic debug line to prove [entry-accepted] is emitted
    # independently and cannot disappear inside the 15-second throttle.
    engine._last_log_ts = time.time()
    conf = clean_live_like_conf("SHORT")
    downward_move = -trading.ENTRY_MOMENTUM_SATURATION_PCT

    with Capture() as cap:
        decision = engine.evaluate(
            conf, sideways_regime(), volume_z=2.0,
            momentum=downward_move, features=[0.0] * 34,
        )

    assert decision.should_enter is True
    assert decision.score >= trading.SIDEWAYS_ENTRY_SCORE_THRESHOLD
    assert decision.components["momentum_aligned"] is True
    assert decision.components["sideways_counter_momentum_blocked"] is False
    assert decision.components["momentum"] == 1.0
    assert cap.text.count("[entry-accepted]") == 1
    assert "side=SHORT" in cap.text
    assert "threshold=0.6000" in cap.text
    assert "raw_momentum=-" in cap.text
    assert "momentum_aligned=True" in cap.text
    print("PASS: aligned SIDEWAYS SHORT remains eligible with an exact accepted-entry audit line")


async def test_tiny_counter_sign_jitter_is_not_hard_blocked():
    engine = trading.EntryEngineV2()
    conf = clean_live_like_conf("SHORT")
    tiny_up_move = (
        trading.ENTRY_MOMENTUM_SATURATION_PCT
        * trading.SIDEWAYS_ENTRY_COUNTER_MOMENTUM_BLOCK_RATIO
        * 0.25
    )
    decision = engine.evaluate(
        conf, sideways_regime(), volume_z=0.0,
        momentum=tiny_up_move, features=[0.0] * 34,
    )
    assert decision.components["momentum_aligned"] is False
    assert decision.components["sideways_counter_momentum_blocked"] is False
    assert decision.should_enter is False  # normal score threshold still decides
    print("PASS: tiny sign jitter is score-controlled, not treated as a hard directional move")


async def test_trending_pullback_is_not_sideways_hard_blocked():
    engine = trading.EntryEngineV2()
    conf = clean_live_like_conf("SHORT")
    trend = trading.RegimeReading(
        regime=trading.REGIME_WEAK_TREND,
        atr_pct=0.00060,
        atr_ratio=1.0,
        trend_slope=-0.00030,
    )
    decision = engine.evaluate(
        conf, trend, volume_z=2.0,
        momentum=trading.ENTRY_MOMENTUM_SATURATION_PCT,
        features=[0.0] * 34,
    )
    assert decision.components["sideways_counter_momentum_blocked"] is False
    assert decision.components["momentum"] == 0.0
    print("PASS: the new hard guard is isolated to SIDEWAYS; trend pullbacks keep normal score handling")


async def test_initial_fill_log_uses_frozen_entry_confidence():
    filters = bot.SymbolFilters(
        tick_size=0.01, step_size=0.01, min_qty=0.01, min_notional=5.0,
    )
    manager = bot.MartingaleManager(
        client=None, symbol="SOLUSDT", filters=filters, leverage=20,
    )
    manager.position.side = "SHORT"
    manager.position.status = "ENTERING"
    manager.position.entry_confidence = 0.82
    manager.last_confidence = trading.ConfidenceReading(confidence_score=0.11)
    manager.last_regime = sideways_regime()

    with Capture() as cap:
        await manager._on_entry_filled(
            "initial", fill_price=75.0, fill_qty=1.0, order_id=12345,
        )

    assert "ENTRY FILLED [INITIAL]" in cap.text
    assert "entry_confidence=0.82" in cap.text
    assert "entry_confidence=0.11" not in cap.text
    print("PASS: fill log reports the frozen accepted-entry confidence, not a later tick")


async def main():
    await test_counter_momentum_short_is_blocked()
    await test_aligned_short_remains_eligible_and_logs_exact_tick()
    await test_tiny_counter_sign_jitter_is_not_hard_blocked()
    await test_trending_pullback_is_not_sideways_hard_blocked()
    await test_initial_fill_log_uses_frozen_entry_confidence()
    print("ALL ENTRY-QUALITY GUARD TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
