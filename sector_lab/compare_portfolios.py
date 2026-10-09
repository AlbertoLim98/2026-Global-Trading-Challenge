"""Confronta due portafogli Alpaca paper, ognuno con il proprio file di chiavi (.env).

Sola lettura: non invia ordini. Per ogni portafoglio legge conto, posizioni e storico del patrimonio, e (se
indicato) un riepilogo del journal; poi confronta rendimento, rischio, composizione, sovrapposizione dei titoli
e dei settori, anche rispetto a SPY. Stampa un riepilogo e scrive un Excel.

    uv run python sector_lab/compare_portfolios.py --env-a .env.p17 --env-b .env.p18 \\
        --name-a "Portafoglio 17" --name-b "Portafoglio 18" --period 1M

(su PowerShell scrivi il comando su una riga sola)

Opzioni utili: --journal-a/--journal-b (percorso del journal di ciascuno), --period (1W, 1M, 3M, 1A, all),
--out (file Excel), --feed (iex|sip), --demo (conti simulati, senza chiavi).
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import data
from export import Col, Workbook, _notes_sheet, _save, _sheet
from journal import STRATEGY_LABEL

PCT, PCT2, NUM, MONEY = "0.0%", "0.00%", "0.00", "#,##0"
# barre per seduta di ogni timeframe (per annualizzare la volatilità)
BARS_PER_DAY = {"1D": 1, "1H": 7, "15Min": 26, "5Min": 78, "1Min": 390}
MINUTES = {"1H": 60, "15Min": 15, "5Min": 5, "1Min": 1}


# --- lettura dei dati ------------------------------------------------------------------------------
def read_env(path: str | Path) -> dict[str, str]:
    """Legge KEY=VALUE da un file .env senza toccare le variabili d'ambiente del processo."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("\"'")
    return out


def credentials(path: str | Path) -> tuple[str, str]:
    env = read_env(path)
    key, secret = env.get("ALPACA_API_KEY", ""), env.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise SystemExit(f"{path}: mancano ALPACA_API_KEY e/o ALPACA_SECRET_KEY")
    return key, secret


def _empty_curve() -> pd.Series:
    return pd.Series(dtype=float, index=pd.DatetimeIndex([]))


def journal_curve(paths: str | Path | list | tuple | None) -> pd.Series:
    """Curva del patrimonio campionata dal programma e salvata nel journal (tabella equity_curve), in sola lettura."""
    if not paths:
        return _empty_curve()
    if isinstance(paths, (str, Path)):
        paths = [paths]
    parts = []
    for path in paths:
        if not Path(path).exists():
            continue
        db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT ts, equity FROM equity_curve WHERE equity > 0 ORDER BY ts, id"
            ).fetchall()
        except sqlite3.DatabaseError:
            rows = []  # journal senza la tabella (versione vecchia)
        finally:
            db.close()
        if rows:
            s = pd.Series({pd.Timestamp(t).tz_localize(None): float(e) for t, e in rows})
            parts.append(s)
    if not parts:
        return _empty_curve()
    out = pd.concat(parts).sort_index()
    return out[~out.index.duplicated(keep="last")]


def resample_curve(s: pd.Series, timeframe: str) -> pd.Series:
    """Porta la curva campionata (irregolare) sulla griglia del timeframe scelto, ultimo valore di ogni intervallo."""
    if s.empty:
        return s
    rule = "1D" if timeframe == "1D" else f"{MINUTES[timeframe]}min"
    return s.resample(rule).last().dropna()


def clean_equity(s: pd.Series, max_dev: float = 0.5) -> tuple[pd.Series, list[float]]:
    """Toglie dallo storico i punti impossibili (patrimonio <= 0 o oltre `max_dev` dalla mediana).

    Lo storico intraday di Alpaca a volte contiene valori anomali (soprattutto su conti azzerati o appena
    ricreati): lasciarli falserebbe rendimento e volatilità. Restituisce la serie pulita e i valori scartati.
    """
    if s.empty:
        return s, []
    med = float(s.median())
    bad = (s <= 0) | ((s / med - 1).abs() > max_dev)
    return s[~bad], [float(v) for v in s[bad]]


