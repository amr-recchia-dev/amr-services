"""
voice_learning.py - Sistema di Apprendimento Continuo per Commesse AMR Recchia
=============================================================================
Memorizza le correzioni e le selezioni degli utenti (es. disambiguazione tra più
commesse dello stesso cliente come "Henoto") per migliorare progressivamente la
precisione del matching ad ogni utilizzo.
"""

from __future__ import annotations
import os
import json
import re
import logging

logger = logging.getLogger("voice_learning")

STORE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "alias_learning_store.json")


def _clean_keyword_phrase(text: str) -> str:
    """Isola i termini salienti della commessa rimuovendo parole di riempimento e verbi d'officina."""
    if not text:
        return ""
    t = text.lower()
    # Rimuovi orari e tempi
    t = re.sub(r"\b\d+\s*(?:ore|ora|h|minuti|min|mezza)\b", " ", t)
    # Rimuovi verbi e preposizioni comuni
    fillers = [
        "ho finito il", "ho finito la", "ho completato", "abbiamo finito", "abbiamo completato",
        "finito il", "finita la", "fatto il", "fatta la", "iniziato il", "in corso",
        "segna che", "scrivi che", "diciamo che", "tagga", "avvisa", "metti in pausa",
        "bloccato", "perché manca", "manca il", "sulla commessa", "sul progetto", "su commessa",
        "sul", "sulla", "sulle", "della", "dello", "degli", "dei", "del", "di", "da", "in", "per", "con", "su", "a"
    ]
    for f in fillers:
        t = re.sub(r"\b" + re.escape(f) + r"\b", " ", t)

    words = [w for w in re.split(r"[\s\-_/.,;:?!()]+", t) if len(w) >= 3]
    return " ".join(words).strip()


def load_learning_store() -> dict:
    """Carica il database persistente degli alias appresi."""
    if not os.path.exists(STORE_FILE):
        return {"aliases": {}, "corrections": []}
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Errore lettura store apprendimento {STORE_FILE}: {e}")
        return {"aliases": {}, "corrections": []}


def save_learning_store(data: dict):
    """Salva il database persistente degli alias appresi."""
    try:
        with open(STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Errore scrittura store apprendimento {STORE_FILE}: {e}")


def learn_project_alias(phrase: str, project_id: str, project_name: str, board_id: str = ""):
    """
    Registra l'associazione tra la frase pronunciata/scelta e la specifica commessa Monday.
    Aumenta il contatore di frequenza per consolidare l'apprendimento.
    """
    if not phrase or not project_id:
        return

    clean_key = _clean_keyword_phrase(phrase)
    if not clean_key or len(clean_key) < 3:
        clean_key = phrase.strip().lower()

    store = load_learning_store()
    aliases = store.setdefault("aliases", {})

    record = aliases.get(clean_key, {
        "project_id": str(project_id),
        "project_name": project_name,
        "board_id": str(board_id),
        "count": 0
    })

    # Aggiorna commessa se modificata dall'utente e incrementa il contatore di affidabilità
    record["project_id"] = str(project_id)
    record["project_name"] = project_name
    if board_id:
        record["board_id"] = str(board_id)
    record["count"] = record.get("count", 0) + 1

    aliases[clean_key] = record
    save_learning_store(store)
    logger.info(f"🧠 [LEARNING] Appresa associazione: '{clean_key}' ➔ '{project_name}' (#{project_id}, conf: {record['count']})")


def get_learned_match(text: str, projects: list) -> dict | None:
    """
    Verifica se il testo contiene un alias appreso precedentemente e restituisce la commessa esatta.
    """
    if not text or not projects:
        return None

    clean_input = _clean_keyword_phrase(text)
    store = load_learning_store()
    aliases = store.get("aliases", {})
    if not aliases:
        return None

    proj_by_id = {str(p["id"]): p for p in projects}

    # 1. Match esatto sulla chiave ripulita
    if clean_input in aliases:
        target_id = aliases[clean_input].get("project_id")
        if target_id and target_id in proj_by_id:
            logger.info(f"🎯 [LEARNING HIT] Match esatto appreso per '{clean_input}' ➔ item #{target_id}")
            return proj_by_id[target_id]

    # 2. Match di sottostringa su chiavi apprese con alta confidenza (count >= 1)
    text_lower = text.lower()
    for alias_key, info in sorted(aliases.items(), key=lambda x: len(x[0]), reverse=True):
        if len(alias_key) >= 4 and alias_key in text_lower:
            target_id = info.get("project_id")
            if target_id and target_id in proj_by_id:
                logger.info(f"🎯 [LEARNING HIT] Sottostringa appresa '{alias_key}' trovata nel testo ➔ #{target_id}")
                return proj_by_id[target_id]

    return None
