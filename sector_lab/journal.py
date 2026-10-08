"""Journal delle decisioni (SQLite, solo accodamento) e stato corrente delle proposte.

`events` è il registro: ogni esecuzione, proposta, decisione, ordine ed errore è una riga che non
viene mai modificata né cancellata. `proposals` tiene solo lo stato corrente per l'interfaccia.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    run_id TEXT,
    proposal_id TEXT,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    snooze_until TEXT,
    data TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
"""

OPEN_STATUSES = ("pending", "snoozed")


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    # --- registro -------------------------------------------------------------------------------
    def log(
        self, kind: str, payload: dict, run_id: str | None = None, proposal_id: str | None = None
    ) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO events (ts, kind, run_id, proposal_id, payload) VALUES (?,?,?,?,?)",
                (iso(now()), kind, run_id, proposal_id, json.dumps(payload, default=str)),
            )
            return int(cur.lastrowid)

    def events(
        self, limit: int = 500, kind: str | None = None, run_id: str | None = None
    ) -> list[dict]:
        q, args = "SELECT * FROM events", []
        conds = []
        if kind:
            conds.append("kind = ?")
            args.append(kind)
        if run_id:
            conds.append("run_id = ?")
            args.append(run_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._db.execute(q, [*args, limit]).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # --- proposte ---------------------------------------------------------------------------------
    def add_proposal(self, run_id: str, prop: dict) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO proposals (id, run_id, status, snooze_until, data, updated) VALUES (?,?,?,?,?,?)",
                (prop["id"], run_id, "pending", None, json.dumps(prop, default=str), iso(now())),
            )
        self.log("PROPOSAL", {**prop, "status": "pending"}, run_id, prop["id"])

    def get_proposal(self, pid: str) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
        return self._row(r) if r else None

    def proposals(
        self, statuses: tuple[str, ...] | None = None, run_id: str | None = None
    ) -> list[dict]:
        q, args = "SELECT * FROM proposals", []
        conds = []
        if statuses:
            conds.append(f"status IN ({','.join('?' * len(statuses))})")
            args += list(statuses)
        if run_id:
            conds.append("run_id = ?")
            args.append(run_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        with self._lock:
            rows = self._db.execute(q + " ORDER BY updated DESC", args).fetchall()
        out = [self._row(r) for r in rows]
        return sorted(out, key=lambda p: (p["priority"], -p["value"]))

    def set_status(
        self,
        pid: str,
        status: str,
        kind: str,
        extra: dict | None = None,
        snooze_until: str | None = None,
    ) -> None:
        """Aggiorna lo stato corrente e registra l'evento corrispondente."""
        with self._lock, self._db:
            r = self._db.execute("SELECT run_id FROM proposals WHERE id = ?", (pid,)).fetchone()
            self._db.execute(
                "UPDATE proposals SET status = ?, snooze_until = ?, updated = ? WHERE id = ?",
                (status, snooze_until, iso(now()), pid),
            )
        self.log(
            kind,
            {"status": status, "snooze_until": snooze_until, **(extra or {})},
            r["run_id"] if r else None,
            pid,
        )

    def update_data(self, pid: str, **extra: object) -> None:
        """Aggiunge campi (es. id ordine) ai dati correnti della proposta."""
        with self._lock, self._db:
            r = self._db.execute("SELECT data FROM proposals WHERE id = ?", (pid,)).fetchone()
            d = {**json.loads(r["data"]), **extra}
            self._db.execute(
                "UPDATE proposals SET data = ? WHERE id = ?", (json.dumps(d, default=str), pid)
            )

    def supersede_open(self, run_id: str) -> int:
        """Una nuova esecuzione sostituisce le proposte ancora aperte delle esecuzioni precedenti."""
        old = [p for p in self.proposals(OPEN_STATUSES) if p["run_id"] != run_id]
        for p in old:
            self.set_status(p["id"], "superseded", "SUPERSEDED", {"by_run": run_id})
        return len(old)

    @staticmethod
    def _row(r: sqlite3.Row) -> dict:
        d = json.loads(r["data"])
        return {
            **d,
            "run_id": r["run_id"],
            "status": r["status"],
            "snooze_until": r["snooze_until"],
            "updated": r["updated"],
        }


def summarize(kind: str, p: dict) -> str:
    """Riga di testo leggibile per un evento (per interfaccia ed Excel)."""
    if kind == "RUN":
        return (
            f"Ribilanciamento avviato: modalità {p.get('params', {}).get('mode')}, "
            f"{p.get('n_proposals', 0)} proposte, capitale {p.get('params', {}).get('capital', 0):,.0f}$"
        )
    if kind == "PROPOSAL":
        return f"{p['kind']} {p['qty']:g} {p['symbol']} @ {p['price']:.2f}$ ({p['value']:,.0f}$) - {p['reason']}"
    if kind == "DECISION":
        return f"Decisione: {p.get('action')} - {p.get('symbol', '')}"
    if kind == "ORDER":
        return (
            f"Ordine {p.get('side')} {p.get('qty'):g} {p.get('symbol')} inviato "
            f"(id {p.get('order_id')}, stato {p.get('order_status')})"
        )
    if kind == "ORDER_UPDATE":
        return f"Ordine {p.get('order_id')}: {p.get('order_status')}, eseguiti {p.get('filled_qty')} @ {p.get('filled_avg_price')}"
    if kind == "DECISION_DONE":
        return f"Esito della proposta: {p.get('status')} (ordine {p.get('order_id')}, {p.get('order_status')})"
    if kind == "STOP_CHECK":
        return f"Controllo stop ATR: {p.get('n_new', 0)} nuove proposte su {p.get('n_positions', 0)} posizioni"
    if kind == "SUPERSEDED":
        return f"Proposta sostituita dalla nuova esecuzione {p.get('by_run')}"
    if kind == "ERROR":
        return f"ERRORE: {p.get('message')}"
    return f"{kind}: {p.get('status', '')}"
