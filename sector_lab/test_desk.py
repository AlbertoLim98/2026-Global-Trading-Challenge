import json
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import broker as broker_mod
import data
import desk as desk_mod
import journal as journal_mod
import portfolio
import server

P = portfolio.Params()


def test_allocate_caps_and_leftover():
    a = portfolio.allocate({"a": 5, "b": 1, "c": 1}, 100.0, 40.0)
    assert a["a"] == 40 and a["b"] == pytest.approx(30) and sum(a.values()) == pytest.approx(100)
    assert (
        sum(portfolio.allocate({"a": 1, "b": 1}, 100.0, 30.0).values()) == 60
    )  # il resto resta liquidità


def _sector(sym, trend, total):
    return {"symbol": sym, "name": sym, "trend_label": trend, "scores": {"total": total}}


def _stock(sym, score, price=100.0, trend="Rialzista", twrr=0.05, beta=1.2):
    return {
        "symbol": sym,
        "name": sym,
        "price": price,
        "trend_label": trend,
        "twrr_w": twrr,
        "beta_1y": beta,
        "scores": {"total": score},
        "strategy_weight": 0.1,
    }


def test_targets_skip_bearish_and_low_score_and_respect_caps():
    secs = [
        _sector("XLK", "Rialzista", 90),
        _sector("XLE", "Ribassista", 95),
        _sector("XLF", "Misto", 40),
    ]
    top = {
        "XLK": {
            "top": [_stock(f"K{i}", 80 - i) for i in range(10)]
            + [_stock("BAD", 99, trend="Ribassista")]
        }
    }
    t = portfolio.build_targets(secs, top, P)
    budgets = {s["symbol"]: s for s in t["sectors"]}
    assert (
        budgets["XLK"]["eligible"]
        and not budgets["XLE"]["eligible"]
        and not budgets["XLF"]["eligible"]
    )
    assert "BAD" not in t["targets"]  # trend relativo ribassista
    assert max(x["value"] for x in t["targets"].values()) <= P.max_stock * P.capital + 1e-6
    # un solo settore idoneo: tetto 25% del capitale, il resto resta liquidità
    assert sum(x["value"] for x in t["targets"].values()) <= P.max_sector * P.capital + 1e-6


def test_blocked_symbol_not_targeted():
    secs = [_sector("XLK", "Rialzista", 90)]
    top = {"XLK": {"top": [_stock("AAA", 80), _stock("BBB", 70)]}}
    assert "AAA" not in portfolio.build_targets(secs, top, P, blocked={"AAA"})["targets"]


def test_proposals_stop_exit_buy_and_unmanaged():
    managed = {s: {"name": s, "sector": "XLK"} for s in ("STP", "OUT", "NEW")}
    targets = {
        "NEW": {
            "symbol": "NEW",
            "name": "NEW",
            "sector": "XLK",
            "value": 20_000.0,
            "price": 50.0,
            "score": 80,
        }
    }
    pos = {
        "STP": {"qty": 100, "avg_entry": 100.0, "price": 97.0},  # perdita 3$ > ATR 2$
        "OUT": {"qty": 10, "avg_entry": 50.0, "price": 52.0},
        "ZZZ": {"qty": 5, "avg_entry": 10.0, "price": 9.0},  # fuori strategia
    }
    r = portfolio.build_proposals(
        targets, pos, {"NEW": 50.0}, {"STP": 2.0, "OUT": 1.0}, managed, 500_000.0, P
    )
    by = {(x["kind"], x["symbol"]): x for x in r["proposals"]}
    assert ("STOP", "STP") in by and by[("STOP", "STP")]["qty"] == 100 and ("SELL", "STP") not in by
    assert ("SELL", "OUT") in by and ("BUY", "NEW") in by and by[("BUY", "NEW")]["qty"] == 400
    assert not any(x["symbol"] == "ZZZ" for x in r["proposals"]) and any(
        "ZZZ" in n for n in r["notes"]
    )
    assert [x["priority"] for x in r["proposals"]] == sorted(x["priority"] for x in r["proposals"])


