# 📧 Agente Email → Monday.com | AMR Recchia

Agente Python che monitora **ordini@amrrecchia.it** e inserisce automaticamente le email commerciali nella board **NEW COMMERCIALE** di Monday.com, classificandole con Gemini AI.

## 🔄 Come Funziona

```
Email ricevuta su ordini@amrrecchia.it
          ↓  IMAP SSL (Register.it)
    Gemini AI classifica:
    ├── SPAM/newsletter → ignora
    ├── ORDINE → crea item Monday.com
    ├── PREVENTIVO → crea item Monday.com
    └── AGGIORNAMENTO ORDINE → crea item Monday.com
          ↓
    Board "NEW COMMERCIALE" (Monday.com)
    → Azienda, Nome Progetto, Referente, Email, Tipo, Note
```

---

## ⚙️ Setup (Prima Volta)

### 1. Configurazione credenziali

```bash
# Copia il template
cp .env.example .env

# Modifica con le tue credenziali
nano .env
```

Devi compilare nel file `.env`:

| Variabile | Dove trovarlo |
|-----------|--------------|
| `IMAP_PASSWORD` | Pannello Register.it → Email → Casella ordini@amrrecchia.it |
| `MONDAY_API_TOKEN` | https://amr-srl.monday.com/p/admin/tokens |
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey |

### 2. Aggiungi le colonne alla board Monday.com

```bash
bash run.sh --setup-board
```

Questo aggiunge automaticamente: **Tipo Richiesta**, **Data Ricezione**, **Note Email**.

### 3. Test (dry-run, senza modifiche)

```bash
bash run.sh --test
```

Analizza le email ma **non crea item** e **non marca come lette**. Perfetto per testare.

---

## 🚀 Avvio

### Esecuzione singola (manuale)

```bash
bash run.sh
```

### Loop automatico (ogni 5 minuti)

```bash
bash run.sh --daemon
```

### Configura come cron job (avvio automatico sul Mac)

```bash
bash run.sh --cron
```

Questo configura un cron job che esegue l'agente ogni 5 minuti automaticamente, anche dopo i riavvii.

Per rimuovere il cron job:
```bash
bash run.sh --remove-cron
```

---

## 📊 Monitoraggio

### Vedere le statistiche

```bash
bash run.sh --stats
```

### Vedere i log in tempo reale

```bash
tail -f agent.log
```

---

## 🗂️ Struttura Progetto

```
email-agent/
├── .env               # Credenziali (NON su git)
├── .env.example       # Template credenziali
├── requirements.txt   # Dipendenze Python
├── agent.py           # 🤖 Script principale
├── email_reader.py    # 📬 Lettura IMAP
├── ai_classifier.py   # 🧠 Classificazione Gemini
├── monday_client.py   # 📋 API Monday.com
├── db.py              # 💾 Database anti-duplicati
├── run.sh             # 🚀 Script avvio
└── agent.log          # 📝 Log (auto-generato)
```

---

## 🔧 Configurazione Avanzata

Nel file `.env` puoi cambiare:

- `CHECK_INTERVAL_MINUTES=5` → frequenza controllo email
- `MARK_AS_READ=True` → marca le email come lette dopo l'elaborazione
- `LOG_LEVEL=DEBUG` → log più dettagliati per debugging
- `MONDAY_GROUP_ID=` → forza un gruppo specifico della board (lascia vuoto per usare il primo)

---

## ❓ Troubleshooting

**Errore IMAP:** Verifica che `IMAP_HOST=mail.register.it` sia corretto per Register.it.  
Prova anche: `imap.register.it` o controlla nel pannello Register.it → Email → Configurazione client.

**Errore Monday.com 401:** Il token API è scaduto o non valido. Rigeneralo da Monday.com → Profilo → Amministrazione → API.

**Gemini non classifica bene:** Aumenta i log con `LOG_LEVEL=DEBUG` e verifica le risposte nel log.
