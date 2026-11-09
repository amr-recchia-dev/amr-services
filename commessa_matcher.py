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
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    import pathlib as _pl
    _ef = _pl.Path(__file__).parent / ".env"
    if _ef.exists():
        for _l in _ef.read_text(encoding="utf-8").splitlines():
            _l = _l.strip()
            if _l and not _l.startswith("#") and "=" in _l:
                _k, _v = _l.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

logger = logging.getLogger(__name__)

MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN", "")
BOARD_COMMERCIALE_OLD = "1865049112"
BOARD_COMMERCIALE = os.getenv("MONDAY_BOARD_ID", "2133436509")
BOARD_GESTIONE_PROGETTI_OLD = "1865197409"
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
    f"num_commessa_{BOARD_PROGETTI}": "text_mm51yk45",
    f"num_commessa_{BOARD_GESTIONE_PROGETTI_OLD}": "testo_mkmnxqsk",
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


def _normalize_text(text: str) -> str:
    """Normalizza testo per confronto parole chiave (lowercase, no punteggiatura)."""
    return re.sub(r"[^\w\s]", " ", text or "").lower().strip()


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


import time as _time

_items_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_columns_cache: Dict[str, Tuple[float, List[Dict]]] = {}
_CACHE_TTL_SEC = 60.0

_MATCHER_COL_IDS = [
    "testo_mkn1sqb4",
    "email_mkn1v37b",
    "testo_lungo_mkn1ydyz",
    "lookup_mknz7srg",
    "lookup_mknzf7p1",
    "testo_mkmnxqsk",
    "file_mknpcqbv",
    "text_mm51yvbk",
    "text_mm51yk45",
]


def _get_all_items(board_id: str) -> List[Dict]:
    """
    Recupera tutti gli item di una board Monday.com (nome + id + colonne rilevanti).
    Usa una cache in-memory di 60s per evitare chiamate API ripetute nello stesso ciclo.
    """
    now = _time.time()
    cached = _items_cache.get(str(board_id))
    if cached and (now - cached[0]) < _CACHE_TTL_SEC:
        return cached[1]

    items = []
    cursor = None

    query_first = """
    query ($board_id: [ID!], $limit: Int!, $col_ids: [String!]) {
      boards(ids: $board_id) {
        items_page(limit: $limit) {
          cursor
          items {
            id
            name
            column_values(ids: $col_ids) {
              id
              text
              value
              ... on MirrorValue { display_value }
            }
          }
        }
      }
    }
    """

    query_next = """
    query ($cursor: String!, $limit: Int!, $col_ids: [String!]) {
      next_items_page(limit: $limit, cursor: $cursor) {
        cursor
        items {
          id
          name
          column_values(ids: $col_ids) {
            id
            text
            value
            ... on MirrorValue { display_value }
          }
        }
      }
    }
    """

    # Prima pagina
    try:
        data = _graphql(query_first, {"board_id": [board_id], "limit": 200, "col_ids": _MATCHER_COL_IDS})
        page = data.get("boards", [{}])[0].get("items_page", {})
        items.extend(page.get("items", []))
        cursor = page.get("cursor")
    except Exception as e:
        logger.error("Errore recupero item dalla board %s: %s", board_id, e)
        return []

    # Pagine successive
    while cursor:
        try:
            data = _graphql(query_next, {"cursor": cursor, "limit": 200, "col_ids": _MATCHER_COL_IDS})
            page = data.get("next_items_page", {})
            batch = page.get("items", [])
            if not batch:
                break
            items.extend(batch)
            cursor = page.get("cursor")
        except Exception as e:
            logger.error("Errore paginazione item board %s: %s", board_id, e)
            break

    _items_cache[str(board_id)] = (_time.time(), items)
    logger.debug("Board %s: %d item recuperati", board_id, len(items))
    return items


