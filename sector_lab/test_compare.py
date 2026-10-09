import io
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).parent))
import compare_portfolios as cp
import journal as journal_mod


def _res():
    ba, bb = cp._demo_brokers()
    a, b = cp.snapshot(ba, "Portafoglio 17", "1M"), cp.snapshot(bb, "Portafoglio 18", "1M")
    return cp.compare(a, b), a, b


def test_read_env_ignores_comments_quotes_and_bom(tmp_path):
    f = tmp_path / ".env.x"
    f.write_text(
        '﻿# commento\nALPACA_API_KEY="abc"\nALPACA_SECRET_KEY = s3cr3t \n\nALTRO=1\n',
        encoding="utf-8",
    )
    assert cp.read_env(f)["ALPACA_API_KEY"] == "abc" and cp.credentials(f) == ("abc", "s3cr3t")
    g = tmp_path / ".env.vuoto"
    g.write_text("ALPACA_API_KEY=solo\n")
    with pytest.raises(SystemExit):
        cp.credentials(g)


def test_overlap_sectors_and_weights_are_consistent():
    res, a, b = _res()
    o = res["overlap"]
    wa = {p["symbol"]: p["weight"] for p in a["positions"]}
    wb = {p["symbol"]: p["weight"] for p in b["positions"]}
    assert (
        o["n_a"] == len(wa) and o["n_b"] == len(wb) and o["n_common"] == len(set(wa) & set(wb)) > 0
    )
    assert o["weight_overlap"] == pytest.approx(
        sum(min(wa.get(s, 0), wb.get(s, 0)) for s in set(wa) | set(wb))
    )
    assert o["jaccard"] == pytest.approx(o["n_common"] / len(set(wa) | set(wb)))
    assert sum(d["w_a"] for d in res["sectors"]) == pytest.approx(sum(wa.values()))
    assert sum(d["w_b"] for d in res["sectors"]) == pytest.approx(sum(wb.values()))
    assert all(r["diff"] == pytest.approx(r["w_a"] - r["w_b"]) for r in res["positions"])
    assert {r["in"] for r in res["positions"]} == {"entrambi", "Portafoglio 17", "Portafoglio 18"}


def test_window_metrics_use_only_common_days_and_curve_is_base_100():
    res, a, _ = _res()
    w = res["window"]
    s = a["history"]
    assert w["a"]["total_return"] == pytest.approx(s.iloc[-1] / s.iloc[0] - 1)
    assert w["a"]["max_drawdown"] <= 0 and w["a"]["worst_day"] <= w["a"]["best_day"]
    cur = w["curve"]
    assert list(cur.columns)[:2] == ["Portafoglio 17", "Portafoglio 18"] and cur.iloc[0].tolist()[
        :2
    ] == [100.0, 100.0]


def test_metrics_edge_cases():
    import pandas as pd

    m = cp.series_metrics(pd.Series([100.0]))
    assert m["total_return"] is None and m["n_days"] == 1
    m = cp.series_metrics(pd.Series([100.0, 110.0, 99.0, 120.0]))
    assert m["total_return"] == pytest.approx(0.2) and m["max_drawdown"] == pytest.approx(
        99 / 110 - 1
    )


def test_report_and_excel_contain_both_portfolios(tmp_path):
    res, _, _ = _res()
    ja = {"strategy": "beta", "n_runs": 3, "trade_failed": 1}
    jb = {"strategy": "quality", "n_runs": 2, "trade_failed": 0}
    txt = cp.report(res, ja, jb)
    assert (
        "Portafoglio 17" in txt
        and "Portafoglio 18" in txt
        and "Alto beta" in txt
        and "Qualità" in txt
    )
    assert "Titoli in comune" in txt and "Finestra comune" in txt
    out = tmp_path / "c.xlsx"
    cp.to_excel(res, ja, jb, out)
    wb = load_workbook(out)
    assert wb.sheetnames == ["Riepilogo", "Posizioni", "Settori", "Andamento (base 100)", "Note"]
    rows = {r[0]: r for r in wb["Riepilogo"].iter_rows(min_row=3, values_only=True)}
    assert rows["Strategia (journal)"][1:3] == ("Alto beta", "Qualità") and rows[
        "Ribilanciamenti (journal)"
    ][1:3] == (3, 2)
    assert wb["Posizioni"].max_row > 20


def test_journal_summary_reads_strategy_read_only(tmp_path):
    path = tmp_path / "j.db"
    j = journal_mod.Journal(path, "Portafoglio 18", "quality")
    j.log("RUN", {"params": {"mode": "quality"}, "account": {"equity": 1_000_000.0}}, "r1")
    j.log("TRADE_FAILED", {"symbol": "A", "reason": "x"}, "r1")
    before = path.read_bytes()
    s = cp.journal_summary(path)
    assert s["n_runs"] == 1 and s["strategy"] == "quality" and s["portfolio"] == "Portafoglio 18"
    assert s["trade_failed"] == 1 and s["first_equity"] == 1_000_000.0
    assert path.read_bytes() == before  # sola lettura
    assert cp.journal_summary(tmp_path / "manca.db") == {} and cp.journal_summary(None) == {}


