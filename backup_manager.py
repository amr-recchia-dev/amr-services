#!/usr/bin/env python3
"""
backup_manager.py — Sistema di Backup Automatico Multi-Destinazione (Ogni 4 Ore) per AMR Recchia S.r.l.

Esegue il salvataggio completo di:
  1. Registro di Sicurezza Timbrature & Dispositivi Autorizzati (SQLite .db + CSV + JSON)
  2. Database Email Processate & Lead Sito Wix (SQLite .db + CSV)
  3. Snapshot Completo Schede Monday.com (COMMERCIALE, NEW COMMERCIALE, GESTIONE PROGETTI,
     GESTIONE PROGETTI NEW, PRODUZIONE, TAGLIO E FRESA, FINITURE, PROGETTAZIONE, INSTALLAZIONI)
  4. Riepilogo Presenze & Ore Lavorate del mese corrente (JSON)

Destinazioni di salvataggio:
  1. Disco Persistente Server Render (/var/data/backups/) — rotazione ultimi 42 snapshot (7 giorni)
  2. Google Drive aziendale (amrrecchia2@gmail.com) -> "Backup Automatici Server AMR (Ogni 4h)/YYYY-MM/YYYY-MM-DD"
  3. Server Secondario Hetzner (46.62.254.242 -> /home/team/backups/amr-render/) tramite endpoint /api/backup/download e mirror diretto
"""

from __future__ import annotations

import datetime
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _env_file = Path(__file__).parent / ".env"
    if _env_file.exists():
        for _line in _env_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import db
import dispositivi
import drive_client
import timbra_log

logger = logging.getLogger("backup_manager")

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Rome")
except Exception:
    _TZ = None

BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "4"))
MAX_LOCAL_BACKUPS = int(os.getenv("MAX_LOCAL_BACKUPS", "42"))  # 7 giorni (6 backup/giorno)
DRIVE_BACKUP_FOLDER_NAME = "Backup Automatici Server AMR (Ogni 4h)"

MONDAY_BOARDS_TO_BACKUP = {
    "1865049112": "COMMERCIALE",
    "2133436509": "NEW_COMMERCIALE",
    "1865197409": "GESTIONE_PROGETTI",
    "2136092569": "GESTIONE_PROGETTI_NEW",
    "1865050352": "PRODUZIONE",
    "5086546323": "TAGLIO_E_FRESA",
    "5088215890": "FINITURE",
    "1988908927": "PROGETTAZIONE",
    "1863989733": "INSTALLAZIONI",
}

_BACKUP_LOCK = threading.Lock()
_LAST_BACKUP_STATUS: Dict = {
    "last_run_at": None,
    "last_success": None,
    "last_archive_path": None,
    "last_drive_folder_url": None,
    "last_drive_file_id": None,
    "counts": {},
    "errors": [],
}


def _now_rome() -> datetime.datetime:
    return datetime.datetime.now(_TZ) if _TZ else datetime.datetime.now()


def get_backup_dir() -> str:
    """Restituisce la cartella di backup su disco persistente Render (/var/data/backups) o locale."""
    custom_dir = os.getenv("AMR_BACKUP_DIR", "").strip()
    if custom_dir:
        os.makedirs(custom_dir, exist_ok=True)
        return custom_dir

    base_dir = os.getenv("TIMBRA_LOG_DIR", "").strip() or "/var/data"
    if os.path.isdir(base_dir) and (os.getenv("TIMBRA_LOG_DIR") or os.path.ismount(base_dir)):
        bdir = os.path.join(base_dir, "backups")
    else:
        bdir = str(Path(__file__).parent / "backups")

    os.makedirs(bdir, exist_ok=True)
    return bdir


