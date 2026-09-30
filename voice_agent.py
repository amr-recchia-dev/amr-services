"""
voice_agent.py - Agente Vocale Custom per AMR Recchia
=====================================================
Comprende comandi vocali in italiano per:
- Segnare step completati o in corso (Taglio, Pantografo, Fresa, Carteggiatura, Colore, Resina, Imballo)
- Registrare tempi effettivi e stime (es. "2 ore e mezza", "45 minuti", "3h")
- Aggiornare stati commessa (Fatto, In corso, Bloccato)
- Aggiungere note o specifiche
- Fuzzy matching intelligente su nomi clienti e progetti reali di Monday.com
"""

from __future__ import annotations
import os, re, json, difflib, logging, requests, base64, time
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("voice_agent")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

from user_auth import (
    AUTH_USERS,
    BOARD_COMMERCIALE,
    BOARD_PRODUZIONE,
    BOARD_GESTIONE_PROGETTI,
    BOARD_APPUNTAMENTI,
    BOARD_PROGETTAZIONE,
    BOARD_INVENTARIO,
    BOARD_AMMINISTRAZIONE,
    BOARD_CONTESTAZIONI,
    BOARD_INSTALLAZIONI,
    BOARD_PALLET_EPS,
    BOARD_TAGLIO,
    BOARD_FINITURE,
    RESTRICTED_BOARDS,
    ALL_ACTIVE_BOARDS,
    get_user_by_pin,
    can_user_access_board,
    is_board_restricted
)

# Mappatura stati per le schede originali di Monday
STATUS_GESTIONE_PROGETTI = {
    "In corso": "in produzione",
    "Fatto": "Fatto",
    "Bloccato": "Bloccato",
    "Da iniziare": "non in produzione"
}

STATUS_PRODUZIONE = {
    "In corso": "In svolgimento",
    "Fatto": "Fatto",
    "Bloccato": "Bloccato"
}

def get_active_commesse_hint() -> str:
    """Restituisce una sintesi dei clienti e numeri commessa attivi su tutte le schede per guidare la trascrizione Gemini."""
    try:
        projs = get_active_projects_cache()
        hints = []
        for p in projs[:50]:
            c = p.get("commessa")
            n = p.get("name")
            if c and n and c not in n:
                hints.append(f"{n} ({c})")
            elif n:
                hints.append(n)
        return ", ".join(hints)
    except Exception:
        return ""

def transcribe_audio_with_gemini(audio_bytes: bytes, mime_type: str = "audio/webm") -> dict:
    """
    Trascrive l'audio registrato dall'utente tramite Gemini Flash/Lite con supporto multilingua
    (Italiano, Arabo standard e dialetti nordafricani come marocchino/darija o tunisino).
    Restituisce un dizionario con la trascrizione originale, la lingua rilevata e la traduzione italiana.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.error("GEMINI_API_KEY mancante per trascrizione audio.")
        return {"text": "", "transcription": "", "language": "it", "italian_translation": ""}

    if len(audio_bytes) < 400:
        logger.warning(f"Audio troppo corto ({len(audio_bytes)} bytes), scartato.")
        return {"text": "", "transcription": "", "language": "it", "italian_translation": ""}

    # Normalizzazione precisa del MIME type per le specifiche di Gemini
    raw_mime = (mime_type or "audio/webm").split(";")[0].strip().lower()
    if "mp4" in raw_mime:
        clean_mime = "audio/mp4"
    elif "webm" in raw_mime:
        clean_mime = "audio/webm"
    elif "ogg" in raw_mime:
        clean_mime = "audio/ogg"
    elif "wav" in raw_mime:
        clean_mime = "audio/wav"
    elif "aac" in raw_mime:
        clean_mime = "audio/aac"
    else:
        clean_mime = "audio/webm"

    b64_audio = base64.b64encode(audio_bytes).decode("utf-8")
    hint = get_active_commesse_hint()
    hint_text = f"\nClienti e commesse attualmente attivi in officina: {hint}\n" if hint else ""

    prompt_instructions = (
        "Sei il modulo di trascrizione e comprensione vocale per l'officina dell'azienda italiana AMR Recchia "
        "(reparti: taglio, fresa, pantografo, resine, finiture, verniciatura, assemblaggio, imballo, cantieri).\n"
        f"{hint_text}"
        "L'operatore in officina può parlare in italiano, in arabo (arabo standard o dialetti nordafricani come marocchino/darija o tunisino) oppure un mix.\n"
        "Ascolta attentamente la nota vocale e restituisci ESCLUSIVAMENTE un JSON valido con questa struttura:\n"
        "{\n"
        '  "language": "it" oppure "ar" oppure "mixed",\n'
        '  "transcription": "trascrizione fedele e letterale delle parole pronunciate nella lingua originale",\n'
        '  "italian_translation": "traduzione e normalizzazione fedele in italiano, adatta al gergo di officina AMR (con i nomi corretti di commesse/clienti, reparti: taglio, fresa, resine, finiture, assemblaggio, cantieri, tempi di lavoro, stati ed eventuali colleghi da taggare)"\n'
        "}\n"
        "Se l'operatore parla già in italiano, 'italian_translation' sarà identica a 'transcription'."
    )

    models_to_try = [
        "gemini-flash-lite-latest",
        "gemini-3.1-flash-lite-preview",
        "gemini-flash-latest",
        "gemini-2.5-flash"
    ]

    for model_name in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        payload = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": clean_mime, "data": b64_audio}},
                    {"text": prompt_instructions}
                ]
            }],
            "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"}
        }

        try:
            resp = requests.post(url, json=payload, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                if parts:
                    txt = parts[0].get("text", "").strip()
                    logger.info(f"🎧 Trascrizione Gemini ({model_name}): {txt[:200]}")
                    try:
                        parsed = json.loads(txt)
                        lang = parsed.get("language", "it")
                        trans = parsed.get("transcription", "").strip()
                        it_trans = parsed.get("italian_translation", "").strip() or trans
                        return {
                            "text": it_trans,
                            "italian_translation": it_trans,
                            "transcription": trans,
                            "language": lang
                        }
                    except Exception:
                        return {
                            "text": txt,
                            "italian_translation": txt,
                            "transcription": txt,
                            "language": "it"
                        }
            else:
                logger.warning(f"Modello {model_name} status {resp.status_code}: {resp.text[:150]}")
        except Exception as ex:
            logger.error(f"Errore chiamata Gemini audio su {model_name}: {ex}")

    return {"text": "", "transcription": "", "language": "it", "italian_translation": ""}


def translate_arabic_to_italian_if_needed(text: str) -> dict:
    """Se il testo contiene caratteri arabi, traduce e normalizza in italiano con Gemini."""
    if not text:
        return {"text": "", "transcription": "", "language": "it", "italian_translation": ""}
    if not re.search(r"[\u0600-\u06FF]", text):
        return {"text": text, "transcription": text, "language": "it", "italian_translation": text}
    
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return {"text": text, "transcription": text, "language": "ar", "italian_translation": text}
        
    prompt = f"""
