"""
Modulo Tracciamento Fasi di Lavorazione AMR Recchia (Zero Attrito)
===================================================================
Gestisce:
- Mappatura automatica tra Colonne Stato Reparto e Colonne Tempo (testo)
- Registrazione dell'orario di inizio (quando lo stato passa a "In svolgimento")
- Calcolo automatico delle ore lavorative reali (8h/giorno max, no weekend, no notti)
  quando lo stato passa a "Fatto"
- Scrittura del risultato nella colonna Tempo su Monday.com
- Preservazione dei valori inseriti manualmente dagli operatori
"""

import os
import json
import logging
from datetime import datetime
import zoneinfo
import requests
from dotenv import load_dotenv

import business_time

load_dotenv()
logger = logging.getLogger("step_time_tracker")

TZ_ROME = zoneinfo.ZoneInfo("Europe/Rome")
DATA_FILE = os.path.join(os.path.dirname(__file__), "step_tracker_store.json")

# Mappa Status Column ID -> Target Time Column ID (e nome descrittivo)
STEP_COLUMN_MAPPING = {
    # TAGLIO E FRESA (5086546323)
    "color_mm6wbsx6": {"time_col": "text_mm6wgnhe", "name": "Pantografo", "board_id": "5086546323"},
    "color_mm6wgk9e": {"time_col": "text_mm6werzq", "name": "Taglierina", "board_id": "5086546323"},
    "color_mm6wjwvr": {"time_col": "text_mm6wfxj8", "name": "Frese e Assemblaggio", "board_id": "5086546323"},
    "color_mm6w81nf": {"time_col": "text_mm6w9vp7", "name": "Assemblaggio e Imballo", "board_id": "5086546323"},
    
    # FINITURE (5088215890)
    "color_mm6w7av0": {"time_col": "text_mm6wa1rk", "name": "Resine", "board_id": "5088215890"},
    "color_mm6w4v44": {"time_col": "text_mm6w8jdw", "name": "Carteggiatura", "board_id": "5088215890"},
    "color_mm6wztyf": {"time_col": "text_mm6wpeck", "name": "Colore / Finitura", "board_id": "5088215890"},
    "color_mm6wve59": {"time_col": "text_mm6w293y", "name": "Cantieri / Installazioni", "board_id": "5088215890"},
}

IN_PROGRESS_LABELS = ["in svolgimento", "in corso", "lavorazione", "in lavorazione", "working on it"]
DONE_LABELS = ["fatto", "completato", "terminato", "done"]


def _load_store() -> dict:
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Errore lettura store {DATA_FILE}: {e}")
    return {"active_steps": {}, "completed_history": {}}


def _save_store(data: dict):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Errore salvataggio store {DATA_FILE}: {e}")


