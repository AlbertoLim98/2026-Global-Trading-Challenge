"""Quale indicatore del punteggio spiega meglio le buone performance? (Beta e Breve termine)

Sola lettura: legge gli indicatori salvati nel journal a ogni ribilanciamento (tabella `indicators`, con le
etichette di portafoglio/strategia già corrette), scarica i prezzi successivi da Alpaca (solo dati, nessun
ordine) e misura, per ogni indicatore e per ogni orizzonte:

  * IC medio: correlazione di rango (Spearman) tra indicatore e rendimento, calcolata dentro ogni ribilanciamento
    (così il movimento di tutto il mercato non conta) e poi mediata; accanto, la t di Student e la quota di
    ribilanciamenti con IC > 0;
  * rendimento medio del quintile migliore meno quello peggiore (spread Q5-Q1);
  * coefficiente di una regressione multipla sui ranghi (isola il contributo di ciascun pilastro quando i
    pilastri sono correlati fra loro).

    uv run python sector_lab/indicator_analysis.py --journal sector_lab/journal_beta.db --journal sector_lab/journal_1g.db --env .env.p17

(su PowerShell scrivi il comando su una riga sola)

Il rendimento di un titolo parte dal prezzo registrato nel journal al momento del ribilanciamento e arriva alla
chiusura della seduta k (0 = stessa giornata, 1 = seduta successiva, ...) oppure all'ultimo prezzo ("ora").
Con pochi ribilanciamenti i numeri sono solo indicativi: la t di Student dice quanto fidarsi.
"""

from __future__ import annotations

import argparse
import math
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data
from journal import STRATEGY_LABEL, Journal

# campi numerici che non sono indicatori (identificativi, prezzi, pesi già derivati dal punteggio)
SKIP = {
    "rank", "price", "weight_pct", "weight_in_sector", "target_value", "atr", "value", "shares",
    "sector_rank", "n",
}  # fmt: skip
TOTAL_KEYS = {"scores.total", "total"}
MIN_GROUP = 8  # titoli minimi in un ribilanciamento per calcolare un IC


# --- estrazione ---------------------------------------------------------------------------------
def flatten(d: dict, prefix: str = "") -> dict[str, float]:
    """Tutti i valori numerici di una riga, con nomi 'scores.momentum', 'pillars.rsi', 'beta_1y', ..."""
    out: dict[str, float] = {}
    for k, v in d.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{name}."))
        elif (
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and k not in SKIP
        ):
            out[name] = float(v)
    return out


def load_rows(journal_paths: list[str], strategies: set[str] | None) -> pd.DataFrame:
    """Una riga per (ribilanciamento, titolo) con prezzo, strategia, orario e tutti gli indicatori numerici."""
    recs = []
    for path in journal_paths:
        if not Path(path).exists():
            print(f"(journal non trovato, salto: {path})")
            continue
        with (
            tempfile.TemporaryDirectory() as tmp
        ):  # lavora su una copia: il journal originale non si tocca
            copy = Path(tmp) / "j.db"
            shutil.copy(path, copy)
            j = Journal(copy)
            try:
                items = j.indicators()
            finally:
                j._db.close()
        seen = set()
        for it in items:
            if it["kind"] != "stock":
                continue
            key = (it["run_id"], it["symbol"])
            price = (it["data"] or {}).get("price")
            if key in seen or not price:
                continue
            strat = it.get("strategy") or "?"
            if strategies and strat not in strategies:
                continue
            seen.add(key)
            recs.append(
                {
                    "run_id": it["run_id"],
                    "ts": pd.Timestamp(it["ts"]).tz_convert("UTC"),
                    "symbol": it["symbol"],
                    "strategy": strat,
                    "selected": bool(it["selected"]),
                    "ref_price": float(price),
                    **flatten(it["data"]),
                }
            )
    return pd.DataFrame(recs)


def forward_returns(
    rows: pd.DataFrame,
    bars: dict[str, pd.DataFrame],
    horizons: tuple[int, ...],
) -> pd.DataFrame:
    """Aggiunge ret_k (chiusura k sedute dopo) e ret_now (ultima chiusura) rispetto al prezzo del journal."""
    out = rows.copy()
    cols: dict[str, list[float]] = {f"ret_{k}": [] for k in horizons} | {"ret_now": []}
    for r in out.itertuples():
        b = bars.get(r.symbol)
        day = r.ts.tz_convert("America/New_York").tz_localize(None).normalize()
        vals = dict.fromkeys(cols, np.nan)
        if b is not None and not b.empty:
            closes = b["close"].astype(float)
            pos = closes.index.searchsorted(day)  # prima seduta >= giorno del ribilanciamento
            for k in horizons:
                if pos + k < len(closes):
                    vals[f"ret_{k}"] = closes.iloc[pos + k] / r.ref_price - 1
            if pos < len(closes):
                vals["ret_now"] = closes.iloc[-1] / r.ref_price - 1
        for c, value in vals.items():
            cols[c].append(value)
    for c, v in cols.items():
        out[c] = v
    return out


