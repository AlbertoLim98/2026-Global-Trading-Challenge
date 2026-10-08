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
    # nessun tetto per settore: con un solo settore idoneo e 10 titoli si investe fino al 97%
    assert sum(x["value"] for x in t["targets"].values()) == pytest.approx(
        P.capital * (1 - P.cash_reserve)
    )
    assert P.max_stock == 0.10 and not hasattr(P, "max_sector")


def test_two_sectors_only_every_stock_capped_at_10_percent_of_portfolio():
    secs = [_sector("XLK", "Rialzista", 90), _sector("XLF", "Rialzista", 70)] + [
        _sector(e, "Ribassista", 80) for e in ("XLE", "XLV")
    ]
    top = {
        "XLK": {"top": [_stock(f"K{i}", 90 - i) for i in range(10)]},
        "XLF": {"top": [_stock(f"F{i}", 70 - i) for i in range(10)]},
    }
    t = portfolio.build_targets(secs, top, P)
    vals = [x["value"] for x in t["targets"].values()]
    assert len(vals) == 20 and max(vals) <= 0.10 * P.capital + 1e-6
    assert sum(vals) == pytest.approx(P.capital * (1 - P.cash_reserve))
    by = {x["symbol"]: x for x in t["sectors"]}
    assert by["XLK"]["budget"] > by["XLF"]["budget"] > 0 and by["XLE"]["budget"] == 0


def test_always_invests_97_percent_by_adding_more_sectors():
    # un solo settore idoneo con 4 titoli (max 40%): servono altri settori per arrivare al 97%
    secs = [
        _sector("XLK", "Rialzista", 90),
        _sector("XLF", "Misto", 40),  # sotto soglia punteggio
        _sector("XLE", "Misto", 35),
        _sector("XLV", "Ribassista", 80),
    ]
    top = {
        "XLK": {"top": [_stock(f"K{i}", 90 - i) for i in range(4)]},
        "XLF": {"top": [_stock(f"F{i}", 60 - i) for i in range(4)]},
        "XLE": {"top": [_stock(f"E{i}", 50 - i) for i in range(4)]},
        "XLV": {"top": [_stock(f"V{i}", 70 - i) for i in range(4)]},
    }
    t = portfolio.build_targets(secs, top, P)
    vals = [x["value"] for x in t["targets"].values()]
    assert sum(vals) == pytest.approx(P.capital * 0.97) and max(vals) <= 0.10 * P.capital + 1e-6
    by = {x["symbol"]: x for x in t["sectors"]}
    assert by["XLK"]["budget"] == pytest.approx(0.40 * P.capital)  # il settore idoneo è al massimo
    assert by["XLF"]["budget"] > 0 and by["XLE"]["budget"] > 0  # aggiunti (non ribassisti) prima...
    assert by["XLV"]["budget"] == 0  # ...del settore ribassista, non necessario
    assert any("sono stati aggiunti" in n for n in t["notes"])


def test_bearish_sector_used_only_when_still_short():
    secs = [_sector("XLK", "Rialzista", 90), _sector("XLV", "Ribassista", 80)]
    top = {
        "XLK": {"top": [_stock(f"K{i}", 90 - i) for i in range(6)]},
        "XLV": {"top": [_stock(f"V{i}", 70 - i) for i in range(6)]},
    }
    t = portfolio.build_targets(secs, top, P)
    assert sum(x["value"] for x in t["targets"].values()) == pytest.approx(P.capital * 0.97)
    assert {x["sector"] for x in t["targets"].values()} == {"XLK", "XLV"}
    assert "ribassista" in " ".join(t["notes"])


