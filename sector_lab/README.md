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