def test_stop_needs_loss_above_one_atr():
    assert portfolio.stop_hit({"avg_entry": 100, "price": 98.5}, 2.0, 1.0) is None
    assert portfolio.stop_hit({"avg_entry": 100, "price": 97.9}, 2.0, 1.0) == pytest.approx(2.1)
    assert portfolio.stop_hit({"avg_entry": 100, "price": 50}, None, 1.0) is None


def test_buys_scaled_when_cash_short_and_small_drift_ignored():
    managed = {"NEW": {"name": "N", "sector": "XLK"}, "OLD": {"name": "O", "sector": "XLK"}}
    targets = {
        "NEW": {
            "symbol": "NEW",
            "name": "N",
            "sector": "XLK",
            "value": 40_000.0,
            "price": 100.0,
            "score": 70,
        },
        "OLD": {
            "symbol": "OLD",
            "name": "O",
            "sector": "XLK",
            "value": 10_000.0,
            "price": 100.0,
            "score": 70,
        },
    }
    pos = {
        "OLD": {"qty": 95, "avg_entry": 100.0, "price": 100.0}
    }  # 9.5k vs 10k: scarto trascurabile
    r = portfolio.build_proposals(
        targets, pos, {}, {}, managed, P.cash_reserve * P.capital + 10_000, P
    )
    assert [x["symbol"] for x in r["proposals"]] == ["NEW"]
    assert r["proposals"][0]["qty"] == 100 and any(
        "Liquidità insufficiente" in n for n in r["notes"]
    )


def test_journal_is_append_only(tmp_path):
    j = journal_mod.Journal(tmp_path / "j.db")
    j.log("RUN", {"x": 1}, "r1")
    with pytest.raises(sqlite3.DatabaseError):
        j._db.execute("UPDATE events SET kind = 'X'")
    with pytest.raises(sqlite3.DatabaseError):
        j._db.execute("DELETE FROM events")
    assert len(j.events()) == 1


@pytest.fixture
def env(tmp_path):
    state = server.State(True, "iex")
    syms = [*data.load_universe().ticker, data.STOCK_BENCHMARK, *data.SECTORS, data.BENCHMARK]
    prices = {k: float(v["close"].iloc[-1]) for k, v in data.demo_bars(syms).items()}
    broker = broker_mod.DemoBroker(1_000_000.0, prices)
    desk = desk_mod.Desk(state, broker, journal_mod.Journal(tmp_path / "j.db"))
    return desk, broker, prices


def test_daily_run_then_decisions_are_journaled(env):
    desk, broker, _ = env
    res = desk.run("quality", 1_000_000)
    props = desk.open_proposals()
    assert res["n_targets"] > 0 and props and all(p["kind"] == "BUY" for p in props)
    kinds = {e["kind"] for e in desk.journal.events()}
    assert {"RUN", "PROPOSAL"} <= kinds
    run_evt = desk.journal.events(kind="RUN")[0]["payload"]
    assert (
        run_evt["sector_table"] and run_evt["stock_tables"] and run_evt["targets"]
    )  # tabelle registrate

    a, b, c = props[0], props[1], props[2]
    out = desk.decide(a["id"], "execute")
    assert out["status"] == "filled" and a["symbol"] in {p["symbol"] for p in broker.positions()}
    assert desk.decide(b["id"], "reject")["status"] == "rejected"
    sn = desk.decide(c["id"], "snooze")
    assert sn["status"] == "snoozed" and sn["snooze_until"]
    with pytest.raises(desk_mod.DeskError):
        desk.decide(a["id"], "execute")  # già eseguita: niente doppio ordine
    kinds = [e["kind"] for e in desk.journal.events(1000)]
    assert "ORDER" in kinds and kinds.count("DECISION") >= 3
    assert len(broker.orders) == 1
    left = {p["id"]: p for p in desk.open_proposals()}
    assert (
        c["id"] in left and left[c["id"]]["status"] == "snoozed" and left[c["id"]]["due"] is False
    )