def snapshot(
    broker, name: str, period: str, creds: tuple[str, str] | None = None, timeframe: str = "1D"
) -> dict:
    """Foto di un portafoglio: conto, posizioni (con peso) e storico del patrimonio."""
    acct = broker.account()
    equity = acct["equity"]
    pos = broker.positions()
    for p in pos:
        p["weight"] = p["market_value"] / equity if equity else 0.0
    hist = pd.Series(dict(broker.portfolio_history(period, timeframe)), dtype=float)
    hist.index = pd.to_datetime(hist.index)
    hist, dropped = clean_equity(hist.sort_index())
    return {
        "dropped": dropped,
        "name": name,
        "account": acct,
        "positions": pos,
        "history": hist.sort_index(),
        "creds": creds,
        "cash_weight": acct["cash"] / equity if equity else 0.0,
    }


def journal_summary(paths: str | Path | list | tuple | None) -> dict:
    """Riepilogo in sola lettura di uno o più journal dello stesso portafoglio (ribilanciamenti, strategia, esiti).

    Con più file i conteggi si sommano, il primo ordine eseguito è il più antico e la strategia è quella
    dell'ultimo ribilanciamento.
    """
    if not paths:
        return {}
    if isinstance(paths, (str, Path)):
        return _journal_one(paths)
    parts = [x for x in (_journal_one(p) for p in paths) if x]
    if not parts:
        return {}
    last = max(
        (x for x in parts if x.get("last_run")), key=lambda x: x["last_run"], default=parts[0]
    )
    fills = [x["first_fill"] for x in parts if x.get("first_fill")]
    ops: dict[str, int] = {}
    for x in parts:
        for k, v in x.get("operations", {}).items():
            ops[k] = ops.get(k, 0) + v
    return {
        **last,
        "n_runs": sum(x.get("n_runs", 0) for x in parts),
        "operations": ops,
        "trade_failed": sum(x.get("trade_failed", 0) for x in parts),
        "first_fill": min(fills) if fills else None,
        "n_journals": len(parts),
    }


def _journal_one(path: str | Path) -> dict:
    if not Path(path).exists():
        return {}
    db = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(events)")}
        sel = "ts, run_id, payload" + (", portfolio, strategy" if "strategy" in cols else "")
        runs = db.execute(f"SELECT {sel} FROM events WHERE kind = 'RUN' ORDER BY id").fetchall()
        status = {
            r["status"]: r["n"]
            for r in db.execute("SELECT status, COUNT(*) AS n FROM proposals GROUP BY status")
        }
        failed = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'TRADE_FAILED'").fetchone()[0]
        first_fill = db.execute(
            "SELECT MIN(updated) FROM proposals WHERE status = 'filled'"
        ).fetchone()[0]
    except sqlite3.DatabaseError:
        return {}
    finally:
        db.close()
    import json

    out: dict = {
        "n_runs": len(runs),
        "operations": status,
        "trade_failed": failed,
        "first_fill": first_fill,  # primo ordine eseguito: da qui il portafoglio è davvero investito
    }
    if runs:
        last = runs[-1]
        p = json.loads(last["payload"])
        mode = (p.get("params") or {}).get("mode")
        out |= {
            "last_run": last["ts"],
            "portfolio": (last["portfolio"] if "strategy" in cols else None) or p.get("portfolio"),
            "strategy": (last["strategy"] if "strategy" in cols else None) or mode,
            "first_equity": (json.loads(runs[0]["payload"]).get("account") or {}).get("equity"),
        }
    return out


# --- metriche ---------------------------------------------------------------------------------------
def series_metrics(s: pd.Series, periods_per_year: float = 252) -> dict:
    """Rendimento, volatilità e perdita massima di una serie di patrimonio giornaliero."""
    s = s.dropna()
    if len(s) < 2:
        return {
            "n_days": len(s),
            "n_points": len(s),
            "total_return": None,
            "vol": None,
            "max_drawdown": None,
            "best_day": None,
            "worst_day": None,
        }
    r = s.pct_change().dropna()
    return {
        "n_days": len(s),
        "n_points": len(s),
        "total_return": float(s.iloc[-1] / s.iloc[0] - 1),
        "vol": float(r.std() * math.sqrt(periods_per_year)) if len(r) > 1 else None,
        "max_drawdown": float((s / s.cummax() - 1).min()),
        "best_day": float(r.max()),
        "worst_day": float(r.min()),
    }


