import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import data
import metrics


def frame(close, volume=None):
    idx = pd.bdate_range(end="2026-10-06", periods=len(close))
    close = np.asarray(close, float)
    vol = np.full(len(close), 1e6) if volume is None else np.asarray(volume, float)
    return pd.DataFrame(
        {"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": vol},
        index=idx,
    )


def test_uptrend_label_and_returns():
    df = frame(100 * 1.001 ** np.arange(300))
    m = metrics.compute(df)
    assert m["trend_label"] == "Rialzista"
    assert m["ret_3m"] == pytest.approx(1.001**63 - 1)
    assert m["drawdown_52w"] == pytest.approx(0)
    assert m["rsi14"] == 100


def test_downtrend_label():
    assert metrics.compute(frame(100 * 0.999 ** np.arange(300)))["trend_label"] == "Ribassista"


def test_short_history_gives_none_not_crash():
    m = metrics.compute(frame(100 + np.arange(60.0)))
    assert m["px_vs_sma200"] is None and m["ret_12m"] is None and m["trend_label"] == "n/d"


def test_volume_ratio_and_updown():
    vol = np.r_[np.full(280, 1e6), np.full(20, 2e6)]
    close = np.r_[np.full(280, 100.0), 100 + np.arange(1.0, 21)]
    m = metrics.compute(frame(close, vol))
    assert m["vol_ratio_20_90"] == pytest.approx(2e6 / ((70 * 1e6 + 20 * 2e6) / 90))
    assert m["updown_volume"] is None  # nessun giorno in calo -> indefinito


def test_relative_strength_vs_benchmark():
    s = frame(100 * 1.002 ** np.arange(300))
    b = frame(100 * 1.001 ** np.arange(300))["close"]
    m = metrics.compute(s, b)
    assert m["rs_3m"] == pytest.approx((1.002**63 - 1) - (1.001**63 - 1))


def test_scores_rank_and_low_vol_is_better():
    rows = {}
    for sym, drift, sig in [("A", 0.002, 0.005), ("B", 0.0, 0.01), ("C", -0.001, 0.02)]:
        rng = np.random.default_rng(1)
        rows[sym] = metrics.compute(frame(100 * np.exp(np.cumsum(rng.normal(drift, sig, 300)))))
    sc = metrics.score(rows)
    assert sc["A"]["momentum"] > sc["B"]["momentum"] > sc["C"]["momentum"]
    assert sc["A"]["volatility"] > sc["C"]["volatility"]
    assert all(0 <= v <= 100 for s in sc.values() for v in s.values() if v is not None)


def test_history_has_no_nan():
    h = metrics.history(frame(100 + np.arange(500.0)))
    assert len(h["dates"]) == 252 and None not in h["close"] and None not in h["sma200"]


def test_demo_bars_cover_all_sectors_and_json_safe():
    import json

    bars = data.demo_bars([*data.SECTORS, data.BENCHMARK])
    rows = {s: metrics.compute(bars[s], bars["SPY"]["close"]) for s in data.SECTORS}
    json.dumps({"r": rows, "s": metrics.score(rows)}, allow_nan=False)