def test_relaxed_filters_as_last_resort_to_reach_97_percent():
    secs = [_sector("XLK", "Rialzista", 90)]
    stocks_ = [_stock(f"K{i}", 90 - i) for i in range(5)]
    stocks_ += [_stock(f"W{i}", 40 - i, trend="Ribassista") for i in range(6)]  # non idonei
    t = portfolio.build_targets(secs, {"XLK": {"top": stocks_}}, P)
    assert sum(x["value"] for x in t["targets"].values()) == pytest.approx(P.capital * 0.97)
    assert max(x["value"] for x in t["targets"].values()) <= 0.10 * P.capital + 1e-6
    assert len(t["targets"]) == 10 and any(k.startswith("W") for k in t["targets"])


def test_cash_exceeds_3_percent_only_when_titles_do_not_exist_at_all():
    secs = [_sector("XLK", "Rialzista", 90)]
    top = {"XLK": {"top": [_stock(f"K{i}", 80) for i in range(3)]}}
    t = portfolio.build_targets(secs, top, P)
    assert all(x["value"] == pytest.approx(0.10 * P.capital) for x in t["targets"].values())
    assert sum(x["value"] for x in t["targets"].values()) == pytest.approx(0.30 * P.capital)
    assert any("Non ci sono abbastanza titoli" in n for n in t["notes"])


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


def test_no_partial_sells_for_overweight_titles():
    managed = {"BIG": {"name": "B", "sector": "XLK"}}
    targets = {
        "BIG": {
            "symbol": "BIG",
            "name": "B",
            "sector": "XLK",
            "value": 10_000.0,
            "price": 100.0,
            "score": 70,
        }
    }
    pos = {"BIG": {"qty": 500, "avg_entry": 100.0, "price": 100.0}}  # 50k contro target 10k
    assert portfolio.build_proposals(targets, pos, {}, {}, managed, 100_000.0, P)["proposals"] == []
    assert P.cash_reserve == 0.03


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
    desk = desk_mod.Desk(state, broker, journal_mod.Journal(tmp_path / "j.db"), settle_seconds=0)
    return desk, broker, prices


def _statuses(desk):
    return [t["status"] for t in desk.trades()]


def test_run_sends_all_orders_automatically_and_journals_everything(env):
    desk, broker, _ = env
    res = desk.run("quality", 1_000_000)
    out = res["outcome"]
    assert out["total"] > 0 and out["filled"] == out["total"] and out["failed"] == 0
    assert len(broker.positions()) == out["total"] and len(broker.orders) == out["total"]
    events = desk.journal.events(10_000)
    kinds = [e["kind"] for e in events]
    assert kinds.count("DECISION") == out["total"] and kinds.count("ORDER") == out["total"]
    assert all(e["payload"]["action"] == "auto" for e in events if e["kind"] == "DECISION")
    run_evt = desk.journal.events(kind="RUN")[0]["payload"]
    assert run_evt["sector_table"] and run_evt["stock_tables"] and run_evt["targets"]
    # nessuna operazione resta da approvare: tutte eseguite
    assert set(_statuses(desk)) == {"filled"}


def test_second_daily_run_proposes_nothing_when_portfolio_is_in_line(env):
    desk, _, _ = env
    desk.run("quality", 1_000_000)
    assert desk.run("quality", 1_000_000)["outcome"]["total"] == 0


def test_sells_are_sent_before_buys(env):
    desk, broker, prices = env
    sym = next(s for s in data.load_universe().ticker if s not in ())
    # una posizione fuori target (qualsiasi titolo dell'universo non selezionato) viene venduta per prima
    held = [s for s in prices if s in desk.managed][:1][0]
    broker.pos[held] = {"qty": 10.0, "avg_entry": prices[held]}
    desk.run("quality", 1_000_000)
    sent = [e["payload"] for e in reversed(desk.journal.events(10_000)) if e["kind"] == "DECISION"]
    sides = [x["side"] for x in sent]
    assert "sell" in sides and sides.index("buy") > max(
        i for i, v in enumerate(sides) if v == "sell"
    )
    assert sym