def spy_series(
    creds: tuple[str, str] | None, start: pd.Timestamp, feed: str, timeframe: str = "1D"
) -> pd.Series | None:
    """Chiusure di SPY dal giorno `start` (giornaliere o intraday); None se non raggiungibile o in demo."""
    if creds is None:
        return None
    try:
        if timeframe == "1D":
            bars = data.fetch_bars([data.BENCHMARK], feed, creds)
            s = bars.get(data.BENCHMARK)
            return None if s is None else s["close"].loc[start:]
        begin = (start - pd.Timedelta(days=1)).to_pydatetime().replace(tzinfo=UTC)
        return data.fetch_intraday(data.BENCHMARK, begin, MINUTES[timeframe], feed, creds)
    except Exception:  # noqa: BLE001 - il confronto con SPY è facoltativo
        return None


def _fmt_ts(ts: pd.Timestamp) -> str:
    return str(ts.date()) if (ts.hour, ts.minute) == (0, 0) else f"{ts:%Y-%m-%d %H:%M}"


def compare(
    a: dict,
    b: dict,
    spy: pd.Series | None = None,
    start: pd.Timestamp | None = None,
    ppy: float = 252,
    own_starts: tuple[pd.Timestamp | None, pd.Timestamp | None] = (None, None),
) -> dict:
    """Confronto completo di due fotografie."""
    uni = data.load_universe()
    names, sectors = (
        dict(zip(uni.ticker, uni.name, strict=True)),
        dict(zip(uni.ticker, uni.sector_etf, strict=True)),
    )

    # finestra comune: dal primo giorno in cui entrambi hanno un patrimonio
    ha, hb = a["history"], b["history"]
    common = ha.index.intersection(hb.index)
    if start is not None:  # dal momento in cui anche il portafoglio più recente è investito
        common = common[common >= start]
    win = {}
    if len(common) >= 2:
        sa, sb = ha.loc[common], hb.loc[common]
        win = {
            "from": _fmt_ts(common[0]),
            "to": _fmt_ts(common[-1]),
            "a": series_metrics(sa, ppy),
            "b": series_metrics(sb, ppy),
        }
        if spy is not None and len(spy):
            sp = spy.copy()
            sp.index = pd.to_datetime(sp.index)
            sp = sp.reindex(common).ffill().dropna()
            if len(sp) >= 2:
                win["spy"] = series_metrics(sp, ppy)
        win["curve"] = pd.DataFrame(
            {a["name"]: sa / sa.iloc[0] * 100, b["name"]: sb / sb.iloc[0] * 100}
            | (
                {"SPY": spy_n}
                if (spy_n := _norm(win.get("spy") and spy, common)) is not None
                else {}
            )
        )
    own = {
        k: series_metrics(h.loc[st:] if st is not None else h, ppy)
        for k, h, st in (("a", ha, own_starts[0]), ("b", hb, own_starts[1]))
    }

    # posizioni
    wa = {p["symbol"]: p for p in a["positions"]}
    wb = {p["symbol"]: p for p in b["positions"]}
    rows = []
    for sym in sorted(set(wa) | set(wb)):
        pa, pb = wa.get(sym), wb.get(sym)
        rows.append(
            {
                "symbol": sym,
                "name": names.get(sym, sym),
                "sector": sectors.get(sym, "fuori universo"),
                "in": "entrambi" if pa and pb else a["name"] if pa else b["name"],
                "w_a": pa["weight"] if pa else 0.0,
                "w_b": pb["weight"] if pb else 0.0,
                "value_a": pa["market_value"] if pa else 0.0,
                "value_b": pb["market_value"] if pb else 0.0,
                "pl_pct_a": pa["pl_pct"] if pa else None,
                "pl_pct_b": pb["pl_pct"] if pb else None,
            }
        )
    for r in rows:
        r["diff"] = r["w_a"] - r["w_b"]
    rows.sort(key=lambda r: -(r["w_a"] + r["w_b"]))
    both = [r for r in rows if r["in"] == "entrambi"]
    union = len(rows)
    overlap = {
        "n_a": len(wa),
        "n_b": len(wb),
        "n_common": len(both),
        "jaccard": len(both) / union if union else 0.0,
        "weight_overlap": float(sum(min(r["w_a"], r["w_b"]) for r in rows)),
    }

    # settori
    sec: dict[str, dict] = {}
    for r in rows:
        d = sec.setdefault(
            r["sector"], {"sector": r["sector"], "w_a": 0.0, "w_b": 0.0, "n_a": 0, "n_b": 0}
        )
        d["w_a"] += r["w_a"]
        d["w_b"] += r["w_b"]
        d["n_a"] += r["w_a"] > 0
        d["n_b"] += r["w_b"] > 0
    sector_rows = sorted(sec.values(), key=lambda d: -(d["w_a"] + d["w_b"]))
    for d in sector_rows:
        d["diff"] = d["w_a"] - d["w_b"]
        d["name"] = data.SECTORS.get(d["sector"], d["sector"])
    return {
        "a": a,
        "b": b,
        "own": own,
        "window": win,
        "positions": rows,
        "overlap": overlap,
        "sectors": sector_rows,
    }