def test_cli_demo_writes_the_excel_and_requires_two_envs(tmp_path, capsys):
    out = tmp_path / "out.xlsx"
    assert cp.main(["--demo", "--out", str(out), "--name-a", "A", "--name-b", "B"]) == 0
    assert out.exists() and "Excel scritto" in capsys.readouterr().out
    assert load_workbook(io.BytesIO(out.read_bytes())).sheetnames[0] == "Riepilogo"
    with pytest.raises(SystemExit):
        cp.main([])  # senza --demo servono entrambi i file di chiavi


def test_window_can_start_from_the_most_recent_portfolio_and_curve_restarts_at_100():
    import pandas as pd

    ba, bb = cp._demo_brokers()
    a, b = cp.snapshot(ba, "A", "1M"), cp.snapshot(bb, "B", "1M")
    start = a["history"].index[8]
    res = cp.compare(a, b, start=start, own_starts=(start, None))
    w = res["window"]
    assert w["from"] == cp._fmt_ts(start) and w["a"]["n_points"] == len(a["history"]) - 8
    sa = a["history"].loc[start:]
    assert w["a"]["total_return"] == pytest.approx(sa.iloc[-1] / sa.iloc[0] - 1)
    assert w["curve"].iloc[0].tolist()[:2] == [100.0, 100.0]
    assert res["own"]["a"]["n_points"] == len(sa) and res["own"]["b"]["n_points"] == len(
        b["history"]
    )
    assert cp.compare(a, b, start=a["history"].index[-1] + pd.Timedelta(days=5))["window"] == {}


def test_volatility_is_annualized_with_the_bars_per_year_of_the_timeframe():
    import pandas as pd

    s = pd.Series([100.0, 100.5, 100.1, 100.8, 100.4, 101.0])
    daily = cp.series_metrics(s, 252)["vol"]
    m15 = cp.series_metrics(s, 252 * cp.BARS_PER_DAY["15Min"])["vol"]
    assert m15 == pytest.approx(daily * cp.BARS_PER_DAY["15Min"] ** 0.5)
    assert cp.BARS_PER_DAY["1D"] == 1 and cp.MINUTES["1H"] == 60


def test_timestamp_formatting_for_daily_and_intraday_points():
    import pandas as pd

    assert cp._fmt_ts(pd.Timestamp("2026-10-09")) == "2026-10-09"
    assert cp._fmt_ts(pd.Timestamp("2026-10-09 14:30")) == "2026-10-09 14:30"


def test_journal_summary_reports_the_first_filled_order(tmp_path):
    j = journal_mod.Journal(tmp_path / "f.db", "P", "short")
    j.log("RUN", {"params": {"mode": "short"}, "account": {"equity": 1.0}}, "r1")
    for pid, status in (("p1", "failed"), ("p2", "filled")):
        j.add_proposal("r1", {"id": pid, "kind": "BUY", "side": "buy", "symbol": "A", "name": "a", "sector": "XLK",
                              "qty": 1, "price": 1.0, "value": 1.0, "reason": "r", "priority": 2})  # fmt: skip
        j.set_status(pid, status, "DECISION_DONE")
    s = cp.journal_summary(tmp_path / "f.db")
    assert s["first_fill"] and s["first_fill"].endswith("Z")
    assert cp.journal_summary(None) == {}


def test_alpaca_history_is_dated_for_1D_and_timestamped_for_intraday():
    from types import SimpleNamespace

    from broker import AlpacaBroker

    ts_day = 1_791_504_000  # 2026-10-09 00:00 UTC
    fake = SimpleNamespace(
        get_portfolio_history=lambda req: SimpleNamespace(
            timestamp=[ts_day, ts_day + 52_200, ts_day + 53_100],
            equity=[1_000_000.0, None, 1_001_000.0],
        )
    )
    br = AlpacaBroker.__new__(AlpacaBroker)
    br._t = fake
    assert br.portfolio_history("1M", "1D") == [
        ("2026-10-09", 1_000_000.0),
        ("2026-10-09", 1_001_000.0),
    ]
    assert br.portfolio_history("1W", "15Min") == [
        ("2026-10-09T00:00", 1_000_000.0),
        ("2026-10-09T14:45", 1_001_000.0),
    ]