def test_stop_check_sells_automatically(env):
    desk, broker, prices = env
    sym = next(s for s in data.load_universe().ticker if s in prices)
    broker.pos[sym] = {"qty": 50.0, "avg_entry": prices[sym] * 1.5}  # perdita enorme: oltre 1 ATR
    res = desk.stop_check()
    assert res["n_new"] == 1 and res["outcome"]["filled"] == 1
    assert sym not in {p["symbol"] for p in broker.positions()}
    stop = next(t for t in desk.trades() if t["kind"] == "STOP")
    assert stop["symbol"] == sym and stop["priority"] == 0 and stop["status"] == "filled"
    assert desk.stop_check()["n_new"] == 0


def test_failed_orders_are_reported_and_can_be_repositioned_at_current_price(env, monkeypatch):
    desk, broker, prices = env

    def boom(*a, **k):
        raise RuntimeError("rete caduta")

    monkeypatch.setattr(broker, "submit_market", boom)
    out = desk.run("quality", 1_000_000)["outcome"]
    assert out["total"] > 0 and out["failed"] == out["total"] and out["filled"] == 0
    failed = [t for t in desk.trades() if t["status"] == "failed"]
    assert all("rete caduta" in t["error"] for t in failed)
    kinds = [e["kind"] for e in desk.journal.events(10_000)]
    assert kinds.count("TRADE_FAILED") == len(failed)

    monkeypatch.undo()
    t = failed[0]
    prices[t["symbol"]] *= 1.03  # il prezzo si è mosso: si usa quello attuale
    r = desk.reposition(t["id"])
    assert r["status"] == "filled" and r["limit_price"] == pytest.approx(prices[t["symbol"]])
    assert desk.journal.get_proposal(t["id"])["status"] == "repositioned"
    new = desk.journal.get_proposal(r["new_id"])
    assert (
        new["parent_id"] == t["id"] and new["order_type"] == "limit" and new["status"] == "filled"
    )
    assert t["symbol"] in {p["symbol"] for p in broker.positions()}
    with pytest.raises(desk_mod.DeskError):
        desk.reposition(t["id"])  # già riposizionata: niente doppio ordine
    assert any(e["kind"] == "REPOSITION" for e in desk.journal.events(10_000))


def test_rejected_and_partially_filled_orders_are_failures(env, monkeypatch):
    desk, broker, _ = env
    calls = []

    def fake(symbol, side, qty, client_id):
        calls.append(qty)
        if len(calls) == 1:
            return {
                "order_id": "o1",
                "order_status": "rejected",
                "filled_qty": 0.0,
                "filled_avg_price": None,
            }
        return {
            "order_id": f"o{len(calls)}",
            "order_status": "expired",
            "filled_qty": qty // 2,
            "filled_avg_price": 10.0,
        }

    monkeypatch.setattr(broker, "submit_market", fake)
    desk.run("quality", 1_000_000)
    failed = {t["order_id"]: t for t in desk.trades() if t["status"] == "failed"}
    assert failed["o1"]["error"] == "ordine rejected" and failed["o1"]["remaining_qty"] == calls[0]
    part = failed["o2"]
    assert "eseguite" in part["error"] and part["remaining_qty"] == calls[1] - calls[1] // 2


def test_buying_power_shortage_is_a_reported_failure_not_a_crash(env, monkeypatch):
    desk, broker, _ = env
    orig = broker.account
    monkeypatch.setattr(broker, "account", lambda: {**orig(), "buying_power": 0.0})
    out = desk.run("quality", 1_000_000)["outcome"]
    assert out["failed"] > 0 and out["failed"] == out["total"]
    errs = [t["error"] for t in desk.trades() if t["status"] == "failed"]
    assert all("potere d'acquisto" in e for e in errs)


def test_scarce_cash_scales_buys_down_instead_of_failing(env):
    desk, broker, _ = env
    broker.cash = 100_000.0
    out = desk.run("quality", 1_000_000)["outcome"]
    assert out["failed"] == 0 and out["filled"] == out["total"]
    assert broker.account()["cash"] >= 0.03 * 1_000_000 - 1  # la riserva del 3% viene rispettata


