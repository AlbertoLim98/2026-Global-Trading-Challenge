"""Classifica delle migliori aziende di ogni settore, valutate rispetto ad ACWI.

Tutte le caratteristiche (trend, momentum, volume, volatilità) sono calcolate sul
prezzo relativo  rp = prezzo titolo / prezzo ACWI. Il momentum usa il rendimento
relativo ponderato nel tempo (time-weighted relative return, TWRR).
Modulo puro (pandas/numpy): nessuna rete.
"""

from __future__ import annotations

import math

import pandas as pd
from metrics import MIN_BARS, TRADING_DAYS, rsi, trend_label

# finestre (giorni di borsa) -> peso: le più recenti contano di più
TWRR_WINDOWS: dict[int, int] = {21: 4, 63: 3, 126: 2, 252: 1}
TOP_N = 10

STOCK_PILLARS: dict[str, dict[str, int]] = {
    "trend": {"rp_vs_sma200": 1, "rp_sma50_vs_sma200": 1, "rp_vs_sma50": 1},
    "momentum": {"twrr_w": 1, "twrr_3m": 1, "twrr_6m": 1, "twrr_12m": 1},
    "volume": {"vol_ratio_rel": 1, "updown_rel": 1},
    "volatility": {"te_60d": -1, "rel_drawdown": 1, "vol_60d": -1},
}


def _f(v: float) -> float | None:
    return None if v is None or math.isnan(v) or math.isinf(v) else float(v)


def twrr(rp: pd.Series, n: int) -> float | None:
    """Rendimento relativo sulla finestra n: (1+r_titolo)/(1+r_benchmark) - 1."""
    return None if len(rp) <= n else _f(rp.iloc[-1] / rp.iloc[-1 - n] - 1)


def compute_relative(df: pd.DataFrame, bench: pd.DataFrame) -> dict | None:
    """Metriche relative a ACWI per un titolo; None se lo storico è troppo breve."""
    j = pd.concat(
        [df[["close", "volume"]], bench[["close", "volume"]].add_prefix("b_")],
        axis=1,
        join="inner",
    ).dropna()
    if len(j) < MIN_BARS:
        return None
    c, v, bc, bv = j["close"], j["volume"], j["b_close"], j["b_volume"]
    rp = c / bc
    rrets = rp.pct_change()

    out: dict[str, float | str | None] = {
        "price": float(c.iloc[-1]),
        "last_date": str(j.index[-1].date()),
    }
    for n in TWRR_WINDOWS:
        out[f"twrr_{n // 21}m"] = twrr(rp, n)
    parts = [(w, out[f"twrr_{n // 21}m"]) for n, w in TWRR_WINDOWS.items()]
    out["twrr_w"] = _f(sum(w * x for w, x in parts) / sum(w for w, _ in parts))

    s50, s200 = rp.rolling(50).mean(), rp.rolling(200).mean()
    out["rp_vs_sma50"] = _f(rp.iloc[-1] / s50.iloc[-1] - 1)
    out["rp_vs_sma200"] = _f(rp.iloc[-1] / s200.iloc[-1] - 1)
    out["rp_sma50_vs_sma200"] = _f(s50.iloc[-1] / s200.iloc[-1] - 1)
    out["trend_label"] = trend_label(float(rp.iloc[-1]), _f(s50.iloc[-1]), _f(s200.iloc[-1]))
    out["rsi_rel"] = _f(rsi(rp).iloc[-1])

    def vr(x: pd.Series) -> float | None:
        return _f(x.tail(20).mean() / x.tail(90).mean())

    own, ref = vr(v), vr(bv)
    out["vol_ratio_rel"] = None if own is None or not ref else own / ref
    up, down = v.where(rrets > 0).tail(20).sum(), v.where(rrets < 0).tail(20).sum()
    out["updown_rel"] = _f(up / down) if down else None

    out["te_60d"] = _f(rrets.tail(60).std() * math.sqrt(TRADING_DAYS))
    out["rel_drawdown"] = _f(rp.iloc[-1] / rp.tail(TRADING_DAYS).max() - 1)
    out["vol_60d"] = _f(c.pct_change().tail(60).std() * math.sqrt(TRADING_DAYS))
    return out


def rank_sector(
    universe: pd.DataFrame, bars: dict[str, pd.DataFrame], bench: pd.DataFrame, top_n: int = TOP_N
) -> dict:
    """Migliori `top_n` titoli del settore (universe: colonne ticker, name, weight_pct)."""
    from metrics import score

    rows: dict[str, dict] = {}
    for t in universe.itertuples():
        if t.ticker in bars and (m := compute_relative(bars[t.ticker], bench)) is not None:
            rows[t.ticker] = {
                "symbol": t.ticker,
                "name": t.name,
                "weight_pct": float(t.weight_pct),
                **m,
            }
    scores = (
        score(rows, STOCK_PILLARS)
        if len(rows) > 1
        else {k: dict.fromkeys([*STOCK_PILLARS, "total"]) for k in rows}
    )
    ranked = sorted(
        rows, key=lambda k: -(scores[k]["total"] if scores[k]["total"] is not None else -1)
    )
    top = [{**rows[k], "scores": scores[k], "rank": i} for i, k in enumerate(ranked[:top_n], 1)]
    return {"top": top, "n_analyzed": len(rows), "n_skipped": len(universe) - len(rows)}
