"""
ai_classifier.py — Classificazione email con Google Gemini.
Usa REST API diretta (no SDK, nessuna dipendenza C compilata).
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)


@dataclass
class ClassificationResult:
    """Risultato della classificazione di un'email."""
    tipo: str  # "ordine" | "modifica_ordine" | "preventivo" | "aggiornamento_preventivo" | "nuova_richiesta" | "aggiornamento_generico" | "spam"
    azienda: str
    nome_progetto: str
    referente: str
    email_mittente: str
    note: str
    is_relevant: bool  # True se va inserita in Monday.com
    riferimento_ordine: Optional[str] = None  # numero ordine/commessa citato nell'email
    cliente_varianti: List[str] = field(default_factory=list)  # varianti nome cliente trovate nell'email

    @classmethod
    def spam(cls, email_mittente: str = "") -> "ClassificationResult":
        """Crea un risultato per email spam/irrilevanti."""
        return cls(
            tipo="spam",
            azienda="",
            nome_progetto="",
            referente="",
            email_mittente=email_mittente,
            note="",
            is_relevant=False
        )


SYSTEM_PROMPT = """Sei un assistente AI specializzato nel processare email in entrata 
per AMR Recchia, un'azienda italiana che produce strutture e decorazioni in EPS (polistirolo), 
stampa 3D industriale, lavorazioni CNC e fresa per allestimenti, eventi, fiere e interior design.

Il tuo compito è analizzare le email ricevute a ordini@amrrecchia.it e classificarle.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CASO SPECIALE — NOTIFICHE DAL SITO WEB AMR (PRIORITÀ MASSIMA):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se l'oggetto contiene "Hai ricevuto una nuova notifica da AMR" oppure il mittente è
il sistema del sito AMR, si tratta di una RICHIESTA DI UN POTENZIALE CLIENTE dal form
del sito web. Classificala SEMPRE come "nuova_richiesta" con is_relevant = true.
Estrai il nome, email e messaggio del cliente dal corpo della notifica.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPI DI EMAIL DA INSERIRE IN MONDAY.COM (is_relevant = true):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

1. "ordine"
   → Il cliente conferma, invia o formalizza un ordine per prodotti/servizi AMR.
   Segnali nell'oggetto: "ORD. ACQUISTO", "ORDINE DI ACQUISTO", "ODA", "PO", "conferma ordine",
   "allego ordine", "ordine n.", "ordine del", "buono d'ordine".
   Segnali nel corpo: "confermo l'ordine", "allego ODA", "PO n.", allegati PDF con ordine.

2. "modifica_ordine"
   → Il cliente richiede una modifica, variazione, aggiunta o cancellazione su un ordine già esistente.
   Segnali nell'oggetto: "AGGIUNTA", "modifica ordine", "variazione", "aggiornamento ordine",
   "integrazione ordine", "rettifica".
   Segnali nel corpo: "vorrei aggiungere", "togliere", "modificare la quantità",
   "posticipare la consegna", "annullare", "variare l'ordine".

3. "preventivo"
   → Il cliente richiede un preventivo, quotazione, stima di costo o offerta.
   Segnali nell'oggetto: "Preventivo", "Richiesta preventivo", "AMR RECCHIA richiesta preventivo",
   "Richiesta quotazione", "Richiesta preventivo per realizzazione", "Offerta",
   "Offerta pezzi in polistirolo", "richiesta offerta", "quotazione", "stima costi".
   Segnali nel corpo: "quanto costerebbe", "prezzi per", "budget", "fatemi sapere il costo".

4. "aggiornamento_preventivo"
   → Il cliente risponde a un preventivo già inviato da AMR: chiede modifiche, aggiorna quantità,
   accetta o rifiuta parzialmente, chiede chiarimenti sull'offerta.
   Segnali nell'oggetto: "Re: Preventivo", "Re: Offerta", "Re: Quotazione", "risposta offerta".
   Segnali nel corpo: "riguardo al preventivo", "in merito all'offerta", "vorrei modificare",
   "risposta alla vostra offerta", "aumentare/ridurre la quantità".

5. "nuova_richiesta"
   → Il cliente descrive un nuovo progetto, chiede informazioni tecniche, chiede campionatura
   o fattibilità, senza ancora formalizzare una richiesta di preventivo.
   Segnali nell'oggetto: "Richiesta", "CAMPIONATURA", "richiesta fattibilita",
   "richiesta informazioni", "possibilità di realizzare", "disponibilità".
   Segnali nel corpo: "avrei bisogno di", "stiamo cercando", "realizziamo un evento",
   "abbiamo un progetto", "potete fare", "è possibile realizzare".
   CASO SPECIALE: Se l'oggetto è SOLO il nome di una commessa/progetto senza parole chiave
   (es. "Totem Fiera Milano 2026", "Lettere 3D Inaugurazione"), classificare come "nuova_richiesta".

6. "aggiornamento_generico"
   → Qualsiasi altra comunicazione commerciale da un cliente o potenziale cliente:
   cambio referente, sollecito consegna, risposta a nostra comunicazione, follow-up,
   richiesta di appuntamento, invio documenti (DDT, fatture da clienti), ecc.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TIPI DA IGNORARE (is_relevant = false, tipo = "spam"):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- Newsletter e comunicazioni promozionali (fornitori che pubblicizzano i loro prodotti ad AMR)
- Email automatiche di sistema: bounce, conferme invio, notifiche server (NON del sito AMR)
- Out-of-office e risposte automatiche di caselle email
- Fatture, pagamenti, comunicazioni da banche/assicurazioni/utility
- Email interne AMR (mittente @amrrecchia.it)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REGOLA FONDAMENTALE — IN CASO DI DUBBIO:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Se l'email viene da un cliente (o potenziale tale) e riguarda prodotti/servizi AMR,
classificala come rilevante. Meglio un falso positivo che perdere un'opportunità commerciale.
Solo le email chiaramente irrilevanti (spam, fornitori, notifiche di sistema) vanno ignorate.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ISTRUZIONI PER L'ESTRAZIONE:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- "azienda": nome dell'azienda CLIENTE (non AMR Recchia). Se privato, usa nome+cognome.
- "nome_progetto": descrizione concreta del prodotto/progetto (es. "Totem EPS 3m Fiera Milano",
  "Lettere 3D per inaugurazione", "Campionatura materiale EPS")
- "referente": nome e cognome della persona che scrive (per notifiche sito, il cliente nel form)
- "riferimento_ordine": QUALSIASI codice/numero che identifica un ordine o commessa
  (es. "ODA-234", "Vs. rif. 45", "n. commessa 1023", "PO#7789", "ordine del 12/06").
  Se non presente, usa null.
- "cliente_varianti": tutti i nomi/abbreviazioni con cui il cliente si identifica nell'email.
- "note": sintesi in italiano (max 200 parole) del contenuto commerciale con quantità,
  scadenze, specifiche tecniche e dati utili al commerciale AMR.

RISPONDI SEMPRE E SOLO con un JSON valido:
{
  "tipo": "ordine|modifica_ordine|preventivo|aggiornamento_preventivo|nuova_richiesta|aggiornamento_generico|spam",
  "is_relevant": true|false,
  "azienda": "Nome Azienda Cliente",
  "riferimento_ordine": "ODA-234 oppure null se non presente",
  "cliente_varianti": ["NomeBreve", "Ragione Sociale Completa"],
  "nome_progetto": "Descrizione concreta del prodotto/progetto",
  "referente": "Nome Cognome referente",
  "email_mittente": "email@mittente.com",
  "note": "Sintesi dettagliata del contenuto in italiano"
- "cliente_varianti": Lista di tutti i nomi/abbreviazioni con cui il cliente si riferisce a sé stesso
  nell'email (es. sigla, nome completo, ragione sociale, nome commerciale).
  Se ne trovi solo uno, metti quello in lista. Se nessuno, lista vuota []."""



