"""Algoritmo di ribilanciamento giornaliero: dalle tabelle (settori + aziende) ai target e alle proposte.

Modulo puro (nessuna rete, nessun ordine): riceve tabelle, posizioni e prezzi e restituisce
l'allocazione obiettivo e l'elenco delle proposte di acquisto/vendita. Le proposte non vengono
eseguite qui: le invia `desk.py`.

Regole (tutte modificabili in `Params`):
  1. Settori idonei: trend dell'ETF non ribassista e punteggio totale >= `min_sector_score`.
  2. Peso dei settori proporzionale al punteggio, senza tetto per settore (anche solo 2 settori vanno bene).
  3. Dentro il settore: solo le aziende della top 10 con trend relativo non ribassista e TWRR > 0,
     peso proporzionale al punteggio (in modalità alto beta il beta sceglie i titoli, il punteggio li pesa),
     tetto `max_stock` (10%) del capitale per titolo; l'eccedenza passa agli altri titoli.
  4. Riserva di liquidità minima `cash_reserve`.
  5. Nessuna regola d'uscita a parte il ribilanciamento: niente stop (ATR o altro). Un titolo esce solo
     quando, rifatte le statistiche, non è più selezionato.
  6. Vendite: solo complete (titolo che esce dalla selezione). Nessuna vendita parziale: le statistiche si
     rifanno ogni giorno, quindi un titolo sopra target resta com'è.
  7. Acquisti: si integra un titolo sotto target solo se lo scarto supera `min_trade` e
     `drift_tolerance` x valore target (evita operazioni inutili ogni giorno).
Nessuna regola può garantire l'assenza di perdite: riducono il rischio, non lo azzerano.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass

import stocks


@dataclass(frozen=True)
class Params:
    capital: float = 1_000_000.0
    cash_reserve: float = 0.03
    max_stock: float = 0.10
    min_sector_score: float = 50.0
    min_trade: float = 2_000.0
    drift_tolerance: float = 0.20
    mode: str = "quality"  # "quality" | "beta" | "short"
    short_n: int = (
        20  # strategia a 1 giorno: quanti titoli tenere (i migliori per score di breve periodo)
    )

    def as_dict(self) -> dict:
        return asdict(self)


def allocate(raw: dict[str, float], total: float, cap: float) -> dict[str, float]:
    """Ripartisce `total` in proporzione a `raw` con tetto `cap` per voce.

    Se tutte le voci raggiungono il tetto, il resto non viene assegnato (resta liquidità).
    """
    alloc: dict[str, float] = {}
    free = {k: v for k, v in raw.items() if v > 0}
    remaining = total
    while free:
        s = sum(free.values())
        over = [k for k, v in free.items() if remaining * v / s > cap + 1e-9]
        if not over:
            alloc.update({k: remaining * v / s for k, v in free.items()})
            break
        for k in over:
            alloc[k] = cap
            remaining -= cap
            del free[k]
    return alloc


def sector_budgets(sector_rows: list[dict], p: Params) -> list[dict]:
    """Settori con idoneità e peso grezzo. Tutti compaiono, con il motivo se esclusi.

    Il budget in dollari viene assegnato da `build_targets` insieme ai titoli.
    """
    out = []
    for r in sector_rows:
        total = (r.get("scores") or {}).get("total")
        ok = r["trend_label"] != "Ribassista" and total is not None and total >= p.min_sector_score
        out.append(
            {
                "symbol": r["symbol"],
                "name": r["name"],
                "trend_label": r["trend_label"],
                "score": total,
                "eligible": ok,
                "budget": 0.0,
                "raw_weight": total - p.min_sector_score + 10
                if ok
                else 0.0,  # chi supera appena la soglia pesa poco
                "reason": ""
                if ok
                else (
                    "trend ribassista"
                    if r["trend_label"] == "Ribassista"
                    else f"punteggio < {p.min_sector_score:g}"
                ),
            }
        )
    return sorted(out, key=lambda s: -(s["score"] or 0))


def _stock_raw(top: list[dict], p: Params, relaxed: bool) -> dict[str, tuple[float, dict]]:
    """Titoli candidati di un settore con il loro peso interno (solo idonei, o tutta la top 10 se `relaxed`)."""
    rows = top if relaxed else [r for r in top if stocks.is_eligible(r)]
    out = {}
    for r in rows:
        w = r["scores"]["total"] or 0  # il peso segue il punteggio (anche in modalità alto beta)
        if w > 0:
            out[r["symbol"]] = (w, r)
    return out


def build_targets(
    sector_rows: list[dict],
    stocks_by_etf: dict[str, dict],
    p: Params,
) -> dict:
    """Allocazione obiettivo: titoli con valore in dollari e budget risultante per settore.

    Nessun tetto per settore: l'unico limite è `max_stock` del capitale per titolo. Il peso di un
    titolo è (peso del settore) x (sua quota dentro il settore); l'eccedenza dei titoli al tetto passa
    agli altri, in tutti i settori.

    La quota investita è sempre `1 - cash_reserve` (97%): se i titoli idonei non bastano a
    raggiungerla con il tetto per titolo, si estende la selezione, in quest'ordine:
      1. altri settori non ribassisti (per punteggio decrescente), con i loro titoli idonei;
      2. settori ribassisti, con i loro titoli idonei;
      3. titoli della top 10 che non superano i filtri, a partire dai settori meglio classificati.
    I settori e i titoli aggiunti così sono segnalati (`fallback`) e annotati in `notes`.
    Solo se non esistono abbastanza titoli in assoluto resta liquidità in più.
    """
    sectors = sector_budgets(sector_rows, p)
    invest = p.capital * (1 - p.cash_reserve)
    cap_value = p.max_stock * p.capital
    raw_all: dict[str, float] = {}
    info: dict[str, dict] = {}
    notes: list[str] = []
    by_sym = {s["symbol"]: s for s in sectors}

    def add(sector: dict, relaxed: bool, tier: str) -> int:
        top = (stocks_by_etf.get(sector["symbol"]) or {}).get("top", [])
        inner = {k: v for k, v in _stock_raw(top, p, relaxed).items() if k not in raw_all}
        if (
            relaxed
        ):  # filtri allentati: solo i titoli strettamente necessari, i migliori per punteggio
            missing = math.ceil(invest / cap_value - 1e-9) - len(raw_all)
            best = sorted(inner, key=lambda k: -inner[k][0])[: max(missing, 0)]
            inner = {k: inner[k] for k in best}
        tot = sum(w for w, _ in inner.values())
        weight = sector["raw_weight"] or 5.0  # settori aggiunti: pesano meno di quelli idonei
        for sym, (w, r) in inner.items():
            raw_all[sym] = weight * w / tot
            info[sym] = {"row": r, "sector": sector["symbol"], "tier": tier}
        sector["n_candidates"] = sector.get("n_candidates", 0) + len(inner)
        return len(inner)

    def short() -> bool:
        return len(raw_all) * cap_value < invest - 1e-6

    for s in sectors:
        if s["eligible"]:
            add(s, False, "base")
    extra: list[str] = []
    for tier, pick in (
        (
            "settore aggiunto (non idoneo)",
            lambda s: not s["eligible"] and s["trend_label"] != "Ribassista",
        ),
        ("settore ribassista aggiunto", lambda s: s["trend_label"] == "Ribassista"),
    ):
        for s in sectors:
            if short() and pick(s) and add(s, False, tier):
                s["fallback"] = tier
                extra.append(f"{s['name']} ({tier})")
    if short():
        for s in sectors:  # filtri allentati: anche titoli della top 10 non idonei
            if short() and add(s, True, "titoli non idonei aggiunti"):
                s["fallback"] = s.get("fallback") or "titoli non idonei aggiunti"
                extra.append(f"{s['name']} (titoli non idonei)")
    if extra:
        notes.append(
            f"Per investire il {1 - p.cash_reserve:.0%} con al massimo {p.max_stock:.0%} per titolo "
            "sono stati aggiunti: " + ", ".join(extra)
        )
    alloc = allocate(raw_all, invest, cap_value)
    if sum(alloc.values()) < invest - 1.0:
        notes.append(
            f"Non ci sono abbastanza titoli per investire il {1 - p.cash_reserve:.0%} con il tetto "
            f"del {p.max_stock:.0%}: investiti {sum(alloc.values()):,.0f}$ su {invest:,.0f}$"
        )
    targets: dict[str, dict] = {}
    for sym, v in alloc.items():
        r = info[sym]["row"]
        targets[sym] = {
            "symbol": sym,
            "name": r["name"],
            "sector": info[sym]["sector"],
            "value": v,
            "price": r["price"],
            "score": r["scores"]["total"],
            "beta": r.get("beta_1y"),
            "tier": info[sym]["tier"],
        }
    for s in by_sym.values():
        s["budget"] = sum(t["value"] for t in targets.values() if t["sector"] == s["symbol"])
    return {"sectors": sectors, "targets": targets, "notes": notes}


def build_targets_short(cands: list[dict], p: Params) -> dict:
    """Strategia a 1 giorno: i `short_n` titoli con lo score di breve periodo più alto, peso in proporzione.

    Nessuna struttura per settori: l'unico limite è `max_stock` del capitale per titolo; il 97% viene investito
    se i titoli sono almeno 10..
    """
    top = sorted(cands, key=lambda r: -r["total"])[: p.short_n]
    invest = p.capital * (1 - p.cash_reserve)
    alloc = allocate(
        {r["symbol"]: max(r["total"], 1.0) for r in top}, invest, p.max_stock * p.capital
    )
    targets = {
        r["symbol"]: {
            "symbol": r["symbol"],
            "name": r["name"],
            "sector": r["sector"],
            "value": alloc[r["symbol"]],
            "price": r["price"],
            "score": r["total"],
            "beta": r.get("beta"),
            "trend": r["trend_label"],
            "tier": "breve termine",
        }
        for r in top
        if r["symbol"] in alloc
    }
    notes = []
    if sum(alloc.values()) < invest - 1.0:
        notes.append(
            f"Con {len(top)} titoli e tetto del {p.max_stock:.0%} si investe {sum(alloc.values()):,.0f}$ "
            f"su {invest:,.0f}$: servono almeno {int(-(-invest // (p.max_stock * p.capital)))} titoli"
        )
    return {"sectors": [], "targets": targets, "notes": notes}


def _proposal(
    kind: str,
    side: str,
    sym: str,
    name: str,
    sector: str,
    qty: float,
    price: float,
    reason: str,
    **extra,
) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": kind,  # SELL | BUY
        "side": side,
        "symbol": sym,
        "name": name,
        "sector": sector,
        "qty": qty,
        "price": price,
        "value": qty * price,
        "reason": reason,
        "priority": {"SELL": 1, "BUY": 2}[kind],
        **extra,
    }


def build_proposals(
    targets: dict[str, dict],
    positions: dict[str, dict],
    prices: dict[str, float],
    managed: dict[str, dict],
    cash: float,
    p: Params,
) -> dict:
    """Proposte per portare il portafoglio sul target.

    positions: simbolo -> {qty, avg_entry, price}; managed: simbolo -> {name, sector} (universo della
    strategia: le posizioni fuori universo non vengono toccate); cash: liquidità disponibile.
    """
    props: list[dict] = []
    notes: list[str] = []

    for sym, pos in positions.items():
        if sym not in managed:
            continue
        info = managed[sym]
        price = prices.get(sym) or pos["price"]
        cur = pos["qty"] * price
        tgt = targets.get(sym)
        if tgt is None:
            props.append(
                _proposal(
                    "SELL",
                    "sell",
                    sym,
                    info["name"],
                    info["sector"],
                    pos["qty"],
                    price,
                    "Esce dalla strategia (settore o titolo non più idoneo): vendita completa",
                    current_value=cur,
                    target_value=0.0,
                )
            )
            continue
        # sopra o vicino al target: nessuna vendita parziale (si riparte da zero ogni giorno)

    held = {s for s, pos in positions.items() if pos["qty"] > 0}
    buys: list[dict] = []
    for sym, tgt in targets.items():
        price = prices.get(sym) or tgt["price"]
        cur = positions[sym]["qty"] * price if sym in held else 0.0
        delta = tgt["value"] - cur
        if delta < max(p.min_trade, p.drift_tolerance * tgt["value"]):
            continue
        qty = math.floor(delta / price)
        if qty >= 1:
            buys.append(
                _proposal(
                    "BUY",
                    "buy",
                    sym,
                    tgt["name"],
                    tgt["sector"],
                    qty,
                    price,
                    f"Sotto il target ({cur:,.0f}$ contro {tgt['value']:,.0f}$)"
                    + (f", punteggio {tgt['score']:.0f}" if tgt["score"] is not None else ""),
                    current_value=cur,
                    target_value=tgt["value"],
                )
            )

    sells_value = sum(x["value"] for x in props)
    spendable = max(0.0, cash + sells_value - p.cash_reserve * p.capital)
    need = sum(x["value"] for x in buys)
    if need > spendable:
        k = spendable / need if need else 0
        notes.append(
            f"Liquidità insufficiente: acquisti ridotti al {k:.0%} (disponibili {spendable:,.0f}$ su {need:,.0f}$)"
        )
        scaled = []
        for b in buys:
            q = math.floor(b["qty"] * k)
            if q >= 1:
                b |= {
                    "qty": q,
                    "value": q * b["price"],
                    "reason": b["reason"] + " (ridotto per liquidità)",
                }
                scaled.append(b)
        buys = scaled
    props += buys
    props.sort(key=lambda x: (x["priority"], -x["value"]))
    unmanaged = sorted(s for s in positions if s not in managed)
    if unmanaged:
        notes.append("Posizioni fuori dalla strategia (non toccate): " + ", ".join(unmanaged))
    return {"proposals": props, "notes": notes}
