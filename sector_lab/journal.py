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
CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT NOT NULL,
    portfolio TEXT,
    strategy TEXT,
    kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    selected INTEGER NOT NULL,
    target_value REAL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS indicators_no_update BEFORE UPDATE ON indicators
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
CREATE TRIGGER IF NOT EXISTS indicators_no_delete BEFORE DELETE ON indicators
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'journal append-only'); END;
"""

STRATEGY_LABEL = {"beta": "Alto beta", "quality": "Qualità", "short": "Breve termine (1 gg)"}
RELABEL = "RELABEL"  # evento di riclassificazione: il registro resta in sola aggiunta

# stati "attivi": ordine in corso oppure fallito e ancora da gestire
ACTIVE_STATUSES = ("submitted", "failed")


class JournalAccountError(Exception):
    """Il journal appartiene a un altro conto Alpaca: usarlo mescolerebbe i dati di due portafogli."""


def peek_account(path: str | Path) -> str | None:
    """Conto Alpaca a cui appartiene un journal (lettura senza modificare il file), se noto."""
    p = Path(path)
    if not p.exists():
        return None
    db = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
    try:
        r = db.execute("SELECT value FROM journal_meta WHERE key = 'account_number'").fetchone()
        if r:
            return r[0]
        for (payload,) in db.execute(
            "SELECT payload FROM events WHERE kind = 'RUN' ORDER BY id DESC"
        ):
            acct = (json.loads(payload).get("account") or {}).get("account_number")
            if acct:
                return acct
    except sqlite3.DatabaseError:
        return None
    finally:
        db.close()
    return None


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Journal:
    def __init__(
        self, path: str | Path, portfolio: str | None = None, strategy: str | None = None
    ) -> None:
        """`portfolio` e `strategy` sono scritti in ogni nuovo evento (None = non assegnato)."""
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.portfolio, self.strategy = portfolio, strategy
        with (
            self._db
        ):  # journal creati prima di queste colonne: si aggiungono senza toccare le righe
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(events)")}
            for c in ("portfolio", "strategy"):
                if c not in cols:
                    self._db.execute(f"ALTER TABLE events ADD COLUMN {c} TEXT")

    def bind_account(self, account_number: str | None) -> None:
        """Lega il journal a un conto Alpaca; rifiuta un conto diverso (evita di mescolare due portafogli)."""
        if not account_number:
            return
        with self._lock:
            r = self._db.execute(
                "SELECT value FROM journal_meta WHERE key = 'account_number'"
            ).fetchone()
            known = r[0] if r else None
            if known is None:  # journal vecchi: il conto dell'ultimo ribilanciamento registrato
                for (payload,) in self._db.execute(
                    "SELECT payload FROM events WHERE kind = 'RUN' ORDER BY id DESC"
                ):
                    known = (json.loads(payload).get("account") or {}).get("account_number")
                    if known:
                        break
            if known and known != account_number:
                raise JournalAccountError(
                    f"Questo journal appartiene al conto {known}, ma le chiavi in uso sono del conto "
                    f"{account_number}. Per non mescolare due portafogli usa un journal diverso "
                    "(--journal PERCORSO) oppure le chiavi giuste."
                )
            if r is None:
                with self._db:
                    self._db.execute(
                        "INSERT OR IGNORE INTO journal_meta (key, value) VALUES ('account_number', ?)",
                        (account_number,),
                    )

    # --- registro -------------------------------------------------------------------------------
    def log(
        self, kind: str, payload: dict, run_id: str | None = None, proposal_id: str | None = None
    ) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO events (ts, kind, run_id, proposal_id, payload, portfolio, strategy)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    iso(now()),
                    kind,
                    run_id,
                    proposal_id,
                    json.dumps(payload, default=str),
                    self.portfolio,
                    self.strategy,
                ),
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
        labels = self.labels()
        return [
            {**dict(r), **self._effective(r, labels), "payload": json.loads(r["payload"])}
            for r in rows
        ]

    # --- valori degli indicatori che hanno composto i punteggi ----------------------------------------
    def log_indicators(self, run_id: str, rows: list[dict]) -> int:
        """Salva, per un ribilanciamento, tutti gli indicatori e i punteggi di ogni titolo/settore considerato.

        Ogni riga: kind ("stock" | "sector"), symbol, selected (0/1), target_value, data (dict con i valori).
        """
        ts = iso(now())
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO indicators (ts, run_id, portfolio, strategy, kind, symbol, selected, target_value, data)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        ts,
                        run_id,
                        self.portfolio,
                        self.strategy,
                        r["kind"],
                        r["symbol"],
                        int(bool(r.get("selected"))),
                        r.get("target_value"),
                        json.dumps(r["data"], default=str),
                    )
                    for r in rows
                ],
            )
        return len(rows)

    def indicators(self, run_id: str | None = None) -> list[dict]:
        """Indicatori di ogni ribilanciamento. Per i ribilanciamenti vecchi, senza tabella dedicata, si ricavano
        dai dati che il RUN ha registrato (meno completi: solo i campi allora salvati)."""
        labels = self.labels()
        with self._lock:
            q = (
                "SELECT * FROM indicators"
                + (" WHERE run_id = ?" if run_id else "")
                + " ORDER BY id"
            )
            rows = self._db.execute(q, [run_id] if run_id else []).fetchall()
            runs = self._db.execute(
                "SELECT ts, run_id, payload, portfolio, strategy FROM events WHERE kind = 'RUN' ORDER BY id"
            ).fetchall()
        out = []
        have = set()
        for r in rows:
            lab = labels.get(r["run_id"]) or {}
            have.add(r["run_id"])
            out.append(
                {
                    "run_id": r["run_id"],
                    "ts": r["ts"],
                    "portfolio": lab.get("portfolio") or r["portfolio"],
                    "strategy": lab.get("strategy") or r["strategy"],
                    "kind": r["kind"],
                    "symbol": r["symbol"],
                    "selected": bool(r["selected"]),
                    "target_value": r["target_value"],
                    "source": "completo",
                    "data": json.loads(r["data"]),
                }
            )
        for r in runs:
            if r["run_id"] in have or (run_id and r["run_id"] != run_id):
                continue
            p = json.loads(r["payload"])
            lab = labels.get(r["run_id"]) or {}
            meta = {
                "run_id": r["run_id"],
                "ts": r["ts"],
                "portfolio": lab.get("portfolio") or r["portfolio"] or p.get("portfolio"),
                "strategy": lab.get("strategy")
                or r["strategy"]
                or (p.get("params") or {}).get("mode"),
                "source": "dal RUN (parziale)",
            }
            tv = {t["symbol"]: t["value"] for t in p.get("targets", [])}
            for row in p.get("sector_table") or []:
                out.append({**meta, "kind": "sector", "symbol": row["symbol"], "selected": False,
                            "target_value": None, "data": row})  # fmt: skip
            tables = [x for rows_ in (p.get("stock_tables") or {}).values() for x in rows_]
            tables += p.get("short_table") or []
            for row in tables:
                out.append({**meta, "kind": "stock", "symbol": row["symbol"], "selected": row["symbol"] in tv,
                            "target_value": tv.get(row["symbol"]), "data": row})  # fmt: skip
        return out

    # --- etichette portafoglio / strategia ---------------------------------------------------------
    def labels(self) -> dict[str, dict]:
        """Ultima riclassificazione di ogni esecuzione (run_id -> portafoglio, strategia)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT payload FROM events WHERE kind = ? ORDER BY id ASC", (RELABEL,)
            ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            p = json.loads(r["payload"])
            for rid in p.get("run_ids", []):
                out[rid] = {"portfolio": p.get("portfolio"), "strategy": p.get("strategy")}
        return out

    @staticmethod
    def _effective(row: sqlite3.Row, labels: dict[str, dict]) -> dict:
        """Etichetta valida di un evento: la riclassificazione vince su quella scritta alla nascita."""
        lab = labels.get(row["run_id"]) if row["run_id"] else None
        return {
            "portfolio": lab["portfolio"] if lab else row["portfolio"],
            "strategy": lab["strategy"] if lab else row["strategy"],
            "relabeled": lab is not None,
        }

    def relabel(self, run_ids: list[str], portfolio: str, strategy: str, reason: str = "") -> int:
        """Assegna portafoglio e strategia a esecuzioni già registrate, senza modificare le righe."""
        if strategy not in STRATEGY_LABEL:
            raise ValueError(f"strategia sconosciuta: {strategy}")
        before = {r: self.labels().get(r) for r in run_ids}
        self.log(
            RELABEL,
            {
                "run_ids": run_ids,
                "portfolio": portfolio,
                "strategy": strategy,
                "reason": reason,
                "previous": before,
            },
        )
        return len(run_ids)

    def run_catalog(self) -> list[dict]:
        """Tutte le esecuzioni (anche controlli stop) con ora, strategia registrata ed etichetta valida."""
        with self._lock:
            rows = self._db.execute(
                "SELECT run_id, MIN(ts) AS first_ts, COUNT(*) AS n, GROUP_CONCAT(DISTINCT kind) AS kinds,"
                " MIN(portfolio) AS portfolio, MIN(strategy) AS strategy"
                " FROM events WHERE run_id IS NOT NULL GROUP BY run_id ORDER BY first_ts, run_id"
            ).fetchall()
            runs = self._db.execute(
                "SELECT run_id, payload FROM events WHERE kind = 'RUN'"
            ).fetchall()
        recorded = {
            r["run_id"]: (json.loads(r["payload"]).get("params") or {}).get("mode") for r in runs
        }
        labels = self.labels()
        out = []
        for r in rows:
            lab = labels.get(r["run_id"])
            out.append(
                {
                    "run_id": r["run_id"],
                    "ts": r["first_ts"],
                    "n_events": r["n"],
                    "kinds": r["kinds"],
                    "recorded_mode": recorded.get(r["run_id"]),
                    "portfolio": lab["portfolio"] if lab else r["portfolio"],
                    "strategy": lab["strategy"] if lab else r["strategy"],
                    "relabeled": lab is not None,
                }
            )
        return out

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

    def supersede_failed(self, run_id: str) -> int:
        """Una nuova esecuzione ricalcola tutto dalle posizioni: i fallimenti vecchi non servono più."""
        old = [p for p in self.proposals(("failed",)) if p["run_id"] != run_id]
        for p in old:
            self.set_status(p["id"], "superseded", "SUPERSEDED", {"by_run": run_id})
        return len(old)

    def latest_run_id(self) -> str | None:
        with self._lock:
            r = self._db.execute(
                "SELECT run_id FROM events WHERE kind = 'RUN' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return r["run_id"] if r else None

    def runs(self) -> list[dict]:
        """Riallocazioni (esecuzioni complete) dalla più vecchia: data, patrimonio al via, modalità."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, run_id, payload, portfolio, strategy FROM events WHERE kind = 'RUN' ORDER BY id ASC"
            ).fetchall()
        out = []
        labels = self.labels()
        for r in rows:
            p = json.loads(r["payload"])
            lab = labels.get(r["run_id"]) or {}
            out.append(
                {
                    "run_id": r["run_id"],
                    "portfolio": lab.get("portfolio") or r["portfolio"],
                    "strategy": lab.get("strategy")
                    or r["strategy"]
                    or (p.get("params") or {}).get("mode"),
                    "ts": r["ts"],
                    "equity": (p.get("account") or {}).get("equity"),
                    "mode": (p.get("params") or {}).get("mode"),
                    "n_orders": p.get("n_proposals", 0),
                }
            )
        return out

    def set_context(self, portfolio: str | None, strategy: str | None) -> None:
        """Portafoglio e strategia in uso: scritti in ogni evento registrato da qui in avanti."""
        self.portfolio, self.strategy = portfolio, strategy

    def opening(self) -> dict | None:
        """Apertura del portafoglio: data e patrimonio del conto al primo ribilanciamento registrato."""
        with self._lock:
            r = self._db.execute(
                "SELECT ts, payload FROM events WHERE kind = 'RUN' ORDER BY id ASC LIMIT 1"
            ).fetchone()
        if not r:
            return None
        acct = json.loads(r["payload"]).get("account") or {}
        return {"ts": r["ts"], "equity": acct.get("equity")}

    def trades(self) -> list[dict]:
        """Operazioni dell'ultima esecuzione (e controlli stop successivi) più tutto ciò che è attivo."""
        last = self.latest_run_id()
        return [
            p
            for p in self.proposals()
            if p["status"] in ACTIVE_STATUSES or last is None or p["run_id"] >= last
        ]

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
        return f"Operazione {p['kind']} {p['qty']:g} {p['symbol']} @ {p['price']:.2f}$ ({p['value']:,.0f}$) - {p['reason']}"
    if kind == "DECISION":
        return f"Invio automatico: {p.get('side', '')} {p.get('qty')} {p.get('symbol', '')} @ ~{p.get('price_at_decision', 0):.2f}$"
    if kind == "TRADE_FAILED":
        return f"OPERAZIONE FALLITA {p.get('symbol', '')}: {p.get('reason')}"
    if kind == "REPOSITION":
        return f"Riposizionata a prezzo attuale: {p.get('symbol')} {p.get('side')} {p.get('qty')} @ {p.get('limit_price')}"
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
    if kind == RELABEL:
        n = len(p.get("run_ids", []))
        return (
            f"Riclassificate {n} esecuzioni: {p.get('portfolio')} / "
            f"{STRATEGY_LABEL.get(p.get('strategy'), p.get('strategy'))}"
        )
    if kind == "ORDER_CANCELED":
        return f"Ordine annullato {p.get('symbol', '')} ({p.get('order_id')}){': ' + p['note'] if p.get('note') else ''}"
    if kind == "SUPERSEDED":
        return f"Operazione fallita archiviata dalla nuova esecuzione {p.get('by_run')}"
    if kind == "ERROR":
        return f"ERRORE: {p.get('message')}"
    return f"{kind}: {p.get('status', '')}"
