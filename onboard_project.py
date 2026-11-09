#!/usr/bin/env python3
"""
onboard_project.py — Sincronizza e trasferisce un progetto approvato (Preventivo Accettato = SI)
da NEW COMMERCIALE (2133436509) a GESTIONE PROGETTI NEW (2136092569),
copiando TUTTE le informazioni, le assegnazioni di reparto (Taglio, Fresa, Finitura, Disegno tecnico),
le date, la priorità, i contatti, i commenti e i file allegati, e innescando
immediatamente l'inoltro verso le schede di reparto (TAGLIO E FRESA, FINITURE, PROGETTAZIONE NEW).

REGOLE DI SICUREZZA:
- Opera ESCLUSIVAMENTE sulle schede nuove (2133436509 -> 2136092569).
- NON tocca MAI le vecchie schede operative (1865049112, 1865197409, 1865050352, 1988908927).
- Idempotente: se la commessa esiste già in GESTIONE PROGETTI NEW, aggiorna le colonne e i file mancanti senza creare duplicati.
"""

from __future__ import annotations
import os
import json
import time
import threading
import logging
import requests
import tempfile
import mimetypes
from pathlib import Path
from dotenv import load_dotenv

_onboard_lock = threading.Lock()
_recent_onboard_ts: dict[str, float] = {}

script_dir = Path(__file__).resolve().parent
load_dotenv(dotenv_path=script_dir / ".env")

logger = logging.getLogger("onboard_project")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] [ONBOARD] %(message)s")

MONDAY_TOKEN = os.getenv("MONDAY_API_TOKEN", "").strip()
MONDAY_API = "https://api.monday.com/v2"
MONDAY_HEADERS = {
    "Authorization": MONDAY_TOKEN,
    "Content-Type": "application/json",
    "API-Version": "2024-10",
}

BOARD_COMMERCIALE_NEW = "2133436509"
BOARD_PROGETTI_NEW = "2136092569"
BOARD_AMMINISTRAZIONE_NEW = "5107455153"
FORBIDDEN_BOARDS = {"1865049112", "1865197409", "1865050352", "1988908927", "1865824381"}


def _query(query: str, variables: dict = None) -> dict:
    for fb in FORBIDDEN_BOARDS:
        if fb in query or (variables and any(fb in str(v) for v in variables.values())):
            raise ValueError(f"CRITICAL SAFETY: Tentativo di accesso a vecchia board proibita {fb}")

    for attempt in range(4):
        try:
            r = requests.post(
                MONDAY_API,
                headers=MONDAY_HEADERS,
                json={"query": query, "variables": variables or {}},
                timeout=30,
            )
            r.raise_for_status()
            res = r.json()
            if "errors" in res:
                logger.warning("GraphQL warning/errors: %s", res["errors"])
            return res.get("data", {})
        except Exception as e:
            logger.warning("Tentativo GraphQL %d fallito: %s", attempt + 1, e)
            time.sleep((attempt + 1) * 2)
    return {}


def upload_file_to_monday(item_id: str, column_id: str, filepath: str) -> dict:
    headers = {
        "Authorization": MONDAY_TOKEN,
        "API-Version": "2024-10",
    }
    filename = Path(filepath).name
    mime_type, _ = mimetypes.guess_type(filepath)
    mime_type = mime_type or "application/octet-stream"

    logger.info("  ⬆️ Caricamento file '%s' su item #%s colonna '%s'...", filename, item_id, column_id)
    query = f'mutation ($file: File!) {{ add_file_to_column (item_id: {item_id}, column_id: "{column_id}", file: $file) {{ id }} }}'

    payload = {"query": query}
    with open(filepath, "rb") as f:
        files = {"variables[file]": (filename, f, mime_type)}
        r = requests.post("https://api.monday.com/v2/file", headers=headers, data=payload, files=files, timeout=90)
        r.raise_for_status()
        res = r.json()
        if "errors" in res:
            logger.error("  ❌ Errore caricamento file %s: %s", filename, res["errors"])
        else:
            logger.info("  ✅ File %s caricato con successo!", filename)
        return res


