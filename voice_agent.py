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

import os, re, json, difflib, logging, requests, base64, time
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("voice_agent")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN")
MONDAY_API_URL = "https://api.monday.com/v2"

# Schede Ufficiali Workspace AMR (Monday Originali)
BOARD_GESTIONE_PROGETTI = "1865197409"  # GESTIONE PROGETTI (Originale)
BOARD_PRODUZIONE = "1865050352"         # PRODUZIONE (Originale)
BOARD_COMMERCIALE = "1865049112"        # COMMERCIALE (Originale)
BOARD_PROGETTAZIONE = "1988908927"      # PROGETTAZIONE (Originale)
BOARD_TAGLIO = "5086546323"
BOARD_FINITURE = "5088215890"

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
    """Restituisce una sintesi dei clienti e numeri commessa attivi per guidare la trascrizione Gemini."""
    try:
        projs = get_active_projects_cache()
        hints = []
        for p in projs[:28]:
            c = p.get("commessa")
            n = p.get("name")
            if c and n:
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
    Recupera e mette in cache la lista dei progetti e commesse attive interrogando sia la scheda nuova
    (GESTIONE PROGETTI NEW) sia le schede storiche/vecchie (GESTIONE PROGETTI e COMMERCIALE),
    arricchendoli con i link bidirezionali per consentire l'uso simultaneo sia delle
    nuove che delle vecchie schede da parte degli operai in officina.
    """
    global _PROJECTS_CACHE, _PROJECTS_CACHE_TIME
    now = time.time()
    if not force_refresh and _PROJECTS_CACHE and (now - _PROJECTS_CACHE_TIME < PROJECTS_CACHE_TTL):
        return _PROJECTS_CACHE

    headers = {"Authorization": MONDAY_TOKEN, "API-Version": "2024-10"}

    q = f"""
    query {{
      boards(ids: ["{BOARD_GESTIONE_PROGETTI}", "{BOARD_PRODUZIONE}", "{BOARD_COMMERCIALE}"]) {{
        id
        name
        items_page(limit: 120) {{
          items {{
            id
            name
            state
            column_values(ids: ["testo_mkmnxqsk", "project_status", "color_mm1v12gx", "testo_mkn1sqb4", "color_mkn4s77r"]) {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    try:
        resp = requests.post(MONDAY_API_URL, headers=headers, json={"query": q}, timeout=12)
        boards_data = {str(b.get("id")): b for b in resp.json().get("data", {}).get("boards", [])}

        # Mappa item produzione per nome
        prod_map = {}
        for it in boards_data.get(BOARD_PRODUZIONE, {}).get("items_page", {}).get("items", []):
            if it.get("state") == "active":
                prod_map[it["name"].strip().lower()] = str(it["id"])

        # Mappa item commerciale per nome
        comm_map = {}
        for it in boards_data.get(BOARD_COMMERCIALE, {}).get("items_page", {}).get("items", []):
            if it.get("state") == "active":
                comm_map[it["name"].strip().lower()] = str(it["id"])

        clean = []
        # 1. Progetti principali da GESTIONE PROGETTI (1865197409)
        for it in boards_data.get(BOARD_GESTIONE_PROGETTI, {}).get("items_page", {}).get("items", []):
            if it.get("state") != "active":
                continue
            cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
            clean_name = it["name"].strip()
            norm_name = clean_name.lower()
            clean.append({
                "id": str(it["id"]),
                "board_id": BOARD_GESTIONE_PROGETTI,
                "name": clean_name,
                "commessa": cols.get("testo_mkmnxqsk", ""),
                "progetto": "",
                "stato": cols.get("project_status", ""),
                "produzione_id": prod_map.get(norm_name),
                "commerciale_id": comm_map.get(norm_name)
            })

        # 2. Preventivi o richieste da COMMERCIALE non ancora in Gestione Progetti
        existing_names = {p["name"].lower() for p in clean}
        for it in boards_data.get(BOARD_COMMERCIALE, {}).get("items_page", {}).get("items", []):
            if it.get("state") != "active":
                continue
            norm_name = it["name"].strip().lower()
            if norm_name not in existing_names:
                cols = {cv["id"]: cv.get("text") for cv in it.get("column_values", []) if cv.get("text")}
                clean.append({
                    "id": str(it["id"]),
                    "board_id": BOARD_COMMERCIALE,
                    "name": it["name"].strip(),
                    "commessa": "",
                    "progetto": cols.get("testo_mkn1sqb4", ""),
                    "stato": cols.get("color_mkn4s77r", ""),
                    "produzione_id": prod_map.get(norm_name),
                    "commerciale_id": str(it["id"])
                })
                existing_names.add(norm_name)

        _PROJECTS_CACHE = clean
        _PROJECTS_CACHE_TIME = now
        logger.info(f"✅ Cache schede originali AMR aggiornata: {len(clean)} commesse attive caricate.")
        return clean
    except Exception as e:
        logger.error(f"Errore caricamento progetti schede originali: {e}")
        return _PROJECTS_CACHE or []


# Mappatura membri del team AMR per tag e notifiche
TEAM_USERS = [
    {"id": "71533914", "name": "Riccardo Gazzola", "keywords": ["riccardo", "gazzola", "riccardo gazzola"]},
    {"id": "71478506", "name": "Alessandro Recchia", "keywords": ["alessandro recchia", "alessandro", "recchia"]},
    {"id": "71489364", "name": "Gary Innocente", "keywords": ["gary", "innocente", "gary innocente"]},
    {"id": "71533503", "name": "Maurizio Nordio", "keywords": ["maurizio", "nordio", "maurizio nordio"]},
    {"id": "71533953", "name": "Andrea Moscon", "keywords": ["andrea", "moscon", "andrea moscon"]},
    {"id": "71533986", "name": "Taglio AMR", "keywords": ["tagga taglio", "avvisa taglio", "reparto taglio"]},
    {"id": "78115008", "name": "Antonio Ambrosino", "keywords": ["antonio", "ambrosino"]},
    {"id": "78744209", "name": "Jamal Sriti", "keywords": ["jamal", "sriti"]}
]

def normalize_continuous(s: str) -> str:
    """Rimuove ogni spazio e punteggiatura per matching a prova di errori fonetici: 'Dal Pian' -> 'dalpian'."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_project_from_text(text: str, projects: list) -> dict:
    """Identifica con altissima precisione il progetto citato nel comando vocale."""
    t_clean = text.lower()
    t_continuous = normalize_continuous(text)
    t_words = [w for w in re.split(r"[\s\-_/.,;:?!]+", t_clean) if len(w) >= 3]
    
    best_match = None
    best_score = 0.0

    for p in projects:
        p_name = p["name"].lower()
        p_proj = (p.get("progetto") or "").lower()
        p_comm = (p.get("commessa") or "").lower()
        
        score = 0.0
        
        # 1. Matching continuo sul codice commessa (es. 26_565, 26 565, 26_24)
        if p_comm:
            c_norm = normalize_continuous(p_comm)
            if c_norm and len(c_norm) >= 3 and c_norm in t_continuous:
                score += 200.0

        # 2. Matching continuo sul primo token / parola chiave cliente (es. 'extreme', 'zanesco', 'led4led')
        p_first_word = normalize_continuous(p_name.split()[0])
        if len(p_first_word) >= 3 and p_first_word in t_continuous:
            score += 120.0

        # 3. Matching continuo su tutto il nome del progetto
        p_full_cont = normalize_continuous(p_name)
        if len(p_full_cont) >= 4 and (p_full_cont in t_continuous or t_continuous in p_full_cont):
            score += 150.0

        p_tokens = [tok for tok in re.split(r"[\s\-_/.,]+", p_name) if len(tok) >= 3]
        if p_proj:
            p_tokens.extend([tok for tok in re.split(r"[\s\-_/.,]+", p_proj) if len(tok) >= 3])

        for w in t_words:
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

        # Priorità a GESTIONE PROGETTI rispetto a COMMERCIALE
        if p.get("board_id") == BOARD_GESTIONE_PROGETTI:
            score += 10.0

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



def process_voice_command(spoken_text: str, original_text: str = None, detected_lang: str = "it") -> dict:
    """
    Elabora un comando vocale (in italiano o arabo/multilingua), interpreta l'intento e aggiorna Monday.com.
    Se il testo originale è in arabo, lo traduce per i record di Monday.com e per le schede di reparto.
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

    logger.info(f"🎙️ Elaborazione comando vocale (lingua: {detected_lang}): \"{spoken_text}\" [Originale: \"{original_text}\"]")
    
    projects = get_active_projects_cache()
    matched_project = match_project_from_text(spoken_text, projects)
    
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
    t_lower = spoken_text.lower()
    headers = {"Authorization": MONDAY_TOKEN, "API-Version": "2024-10", "Content-Type": "application/json"}

    # 1. VERIFICA SE È UNA NOTA / AGGIORNAMENTO SCRITTO CON MENZIONI O TAG
    tagged_users, msg_body, is_update = extract_update_and_tags(spoken_text)
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
        mut_up = f'''
        mutation {{
          create_update(item_id: "{proj_id}", body: {json.dumps(body_html)}) {{
            id
          }}
        }}
        '''
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
                mut_up_prod = f'''
                mutation {{
                  create_update(item_id: "{produzione_id}", body: {json.dumps(body_html)}) {{
                    id
                  }}
                }}
                '''
                r_prod = requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_up_prod}, timeout=10)
                logger.info(f"Update replicato su PRODUZIONE #{produzione_id}: {r_prod.text[:150]}")
            except Exception as e:
                logger.warning(f"Errore replica update su PRODUZIONE #{produzione_id}: {e}")

        # Invia notifica su Monday a ciascun utente menzionato
        for u in tagged_users:
            u_id = u["id"]
            notif_prefix = "🎙️ [Arabo ➔ Tradotto]" if is_arabic else "🎙️"
            notif_text = f"{notif_prefix} Nota vocale su commessa {proj_name}: {msg_body[:90]}"
            mut_notif = f'''
            mutation {{
              create_notification(
                user_id: {u_id},
                target_id: {proj_id},
                text: {json.dumps(notif_text)},
                target_type: Project
              ) {{
                id
              }}
            }}
            '''
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

    # Riconoscimento dello Stato
    is_done = any(w in t_lower for w in ["fatto", "completat", "finito", "terminat", "pronto"])
    is_blocked = any(w in t_lower for w in ["bloccat", "fermo", "manca", "pausa", "attesa"])
    is_progress = any(w in t_lower for w in ["in corso", "iniziato", "svolgimento", "al lavoro", "partito", "in produzione"])

    # Se è stato pubblicato un aggiornamento/nota:
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
            "tagged_users": [u["name"] for u in tagged_users],
            "update_body": msg_body,
            "message": confirm_msg
        }

    # CASO A: Aggiornamento di uno Step di Reparto (es. "finito il taglio in 2 ore")
    if detected_step:
        target_board = detected_step["board"]
        step_name = detected_step["name"]
        
        # Cerca l'item nella scheda di reparto corrispondente (per nome o commessa)
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
            # 1. Aggiorna Tempo se rilevato
            if detected_time:
                time_col = detected_step["time_col"]
                mut_t = """
                mutation ($b: ID!, $it: ID!, $c: String!, $val: String!) {
                  change_simple_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
                }
                """
                requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_t, "variables": {"b": target_board, "it": str(d_id), "c": time_col, "val": detected_time}}, timeout=10)
                updates_done.append(f"Tempo {step_name}: {detected_time}")

            # 2. Aggiorna Stato dello step se rilevato
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
            "step": step_name,
            "time": detected_time,
            "status": "Fatto" if is_done else "In svolgimento",
            "message": confirm_msg
        }

    # CASO B: Aggiornamento Stato Generale Commessa (su GESTIONE PROGETTI 1865197409 e PRODUZIONE 1865050352)
    new_general_status = "Fatto" if is_done else ("Bloccato" if is_blocked else ("In corso" if is_progress else None))
    if new_general_status:
        # 1. Aggiorna project_status su GESTIONE PROGETTI (1865197409)
        gp_label = STATUS_GESTIONE_PROGETTI.get(new_general_status, "in produzione")
        mut_gp = """
        mutation ($b: ID!, $it: ID!, $c: String!, $val: JSON!) {
          change_column_value(board_id: $b, item_id: $it, column_id: $c, value: $val) { id }
        }
        """
        requests.post(MONDAY_API_URL, headers=headers, json={"query": mut_gp, "variables": {"b": BOARD_GESTIONE_PROGETTI, "it": str(proj_id), "c": "project_status", "val": json.dumps({"label": gp_label})}}, timeout=10)
        logger.info(f"Stato su GESTIONE PROGETTI #{proj_id} aggiornato a '{gp_label}'")

        # 2. Aggiorna color_mm1v12gx su PRODUZIONE (1865050352)
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
            "status": new_general_status,
            "message": confirm_msg
        }


    confirm_fallback = f"Commessa '{proj_name}' identificata. Specificare l'azione (es. 'taglio fatto in 2 ore' o 'bloccato')."
    if is_arabic:
        confirm_fallback = f"🇸🇦 Riconosciuto Arabo ➔ Tradotto: {confirm_fallback}"
    return {
        "success": True,
        "language": detected_lang,
        "transcription": original_text or spoken_text,
        "italian_translation": spoken_text,
        "project": proj_name,
        "message": confirm_fallback
    }
