import indicator_analysis as ia
import numpy as np
import pandas as pd


def _synthetic(n_runs=6, n=40, seed=1):
    rng = np.random.default_rng(seed)
    recs, bars = [], {}
    for r in range(n_runs):
        ts = pd.Timestamp("2026-10-01T14:00:00Z") + pd.Timedelta(days=r)
        for i in range(n):
            sym = f"S{r}_{i}"
            good, noise = rng.normal(), rng.normal()
            recs.append(
                {
                    "run_id": f"r{r}", "ts": ts, "symbol": sym, "strategy": "short",
                    "selected": False, "ref_price": 100.0,
                    "pillars.rsi": good, "pillars.volume": noise, "total": good + noise,
                }
            )  # fmt: skip
            day = ts.tz_convert("America/New_York").tz_localize(None).normalize()
            idx = pd.bdate_range(day, periods=4)
            ret = 0.02 * good + 0.002 * rng.normal()
            bars[sym] = pd.DataFrame(
                {"close": [100.0, 100 * (1 + ret), 100 * (1 + ret), 100 * (1 + ret)]}, index=idx
            )
    return pd.DataFrame(recs), bars


def test_pillar_with_signal_ranks_first():
    rows, bars = _synthetic()
    df = ia.forward_returns(rows, bars, (0, 1))
    res = ia.analyze(df, (0, 1))
    per = res["short"]["per"]["ret_1"]
    assert per["ic"].iloc[0]["indicatore"] in ("pillars.rsi", "total")
    ic = per["ic"].set_index("indicatore")["IC medio"]
    assert ic["pillars.rsi"] > 0.8 and abs(ic["pillars.volume"]) < 0.3
    reg = per["reg"]
    assert reg["pillars.rsi"] > 5 * abs(reg["pillars.volume"])
    assert "pillars.rsi" in ia.report(res)


def test_flatten_skips_ids_and_nan():
    out = ia.flatten(
        {"rank": 3, "price": 10.0, "beta_1y": 1.2, "scores": {"total": 70.0, "trend": None}}
    )
    assert out == {"beta_1y": 1.2, "scores.total": 70.0}


def test_few_stocks_gives_no_ic():
    rows, bars = _synthetic(n=4)
    df = ia.forward_returns(rows, bars, (1,))
    assert ia.analyze(df, (1,))["short"]["per"] == {}