def test_open_orders_block_a_new_run_until_resolved(env, monkeypatch):
    desk, broker, _ = env

    def queued(symbol, side, qty, client_id):
        return {
            "order_id": f"q-{client_id}",
            "order_status": "accepted",
            "filled_qty": 0.0,
            "filled_avg_price": None,
        }

    monkeypatch.setattr(broker, "submit_market", queued)
    assert desk.run("quality", 1_000_000)["outcome"]["in_progress"] > 0
    with pytest.raises(desk_mod.DeskError, match="ordini ancora in corso"):
        desk.run("quality", 1_000_000)
    monkeypatch.setattr(
        broker,
        "get_order",
        lambda oid: {
            "order_id": oid,
            "order_status": "filled",
            "filled_qty": 1.0,
            "filled_avg_price": 1.0,
        },
    )
    assert desk.refresh_orders() > 0 and "submitted" not in _statuses(desk)


def test_new_run_archives_old_failures(env, monkeypatch):
    desk, broker, _ = env

    def boom(*a, **k):
        raise RuntimeError("giù")

    monkeypatch.setattr(broker, "submit_market", boom)
    desk.run("quality", 1_000_000)
    old = {t["id"] for t in desk.trades() if t["status"] == "failed"}
    monkeypatch.undo()
    desk.run("quality", 1_000_000)
    assert old and all(desk.journal.get_proposal(i)["status"] == "superseded" for i in old)


def test_portfolio_view_composition_pl_and_todays_movements(env):
    desk, _, prices = env
    assert desk.portfolio()["opening"] is None and desk.portfolio()["since_open_pl"] is None
    desk.run("quality", 1_000_000)
    v = desk.portfolio()
    acct = v["account"]
    assert v["opening"]["equity"] == pytest.approx(1_000_000.0)
    assert v["since_open_pl"] == pytest.approx(acct["equity"] - 1_000_000.0)
    assert v["day_pl"] == pytest.approx(acct["equity"] - acct["last_equity"])
    assert sum(r["weight"] for r in v["positions"]) + v["cash_weight"] == pytest.approx(1.0)
    assert max(r["weight"] for r in v["positions"]) <= 0.10 + 1e-3  # nessun titolo oltre il 10%
    assert sum(x["weight"] for x in v["sectors"]) == pytest.approx(1 - v["cash_weight"])
    assert len(v["movements"]) == len(v["positions"]) and all(
        m["side"] == "buy" for m in v["movements"]
    )
    assert v["movements"][0]["time"] >= v["movements"][-1]["time"]  # dal più recente
    sym = v["positions"][0]["symbol"]
    prices[sym] *= 1.1  # il prezzo sale: P/L non realizzato positivo
    assert next(r for r in desk.portfolio()["positions"] if r["symbol"] == sym)["pl"] > 0


def test_reposition_only_for_failed(env):
    desk, _, _ = env
    desk.run("quality", 1_000_000)
    ok = desk.trades()[0]
    with pytest.raises(desk_mod.DeskError):
        desk.reposition(ok["id"])
    with pytest.raises(desk_mod.DeskError):
        desk.reposition("inesistente")


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
        assert call("/api/trade/reposition", "POST")[0] == 403  # senza intestazione
        assert (
            call("/api/trade/reposition", "POST", {"X-Sector-Lab": "1", "Host": "evil.example"})[0]
            == 403
        )
        assert call("/api/account", headers={"Host": "evil.example"})[0] == 403
        assert (
            call(
                "/api/trade/reposition",
                "POST",
                {"X-Sector-Lab": "1"},
                b'{"id": "x", "action": "execute"}',
            )[0]
            == 400
        )
        assert call("/api/account")[0] == 200
    finally:
        srv.shutdown()