def download_file(url: str, dest_path: str, use_auth: bool = False) -> bool:
    try:
        hdrs = {"Authorization": MONDAY_TOKEN} if use_auth else {}
        r = requests.get(url, headers=hdrs, timeout=60, stream=True)
        if r.status_code == 200:
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
    except Exception as e:
        logger.error("  ❌ Errore download file %s: %s", url, e)
    return False


def _find_existing_project_in_gp_new(commessa_code: str, item_name: str, commercial_item_id: str, board_id: str = BOARD_PROGETTI_NEW, comm_col_id: str = "text_mm51yk45") -> dict | None:
    """Cerca se il progetto esiste già nella board target (GESTIONE PROGETTI NEW o AMMINISTRAZIONE NEW) per N° Commessa o per nome."""
    q = f"""
    query {{
      boards(ids: ["{board_id}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            assets {{ id name }}
            updates(limit: 30) {{ id body text_body }}
            column_values {{ id text value }}
          }}
        }}
      }}
    }}
    """
    data = _query(q)
    items = data.get("boards", [{}])[0].get("items_page", {}).get("items", [])
    target_name_clean = item_name.strip().lower()
    fallback_comm = f"COMM-{commercial_item_id}"

    for it in items:
        cols = {c["id"]: (c.get("text") or "").strip() for c in it.get("column_values", [])}
        existing_comm = cols.get(comm_col_id, "")
        if commessa_code and existing_comm == commessa_code:
            return it
        if existing_comm == fallback_comm:
            return it

    for it in items:
        if it["name"].strip().lower() == target_name_clean:
            return it

    return None


def _extract_asset_ids_from_col(col_obj: dict) -> set[str]:
    """Estrae gli assetId associati a una specifica colonna file."""
    out = set()
    if not col_obj or not col_obj.get("value"):
        return out
    try:
        val = json.loads(col_obj["value"])
        for f in val.get("files", []):
            aid = f.get("assetId")
            if aid is not None:
                out.add(str(aid))
    except Exception:
        pass
    return out


def onboard_item(commercial_item_id: str, force: bool = False) -> dict:
    """
    Trasferisce o aggiorna un item da NEW COMMERCIALE (2133436509) a GESTIONE PROGETTI NEW (2136092569)
    e avvia subito la sincronizzazione verso i reparti (TAGLIO E FRESA, FINITURE, PROGETTAZIONE NEW, AMMINISTRAZIONE NEW).
    Protetto da lock e debounce contro raffiche di webhook simultanei.
    """
    with _onboard_lock:
        now = time.time()
        last_ts = _recent_onboard_ts.get(str(commercial_item_id), 0.0)
        if not force and (now - last_ts) < 4.0:
            logger.info("⏭️ [DEBOUNCE] Onboarding per item #%s già eseguito %.1fs fa, ignoro evento duplicato.", commercial_item_id, now - last_ts)
            return {"ok": True, "debounced": True, "commercial_item_id": str(commercial_item_id)}
        _recent_onboard_ts[str(commercial_item_id)] = now
        return _onboard_item_locked(commercial_item_id, force=force)


