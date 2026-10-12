"""
Registro di sicurezza delle timbrature (copia indipendente da Google).

Ogni timbratura che arriva su /api/timbra/registra viene scritta qui PRIMA di
essere inoltrata ad Apps Script, poi si aggiorna l'esito dell'invio.
Se Google non risponde, o se il foglio viene modificato, l'originale resta qui.

Il registro vive sul Disco persistente di Render (montato su /var/data).
Senza disco il registro resta spento: scrivere sul filesystem effimero
darebbe una falsa sicurezza (verrebbe cancellato a ogni deploy).

Il PIN NON viene salvato in chiaro: si salva il nome del dipendente e
un'impronta (hash) del PIN, utile solo per confronti.
"""
from __future__ import annotations

import csv
import datetime
import hashlib
import io
import logging
import os
import sqlite3
import threading

logger = logging.getLogger(__name__)

_ENV_DIR = os.getenv("TIMBRA_LOG_DIR", "").strip()
_BASE_DIR = _ENV_DIR or "/var/data"
_DB_PATH = os.path.join(_BASE_DIR, "timbrature", "registro_sicurezza.db")
_SALT = os.getenv("TIMBRA_LOG_SALT", "amr-log-7Qw2")
_LOCK = threading.Lock()
_INIT_DONE = False

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Rome")
except Exception:  # pragma: no cover
    _TZ = None


def is_enabled() -> bool:
    """Attivo solo se esiste un disco persistente (o una cartella indicata a mano)."""
    if not os.path.isdir(_BASE_DIR):
        return False
    if _ENV_DIR:
        return True
    return os.path.ismount(_BASE_DIR)


def _now_local() -> datetime.datetime:
    return datetime.datetime.now(_TZ) if _TZ else datetime.datetime.now()


