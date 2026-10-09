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

1. settori idonei: ETF non ribassista e punteggio totale >= 50; peso in proporzione al punteggio,
   **senza tetto per settore** (anche solo 2 settori vanno bene);
2. titoli: solo la top 10 del settore con trend relativo non ribassista e TWRR > 0; pesi in
   proporzione al punteggio (o al beta in modalità alto beta); **massimo 10% del capitale per titolo** (l'eccedenza passa agli altri);
   il **97% è sempre investito**: se i titoli idonei non bastano (servono almeno 10 titoli), si aggiungono
   prima altri settori non ribassisti, poi settori ribassisti, infine titoli della top 10 che non superano
   i filtri; ogni aggiunta è segnalata nelle note del ribilanciamento e nel Journal. Resta più liquidità
   solo se in assoluto non esistono abbastanza titoli;
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

**Uscita:** stop a **1,5 ATR** se il titolo è rialzista (sopra la media a 50 giorni) all'acquisto, **2,5 ATR**
se ribassista; il moltiplicatore è fissato all'acquisto e salvato nel journal. Vale solo per questa strategia
(Qualità e Alto beta restano a 1 ATR).

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

**Titoli non negoziabili:** a ogni ribilanciamento il programma chiede ad Alpaca l'elenco delle azioni
attive e negoziabili ed esclude dall'elenco quelle che non lo sono (fusi, ritirati, sospesi: es. un
errore "asset WBD is not active"). Vale anche per la scheda Aziende. Dopo un errore di questo tipo
rilancia il ribilanciamento: il titolo sarà escluso e il suo importo ridistribuito.

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
