"""Indicatori di trend, momentum, volume e volatilità per ETF settoriali.

Modulo puro (solo pandas/numpy): nessuna rete, nessuna chiave. Riceve barre
giornaliere con colonne open/high/low/close/volume e indice di date.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252
MIN_BARS = 253  # 12 mesi di rendimenti + il giorno di partenza


def _last(s: pd.Series) -> float | None:
    if s.empty:
        return None
    v = float(s.iloc[-1])
    return None if math.isnan(v) or math.isinf(v) else v


def _ret(close: pd.Series, n: int) -> float | None:
    if len(close) <= n:
        return None
    return float(close.iloc[-1] / close.iloc[-1 - n] - 1)


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """RSI di Wilder."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def trend_label(close: float, sma50: float | None, sma200: float | None) -> str:
    if sma50 is None or sma200 is None:
        return "n/d"
    if close > sma50 > sma200:
        return "Rialzista"
    if close < sma50 < sma200:
        return "Ribassista"
    return "Misto"


def compute(
    df: pd.DataFrame, bench_close: pd.Series | None = None
) -> dict[str, float | str | None]:
    """Metriche grezze per un singolo ETF."""
    close = df["close"].astype(float)
    vol = df["volume"].astype(float)
    ret = close.pct_change()
    px = float(close.iloc[-1])

    sma50 = _last(close.rolling(50).mean())
    sma200 = _last(close.rolling(200).mean())
    sma50_prev = _last(close.rolling(50).mean().iloc[:-20]) if len(close) > 70 else None

    up = vol.where(ret > 0).tail(20).sum()
    down = vol.where(ret < 0).tail(20).sum()
    v20, v90 = vol.tail(20).mean(), vol.tail(90).mean()

    high252 = close.tail(TRADING_DAYS).max()
    atr14 = _last(atr(df))

    r3 = _ret(close, 63)
    rs3 = rs6 = None
    if bench_close is not None:
        b = bench_close.reindex(close.index).ffill()
        b3, b6 = _ret(b.dropna(), 63), _ret(b.dropna(), 126)
        r6 = _ret(close, 126)
        rs3 = None if r3 is None or b3 is None else r3 - b3
        rs6 = None if r6 is None or b6 is None else r6 - b6

    vol20 = ret.tail(20).std() * math.sqrt(TRADING_DAYS)
    vol60 = ret.tail(60).std() * math.sqrt(TRADING_DAYS)

    return {
        "price": px,
        "last_date": str(close.index[-1].date()),
        # trend
        "px_vs_sma50": None if sma50 is None else px / sma50 - 1,
        "px_vs_sma200": None if sma200 is None else px / sma200 - 1,
        "sma50_vs_sma200": None if sma50 is None or sma200 is None else sma50 / sma200 - 1,
        "sma50_slope_20d": None if not sma50 or not sma50_prev else sma50 / sma50_prev - 1,
        "trend_label": trend_label(px, sma50, sma200),
        # momentum
        "ret_1m": _ret(close, 21),
        "ret_3m": r3,
        "ret_6m": _ret(close, 126),
        "ret_12m": _ret(close, 252),
        "rsi14": _last(rsi(close)),
        "rs_3m": rs3,
        "rs_6m": rs6,
        # volume
        "vol_ratio_20_90": None if not v90 else float(v20 / v90),
        "updown_volume": None if not down else float(up / down),
        "dollar_volume_20d": float((close * vol).tail(20).mean()),
        # volatilità
        "vol_20d": None if math.isnan(vol20) else float(vol20),
        "vol_60d": None if math.isnan(vol60) else float(vol60),
        "atr_pct": None if atr14 is None else atr14 / px,
        "drawdown_52w": float(px / high252 - 1),
    }


# componente -> (+1 più alto = meglio, -1 più basso = meglio)
PILLARS: dict[str, dict[str, int]] = {
    "trend": {"px_vs_sma200": 1, "sma50_vs_sma200": 1, "px_vs_sma50": 1},
    "momentum": {"ret_3m": 1, "ret_6m": 1, "ret_12m": 1, "rs_3m": 1},
    "volume": {"vol_ratio_20_90": 1, "updown_volume": 1},
    "volatility": {"vol_60d": -1, "atr_pct": -1, "drawdown_52w": 1},
}


def score(rows: dict[str, dict]) -> dict[str, dict[str, float | None]]:
    """Punteggi 0-100 per pilastro: rango percentile del settore sugli altri settori.

    Per la volatilità un valore basso dà un punteggio alto. Il punteggio totale è la
    media dei quattro pilastri. È una classifica relativa, non un segnale operativo.
    """
    df = pd.DataFrame(rows).T
    out: dict[str, dict[str, float | None]] = {k: {} for k in rows}
    pillar_scores = {}
    for pillar, comps in PILLARS.items():
        ranks = []
        for col, sign in comps.items():
            s = pd.to_numeric(df[col], errors="coerce") * sign
            ranks.append(s.rank(pct=True) * 100)
        pillar_scores[pillar] = pd.concat(ranks, axis=1).mean(axis=1, skipna=True)
    tot = pd.concat(pillar_scores.values(), axis=1).mean(axis=1, skipna=False)
    for sym in rows:
        for pillar, s in pillar_scores.items():
            v = s.get(sym)
            out[sym][pillar] = None if v is None or pd.isna(v) else round(float(v), 1)
        v = tot.get(sym)
        out[sym]["total"] = None if v is None or pd.isna(v) else round(float(v), 1)
    return out


def history(df: pd.DataFrame, n: int = 252) -> dict[str, list]:
    """Serie per il grafico di dettaglio: chiusura, SMA50, SMA200, volume."""
    close = df["close"].astype(float)
    d = pd.DataFrame(
        {
            "close": close,
            "sma50": close.rolling(50).mean(),
            "sma200": close.rolling(200).mean(),
            "volume": df["volume"].astype(float),
        }
    ).tail(n)
    clean = lambda s: [None if pd.isna(x) else round(float(x), 4) for x in s]
    return {
        "dates": [str(i.date()) for i in d.index],
        "close": clean(d["close"]),
        "sma50": clean(d["sma50"]),
        "sma200": clean(d["sma200"]),
        "volume": clean(d["volume"]),
    }
