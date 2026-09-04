"""
monday_client.py — Client Monday.com GraphQL API.
Gestisce la creazione di item sulla board NEW COMMERCIALE.
"""

import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
MONDAY_BOARD_ID = os.getenv("MONDAY_BOARD_ID", "2133436509")
MONDAY_GROUP_ID = os.getenv("MONDAY_GROUP_ID", "topics")  # "Richieste commerciali AMR"

API_URL = "https://api.monday.com/v2"

# ── Column IDs reali della board NEW COMMERCIALE ──────────────────────────────
# Scoperti via API il 2026-06-10
COL_NOME_PROGETTO    = "testo_mkn1sqb4"           # text
COL_REFERENTE        = "dup__of_nome_referente_mkn3gf63"  # text
COL_EMAIL            = "email_mkn1v37b"            # email
COL_RICHIESTA        = "testo_lungo_mkn1ydyz"      # long_text → corpo email
COL_SPECIFICHE       = "text_mkypgtb8"             # text → tipo richiesta
COL_STATO_PREVENTIVO = "color_mkn4s77r"            # status → Stato preventivo
COL_PRIORITA         = "color_mknssm0t"            # status → Priorità

# Mappa tipo email → specifiche (etichetta visibile su Monday.com)
TIPO_SPEC_MAP = {
    "ordine":                  "📦 Ordine Confermato",
    "modifica_ordine":         "✏️ Modifica Ordine",
    "preventivo":              "📋 Richiesta Preventivo",
    "aggiornamento_preventivo":"🔄 Aggiornamento Preventivo",
    "nuova_richiesta":         "💡 Nuova Richiesta",
    "aggiornamento_generico":  "📨 Aggiornamento",
    # legacy (backward compat)
    "aggiornamento_ordine":    "✏️ Modifica Ordine",
}



