"""Score di breve periodo (orizzonte 1 giorno) per le azioni dell'universo.

Modulo puro (pandas/numpy): tutto si calcola su tabelle "date x titoli", quindi la stessa funzione
serve per il punteggio di oggi (ultima riga) e per la validazione storica (tutte le righe).

Sette pilastri, ognuno è un percentile 0-100 tra i titoli dello stesso giorno (parità = 50):

  1. rsi          RSI a 2 e 3 giorni (media), con filtro di trend: conta solo se il prezzo è sopra la
                  media a 50 giorni (ipervenduto in trend rialzista = punteggio alto), altrimenti 50.
  2. move         rendimento di ieri e della settimana (5 giorni) diviso per l'ATR: segno di ritorno alla
                  media (chi è sceso di più ha il punteggio più alto).
  3. volume       volume di ieri / media dei 20 giorni precedenti, con la direzione: volume alto in un
                  rialzo premia, volume alto in un ribasso penalizza (continuazione).
  4. candle       posizione della chiusura nel range del giorno (alta = continuazione) e gap notturno
                  diviso per l'ATR (gap che tende a chiudersi: gap al rialzo penalizza).
  5. relstrength  forza relativa a 3 giorni: ritorno alla media della parte idiosincratica (titolo
                  meno proprio settore, in ATR) e momentum del settore rispetto a SPY.
  6. context      contesto di mercato: SPY sopra/sotto la media a 50 giorni. Se rialzista premia i beta
                  alti, se ribassista i beta bassi (beta a 60 giorni vs SPY).
  7. volatility   regime di volatilità del titolo: ATR a 5 giorni / ATR a 20 giorni; l'espansione
                  penalizza.

Il segno di ogni indicatore è un'IPOTESI di partenza (vedi COMPONENT_NOTES): `ic_report` misura sui dati
storici se il segno regge e va riletto prima di fidarsi. Nessuna regola d'uscita: si esce solo con il
ribilanciamento quotidiano (l'ATR resta solo come unità di misura dentro gli indicatori).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")

PILLARS = ("rsi", "move", "volume", "candle", "relstrength", "context", "volatility")
PILLAR_LABEL = {
    "rsi": "RSI 2-3g + trend",
    "move": "Rendimento / ATR",
    "volume": "Volume relativo + direzione",
    "candle": "Chiusura nel range + gap",
    "relstrength": "Forza relativa settore / SPY",
    "context": "Contesto di mercato",
    "volatility": "Regime di volatilità",
}
WEIGHTS = dict.fromkeys(PILLARS, 1.0)  # pesi dei pilastri nello score totale
COMPONENT_PILLAR = {
    "rsi_rev": "rsi",
    "move_rev": "move",
    "volume_dir": "volume",
    "close_loc": "candle",
    "gap_fade": "candle",
    "rs_sector_rev": "relstrength",
    "sector_mom": "relstrength",
    "ctx_beta": "context",
    "vol_regime": "volatility",
}
COMPONENT_NOTES = {
    "rsi_rev": "100 - RSI(2,3), solo se prezzo > SMA50 (ritorno alla media in trend rialzista)",
    "move_rev": "-(rend. 1g + rend. 5g) / ATR (chi è sceso di più rimbalza)",
    "volume_dir": "(volume/media20 - 1) x segno del rendimento di ieri (continuazione)",
    "close_loc": "((C-L)-(H-C))/(H-L): chiusura alta nel range = continuazione",
    "gap_fade": "-gap notturno / ATR (il gap tende a chiudersi)",
    "rs_sector_rev": "-(rend. 3g titolo - rend. 3g settore) / ATR% (la parte idiosincratica rientra)",
    "sector_mom": "rend. 3g settore - rend. 3g SPY (momentum di settore)",
    "ctx_beta": "beta 60g vs SPY x (+1 se SPY > SMA50, -1 altrimenti)",
    "vol_regime": "-(ATR 5g / ATR 20g): l'espansione di volatilità penalizza",
}

RSI_PERIODS = (2, 3)
ATR_N, SMA_N, FEW, WEEK, VOL_N, BETA_N = 14, 50, 3, 5, 20, 60
TOP_N = 20


def drop_incomplete(df: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Toglie la barra di oggi se la seduta non è chiusa (le barre incomplete falsano gap e range)."""
    now = (now or datetime.now(NY)).astimezone(NY)
    if len(df) and df.index[-1].date() == now.date() and (now.hour, now.minute) < (16, 10):
        return df.iloc[:-1]
    return df