def _onboard_item_locked(commercial_item_id: str, force: bool = False) -> dict:
    logger.info("🚀 Avvio sincronizzazione NEW COMMERCIALE -> GESTIONE PROGETTI NEW per item #%s", commercial_item_id)

    q_get = """
    query GetCommercialItem($ids: [ID!]!) {
      items(ids: $ids) {
        id
        name
        board { id name }
        updates(limit: 30) {
          id
          body
          text_body
        }
        column_values {
          id
          text
          value
          type
        }
        assets {
          id
          name
          url
          public_url
        }
      }
    }
    """
    data = _query(q_get, {"ids": [str(commercial_item_id)]})
    items = data.get("items", [])
    if not items:
        raise ValueError(f"Item commerciale {commercial_item_id} non trovato.")

    c_item = items[0]
    board_id = str(c_item.get("board", {}).get("id", ""))
    if board_id != BOARD_COMMERCIALE_NEW:
        logger.warning("Item #%s appartiene alla board %s (attesa %s). Ignoro.", commercial_item_id, board_id, BOARD_COMMERCIALE_NEW)
        return {"ok": False, "reason": f"Board {board_id} non è NEW COMMERCIALE"}

    col_values = {c["id"]: c for c in c_item.get("column_values", [])}

    # Verifica che "Preventivo Accettato" (label_mkn37zp7) sia "SI" (a meno che non sia forzato)
    prev_accettato = (col_values.get("label_mkn37zp7", {}).get("text") or "").strip().upper()
    if not force and prev_accettato != "SI":
        logger.info("⏭️ Item #%s ('%s') ha Preventivo Accettato='%s' (non 'SI'). Nessun inoltro a Gestione Progetti.", commercial_item_id, c_item["name"], prev_accettato)
        return {"ok": False, "skipped": True, "reason": f"Preventivo Accettato = '{prev_accettato}' (richiesto 'SI')"}

    # 1. Estrazione completa di TUTTE le colonne da NEW COMMERCIALE
    nome_progetto = (col_values.get("testo_mkn1sqb4", {}).get("text") or "").strip() or c_item["name"]
    referente = (col_values.get("dup__of_nome_referente_mkn3gf63", {}).get("text") or "").strip()
    email = (col_values.get("email_mkn1v37b", {}).get("text") or "").strip()
    telefono = (col_values.get("telefono_mkn3gd8v", {}).get("text") or "").strip()

    richiesta_lunga = (col_values.get("testo_lungo_mkn1ydyz", {}).get("text") or "").strip()
    specifiche_brevi = (col_values.get("text_mkypgtb8", {}).get("text") or "").strip()
    if richiesta_lunga and specifiche_brevi and specifiche_brevi not in richiesta_lunga:
        specifiche_complete = f"{richiesta_lunga}\n\n--- Specifiche ---\n{specifiche_brevi}"
    else:
        specifiche_complete = richiesta_lunga or specifiche_brevi

    num_commessa = (col_values.get("text_mm51yvbk", {}).get("text") or "").strip()
    if not num_commessa:
        num_commessa = f"COMM-{commercial_item_id}"
        # Scrivi il codice commessa generato anche su NEW COMMERCIALE per tracciabilità univoca
        try:
            _query(
                f'mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_COMMERCIALE_NEW}, item_id: {commercial_item_id}, column_values: $val) {{ id }} }}',
                {"val": json.dumps({"text_mm51yvbk": num_commessa})},
            )
        except Exception as e:
            logger.warning("Impossibile salvare N° Commessa su NEW COMMERCIALE: %s", e)

    data_preventivo = (col_values.get("date_mknkb99e", {}).get("text") or "").strip()
    data_consegna = (col_values.get("date4", {}).get("text") or "").strip()
    priorita = (col_values.get("color_mknssm0t", {}).get("text") or "").strip()
    disegno_tecnico = (col_values.get("color_mkrkwak4", {}).get("text") or "").strip()
    fresa = (col_values.get("color_mknsezab", {}).get("text") or "").strip()
    taglio = (col_values.get("color_mkns43r0", {}).get("text") or "").strip()
    finitura = (col_values.get("color_mknsghqz", {}).get("text") or "").strip()
    lav_esterna = (col_values.get("color_mky76qyw", {}).get("text") or "").strip()
    pagamento = (col_values.get("color_mknrnt50", {}).get("text") or "").strip()
    dati_fiscali = (col_values.get("text_mkp5k1vg", {}).get("text") or "").strip()
    budget_val = (col_values.get("numeri_mkn1w8z2", {}).get("text") or "").strip()
    fornitori_text = (col_values.get("dropdown_mknxwdw3", {}).get("text") or "").strip()

    operatore_val_raw = col_values.get("multiple_person_mknmf28d", {}).get("value")
    responsabile_val_raw = col_values.get("person", {}).get("value")

    # 2. Cerca se il progetto esiste già in GESTIONE PROGETTI NEW
    existing_gp = _find_existing_project_in_gp_new(num_commessa, c_item["name"], str(commercial_item_id))
    created_new = False

    if existing_gp:
        project_item_id = str(existing_gp["id"])
        existing_gp_cols = {c["id"]: (c.get("text") or "").strip() for c in existing_gp.get("column_values", [])}
        logger.info("🔄 Commessa già presente in GESTIONE PROGETTI NEW (#%s '%s'). Aggiorno tutte le informazioni...", project_item_id, existing_gp["name"])
    else:
        q_create = """
        mutation CreateProject($board_id: ID!, $item_name: String!) {
          create_item (board_id: $board_id, item_name: $item_name) {
            id
          }
        }
        """
        res_create = _query(q_create, {
            "board_id": BOARD_PROGETTI_NEW,
            "item_name": c_item["name"],
        })
        project_item_id = str(res_create["create_item"]["id"])
        existing_gp_cols = {}
        created_new = True
        logger.info("✅ Nuova riga creata in GESTIONE PROGETTI NEW con ID #%s", project_item_id)

    # 3. Costruisci il payload completo delle colonne per GESTIONE PROGETTI NEW
    op_values = {
        "testo_mkn1sqb4": nome_progetto,
        "text_mm51yk45": num_commessa,
    }
    if created_new or not existing_gp_cols.get("color_mm45raj9"):
        op_values["color_mm45raj9"] = {"label": "In corso"}
    if referente:
        op_values["dup__of_nome_referente_mkn3gf63"] = referente
    if email:
        op_values["email_mkn1v37b"] = {"email": email, "text": email}
    if telefono:
        clean_tel = "".join(ch for ch in telefono if ch.isdigit() or ch == "+")
        if clean_tel:
            op_values["telefono_mkn3gd8v"] = {"phone": clean_tel, "countryShortName": "IT"}
    if specifiche_complete:
        op_values["testo_lungo_mkn1ydyz"] = {"text": specifiche_complete}
    if data_preventivo:
        op_values["date_mknkb99e"] = {"date": data_preventivo}
    if data_consegna:
        op_values["date4"] = {"date": data_consegna}
    if priorita:
        op_values["color_mknssm0t"] = {"label": priorita}
    if disegno_tecnico in ("SI", "NO"):
        op_values["color_mkrkwak4"] = {"label": disegno_tecnico}
    if fresa == "SI":
        op_values["color_mknsezab"] = {"label": "SI"}
    if taglio == "SI":
        op_values["color_mkns43r0"] = {"label": "SI"}
    if finitura == "SI":
        op_values["color_mknsghqz"] = {"label": "SI"}
    if lav_esterna in ("SI", "Bloccato", "NO"):
        op_values["color_mkyn8z12"] = {"label": lav_esterna}
    if pagamento:
        op_values["color_mknrnt50"] = {"label": pagamento}
    if fornitori_text:
        labels_list = [x.strip() for x in fornitori_text.split(",") if x.strip()]
        if labels_list:
            op_values["dropdown_mknxwdw3"] = {"labels": labels_list}

    for src_raw, target_col in ((operatore_val_raw, "multiple_person_mknmf28d"), (responsabile_val_raw, "person")):
        if src_raw:
            try:
                parsed_p = json.loads(src_raw)
                if parsed_p.get("personsAndTeams"):
                    op_values[target_col] = {"personsAndTeams": parsed_p["personsAndTeams"]}
            except Exception:
                pass

    q_update = f"""
    mutation UpdateProjectColumns($column_values: JSON!) {{
      change_multiple_column_values (board_id: {BOARD_PROGETTI_NEW}, item_id: {project_item_id}, column_values: $column_values) {{
        id
      }}
    }}
    """
    _query(q_update, {"column_values": json.dumps(op_values)})
    logger.info("📋 Tutte le colonne (contatti, date, priorità, reparti Taglio/Fresa/Finitura/Disegno) copiate su GESTIONE PROGETTI NEW #%s.", project_item_id)

    # 3b. Sincronizza anche su AMMINISTRAZIONE NEW (5107455153)
    admin_item_id = None
    try:
        existing_adm = _find_existing_project_in_gp_new(
            num_commessa, c_item["name"], str(commercial_item_id),
            board_id=BOARD_AMMINISTRAZIONE_NEW, comm_col_id="text_mm8y3t4q"
        )
        adm_values = {
            "text_mm8y3t4q": num_commessa,
            "text_mm8y9vxm": nome_progetto,
        }
        if referente:
            adm_values["text_mm8ynnhj"] = referente
        if email:
            adm_values["email_mm8yvmcy"] = {"email": email, "text": email}
        if telefono:
            clean_tel = "".join(ch for ch in telefono if ch.isdigit() or ch == "+")
            if clean_tel:
                adm_values["phone_mm8yt1b9"] = {"phone": clean_tel, "countryShortName": "IT"}
        if dati_fiscali:
            adm_values["text_mm8yp2yy"] = dati_fiscali
        if data_preventivo:
            adm_values["date_mm8y1n2b"] = {"date": data_preventivo}
        if budget_val:
            adm_values["numeric_mm8yajj2"] = budget_val
        if pagamento:
            adm_values["text_mm8y8eqg"] = pagamento
        if priorita:
            adm_values["text_mm8yeabv"] = priorita
        if data_consegna:
            adm_values["date_mm8ymb9n"] = {"date": data_consegna}

        if existing_adm:
            admin_item_id = str(existing_adm["id"])
            _query(
                f"mutation ($val: JSON!) {{ change_multiple_column_values(board_id: {BOARD_AMMINISTRAZIONE_NEW}, item_id: {admin_item_id}, column_values: $val) {{ id }} }}",
                {"val": json.dumps(adm_values)},
            )
            logger.info("💼 Aggiornata commessa su AMMINISTRAZIONE NEW #%s", admin_item_id)
        else:
            adm_values["color_mm8ynr3s"] = {"label": "In produzione"}
            adm_values["color_mm8yt562"] = {"label": "DA FARE"}
            adm_values["color_mm8yk54d"] = {"label": "DA FARE"}
            adm_values["color_mm8yhrqg"] = {"label": "DA FARE"}
            adm_values["color_mm8yr340"] = {"label": "Non Consegnato"}
            adm_values["color_mm8y87rn"] = {"label": "DA FARE"}
            res_adm = _query(
                "mutation ($bid: ID!, $name: String!, $val: JSON!) { create_item(board_id: $bid, item_name: $name, column_values: $val) { id } }",
                {"bid": BOARD_AMMINISTRAZIONE_NEW, "name": c_item["name"], "val": json.dumps(adm_values)},
            )
            admin_item_id = str((res_adm.get("create_item") or {}).get("id") or "")
            logger.info("💼 Creata nuova commessa su AMMINISTRAZIONE NEW #%s", admin_item_id)
    except Exception as adm_err:
        logger.warning("Impossibile sincronizzare AMMINISTRAZIONE NEW: %s", adm_err)

    # 4. Copia i file allegati nelle rispettive colonne (Doc Prev. vs Disegno) evitando duplicati
    existing_asset_names = {a["name"].strip().lower() for a in (existing_gp.get("assets", []) if existing_gp else [])}
    existing_adm_assets = {a["name"].strip().lower() for a in (existing_adm.get("assets", []) if locals().get("existing_adm") else [])}
    doc_prev_only_ids = _extract_asset_ids_from_col(col_values.get("file_mknpery4"))
    dati_fisc_ids = _extract_asset_ids_from_col(col_values.get("file_mkt3h3j7"))
    doc_prev_asset_ids = doc_prev_only_ids | dati_fisc_ids
    disegno_asset_ids = _extract_asset_ids_from_col(col_values.get("file_mkn13rg5")) | _extract_asset_ids_from_col(col_values.get("file_mkyrtsyj"))

    assets = c_item.get("assets", [])
    copied_files = 0
    if assets:
        with tempfile.TemporaryDirectory() as tmp_dir:
            for asset in assets:
                filename = asset["name"]
                need_gp = filename.strip().lower() not in existing_asset_names
                need_adm = bool(admin_item_id) and (filename.strip().lower() not in existing_adm_assets)
                if not need_gp and not need_adm:
                    continue
                aid = str(asset.get("id", ""))
                p_url = asset.get("public_url")
                priv_url = asset.get("url")
                local_path = Path(tmp_dir) / filename

                ok_dl = False
                if p_url:
                    ok_dl = download_file(p_url, str(local_path), use_auth=False)
                if not ok_dl and priv_url:
                    ok_dl = download_file(priv_url, str(local_path), use_auth=True)

                if ok_dl:
                    if need_gp:
                        if aid in doc_prev_asset_ids and aid not in disegno_asset_ids:
                            target_file_col = "file_mknpery4"  # Doc Prev.
                        else:
                            target_file_col = "file_mkn13rg5"  # Disegno
                        upload_file_to_monday(project_item_id, target_file_col, str(local_path))
                        existing_asset_names.add(filename.strip().lower())
                        copied_files += 1
                    if need_adm and (aid in doc_prev_asset_ids or filename.lower().endswith(".pdf")):
                        adm_col = "file_mm8yh8m1" if aid in dati_fisc_ids else "file_mm8yb2dj"
                        upload_file_to_monday(admin_item_id, adm_col, str(local_path))
                        existing_adm_assets.add(filename.strip().lower())

    # 5. Copia gli Updates / Commenti (evitando duplicati)
    existing_updates_text = {(u.get("text_body") or "").strip()[:120] for u in (existing_gp.get("updates", []) if existing_gp else []) if u.get("text_body")}
    c_updates = list(reversed(c_item.get("updates", [])))
    copied_updates = 0
    for upd in c_updates:
        t_body = (upd.get("text_body") or "").strip()
        html_body = (upd.get("body") or "").strip()
        if not t_body:
            continue
        if t_body[:120] in existing_updates_text:
            continue
        mut_u = """
        mutation ($item_id: ID!, $body: String!) {
          create_update (item_id: $item_id, body: $body) { id }
        }
        """
        _query(mut_u, {"item_id": project_item_id, "body": html_body or t_body})
        existing_updates_text.add(t_body[:120])
        copied_updates += 1

    # 6. Innesca immediatamente la sincronizzazione da GESTIONE PROGETTI NEW verso i reparti (TAGLIO E FRESA, FINITURE, PROGETTAZIONE NEW, AMMINISTRAZIONE NEW)
    dept_result = {}
    try:
        import department_syncer
        dept_result = department_syncer.sync_project_to_departments(project_item_id)
        logger.info("🔄 Sincronizzazione a cascata verso i reparti completata: %s", dept_result)
    except Exception as e:
        logger.error("❌ Errore durante la sincronizzazione a cascata verso i reparti: %s", e, exc_info=True)

    result = {
        "ok": True,
        "commercial_item_id": str(commercial_item_id),
        "project_item_id": project_item_id,
        "admin_item_id": admin_item_id,
        "created_new": created_new,
        "commessa": num_commessa,
        "copied_files": copied_files,
        "copied_updates": copied_updates,
        "departments_sync": dept_result,
    }
    logger.info("🎉 Onboarding/Sync completato con successo: %s", result)
    return result