def _graphql(query: str, variables: Optional[Dict] = None) -> Dict:
    """Esegue una query GraphQL sulla Monday.com API."""
    if not MONDAY_API_TOKEN:
        raise ValueError("MONDAY_API_TOKEN non configurato nel file .env")

    headers = {
        "Authorization": MONDAY_API_TOKEN,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    response = requests.post(API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()

    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"Errori GraphQL Monday.com: {data['errors']}")
    return data.get("data", {})


def get_board_columns() -> List[Dict]:
    """
    Restituisce l'elenco delle colonne della board con id, titolo e tipo.
    Utile per mappare i nomi alle column ID.
    """
    query = """
    query ($board_id: [ID!]) {
      boards(ids: $board_id) {
        columns {
          id
          title
          type
          settings_str
        }
      }
    }
    """
    data = _graphql(query, {"board_id": [MONDAY_BOARD_ID]})
    boards = data.get("boards", [])
    if not boards:
        return []
    return boards[0].get("columns", [])


def get_board_groups() -> List[Dict]:
    """Restituisce i gruppi della board."""
    query = """
    query ($board_id: [ID!]) {
      boards(ids: $board_id) {
        groups {
          id
          title
        }
      }
    }
    """
    data = _graphql(query, {"board_id": [MONDAY_BOARD_ID]})
    boards = data.get("boards", [])
    if not boards:
        return []
    return boards[0].get("groups", [])


def get_default_group_id() -> str:
    """
    Restituisce l'ID del primo gruppo della board.
    Se MONDAY_GROUP_ID è configurato nel .env, usa quello.
    """
    if MONDAY_GROUP_ID:
        return MONDAY_GROUP_ID

    groups = get_board_groups()
    if not groups:
        raise RuntimeError("Nessun gruppo trovato nella board Monday.com")

    # Prendi il primo gruppo (di solito "Richieste commerciali AMR")
    group_id = groups[0]["id"]
    group_title = groups[0]["title"]
    logger.info("Usando il gruppo: '%s' (id: %s)", group_title, group_id)
    return group_id


def _build_column_values_direct(classification) -> Tuple[Dict, str]:
    """
    Costruisce il dizionario dei valori colonna usando gli ID colonna reali.
    Nessuna chiamata API aggiuntiva necessaria.
    """
    item_name = (classification.azienda or classification.email_mittente or "Nuova Richiesta")[:255]
    tipo_spec = TIPO_SPEC_MAP.get(classification.tipo, classification.tipo)

    column_values: Dict = {}

    # Nome Progetto (text)
    if classification.nome_progetto:
        column_values[COL_NOME_PROGETTO] = classification.nome_progetto[:500]

    # Referente (text)
    if classification.referente:
        column_values[COL_REFERENTE] = classification.referente[:200]

    # Email (email)
    if classification.email_mittente:
        column_values[COL_EMAIL] = {
            "email": classification.email_mittente,
            "text": classification.email_mittente,
        }

    # Richiesta/Note (long_text) → corpo sintetico email
    if classification.note:
        column_values[COL_RICHIESTA] = {"text": classification.note}

    # Specifiche richiesta (text) → tipo classificazione
    column_values[COL_SPECIFICHE] = tipo_spec

    return column_values, item_name


def create_item(classification) -> Optional[str]:
    """
    Crea un nuovo item sulla board Monday.com NEW COMMERCIALE.

    Args:
        classification: ClassificationResult con i dati estratti dall'email

    Returns:
        ID del nuovo item creato, o None in caso di errore.
    """
    try:
        group_id = MONDAY_GROUP_ID  # "topics" = Richieste commerciali AMR

        # Costruisce i valori colonna con ID diretti
        column_values, item_name = _build_column_values_direct(classification)

        query = """
        mutation CreateItem(
            $board_id: ID!,
            $group_id: String!,
            $item_name: String!,
            $column_values: JSON!
        ) {
          create_item(
            board_id: $board_id,
            group_id: $group_id,
            item_name: $item_name,
            column_values: $column_values
          ) {
            id
            name
          }
        }
        """
        variables = {
            "board_id": MONDAY_BOARD_ID,
            "group_id": group_id,
            "item_name": item_name,
            "column_values": json.dumps(column_values),
        }

        data = _graphql(query, variables)
        item_id = data.get("create_item", {}).get("id")
        item_name_returned = data.get("create_item", {}).get("name", "")

        if item_id:
            logger.info(
                "✅ Item creato su Monday.com: '%s' (id: %s)",
                item_name_returned, item_id
            )
            # Aggiunge un update con la sintesi completa
            _add_update_to_item(item_id, classification)
        else:
            logger.error("Creazione item fallita: risposta inattesa da Monday.com")

        return item_id

    except Exception as e:
        logger.error("Errore creazione item Monday.com: %s", e)
        return None




def _add_update_to_item(item_id: str, classification) -> None:
    """Aggiunge un update/commento all'item con il dettaglio dell'email."""
    try:
        note_text = (
            f"📧 **Email ricevuta** da {classification.referente} "
            f"({classification.email_mittente})\n\n"
            f"**Tipo**: {classification.tipo.replace('_', ' ').title()}\n"
            f"**Progetto**: {classification.nome_progetto}\n\n"
            f"**Sintesi**: {classification.note}"
        )
        query = """
        mutation AddUpdate($item_id: ID!, $body: String!) {
          create_update(item_id: $item_id, body: $body) {
            id
          }
        }
        """
        _graphql(query, {"item_id": item_id, "body": note_text})
        logger.debug("Update aggiunto all'item %s", item_id)
    except Exception as e:
        logger.warning("Impossibile aggiungere update all'item %s: %s", item_id, e)


def setup_board_columns() -> None:
    """
    Aggiunge le colonne mancanti alla board NEW COMMERCIALE:
    - Tipo Richiesta (Status)
    - Data Ricezione (Date)
    - Note Email (Long Text)
    """
    existing_columns = get_board_columns()
    existing_titles = {col["title"] for col in existing_columns}

    columns_to_add = []

    if "Tipo Richiesta" not in existing_titles:
        columns_to_add.append({
            "title": "Tipo Richiesta",
            "column_type": "status",
        })

    if "Data Ricezione" not in existing_titles:
        columns_to_add.append({
            "title": "Data Ricezione",
            "column_type": "date",
        })

    if "Note Email" not in existing_titles:
        columns_to_add.append({
            "title": "Note Email",
            "column_type": "long_text",
        })

    if not columns_to_add:
        logger.info("Tutte le colonne necessarie sono già presenti sulla board.")
        return

    for col in columns_to_add:
        try:
            query = """
            mutation AddColumn($board_id: ID!, $title: String!, $column_type: ColumnType!) {
              create_column(board_id: $board_id, title: $title, column_type: $column_type) {
                id
                title
              }
            }
            """
            data = _graphql(query, {
                "board_id": MONDAY_BOARD_ID,
                "title": col["title"],
                "column_type": col["column_type"],
            })
            new_col = data.get("create_column", {})
            logger.info("✅ Colonna creata: '%s' (id: %s)", new_col.get("title"), new_col.get("id"))
        except Exception as e:
            logger.error("Errore creazione colonna '%s': %s", col["title"], e)