# --- statistiche ----------------------------------------------------------------------------------
def features_of(df: pd.DataFrame) -> list[str]:
    skip = {"run_id", "ts", "symbol", "strategy", "selected", "ref_price"}
    return [
        c
        for c in df.columns
        if c not in skip and not c.startswith("ret_") and df[c].notna().sum() > 0
    ]


def ic_table(df: pd.DataFrame, ret: str, feats: list[str]) -> pd.DataFrame:
    """IC di rango per ribilanciamento, mediato; t, quota di IC>0, spread Q5-Q1 (rendimento al netto della media)."""
    rows = []
    d = df[df[ret].notna()]
    for f in feats:
        ics, spreads, n_obs = [], [], 0
        for _, g in d.groupby("run_id"):
            g = g[[f, ret]].dropna()
            if len(g) < MIN_GROUP or g[f].nunique() < 3:
                continue
            ic = g[f].rank().corr(g[ret].rank())
            if not math.isfinite(ic):
                continue
            ics.append(ic)
            n_obs += len(g)
            q = pd.qcut(g[f].rank(method="first"), 5, labels=False)
            spreads.append(g.loc[q == 4, ret].mean() - g.loc[q == 0, ret].mean())
        if not ics:
            continue
        a = np.array(ics)
        t = (
            a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))
            if len(a) > 1 and a.std(ddof=1) > 0
            else np.nan
        )
        rows.append(
            {
                "indicatore": f,
                "IC medio": a.mean(),
                "t": t,
                "% rib. IC>0": float((a > 0).mean()),
                "spread Q5-Q1": float(np.mean(spreads)),
                "n rib.": len(a),
                "n titoli": n_obs,
            }
        )
    out = pd.DataFrame(rows)
    return out if out.empty else out.sort_values("IC medio", ascending=False, ignore_index=True)


def rank_regression(df: pd.DataFrame, ret: str, feats: list[str]) -> pd.Series:
    """Regressione multipla del rango del rendimento sui ranghi (percentili) degli indicatori, dentro ogni
    ribilanciamento; restituisce i coefficienti (peso di ciascun indicatore a parità degli altri)."""
    parts = []
    d = df[df[ret].notna()]
    for _, g in d.groupby("run_id"):
        g = g[[*feats, ret]].dropna()
        if len(g) < MIN_GROUP:
            continue
        parts.append(g.rank(pct=True) - 0.5)
    if not parts:
        return pd.Series(dtype=float)
    z = pd.concat(parts)
    if len(z) <= len(feats) + 2:
        return pd.Series(dtype=float)
    coef, *_ = np.linalg.lstsq(z[feats].to_numpy(), z[ret].to_numpy(), rcond=None)
    return pd.Series(coef, index=feats)


def pillar_features(feats: list[str]) -> list[str]:
    """I pilastri che compongono il punteggio (scores.* / pillars.*), totale escluso."""
    return [
        f
        for f in feats
        if f.startswith(("scores.", "pillars."))
        and f not in TOTAL_KEYS
        and not f.endswith(".total")
    ]


def analyze(df: pd.DataFrame, horizons: tuple[int, ...]) -> dict[str, dict]:
    """Per ogni strategia e orizzonte: tabella IC e coefficienti di regressione sui pilastri."""
    res: dict[str, dict] = {}
    for strat, g in df.groupby("strategy"):
        feats = features_of(g)
        pill = pillar_features(feats)
        per = {}
        for ret in [f"ret_{k}" for k in horizons] + ["ret_now"]:
            if g[ret].notna().sum() < MIN_GROUP:
                continue
            ic = ic_table(g, ret, feats)
            if ic.empty:
                continue
            per[ret] = {
                "ic": ic,
                "reg": rank_regression(g, ret, pill) if len(pill) > 1 else pd.Series(dtype=float),
                "n": int(g[ret].notna().sum()),
                "runs": int(g.loc[g[ret].notna(), "run_id"].nunique()),
            }
        res[strat] = {"per": per, "pillars": pill}
    return res


# --- output ---------------------------------------------------------------------------------------
def label_ret(ret: str) -> str:
    return "ultimo prezzo" if ret == "ret_now" else f"chiusura +{ret.split('_')[1]} sedute"