def test_cli_from_option_shortens_the_window(tmp_path, capsys):
    out = tmp_path / "o.xlsx"
    assert cp.main(["--demo", "--out", str(out)]) == 0
    full = capsys.readouterr().out
    day = next(ln for ln in full.splitlines() if ln.startswith("Finestra comune")).split()[3]
    import datetime as dt

    later = (dt.date.fromisoformat(day) + dt.timedelta(days=10)).isoformat()
    assert cp.main(["--demo", "--out", str(out), "--from", later]) == 0
    short = capsys.readouterr().out
    assert "Finestra di confronto dal" in short and f"dal {later}" in short.replace(
        "Finestra comune: ", ""
    )
    n_full = int(full.split("rilevazioni")[0].rsplit("(", 1)[1])
    n_short = int(short.split("rilevazioni")[0].rsplit("(", 1)[1])
    assert n_short < n_full


def test_clean_equity_drops_impossible_points_but_keeps_real_moves():
    import pandas as pd

    idx = pd.date_range("2026-10-09 14:30", periods=8, freq="15min")
    s = pd.Series(
        [1_000_000, 1_001_000, -5_200_000, 1_002_000, 90_000, 1_000_500, 999_000, 1_003_000.0],
        index=idx,
    )
    clean, dropped = cp.clean_equity(s)
    assert dropped == [-5_200_000.0, 90_000.0] and len(clean) == 6
    assert clean.min() > 900_000 and clean.index.is_monotonic_increasing
    ok, none = cp.clean_equity(s.drop(s.index[[2, 4]]))
    assert none == [] and len(ok) == 6


def test_glitchy_history_does_not_produce_absurd_returns_and_is_reported():
    import pandas as pd

    ba, bb = cp._demo_brokers()
    bad = list(bb.history)
    bad[5] = (bad[5][0], -7_300_000.0)  # punto impossibile come quello visto su Alpaca
    bad[9] = (bad[9][0], 120_000.0)
    bb.history = bad
    a, b = cp.snapshot(ba, "A", "1M"), cp.snapshot(bb, "B", "1M")
    assert len(b["dropped"]) == 2 and a["dropped"] == []
    res = cp.compare(a, b)
    m = res["window"]["b"]
    assert abs(m["total_return"]) < 0.2 and m["max_drawdown"] > -0.2 and (m["vol"] or 0) < 1
    txt = cp.report(res)
    assert "ATTENZIONE: scartati 2 punti anomali dallo storico di B" in txt
    assert isinstance(pd.Series(b["history"]).min(), float) and b["history"].min() > 0


# --- curva del patrimonio campionata dal programma (indipendente dallo storico di Alpaca) ----------------
def _curve_journal(path, name, start, points, step=5, base=1_000_000.0, drift=10.0):
    """Journal con una curva del patrimonio già campionata (un punto ogni `step` minuti)."""
    import pandas as pd

    j = journal_mod.Journal(path, name, "beta")
    with j._db:
        for k in range(points):
            ts = (start + pd.Timedelta(minutes=step * k)).strftime("%Y-%m-%dT%H:%M:%SZ")
            j._db.execute(
                "INSERT INTO equity_curve (ts, equity, cash, n_positions, portfolio, strategy) VALUES (?,?,?,?,?,?)",
                (ts, base + drift * k, 1.0, 5, name, "beta"),
            )
    return path


def test_equity_curve_is_logged_append_only_and_read_back(tmp_path):
    import sqlite3

    j = journal_mod.Journal(tmp_path / "c.db", "P", "short")
    j.log_equity(1_000_000.0, 30_000.0, 20)
    j.log_equity(1_001_500.0, 29_000.0, 20)
    curve = j.equity_curve()
    assert [c["equity"] for c in curve] == [1_000_000.0, 1_001_500.0] and curve[0][
        "portfolio"
    ] == "P"
    with pytest.raises(sqlite3.DatabaseError):
        j._db.execute("UPDATE equity_curve SET equity = 1")
    with pytest.raises(sqlite3.DatabaseError):
        j._db.execute("DELETE FROM equity_curve")


def test_desk_snapshots_equity_before_and_after_a_rebalance_and_survives_a_dead_broker(tmp_path):
    import broker as broker_mod
    import data
    import desk as desk_mod
    import server

    state = server.State(True, "iex")
    syms = [*data.load_universe().ticker, data.STOCK_BENCHMARK, *data.SECTORS, data.BENCHMARK]
    prices = {k: float(v["close"].iloc[-1]) for k, v in data.demo_bars(syms).items()}
    br = broker_mod.DemoBroker(1_000_000.0, prices)
    d = desk_mod.Desk(
        state, br, journal_mod.Journal(tmp_path / "e.db", "P", "quality"), settle_seconds=0
    )
    d.run("quality", 1_000_000)
    curve = d.journal.equity_curve()
    assert (
        len(curve) == 2
        and curve[0]["equity"] == pytest.approx(1_000_000.0)
        and curve[1]["n_positions"] > 0
    )
    assert (
        d.snapshot_equity() == pytest.approx(br.account()["equity"])
        and len(d.journal.equity_curve()) == 3
    )

    def boom():
        raise RuntimeError("rete")

    br.account = boom
    assert d.snapshot_equity() is None and len(d.journal.equity_curve()) == 3


