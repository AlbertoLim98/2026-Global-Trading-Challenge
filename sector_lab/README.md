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
3. il beta sceglie i titoli; il peso nel settore segue il **punteggio**, senza tetto per titolo; si mostra
   il beta del settore (media pesata).

Se gli idonei sono meno di 10 la lista è più corta. Un beta alto amplifica guadagni e perdite
rispetto al mercato. Nella tabella ci sono due pesi: **Peso settore** (quota del titolo in proporzione al punteggio, senza tetti)
e **Peso portafoglio** (peso reale dell'algoritmo di ribilanciamento, dove il punteggio del titolo è
valutato insieme ai punteggi degli indici degli altri settori, cioè peso del settore ∝ punteggio dell'ETF, poi quota del titolo nel settore: tetto 10% per titolo, 97% investito). Per questo la
scheda, la prima volta, scarica anche gli altri settori. Anche questa lista si scarica in Excel.

## Ribilanciamento giornaliero e ordini (schede "Ribilancio" e "Journal")

Il pulsante **Avvia ribilanciamento giornaliero** rigenera le tabelle (settori e aziende, in
modalità qualità o alto beta), calcola l'allocazione obiettivo su un capitale di 1.000.000$ e
**invia da solo gli ordini** al conto **paper**: prima le vendite (si attende l'esito, fino a
15 s), poi gli acquisti, tutti a mercato. Non c'è approvazione per singola operazione: il pulsante
è l'unico passaggio umano. Se ci sono ordini ancora in corso (per esempio in coda a mercato chiuso) il nuovo ribilanciamento non parte,
per non duplicarli: il pulsante **Annulla ordini in corso** li annulla sul conto e sblocca il ribilanciamento.
Gli ordini che il conto non conosce più (conto azzerato, ricreato o cambiato) vengono sbloccati da soli.

**Operazioni non andate a buon fine** (ordine rifiutato, annullato, scaduto, eseguito solo in parte,
potere d'acquisto insufficiente, errore del broker): compaiono in un avviso rosso in cima alla
scheda con il motivo e la quantità non eseguita, e sono registrate nel Journal (evento FALLITA).
Il pulsante **Riposiziona a prezzo attuale** rimanda la quantità non eseguita con un ordine limite
al prezzo corrente. Un nuovo ribilanciamento archivia i fallimenti precedenti, perché ricalcola
tutto dalle posizioni reali.

Regole dell'algoritmo (`portfolio.py`, parametri in `Params`):

1. settori idonei: ETF non ribassista e punteggio totale >= 50; peso in proporzione al punteggio,
   **senza tetto per settore** (anche solo 2 settori vanno bene);
2. titoli: solo la top 10 del settore con trend relativo non ribassista e TWRR > 0; pesi in
   proporzione al punteggio (o al beta in modalità alto beta); **massimo 10% del capitale per titolo** (l'eccedenza passa agli altri);
   il **97% è sempre investito**: se i titoli idonei non bastano (servono almeno 10 titoli), si aggiungono
   prima altri settori non ribassisti, poi settori ribassisti, infine titoli della top 10 che non superano
   i filtri; ogni aggiunta è segnalata nelle note del ribilanciamento e nel Journal. Resta più liquidità
   solo se in assoluto non esistono abbastanza titoli;
3. liquidità minima 3%; gli acquisti si riducono se manca liquidità;
4. **nessuno stop né regola d'uscita** (ATR o altro): si esce solo col ribilanciamento, quando un titolo non
   è più selezionato; nessun divieto di riacquisto;
5. vendite solo complete (titolo che esce dalla top 10 o dai filtri), mai parziali: le
   statistiche si rifanno ogni giorno. Un titolo già in portafoglio sotto target viene integrato
   solo se lo scarto supera 2.000$ e il 20% del valore target;
6. le posizioni fuori dall'universo della strategia non vengono toccate.

Queste regole **riducono** il rischio di perdite, non lo eliminano: nessun algoritmo può garantire
guadagni senza perdite.

La scheda **Portafoglio paper** mostra la composizione (titoli con peso, P/L di oggi e totale; liquidità;
ripartizione per settore), il guadagno/perdita dall'ultima riallocazione, per ogni riallocazione (patrimonio al via di una e della
successiva) e da apertura portafoglio (patrimonio al primo ribilanciamento registrato) e, in un elenco a parte, i movimenti eseguiti oggi (giorno di borsa di New York).

### Strategia a 1 giorno (scheda "1 giorno")

Terza strategia (`--strategy short`, o "Breve termine" nel selettore di Ribilancio) con uno **score di
breve periodo** su tutto l'universo, media di 7 pilastri (percentile 0-100 tra i titoli dello stesso giorno):

| Pilastro | Cosa misura (segno ipotizzato) |
|---|---|
| RSI 2-3g + trend | RSI a 2 e 3 giorni; conta solo sopra la media a 50 giorni (ipervenduto = punteggio alto), sotto è neutro |
| Rendimento / ATR | rendimento di ieri e della settimana diviso per l'ATR (chi è sceso di più rimbalza) |
| Volume + direzione | volume di ieri / media dei 20 giorni precedenti x segno del rendimento (continuazione) |
| Candela | chiusura nel range del giorno (alta = continuazione) e gap notturno / ATR (il gap si chiude) |
| Forza relativa | rendimento a 3 giorni di titolo meno settore (rientra) e di settore meno SPY (momentum) |
| Contesto di mercato | beta a 60 giorni x (+1 se SPY sopra la media a 50 giorni, -1 se sotto) |
| Regime di volatilità | -(ATR 5g / ATR 20g): l'espansione penalizza |

Si tengono i **20 titoli** con lo score più alto (peso in proporzione, massimo 10% per titolo, 97% investito).
Si usa l'ultima seduta completa (la barra di oggi non completa viene scartata).

**Uscita:** nessuna regola d'uscita (niente stop ATR): si esce solo con il ribilanciamento quotidiano, quando
un titolo non è più tra i 20 migliori. L'ATR resta solo come unità di misura dentro gli indicatori dello score.

I segni degli indicatori sono **ipotesi**. Il pulsante **Valida gli indicatori sullo storico** (e l'Excel con
validazione) misura, su circa un anno di dati giornalieri, quanto ogni indicatore prevede il rendimento del
giorno dopo (correlazione di rango media, t, % giorni positivi, spread tra il quinto più alto e più basso,
anche al netto del beta) e quanto renderebbe tenere ogni giorno i 20 migliori, con costi. Un indicatore con
t sotto 2 non è distinguibile dal caso; un t molto negativo suggerisce di invertire il segno (in
`shortterm.py`, `build_panel`). Il calcolo è lo stesso del punteggio di oggi e non guarda mai al futuro.

### Due portafogli con strategie diverse

Ogni portafoglio è un'istanza del programma con **conto Alpaca paper, journal e strategia propri**
(le posizioni dei due portafogli non si mescolano). La strategia (`beta` = alto beta, `quality` =
qualità, `short` = breve termine a 1 giorno) è fissa e compare nell'intestazione; ogni evento del journal ne porta nome e strategia.

```bash
# ognuno con il proprio file di chiavi (stesse variabili di .env.example) e la propria porta
uv run python sector_lab/server.py --portfolio "Portafoglio 17" --strategy beta    --env .env.p17 --port 8770
uv run python sector_lab/server.py --portfolio "Portafoglio 18" --strategy quality --env .env.p18 --port 8771
```

Il journal di ciascuno è `sector_lab/journal_<nome>.db` (cambialo con `--journal`). Per tenere un
journal già esistente, passa il suo percorso con `--journal`.

**Un journal = un conto Alpaca.** Non cambiare le chiavi nel `.env` (togliendo e rimettendo `#`) usando lo
stesso journal: i dati di due portafogli si mescolano. Il programma ora lega ogni journal al numero del conto
Alpaca: se avvii con chiavi di un altro conto, senza `--journal` usa `journal_<numero del conto>.db`; con un
`--journal` o un `--portfolio` che appartiene a un altro conto si ferma con un messaggio. Il modo giusto è un file
di chiavi e un'istanza per portafoglio (`--env .env.p17`, `--env .env.p18`, porte diverse). I file `.env.*`
non vanno mai nel repository (sono nel `.gitignore`).

**Separare un journal già mescolato** (l'originale non cambia): `journal_tool.py split` copia le esecuzioni scelte
in un journal nuovo, con portafoglio e strategia:

```bash
uv run python sector_lab/journal_tool.py split --db sector_lab/journal.db --to sector_lab/journal_p18.db --run-id 20261008-180425-8dc4 --portfolio "Portafoglio 18" --strategy quality
```

Per **riunire** in un solo journal le esecuzioni di uno stesso portafoglio finite in file diversi usa `--append`
(le esecuzioni già presenti si saltano e un conto diverso viene rifiutato). `compare_portfolios.py` accetta più
volte `--journal-a` / `--journal-b` se un portafoglio è su più file.

`--run-id` si ripete per più esecuzioni; se il file di destinazione esiste già (un tentativo precedente) il comando
si ferma: aggiungi `--overwrite` per sostituirlo. Con `list` vedi le esecuzioni con data e ora (UTC).

**Correggere i dati precedenti.** Il journal è in sola aggiunta, quindi le righe vecchie non si
modificano: si registra un evento di riclassificazione che interfaccia ed Excel applicano a tutte le
righe di quelle esecuzioni (vince l'ultima assegnazione). Corregge l'etichetta, non ciò che è stato
eseguito sul conto.

```bash
# una riga per comando (su PowerShell il carattere \ per andare a capo non funziona: usa una riga sola)
uv run python sector_lab/journal_tool.py list --db sector_lab/journal.db
uv run python sector_lab/journal_tool.py relabel --db sector_lab/journal.db --hour 17 --portfolio "Portafoglio 17" --strategy beta --dry-run
uv run python sector_lab/journal_tool.py relabel --db sector_lab/journal.db --hour 17 --portfolio "Portafoglio 17" --strategy beta
uv run python sector_lab/journal_tool.py relabel --db sector_lab/journal.db --hour 18 --portfolio "Portafoglio 18" --strategy quality
```

Selezione: `--hour` (ora di avvio di esecuzioni **già registrate**, letta in UTC come nell'Excel; con
`--tz` si cambia fuso, con `--date AAAA-MM-GG` si limita a un giorno), `--run-id` (ripetibile) oppure
`--all`. L'ora serve solo a scegliere righe già scritte: il programma non pianifica né forza mai i
ribilanciamenti, che partono solo quando premi il pulsante.

### Confronto tra due portafogli (`compare_portfolios.py`)

Script in sola lettura che confronta due portafogli, ognuno con il proprio file di chiavi:

```bash
uv run python sector_lab/compare_portfolios.py --env-a .env.p17 --env-b .env.p18 --name-a "Portafoglio 17" --name-b "Portafoglio 18" --period 1M
```

Legge per ciascuno conto, posizioni e storico del patrimonio (Alpaca) e, con `--journal-a/--journal-b`, un
riepilogo del journal (strategia, ribilanciamenti, operazioni fallite). Stampa e scrive un Excel
(`confronto_portafogli_AAAA-MM-GG.xlsx`, o `--out`) con rendimento, volatilità e perdita massima sulla
finestra in comune, confronto con SPY, titoli in comune e sovrapposizione dei pesi, composizione per settore
e andamento in base 100. `--demo` prova lo script con due conti simulati.

**Portafogli partiti in momenti diversi:** passa i due journal e un confronto intraday, così la finestra parte dal
primo ordine eseguito del portafoglio più recente (`--from` per scegliere un altro inizio, in UTC):

```bash
uv run python sector_lab/compare_portfolios.py --env-a .env.p17 --env-b .env.p18 --name-a "Portafoglio beta" --name-b "Portafoglio 1g" --journal-a sector_lab/journal_beta.db --journal-b sector_lab/journal_1g.db --period 1W --timeframe 15Min
```

La volatilità è annualizzata in base alla granularità scelta (1D, 1H, 15Min, 5Min, 1Min). Con pochi giorni di storico le
differenze possono essere solo rumore.

**Titoli non negoziabili:** a ogni ribilanciamento il programma chiede ad Alpaca l'elenco delle azioni
attive e negoziabili ed esclude dall'elenco quelle che non lo sono (fusi, ritirati, sospesi: es. un
errore "asset WBD is not active"). Vale anche per la scheda Aziende. Dopo un errore di questo tipo
rilancia il ribilanciamento: il titolo sarà escluso e il suo importo ridistribuito.

**Su che portafoglio e con quale strategia.** Ogni evento del journal e ogni operazione portano il nome del
portafoglio (quello dato con `--portfolio`, altrimenti `Conto <numero del conto Alpaca>`) e la strategia usata per
calcolare le posizioni di quel ribilanciamento (`quality`, `beta` o `short`); si vedono nelle schede Journal,
Portafoglio e Ribilancio e nell'Excel.

**Indicatori nel journal.** Ad ogni ribilanciamento il journal salva, in una tabella dedicata, tutti i valori che hanno
composto i punteggi: per la strategia a 1 giorno i 9 indicatori grezzi e i 7 pilastri di tutti i ~500 titoli; per
Qualità e Alto beta tutti gli indicatori di settori (11) e aziende (top 10 per settore); per ognuno se è stato
selezionato e con quale valore target. Nell'Excel del journal sono nel foglio **Indicatori**. Per i ribilanciamenti
vecchi lo stesso foglio si ricava dai dati che il RUN aveva registrato (parziali: sono indicati come "dal RUN").

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
