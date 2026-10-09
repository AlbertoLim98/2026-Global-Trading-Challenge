import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import journal as journal_mod
import journal_tool
import server

A, B = "PA3AAAAAAAAA", "PA3BBBBBBBBB"


def _run(j, run_id, acct, mode, n_prop=2):
    j.log("RUN", {"params": {"mode": mode}, "account": {"account_number": acct, "equity": 1.0},
                  "portfolio": j.portfolio, "strategy": j.strategy}, run_id)  # fmt: skip
    for i in range(n_prop):
        pid = f"{run_id}-p{i}"
        j.add_proposal(run_id, {"id": pid, "kind": "BUY", "side": "buy", "symbol": f"S{i}", "name": "n", "sector": "XLK",
                                "qty": 1, "price": 1.0, "value": 1.0, "reason": "r", "priority": 2,
                                "portfolio": j.portfolio, "strategy": j.strategy})  # fmt: skip
    j.log_indicators(
        run_id,
        [
            {
                "kind": "stock",
                "symbol": "S0",
                "selected": True,
                "target_value": 1.0,
                "data": {"x": 1},
            }
        ],
    )


def test_journal_is_bound_to_its_account_and_refuses_another_one(tmp_path):
    j = journal_mod.Journal(tmp_path / "j.db")
    j.bind_account(A)
    j.bind_account(A)  # stesso conto: va bene
    with pytest.raises(journal_mod.JournalAccountError, match=B):
        journal_mod.Journal(tmp_path / "j.db").bind_account(B)
    assert journal_mod.peek_account(tmp_path / "j.db") == A
    j.bind_account(None)  # senza numero del conto non si controlla nulla


def test_old_journal_without_meta_takes_the_account_of_its_last_run(tmp_path):
    j = journal_mod.Journal(tmp_path / "old.db")
    _run(j, "r1", A, "beta")
    _run(j, "r2", B, "quality")  # journal già misto: l'ultimo conto è B
    assert journal_mod.peek_account(tmp_path / "old.db") == B
    with pytest.raises(journal_mod.JournalAccountError):
        journal_mod.Journal(tmp_path / "old.db").bind_account(A)
    journal_mod.Journal(tmp_path / "old.db").bind_account(B)


def test_default_journal_follows_the_account_instead_of_mixing(tmp_path):
    here = tmp_path
    p, note = server.choose_journal_path(here, None, None, None, A)
    assert p.name == "journal.db" and note is None  # primo uso: file generico
    j = journal_mod.Journal(p)
    _run(j, "r1", A, "beta")
    assert (
        server.choose_journal_path(here, None, None, None, A)[0].name == "journal.db"
    )  # stesso conto
    p2, note = server.choose_journal_path(here, None, None, None, B)  # chiavi cambiate nel .env
    assert p2.name == f"journal_{B}.db" and A in note and B in note
    assert (
        server.choose_journal_path(here, "Portafoglio 18", None, None, B)[0].name
        == "journal_portafoglio_18.db"
    )
    assert (
        server.choose_journal_path(here, None, str(tmp_path / "x.db"), None, B)[0]
        == tmp_path / "x.db"
    )
    assert (
        server.choose_journal_path(here, None, None, str(tmp_path / "e.db"), B)[0]
        == tmp_path / "e.db"
    )