def test_journal_curve_is_read_resampled_and_preferred_when_it_has_enough_points(tmp_path):
    import pandas as pd

    t0 = pd.Timestamp("2026-10-09 14:23")
    a = _curve_journal(tmp_path / "a.db", "A", t0, 40, 5, drift=20.0)
    b = _curve_journal(tmp_path / "b.db", "B", t0, 40, 5, drift=-5.0)
    raw = cp.journal_curve(a)
    assert (
        len(raw) == 40 and raw.index[0] == t0 and raw.iloc[-1] == pytest.approx(1_000_000 + 20 * 39)
    )
    r15 = cp.resample_curve(raw, "15Min")
    assert r15.index.freqstr in ("15min", "15T") or len(r15) < len(raw)
    assert (r15.index.minute % 15 == 0).all() and cp.resample_curve(
        pd.Series(dtype=float), "15Min"
    ).empty
    ba, bb = cp._demo_brokers()
    sa, sb = cp.snapshot(ba, "A", "1M"), cp.snapshot(bb, "B", "1M")
    assert cp._choose_history(sa, sb, [a], [b], "auto", "15Min", t0) == "journal"
    assert sa["history"].index.equals(sb["history"].index) and sa["dropped"] == []
    res = cp.compare(sa, sb, start=t0, ppy=252 * 26)
    assert (
        res["window"]["a"]["total_return"] > 0 > res["window"]["b"]["total_return"]
    )  # le curve vere


def test_alpaca_history_is_used_when_the_journal_has_too_few_points_or_when_forced(
    tmp_path, capsys
):
    import pandas as pd

    t0 = pd.Timestamp("2026-10-09 14:23")
    few = _curve_journal(tmp_path / "f.db", "F", t0, 2)
    many = _curve_journal(tmp_path / "m.db", "M", t0, 40)
    ba, bb = cp._demo_brokers()
    sa, sb = cp.snapshot(ba, "A", "1M"), cp.snapshot(bb, "B", "1M")
    assert cp._choose_history(sa, sb, [many], [few], "auto", "15Min", t0) == "alpaca"
    assert "da Alpaca" in capsys.readouterr().out
    assert cp._choose_history(sa, sb, [many], [many], "alpaca", "15Min", t0) == "alpaca"
    assert (
        cp._choose_history(sa, sb, [many], [few], "journal", "15Min", t0) == "alpaca"
    )  # avviso su stderr
    assert "insufficiente" in capsys.readouterr().err
    assert cp._choose_history(sa, sb, None, None, "auto", "1D", None) == "alpaca"
    assert cp.journal_curve(tmp_path / "manca.db").empty and cp.journal_curve(None).empty


def test_raw_history_report_shows_what_alpaca_returned(capsys):
    raw = {
        "timestamp": [f"2026-10-09T14:{m:02d}" for m in range(0, 40, 2)],
        "equity": [1_000_000.0] * 18 + [-1857.0, None],
        "profit_loss": [0.0] * 20,
        "profit_loss_pct": [0.0] * 20,
        "base_value": 1_000_000.0,
    }
    txt = cp._raw_report("1g", raw)
    assert (
        "20 punti" in txt
        and "punti <= 0: 1" in txt
        and "altri 5 punti" in txt
        and "base_value 1000000.0" in txt
    )


def test_cli_debug_history_prints_the_raw_series(tmp_path, capsys):
    assert cp.main(["--demo", "--debug-history", "--out", str(tmp_path / "d.xlsx")]) == 0
    out = capsys.readouterr().out
    assert out.count("== Storico grezzo di") == 2 and "profit_loss_pct" in out


def test_cli_record_appends_the_current_equity_to_the_journals(tmp_path, capsys):
    ja, jb = tmp_path / "a.db", tmp_path / "b.db"
    journal_mod.Journal(ja, "A", "beta")
    journal_mod.Journal(jb, "B", "short")
    argv = [
        "--demo",
        "--record",
        "--journal-a",
        str(ja),
        "--journal-b",
        str(jb),
        "--out",
        str(tmp_path / "o.xlsx"),
    ]
    assert cp.main(argv) == 0 and cp.main(argv) == 0
    assert (
        len(journal_mod.Journal(ja).equity_curve()) == 2
        and len(journal_mod.Journal(jb).equity_curve()) == 2
    )
    assert "registrato in" in capsys.readouterr().out