def report(res: dict[str, dict], top: int = 12) -> str:
    L: list[str] = []
    for strat, info in res.items():
        L += ["", "=" * 78, f"STRATEGIA: {STRATEGY_LABEL.get(strat, strat)}", "=" * 78]
        if not info["per"]:
            L.append(
                "Nessun rendimento calcolabile (ribilanciamenti troppo recenti o pochi titoli)."
            )
            continue
        for ret, r in info["per"].items():
            L += [
                "",
                f"Orizzonte: {label_ret(ret)}  ({r['n']} titoli in {r['runs']} ribilanciamenti)",
            ]
            t = r["ic"]
            if t.empty:
                L.append("  dati insufficienti")
                continue
            L.append(f"  {'indicatore':<34}{'IC':>8}{'t':>7}{'%IC>0':>7}{'Q5-Q1':>9}{'n rib':>7}")
            sel = pd.concat([t.head(top), t.tail(3)]).drop_duplicates("indicatore")
            for x in sel.itertuples(index=False):
                tt = "" if pd.isna(x[2]) else f"{x[2]:.1f}"
                L.append(f"  {x[0]:<34}{x[1]:>+8.3f}{tt:>7}{x[3]:>7.0%}{x[4]:>+9.2%}{x[5]:>7}")
            if len(r["reg"]):
                reg = r["reg"].sort_values(ascending=False)
                L.append("  Peso dei pilastri a parità degli altri (regressione sui ranghi):")
                L += [f"    {k:<32}{v:>+8.3f}" for k, v in reg.items()]
        best = _best(info)
        if best:
            L += ["", f">> Indicatore con IC medio più alto, sul primo orizzonte con dati: {best}"]
    L += [
        "",
        "Come leggere: IC > 0 = più alto è l'indicatore, meglio va il titolo (|IC| 0,05 è già utile, 0,10 forte).",
        "t sopra ~2 con molti ribilanciamenti = relazione credibile; con pochi ribilanciamenti è solo un indizio.",
        "L'IC è calcolato dentro ogni ribilanciamento: non premia chi segue il mercato.",
    ]
    return "\n".join(L)


def _best(info: dict) -> str | None:
    for r in info["per"].values():
        if not r["ic"].empty:
            pill = [p for p in info["pillars"] if p in set(r["ic"]["indicatore"])]
            t = r["ic"]
            t = t[t["indicatore"].isin(pill)] if pill else t
            if not t.empty:
                return f"{t.iloc[0]['indicatore']} (IC {t.iloc[0]['IC medio']:+.3f})"
    return None


def to_excel(res: dict[str, dict], path: str) -> None:
    with pd.ExcelWriter(path) as xw:
        for strat, info in res.items():
            for ret, r in info["per"].items():
                name = f"{strat}_{ret.replace('ret_', '')}"[:28]
                r["ic"].to_excel(xw, sheet_name=name, index=False)
                if len(r["reg"]):
                    r["reg"].rename("coefficiente").to_frame().to_excel(
                        xw, sheet_name=f"{name}_reg"[:31]
                    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--journal", action="append", required=True, help="journal da leggere (ripetibile)"
    )
    ap.add_argument(
        "--strategy", action="append", help="solo queste strategie (beta, short, quality)"
    )
    ap.add_argument(
        "--env", default=".env", help="file con le chiavi Alpaca (servono solo per i prezzi)"
    )
    ap.add_argument("--feed", default="iex", choices=["iex", "sip"])
    ap.add_argument(
        "--horizons", default="0,1,3,5", help="sedute dopo il ribilanciamento, es. 0,1,3,5"
    )
    ap.add_argument("--top", type=int, default=12, help="righe mostrate per tabella")
    ap.add_argument("--out", help="Excel di dettaglio (facoltativo)")
    a = ap.parse_args()

    horizons = tuple(int(x) for x in a.horizons.split(",") if x.strip())
    rows = load_rows(a.journal, set(a.strategy) if a.strategy else None)
    if rows.empty:
        raise SystemExit("Nessun indicatore di titoli nel journal indicato.")
    from compare_portfolios import credentials

    creds = credentials(a.env)
    syms = sorted(rows["symbol"].unique())
    print(
        f"{len(rows)} osservazioni, {rows['run_id'].nunique()} ribilanciamenti, {len(syms)} titoli: scarico i prezzi..."
    )
    bars = data.fetch_bars(syms, a.feed, creds)
    df = forward_returns(rows, bars, horizons)
    res = analyze(df, horizons)
    print(report(res, a.top))
    if a.out:
        to_excel(res, a.out)
        print(f"\nExcel scritto: {a.out}")


if __name__ == "__main__":
    main()
