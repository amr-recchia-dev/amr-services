"""
commessa_matcher.py — Cerca commesse esistenti su Monday.com
per aggiornare invece di creare duplicati.

Strategia di ricerca (in ordine di priorità):
  1. Match esatto/fuzzy per numero ordine/commessa nell'email
  2. Match fuzzy per nome cliente (soglia >= MATCH_THRESHOLD)
  3. Nessun match → restituisce MatchResult(found=False)

Board cercate (in ordine):
  - NEW COMMERCIALE   (BOARD_COMMERCIALE)
  - GESTIONE PROGETTI NEW (BOARD_PROGETTI)
"""

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
BOARD_COMMERCIALE = os.getenv("MONDAY_BOARD_ID", "2133436509")
BOARD_PROGETTI = "2136092569"

# Soglia minima per il fuzzy match sul nome cliente (0–1)
MATCH_THRESHOLD = 0.80

API_URL = "https://api.monday.com/v2"

# ID della colonna 'N° Commessa' su ciascuna board.
COL_NUM_COMMESSA_COMMERCIALE: Optional[str] = "text_mm51yvbk"
COL_NUM_COMMESSA_PROGETTI: Optional[str] = "text_mm51yk45"

# Cache per gli ID colonna N° Commessa (evita query ripetute)
_col_cache: Dict[str, Optional[str]] = {
    f"num_commessa_{BOARD_COMMERCIALE}": "text_mm51yvbk",
    f"num_commessa_{BOARD_PROGETTI}": "text_mm51yk45"
}


# Percorso opzionale del file di mapping nome_email → nome_monday
import pathlib
CLIENT_PATTERNS_PATH = pathlib.Path(__file__).parent / "client_patterns.json"

# Knowledge base clienti (generata da build_knowledge_base.py)
KNOWLEDGE_BASE_PATH = pathlib.Path(__file__).parent / "client_knowledge_base.json"

# Cache in-memory della knowledge base
_knowledge_base_cache: dict = {}
_kb_loaded: bool = False


# ── Struttura risultato ────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """Risultato della ricerca di una commessa su Monday.com."""
    found: bool
    item_id: Optional[str] = None
    item_name: Optional[str] = None
    board_id: Optional[str] = None
    confidence: float = 0.0
    ambiguous: bool = False       # True se ci sono più candidati con score simile
    candidates: List[Dict] = field(default_factory=list)


# ── Helpers interni ────────────────────────────────────────────────────────────

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


def _fuzzy_score(a: str, b: str) -> float:
    """Calcola la similarità tra due stringhe (0.0–1.0)."""
    return difflib.SequenceMatcher(
        None,
        a.lower().strip(),
        b.lower().strip()
    ).ratio()


def _normalize_ref(ref: str) -> str:
    """Normalizza un numero di riferimento rimuovendo spazi e caratteri speciali."""
    return re.sub(r"[\s\-_/.]", "", ref).upper()


