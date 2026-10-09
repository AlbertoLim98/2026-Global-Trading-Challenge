"""Strumento per il journal: elenca le esecuzioni e assegna portafoglio e strategia a quelle passate.

Il journal è in sola aggiunta: nessuna riga viene modificata. L'assegnazione è un nuovo evento
RELABEL che l'interfaccia e l'Excel applicano a tutte le righe di quelle esecuzioni (l'ultima
assegnazione vince). Non cambia ciò che è stato davvero eseguito sul conto: corregge solo l'etichetta.

Le ore si leggono in UTC (come la colonna "Data/ora (UTC)" dell'Excel); con --tz si cambia fuso.
L'ora serve solo a scegliere righe già scritte: il programma non pianifica né forza mai i ribilanciamenti,
che partono solo quando premi il pulsante.

    uv run python sector_lab/journal_tool.py list    --db sector_lab/journal.db
    uv run python sector_lab/journal_tool.py relabel --db sector_lab/journal.db \\
        --hour 17 --portfolio "Portafoglio 17" --strategy beta --dry-run
    uv run python sector_lab/journal_tool.py relabel --db sector_lab/journal.db \\
        --run-id 20261008-180425-8dc4 --portfolio "Portafoglio 18" --strategy quality

Per SEPARARE un journal in cui si sono mescolati due portafogli (originale intatto):

    uv run python sector_lab/journal_tool.py split --db sector_lab/journal.db --to sector_lab/journal_p18.db \\
        --run-id 20261008-180425-8dc4 --portfolio "Portafoglio 18" --strategy quality
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from journal import STRATEGY_LABEL, Journal


def _local(ts: str, tz: ZoneInfo) -> datetime:
    return datetime.fromisoformat(ts).astimezone(tz)


def _fmt(runs: list[dict], tz: ZoneInfo) -> str:
    head = f"ora ({tz.key})"
    lines = [f"{'run_id':<26}{head:<20}{'tipo':<18}{'registrata':<11}{'assegnata a'}"]
    for r in runs:
        kind = "ribilanciamento" if "RUN" in (r["kinds"] or "") else "controllo stop"
        lab = f"{r['portfolio'] or '-'} / {STRATEGY_LABEL.get(r['strategy'], r['strategy'] or '-')}"
        lines.append(
            f"{r['run_id']:<26}{_local(r['ts'], tz).strftime('%Y-%m-%d %H:%M'):<20}{kind:<18}"
            f"{r['recorded_mode'] or '-':<11}{lab}{' (riclassificata)' if r['relabeled'] else ''}"
        )
    return "\n".join(lines)


def select(
    runs: list[dict], tz: ZoneInfo, hour: int | None, date: str | None, ids: list[str]
) -> list[dict]:
    out = []
    for r in runs:
        when = _local(r["ts"], tz)
        if r["run_id"] in ids or (
            hour is not None and when.hour == hour and (date is None or f"{when:%Y-%m-%d}" == date)
        ):
            out.append(r)
    return out


def split(
    src: str | Path,
    dst: str | Path,
    run_ids: list[str],
    portfolio: str | None,
    strategy: str | None,
) -> dict:
    """Copia le esecuzioni indicate (eventi, operazioni, indicatori) in un journal NUOVO; l'originale non cambia.

    Serve a separare un journal in cui si sono mescolati due portafogli (per esempio cambiando le chiavi nel .env
    ma usando sempre lo stesso file). Se indicati, `portfolio` e `strategy` vengono scritti in tutte le righe copiate.
    """
    import json
    import sqlite3

    if Path(dst).exists():
        raise FileExistsError(f"{dst} esiste già: scegli un nome nuovo")
    srcdb = sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True)
    srcdb.row_factory = sqlite3.Row
    out = Journal(dst, portfolio, strategy)
    marks = ",".join("?" * len(run_ids))
    counts = {"events": 0, "proposals": 0, "indicators": 0}
    accounts: set[str] = set()

    def rewrite(d: dict) -> dict:
        if portfolio:
            d |= {"portfolio": portfolio} if "portfolio" in d else {}
        if strategy:
            d |= (
                {"strategy": strategy, "strategy_label": STRATEGY_LABEL[strategy]}
                if "strategy" in d
                else {}
            )
        return d

    try:
        cols = {r["name"] for r in srcdb.execute("PRAGMA table_info(events)")}
        with out._db:
            for r in srcdb.execute(
                f"SELECT * FROM events WHERE run_id IN ({marks}) ORDER BY id", run_ids
            ):
                p = json.loads(r["payload"])
                if r["kind"] == "RUN":
                    acct = (p.get("account") or {}).get("account_number")
                    if acct:
                        accounts.add(acct)
                    p = rewrite(p)
                out._db.execute(
                    "INSERT INTO events (ts, kind, run_id, proposal_id, payload, portfolio, strategy) VALUES (?,?,?,?,?,?,?)",
                    (
                        r["ts"], r["kind"], r["run_id"], r["proposal_id"], json.dumps(p),
                        portfolio or (r["portfolio"] if "portfolio" in cols else None),
                        strategy or (r["strategy"] if "strategy" in cols else None),
                    ),
                )  # fmt: skip
                counts["events"] += 1
            for r in srcdb.execute(f"SELECT * FROM proposals WHERE run_id IN ({marks})", run_ids):
                d = rewrite(json.loads(r["data"]))
                out._db.execute(
                    "INSERT INTO proposals (id, run_id, status, snooze_until, data, updated) VALUES (?,?,?,?,?,?)",
                    (
                        r["id"],
                        r["run_id"],
                        r["status"],
                        r["snooze_until"],
                        json.dumps(d),
                        r["updated"],
                    ),
                )
                counts["proposals"] += 1
            if srcdb.execute("SELECT 1 FROM sqlite_master WHERE name = 'indicators'").fetchone():
                for r in srcdb.execute(
                    f"SELECT * FROM indicators WHERE run_id IN ({marks}) ORDER BY id", run_ids
                ):
                    out._db.execute(
                        "INSERT INTO indicators (ts, run_id, portfolio, strategy, kind, symbol, selected, target_value, data)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (r["ts"], r["run_id"], portfolio or r["portfolio"], strategy or r["strategy"], r["kind"],
                         r["symbol"], r["selected"], r["target_value"], r["data"]),
                    )  # fmt: skip
                    counts["indicators"] += 1
            if len(accounts) == 1:
                out._db.execute(
                    "INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('account_number', ?)",
                    (next(iter(accounts)),),
                )
    finally:
        srcdb.close()
    return counts | {
        "account": next(iter(accounts)) if len(accounts) == 1 else None,
        "accounts_seen": sorted(accounts),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["list", "relabel", "split"])
    ap.add_argument("--db", required=True, help="percorso del journal (.db)")
    ap.add_argument(
        "--tz",
        default="UTC",
        help="fuso con cui leggere le ore (default UTC, come l'Excel del journal)",
    )
    ap.add_argument(
        "--hour",
        type=int,
        help="seleziona le esecuzioni GIÀ REGISTRATE avviate a quest'ora (0-23), solo per etichettarle",
    )
    ap.add_argument("--date", help="limita a un giorno (AAAA-MM-GG, nel fuso scelto)")
    ap.add_argument("--run-id", action="append", default=[], help="esecuzione precisa (ripetibile)")
    ap.add_argument("--all", action="store_true", help="assegna tutte le esecuzioni del journal")
    ap.add_argument("--portfolio", help="nome del portafoglio da assegnare")
    ap.add_argument("--strategy", choices=sorted(STRATEGY_LABEL), help="beta, quality o short")
    ap.add_argument("--reason", default="correzione etichette", help="nota registrata nell'evento")
    ap.add_argument("--dry-run", action="store_true", help="mostra cosa cambierebbe senza scrivere")
    ap.add_argument(
        "--to", help="split: percorso del journal NUOVO in cui copiare le esecuzioni scelte"
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="split: sostituisce il journal di destinazione se esiste già",
    )
    a = ap.parse_args(argv)

    if not Path(a.db).exists():
        print(f"Journal non trovato: {a.db}", file=sys.stderr)
        return 2
    tz = ZoneInfo(a.tz)
    j = Journal(a.db)
    runs = j.run_catalog()
    if a.command == "list":
        print(_fmt(runs, tz))
        return 0
    if a.command == "split":
        if not a.to:
            print("Per split serve --to (journal nuovo)", file=sys.stderr)
            return 2
        chosen = runs if a.all else select(runs, tz, a.hour, a.date, a.run_id)
        if not chosen:
            print("Nessuna esecuzione corrisponde ai criteri.")
            return 1
        print(_fmt(chosen, tz))
        if a.dry_run:
            print(f"\n{len(chosen)} esecuzioni verrebbero copiate in {a.to}")
            return 0
        if a.overwrite and Path(a.to).exists():
            if Path(a.to).resolve() == Path(a.db).resolve():
                print("--to non può essere il journal di origine", file=sys.stderr)
                return 2
            Path(a.to).unlink()
        try:
            res = split(a.db, a.to, [r["run_id"] for r in chosen], a.portfolio, a.strategy)
        except FileExistsError as e:
            print(e, file=sys.stderr)
            return 2
        print(
            f"\nCopiate {len(chosen)} esecuzioni in {a.to}: {res['events']} eventi, {res['proposals']} operazioni, {res['indicators']} righe di indicatori."
        )
        print(
            f"Conto: {res['account'] or 'non determinabile (' + ', '.join(res['accounts_seen']) + ')' if res['accounts_seen'] else 'non registrato nelle esecuzioni copiate'}"
        )
        return 0
    if not (a.portfolio and a.strategy):
        print("Servono --portfolio e --strategy", file=sys.stderr)
        return 2
    if a.all:
        chosen = runs
    elif a.hour is not None or a.run_id:
        chosen = select(runs, tz, a.hour, a.date, a.run_id)
    else:
        print("Indica cosa assegnare: --hour, --run-id oppure --all", file=sys.stderr)
        return 2
    if not chosen:
        print("Nessuna esecuzione corrisponde ai criteri.")
        return 1
    print(_fmt(chosen, tz))
    verb = "verrebbero assegnate" if a.dry_run else "assegnate"
    print(f"\n{len(chosen)} esecuzioni {verb} a {a.portfolio} / {STRATEGY_LABEL[a.strategy]}")
    if not a.dry_run:
        j.relabel([r["run_id"] for r in chosen], a.portfolio, a.strategy, a.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