def _get_columns_for_board(board_id: str) -> List[Dict]:
    """Recupera le colonne di una board (con cache)."""
    now = _time.time()
    cached = _columns_cache.get(str(board_id))
    if cached and (now - cached[0]) < 600.0:
        return cached[1]

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
        cols = boards[0].get("columns", [])
        _columns_cache[str(board_id)] = (now, cols)
        return cols
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
        BOARD_GESTIONE_PROGETTI_OLD: _find_num_commessa_col_id(BOARD_GESTIONE_PROGETTI_OLD),
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

    # ── Strategia 2: full-scan fallback su tutte le board (inclusa GESTIONE PROGETTI 1865197409) ──
    logger.debug("Nessun match preciso per '%s', avvio full-scan fallback", ref)
    for board_id in dict.fromkeys([BOARD_COMMERCIALE_OLD, BOARD_COMMERCIALE, BOARD_GESTIONE_PROGETTI_OLD, BOARD_PROGETTI]):
        order_col_ids = _find_order_column_ids(board_id)
        items = _get_all_items(board_id)

        for item in items:
            # Controlla le colonne ordine specifiche e file ordine interno
            for col_val in item.get("column_values", []):
                col_text = (col_val.get("text") or col_val.get("display_value") or "").strip()
                if not col_text:
                    continue
                if col_val["id"] == "file_mknpcqbv" or (not order_col_ids or col_val["id"] in order_col_ids):
                    if _normalize_ref(col_text) == ref_norm or (len(ref_norm) >= 4 and ref_norm in _normalize_ref(col_text)):
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


# Parole generiche che NON identificano uno specifico progetto
_PROJECT_STOPWORDS = {
    "preventivo", "richiesta", "offerta", "quotazione", "stima", "costi", "costo",
    "ordine", "acquisto", "conferma", "modifica", "aggiornamento", "variazione",
    "realizzazione", "fattibilita", "fattibilità", "verifica", "campionatura",
    "polistirolo", "eps", "resina", "stampa", "taglio", "fresa", "cnc", "3d",
    "elementi", "elemento", "pezzi", "pezzo", "scritta", "scritte", "logo", "loghi",
    "cliente", "progetto", "commessa", "fornitura", "produzione", "allestimento",
    "urgente", "nuova", "nuovo", "info", "informazioni", "disponibilita", "disponibilità",
    "per", "del", "della", "dei", "degli", "delle", "con", "senza", "sul", "sulla",
    "nel", "nella", "alla", "alle", "agli", "dal", "dalla", "tra", "fra", "che",
    "non", "piu", "più", "anche", "come", "sono", "siamo", "buongiorno", "salve",
    "gentile", "cortese", "attenzione", "spett", "srl", "srls", "spa", "snc", "sas",
    "re", "fw", "fwd", "rif", "vs", "ns", "oda", "po", "amr", "recchia",
}


def _extract_specific_project_tokens(text: str, client_names: List[str]) -> set:
    """
    Estrae i token specifici che identificano un progetto, escludendo:
    - parole corte (< 3 caratteri, a meno che non siano codici alfanumerici con cifre)
    - stopwords generiche di preventivi/lavorazioni
    - parole che fanno parte del nome stesso del cliente
    """
    client_tokens = set()
    for cn in client_names:
        client_tokens.update(_normalize_text(cn).split())

    raw_tokens = _normalize_text(text).split()
    specific = set()
    for t in raw_tokens:
        if t in _PROJECT_STOPWORDS or t in client_tokens:
            continue
        if len(t) >= 3 or (len(t) >= 2 and any(ch.isdigit() for ch in t)):
            specific.add(t)
    return specific