def _safe_sqlite_copy(src_db_path: str, dst_db_path: str) -> bool:
    """Esegue una copia atomica a caldo di un database SQLite usando l'API .backup()."""
    if not src_db_path or not os.path.exists(src_db_path):
        return False
    try:
        src_conn = sqlite3.connect(src_db_path, timeout=15)
        dst_conn = sqlite3.connect(dst_db_path, timeout=15)
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
        return True
    except Exception as e:
        logger.warning("Copia SQLite .backup() fallita per %s (%s), provo copy2...", src_db_path, e)
        try:
            shutil.copy2(src_db_path, dst_db_path)
            return True
        except Exception as e2:
            logger.error("Impossibile copiare DB %s: %s", src_db_path, e2)
            return False


def _fetch_monday_board_snapshot(board_id: str, token: str) -> List[Dict]:
    """Scarica tutti gli item attivi di una board Monday.com per lo snapshot di backup."""
    items: List[Dict] = []
    cursor: Optional[str] = None
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "API-Version": "2024-10",
    }

    query_first = """
    query ($board_id: [ID!], $limit: Int!) {
      boards(ids: $board_id) {
        name
        items_page(limit: $limit) {
          cursor
          items {
            id
            name
            state
            created_at
            updated_at
            group {
              id
              title
            }
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
          state
          created_at
          updated_at
          group {
            id
            title
          }
          column_values {
            id
            text
            value
          }
        }
      }
    }
    """

    resp = requests.post(
        "https://api.monday.com/v2",
        headers=headers,
        json={"query": query_first, "variables": {"board_id": [str(board_id)], "limit": 200}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json().get("data", {})
    boards = data.get("boards", [])
    if not boards:
        return items

    page = boards[0].get("items_page", {})
    items.extend(page.get("items", []))
    cursor = page.get("cursor")

    while cursor:
        resp_n = requests.post(
            "https://api.monday.com/v2",
            headers=headers,
            json={"query": query_next, "variables": {"cursor": cursor, "limit": 200}},
            timeout=30,
        )
        resp_n.raise_for_status()
        page_n = resp_n.json().get("data", {}).get("next_items_page", {})
        batch = page_n.get("items", [])
        if not batch:
            break
        items.extend(batch)
        cursor = page_n.get("cursor")

    return items


def _collect_monday_snapshot() -> Dict:
    """Raccoglie lo snapshot completo di tutte le board Monday.com di AMR Recchia."""
    token = os.getenv("MONDAY_API_TOKEN", "").strip()
    snapshot = {
        "generated_at": _now_rome().isoformat(),
        "boards": {},
        "summary": {},
    }
    if not token:
        snapshot["error"] = "MONDAY_API_TOKEN non configurato"
        return snapshot

    for board_id, board_label in MONDAY_BOARDS_TO_BACKUP.items():
        try:
            items = _fetch_monday_board_snapshot(board_id, token)
            snapshot["boards"][board_id] = {
                "label": board_label,
                "items_count": len(items),
                "items": items,
            }
            snapshot["summary"][board_label] = len(items)
        except Exception as e:
            logger.warning("Backup Monday board %s (%s) fallito: %s", board_id, board_label, e)
            snapshot["boards"][board_id] = {
                "label": board_label,
                "error": str(e),
                "items_count": 0,
                "items": [],
            }
    return snapshot


def _collect_presenze_snapshot() -> Dict:
    """Raccoglie il riepilogo presenze del mese corrente dal servizio presenze."""
    month_str = _now_rome().strftime("%Y-%m")
    try:
        import presenze_service
        overview = presenze_service.get_all_employees_overview(month_str)
        return {
            "generated_at": _now_rome().isoformat(),
            "month": month_str,
            "overview": overview,
        }
    except Exception as e:
        logger.warning("Impossibile raccogliere snapshot presenze: %s", e)
        return {
            "generated_at": _now_rome().isoformat(),
            "month": month_str,
            "error": str(e),
        }


def _rotate_local_backups(backup_dir: str) -> int:
    """Mantiene solo gli ultimi MAX_LOCAL_BACKUPS archivi .zip su disco locale."""
    removed = 0
    try:
        archives = sorted(
            Path(backup_dir).glob("amr_backup_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_path in archives[MAX_LOCAL_BACKUPS:]:
            try:
                old_path.unlink()
                removed += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning("Errore rotazione backup locali: %s", e)
    return removed


def run_backup(upload_to_drive: bool = True, include_monday: bool = True) -> Dict:
    """
    Esegue un ciclo completo di backup:
      1. Raccoglie DB timbrature, dispositivi, email processate, Monday.com e presenze
      2. Crea l'archivio compresso amr_backup_<YYYY-MM-DD_HHMM>.zip su disco persistente
      3. Carica l'archivio e i CSV/JSON leggibili su Google Drive (amrrecchia2@gmail.com)
    """
    if not _BACKUP_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "Backup già in esecuzione", "status": _LAST_BACKUP_STATUS}

    now = _now_rome()
    ts_str = now.strftime("%Y-%m-%d_%H%M")
    month_folder_name = now.strftime("%Y-%m")
    day_folder_name = now.strftime("%Y-%m-%d")
    backup_dir = get_backup_dir()
    errors: List[str] = []
    counts: Dict[str, int] = {}

    logger.info("🛡️ [BACKUP 4H] Avvio backup multi-destinazione AMR (%s)...", ts_str)

    try:
        with tempfile.TemporaryDirectory(prefix=f"amr_bkp_{ts_str}_") as tmpdir:
            tmp_path = Path(tmpdir)

            # ── 1. Timbrature & Dispositivi ────────────────────────────────────
            timbra_csv = timbra_log.esporta_csv()
            timbra_stato = timbra_log.stato()
            disp_data = dispositivi.elenco()

            # Se non siamo sul disco persistente di Render (es. esecuzione da server secondario Hetzner),
            # scarica il registro di sicurezza e i dispositivi direttamente da https://amr-services.onrender.com
            render_url = os.getenv("RENDER_PRIMARY_URL", "https://amr-services.onrender.com").rstrip("/")
            if not timbra_stato.get("attivo") and render_url:
                try:
                    r_st = requests.post(f"{render_url}/api/timbra/sicurezza", json={"pin": "4921"}, timeout=20)
                    if r_st.ok:
                        timbra_stato = r_st.json()
                    r_csv = requests.post(f"{render_url}/api/timbra/sicurezza", json={"pin": "4921", "formato": "csv"}, timeout=30)
                    if r_csv.ok and r_csv.text:
                        timbra_csv = r_csv.text
                    r_disp = requests.post(f"{render_url}/api/timbra/dispositivi", json={"pin": "4921", "azione": "lista"}, timeout=20)
                    if r_disp.ok:
                        disp_data = r_disp.json()
                    logger.info("🌐 [BACKUP 4H] Prelevati registro sicurezza e dispositivi live da %s", render_url)
                except Exception as e_rem:
                    logger.warning("Impossibile prelevare timbrature live da Render (%s): %s", render_url, e_rem)

            timbra_csv_file = tmp_path / f"registro_sicurezza_timbrature_{ts_str}.csv"
            timbra_csv_file.write_text(timbra_csv, encoding="utf-8")
            counts["timbrature_csv_lines"] = max(0, len(timbra_csv.splitlines()) - 1)

            timbra_db_src = timbra_stato.get("percorso") or getattr(timbra_log, "_DB_PATH", "")
            timbra_db_dst = tmp_path / "registro_sicurezza.db"
            if timbra_db_src and os.path.exists(timbra_db_src):
                if _safe_sqlite_copy(timbra_db_src, str(timbra_db_dst)):
                    counts["registro_sicurezza_db_bytes"] = timbra_db_dst.stat().st_size

            disp_file = tmp_path / "dispositivi_autorizzati.json"
            disp_file.write_text(json.dumps(disp_data, ensure_ascii=False, indent=2), encoding="utf-8")

            # ── 2. Processed Emails & Wix Leads DB ─────────────────────────────
            try:
                db.init_db()
                emails_csv = db.esporta_csv()
                emails_csv_file = tmp_path / f"processed_emails_{ts_str}.csv"
                emails_csv_file.write_text(emails_csv, encoding="utf-8")
                counts["processed_emails_count"] = max(0, len(emails_csv.splitlines()) - 1)

                emails_db_dst = tmp_path / "processed_emails.db"
                if os.path.exists(db.DB_PATH):
                    if _safe_sqlite_copy(db.DB_PATH, str(emails_db_dst)):
                        counts["processed_emails_db_bytes"] = emails_db_dst.stat().st_size
            except Exception as e:
                err_msg = f"Errore backup processed_emails: {e}"
                logger.warning(err_msg)
                errors.append(err_msg)

            # ── 3. Monday.com Boards Snapshot ──────────────────────────────────
            monday_file = None
            if include_monday:
                try:
                    monday_snap = _collect_monday_snapshot()
                    monday_file = tmp_path / f"monday_snapshot_{ts_str}.json"
                    monday_file.write_text(
                        json.dumps(monday_snap, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    counts["monday_boards"] = len(monday_snap.get("summary", {}))
                    counts["monday_total_items"] = sum(monday_snap.get("summary", {}).values())
                except Exception as e:
                    err_msg = f"Errore snapshot Monday.com: {e}"
                    logger.warning(err_msg)
                    errors.append(err_msg)

            # ── 4. Presenze Snapshot ───────────────────────────────────────────
            try:
                pres_snap = _collect_presenze_snapshot()
                pres_file = tmp_path / f"presenze_snapshot_{ts_str}.json"
                pres_file.write_text(
                    json.dumps(pres_snap, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                err_msg = f"Errore snapshot Presenze: {e}"
                logger.warning(err_msg)
                errors.append(err_msg)

            # ── 5. Manifest ────────────────────────────────────────────────────
            manifest = {
                "timestamp": now.isoformat(),
                "timestamp_tag": ts_str,
                "counts": counts,
                "timbra_stato": timbra_stato,
                "errors": errors,
            }
            manifest_file = tmp_path / "manifest.json"
            manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            # ── 6. Creazione Archivio ZIP su Disco Persistente (Destinazione 1) ─
            zip_filename = f"amr_backup_{ts_str}.zip"
            zip_filepath = os.path.join(backup_dir, zip_filename)
            with zipfile.ZipFile(zip_filepath, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for fpath in tmp_path.iterdir():
                    if fpath.is_file():
                        zf.write(fpath, arcname=fpath.name)

            counts["zip_size_bytes"] = os.path.getsize(zip_filepath)

            # Aggiorna puntatore latest
            latest_zip = os.path.join(backup_dir, "amr_backup_latest.zip")
            shutil.copy2(zip_filepath, latest_zip)
            latest_manifest_path = os.path.join(backup_dir, "latest_manifest.json")
            with open(latest_manifest_path, "w", encoding="utf-8") as mf:
                json.dump({**manifest, "archive_filename": zip_filename, "archive_path": zip_filepath}, mf, ensure_ascii=False, indent=2)

            _rotate_local_backups(backup_dir)
            logger.info("💾 [BACKUP 4H] Salvato su disco: %s (%d bytes)", zip_filepath, counts["zip_size_bytes"])

            # ── 7. Upload su Google Drive amrrecchia2@gmail.com (Destinazione 2) ─
            drive_folder_url = None
            drive_zip_id = None
            if upload_to_drive:
                try:
                    root_bkp_id = drive_client.get_or_create_folder(DRIVE_BACKUP_FOLDER_NAME, drive_client.DRIVE_ROOT)
                    month_id = drive_client.get_or_create_folder(month_folder_name, root_bkp_id)
                    day_id = drive_client.get_or_create_folder(day_folder_name, month_id)
                    drive_folder_url = drive_client.get_folder_url(day_id)

                    # Carica lo ZIP completo
                    drive_zip_id = drive_client.upload_file(zip_filepath, day_id, filename=zip_filename)

                    # Carica anche il CSV timbrature e lo snapshot Monday leggibili direttamente da Drive
                    drive_client.upload_file(str(timbra_csv_file), day_id, filename=timbra_csv_file.name)
                    if monday_file and monday_file.exists():
                        drive_client.upload_file(str(monday_file), day_id, filename=monday_file.name)

                    logger.info("☁️ [BACKUP 4H] Caricato su Google Drive: %s (zip_id=%s)", drive_folder_url, drive_zip_id)
                except Exception as e:
                    err_msg = f"Errore upload Google Drive: {e}"
                    logger.error("❌ [BACKUP 4H] %s", err_msg)
                    errors.append(err_msg)

            _LAST_BACKUP_STATUS.update({
                "last_run_at": now.isoformat(),
                "last_success": len(errors) == 0,
                "last_archive_path": zip_filepath,
                "last_archive_filename": zip_filename,
                "last_drive_folder_url": drive_folder_url,
                "last_drive_file_id": drive_zip_id,
                "counts": counts,
                "errors": errors,
            })

            return {
                "ok": True,
                "timestamp": now.isoformat(),
                "archive_filename": zip_filename,
                "archive_path": zip_filepath,
                "drive_folder_url": drive_folder_url,
                "drive_file_id": drive_zip_id,
                "counts": counts,
                "errors": errors,
            }
    except Exception as e:
        logger.error("❌ [BACKUP 4H] Errore fatale durante il backup: %s", e, exc_info=True)
        _LAST_BACKUP_STATUS.update({
            "last_run_at": now.isoformat(),
            "last_success": False,
            "errors": [str(e)],
        })
        return {"ok": False, "error": str(e)}
    finally:
        _BACKUP_LOCK.release()


def get_status() -> Dict:
    """Restituisce lo stato dell'ultimo backup e la lista dei backup presenti su disco."""
    backup_dir = get_backup_dir()
    archives = []
    try:
        for p in sorted(Path(backup_dir).glob("amr_backup_2*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[:15]:
            archives.append({
                "filename": p.name,
                "size_bytes": p.stat().st_size,
                "modified_at": datetime.datetime.fromtimestamp(p.stat().st_mtime, _TZ).isoformat() if _TZ else p.stat().st_mtime,
            })
    except Exception:
        pass
    return {
        "backup_dir": backup_dir,
        "interval_hours": BACKUP_INTERVAL_HOURS,
        "last_backup": _LAST_BACKUP_STATUS,
        "recent_archives": archives,
    }


def get_latest_archive_path() -> Optional[str]:
    """Restituisce il percorso del file ZIP di backup più recente su disco."""
    backup_dir = get_backup_dir()
    latest = os.path.join(backup_dir, "amr_backup_latest.zip")
    if os.path.exists(latest):
        return latest
    archives = sorted(Path(backup_dir).glob("amr_backup_2*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    if archives:
        return str(archives[0])
    return None


def start_backup_loop() -> None:
    """Loop demone in background che esegue il backup automatico ogni 4 ore."""
    logger.info("🛡️ [BACKUP 4H] Avvio thread demone backup automatico (ogni %d ore)...", BACKUP_INTERVAL_HOURS)
    # Attendi 60 secondi all'avvio del server per non rallentare il boot di Gunicorn
    time.sleep(60)
    interval_sec = max(3600, BACKUP_INTERVAL_HOURS * 3600)
    while True:
        try:
            res = run_backup(upload_to_drive=True, include_monday=True)
            logger.info("🛡️ [BACKUP 4H] Ciclo completato: ok=%s, drive=%s", res.get("ok"), res.get("drive_folder_url"))
        except Exception as e:
            logger.error("❌ [BACKUP 4H] Errore nel loop di backup: %s", e, exc_info=True)
        time.sleep(interval_sec)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    result = run_backup(upload_to_drive=True, include_monday=True)
    print(json.dumps(result, indent=2, ensure_ascii=False))
