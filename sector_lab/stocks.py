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
MAX_WEIGHT = 0.25  # tetto al peso di un singolo titolo nella strategia alto beta

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


def beta(sr: pd.Series, br: pd.Series, n: int) -> float | None:
    """Beta = cov(rend. titolo, rend. ACWI) / var(rend. ACWI) sugli ultimi n giorni."""
    x, y = sr.tail(n), br.tail(n)
    var = y.var()
    return None if not var else _f(x.cov(y) / var)


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

    sr, br = c.pct_change(), bc.pct_change()
    out["beta_1y"] = beta(sr, br, TRADING_DAYS)
    out["beta_6m"] = beta(sr, br, 126)
    out["corr_1y"] = _f(sr.tail(TRADING_DAYS).corr(br.tail(TRADING_DAYS)))
    return out


def cap_weights(raw: list[float], cap: float = MAX_WEIGHT) -> list[float]:
    """Pesi proporzionali a `raw`, con tetto per titolo (l'eccesso va agli altri)."""
    n = len(raw)
    if n == 0:
        return []
    cap = max(cap, 1 / n)  # con pochi titoli il tetto non può essere inferiore a 1/n
    w, fixed = [0.0] * n, set()
    while True:
        free = [i for i in range(n) if i not in fixed]
        room = 1 - cap * len(fixed)
        tot = sum(raw[i] for i in free)
        for i in free:
            w[i] = room * raw[i] / tot if tot else room / len(free)
        over = [i for i in free if w[i] > cap + 1e-12]
        if not over:
            return w
        for i in over:
            w[i] = cap
            fixed.add(i)


def analyze_sector(universe: pd.DataFrame, bars: dict[str, pd.DataFrame], bench: pd.DataFrame):
    """Metriche e punteggi di tutti i titoli analizzabili del settore."""
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
    if len(rows) > 1:
        scores = score(rows, STOCK_PILLARS)
    else:
        scores = {k: dict.fromkeys([*STOCK_PILLARS, "total"]) for k in rows}
    for k, row in rows.items():
        row["scores"] = scores[k]
    return rows


def top_quality(rows: dict[str, dict], n_universe: int, top_n: int = TOP_N) -> dict:
    """Migliori `top_n` per punteggio totale (media dei quattro pilastri)."""
    ranked = sorted(
        rows.values(),
        key=lambda r: -(r["scores"]["total"] if r["scores"]["total"] is not None else -1),
    )
    top = [{**r, "rank": i} for i, r in enumerate(ranked[:top_n], 1)]
    return {
        "top": top,
        "n_analyzed": len(rows),
        "n_skipped": n_universe - len(rows),
        "mode": "quality",
    }


def is_eligible(r: dict) -> bool:
    """Filtro della strategia: relativo non ribassista e TWRR ponderato positivo."""
    return r["trend_label"] != "Ribassista" and (r["twrr_w"] or 0) > 0 and r["beta_1y"] is not None


def top_beta(rows: dict[str, dict], n_universe: int, top_n: int = TOP_N) -> dict:
    """Strategia alto beta: tra i titoli idonei, i `top_n` con beta più alto.

    Pesi proporzionali al beta (tetto MAX_WEIGHT per titolo); beta di portafoglio = somma pesata.
    """
    eligible = [r for r in rows.values() if is_eligible(r)]
    chosen = sorted(eligible, key=lambda r: -r["beta_1y"])[:top_n]
    # beta negativi o nulli non meritano peso: base minima piccola ma positiva
    w = cap_weights([max(r["beta_1y"], 0.01) for r in chosen])
    top = [
        {**r, "rank": i, "strategy_weight": wi}
        for i, (r, wi) in enumerate(zip(chosen, w, strict=True), 1)
    ]
    pbeta = sum(r["beta_1y"] * r["strategy_weight"] for r in top) if top else None
    return {
        "top": top,
        "n_analyzed": len(rows),
        "n_skipped": n_universe - len(rows),
        "n_eligible": len(eligible),
        "portfolio_beta": pbeta,
        "mode": "beta",
    }


def rank_sector(
    universe: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    bench: pd.DataFrame,
    mode: str = "quality",
) -> dict:
    """Top 10 del settore (universe: colonne ticker, name, weight_pct) nella modalità scelta."""
    rows = analyze_sector(universe, bars, bench)
    return (top_beta if mode == "beta" else top_quality)(rows, len(universe))
