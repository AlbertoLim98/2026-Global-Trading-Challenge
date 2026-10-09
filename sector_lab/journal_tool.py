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
    lines = [f"{'run_id':<26}{'ora locale':<20}{'tipo':<18}{'registrata':<11}{'assegnata a'}"]
    for r in runs:
        kind = "ribilanciamento" if "RUN" in (r["kinds"] or "") else "controllo stop"
        lab = f"{r['portfolio'] or '-'} / {STRATEGY_LABEL.get(r['strategy'], r['strategy'] or '-')}"
        lines.append(
            f"{r['run_id']:<26}{_local(r['ts'], tz):%Y-%m-%d %H:%M':<20}{kind:<18}"
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["list", "relabel"])
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
    ap.add_argument("--strategy", choices=sorted(STRATEGY_LABEL), help="beta o quality")
    ap.add_argument("--reason", default="correzione etichette", help="nota registrata nell'evento")
    ap.add_argument("--dry-run", action="store_true", help="mostra cosa cambierebbe senza scrivere")
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
    print(f"\\n{len(chosen)} esecuzioni {verb} a {a.portfolio} / {STRATEGY_LABEL[a.strategy]}")
    if not a.dry_run:
        j.relabel([r["run_id"] for r in chosen], a.portfolio, a.strategy, a.reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
