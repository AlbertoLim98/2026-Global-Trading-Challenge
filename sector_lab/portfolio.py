"""Algoritmo di ribilanciamento giornaliero: dalle tabelle (settori + aziende) ai target e alle proposte.

Modulo puro (nessuna rete, nessun ordine): riceve tabelle, posizioni, prezzi e ATR e restituisce
l'allocazione obiettivo e l'elenco delle proposte di acquisto/vendita. Le proposte non vengono
eseguite qui: le decide l'utente una per una.

Regole (tutte modificabili in `Params`):
  1. Settori idonei: trend dell'ETF non ribassista e punteggio totale >= `min_sector_score`.
  2. Budget per settore proporzionale al punteggio, tetto `max_sector` del capitale; il resto è liquidità.
  3. Dentro il settore: solo le aziende della top 10 con trend relativo non ribassista e TWRR > 0,
     peso proporzionale al punteggio (modalità qualità) o al peso beta (modalità alto beta),
     tetto `max_stock` del capitale per titolo.
  4. Riserva di liquidità minima `cash_reserve`.
  5. Stop: posizione con perdita per azione > `atr_stop_mult` x ATR(14) -> vendita proposta con priorità
     massima (nessun divieto di riacquisto: se il titolo è ancora in classifica può essere ricomprato).
  6. Vendite: solo complete (stop, o titolo che esce dalla top 10 / dai filtri). Nessuna vendita parziale:
     le statistiche si rifanno ogni giorno, quindi un titolo sopra target resta com'è.
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
    max_sector: float = 0.25
    max_stock: float = 0.05
    min_sector_score: float = 50.0
    min_trade: float = 2_000.0
    drift_tolerance: float = 0.20
    atr_stop_mult: float = 1.0
    mode: str = "quality"  # "quality" | "beta"

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
    """Settori idonei con budget in dollari. Tutti i settori compaiono, con motivo se esclusi."""
    invest = p.capital * (1 - p.cash_reserve)
    raw: dict[str, float] = {}
    out = {}
    for r in sector_rows:
        total = (r.get("scores") or {}).get("total")
        ok = r["trend_label"] != "Ribassista" and total is not None and total >= p.min_sector_score
        out[r["symbol"]] = {
            "symbol": r["symbol"],
            "name": r["name"],
            "trend_label": r["trend_label"],
            "score": total,
            "eligible": ok,
            "budget": 0.0,
            "reason": ""
            if ok
            else (
                "trend ribassista"
                if r["trend_label"] == "Ribassista"
                else f"punteggio < {p.min_sector_score:g}"
            ),
        }
        if ok:
            raw[r["symbol"]] = (
                total - p.min_sector_score + 10
            )  # chi supera appena la soglia pesa poco
    for etf, amount in allocate(raw, invest, p.max_sector * p.capital).items():
        out[etf]["budget"] = amount
    return sorted(out.values(), key=lambda s: -(s["score"] or 0))


def build_targets(
    sector_rows: list[dict],
    stocks_by_etf: dict[str, dict],
    p: Params,
) -> dict:
    """Allocazione obiettivo: settori con budget e titoli con valore in dollari."""
    sectors = sector_budgets(sector_rows, p)
    targets: dict[str, dict] = {}
    for s in sectors:
        if not s["eligible"] or s["budget"] <= 0:
            continue
        top = (stocks_by_etf.get(s["symbol"]) or {}).get("top", [])
        cand = [r for r in top if stocks.is_eligible(r)]
        if p.mode == "beta":
            raw = {r["symbol"]: r.get("strategy_weight") or max(r["beta_1y"], 0.01) for r in cand}
        else:
            raw = {r["symbol"]: r["scores"]["total"] or 0 for r in cand}
        alloc = allocate(raw, s["budget"], p.max_stock * p.capital)
        s["n_candidates"] = len(cand)
        s["allocated"] = sum(alloc.values())
        for r in cand:
            v = alloc.get(r["symbol"], 0.0)
            if v > 0:
                targets[r["symbol"]] = {
                    "symbol": r["symbol"],
                    "name": r["name"],
                    "sector": s["symbol"],
                    "value": v,
                    "price": r["price"],
                    "score": r["scores"]["total"],
                    "beta": r.get("beta_1y"),
                }
    return {"sectors": sectors, "targets": targets}


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
        "kind": kind,  # STOP | SELL | BUY
        "side": side,
        "symbol": sym,
        "name": name,
        "sector": sector,
        "qty": qty,
        "price": price,
        "value": qty * price,
        "reason": reason,
        "priority": {"STOP": 0, "SELL": 1, "BUY": 2}[kind],
        **extra,
    }


def stop_hit(pos: dict, atr: float | None, mult: float) -> float | None:
    """Perdita per azione se supera mult x ATR, altrimenti None."""
    if not atr or atr <= 0:
        return None
    loss = pos["avg_entry"] - pos["price"]
    return loss if loss > mult * atr else None


def build_proposals(
    targets: dict[str, dict],
    positions: dict[str, dict],
    prices: dict[str, float],
    atrs: dict[str, float],
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
    stopped: set[str] = set()

    for sym, pos in positions.items():
        if sym not in managed:
            continue
        info = managed[sym]
        loss = stop_hit(pos, atrs.get(sym), p.atr_stop_mult)
        if loss is not None:
            atr = atrs[sym]
            stopped.add(sym)
            props.append(
                _proposal(
                    "STOP",
                    "sell",
                    sym,
                    info["name"],
                    info["sector"],
                    pos["qty"],
                    pos["price"],
                    f"Perdita {loss:.2f}$/azione ({loss / pos['avg_entry']:.1%}) > {p.atr_stop_mult:g} ATR "
                    f"({atr:.2f}$): vendita immediata consigliata",
                    atr=atr,
                    loss_per_share=loss,
                    avg_entry=pos["avg_entry"],
                )
            )

    for sym, pos in positions.items():
        if sym not in managed or sym in stopped:
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
        if sym in stopped:
            continue
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