def _connect() -> sqlite3.Connection:
    global _INIT_DONE
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=10)
    if not _INIT_DONE:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS timbrature (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ricevuta_il      TEXT NOT NULL,
                dipendente       TEXT,
                pin_impronta     TEXT,
                azione           TEXT,
                orario_telefono  TEXT,
                lat              REAL,
                lng              REAL,
                precisione_m     REAL,
                client_id        TEXT,
                ip               TEXT,
                esito            TEXT DEFAULT 'in invio',
                orario_foglio    TEXT,
                dettaglio        TEXT,
                aggiornata_il    TEXT,
                tentativi        INTEGER DEFAULT 1
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_tb_data ON timbrature(ricevuta_il)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_tb_cid ON timbrature(client_id)")
        conn.commit()
        _INIT_DONE = True
    return conn


def _pin_hash(pin: str) -> str:
    return hashlib.sha256((_SALT + str(pin)).encode()).hexdigest()[:16]


def _fmt_ts(ts) -> str:
    try:
        ts = float(ts)
        if ts > 0:
            d = datetime.datetime.fromtimestamp(ts / 1000.0, _TZ) if _TZ else datetime.datetime.fromtimestamp(ts / 1000.0)
            return d.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        pass
    return ""


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # scarta NaN
    except (TypeError, ValueError):
        return None


def registra_ricevuta(pin: str, dipendente: str | None, azione: str, ts, lat, lng, acc,
                      client_id: str, ip: str) -> int | None:
    """Salva la timbratura appena arrivata. Ritorna l'id riga (o None se registro spento/errore)."""
    if not is_enabled():
        return None
    try:
        with _LOCK:
            conn = _connect()
            cid = (client_id or "")[:64]
            if cid:
                gia = conn.execute("SELECT id FROM timbrature WHERE client_id=? ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
                if gia:
                    conn.execute("UPDATE timbrature SET tentativi=COALESCE(tentativi,1)+1 WHERE id=?", (gia[0],))
                    conn.commit()
                    conn.close()
                    return gia[0]
            cur = conn.execute(
                """INSERT INTO timbrature (ricevuta_il, dipendente, pin_impronta, azione, orario_telefono,
                                           lat, lng, precisione_m, client_id, ip)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    _now_local().strftime("%Y-%m-%d %H:%M:%S"),
                    dipendente or "(PIN non in elenco locale)",
                    _pin_hash(pin),
                    azione,
                    _fmt_ts(ts),
                    _num(lat), _num(lng), _num(acc),
                    cid,
                    (ip or "")[:64],
                ),
            )
            conn.commit()
            rid = cur.lastrowid
            conn.close()
            return rid
    except Exception as e:  # il registro non deve MAI bloccare la timbratura
        logger.warning(f"[TIMBRA-LOG] Scrittura fallita: {e}")
        return None


def registra_esito(rid: int | None, data: dict | None, http_code: int) -> None:
    """Aggiorna l'esito dell'invio a Google (registrata / doppio tocco / errore)."""
    if not rid or not is_enabled():
        return
    data = data or {}
    if data.get("ok") and data.get("duplicata"):
        esito = "doppio tocco (già registrata)"
    elif data.get("ok"):
        esito = "registrata nel foglio"
    elif data.get("retry") or http_code >= 500:
        esito = "Google non raggiungibile (il telefono riprova)"
    else:
        esito = "rifiutata"
    try:
        with _LOCK:
            conn = _connect()
            conn.execute(
                """UPDATE timbrature
                      SET esito=?, orario_foglio=?, dettaglio=?, aggiornata_il=?,
                          dipendente=COALESCE(NULLIF(?, ''), dipendente)
                    WHERE id=? AND COALESCE(esito,'') NOT IN ('registrata nel foglio', 'doppio tocco (già registrata)')""",
                (
                    esito,
                    str(data.get("orario") or ""),
                    str(data.get("error") or "")[:300],
                    _now_local().strftime("%Y-%m-%d %H:%M:%S"),
                    str(data.get("nome") or ""),
                    rid,
                ),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.warning(f"[TIMBRA-LOG] Aggiornamento esito fallito: {e}")


def stato() -> dict:
    """Riepilogo per gli amministratori."""
    if not is_enabled():
        return {"attivo": False, "motivo": "Disco persistente non collegato (/var/data)"}
    try:
        with _LOCK:
            conn = _connect()
            tot = conn.execute("SELECT COUNT(*) FROM timbrature").fetchone()[0]
            ultima = conn.execute(
                "SELECT ricevuta_il, dipendente, azione, esito FROM timbrature ORDER BY id DESC LIMIT 1"
            ).fetchone()
            non_reg = conn.execute(
                "SELECT COUNT(*) FROM timbrature WHERE esito NOT IN ('registrata nel foglio', 'doppio tocco (già registrata)')"
            ).fetchone()[0]
            conn.close()
        return {
            "attivo": True,
            "totale": tot,
            "non_registrate": non_reg,
            "ultima": dict(zip(("ricevuta_il", "dipendente", "azione", "esito"), ultima)) if ultima else None,
        }
    except Exception as e:
        return {"attivo": False, "motivo": f"Errore lettura: {e}"}


def esporta_csv(dal: str | None = None, al: str | None = None) -> str:
    """CSV (separatore ';' per Excel italiano) delle timbrature ricevute tra dal/al (YYYY-MM-DD)."""
    q = ("SELECT id, ricevuta_il, dipendente, azione, orario_telefono, orario_foglio, esito, tentativi, dettaglio, "
         "lat, lng, precisione_m, client_id, ip, pin_impronta FROM timbrature WHERE 1=1")
    args: list = []
    if dal:
        q += " AND ricevuta_il >= ?"
        args.append(dal)
    if al:
        q += " AND ricevuta_il < ?"
        args.append(al + " 99")  # include tutto il giorno "al"
    q += " ORDER BY id"
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["N.", "Ricevuta il (server)", "Dipendente", "Azione", "Orario telefono",
                "Orario nel foglio", "Esito invio a Google", "Tentativi", "Dettaglio", "Lat", "Lng",
                "Precisione (m)", "ID dispositivo", "IP", "Impronta PIN"])
    if is_enabled():
        with _LOCK:
            conn = _connect()
            for row in conn.execute(q, args):
                w.writerow(["" if v is None else v for v in row])
            conn.close()
    return "\ufeff" + out.getvalue()  # BOM: Excel legge correttamente gli accenti
