"""
user_auth.py - Gestione Utenti, PIN e Permessi per Assistente Vocale AMR Recchia
================================================================================
Definisce l'elenco degli utenti aziendali, i rispettivi PIN (sincronizzati con Lista_PIN
del foglio presenze) e i permessi di accesso per le schede Monday:
- Utenti Commerciali/Direzione (ordini@, info@, amministrazione@, riccardo.g@):
  accesso completo a COMMERCIALE, AMMINISTRAZIONE, APPUNTAMENTI e tutte le schede di produzione/officina.
- Utenti Officina e Reparti:
  accesso a tutte le schede operative (PRODUZIONE, GESTIONE PROGETTI, PROGETTAZIONE,
  INSTALLAZIONI, INVENTARIO MATERIALI, PALLET EPS, CONTESTAZIONI).
"""

from __future__ import annotations
import os
from dotenv import load_dotenv

load_dotenv()

# Master fallback PIN
AMR_MASTER_PIN = os.getenv("AMR_ACCESS_PIN", "2026")

AUTH_USERS = {
    # ── 1. Commerciale & Direzione (Accesso Totale incl. COMMERCIALE e AMMINISTRAZIONE) ──
    "4921": {
        "pin": "4921",
        "name": "Alessandro Recchia",
        "email": "amministrazione@amrrecchia.it",
        "department": "Amministrazione",
        "is_commercial": True,
        "monday_id": "71478506",
        "label": "Direzione / Amministrazione"
    },
    "6198": {
        "pin": "6198",
        "name": "Gary Innocente",
        "email": "ordini@amrrecchia.it",
        "department": "Commerciale",
        "is_commercial": True,
        "monday_id": "71489364",
        "label": "Ufficio Commerciale & Ordini"
    },
    "9276": {
        "pin": "9276",
        "name": "Massimo Recchia",
        "email": "info@amrrecchia.it",
        "department": "Commerciale",
        "is_commercial": True,
        "monday_id": "71533482",
        "label": "Commerciale & Relazioni Esterne"
    },
    "8359": {
        "pin": "8359",
        "name": "Riccardo Gazzola",
        "email": "riccardo.g@zaffiroweb.com",
        "department": "Digital & Controllo Qualità",
        "is_commercial": True,
        "monday_id": "71533914",
        "label": "Tech Lead & Controllo Qualità"
    },

    # ── 2. Dipendenti Reparti Operativi & Officina ──
    "8173": {
        "pin": "8173",
        "name": "Andrea Moscon",
        "email": "andreamoscon81@gmail.com",
        "department": "Finiture",
        "is_commercial": False,
        "monday_id": "71533953",
        "label": "Reparto Finiture"
    },
    "2365": {
        "pin": "2365",
        "name": "Antonio Ambrosino",
        "email": "hse@amrrecchia.it",
        "department": "Responsabile Sicurezza",
        "is_commercial": False,
        "monday_id": "78115008",
        "label": "HSE & Sicurezza"
    },
    "7542": {
        "pin": "7542",
        "name": "Francesca Semenzin",
        "email": "sefra15@hotmail.com",
        "department": "Finiture",
        "is_commercial": False,
        "monday_id": None,
        "label": "Reparto Finiture"
    },
    "3824": {
        "pin": "3824",
        "name": "Giovanni Fregona",
        "email": "giovi.fregona@gmail.com",
        "department": "Taglio e Fresa",
        "is_commercial": False,
        "monday_id": None,
        "label": "Reparto Taglio e Fresa"
    },
    "5419": {
        "pin": "5419",
        "name": "Jamal Sriti",
        "email": "jamalsriti85@gmail.com",
        "department": "Verniciatura / Finiture",
        "is_commercial": False,
        "monday_id": "78744209",
        "label": "Verniciatura & Finiture"
    },
    "4682": {
        "pin": "4682",
        "name": "Maurizio Nordio",
        "email": "produzione@amrrecchia.it",
        "department": "Produzione / Finiture",
        "is_commercial": False,
        "monday_id": "71533503",
        "label": "Produzione & Finiture"
    },
    "1537": {
        "pin": "1537",
        "name": "Mauro Piccolotto",
        "email": "mauropiccolotto@gmail.com",
        "department": "Taglio e Fresa",
        "is_commercial": False,
        "monday_id": None,
        "label": "Reparto Taglio e Fresa"
    },
    "4839": {
        "pin": "4839",
        "name": "Mohammed Draij",
        "email": "draij_1995@live.it",
        "department": "Taglio e Fresa",
        "is_commercial": False,
        "monday_id": None,
        "label": "Reparto Taglio e Fresa"
    },

    # ── 3. PIN Master / Condiviso Officina ──
    str(AMR_MASTER_PIN).strip(): {
        "pin": str(AMR_MASTER_PIN).strip(),
        "name": "Operatore Officina",
        "email": "officina@amrrecchia.it",
        "department": "Officina Produzione",
        "is_commercial": False,
        "monday_id": None,
        "label": "Officina Produzione AMR"
    }
}