def _norm(spy: pd.Series | None, common: pd.DatetimeIndex) -> pd.Series | None:
    if spy is None or not len(spy):
        return None
    sp = spy.copy()
    sp.index = pd.to_datetime(sp.index)
    sp = sp.reindex(common).ffill().dropna()
    return None if len(sp) < 2 else sp / sp.iloc[0] * 100


# --- stampa ------------------------------------------------------------------------------------------
def _f(v, kind="pct") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    return {"pct": f"{v:+.2%}", "p": f"{v:.1%}", "usd": f"{v:,.0f}$", "n": f"{v:.2f}"}[kind]


def report(res: dict, ja: dict | None = None, jb: dict | None = None) -> str:
    a, b = res["a"], res["b"]
    ja, jb = ja or {}, jb or {}
    L = [f"{'':<34}{a['name']:>22}{b['name']:>22}", "-" * 78]

    def row(label, va, vb):
        L.append(f"{label:<34}{va:>22}{vb:>22}")

    for x in (a, b):
        if x.get("dropped"):
            ex = ", ".join(f"{v:,.0f}" for v in x["dropped"][:4])
            L.insert(
                0,
                f"ATTENZIONE: scartati {len(x['dropped'])} punti anomali dallo storico di {x['name']} (es. {ex}$).",
            )
    row("Patrimonio", _f(a["account"]["equity"], "usd"), _f(b["account"]["equity"], "usd"))
    row("Liquidità", _f(a["cash_weight"], "p"), _f(b["cash_weight"], "p"))
    row("N. titoli", str(len(a["positions"])), str(len(b["positions"])))
    row(
        "P/L non realizzato",
        _f(sum(p["pl"] for p in a["positions"]), "usd"),
        _f(sum(p["pl"] for p in b["positions"]), "usd"),
    )
    if ja or jb:
        row(
            "Strategia (journal)",
            STRATEGY_LABEL.get(ja.get("strategy"), ja.get("strategy") or "–"),
            STRATEGY_LABEL.get(jb.get("strategy"), jb.get("strategy") or "–"),
        )
        row("Ribilanciamenti", str(ja.get("n_runs", "–")), str(jb.get("n_runs", "–")))
        row(
            "Operazioni fallite", str(ja.get("trade_failed", "–")), str(jb.get("trade_failed", "–"))
        )
    w = res["window"]
    if w:
        L += [
            "",
            f"Finestra comune: dal {w['from']} al {w['to']} ({w['a']['n_points']} rilevazioni)",
            "-" * 78,
        ]
        for label, key, kind in (
            ("Rendimento", "total_return", "pct"),
            ("Volatilità annualizzata", "vol", "p"),
            ("Perdita massima", "max_drawdown", "pct"),
            ("Rilevazione migliore", "best_day", "pct"),
            ("Rilevazione peggiore", "worst_day", "pct"),
        ):
            row(label, _f(w["a"][key], kind), _f(w["b"][key], kind))
        if "spy" in w:
            L.append(
                f"{'SPY (stessa finestra)':<34}{_f(w['spy']['total_return']):>22}  vol {_f(w['spy']['vol'], 'p')}  perdita max {_f(w['spy']['max_drawdown'])}"
            )
            row(
                "Extra-rendimento vs SPY",
                _f(_diff(w["a"]["total_return"], w["spy"]["total_return"])),
                _f(_diff(w["b"]["total_return"], w["spy"]["total_return"])),
            )
    else:
        L += [
            "",
            "Storico del patrimonio insufficiente per una finestra comune (servono almeno 2 rilevazioni in comune).",
        ]
    own = res["own"]
    if own["a"]["total_return"] is not None and own["b"]["total_return"] is not None:
        L += ["", "Dall'inizio di ciascun portafoglio (primo ordine eseguito, se noto)", "-" * 78]
        row(
            "Rendimento dal proprio inizio",
            _f(own["a"]["total_return"]),
            _f(own["b"]["total_return"]),
        )
        row(
            "Perdita massima dal proprio inizio",
            _f(own["a"]["max_drawdown"]),
            _f(own["b"]["max_drawdown"]),
        )
        row("Rilevazioni", str(own["a"]["n_points"]), str(own["b"]["n_points"]))
    o = res["overlap"]
    L += [
        "",
        f"Titoli in comune: {o['n_common']} (su {o['n_a']} e {o['n_b']}); sovrapposizione dei pesi {o['weight_overlap']:.0%}; indice di Jaccard {o['jaccard']:.0%}",
        "-" * 78,
    ]
    L.append(f"{'Settore':<26}{a['name'][:16]:>18}{b['name'][:16]:>18}{'differenza':>14}")
    for d in res["sectors"][:12]:
        L.append(f"{d['name'][:25]:<26}{d['w_a']:>18.1%}{d['w_b']:>18.1%}{d['diff']:>+14.1%}")
    L += ["", "Maggiori differenze di peso per titolo:"]
    for r in sorted(res["positions"], key=lambda r: -abs(r["diff"]))[:8]:
        L.append(
            f"  {r['symbol']:<7}{r['w_a']:>8.1%}{r['w_b']:>8.1%}{r['diff']:>+9.1%}  {r['name'][:30]}"
        )
    L += [
        "",
        "Nota: il rendimento è del conto (patrimonio), non dei singoli titoli; con pochi giorni di storico le differenze possono essere solo rumore.",
    ]
    return "\n".join(L)