Sei il traduttore vocale di officina per l'azienda italiana AMR Recchia.
L'operaio ha digitato o pronunciato questa frase in lingua araba:
"{text}"
Traduci fedelmente in italiano contestualizzato per officina (reparti: taglio, fresa, resina, finitura, verniciatura, assemblaggio, ore lavorate, nomi clienti/commesse).
Restituisci ESCLUSIVAMENTE un JSON:
{{
  "language": "ar",
  "transcription": "{text}",
  "italian_translation": "testo tradotto e normalizzato in italiano"
}}
"""
    for m in ["gemini-flash-lite-latest", "gemini-3.1-flash-lite-preview", "gemini-flash-latest"]:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={api_key}"
            resp = requests.post(url, json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "response_mime_type": "application/json"}
            }, timeout=8)
            if resp.status_code == 200:
                data = json.loads(resp.json()["candidates"][0]["content"]["parts"][0]["text"])
                it_tr = data.get("italian_translation", text).strip()
                return {
                    "text": it_tr,
                    "italian_translation": it_tr,
                    "transcription": text,
                    "language": "ar"
                }
        except Exception:
            continue
    return {"text": text, "transcription": text, "language": "ar", "italian_translation": text}



# Mappatura step e colonne per reparto
DEPARTMENT_STEPS = {
    "pantografo": {"board": BOARD_TAGLIO, "status_col": "color_mm6wbsx6", "time_col": "text_mm6wgnhe", "name": "Pantografo"},
    "taglierina": {"board": BOARD_TAGLIO, "status_col": "color_mm6wgk9e", "time_col": "text_mm6werzq", "name": "Taglierina"},
    "taglio": {"board": BOARD_TAGLIO, "status_col": "color_mkwq1c60", "time_col": "text_mm6wgnhe", "name": "Taglio"},
    "fresa": {"board": BOARD_TAGLIO, "status_col": "color_mm6wjwvr", "time_col": "text_mm6wfxj8", "name": "Fresa e Assemblaggio"},
    "assemblaggio": {"board": BOARD_TAGLIO, "status_col": "color_mm6wjwvr", "time_col": "text_mm6wfxj8", "name": "Fresa e Assemblaggio"},
    "imballo": {"board": BOARD_TAGLIO, "status_col": "color_mm6w81nf", "time_col": "text_mm6w9vp7", "name": "Assemblaggio e Imballo"},
    "resina": {"board": BOARD_FINITURE, "status_col": "color_mm6w7av0", "time_col": "text_mm6wa1rk", "name": "Resine"},
    "resine": {"board": BOARD_FINITURE, "status_col": "color_mm6w7av0", "time_col": "text_mm6wa1rk", "name": "Resine"},
    "carteggiatura": {"board": BOARD_FINITURE, "status_col": "color_mm6w4v44", "time_col": "text_mm6w8jdw", "name": "Carteggiatura"},
    "carteggiare": {"board": BOARD_FINITURE, "status_col": "color_mm6w4v44", "time_col": "text_mm6w8jdw", "name": "Carteggiatura"},
    "colore": {"board": BOARD_FINITURE, "status_col": "color_mm6wztyf", "time_col": "text_mm6wpeck", "name": "Colore / Finitura"},
    "finitura": {"board": BOARD_FINITURE, "status_col": "color_mm6wztyf", "time_col": "text_mm6wpeck", "name": "Colore / Finitura"},
    "verniciatura": {"board": BOARD_FINITURE, "status_col": "color_mm6wztyf", "time_col": "text_mm6wpeck", "name": "Colore / Finitura"},
    "cantiere": {"board": BOARD_FINITURE, "status_col": "color_mm6wve59", "time_col": "text_mm6w293y", "name": "Cantieri / Installazioni"},
    "installazione": {"board": BOARD_FINITURE, "status_col": "color_mm6wve59", "time_col": "text_mm6w293y", "name": "Cantieri / Installazioni"}
}


def parse_duration_italian(text: str) -> str:
    """Estrae durate espresse in italiano come '2 ore e mezza', '3 ore', '45 minuti', '1 ora e 15'."""
    t = text.lower()
    
    # 2 ore e mezza / un'ora e mezza
    m_half = re.search(r"(\d+|un|un'|una)\s*or[ae]\s*e\s*mezz[ao]", t)
    if m_half:
        h_str = m_half.group(1)
        h = 1 if h_str in ["un", "un'", "una"] else int(h_str)
        return f"{h}h 30m"

    # X ore e Y minuti
    m_h_m = re.search(r"(\d+|un|un'|una)\s*or[ae]\s*(?:e\s*)?(\d+)\s*minut[io]?", t)
    if m_h_m:
        h_str = m_h_m.group(1)
        h = 1 if h_str in ["un", "un'", "una"] else int(h_str)
        m = int(m_h_m.group(2))
        return f"{h}h {m}m"

    # X ore
    m_h = re.search(r"(\d+|un|un'|una)\s*or[ae]", t)
    if m_h:
        h_str = m_h.group(1)
        h = 1 if h_str in ["un", "un'", "una"] else int(h_str)
        return f"{h}h"

    # X minuti
    m_m = re.search(r"(\d+)\s*minut[io]?", t)
    if m_m:
        return f"{m_m.group(1)}m"

    # Pattern standard "2h 30m", "4h"
    m_std = re.search(r"(\d+)\s*h\s*(?:(\d+)\s*m)?", t)
    if m_std:
        h = m_std.group(1)
        m = m_std.group(2)
        return f"{h}h {m}m" if m else f"{h}h"

    return ""


_PROJECTS_CACHE = []
_PROJECTS_CACHE_TIME = 0
PROJECTS_CACHE_TTL = 45  # secondi

def get_active_projects_cache(force_refresh: bool = False) -> list:
    """
    Recupera e mette in cache tutti i progetti, commesse, installazioni, materiali e appuntamenti
    attivi interrogando le 10 schede ufficiali del Workspace AMR visibili su Monday:
    - GESTIONE PROGETTI & PRODUZIONE (officina e produzione)
    - COMMERCIALE (preventivi e trattative, accesso riservato)
    - PROGETTAZIONE (disegni e 3D)
    - INSTALLAZIONI (cantieri ed esterni)
    - INVENTARIO MATERIALI / PRODOTTI
    - APPUNTAMENTI (visite e incontri)
    - AMMINISTRAZIONE & CONTESTAZIONI
    - PALLET EPS COMPATTATO 2025
    """
    global _PROJECTS_CACHE, _PROJECTS_CACHE_TIME
    now = time.time()
    if not force_refresh and _PROJECTS_CACHE and (now - _PROJECTS_CACHE_TIME < PROJECTS_CACHE_TTL):
        return _PROJECTS_CACHE

    headers = {"Authorization": MONDAY_TOKEN, "API-Version": "2024-10"}

    board_ids = [
        BOARD_GESTIONE_PROGETTI,
        BOARD_PRODUZIONE,
        BOARD_COMMERCIALE,
        BOARD_PROGETTAZIONE,
        BOARD_INSTALLAZIONI,
        BOARD_INVENTARIO,
        BOARD_APPUNTAMENTI,
        BOARD_AMMINISTRAZIONE,
        BOARD_CONTESTAZIONI,
        BOARD_PALLET_EPS
    ]

    q = f"""
    query {{
      boards(ids: {json.dumps(board_ids)}) {{
        id
        name
        items_page(limit: 100) {{
          items {{
            id
            name
            state
            column_values(ids: [
              "testo_mkmnxqsk", "project_status", "color_mm1v12gx",
              "testo_mkn1sqb4", "color_mkn4s77r", "label_mkn37zp7"
            ]) {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    try:
        resp = requests.post(MONDAY_API_URL, headers=headers, json={"query": q}, timeout=15)
        boards_data = {str(b.get("id")): b for b in resp.json().get("data", {}).get("boards", [])}

        # 1. Mappatura PRODUZIONE per nome normalizzato
        prod_map = {}
        for it in boards_data.get(BOARD_PRODUZIONE, {}).get("items_page", {}).get("items", []):
            if it.get("state") == "active":
                prod_map[it["name"].strip().lower()] = str(it["id"])

        # 2. Mappatura COMMERCIALE per nome normalizzato
        comm_map = {}
        for it in boards_data.get(BOARD_COMMERCIALE, {}).get("items_page", {}).get("items", []):
            if it.get("state") == "active":
                comm_map[it["name"].strip().lower()] = str(it["id"])

        # 3. Mappatura GESTIONE PROGETTI per nome normalizzato
        gp_map = {}
        for it in boards_data.get(BOARD_GESTIONE_PROGETTI, {}).get("items_page", {}).get("items", []):
            if it.get("state") == "active":
                gp_map[it["name"].strip().lower()] = str(it["id"])

        clean = []
        seen_item_ids = set()

        # Priorità 1: GESTIONE PROGETTI (1865197409)
        for it in boards_data.get(BOARD_GESTIONE_PROGETTI, {}).get("items_page", {}).get("items", []):
            if it.get("state") != "active":
                continue
            cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
            clean_name = it["name"].strip()
            norm_name = clean_name.lower()
            clean.append({
                "id": str(it["id"]),
                "board_id": BOARD_GESTIONE_PROGETTI,
                "board_name": "GESTIONE PROGETTI",
                "name": clean_name,
                "commessa": cols.get("testo_mkmnxqsk", ""),
                "progetto": "",
                "stato": cols.get("project_status", ""),
                "produzione_id": prod_map.get(norm_name),
                "commerciale_id": comm_map.get(norm_name),
                "requires_commercial": False
            })
            seen_item_ids.add(str(it["id"]))

        # Priorità 2: PRODUZIONE (item non presenti su Gestione Progetti)
        existing_names = {p["name"].lower() for p in clean}
        for it in boards_data.get(BOARD_PRODUZIONE, {}).get("items_page", {}).get("items", []):
            if it.get("state") != "active":
                continue
            norm_name = it["name"].strip().lower()
            if norm_name not in existing_names:
                cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
                clean.append({
                    "id": str(it["id"]),
                    "board_id": BOARD_PRODUZIONE,
                    "board_name": "PRODUZIONE",
                    "name": it["name"].strip(),
                    "commessa": "",
                    "progetto": "",
                    "stato": cols.get("color_mm1v12gx", ""),
                    "produzione_id": str(it["id"]),
                    "gestione_id": gp_map.get(norm_name),
                    "requires_commercial": False
                })
                existing_names.add(norm_name)
                seen_item_ids.add(str(it["id"]))

        # Priorità 3: COMMERCIALE (1865049112 - preventivi / ordini commerciali)
        for it in boards_data.get(BOARD_COMMERCIALE, {}).get("items_page", {}).get("items", []):
            if it.get("state") != "active":
                continue
            norm_name = it["name"].strip().lower()
            if norm_name not in existing_names:
                cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
                clean.append({
                    "id": str(it["id"]),
                    "board_id": BOARD_COMMERCIALE,
                    "board_name": "COMMERCIALE",
                    "name": it["name"].strip(),
                    "commessa": "",
                    "progetto": cols.get("testo_mkn1sqb4", ""),
                    "stato": cols.get("color_mkn4s77r", ""),
                    "preventivo_accettato": cols.get("label_mkn37zp7", ""),
                    "produzione_id": prod_map.get(norm_name),
                    "gestione_id": gp_map.get(norm_name),
                    "requires_commercial": True
                })
                existing_names.add(norm_name)
                seen_item_ids.add(str(it["id"]))

        # Priorità 4: Tutte le altre schede operative e amministrative dello screenshot
        other_board_specs = [
            (BOARD_PROGETTAZIONE, "PROGETTAZIONE", False),
            (BOARD_INSTALLAZIONI, "INSTALLAZIONI", False),
            (BOARD_INVENTARIO, "INVENTARIO MATERIALI / PRODOTTI", False),
            (BOARD_APPUNTAMENTI, "APPUNTAMENTI", True),
            (BOARD_AMMINISTRAZIONE, "AMMINISTRAZIONE", True),
            (BOARD_CONTESTAZIONI, "CONTESTAZIONI", False),
            (BOARD_PALLET_EPS, "PALLET EPS COMPATTATO 2025", False)
        ]
        for b_id, b_title, req_comm in other_board_specs:
            for it in boards_data.get(b_id, {}).get("items_page", {}).get("items", []):
                if it.get("state") != "active":
                    continue
                s_id = str(it["id"])
                if s_id in seen_item_ids:
                    continue
                cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
                clean.append({
                    "id": s_id,
                    "board_id": b_id,
                    "board_name": b_title,
                    "name": it["name"].strip(),
                    "commessa": "",
                    "progetto": "",
                    "stato": cols.get("project_status") or cols.get("color_mkn4s77r") or "",
                    "requires_commercial": req_comm
                })
                seen_item_ids.add(s_id)

        _PROJECTS_CACHE = clean
        _PROJECTS_CACHE_TIME = now
        logger.info(f"✅ Cache 10 schede ufficiali AMR aggiornata: {len(clean)} item attivi caricati.")
        return clean
    except Exception as e:
        logger.error(f"Errore caricamento schede AMR: {e}")
        return _PROJECTS_CACHE or []


# Mappatura membri del team AMR per tag e notifiche
TEAM_USERS = [
    {"id": "71533914", "name": "Riccardo Gazzola", "keywords": ["riccardo", "gazzola", "riccardo gazzola", "tech"]},
    {"id": "71478506", "name": "Alessandro Recchia", "keywords": ["alessandro recchia", "alessandro", "recchia", "amministrazione"]},
    {"id": "71489364", "name": "Gary Innocente", "keywords": ["gary innocente", "gary", "innocente", "ordini", "ufficio ordini"]},
    {"id": "71533482", "name": "Massimo Recchia", "keywords": ["massimo recchia", "massimo", "info", "commerciale"]},
    {"id": "71533503", "name": "Maurizio Nordio", "keywords": ["maurizio", "nordio", "maurizio nordio", "produzione"]},
    {"id": "71533953", "name": "Andrea Moscon", "keywords": ["andrea", "moscon", "andrea moscon", "finiture"]},
    {"id": "71533986", "name": "Taglio AMR", "keywords": ["tagga taglio", "avvisa taglio", "reparto taglio"]},
    {"id": "78115008", "name": "Antonio Ambrosino", "keywords": ["antonio", "ambrosino", "sicurezza", "hse"]},
    {"id": "78744209", "name": "Jamal Sriti", "keywords": ["jamal", "sriti", "verniciatura"]}
]

def normalize_continuous(s: str) -> str:
    """Rimuove ogni spazio e punteggiatura per matching a prova di errori fonetici: 'Dal Pian' -> 'dalpian'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_project_from_text(text: str, projects: list, tagged_users: list = None) -> dict:
    """Identifica con altissima precisione il progetto citato nel comando vocale."""
    t_clean = text.lower()
    t_continuous = normalize_continuous(text)
    t_words = [w for w in re.split(r"[\s\-_/.,;:?!]+", t_clean) if len(w) >= 3]
    
    # Raccogli parole chiave associate ai colleghi menzionati per evitare che "tagga Alessandro"
    # venga scambiato per una commessa/appuntamento chiamata "Alessandro"
    tagged_colleague_words = set()
    if tagged_users:
        for u in tagged_users:
            tagged_colleague_words.add(u["name"].lower())
            tagged_colleague_words.update(u["name"].lower().split())
            for kw in u.get("keywords", []):
                tagged_colleague_words.add(kw.lower())

    best_match = None
    best_score = 0.0

    for p in projects:
        p_name = p["name"].lower()
        p_proj = (p.get("progetto") or "").lower()
        p_comm = (p.get("commessa") or "").lower()
        b_id = str(p.get("board_id"))

        # Se l'item è sulla scheda APPUNTAMENTI e il suo nome coincide con il collega taggato, ignoralo
        if b_id == BOARD_APPUNTAMENTI and p_name in tagged_colleague_words:
            continue

        score = 0.0

        # Nome pulito della commessa (senza date finali es. "permasteelisa 22.07.26" -> "permasteelisa")
        core_name = re.sub(r"\s+\d{1,2}[\./\-]\d{1,2}[\./\-]\d{2,4}.*", "", p_name).strip()
        
        # 1. Matching continuo sul codice commessa (es. 26_565, 26 565, 26_24)
        if p_comm:
            c_norm = normalize_continuous(p_comm)
            if c_norm and len(c_norm) >= 3 and c_norm in t_continuous:
                score += 200.0

        # 2. Matching parola intera sul nome core (es. \bpermasteelisa\b, \bextreme\b, \bzanesco\b)
        if len(core_name) >= 3 and core_name not in tagged_colleague_words:
            if re.search(r"\b" + re.escape(core_name) + r"\b", t_clean):
                score += 180.0
            elif len(core_name) >= 4 and normalize_continuous(core_name) in t_continuous:
                score += 100.0

        # 3. Matching prima parola come parola intera
        first_word = p_name.split()[0].strip().lower()
        if len(first_word) >= 3 and first_word not in tagged_colleague_words:
            if re.search(r"\b" + re.escape(first_word) + r"\b", t_clean):
                score += 120.0

        p_tokens = [tok for tok in re.split(r"[\s\-_/.,]+", p_name) if len(tok) >= 3]
        if p_proj:
            p_tokens.extend([tok for tok in re.split(r"[\s\-_/.,]+", p_proj) if len(tok) >= 3])

        for w in t_words:
            if w in tagged_colleague_words:
                continue
            if w in p_tokens:
                score += 20.0
            elif w in p_name:
                score += 15.0
            elif p_proj and w in p_proj:
                score += 12.0
            else:
                close = difflib.get_close_matches(w, p_tokens, n=1, cutoff=0.8)
                if close:
                    score += 8.0

        # Priorità a schede di commessa e produzione rispetto ad altre
        if b_id == BOARD_GESTIONE_PROGETTI:
            score += 15.0
        elif b_id == BOARD_PRODUZIONE:
            score += 12.0
        elif b_id == BOARD_COMMERCIALE:
            score += 10.0
        elif b_id in [BOARD_PROGETTAZIONE, BOARD_INSTALLAZIONI]:
            score += 8.0

        if score > best_score:
            best_score = score
            best_match = p

    if best_score >= 35.0:
        return best_match
    return None


def extract_update_and_tags(spoken_text: str) -> tuple[list, str, bool]:
    """
    Rileva se il comando vocale richiede di pubblicare una nota o un messaggio su Monday,
    individua gli utenti da taggare e isola il testo del messaggio.
    """
    t_lower = spoken_text.lower()
    
    # Rilevamento utenti taggati
    tagged_users = []
    for u in TEAM_USERS:
        if any(kw in t_lower for kw in u["keywords"]):
            if u not in tagged_users:
                tagged_users.append(u)

    # Trigger di messaggi, note o comunicazioni
    is_message = any(trigger in t_lower for trigger in [
        "messaggio", "tagga", "tagghiamo", "diciamo che", "diciamo", "scrivi che", 
        "scrivi", "segna che", "nota", "avvisa", "comunica", "fai sapere", "invia"
    ]) or bool(tagged_users)

    if not is_message:
        return [], "", False

    # Estrazione del corpo del messaggio ripulito
    patterns = [
        r"(?:gli\s+)?diciamo\s+che\s+(.*)",
        r"(?:scrivi|segna|annota|comunica)\s+che\s+(.*)",
        r"(?:scrivi|segna|invia|manda)\s+(?:un\s+)?(?:messaggio|nota)\s*[:,\-]?\s*(.*)",
        r"tagghiamo\s+[a-zA-Z\s]+(?:e\s+gli\s+diciamo\s+che|,?\s*diciamo\s+che)?\s+(.*)",
    ]

    msg_body = ""
    for pat in patterns:
        m = re.search(pat, spoken_text, re.IGNORECASE)
        if m:
            msg_body = m.group(1).strip()
            break

    if not msg_body:
        # Pulizia dell'incipit
        clean_s = re.sub(r"^(?:mandiamo|invia|manda|scrivi|segna|tagga|tagghiamo)\s+.*?(?:che|:)\s*", "", spoken_text, flags=re.IGNORECASE)
        msg_body = clean_s.strip() if clean_s else spoken_text

    if msg_body:
        msg_body = msg_body[0].upper() + msg_body[1:]

    return tagged_users, msg_body, True



def process_voice_command(spoken_text: str, original_text: str = None, detected_lang: str = "it", current_user: dict = None) -> dict:
    """
    Elabora un comando vocale (in italiano o arabo/multilingua), interpreta l'intento e aggiorna Monday.com.
    Supporta tutte le 10 schede ufficiali del Workspace AMR applicando i permessi di accesso:
    - Schede riservate (COMMERCIALE, AMMINISTRAZIONE, APPUNTAMENTI): consentite solo a utenti commerciali/direzione
    - Schede operative (PRODUZIONE, GESTIONE PROGETTI, PROGETTAZIONE, INSTALLAZIONI, INVENTARIO, PALLET EPS, CONTESTAZIONI):
      accessibili a tutti gli utenti dell'officina.
    """
    if not original_text:
        # Se contiene caratteri arabi, traduce prima in italiano
        if re.search(r"[\u0600-\u06FF]", spoken_text):
            tr_res = translate_arabic_to_italian_if_needed(spoken_text)
            original_text = spoken_text
            spoken_text = tr_res.get("text", spoken_text)
            detected_lang = "ar"
        else:
            original_text = spoken_text
            detected_lang = "it"

    is_arabic = (detected_lang == "ar") or (original_text != spoken_text and bool(re.search(r"[\u0600-\u06FF]", original_text or "")))

    logger.info(f"🎙️ Elaborazione comando vocale (lingua: {detected_lang}, utente: {current_user.get('name') if current_user else 'Anonimo'}): \"{spoken_text}\" [Originale: \"{original_text}\"]")
    
    # Estrazione di eventuali utenti da taggare e del corpo del messaggio
    tagged_users, msg_body, is_update = extract_update_and_tags(spoken_text)

    projects = get_active_projects_cache()
    matched_project = match_project_from_text(spoken_text, projects, tagged_users=tagged_users)
    
    if not matched_project:
        return {
            "success": False,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "message": "Non sono riuscito a identificare la commessa o il cliente. Prova a specificare chiaramente il nome (es. 'EXTREME CORNICE', 'Zanesco', 'Led4Led'...)"
        }

    proj_name = matched_project["name"]
    proj_id = matched_project["id"]
    target_board_id = str(matched_project.get("board_id"))
    target_board_name = matched_project.get("board_name") or ALL_ACTIVE_BOARDS.get(target_board_id, "Monday")
    t_lower = spoken_text.lower()
    headers = {"Authorization": MONDAY_TOKEN, "API-Version": "2024-10", "Content-Type": "application/json"}

    # ── CONTROLLO ACCESSO E PERMESSI (RBAC) ──
    can_access, access_reason = can_user_access_board(current_user, target_board_id)
    if not can_access:
        logger.warning(f"⛔ Accesso negato per {current_user} sulla scheda {target_board_name} (#{proj_id})")
        return {
            "success": False,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": target_board_name,
            "message": f"🔒 {access_reason}"
        }

    # Indicatori di stato comuni
    is_done = any(w in t_lower for w in ["fatto", "completat", "finito", "terminat", "pronto", "chiuso"])
    is_blocked = any(w in t_lower for w in ["bloccat", "fermo", "manca", "pausa", "attesa", "problema"])
    is_progress = any(w in t_lower for w in ["in corso", "iniziato", "svolgimento", "al lavoro", "partito", "in produzione", "arrivati"])

    # ══════════════════════════════════════════════════════════════════
    # CASO 1: SCHEDA COMMERCIALE (1865049112) - Riservata Ordini/Info/Amministrazione
    # ══════════════════════════════════════════════════════════════════
    if target_board_id == BOARD_COMMERCIALE:
        comm_updates = []
        is_accepted = any(w in t_lower for w in ["accettat", "confermat", "approvat", "vinto", "preso", "confermato"])
        is_rejected = any(w in t_lower for w in ["rifiutat", "annullat", "perso", "bocciat", "scartat", "cancellat"])
        is_pending = any(w in t_lower for w in ["in attesa", "inviato", "in trattativa", "da inviare"])

        # Aggiornamento stato preventivo su Monday
        if is_accepted:
            mut_c = """
            mutation ($b: ID!, $it: ID!, $c1: String!, $v1: JSON!, $c2: String!, $v2: JSON!) {
              c1: change_column_value(board_id: $b, item_id: $it, column_id: $c1, value: $v1) { id }
              c2: change_column_value(board_id: $b, item_id: $it, column_id: $c2, value: $v2) { id }
            }
            """
            requests.post(MONDAY_API_URL, headers=headers, json={
                "query": mut_c,
                "variables": {
                    "b": BOARD_COMMERCIALE, "it": str(proj_id),
                    "c1": "color_mkn4s77r", "v1": json.dumps({"label": "FATTO"}),
                    "c2": "label_mkn37zp7", "v2": json.dumps({"label": "SI"})
                }
            }, timeout=10)
            comm_updates.append("Preventivo Accettato (SI / FATTO)")
        elif is_rejected:
            mut_c = """
            mutation ($b: ID!, $it: ID!, $c1: String!, $v1: JSON!, $c2: String!, $v2: JSON!) {
              c1: change_column_value(board_id: $b, item_id: $it, column_id: $c1, value: $v1) { id }
              c2: change_column_value(board_id: $b, item_id: $it, column_id: $c2, value: $v2) { id }
            }
            """
            requests.post(MONDAY_API_URL, headers=headers, json={
                "query": mut_c,
                "variables": {
                    "b": BOARD_COMMERCIALE, "it": str(proj_id),
                    "c1": "color_mkn4s77r", "v1": json.dumps({"label": "RIFIUTATO"}),
                    "c2": "label_mkn37zp7", "v2": json.dumps({"label": "NO"})
                }
            }, timeout=10)
            comm_updates.append("Preventivo Rifiutato (NO / RIFIUTATO)")
        elif is_pending:
            mut_c = """
            mutation ($b: ID!, $it: ID!, $c1: String!, $v1: JSON!, $c2: String!, $v2: JSON!) {
              c1: change_column_value(board_id: $b, item_id: $it, column_id: $c1, value: $v1) { id }
              c2: change_column_value(board_id: $b, item_id: $it, column_id: $c2, value: $v2) { id }
            }
            """
            requests.post(MONDAY_API_URL, headers=headers, json={
                "query": mut_c,
                "variables": {
                    "b": BOARD_COMMERCIALE, "it": str(proj_id),
                    "c1": "color_mkn4s77r", "v1": json.dumps({"label": "IN ATTESA"}),
                    "c2": "label_mkn37zp7", "v2": json.dumps({"label": "In attesa"})
                }
            }, timeout=10)
            comm_updates.append("Preventivo in Attesa")

        # Pubblica nota di aggiornamento commerciale su Monday
        note_text = msg_body or spoken_text
        user_name = current_user.get("name") if current_user else "Ufficio Commerciale"
        tags_html = " ".join([f"<b>@{u['name']}</b>" for u in tagged_users])
        comm_note_html = f"<p>💼 <b>Aggiornamento Commerciale ({user_name})</b>"
        if tags_html:
            comm_note_html += f" per {tags_html}:"
        else:
            comm_note_html += ":"
        comm_note_html += f"<br><b>{note_text}</b>"
        if comm_updates:
            comm_note_html += f"<br><span style='color:#037f4c;'>📌 Stato preventivo: {', '.join(comm_updates)}</span>"
        comm_note_html += "</p>"

        mut_up = f'mutation {{ create_update(item_id: "{proj_id}", body: {json.dumps(comm_note_html)}) {{ id }} }}'
        requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_up}, timeout=10)

        # Invia notifiche agli utenti taggati
        for u in tagged_users:
            notif_text = f"💼 Nota commerciale su preventivo {proj_name} da {user_name}: {note_text[:80]}"
            mut_notif = f'mutation {{ create_notification(user_id: {u["id"]}, target_id: {proj_id}, text: {json.dumps(notif_text)}, target_type: Project) {{ id }} }}'
            try:
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_notif}, timeout=8)
            except Exception:
                pass

        res_msg = f"Preventivo '{proj_name}' aggiornato su COMMERCIALE"
        if comm_updates:
            res_msg += f" [{', '.join(comm_updates)}]"
        if tagged_users:
            res_msg += f" con notifica a {', '.join([u['name'] for u in tagged_users])}"
        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": "COMMERCIALE",
            "message": f"✅ {res_msg}"
        }

    # ══════════════════════════════════════════════════════════════════
    # CASO 2: SCHEDA INSTALLAZIONI (1863989733) - Cantieri ed Esterni
    # ══════════════════════════════════════════════════════════════════
    if target_board_id == BOARD_INSTALLAZIONI:
        inst_label = "Fatto" if is_done else ("Bloccato" if is_blocked else ("In svolgimento" if is_progress else None))
        if inst_label:
            mut_inst = """
            mutation ($b: ID!, $it: ID!, $c: String!, $val: JSON!) {
              change_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
            }
            """
            requests.post(MONDAY_API_URL, headers=headers, json={
                "query": mut_inst,
                "variables": {"b": BOARD_INSTALLAZIONI, "it": str(proj_id), "c": "project_status", "val": json.dumps({"label": inst_label})}
            }, timeout=10)

        inst_note = f"<p>🏗️ <b>Aggiornamento Cantiere / Installazione</b>: {msg_body or spoken_text}</p>"
        requests.post(MONDAY_API_URL, headers=headers, json={
            "query": f'mutation {{ create_update(item_id: "{proj_id}", body: {json.dumps(inst_note)}) {{ id }} }}'
        }, timeout=10)

        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": "INSTALLAZIONI",
            "message": f"✅ Installazione '{proj_name}' aggiornata su INSTALLAZIONI (Stato: {inst_label or 'Ricevuto'})"
        }

    # ══════════════════════════════════════════════════════════════════
    # CASO 3: ALTRE SCHEDE SPECIALISTICHE (PROGETTAZIONE, INVENTARIO, ECC.)
    # ══════════════════════════════════════════════════════════════════
    if target_board_id in [BOARD_PROGETTAZIONE, BOARD_INVENTARIO, BOARD_APPUNTAMENTI, BOARD_AMMINISTRAZIONE, BOARD_CONTESTAZIONI, BOARD_PALLET_EPS]:
        icon_map = {
            BOARD_PROGETTAZIONE: "📐 Disegno / 3D",
            BOARD_INVENTARIO: "📦 Magazzino / Materiali",
            BOARD_APPUNTAMENTI: "📅 Appuntamento",
            BOARD_AMMINISTRAZIONE: "🏛️ Amministrazione",
            BOARD_CONTESTAZIONI: "⚠️ Contestazione",
            BOARD_PALLET_EPS: "♻️ Pallet EPS"
        }
        icon_title = icon_map.get(target_board_id, f"📋 {target_board_name}")
        author = current_user.get("name", "Operatore") if current_user else "Operatore"
        tags_html = " ".join([f"<b>@{u['name']}</b>" for u in tagged_users])
        spec_note = f"<p>{icon_title} ({author})"
        if tags_html:
            spec_note += f" per {tags_html}:"
        else:
            spec_note += ":"
        spec_note += f"<br><b>{msg_body or spoken_text}</b></p>"

        requests.post(MONDAY_API_URL, headers=headers, json={
            "query": f'mutation {{ create_update(item_id: "{proj_id}", body: {json.dumps(spec_note)}) {{ id }} }}'
        }, timeout=10)

        for u in tagged_users:
            notif_text = f"{icon_title} su {proj_name}: {msg_body[:80]}"
            mut_notif = f'mutation {{ create_notification(user_id: {u["id"]}, target_id: {proj_id}, text: {json.dumps(notif_text)}, target_type: Project) {{ id }} }}'
            try:
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_notif}, timeout=8)
            except Exception:
                pass

        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": target_board_name,
            "message": f"✅ Aggiornamento registrato sulla scheda {target_board_name} per '{proj_name}'"
        }

    # ══════════════════════════════════════════════════════════════════
    # CASO 4: SCHEDE CORE OFFICINA (GESTIONE PROGETTI 1865197409 & PRODUZIONE 1865050352)
    # ══════════════════════════════════════════════════════════════════
    update_published = False

    if is_update and msg_body:
        tags_html = " ".join([f"<b>@{u['name']}</b>" for u in tagged_users])
        tags_text = ", ".join([f"@{u['name']}" for u in tagged_users])
        
        lang_header = " (Trasmessa in Arabo ➔ Tradotta in Italiano)" if is_arabic else ""
        body_html = f"<p>🎙️ <b>Nota Vocale dall'Officina{lang_header}</b>"
        if tags_html:
            body_html += f" per {tags_html}:"
        else:
            body_html += ":"
        body_html += f"<br><b>{msg_body}</b>"
        if is_arabic and original_text and original_text != msg_body:
            body_html += f'<br><span style="color:#64748b; font-size:11px; font-style:italic;">Originale pronunciato: {original_text}</span>'
        body_html += "</p>"

        # 1. Pubblica nota su GESTIONE PROGETTI (1865197409)
        mut_up = f'mutation {{ create_update(item_id: "{proj_id}", body: {json.dumps(body_html)}) {{ id }} }}'
        try:
            r_up = requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_up}, timeout=10)
            logger.info(f"Update creato su GESTIONE PROGETTI #{proj_id}: {r_up.text[:150]}")
            update_published = True
        except Exception as e:
            logger.error(f"Errore creazione update Monday: {e}")

        # 2. Se presente, pubblica la stessa nota anche su PRODUZIONE (1865050352)
        produzione_id = matched_project.get("produzione_id")
        if produzione_id and produzione_id != proj_id:
            try:
                mut_up_prod = f'mutation {{ create_update(item_id: "{produzione_id}", body: {json.dumps(body_html)}) {{ id }} }}'
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_up_prod}, timeout=10)
                logger.info(f"Update replicato su PRODUZIONE #{produzione_id}")
            except Exception as e:
                logger.warning(f"Errore replica update su PRODUZIONE #{produzione_id}: {e}")

        # Invia notifica su Monday a ciascun utente menzionato
        for u in tagged_users:
            u_id = u["id"]
            notif_prefix = "🎙️ [Arabo ➔ Tradotto]" if is_arabic else "🎙️"
            notif_text = f"{notif_prefix} Nota vocale su commessa {proj_name}: {msg_body[:90]}"
            mut_notif = f'mutation {{ create_notification(user_id: {u_id}, target_id: {proj_id}, text: {json.dumps(notif_text)}, target_type: Project) {{ id }} }}'
            try:
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_notif}, timeout=10)
                logger.info(f"Notifica inviata a {u['name']} ({u_id}) per #{proj_id}")
            except Exception as e:
                logger.error(f"Errore notifica: {e}")

    # Riconoscimento dello Step / Reparto
    detected_step = None
    for kw, step_info in DEPARTMENT_STEPS.items():
        if kw in t_lower:
            detected_step = step_info
            break

    # Riconoscimento del Tempo (es. "2 ore e mezza")
    detected_time = parse_duration_italian(spoken_text)

    # Se è stato pubblicato un aggiornamento/nota senza step specifico:
    if update_published:
        confirm_msg = f"Aggiornamento scritto pubblicato su '{proj_name}'"
        if tagged_users:
            confirm_msg += f" con notifica a {tags_text}"
        confirm_msg += f": \"{msg_body}\""
        if is_arabic:
            confirm_msg = f"🇸🇦 Riconosciuto Arabo ➔ Tradotto: {confirm_msg}"
        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": "GESTIONE PROGETTI",
            "tagged_users": [u["name"] for u in tagged_users],
            "update_body": msg_body,
            "message": confirm_msg
        }

    # CASO 4A: Aggiornamento di uno Step di Reparto (es. "finito il taglio in 2 ore")
    if detected_step:
        target_board = detected_step["board"]
        step_name = detected_step["name"]
        
        q_find = f"""
        query {{
          boards(ids: ["{target_board}"]) {{
            items_page(limit: 100) {{
              items {{ id name }}
            }}
          }}
        }}
        """
        dept_items = requests.post(MONDAY_API_URL, headers=headers, json={"query": q_find}, timeout=10).json().get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
        dept_item = None
        for di in dept_items:
            if di["name"].strip().lower() == proj_name.lower():
                dept_item = di
                break

        updates_done = []
        new_label = "Fatto" if is_done else ("Bloccato" if is_blocked else ("In svolgimento" if is_progress else None))

        if dept_item:
            d_id = dept_item["id"]
            if detected_time:
                time_col = detected_step["time_col"]
                mut_t = """
                mutation ($b: ID!, $it: ID!, $c: String!, $val: String!) {
                  change_simple_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
                }
                """
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_t, "variables": {"b": target_board, "it": str(d_id), "c": time_col, "val": detected_time}}, timeout=10)
                updates_done.append(f"Tempo {step_name}: {detected_time}")

            if new_label:
                status_col = detected_step["status_col"]
                mut_s = """
                mutation ($b: ID!, $it: ID!, $c: String!, $val: JSON!) {
                  change_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
                }
                """
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_s, "variables": {"b": target_board, "it": str(d_id), "c": status_col, "val": json.dumps({"label": new_label})}}, timeout=10)
                updates_done.append(f"Stato {step_name}: {new_label}")

        # Inserisci una nota di avanzamento reparto su GESTIONE PROGETTI e PRODUZIONE
        dept_note_html = f"<p>⚙️ <b>Avanzamento Reparto ({step_name})</b>: {new_label or 'Completato'}"
        if detected_time:
            dept_note_html += f" in <b>{detected_time}</b>"
        dept_note_html += "</p>"
        for target_it in [proj_id, matched_project.get("produzione_id")]:
            if target_it:
                try:
                    mut_dept_note = f'mutation {{ create_update(item_id: "{target_it}", body: {json.dumps(dept_note_html)}) {{ id }} }}'
                    requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_dept_note}, timeout=8)
                except Exception:
                    pass

        confirm_msg = f"Aggiornata commessa '{proj_name}': {', '.join(updates_done) if updates_done else 'ricevuto'}"
        if is_arabic:
            confirm_msg = f"🇸🇦 Riconosciuto Arabo ➔ Tradotto: {confirm_msg}"
        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": "GESTIONE PROGETTI",
            "step": step_name,
            "time": detected_time,
            "status": "Fatto" if is_done else "In svolgimento",
            "message": confirm_msg
        }

    # CASO 4B: Aggiornamento Stato Generale Commessa (su GESTIONE PROGETTI 1865197409 e PRODUZIONE 1865050352)
    new_general_status = "Fatto" if is_done else ("Bloccato" if is_blocked else ("In corso" if is_progress else None))
    if new_general_status:
        gp_label = STATUS_GESTIONE_PROGETTI.get(new_general_status, "in produzione")
        mut_gp = """
        mutation ($b: ID!, $it: ID!, $c: String!, $val: JSON!) {
          change_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
        }
        """
        requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_gp, "variables": {"b": BOARD_GESTIONE_PROGETTI, "it": str(proj_id), "c": "project_status", "val": json.dumps({"label": gp_label})}}, timeout=10)
        logger.info(f"Stato su GESTIONE PROGETTI #{proj_id} aggiornato a '{gp_label}'")

        produzione_id = matched_project.get("produzione_id")
        if produzione_id:
            prod_label = STATUS_PRODUZIONE.get(new_general_status, "In svolgimento")
            mut_prod = """
            mutation ($b: ID!, $it: ID!, $c: String!, $val: JSON!) {
              change_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
            }
            """
            requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_prod, "variables": {"b": BOARD_PRODUZIONE, "it": str(produzione_id), "c": "color_mm1v12gx", "val": json.dumps({"label": prod_label})}}, timeout=10)
            logger.info(f"Stato su PRODUZIONE #{produzione_id} aggiornato a '{prod_label}'")

        confirm_msg = f"Stato commessa '{proj_name}' aggiornato a '{new_general_status}'"
        if is_arabic:
            confirm_msg = f"🇸🇦 Riconosciuto Arabo ➔ Tradotto: {confirm_msg}"
        return {
            "success": True,
            "language": detected_lang,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "project": proj_name,
            "board": "GESTIONE PROGETTI",
            "status": new_general_status,
            "message": confirm_msg
        }

    confirm_fallback = f"Commessa '{proj_name}' identificata sulla scheda {target_board_name}. Specificare l'azione (es. 'taglio fatto in 2 ore' o 'bloccato')."
    if is_arabic:
        confirm_fallback = f"🇸🇦 Riconosciuto Arabo ➔ Tradotto: {confirm_fallback}"
    return {
        "success": True,
        "language": detected_lang,
        "transcription": original_text or spoken_text,
        "italian_translation": spoken_text,
        "project": proj_name,
        "board": target_board_name,
        "message": confirm_fallback
    }


def preview_voice_command(spoken_text: str, original_text: str = None, detected_lang: str = "it", current_user: dict = None) -> dict:
    """
    Simula e anticipa l'azione del comando vocale SENZA modificare Monday.com.
    Fornisce la trascrizione, la commessa identificata, la scheda di destinazione,
    l'azione prevista e la verifica dei permessi per consentire all'utente di
    confermare, annullare o modificare prima dell'invio effettivo.
    """
    if not spoken_text:
        return {
            "success": False,
            "message": "Nessun testo vocale rilevato."
        }

    if not original_text:
        if re.search(r"[\u0600-\u06FF]", spoken_text):
            tr_res = translate_arabic_to_italian_if_needed(spoken_text)
            original_text = spoken_text
            spoken_text = tr_res.get("text", spoken_text)
            detected_lang = "ar"
        else:
            original_text = spoken_text
            detected_lang = "it"

    is_arabic = (detected_lang == "ar") or (original_text != spoken_text and bool(re.search(r"[\u0600-\u06FF]", original_text or "")))

    tagged_users, msg_body, is_update = extract_update_and_tags(spoken_text)
    projects = get_active_projects_cache()
    matched_project = match_project_from_text(spoken_text, projects, tagged_users=tagged_users)

    if not matched_project:
        return {
            "success": True,
            "matched": False,
            "transcription": original_text or spoken_text,
            "italian_translation": spoken_text,
            "language": detected_lang,
            "project_name": "",
            "board_name": "",
            "board_id": "",
            "action_summary": "Nessuna commessa identificata",
            "can_access": True,
            "access_reason": "",
            "clean_text": spoken_text,
            "message": "Non ho individuato la commessa. Puoi toccare ✏️ Modifica per inserire o correggere il nome."
        }

    proj_name = matched_project["name"]
    target_board_id = str(matched_project.get("board_id"))
    target_board_name = matched_project.get("board_name") or ALL_ACTIVE_BOARDS.get(target_board_id, "Monday")
    t_lower = spoken_text.lower()

    # Verifica autorizzazione
    can_access, access_reason = can_user_access_board(current_user, target_board_id)

    # Indicatori di stato comuni
    is_done = any(w in t_lower for w in ["fatto", "completat", "finito", "terminat", "pronto", "chiuso"])
    is_blocked = any(w in t_lower for w in ["bloccat", "fermo", "manca", "pausa", "attesa", "problema"])
    is_progress = any(w in t_lower for w in ["in corso", "iniziato", "svolgimento", "al lavoro", "partito", "in produzione", "arrivati"])

    action_parts = []

    if target_board_id == BOARD_COMMERCIALE:
        is_accepted = any(w in t_lower for w in ["accettat", "confermat", "approvat", "vinto", "preso", "confermato"])
        is_rejected = any(w in t_lower for w in ["rifiutat", "annullat", "perso", "bocciat", "scartat", "cancellat"])
        is_pending = any(w in t_lower for w in ["in attesa", "inviato", "in trattativa", "da inviare"])
        if is_accepted:
            action_parts.append("Preventivo Accettato (SI / FATTO)")
        elif is_rejected:
            action_parts.append("Preventivo Rifiutato (NO / RIFIUTATO)")
        elif is_pending:
            action_parts.append("Preventivo In Attesa")
        if msg_body:
            action_parts.append(f"Nota: \"{msg_body[:40]}...\"" if len(msg_body) > 40 else f"Nota: \"{msg_body}\"")
        if tagged_users:
            action_parts.append(f"Notifica a: {', '.join([u['name'] for u in tagged_users])}")
        action_summary = " • ".join(action_parts) if action_parts else "Nota su scheda Commerciale"

    elif target_board_id == BOARD_INSTALLAZIONI:
        inst_label = "Fatto" if is_done else ("Bloccato" if is_blocked else ("In svolgimento" if is_progress else None))
        if inst_label:
            action_parts.append(f"Stato Cantiere: {inst_label}")
        if msg_body:
            action_parts.append(f"Nota: \"{msg_body[:40]}\"")
        action_summary = " • ".join(action_parts) if action_parts else "Nota su Cantiere / Installazioni"

    elif target_board_id in [BOARD_PROGETTAZIONE, BOARD_INVENTARIO, BOARD_APPUNTAMENTI, BOARD_AMMINISTRAZIONE, BOARD_CONTESTAZIONI, BOARD_PALLET_EPS]:
        action_summary = f"Nota su {target_board_name}"
        if tagged_users:
            action_summary += f" • Tag: {', '.join([u['name'] for u in tagged_users])}"
        if msg_body:
            snippet = msg_body[:40] + ("..." if len(msg_body) > 40 else "")
            action_summary += f" • \"{snippet}\""

    else:
        # Reparti Officina (PRODUZIONE e GESTIONE PROGETTI)
        step_name = None
        for kw, step_info in DEPARTMENT_STEPS.items():
            if kw in t_lower:
                step_name = step_info["name"]
                break

        detected_time = parse_duration_italian(spoken_text)

        if step_name:
            action_parts.append(f"Fase: {step_name}")
            if is_done:
                action_parts.append("Stato: Fatto")
            elif is_blocked:
                action_parts.append("Stato: In Pausa")
            elif is_progress:
                action_parts.append("Stato: In Svolgimento")
        elif is_done:
            action_parts.append("Stato: Fatto")
        elif is_blocked:
            action_parts.append("Stato: Bloccato / Pausa")
        elif is_progress:
            action_parts.append("Stato: In Corso")

        if detected_time:
            action_parts.append(f"Tempo: {detected_time}")
        if is_update and msg_body:
            snippet = msg_body[:35] + ("..." if len(msg_body) > 35 else "")
            action_parts.append(f"Nota: \"{snippet}\"")
        if tagged_users:
            action_parts.append(f"Tag: {', '.join([u['name'] for u in tagged_users])}")

        action_summary = " • ".join(action_parts) if action_parts else "Avanzamento commessa"

    return {
        "success": True,
        "matched": True,
        "transcription": original_text or spoken_text,
        "italian_translation": spoken_text,
        "language": detected_lang,
        "project_name": proj_name,
        "board_name": target_board_name,
        "board_id": target_board_id,
        "action_summary": action_summary,
        "can_access": can_access,
        "access_reason": access_reason if not can_access else "",
        "clean_text": spoken_text
    }