# ID Ufficiali Schede Monday (Workspace AMR)
BOARD_COMMERCIALE = "1865049112"
BOARD_PRODUZIONE = "1865050352"
BOARD_GESTIONE_PROGETTI = "1865197409"
BOARD_APPUNTAMENTI = "1857040251"
BOARD_PROGETTAZIONE = "1988908927"
BOARD_INVENTARIO = "1919440196"
BOARD_AMMINISTRAZIONE = "1865824381"
BOARD_CONTESTAZIONI = "1811152353"
BOARD_INSTALLAZIONI = "1863989733"
BOARD_PALLET_EPS = "1986638981"

BOARD_TAGLIO = "5086546323"
BOARD_FINITURE = "5088215890"

# Schede riservate che richiedono autorizzazione commerciale/direzione
RESTRICTED_BOARDS = {
    BOARD_COMMERCIALE: "COMMERCIALE",
    BOARD_AMMINISTRAZIONE: "AMMINISTRAZIONE",
    BOARD_APPUNTAMENTI: "APPUNTAMENTI"
}

ALL_ACTIVE_BOARDS = {
    BOARD_COMMERCIALE: "COMMERCIALE",
    BOARD_PRODUZIONE: "PRODUZIONE",
    BOARD_GESTIONE_PROGETTI: "GESTIONE PROGETTI",
    BOARD_APPUNTAMENTI: "APPUNTAMENTI",
    BOARD_PROGETTAZIONE: "PROGETTAZIONE",
    BOARD_INVENTARIO: "INVENTARIO MATERIALI / PRODOTTI",
    BOARD_AMMINISTRAZIONE: "AMMINISTRAZIONE",
    BOARD_CONTESTAZIONI: "CONTESTAZIONI",
    BOARD_INSTALLAZIONI: "INSTALLAZIONI",
    BOARD_PALLET_EPS: "PALLET EPS COMPATTATO 2025"
}


def get_user_by_pin(pin: str) -> dict | None:
    """Restituisce le informazioni del profilo utente per il PIN specificato."""
    if not pin:
        return None
    p = str(pin).strip()
    return AUTH_USERS.get(p)


def is_board_restricted(board_id: str) -> bool:
    """Indica se la scheda indicata richiede permessi commerciali/direzionali."""
    return str(board_id) in RESTRICTED_BOARDS


def can_user_access_board(user: dict | None, board_id: str) -> tuple[bool, str]:
    """
    Verifica se l'utente ha diritto ad accedere e modificare la scheda specificata.
    Ritorna (True, "") se consentito, o (False, motivazione) se respinto.
    """
    if not is_board_restricted(board_id):
        return True, ""
    
    board_name = RESTRICTED_BOARDS.get(str(board_id), "RISERVATA")
    if not user:
        return False, f"La scheda {board_name} richiede autenticazione con PIN aziendale commerciale."
    
    if user.get("is_commercial"):
        return True, ""
    
    user_name = user.get("name", "Operatore")
    return False, (
        f"Accesso limitato: la scheda {board_name} è riservata all'ufficio commerciale e direzione "
        f"(ordini@, info@, amministrazione@, riccardo.g@). L'utente '{user_name}' non è autorizzato a modificarla."
    )