def handle_status_change(board_id: str, item_id: str, column_id: str, new_status_label: str) -> dict:
    """
    Gestisce la transizione di stato per una colonna di fase reparto.
    Restituisce un dizionario con l'esito dell'operazione.
    """
    mapping = STEP_COLUMN_MAPPING.get(column_id)
    if not mapping:
        return {"tracked": False, "reason": "Colonna non mappata come fase tracciabile"}

    status_clean = new_status_label.strip().lower() if new_status_label else ""
    store = _load_store()
    now_dt = datetime.now(TZ_ROME)
    key = f"{item_id}_{column_id}"

    # 1. Passaggio a IN SVOLGIMENTO -> Registra inizio
    if any(prog in status_clean for prog in IN_PROGRESS_LABELS):
        store["active_steps"][key] = {
            "board_id": board_id,
            "item_id": item_id,
            "column_id": column_id,
            "step_name": mapping["name"],
            "started_at": now_dt.isoformat(),
            "status": new_status_label
        }
        _save_store(store)
        logger.info(f"⏱️ Avviato tracciamento fase '{mapping['name']}' per item #{item_id} alle {now_dt.strftime('%H:%M:%S')}")
        return {"tracked": True, "action": "started", "step": mapping["name"], "started_at": now_dt.isoformat()}

    # 2. Passaggio a FATTO -> Calcola business hours e aggiorna colonna tempo
    elif any(d in status_clean for d in DONE_LABELS):
        step_data = store["active_steps"].pop(key, None)
        if not step_data:
            logger.warning(f"⚠️ Item #{item_id} passato a 'Fatto' per {mapping['name']} ma nessun orario di inizio registrato.")
            return {"tracked": False, "reason": "Nessun orario di inizio registrato (preservato eventuale valore manuale)"}

        try:
            start_dt = datetime.fromisoformat(step_data["started_at"])
            minutes = business_time.calculate_business_minutes(start_dt, now_dt)
            formatted_time = business_time.format_duration(minutes)

            # Salva nello storico
            if item_id not in store["completed_history"]:
                store["completed_history"][item_id] = {}
            store["completed_history"][item_id][mapping["name"]] = {
                "started_at": step_data["started_at"],
                "ended_at": now_dt.isoformat(),
                "business_minutes": minutes,
                "formatted": formatted_time
            }
            _save_store(store)

            # Scrivi su Monday.com
            time_col_id = mapping["time_col"]
            success = update_monday_time_column(board_id, item_id, time_col_id, formatted_time)

            logger.info(f"✅ Concluso step '{mapping['name']}' per #{item_id}: {minutes} min lavorativi effettivi -> {formatted_time}")
            return {
                "tracked": True,
                "action": "completed",
                "step": mapping["name"],
                "business_minutes": minutes,
                "formatted_time": formatted_time,
                "monday_updated": success
            }
        except Exception as e:
            logger.error(f"Errore calcolo tempo: {e}")
            return {"tracked": False, "error": str(e)}

    return {"tracked": False, "reason": f"Stato '{new_status_label}' non rilevante per il timer"}


def update_monday_time_column(board_id: str, item_id: str, column_id: str, text_value: str) -> bool:
    """Aggiorna il valore della colonna testo su Monday.com."""
    token = os.getenv("MONDAY_API_TOKEN")
    if not token:
        logger.error("MONDAY_API_TOKEN non impostato")
        return False

    mutation = """
    mutation ($board_id: ID!, $item_id: ID!, $col_id: String!, $val: String!) {
      change_simple_column_value(board_id: $board_id, item_id: $item_id, column_id: $col_id, value: $val) {
        id
      }
    }
    """
    try:
        resp = requests.post(
            "https://api.monday.com/v2",
            json={
                "query": mutation,
                "variables": {
                    "board_id": str(board_id),
                    "item_id": str(item_id),
                    "col_id": column_id,
                    "val": text_value
                }
            },
            headers={"Authorization": token, "API-Version": "2024-10"},
            timeout=10
        )
        data = resp.json()
        if "errors" in data:
            logger.error(f"Errore aggiornamento Monday: {data['errors']}")
            return False
        return True
    except Exception as e:
        logger.error(f"Eccezione aggiornamento Monday: {e}")
        return False


def get_active_tracked_steps() -> list:
    """Restituisce la lista di tutti gli step attualmente in svolgimento."""
    store = _load_store()
    now_dt = datetime.now(TZ_ROME)
    res = []
    for k, v in store.get("active_steps", {}).items():
        try:
            s_dt = datetime.fromisoformat(v["started_at"])
            cur_min = business_time.calculate_business_minutes(s_dt, now_dt)
            res.append({
                "key": k,
                "item_id": v["item_id"],
                "board_id": v["board_id"],
                "step_name": v["step_name"],
                "started_at": v["started_at"],
                "current_business_time": business_time.format_duration(cur_min),
                "business_minutes": cur_min
            })
        except Exception:
            pass
    return res


def is_night_or_weekend() -> bool:
    """Verifica se l'orario attuale è fuori dall'orario lavorativo (notte o weekend)."""
    now_dt = datetime.now(TZ_ROME)
    if now_dt.weekday() >= 5: # Sabato o Domenica
        return True
    now_time = now_dt.time()
    if now_time < business_time.MORNING_START or now_time >= business_time.AFTERNOON_END:
        return True
    return False