def _rsi(c: pd.DataFrame, n: int) -> pd.DataFrame:
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    lo = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    r = 100 - 100 / (1 + g / lo.replace(0, np.nan))
    return r.where(lo != 0, 100.0)


def _wide(bars: dict[str, pd.DataFrame], col: str) -> pd.DataFrame:
    return pd.DataFrame({s: df[col].astype(float) for s, df in bars.items()}).sort_index()


def _pct(df: pd.DataFrame, valid: pd.DataFrame) -> pd.DataFrame:
    """Percentile 0-100 tra i titoli validi dello stesso giorno; non calcolabile = 50 (neutro)."""
    r = df.where(valid).rank(axis=1, pct=True) * 100
    return r.fillna(50.0).where(valid)


@dataclass
class Panel:
    close: pd.DataFrame
    atr: pd.DataFrame
    sma50: pd.DataFrame
    beta: pd.DataFrame
    valid: pd.DataFrame
    raw: dict[str, pd.DataFrame]
    comp_pct: dict[str, pd.DataFrame]
    pillars: dict[str, pd.DataFrame]
    total: pd.DataFrame
    spy: pd.Series
    spy_state: pd.Series
    extra: dict = field(default_factory=dict)


def build_panel(
    stock_bars: dict[str, pd.DataFrame],
    spy: pd.DataFrame,
    sector_of: dict[str, str],
    etf_bars: dict[str, pd.DataFrame],
    weights: dict[str, float] | None = None,
) -> Panel:
    """Calcola indicatori, percentili, pilastri e score totale per ogni giorno e titolo."""
    c, h, lo, o, v = (_wide(stock_bars, k) for k in ("close", "high", "low", "open", "volume"))
    spy_c = spy["close"].astype(float).reindex(c.index).ffill()
    pc = c.shift(1)
    tr = np.maximum(np.maximum(h - lo, (h - pc).abs()), (lo - pc).abs())
    atr = tr.ewm(alpha=1 / ATR_N, adjust=False).mean()
    sma50 = c.rolling(SMA_N).mean()
    ret1 = c / pc - 1

    rsi_avg = sum(_rsi(c, n) for n in RSI_PERIODS) / len(RSI_PERIODS)
    z1, z5 = (c - pc) / atr, (c - c.shift(WEEK)) / atr
    relvol = v / v.rolling(VOL_N).mean().shift(1)
    rng = (h - lo).where(h > lo)
    clv = (((c - lo) - (h - c)) / rng).fillna(0.0)

    ret3 = c / c.shift(FEW) - 1
    etf_ret3 = pd.DataFrame(
        {
            e: b["close"].astype(float).reindex(c.index).ffill().pct_change(FEW)
            for e, b in etf_bars.items()
        }
    )
    sec_ret3 = pd.DataFrame(
        {s: etf_ret3[sector_of[s]] if sector_of.get(s) in etf_ret3 else np.nan for s in c.columns}
    )
    spy_ret3 = spy_c.pct_change(FEW)
    atr_pct = atr / c

    rets = c.pct_change()
    spy_ret = spy_c.pct_change()
    beta = rets.rolling(BETA_N).cov(spy_ret).div(spy_ret.rolling(BETA_N).var(), axis=0)
    state = pd.Series(np.where(spy_c > spy_c.rolling(SMA_N).mean(), 1.0, -1.0), index=c.index)

    raw = {
        "rsi_rev": (100 - rsi_avg).where(c > sma50),
        "move_rev": -(z1 + z5) / 2,
        "volume_dir": (relvol - 1).clip(-1, 3) * np.sign(ret1),
        "close_loc": clv,
        "gap_fade": -((o - pc) / atr).clip(-3, 3),
        "rs_sector_rev": -(ret3 - sec_ret3) / atr_pct,
        "sector_mom": sec_ret3.sub(spy_ret3, axis=0),
        "ctx_beta": beta.mul(state, axis=0),
        "vol_regime": -(tr.rolling(5).mean() / tr.rolling(20).mean()),
    }
    valid = (
        c.notna() & (atr > 0) & sma50.notna() & pc.notna() & c.shift(WEEK).notna() & relvol.notna()
    )
    comp_pct = {k: _pct(x.replace([np.inf, -np.inf], np.nan), valid) for k, x in raw.items()}
    pillars = {}
    for p in PILLARS:
        parts = [comp_pct[k] for k, pp in COMPONENT_PILLAR.items() if pp == p]
        pillars[p] = sum(parts) / len(parts)
    w = {**WEIGHTS, **(weights or {})}
    total = sum(pillars[p] * w[p] for p in PILLARS) / sum(w.values())
    return Panel(c, atr, sma50, beta, valid, raw, comp_pct, pillars, total, spy_c, state)