def test_split_separates_two_portfolios_without_touching_the_original(tmp_path):
    src = tmp_path / "mixed.db"
    j = journal_mod.Journal(src)
    _run(j, "r17", A, "beta", 3)
    _run(j, "r18", B, "quality", 2)
    j.log("ERROR", {"message": "senza esecuzione"})
    j.relabel(["r17"], "Portafoglio 17", "beta", "prova")
    before = src.read_bytes()

    dst = tmp_path / "p18.db"
    res = journal_tool.split(src, dst, ["r18"], "Portafoglio 18", "quality")
    assert res["proposals"] == 2 and res["indicators"] == 1 and res["account"] == B
    assert src.read_bytes() == before  # originale intatto

    new = journal_mod.Journal(dst)
    ev = new.events(1000)
    assert {e["run_id"] for e in ev} == {"r18"}
    assert all(e["portfolio"] == "Portafoglio 18" and e["strategy"] == "quality" for e in ev)
    assert {p["run_id"] for p in new.proposals()} == {"r18"} and len(new.proposals()) == 2
    assert all(
        p["portfolio"] == "Portafoglio 18" and p["strategy"] == "quality" for p in new.proposals()
    )
    run = next(e for e in ev if e["kind"] == "RUN")["payload"]
    assert run["portfolio"] == "Portafoglio 18" and run["strategy"] == "quality"
    assert [r["symbol"] for r in new.indicators() if r["run_id"] == "r18"] == ["S0"]
    assert journal_mod.peek_account(dst) == B
    with pytest.raises(journal_mod.JournalAccountError):
        new.bind_account(A)  # il journal nuovo appartiene a B
    with pytest.raises(FileExistsError):
        journal_tool.split(src, dst, ["r17"], None, None)  # non sovrascrive


def test_split_cli_dry_run_and_validation(tmp_path, capsys):
    src = tmp_path / "m.db"
    j = journal_mod.Journal(src)
    _run(j, "r1", A, "quality")
    dst = tmp_path / "n.db"
    base = ["split", "--db", str(src), "--run-id", "r1"]
    assert journal_tool.main([*base, "--dry-run", "--to", str(dst)]) == 0 and not dst.exists()
    assert journal_tool.main(base) == 2  # manca --to
    assert (
        journal_tool.main([*base, "--to", str(dst), "--portfolio", "P", "--strategy", "quality"])
        == 0
    )
    assert "Copiate 1 esecuzioni" in capsys.readouterr().out and dst.exists()
    assert (
        journal_tool.main(
            ["split", "--db", str(src), "--run-id", "nessuna", "--to", str(tmp_path / "z.db")]
        )
        == 1
    )
    with sqlite3.connect(dst) as db:
        assert (
            json.loads(db.execute("SELECT payload FROM events WHERE kind = 'RUN'").fetchone()[0])[
                "portfolio"
            ]
            == "P"
        )