def sync_all_accepted_commercial_to_gp_new() -> dict:
    """
    Riconcilia tutte le commesse attive con Preventivo Accettato = SI su NEW COMMERCIALE (2133436509)
    che sono già presenti in GESTIONE PROGETTI NEW (o create di recente) per garantire che
    tutte le colonne compilate successivamente (Taglio, Fresa, Finitura, Disegno tecnico, Data consegna, ecc.)
    siano allineate al 100% su GESTIONE PROGETTI NEW e sui reparti.
    Ignora le vecchie commesse archiviate del 2025/inizio 2026 non presenti in GESTIONE PROGETTI NEW.
    """
    OLD_ARCHIVED_COMM_IDS = {"3205608443", "3205627378", "3205647710", "3205643127"}
    q = f"""
    query {{
      boards(ids: ["{BOARD_COMMERCIALE_NEW}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            column_values(ids: ["label_mkn37zp7", "text_mm51yvbk", "color_mkns43r0", "color_mknsezab", "color_mknsghqz", "color_mkrkwak4", "date4"]) {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    items = _query(q).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    gp_q = f"""
    query {{
      boards(ids: ["{BOARD_PROGETTI_NEW}"]) {{
        items_page(limit: 500) {{
          items {{
            id
            name
            column_values(ids: ["text_mm51yk45", "color_mkns43r0", "color_mknsezab", "color_mknsghqz", "color_mkrkwak4", "date4", "dup__of_nome_referente_mkn3gf63"]) {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    gp_items = _query(gp_q).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    gp_by_name = {it["name"].strip().lower(): it for it in gp_items}
    gp_by_comm = {}
    for it in gp_items:
        cols = {c["id"]: (c.get("text") or "").strip() for c in it["column_values"]}
        if cols.get("text_mm51yk45"):
            gp_by_comm[cols["text_mm51yk45"]] = it

    synced_count = 0
    for it in items:
        cid = str(it["id"])
        if cid in OLD_ARCHIVED_COMM_IDS:
            continue
        cols = {c["id"]: (c.get("text") or "").strip() for c in it["column_values"]}
        if cols.get("label_mkn37zp7") != "SI":
            continue

        comm_code = cols.get("text_mm51yvbk") or f"COMM-{cid}"
        gp_match = gp_by_comm.get(comm_code) or gp_by_name.get(it["name"].strip().lower())

        if not gp_match:
            # Nuova commessa con Preventivo Accettato = SI non ancora in GESTIONE PROGETTI NEW
            onboard_item(cid)
            synced_count += 1
        else:
            # Verifica se manca qualche flag di reparto, data consegna o N° commessa in GESTIONE PROGETTI NEW
            gp_cols = {c["id"]: (c.get("text") or "").strip() for c in gp_match["column_values"]}
            needs_update = False
            for col_check in ("color_mkns43r0", "color_mknsezab", "color_mknsghqz", "color_mkrkwak4", "date4"):
                if cols.get(col_check) and gp_cols.get(col_check) != cols.get(col_check):
                    needs_update = True
                    break
            if not gp_cols.get("text_mm51yk45"):
                needs_update = True
            if needs_update:
                onboard_item(cid)
                synced_count += 1

    return {"checked": len(items), "synced": synced_count}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Uso: python3 onboard_project.py <commercial_item_id | --reconcile>", flush=True)
        sys.exit(1)
    if sys.argv[1] == "--reconcile":
        print(json.dumps(sync_all_accepted_commercial_to_gp_new(), indent=2, ensure_ascii=False))
    else:
        print(json.dumps(onboard_item(sys.argv[1], force=True), indent=2, ensure_ascii=False))