def market_context(panel: Panel) -> dict:
    """Stato del mercato all'ultimo giorno: SPY, ampiezza dell'universo, regime di volatilità."""
    i = panel.close.index[-1]
    spy = panel.spy
    last = spy.iloc[-1]
    sma = spy.rolling(SMA_N).mean().iloc[-1]
    rsi2 = float(_rsi(spy.to_frame("x"), 2)["x"].iloc[-1])
    valid = panel.valid.loc[i]
    c = panel.close.loc[i][valid]
    above = float((c > panel.sma50.loc[i][valid]).mean())
    up = float((panel.close.loc[i][valid] > panel.close.shift(1).loc[i][valid]).mean())
    # ATR di SPY stimato sui soli close (le barre alte/basse di SPY non sono nel Panel): usa la volatilità
    rets = spy.pct_change()
    vr = float(rets.tail(5).std() / rets.tail(20).std()) if rets.tail(20).std() else float("nan")
    return {
        "date": str(i.date()),
        "spy_close": float(last),
        "spy_ret1": float(spy.iloc[-1] / spy.iloc[-2] - 1),
        "spy_ret5": float(spy.iloc[-1] / spy.iloc[-1 - WEEK] - 1),
        "spy_rsi2": rsi2,
        "spy_vs_sma50": float(last / sma - 1),
        "state": "Rialzista" if last > sma else "Ribassista",
        "breadth_above_sma50": above,
        "breadth_up_yesterday": up,
        "vol_ratio_5_20": vr,
        "vol_regime": "Espansione" if vr > 1.25 else "Compressione" if vr < 0.8 else "Normale",
    }


def latest_table(panel: Panel, names: dict[str, str], sector_of: dict[str, str]) -> list[dict]:
    """Riga per titolo all'ultimo giorno, ordinata per score totale decrescente."""
    i = panel.close.index[-1]
    rows = []
    for s in panel.close.columns[panel.valid.loc[i].to_numpy()]:
        bull = bool(panel.close.at[i, s] > panel.sma50.at[i, s])
        rows.append(
            {
                "symbol": s,
                "name": names.get(s, s),
                "sector": sector_of.get(s),
                "price": float(panel.close.at[i, s]),
                "atr": float(panel.atr.at[i, s]),
                "beta": None if pd.isna(panel.beta.at[i, s]) else float(panel.beta.at[i, s]),
                "bullish": bull,
                "trend_label": "Rialzista" if bull else "Ribassista",
                "total": float(panel.total.at[i, s]),
                "pillars": {p: float(panel.pillars[p].at[i, s]) for p in PILLARS},
                "raw": {
                    k: (None if pd.isna(x.at[i, s]) else float(x.at[i, s]))
                    for k, x in panel.raw.items()
                },
            }
        )
    rows.sort(key=lambda r: -r["total"])
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows


# --- validazione storica ----------------------------------------------------------------------------
def _row_spearman(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """Correlazione di rango riga per riga (un valore per giorno) sui titoli con entrambi i dati."""
    m = a.notna() & b.notna()
    ra, rb = a.where(m).rank(axis=1), b.where(m).rank(axis=1)
    da, db = ra.sub(ra.mean(axis=1), axis=0), rb.sub(rb.mean(axis=1), axis=0)
    den = np.sqrt((da**2).sum(axis=1) * (db**2).sum(axis=1))
    return ((da * db).sum(axis=1) / den.replace(0, np.nan)).dropna()


def _summ(ic: pd.Series, spread: pd.Series) -> dict:
    n = len(ic)
    sd = float(ic.std()) if n > 1 else float("nan")
    t = float(ic.mean() / (sd / np.sqrt(n))) if n > 1 and sd > 0 else float("nan")
    return {
        "n_days": n,
        "ic_mean": float(ic.mean()) if n else float("nan"),
        "ic_t": t,
        "ic_hit": float((ic > 0).mean()) if n else float("nan"),
        "spread_bps": float(spread.mean() * 1e4) if len(spread) else float("nan"),
    }


def ic_report(panel: Panel, warmup: int = 70, cost_bps: float = 5.0) -> dict:
    """Quanto ogni indicatore prevede il rendimento del giorno dopo (chiusura -> chiusura successiva).

    Per ogni giorno si correla (Spearman) l'indicatore con il rendimento successivo dei titoli: IC medio,
    statistica t, % giorni con IC > 0 e spread tra il quinto più alto e il più basso. Due versioni: rendimento
    grezzo e rendimento al netto del beta (rendimento - beta x rendimento di SPY).
    """
    c, spy = panel.close, panel.spy
    fwd = c.shift(-1) / c - 1
    spy_fwd = spy.shift(-1) / spy - 1
    adj = fwd - panel.beta.mul(spy_fwd, axis=0)
    keep = c.index[warmup:-1]
    series = {
        **{f"componente:{k}": v for k, v in panel.comp_pct.items()},
        **{f"pilastro:{k}": v for k, v in panel.pillars.items()},
        "totale": panel.total,
    }
    rows = []
    for name, sc in series.items():
        row = {"indicatore": name}
        for label, tgt in (("grezzo", fwd), ("netto_beta", adj)):
            s, tg = sc.loc[keep], tgt.loc[keep]
            ic = _row_spearman(s, tg)
            r = s.rank(axis=1, pct=True)
            spread = (tg.where(r > 0.8).mean(axis=1) - tg.where(r <= 0.2).mean(axis=1)).dropna()
            row[label] = _summ(ic, spread)
        t = row["netto_beta"]["ic_t"]
        row["verdetto"] = (
            "da verificare"
            if np.isnan(t)
            else "funziona"
            if t >= 2
            else "segno invertito"
            if t <= -2
            else "non distinguibile dal caso"
        )
        rows.append(row)
    # strategia: i TOP_N titoli per score totale, ogni giorno, contro la media dell'universo
    tot = panel.total.loc[keep]
    rank = tot.rank(axis=1, ascending=False)
    sel = rank <= TOP_N
    port = fwd.loc[keep].where(sel).mean(axis=1)
    uni = fwd.loc[keep].where(panel.valid.loc[keep]).mean(axis=1)
    excess = (port - uni).dropna()
    prev = sel.shift(1, fill_value=False)
    turnover = 1 - (sel & prev).sum(axis=1) / sel.sum(axis=1).replace(0, np.nan)
    net = excess - turnover.reindex(excess.index).fillna(1.0) * 2 * cost_bps / 1e4
    sd = float(excess.std()) if len(excess) > 1 else float("nan")
    return {
        "rows": rows,
        "strategy": {
            "top_n": TOP_N,
            "n_days": len(excess),
            "excess_bps_day": float(excess.mean() * 1e4),
            "excess_t": float(excess.mean() / (sd / np.sqrt(len(excess))))
            if sd and sd > 0
            else float("nan"),
            "turnover": float(turnover.mean()),
            "net_bps_day": float(net.mean() * 1e4),
            "cost_bps_per_side": cost_bps,
        },
        "from": str(keep[0].date()),
        "to": str(keep[-1].date()),
    }