def test_new_run_supersedes_open_proposals(env):
    desk, _, _ = env
    desk.run("quality", 1_000_000)
    first = {p["id"] for p in desk.open_proposals()}
    desk.run("beta", 1_000_000)
    now_open = {p["id"] for p in desk.open_proposals()}
    assert first and not (first & now_open)
    assert any(e["kind"] == "SUPERSEDED" for e in desk.journal.events(5000))


def test_stop_flow_sells_and_blocks_rebuy(env):
    desk, broker, prices = env
    sym = next(s for s in data.load_universe().ticker if s in prices)
    px = prices[sym]
    broker.pos[sym] = {"qty": 50.0, "avg_entry": px * 1.5}  # perdita enorme: oltre 1 ATR
    res = desk.stop_check()
    assert res["n_new"] == 1
    stop = next(p for p in desk.open_proposals() if p["kind"] == "STOP")
    assert stop["symbol"] == sym and stop["priority"] == 0
    assert desk.stop_check()["n_new"] == 0  # nessun duplicato
    assert desk.decide(stop["id"], "execute")["status"] == "filled"
    assert sym not in {p["symbol"] for p in broker.positions()}
    assert sym in desk.journal.recent_stop_symbols(5)
    desk.run("quality", 1_000_000)
    assert not any(p["symbol"] == sym for p in desk.open_proposals())  # cooldown dopo lo stop


def test_execute_after_position_gone_is_voided(env):
    desk, broker, prices = env
    sym = next(iter(data.load_universe().ticker))
    broker.pos[sym] = {"qty": 10.0, "avg_entry": prices[sym] * 2}
    desk.stop_check()
    stop = next(p for p in desk.open_proposals() if p["kind"] == "STOP")
    broker.pos.clear()
    with pytest.raises(desk_mod.DeskError):
        desk.decide(stop["id"], "execute")
    assert desk.journal.get_proposal(stop["id"])["status"] == "void"


def test_broker_error_is_journaled_and_proposal_stays_open(env):
    desk, broker, _ = env
    desk.run("quality", 1_000_000)
    p = desk.open_proposals()[0]
    broker.cash = 0  # il broker rifiuterà l'acquisto
    with pytest.raises(desk_mod.DeskError):
        desk.decide(p["id"], "execute")
    assert desk.journal.get_proposal(p["id"])["status"] == "pending"
    assert any(e["kind"] == "ERROR" for e in desk.journal.events(50))


def test_server_rejects_cross_site_posts_and_foreign_hosts(env):
    desk, _, _ = env
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(desk.state, desk, 0))
    port = srv.server_address[1]
    srv = None  # ricrea con la porta reale per l'elenco degli host ammessi
    srv = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(desk.state, desk, port))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"

    def call(path, method="GET", headers=None, body=b"{}"):
        req = urllib.request.Request(
            url + path,
            data=body if method == "POST" else None,
            method=method,
            headers=headers or {},
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    try:
        assert call("/api/proposal/decide", "POST")[0] == 403  # senza intestazione
        assert (
            call("/api/proposal/decide", "POST", {"X-Sector-Lab": "1", "Host": "evil.example"})[0]
            == 403
        )
        assert call("/api/account", headers={"Host": "evil.example"})[0] == 403
        assert (
            call(
                "/api/proposal/decide",
                "POST",
                {"X-Sector-Lab": "1"},
                b'{"id": "x", "action": "execute"}',
            )[0]
            == 400
        )
        assert call("/api/account")[0] == 200
    finally:
        srv.shutdown()


def test_broker_exception_during_submit_is_journaled(env, monkeypatch):
    desk, broker, _ = env
    desk.run("quality", 1_000_000)
    p = desk.open_proposals()[0]

    def boom(*a, **k):
        raise RuntimeError("rete caduta")

    monkeypatch.setattr(broker, "submit_market", boom)
    with pytest.raises(desk_mod.DeskError, match="rete caduta"):
        desk.decide(p["id"], "execute")
    assert desk.journal.get_proposal(p["id"])["status"] == "pending"
    errs = [e["payload"]["message"] for e in desk.journal.events(50) if e["kind"] == "ERROR"]
    assert any("rete caduta" in m for m in errs)
