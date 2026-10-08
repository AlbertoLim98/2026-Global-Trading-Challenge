# Sector Lab

Programma autonomo per analizzare gli 11 settori USA tramite gli ETF SPDR (XLK, XLF, XLI, XLY,
XLV, XLC, XLP, XLE, XLB, XLU, XLRE), con SPY come benchmark. Usa le API Alpaca (paper) e non
dipende dal resto del progetto `analystagents`.

Per ogni settore calcola:

| Pilastro | Indicatori |
|---|---|
| Trend | prezzo vs SMA50/SMA200, SMA50 vs SMA200, etichetta Rialzista / Misto / Ribassista |
| Momentum | rendimenti 1/3/6/12 mesi, RSI(14), forza relativa 3 e 6 mesi vs SPY |
| Volume | volume medio 20g / 90g, rapporto volume giorni su / giù, controvalore medio |
| Volatilità | volatilità annualizzata 20g e 60g, ATR(14)%, distanza dal massimo a 52 settimane |

Ogni pilastro ha un punteggio 0-100 (percentile del settore sugli altri 10; per la volatilità
un valore basso dà un punteggio alto) e c'è un totale. È una classifica relativa per orientarsi,
non un segnale operativo. La scheda "Portafoglio paper" mostra conto e posizioni in sola lettura.

## Migliori aziende per settore (scheda "Aziende")

Per ogni settore mostra le **10 migliori aziende** tra i titoli USA del fondo iShares MSCI ACWI
(`universe_us.csv`, 514 titoli con settore). Tutte le caratteristiche sono valutate sul
**prezzo relativo ad ACWI** (prezzo titolo / prezzo ACWI):

- **Momentum:** TWRR (time-weighted relative return) = (1 + rend. titolo) / (1 + rend. ACWI) - 1,
  su 1/3/6/12 mesi; il TWRR ponderato li media con pesi 4/3/2/1 (contano di più i periodi recenti).
- **Trend:** prezzo relativo vs sue medie a 50 e 200 giorni.
- **Volume:** rapporto volume 20g/90g del titolo diviso lo stesso rapporto di ACWI, e volume nei
  giorni in cui il relativo sale / scende.
- **Volatilità:** tracking error a 60g, distanza dal massimo relativo a 52 settimane, volatilità del titolo.

Il punteggio è il percentile dentro il settore (media dei quattro pilastri). I titoli con meno di
un anno di storico sono esclusi. Dalla scheda si scarica l'Excel del settore o di tutti i settori.
Per aggiornare l'elenco delle aziende: `uv run python sector_lab/build_universe.py file_ishares.xls`.

### Strategia alto beta

Nella scheda "Aziende" la modalità **Strategia alto beta** dà priorità ai beta più alti
(beta = cov(rend. titolo, rend. ACWI) / var(rend. ACWI), 1 anno di rendimenti giornalieri):

1. filtro: trend relativo non ribassista e TWRR ponderato > 0 (il titolo batte ACWI);
2. priorità: tra gli idonei, i 10 con beta più alto;
3. pesi nel settore proporzionali al beta, tetto 25% per titolo; si mostra il beta del portafoglio.

Se gli idonei sono meno di 10 la lista è più corta. Un beta alto amplifica guadagni e perdite
rispetto al mercato. Anche questa lista si scarica in Excel (colonne Beta e Peso).

## Ribilanciamento giornaliero e ordini (schede "Ribilancio" e "Journal")

Il pulsante **Avvia ribilanciamento giornaliero** rigenera le tabelle (settori e aziende, in
modalità qualità o alto beta), calcola l'allocazione obiettivo su un capitale di 1.000.000$ e
**invia da solo gli ordini** al conto **paper**: prima stop e vendite (si attende l'esito, fino a
15 s), poi gli acquisti, tutti a mercato. Non c'è approvazione per singola operazione: il pulsante
è l'unico passaggio umano. Se ci sono ordini ancora in corso il nuovo ribilanciamento non parte,
per non duplicarli.

**Operazioni non andate a buon fine** (ordine rifiutato, annullato, scaduto, eseguito solo in parte,
potere d'acquisto insufficiente, errore del broker): compaiono in un avviso rosso in cima alla
scheda con il motivo e la quantità non eseguita, e sono registrate nel Journal (evento FALLITA).
Il pulsante **Riposiziona a prezzo attuale** rimanda la quantità non eseguita con un ordine limite
al prezzo corrente. Un nuovo ribilanciamento archivia i fallimenti precedenti, perché ricalcola
tutto dalle posizioni reali.

Regole dell'algoritmo (`portfolio.py`, parametri in `Params`):

1. settori idonei: ETF non ribassista e punteggio totale >= 50; budget in proporzione al punteggio,
   massimo 25% del capitale per settore;
2. titoli: solo la top 10 del settore con trend relativo non ribassista e TWRR > 0; pesi in
   proporzione al punteggio (o al beta in modalità alto beta); massimo 5% del capitale per titolo;
3. liquidità minima 3%; gli acquisti si riducono se manca liquidità;
4. **stop**: perdita per azione > 1 ATR(14) -> vendita completa proposta con priorità massima
   (pulsante "Controlla stop ATR" per verificarlo durante la giornata senza rifare le tabelle);
   nessun divieto di riacquisto: se il titolo è ancora in classifica può essere ricomprato;
5. vendite solo complete (stop, o titolo che esce dalla top 10 o dai filtri), mai parziali: le
   statistiche si rifanno ogni giorno. Un titolo già in portafoglio sotto target viene integrato
   solo se lo scarto supera 2.000$ e il 20% del valore target;
6. le posizioni fuori dall'universo della strategia non vengono toccate.

Queste regole **riducono** il rischio di perdite, non lo eliminano: nessun algoritmo può garantire
guadagni senza perdite.

Il **Journal** (SQLite `sector_lab/journal.db`, solo accodamento: modifiche e cancellazioni sono
bloccate) registra ogni esecuzione con le tabelle usate e i target, ogni proposta, decisione,
ordine, esito ed errore; è consultabile nella scheda e scaricabile in Excel.

Sicurezza: ordini solo sul conto paper (endpoint verificato all'avvio, nessun percorso verso il conto
reale); le richieste che inviano ordini richiedono un'intestazione dedicata e un Host locale, così
un altro sito aperto nel browser non può inviarne. Con `--demo` il conto è simulato in memoria.

## Avvio

```bash
uv sync
# in .env: ALPACA_API_KEY e ALPACA_SECRET_KEY (chiavi PAPER)
uv run python sector_lab/server.py          # dati reali, http://127.0.0.1:8770
uv run python sector_lab/server.py --demo   # dati sintetici, senza chiavi
uv run pytest sector_lab                    # test (offline)
```

Il feed predefinito è `iex` (gratuito): i volumi sono solo quelli della borsa IEX, quindi i
rapporti sono confrontabili ma i valori assoluti no. Con un abbonamento dati usa `--feed sip`
o `ALPACA_DATA_FEED=sip`.

## Prossimo passo

Dai settori migliori alle aziende: una vista che, per il settore scelto, scarica i componenti
principali dell'ETF e applica le stesse metriche.