def _diff(x, y):
    return None if x is None or y is None else x - y


# --- Excel -------------------------------------------------------------------------------------------
def to_excel(res: dict, ja: dict | None, jb: dict | None, path: str | Path) -> None:
    a, b = res["a"], res["b"]
    ja, jb = ja or {}, jb or {}
    wb = Workbook()
    wb.remove(wb.active)
    w = res["window"]

    # Riepilogo
    ws = wb.create_sheet("Riepilogo")
    items: list[tuple[str, object, object, str | None]] = [
        ("Patrimonio ($)", a["account"]["equity"], b["account"]["equity"], MONEY),
        ("Liquidità ($)", a["account"]["cash"], b["account"]["cash"], MONEY),
        ("Liquidità (%)", a["cash_weight"], b["cash_weight"], PCT),
        ("N. titoli", len(a["positions"]), len(b["positions"]), "0"),
        (
            "P/L non realizzato ($)",
            sum(p["pl"] for p in a["positions"]),
            sum(p["pl"] for p in b["positions"]),
            MONEY,
        ),
        (
            "Strategia (journal)",
            STRATEGY_LABEL.get(ja.get("strategy"), ja.get("strategy")),
            STRATEGY_LABEL.get(jb.get("strategy"), jb.get("strategy")),
            None,
        ),
        ("Ribilanciamenti (journal)", ja.get("n_runs"), jb.get("n_runs"), "0"),
        ("Operazioni fallite (journal)", ja.get("trade_failed"), jb.get("trade_failed"), "0"),
    ]
    if w:
        for label, key, fmt in (
            ("Rendimento (finestra comune)", "total_return", PCT2),
            ("Volatilità annualizzata", "vol", PCT),
            ("Perdita massima", "max_drawdown", PCT2),
            ("Giorno migliore", "best_day", PCT2),
            ("Giorno peggiore", "worst_day", PCT2),
        ):
            items.append((label, w["a"][key], w["b"][key], fmt))
        if "spy" in w:
            items.append(
                (
                    "Rendimento SPY (stessa finestra)",
                    w["spy"]["total_return"],
                    w["spy"]["total_return"],
                    PCT2,
                )
            )
            items.append(
                (
                    "Extra-rendimento vs SPY",
                    _diff(w["a"]["total_return"], w["spy"]["total_return"]),
                    _diff(w["b"]["total_return"], w["spy"]["total_return"]),
                    PCT2,
                )
            )
    rows = [{"label": lbl, "a": va, "b": vb, "fmt": fmt} for lbl, va, vb, fmt in items]
    cols: list[Col] = [
        ("", "Indicatore", lambda r: r["label"], None),
        ("", a["name"], lambda r: r["a"], None),
        ("", b["name"], lambda r: r["b"], None),
    ]
    _sheet(ws, cols, rows, freeze="B3")
    for i, r in enumerate(rows, 3):
        for c in (2, 3):
            if r["fmt"]:
                ws.cell(i, c).number_format = r["fmt"]
    ws.column_dimensions["A"].width = 36
    for c in "BC":
        ws.column_dimensions[c].width = 24

    # Posizioni
    ws = wb.create_sheet("Posizioni")
    pcols: list[Col] = [
        ("", "Titolo", lambda r: r["symbol"], None),
        ("", "Azienda", lambda r: r["name"], None),
        ("", "Settore", lambda r: r["sector"], None),
        ("", "Presente in", lambda r: r["in"], None),
        (a["name"], "Peso", lambda r: r["w_a"], PCT),
        (a["name"], "Valore ($)", lambda r: r["value_a"], MONEY),
        (a["name"], "P/L %", lambda r: r["pl_pct_a"], PCT2),
        (b["name"], "Peso", lambda r: r["w_b"], PCT),
        (b["name"], "Valore ($)", lambda r: r["value_b"], MONEY),
        (b["name"], "P/L %", lambda r: r["pl_pct_b"], PCT2),
        ("", "Differenza peso (A-B)", lambda r: r["diff"], PCT),
    ]
    _sheet(ws, pcols, res["positions"], freeze="C3")

    # Settori
    ws = wb.create_sheet("Settori")
    scols: list[Col] = [
        ("", "Settore", lambda r: r["name"], None),
        (a["name"], "Peso", lambda r: r["w_a"], PCT),
        (a["name"], "N. titoli", lambda r: r["n_a"], "0"),
        (b["name"], "Peso", lambda r: r["w_b"], PCT),
        (b["name"], "N. titoli", lambda r: r["n_b"], "0"),
        ("", "Differenza peso (A-B)", lambda r: r["diff"], PCT),
    ]
    _sheet(ws, scols, res["sectors"], freeze="B3")

    # Andamento
    if w and "curve" in w:
        ws = wb.create_sheet("Andamento (base 100)")
        cur = w["curve"]
        ccols: list[Col] = [("", "Data", lambda r: r["date"], None)] + [
            ("", c, (lambda r, c=c: r[c]), NUM) for c in cur.columns
        ]
        _sheet(
            ws,
            ccols,
            [{"date": str(i.date()), **row.to_dict()} for i, row in cur.iterrows()],
            freeze="B3",
        )

    o = res["overlap"]
    _notes_sheet(
        wb,
        "Confronto tra due portafogli",
        [
            f"Portafoglio A: {a['name']} (conto {a['account'].get('account_number', '?')}); B: {b['name']} (conto {b['account'].get('account_number', '?')})",
            f"Titoli in comune: {o['n_common']}; sovrapposizione dei pesi {o['weight_overlap']:.1%} (somma dei minimi tra i due pesi); Jaccard {o['jaccard']:.1%}",
            f"Finestra comune: {w['from']} - {w['to']}"
            if w
            else "Finestra comune: non disponibile",
            "Rendimento, volatilità e perdita massima si calcolano sul patrimonio del conto, giorno per giorno.",
            "Con pochi giorni di storico le differenze possono essere solo rumore: confrontare almeno 20 sedute.",
        ],
    )
    Path(path).write_bytes(_save(wb))