def _search_by_client_name(
    client_name: str,
    varianti: List[str],
    nome_progetto: str = "",
    email_subject: str = "",
    tipo: str = "",
    email_mittente: str = "",
    note: str = "",
) -> MatchResult:
    """
    Cerca per nome cliente (o email mittente) + corrispondenza progetto su tutte le board commerciali/progetti
    (COMMERCIALE 1865049112, NEW COMMERCIALE 2133436509, GESTIONE PROGETTI 1865197409 e GESTIONE PROGETTI NEW 2136092569).
    Include normalizzazione nomi manuali (es. 'ipe 26.10.26' -> 'ipe') e verifica anti-clone su numero ordine e progetto.
    """
    if not client_name and not email_mittente:
        return MatchResult(found=False)

    import urllib.parse as _urlparse
    from monday_client import normalize_client_name, is_same_commessa_project

    # Prepara tutte le varianti da confrontare
    patterns = _load_client_patterns()
    search_names = list({client_name} | set(varianti)) if client_name else list(set(varianti))

    # Aggiungi eventuali mapping dal file client_patterns.json
    for name in list(search_names):
        if name in patterns:
            search_names.append(patterns[name])
    # Rimuovi duplicati preservando ordine
    seen = set()
    unique_names = []
    for n in search_names:
        if n and n.lower() not in seen:
            seen.add(n.lower())
            unique_names.append(n)
    search_names = unique_names

    # ── Arricchisci con la knowledge base clienti ──────────────────────────
    search_names = _expand_variants_from_kb(search_names)
    norm_search_names = [normalize_client_name(sn) for sn in search_names if normalize_client_name(sn)]
    sender_clean = (email_mittente or "").strip().lower()
    is_internal_sender = sender_clean in ("ordini@amrrecchia.it", "info@amrrecchia.it", "")

    candidates = []

    boards_to_check = list(dict.fromkeys([
        BOARD_COMMERCIALE_OLD,
        BOARD_COMMERCIALE,
        BOARD_GESTIONE_PROGETTI_OLD,
        BOARD_PROGETTI,
    ]))
    for board_id in boards_to_check:
        items = _get_all_items(board_id)
        for item in items:
            item_name = item.get("name", "").strip()
            if not item_name:
                continue

            norm_item = normalize_client_name(item_name)
            best_score = max(
                [_fuzzy_score(sn, item_name) for sn in search_names]
                + ([_fuzzy_score(nsn, norm_item) for nsn in norm_search_names if norm_item] if norm_search_names else [0.0])
            )
            # In GESTIONE PROGETTI il nome dell'item spesso contiene "cliente + progetto" (es. "mogg culla poltrona", "BERTONE TAVOLI")
            for nsn in norm_search_names:
                if len(nsn) >= 3 and norm_item and (norm_item.startswith(nsn + " ") or nsn == norm_item):
                    best_score = max(best_score, 0.90)

            proj_col = ""
            item_email = ""
            item_note = ""
            for cv in item.get("column_values", []):
                cid = cv.get("id")
                val_txt = (cv.get("text") or cv.get("display_value") or "").strip()
                if cid in ("testo_mkn1sqb4", "text_mm51yk45", "lookup_mknz7srg", "testo_mkmnxqsk") and val_txt:
                    proj_col = (proj_col + " " + val_txt).strip()
                elif cid == "file_mknpcqbv" and val_txt:
                    # Estrai il nome del PDF "Doc. Ordine interno" in GESTIONE PROGETTI (contiene N.ord.cliente)
                    fname_dec = _urlparse.unquote(val_txt.split("/")[-1])
                    proj_col = (proj_col + " " + fname_dec).strip()
                    item_note = (item_note + " " + fname_dec).strip()
                elif cid == "email_mkn1v37b" and val_txt:
                    item_email = val_txt.lower()
                elif cid in ("testo_lungo_mkn1ydyz", "lookup_mknzf7p1") and val_txt:
                    item_note = (item_note + " " + val_txt).strip()

            if not is_internal_sender and sender_clean and item_email and item_email == sender_clean:
                best_score = max(best_score, 0.95)

            if best_score >= MATCH_THRESHOLD:
                candidates.append({
                    "id": item["id"],
                    "name": item_name,
                    "board_id": board_id,
                    "score": best_score,
                    "project": proj_col,
                    "note": item_note,
                })

    if not candidates:
        logger.debug(
            "Nessun match fuzzy per '%s' (soglia: %.2f)",
            client_name, MATCH_THRESHOLD
        )
        return MatchResult(found=False)

    # 0) Controllo diretto Anti-Clone (numero ordine o progetto già presente su Monday, anche se inserito a mano da Gary)
    for c in candidates:
        if is_same_commessa_project(
            f"{nome_progetto} {email_subject}",
            f"{c.get('project', '')} {c['name']}",
            note,
            c.get("note", ""),
        ):
            logger.info(
                "🛡️ [ANTI-CLONE MATCH] Commessa già presente su Monday: '%s' / '%s' (id: #%s, board: %s)",
                c["name"], c.get("project"), c["id"], c["board_id"]
            )
            return MatchResult(
                found=True,
                item_id=c["id"],
                item_name=c["name"],
                board_id=c["board_id"],
                confidence=c["score"],
                ambiguous=False,
                candidates=candidates,
            )

    # Verifica se il progetto dell'email corrisponde davvero a uno dei progetti esistenti del cliente
    incoming_proj_text = f"{nome_progetto} {email_subject}".strip()
    incoming_tokens = _extract_specific_project_tokens(incoming_proj_text, search_names)
    subj_lower = (email_subject or "").strip().lower()
    is_reply = subj_lower.startswith(("re:", "r:", "aw:")) or "re:" in subj_lower[:15]

    best_proj_match = None
    max_common = 0
    best_overlap_ratio = 0.0

    for c in candidates:
        cand_tokens = _extract_specific_project_tokens(
            f"{c.get('project', '')} {c['name']}",
            search_names,
        )
        if not cand_tokens or not incoming_tokens:
            continue
        common_tokens = incoming_tokens & cand_tokens
        common = len(common_tokens)
        overlap_ratio = common / max(1, min(len(incoming_tokens), len(cand_tokens)))
        if (common, overlap_ratio) > (max_common, best_overlap_ratio):
            max_common = common
            best_overlap_ratio = overlap_ratio
            best_proj_match = c

    # Regola anti-occultamento:
    # 1) Se l'email ha parole specifiche di progetto e NESSUNA coincide con le righe esistenti del cliente
    #    → è un NUOVO PROGETTO di un cliente già noto! Creiamo una nuova riga (found=False).
    if incoming_tokens:
        min_required_common = 1 if is_reply else (1 if len(incoming_tokens) == 1 else 2)
        if best_proj_match and (max_common >= min_required_common or best_overlap_ratio >= 0.6):
            logger.info(
                "🎯 Match cliente+progetto confermato ('%s' / '%s', %d token specifici in comune) → item '%s' (id: %s)",
                client_name, nome_progetto, max_common, best_proj_match["name"], best_proj_match["id"]
            )
            return MatchResult(
                found=True,
                item_id=best_proj_match["id"],
                item_name=best_proj_match["name"],
                board_id=best_proj_match["board_id"],
                confidence=best_proj_match["score"],
                ambiguous=False,
                candidates=candidates,
            )
        else:
            logger.info(
                "🆕 Cliente '%s' già presente su Monday (%d righe), ma il progetto '%s' è NUOVO "
                "(token specifici: %s, max in comune: %d) → creo NUOVA RIGA!",
                client_name, len(candidates), nome_progetto, sorted(incoming_tokens), max_common
            )
            return MatchResult(found=False)

    # 2) Se l'email non ha alcun token specifico di progetto:
    #    - Se NON è una risposta (Re:/R:) → crea SEMPRE una nuova riga (anche se classificata generica)
    #    - Se è una risposta (Re:/R:) → aggiorna l'item più recente del cliente
    if not is_reply:
        logger.info(
            "🆕 Email non-risposta per '%s' senza progetto specifico preesistente → creo NUOVA RIGA",
            client_name
        )
        return MatchResult(found=False)

    # Ordina per ID decrescente (il più recente su Monday) per aggiornamenti generici senza progetto
    candidates.sort(key=lambda x: int(x["id"]) if x["id"].isdigit() else 0, reverse=True)
    chosen = candidates[0]
    logger.info(
        "✅ Aggiornamento su cliente esistente '%s' → selezionato item più recente '%s' (id: %s, board: %s)",
        client_name, chosen["name"], chosen["id"], chosen["board_id"]
    )
    return MatchResult(
        found=True,
        item_id=chosen["id"],
        item_name=chosen["name"],
        board_id=chosen["board_id"],
        confidence=chosen["score"],
        ambiguous=False,
        candidates=candidates,
    )