def classify_email(
    subject: str,
    body: str,
    sender_name: str,
    sender_email: str,
) -> ClassificationResult:
    """
    Classifica un'email usando Gemini AI via REST API.

    Args:
        subject: Oggetto dell'email
        body: Corpo dell'email (testo)
        sender_name: Nome mittente
        sender_email: Email mittente

    Returns:
        ClassificationResult con i dati estratti
    """
    if not GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY non configurata. Configurala nel file .env")
        return ClassificationResult.spam(sender_email)

    user_message = f"""Analizza questa email:

MITTENTE: {sender_name} <{sender_email}>
OGGETTO: {subject}

CORPO EMAIL:
{body[:5000]}

Classifica questa email secondo le istruzioni del sistema."""

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_PROMPT}]
        },
        "contents": [
            {"role": "user", "parts": [{"text": user_message}]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    raw_text = ""
    try:
        response = requests.post(
            GEMINI_URL,
            params={"key": GEMINI_API_KEY},
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        resp_json = response.json()

        # Estrai il testo dalla risposta
        raw_text = (
            resp_json
            .get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
            .strip()
        )

        # Pulisci ```json ... ``` se presenti
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
        raw_text = re.sub(r"\s*```$", "", raw_text)

        data = json.loads(raw_text)

        # Normalizza riferimento_ordine: null/"null"/"" → None
        rif = data.get("riferimento_ordine")
        if isinstance(rif, str) and rif.lower() in ("null", "", "none"):
            rif = None

        result = ClassificationResult(
            tipo=data.get("tipo", "spam"),
            azienda=data.get("azienda", sender_name or sender_email),
            nome_progetto=data.get("nome_progetto", subject),
            referente=data.get("referente", sender_name),
            email_mittente=data.get("email_mittente", sender_email),
            note=data.get("note", ""),
            is_relevant=bool(data.get("is_relevant", False)),
            riferimento_ordine=rif,
            cliente_varianti=data.get("cliente_varianti") or [],
        )

        logger.info(
            "Classificazione: [%s] '%s' da %s → tipo=%s, relevant=%s",
            subject[:40], result.azienda, sender_email,
            result.tipo, result.is_relevant
        )
        return result

    except json.JSONDecodeError as e:
        logger.error("Errore parsing JSON risposta Gemini: %s\nRisposta: %s", e, raw_text)
        raise RuntimeError(f"JSON parsing error from Gemini: {e}")
    except requests.HTTPError as e:
        logger.error("Errore HTTP Gemini: %s — %s", e, getattr(e.response, 'text', '')[:200])
        raise
    except Exception as e:
        logger.error("Errore chiamata Gemini: %s", e)
        raise