# --- avvio ------------------------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument(
        "--env-a", help="file di chiavi del portafoglio A (ALPACA_API_KEY / ALPACA_SECRET_KEY)"
    )
    ap.add_argument("--env-b", help="file di chiavi del portafoglio B")
    ap.add_argument("--name-a", default="Portafoglio A")
    ap.add_argument("--name-b", default="Portafoglio B")
    ap.add_argument(
        "--journal-a",
        action="append",
        help="journal del portafoglio A (facoltativo; ripetibile se è su più file)",
    )
    ap.add_argument(
        "--journal-b",
        action="append",
        help="journal del portafoglio B (facoltativo; ripetibile se è su più file)",
    )
    ap.add_argument(
        "--period", default="1M", help="storico del patrimonio: 1W, 1M, 3M, 1A, all (default 1M)"
    )
    ap.add_argument(
        "--timeframe",
        default="1D",
        choices=sorted(BARS_PER_DAY),
        help="granularità dello storico: 1D (giornaliero, default) oppure intraday 1H, 15Min, 5Min, 1Min.\n"
        "Con portafogli partiti in momenti diversi conviene un intraday (es. --period 1W --timeframe 15Min)",
    )
    ap.add_argument(
        "--from",
        dest="from_",
        help="inizio della finestra di confronto (AAAA-MM-GG o AAAA-MM-GGTHH:MM, UTC). Default: il primo ordine\n"
        "eseguito del portafoglio più recente (se si indicano i journal), altrimenti il primo giorno in comune",
    )
    ap.add_argument(
        "--history-source",
        default="auto",
        choices=["auto", "journal", "alpaca"],
        help="da dove leggere l'andamento del patrimonio: 'journal' = campionato dal programma (affidabile),\n"
        "'alpaca' = storico di Alpaca, 'auto' (default) = journal se ha abbastanza punti per entrambi",
    )
    ap.add_argument(
        "--record",
        action="store_true",
        help="registra nel journal (il primo di ciascun portafoglio) il patrimonio attuale del conto: utile se\n"
        "l'app non resta sempre aperta, lancia lo script a fine seduta per costruire la curva",
    )
    ap.add_argument(
        "--debug-history",
        action="store_true",
        help="stampa lo storico del patrimonio esattamente come lo restituisce Alpaca (per capire i dati anomali)",
    )
    ap.add_argument("--feed", default="iex")
    ap.add_argument(
        "--out", help="file Excel di output (default confronto_portafogli_AAAA-MM-GG.xlsx)"
    )
    ap.add_argument("--demo", action="store_true", help="conti simulati, senza chiavi")
    a = ap.parse_args(argv)

    if a.demo:
        brokers = _demo_brokers()
        creds_a = creds_b = None
    else:
        if not (a.env_a and a.env_b):
            ap.error("servono --env-a e --env-b (oppure --demo)")
        from broker import AlpacaBroker

        creds_a, creds_b = credentials(a.env_a), credentials(a.env_b)
        brokers = (AlpacaBroker(*creds_a, feed=a.feed), AlpacaBroker(*creds_b, feed=a.feed))
        if creds_a == creds_b:
            print(
                "ATTENZIONE: i due file contengono le stesse chiavi (stesso conto).",
                file=sys.stderr,
            )
    if a.debug_history:
        for br, nm in zip(brokers, (a.name_a, a.name_b), strict=True):
            print(_raw_report(nm, br.portfolio_history_raw(a.period, a.timeframe)))
    sa = snapshot(brokers[0], a.name_a, a.period, creds_a, a.timeframe)
    sb = snapshot(brokers[1], a.name_b, a.period, creds_b, a.timeframe)
    if a.record:
        from journal import Journal

        for snap, paths in ((sa, a.journal_a), (sb, a.journal_b)):
            if paths:
                Journal(paths[0]).log_equity(
                    snap["account"]["equity"], snap["account"]["cash"], len(snap["positions"])
                )
                print(f"Patrimonio di {snap['name']} registrato in {paths[0]}")
    ja, jb = journal_summary(a.journal_a), journal_summary(a.journal_b)
    fills = [
        pd.Timestamp(j["first_fill"]).tz_localize(None) if j.get("first_fill") else None
        for j in (ja, jb)
    ]
    start = (
        pd.Timestamp(a.from_)
        if a.from_
        else (max(fills) if all(f is not None for f in fills) else None)
    )
    if start is not None and start.tzinfo is not None:
        start = start.tz_convert("UTC").tz_localize(None)
    if start is not None:
        print(f"Finestra di confronto dal {_fmt_ts(start)} UTC "
              f"({'indicata da te' if a.from_ else 'primo ordine eseguito del portafoglio più recente'})\n")  # fmt: skip
    _choose_history(sa, sb, a.journal_a, a.journal_b, a.history_source, a.timeframe, start)
    spy_from = (
        start
        if start is not None
        else (
            max(sa["history"].index.min(), sb["history"].index.min())
            if len(sa["history"]) and len(sb["history"])
            else pd.Timestamp(datetime.now(UTC).date())
        )
    )
    ppy = 252 * BARS_PER_DAY[a.timeframe]
    res = compare(
        sa, sb, spy_series(creds_a, spy_from, a.feed, a.timeframe), start, ppy, tuple(fills)
    )
    print(report(res, ja, jb))
    out = a.out or f"confronto_portafogli_{datetime.now(UTC):%Y-%m-%d}.xlsx"
    to_excel(res, ja, jb, out)
    print(f"\nExcel scritto in {out}")
    return 0


