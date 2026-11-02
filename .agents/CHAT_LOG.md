# Diario Condiviso delle Chat in Tempo Reale — email-agent (Team Soci (Stanza Soci))

Questo file sincronizza in tempo reale tutte le richieste, i nuovi file e le immagini caricate nelle chat di Antigravity sui diversi Mac collegati a questo progetto.

---

- **[2026-10-27 10:48] (Riccardo Gazzola / shallo)**:
  1. Attivata sincronizzazione automatica in tempo reale (Auto-Pull / Auto-Push), cartella `shared_uploads/` e diario condiviso delle chat per **Team Soci (Stanza Soci)** (`antigravity-team:/home/team/projects/email-agent`).
  2. Documentata l'architettura completa del progetto in `.agents/MEMORY.md` (Agente Email AI Gemini, Server Webhook Render `amr-services`, Assistente Vocale RBAC, Timbrature/Presenze, Sincronizzazione Reparti Monday e Bonifica Sicurezza Phishing *Email-to-Board*).
  3. Integrata in `.agents/MEMORY.md` l'analisi Pro/Contro e la stima ore/costi (a `€ 95,00/ora + IVA`) per l'eventuale ricostruzione di Monday.com tramite Web App + Mobile PWA custom per AMR Recchia S.r.l.
  4. Inviata email di istruzioni complete al socio **Francesco Silvello** (`francesco.s@zaffiroweb.com`, CC `riccardo.g@zaffiroweb.com`) tramite `send_instructions_francesco.py`.

- **[2026-11-02 11:30] (Riccardo Gazzola / shallo)**:
  1. **Fix Inserimento Email su Monday.com (`commessa_matcher.py`, `ai_classifier.py`, `agent.py`, `db.py`)**:
     - Risolto il problema per cui nuove richieste di preventivo/ordine inviate da clienti già presenti su Monday (es. *Posterland*, *Xpose Forpress*, *Grafiche Antiga*) venivano accodate come commenti (`Update`) dentro vecchie righe di altri progetti dello stesso cliente invece di creare una nuova riga. Ora `commessa_matcher.py` verifica i token specifici del `Nome Progetto` e crea sempre una nuova riga su `COMMERCIALE` (`1865049112`) e `NEW COMMERCIALE` (`2133436509`) per ogni nuovo progetto.
     - Aggiornato `SYSTEM_PROMPT` in `ai_classifier.py` affinché le email di clienti inoltrate (`Fwd:` / `I:`) da caselle interne `@amrrecchia.it` (`info@`, `amministrazione@`, `produzione@`) non vengano mai scartate come email interne, ma vengano estratte con i dati del cliente originale.
     - Spostato `processed_emails.db` in `db.py` sul disco persistente `/var/data/timbrature/processed_emails.db` su Render con migrazione automatica dello storico.
  2. **Sistema di Backup Automatico Multi-Destinazione Ogni 4 Ore (`backup_manager.py`, `webhook_server.py`)**:
     - Creato `backup_manager.py` ed esposti gli endpoint protetti `/api/backup/status`, `/api/backup/run` e `/api/backup/download`.
     - Salvataggio completo ogni 4 ore (`00:05, 04:05, 08:05, 12:05, 16:05, 20:05`) di: (1) Registro Sicurezza Timbrature & Dispositivi (`registro_sicurezza.db` + CSV + JSON), (2) Database Email Processate (`processed_emails.db` + CSV), (3) Snapshot completo di tutte le 9 board Monday.com (`monday_snapshot.json`), (4) Riepilogo Presenze del mese (`presenze_snapshot.json`).
     - Salvato simultaneamente su **3 destinazioni indipendenti**: Disco Persistente Render (`/var/data/backups/`), Google Drive `amrrecchia2@gmail.com` (`Backup Automatici Server AMR (Ogni 4h)/YYYY-MM/YYYY-MM-DD`) e Server Secondario Hetzner (`46.62.254.242` in `/home/team/backups/amr-render/`).

