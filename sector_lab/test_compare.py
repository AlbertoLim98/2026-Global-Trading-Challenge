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