def _choose_history(sa, sb, ja_paths, jb_paths, source, timeframe, start) -> str:
    """Sceglie l'andamento del patrimonio: curva del journal (se c'è per entrambi) o storico di Alpaca."""
    if source != "alpaca":
        ca = resample_curve(journal_curve(ja_paths), timeframe)
        cb = resample_curve(journal_curve(jb_paths), timeframe)
        if start is not None and len(ca) and len(cb):
            ca, cb = ca[ca.index >= start], cb[cb.index >= start]
        if len(ca) >= 3 and len(cb) >= 3:
            sa["history"], sb["history"] = ca, cb
            sa["dropped"] = sb["dropped"] = []
            print("Andamento del patrimonio: dal journal (campionato dal programma)\n")
            return "journal"
        if source == "journal":
            print(
                f"ATTENZIONE: curva del journal insufficiente ({len(ca)} e {len(cb)} punti): uso Alpaca.",
                file=sys.stderr,
            )
        else:
            print(
                "Andamento del patrimonio: da Alpaca (nel journal non ci sono ancora abbastanza campioni)\n"
            )
    else:
        print("Andamento del patrimonio: da Alpaca\n")
    return "alpaca"


def _raw_report(name: str, raw: dict) -> str:
    """Prime righe dello storico grezzo di Alpaca e un riepilogo (per diagnosticare valori anomali)."""
    eq = [e for e in raw["equity"] if e is not None]
    L = [
        f"== Storico grezzo di {name}: {len(raw['timestamp'])} punti, base_value {raw.get('base_value')}"
    ]
    if eq:
        srt = sorted(eq)
        L.append(f"   patrimonio: min {srt[0]:,.2f}  mediana {srt[len(srt) // 2]:,.2f}  max {srt[-1]:,.2f}  "
                 f"(punti <= 0: {sum(e <= 0 for e in eq)})")  # fmt: skip
    L.append(f"   {'timestamp':<18}{'equity':>16}{'profit_loss':>14}{'profit_loss_pct':>17}")
    pl, pp = raw.get("profit_loss") or [], raw.get("profit_loss_pct") or []
    for i, t in enumerate(raw["timestamp"][:15]):
        g = lambda arr, i=i: "-" if i >= len(arr) or arr[i] is None else f"{arr[i]:,.4f}"
        L.append(f"   {t:<18}{g(raw['equity']):>16}{g(pl):>14}{g(pp):>17}")
    if len(raw["timestamp"]) > 15:
        L.append(f"   ... altri {len(raw['timestamp']) - 15} punti")
    return "\n".join(L) + "\n"


def _demo_brokers():
    """Due conti simulati con strategie e storici diversi, per provare lo script senza chiavi."""
    from broker import DemoBroker

    syms = list(data.load_universe().ticker)
    bars = data.demo_bars([*syms, data.BENCHMARK])
    prices = {k: float(v["close"].iloc[-1]) for k, v in bars.items()}
    days = pd.bdate_range(end=pd.Timestamp(datetime.now(UTC).date()), periods=15)
    out = []
    for i, (lo, hi) in enumerate(((0, 25), (12, 40))):
        br = DemoBroker(1_000_000.0, prices)
        for s in syms[lo:hi]:
            br.pos[s] = {
                "qty": float(int(35_000 / prices[s]) or 1),
                "avg_entry": prices[s] * (0.99 if i == 0 else 1.01),
            }
        br.cash -= sum(p["qty"] * p["avg_entry"] for p in br.pos.values())
        rng = np.random.default_rng(i + 1)
        eq = 1_000_000 * np.cumprod(1 + rng.normal(0.0004 * (i + 1), 0.006, len(days)))
        br.history = [(str(d.date()), float(e)) for d, e in zip(days, eq, strict=True)]
        out.append(br)
    return tuple(out)


if __name__ == "__main__":
    raise SystemExit(main())