def test_list_output_is_well_formatted(tmp_path, capsys):
    j = journal_mod.Journal(tmp_path / "l.db", "P", "beta")
    _run(j, "20261008-173732-0c87", A, "beta")
    assert journal_tool.main(["list", "--db", str(tmp_path / "l.db")]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert "ora (UTC)" in lines[0] and "':" not in out and "<20" not in out
    assert lines[1].startswith("20261008-173732-0c87") and "ribilanciamento" in lines[1]
    assert lines[1].split()[1] == journal_mod.iso(journal_mod.now())[:10]  # data di oggi (UTC)


def test_split_overwrite_replaces_a_previous_attempt_but_never_the_source(tmp_path):
    src = tmp_path / "m.db"
    j = journal_mod.Journal(src)
    _run(j, "r1", A, "beta")
    _run(j, "r2", A, "short")
    dst = tmp_path / "d.db"
    base = ["split", "--db", str(src), "--to", str(dst), "--portfolio", "P", "--strategy", "beta"]
    assert journal_tool.main([*base, "--run-id", "r1"]) == 0
    assert journal_tool.main([*base, "--run-id", "r2"]) == 2  # esiste già: non sovrascrive da solo
    assert journal_tool.main([*base, "--run-id", "r2", "--overwrite"]) == 0
    assert {e["run_id"] for e in journal_mod.Journal(dst).events(1000)} == {"r2"}
    same = ["split", "--db", str(src), "--to", str(src), "--run-id", "r1", "--overwrite"]
    assert (
        journal_tool.main(same) == 2
        and src.exists()
        and len(journal_mod.Journal(src).events(1000)) > 0
    )


def test_split_append_merges_runs_of_the_same_portfolio_into_one_journal(tmp_path):
    a_old, a_new = tmp_path / "beta.db", tmp_path / "beta_oggi.db"
    j1 = journal_mod.Journal(a_old, "Portafoglio beta", "beta")
    _run(j1, "r-ieri", A, "beta", 3)
    j2 = journal_mod.Journal(a_new, "Portafoglio beta", "beta")
    _run(j2, "r-oggi", A, "beta", 2)
    before = a_new.read_bytes()
    res = journal_tool.split(a_new, a_old, ["r-oggi"], None, None, append=True)
    assert res["proposals"] == 2 and res["skipped"] == [] and res["account"] == A
    assert a_new.read_bytes() == before  # la sorgente non cambia
    merged = journal_mod.Journal(a_old)
    runs = merged.runs()
    assert [r["run_id"] for r in runs] == ["r-ieri", "r-oggi"]  # in ordine di tempo
    assert {p["run_id"] for p in merged.proposals()} == {"r-ieri", "r-oggi"} and len(
        merged.proposals()
    ) == 5
    assert all(e["portfolio"] == "Portafoglio beta" for e in merged.events(1000))
    # rilanciare non duplica
    res2 = journal_tool.split(a_new, a_old, ["r-oggi"], None, None, append=True)
    assert res2["skipped"] == ["r-oggi"] and res2["events"] == 0 and len(merged.proposals()) == 5


def test_split_append_refuses_a_different_account_and_plain_split_still_refuses_existing_files(
    tmp_path,
):
    dst, src = tmp_path / "d.db", tmp_path / "s.db"
    _run(journal_mod.Journal(dst), "r1", A, "beta")
    _run(journal_mod.Journal(src), "r2", B, "quality")
    with pytest.raises(journal_mod.JournalAccountError, match="non le unisco"):
        journal_tool.split(src, dst, ["r2"], None, None, append=True)
    assert {r["run_id"] for r in journal_mod.Journal(dst).runs()} == {"r1"}
    with pytest.raises(FileExistsError):
        journal_tool.split(src, dst, ["r2"], None, None)
    with pytest.raises(FileExistsError):
        journal_tool.split(dst, dst, ["r1"], None, None, append=True)


def test_split_append_cli(tmp_path, capsys):
    dst, src = tmp_path / "d.db", tmp_path / "s.db"
    _run(journal_mod.Journal(dst, "P", "beta"), "r1", A, "beta")
    _run(journal_mod.Journal(src, "P", "beta"), "r2", A, "beta")
    cmd = ["split", "--db", str(src), "--to", str(dst), "--run-id", "r2", "--append"]
    assert journal_tool.main(cmd) == 0 and "Copiate 1 esecuzioni" in capsys.readouterr().out
    assert journal_tool.main(cmd) == 0 and "Già presenti e saltate: r2" in capsys.readouterr().out
    assert journal_tool.main([*cmd, "--overwrite"]) == 2


def test_compare_summarizes_several_journals_of_one_portfolio(tmp_path):
    import compare_portfolios as cp

    j1 = journal_mod.Journal(tmp_path / "a.db", "B", "beta")
    _run(j1, "r1", A, "beta")
    j1.set_status("r1-p0", "filled", "DECISION_DONE")
    j2 = journal_mod.Journal(tmp_path / "b.db", "B", "beta")
    _run(j2, "r2", A, "beta")
    j2.set_status("r2-p0", "filled", "DECISION_DONE")
    j2.log("TRADE_FAILED", {"symbol": "X", "reason": "x"}, "r2")
    one = cp.journal_summary(tmp_path / "a.db")
    both = cp.journal_summary([tmp_path / "a.db", tmp_path / "b.db"])
    assert one["n_runs"] == 1 and both["n_runs"] == 2 and both["n_journals"] == 2
    assert both["trade_failed"] == 1 and both["strategy"] == "beta"
    assert both["first_fill"] == min(
        one["first_fill"], cp.journal_summary(tmp_path / "b.db")["first_fill"]
    )
    assert cp.journal_summary([]) == {} and cp.journal_summary([tmp_path / "manca.db"]) == {}