def _load_client_patterns() -> Dict[str, str]:
    """
    Carica il file client_patterns.json (se esiste).
    Formato: {"nome_email": "nome_monday", ...}
    """
    if CLIENT_PATTERNS_PATH.exists():
        try:
            with open(CLIENT_PATTERNS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Impossibile leggere client_patterns.json: %s", e)
    return {}


def _load_knowledge_base() -> dict:
    """
    Carica la knowledge base clienti generata da build_knowledge_base.py.
    Usa una cache in-memory per evitare letture ripetute.
    Formato: { "NOME_FOLDER": { "nome_cliente": ..., "nomi_usati": [...], ... } }
    """
    global _knowledge_base_cache, _kb_loaded
    if _kb_loaded:
        return _knowledge_base_cache

    if KNOWLEDGE_BASE_PATH.exists():
        try:
            with open(KNOWLEDGE_BASE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _knowledge_base_cache = data.get("clients", {})
            _kb_loaded = True
            logger.info(
                "📚 Knowledge base caricata: %d clienti da %s",
                len(_knowledge_base_cache), KNOWLEDGE_BASE_PATH.name
            )
        except Exception as e:
            logger.warning("Impossibile leggere client_knowledge_base.json: %s", e)
            _knowledge_base_cache = {}
            _kb_loaded = True
    else:
        logger.debug("client_knowledge_base.json non trovato — fuzzy match standard")
        _knowledge_base_cache = {}
        _kb_loaded = True

    return _knowledge_base_cache


def _expand_variants_from_kb(search_names: List[str]) -> List[str]:
    """
    Arricchisce la lista di varianti con i 'nomi_usati' dalla knowledge base.
    Per ogni nome in search_names, cerca entry nella KB che abbiano
    quel nome in 'nomi_usati' e aggiunge tutti gli altri alias.
    """
    kb = _load_knowledge_base()
    if not kb:
        return search_names

    extra_names = set()
    search_lower = {n.lower() for n in search_names}

    for _folder, entry in kb.items():
        # Tutti i nomi/alias di questa entry KB
        kb_aliases = (
            [entry.get("nome_cliente", "")]
            + entry.get("nomi_usati", [])
        )
        kb_aliases_lower = {a.lower() for a in kb_aliases if a}

        # Se c'è sovrapposizione con i nomi cercati, aggiungi tutti gli alias
        if search_lower & kb_aliases_lower:
            extra_names.update(a for a in kb_aliases if a)

    # Unisci, eliminando duplicati (case-insensitive)
    combined = list(search_names)
    seen_lower = {n.lower() for n in combined}
    for name in extra_names:
        if name.lower() not in seen_lower:
            combined.append(name)
            seen_lower.add(name.lower())

    if len(combined) > len(search_names):
        logger.debug(
            "🔗 KB espande varianti: %s → %s",
            search_names[:3], combined[:6]
        )

    return combined


def _get_all_items(board_id: str) -> List[Dict]:
    """
    Recupera tutti gli item di una board Monday.com (nome + id + colonne).
    Usa la paginazione cursor-based per board con molti item.
    """
    items = []
    cursor = None

    query_first = """
    query ($board_id: [ID!], $limit: Int!) {
      boards(ids: $board_id) {
        items_page(limit: $limit) {
          cursor
          items {
            id
            name
            column_values {
              id
              text
              value
            }
          }
        }
      }
    }
    """

    query_next = """
    query ($cursor: String!, $limit: Int!) {
      next_items_page(limit: $limit, cursor: $cursor) {
        cursor
        items {
          id
          name
          column_values {
            id
            text
            value
          }
        }
      }
    }
    """

    # Prima pagina
    try:
        data = _graphql(query_first, {"board_id": [board_id], "limit": 200})
        page = data.get("boards", [{}])[0].get("items_page", {})
        items.extend(page.get("items", []))
        cursor = page.get("cursor")
    except Exception as e:
        logger.error("Errore recupero item dalla board %s: %s", board_id, e)
        return []

    # Pagine successive
    while cursor:
        try:
            data = _graphql(query_next, {"cursor": cursor, "limit": 200})
            page = data.get("next_items_page", {})
            batch = page.get("items", [])
            if not batch:
                break
            items.extend(batch)
            cursor = page.get("cursor")
        except Exception as e:
            logger.error("Errore paginazione item board %s: %s", board_id, e)
            break

    logger.debug("Board %s: %d item recuperati", board_id, len(items))
    return items


def _get_columns_for_board(board_id: str) -> List[Dict]:
    """Recupera le colonne di una board."""
    query = """
    query ($board_id: [ID!]) {
      boards(ids: $board_id) {
        columns {
          id
          title
          type
        }
      }
    }
    """
    try:
        data = _graphql(query, {"board_id": [board_id]})
        boards = data.get("boards", [])
        if not boards:
            return []
        return boards[0].get("columns", [])
    except Exception as e:
        logger.error("Errore recupero colonne board %s: %s", board_id, e)
        return []


def _find_order_column_ids(board_id: str) -> List[str]:
    """
    Cerca gli ID delle colonne che potrebbero contenere numeri d'ordine/commessa.
    Parole chiave cercate nei titoli colonna (case-insensitive).
    """
    keywords = ["commessa", "ordine", "order", "rif", "riferimento", "numero", "po", "oda"]
    columns = _get_columns_for_board(board_id)
    matching_ids = []
    for col in columns:
        title_lower = col.get("title", "").lower()
        if any(kw in title_lower for kw in keywords):
            matching_ids.append(col["id"])
    return matching_ids


# ── Ricerca per colonna 'N° Commessa' ─────────────────────────────────────────

# Titoli accettati per la colonna N° Commessa (case-insensitive, parziale)
_NUM_COMMESSA_TITLE_KEYWORDS = ["n° commessa", "num commessa", "numero commessa", "n.commessa", "commessa"]


def _find_num_commessa_col_id(board_id: str) -> Optional[str]:
    """
    Trova dinamicamente l'ID della colonna 'N° Commessa' su una board.
    Usa una cache in-process per evitare query ripetute.
    Restituisce None se la colonna non esiste ancora sulla board.
    """
    global _col_cache
    cache_key = f"num_commessa_{board_id}"
    if cache_key in _col_cache:
        return _col_cache[cache_key]

    columns = _get_columns_for_board(board_id)
    col_id = None
    for col in columns:
        title_lower = col.get("title", "").lower().strip()
        if any(kw in title_lower for kw in _NUM_COMMESSA_TITLE_KEYWORDS):
            col_id = col["id"]
            logger.debug(
                "🔍 Colonna N° Commessa trovata su board %s: id='%s' title='%s'",
                board_id, col_id, col["title"]
            )
            break

    if col_id is None:
        logger.debug("⚠️  Colonna N° Commessa non trovata su board %s", board_id)

    _col_cache[cache_key] = col_id
    return col_id


def _invalidate_col_cache() -> None:
    """Svuota la cache degli ID colonna (utile nei test)."""
    global _col_cache
    _col_cache = {}


def _search_by_column_value(board_id: str, col_id: str, value: str) -> Optional[Dict]:
    """
    Cerca un item su una board usando items_page_by_column_values.
    Restituisce il primo item trovato, o None se nessun match.

    Args:
        board_id: ID della board Monday.com
        col_id:   ID della colonna in cui cercare
        value:    Valore da cercare (stringa esatta)
    """
    query = """
    query ($board_id: ID!, $col_id: String!, $col_value: String!) {
      items_page_by_column_values(
        limit: 5
        board_id: $board_id
        columns: [{ column_id: $col_id, column_values: [$col_value] }]
      ) {
        items {
          id
          name
          column_values {
            id
            text
            value
          }
        }
      }
    }
    """
    try:
        data = _graphql(query, {
            "board_id": board_id,
            "col_id": col_id,
            "col_value": value,
        })
        items = data.get("items_page_by_column_values", {}).get("items", [])
        if items:
            logger.debug(
                "items_page_by_column_values: %d risultati per '%s' su board %s col %s",
                len(items), value, board_id, col_id
            )
            return items[0]
        return None
    except Exception as e:
        logger.warning(
            "Errore items_page_by_column_values (board %s, col %s, val '%s'): %s",
            board_id, col_id, value, e
        )
        return None


# ── Logica di matching ─────────────────────────────────────────────────────────

def _search_by_order_ref(ref: str) -> MatchResult:
    """
    Cerca per numero commessa/ordine su entrambe le board.

    Strategia a due livelli:
      1. PRECISA: items_page_by_column_values sulla colonna 'N° Commessa'
         (O(1) lato server, molto più veloce del full-scan)
      2. FALLBACK: full-scan su tutti gli item con keyword matching sulle
         colonne ordine-related e sul nome dell'item

    Il livello 1 viene usato solo se la colonna 'N° Commessa' esiste sulla board.
    Se non trovata, si passa direttamente al fallback.
    """
    ref_norm = _normalize_ref(ref)
    if not ref_norm:
        return MatchResult(found=False)

    board_col_map = {
        BOARD_COMMERCIALE: _find_num_commessa_col_id(BOARD_COMMERCIALE),
        BOARD_PROGETTI:    _find_num_commessa_col_id(BOARD_PROGETTI),
    }

    # ── Strategia 1: ricerca precisa per colonna N° Commessa ──────────────────
    for board_id, col_id in board_col_map.items():
        if not col_id:
            logger.debug("Board %s: colonna N° Commessa non disponibile, skip ricerca precisa", board_id)
            continue

        # Prova sia il valore grezzo che quello normalizzato
        for search_val in dict.fromkeys([ref, ref_norm]):  # preserva ordine, rimuove duplicati
            item = _search_by_column_value(board_id, col_id, search_val)
            if item:
                logger.info(
                    "✅ Match preciso N° Commessa '%s' → item '%s' (id: %s) su board %s [col: %s]",
                    ref, item["name"], item["id"], board_id, col_id
                )
                return MatchResult(
                    found=True,
                    item_id=item["id"],
                    item_name=item["name"],
                    board_id=board_id,
                    confidence=1.0,
                    ambiguous=False,
                    candidates=[{"id": item["id"], "name": item["name"], "board_id": board_id}]
                )

    # ── Strategia 2: full-scan fallback ───────────────────────────────────────
    logger.debug("Nessun match preciso per '%s', avvio full-scan fallback", ref)
    for board_id in [BOARD_COMMERCIALE, BOARD_PROGETTI]:
        order_col_ids = _find_order_column_ids(board_id)
        items = _get_all_items(board_id)

        for item in items:
            # Controlla le colonne ordine specifiche
            for col_val in item.get("column_values", []):
                if order_col_ids and col_val["id"] not in order_col_ids:
                    continue
                col_text = (col_val.get("text") or "").strip()
                if not col_text:
                    continue
                if _normalize_ref(col_text) == ref_norm:
                    logger.info(
                        "✅ Match per numero ordine '%s' → item '%s' (id: %s) su board %s [full-scan]",
                        ref, item["name"], item["id"], board_id
                    )
                    return MatchResult(
                        found=True,
                        item_id=item["id"],
                        item_name=item["name"],
                        board_id=board_id,
                        confidence=1.0,
                        ambiguous=False,
                        candidates=[{"id": item["id"], "name": item["name"], "board_id": board_id}]
                    )

            # Fallback: controlla anche il nome dell'item stesso (contiene il numero)
            if ref_norm in _normalize_ref(item.get("name", "")):
                logger.info(
                    "✅ Match ref '%s' nel nome item '%s' (id: %s) su board %s [full-scan nome]",
                    ref, item["name"], item["id"], board_id
                )
                return MatchResult(
                    found=True,
                    item_id=item["id"],
                    item_name=item["name"],
                    board_id=board_id,
                    confidence=0.95,
                    ambiguous=False,
                    candidates=[{"id": item["id"], "name": item["name"], "board_id": board_id}]
                )

    return MatchResult(found=False)


def _search_by_client_name(client_name: str, varianti: List[str]) -> MatchResult:
    """
    Cerca per nome cliente con fuzzy matching su entrambe le board.
    Considera anche le varianti del nome trovate nell'email, il file client_patterns.json
    e la knowledge base clienti (client_knowledge_base.json).

    Restituisce:
    - found=True, ambiguous=False → un solo candidato sopra la soglia
    - found=True, ambiguous=True  → più candidati con score simile
    - found=False                 → nessun candidato sopra la soglia
    """
    if not client_name:
        return MatchResult(found=False)

    # Prepara tutte le varianti da confrontare
    patterns = _load_client_patterns()
    search_names = list({client_name} | set(varianti))

    # Aggiungi eventuali mapping dal file client_patterns.json
    for name in list(search_names):
        if name in patterns:
            search_names.append(patterns[name])
    # Rimuovi duplicati preservando ordine
    seen = set()
    unique_names = []
    for n in search_names:
        if n.lower() not in seen:
            seen.add(n.lower())
            unique_names.append(n)
    search_names = unique_names

    # ── Arricchisci con la knowledge base clienti ──────────────────────────
    # Aggiunge tutti i nomi_usati / alias dalla KB per il cliente corrispondente
    search_names = _expand_variants_from_kb(search_names)

    candidates = []

    for board_id in [BOARD_COMMERCIALE, BOARD_PROGETTI]:
        items = _get_all_items(board_id)
        for item in items:
            item_name = item.get("name", "").strip()
            if not item_name:
                continue

            # Calcola il punteggio migliore tra tutte le varianti
            best_score = max(
                _fuzzy_score(sn, item_name)
                for sn in search_names
            )

            if best_score >= MATCH_THRESHOLD:
                candidates.append({
                    "id": item["id"],
                    "name": item_name,
                    "board_id": board_id,
                    "score": best_score,
                })

    if not candidates:
        logger.debug(
            "Nessun match fuzzy per '%s' (soglia: %.2f)",
            client_name, MATCH_THRESHOLD
        )
        return MatchResult(found=False)

    # Ordina per score decrescente
    candidates.sort(key=lambda x: x["score"], reverse=True)
    best = candidates[0]

    # Ambiguità: ci sono più candidati con score > 0.75?
    high_score_candidates = [c for c in candidates if c["score"] >= 0.75]
    ambiguous = len(high_score_candidates) > 1

    if ambiguous:
        logger.warning(
            "⚠️ Match ambiguo per '%s': %d candidati (best score: %.2f)",
            client_name, len(high_score_candidates), best["score"]
        )
        return MatchResult(
            found=True,
            item_id=best["id"],
            item_name=best["name"],
            board_id=best["board_id"],
            confidence=best["score"],
            ambiguous=True,
            candidates=high_score_candidates,
        )

    logger.info(
        "✅ Match fuzzy per '%s' → item '%s' (id: %s, score: %.2f, board: %s)",
        client_name, best["name"], best["id"], best["score"], best["board_id"]
    )
    return MatchResult(
        found=True,
        item_id=best["id"],
        item_name=best["name"],
        board_id=best["board_id"],
        confidence=best["score"],
        ambiguous=False,
        candidates=candidates,
    )


# ── API pubblica ───────────────────────────────────────────────────────────────

def find_commessa(classification) -> MatchResult:
    """
    Entry point principale: cerca la commessa su entrambe le board Monday.com.

    Strategia:
    1. Numero ordine/commessa (se presente) → match esatto
    2. Nome cliente fuzzy matching

    Args:
        classification: ClassificationResult con i campi azienda,
                        riferimento_ordine e cliente_varianti.

    Returns:
        MatchResult con l'esito della ricerca.
    """
    # Strategia 1: match per numero ordine/commessa
    ref = getattr(classification, "riferimento_ordine", None)
    if ref:
        logger.info("🔍 Ricerca per numero ordine: '%s'", ref)
        result = _search_by_order_ref(ref)
        if result.found:
            return result

    # Strategia 2: fuzzy match per nome cliente
    client_name = getattr(classification, "azienda", "") or ""
    varianti = getattr(classification, "cliente_varianti", []) or []
    logger.info("🔍 Ricerca fuzzy per cliente: '%s' (varianti: %s)", client_name, varianti)
    result = _search_by_client_name(client_name, varianti)
    return result


def add_email_update(
    item_id: str,
    board_id: str,
    classification,
    email_subject: str,
) -> bool:
    """
    Aggiunge un update/commento all'item esistente con i dettagli dell'email ricevuta.

    Args:
        item_id:        ID dell'item Monday.com da aggiornare
        board_id:       ID della board di appartenenza
        classification: ClassificationResult con i dati estratti dall'email
        email_subject:  Oggetto originale dell'email

    Returns:
        True se l'update è stato aggiunto con successo, False altrimenti.
    """
    try:
        tipo_label = classification.tipo.replace("_", " ").title()
        ref = getattr(classification, "riferimento_ordine", None)
        ref_line = f"**Rif. Ordine**: {ref}\n" if ref else ""

        note_text = (
            f"📧 **Nuova email ricevuta** — *{email_subject}*\n\n"
            f"**Da**: {classification.referente} ({classification.email_mittente})\n"
            f"**Tipo**: {tipo_label}\n"
            f"**Progetto**: {classification.nome_progetto}\n"
            f"{ref_line}"
            f"\n---\n"
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
        logger.info("✅ Update aggiunto all'item %s (board: %s)", item_id, board_id)
        return True

    except Exception as e:
        logger.error(
            "❌ Impossibile aggiungere update all'item %s (board %s): %s",
            item_id, board_id, e
        )
        return False


# ── Utility CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Utilizzo diretto per test/debug:
        python commessa_matcher.py "EventPro Italia" "ODA-234"
    """
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s")

    client = sys.argv[1] if len(sys.argv) > 1 else "Test Cliente"
    ref = sys.argv[2] if len(sys.argv) > 2 else None

    class MockClassification:
        azienda = client
        riferimento_ordine = ref
        cliente_varianti = []

    result = find_commessa(MockClassification())
    print(f"\n{'='*50}")
    print(f"RISULTATO MATCHING")
    print(f"{'='*50}")
    print(f"Trovato:    {result.found}")
    print(f"Item ID:    {result.item_id}")
    print(f"Item Name:  {result.item_name}")
    print(f"Board ID:   {result.board_id}")
    print(f"Confidenza: {result.confidence:.2%}")
    print(f"Ambiguo:    {result.ambiguous}")
    print(f"Candidati:  {len(result.candidates)}")
    if result.candidates:
        for c in result.candidates:
            score = c.get("score", "N/A")
            score_str = f"{score:.2%}" if isinstance(score, float) else str(score)
            print(f"  - [{score_str}] {c['name']} (id: {c['id']}, board: {c['board_id']})")