# ── API pubblica ───────────────────────────────────────────────────────────────

def find_commessa(classification, email_subject: str = "") -> MatchResult:
    """
    Entry point principale: cerca la commessa su entrambe le board Monday.com.

    Strategia:
    1. Numero ordine/commessa (se presente) → match esatto
    2. Nome cliente + verifica stesso progetto

    Args:
        classification: ClassificationResult con i campi azienda,
                        riferimento_ordine e cliente_varianti.
        email_subject:  Oggetto originale dell'email (opzionale, aiuta a capire se è Re:/R: o nuovo progetto).

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

    # Strategia 2: fuzzy match per nome cliente + corrispondenza progetto
    client_name = getattr(classification, "azienda", "") or ""
    varianti = getattr(classification, "cliente_varianti", []) or []
    nome_progetto = getattr(classification, "nome_progetto", "") or ""
    tipo = getattr(classification, "tipo", "") or ""
    email_mittente = getattr(classification, "email_mittente", "") or ""
    note = getattr(classification, "note", "") or ""
    logger.info(
        "🔍 Ricerca per cliente: '%s' (progetto: '%s', tipo: '%s', varianti: %s)",
        client_name, nome_progetto, tipo, varianti
    )
    result = _search_by_client_name(
        client_name,
        varianti,
        nome_progetto=nome_progetto,
        email_subject=email_subject,
        tipo=tipo,
        email_mittente=email_mittente,
        note=note,
    )
    return result


def add_email_update(
    item_id: str,
    board_id: str,
    classification,
    email_subject: str,
    attachments: Optional[List[Tuple[str, bytes]]] = None,
) -> bool:
    """
    Aggiorna le colonne (Testo lungo / Richiesta + Disegno allegati) dell'item esistente
    senza chiamare create_update("Email ricevuta..."), così Monday.com non invia
    un'email ridondante di notifica a ordini@amrrecchia.it.
    """
    try:
        from monday_client import _append_note_to_column, upload_attachments_to_item

        check_query = """
        query ($item_id: [ID!]) {
          items(ids: $item_id) {
            name
            column_values(ids: ["testo_lungo_mkn1ydyz"]) {
              id
              text
            }
          }
        }
        """
        existing_data = _graphql(check_query, {"item_id": [item_id]})
        items_res = existing_data.get("items", [])
        item_name_found = ""
        existing_note = ""
        if items_res:
            item_name_found = (items_res[0].get("name") or "").strip()
            for cv in items_res[0].get("column_values", []):
                if cv.get("id") == "testo_lungo_mkn1ydyz":
                    existing_note = (cv.get("text") or "").strip()

        _append_note_to_column(str(board_id), str(item_id), existing_note, classification)
        logger.info("✅ Colonna note aggiornata sull'item %s (board: %s) senza notifica email ridondante", item_id, board_id)

        if attachments:
            try:
                upload_attachments_to_item(str(item_id), attachments)
            except Exception as att_err:
                logger.warning("Impossibile caricare allegati su item %s: %s", item_id, att_err)

        # Se l'item è su COMMERCIALE / NEW COMMERCIALE o GESTIONE PROGETTI / GESTIONE PROGETTI NEW,
        # propaga anche all'item gemello sull'altra scheda!
        twin_board = None
        if str(board_id) == "1865049112":
            twin_board = "2133436509"
        elif str(board_id) == "2133436509":
            twin_board = "1865049112"
        elif str(board_id) == "1865197409":
            twin_board = "2136092569"
        elif str(board_id) == "2136092569":
            twin_board = "1865197409"

        if twin_board and item_name_found:
            try:
                twin_q = """
                query ($board_id: ID!, $item_name: String!) {
                  items_page_by_column_values(
                    limit: 5
                    board_id: $board_id
                    columns: [{ column_id: "name", column_values: [$item_name] }]
                  ) {
                    items {
                      id
                      name
                      column_values(ids: ["testo_lungo_mkn1ydyz"]) { id text }
                    }
                  }
                }
                """
                twin_res = _graphql(twin_q, {"board_id": twin_board, "item_name": item_name_found})
                twin_items = twin_res.get("items_page_by_column_values", {}).get("items", [])
                if twin_items:
                    twin_id = str(twin_items[0]["id"])
                    twin_note = ""
                    for cv in twin_items[0].get("column_values", []):
                        if cv.get("id") == "testo_lungo_mkn1ydyz":
                            twin_note = (cv.get("text") or "").strip()
                    _append_note_to_column(twin_board, twin_id, twin_note, classification)
                    if attachments:
                        upload_attachments_to_item(twin_id, attachments)
                    logger.info("🔗 Aggiornamento propagato anche a item gemello #%s su board %s", twin_id, twin_board)
            except Exception as twin_err:
                logger.debug("Propagazione twin update su board %s: %s", twin_board, twin_err)

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
