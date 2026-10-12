"""
Associazione telefono ↔ dipendente per la app /timbra.

Il MAC del telefono non è leggibile da una pagina web (e i telefoni moderni lo
cambiano a caso), quindi ogni dispositivo ha una "chiave" casuale generata alla
prima apertura dell'app e salvata nel telefono.

Regole (decise dall'amministrazione il 12/10/2026):
  - il primo telefono con cui il dipendente timbra diventa il suo (automatico);
  - un telefono diverso viene BLOCCATO e compare una richiesta in "Approva";
  - un solo telefono per dipendente: approvando il nuovo, il vecchio viene disattivato;
  - il tablet in sede viene attivato da un amministratore come "condiviso" (vale per tutti).

Dati sul Disco persistente di Render (stesso database del registro di sicurezza).
Se il disco non c'è, il controllo è spento (non deve mai impedire le timbrature per un guasto).
"""
from __future__ import annotations

import logging

import timbra_log

logger = logging.getLogger(__name__)

ATTIVO, IN_ATTESA, RIFIUTATO, SOSTITUITO, CONDIVISO = "attivo", "in_attesa", "rifiutato", "sostituito", "condiviso"
_INIT = False


def _conn():
    global _INIT
    conn = timbra_log._connect()
    if not _INIT:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dispositivi (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                dipendente    TEXT NOT NULL,
                device_id     TEXT NOT NULL,
                stato         TEXT NOT NULL,
                descrizione   TEXT,
                creato_il     TEXT,
                ultimo_uso    TEXT,
                deciso_da     TEXT,
                deciso_il     TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS ix_disp_dev ON dispositivi(device_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_disp_dip ON dispositivi(dipendente)")
        conn.commit()
        _INIT = True
    return conn


def _now() -> str:
    return timbra_log._now_local().strftime("%Y-%m-%d %H:%M:%S")


def _descr(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    if "iphone" in ua:
        mod = "iPhone"
    elif "ipad" in ua:
        mod = "iPad"
    elif "android" in ua:
        mod = "Android"
    elif "mac os" in ua:
        mod = "Mac"
    elif "windows" in ua:
        mod = "PC Windows"
    else:
        mod = "Dispositivo"
    if "crios" in ua or ("chrome" in ua and "edg" not in ua):
        br = "Chrome"
    elif "fxios" in ua or "firefox" in ua:
        br = "Firefox"
    elif "edg" in ua:
        br = "Edge"
    elif "samsungbrowser" in ua:
        br = "Samsung Internet"
    elif "safari" in ua:
        br = "Safari"
    else:
        br = "app"
    return f"{mod} · {br}"


def attivo() -> bool:
    return timbra_log.is_enabled()


def controlla(dipendente: str, device_id: str, kiosk: bool, user_agent: str, vincola: bool) -> dict:
    """
    Verifica se il dispositivo può timbrare per il dipendente.
    vincola=True (timbratura): se il dipendente non ha ancora un telefono, questo diventa il suo.
    vincola=False (accesso col PIN): non associa, ma crea la richiesta se il telefono è nuovo.
    Ritorna {"ok": bool, "stato": ..., "messaggio": ...}
    """
    if not attivo() or not dipendente:
        return {"ok": True, "stato": "controllo_spento"}
    device_id = (device_id or "").strip()[:64]
    if not device_id:
        # App vecchia ancora in cache: lasciamo passare (si aggiorna da sola al prossimo avvio)
        return {"ok": True, "stato": "senza_chiave"}
    key = dipendente.strip().lower()
    try:
        with timbra_log._LOCK:
            conn = _conn()
            try:
                cond = conn.execute("SELECT id FROM dispositivi WHERE device_id=? AND stato=?", (device_id, CONDIVISO)).fetchone()
                if cond:
                    conn.execute("UPDATE dispositivi SET ultimo_uso=? WHERE id=?", (_now(), cond[0]))
                    conn.commit()
                    return {"ok": True, "stato": CONDIVISO}
                if kiosk:
                    return {"ok": False, "stato": "tablet_non_attivo",
                            "messaggio": "Questo tablet non è ancora attivato. Un amministratore deve attivarlo dalla scheda Approva."}

                mio = conn.execute("SELECT id, device_id FROM dispositivi WHERE dipendente=? AND stato=?", (key, ATTIVO)).fetchone()
                if mio and mio[1] == device_id:
                    conn.execute("UPDATE dispositivi SET ultimo_uso=? WHERE id=?", (_now(), mio[0]))
                    conn.commit()
                    return {"ok": True, "stato": ATTIVO}
                if not mio:
                    if not vincola:
                        return {"ok": True, "stato": "nuovo"}
                    conn.execute(
                        "INSERT INTO dispositivi (dipendente, device_id, stato, descrizione, creato_il, ultimo_uso, deciso_da, deciso_il) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (key, device_id, ATTIVO, _descr(user_agent), _now(), _now(), "automatico (primo telefono)", _now()),
                    )
                    conn.commit()
                    return {"ok": True, "stato": "associato"}

                # Il dipendente ha già un altro telefono: cerca/crea la richiesta per questo
                req = conn.execute(
                    "SELECT id, stato FROM dispositivi WHERE dipendente=? AND device_id=? ORDER BY id DESC LIMIT 1",
                    (key, device_id),
                ).fetchone()
                if req and req[1] == RIFIUTATO:
                    return {"ok": False, "stato": RIFIUTATO,
                            "messaggio": "Questo telefono non è autorizzato per te. Usa il tuo telefono o chiedi all'amministrazione."}
                if not req or req[1] != IN_ATTESA:
                    conn.execute(
                        "INSERT INTO dispositivi (dipendente, device_id, stato, descrizione, creato_il) VALUES (?,?,?,?,?)",
                        (key, device_id, IN_ATTESA, _descr(user_agent), _now()),
                    )
                    conn.commit()
                return {"ok": False, "stato": IN_ATTESA,
                        "messaggio": "Telefono non autorizzato: chiedi all'amministrazione. La richiesta è già stata inviata."}
            finally:
                conn.close()
    except Exception as e:  # guasto del disco: non bloccare le timbrature
        logger.warning(f"[DISPOSITIVI] Errore controllo: {e}")
        return {"ok": True, "stato": "errore_controllo"}


def e_condiviso(device_id: str) -> bool:
    device_id = (device_id or "").strip()[:64]
    if not device_id or not attivo():
        return False
    with timbra_log._LOCK:
        conn = _conn()
        try:
            return bool(conn.execute("SELECT 1 FROM dispositivi WHERE device_id=? AND stato=?", (device_id, CONDIVISO)).fetchone())
        finally:
            conn.close()


def elenco() -> dict:
    """Per la scheda Approva: richieste in attesa, telefoni attivi, tablet condivisi."""
    if not attivo():
        return {"attivo": False}
    with timbra_log._LOCK:
        conn = _conn()
        try:
            def rows(q, args=()):
                cols = ("id", "dipendente", "stato", "descrizione", "creato_il", "ultimo_uso", "deciso_da", "deciso_il")
                return [dict(zip(cols, r)) for r in conn.execute(
                    "SELECT id, dipendente, stato, descrizione, creato_il, ultimo_uso, deciso_da, deciso_il FROM dispositivi " + q, args)]
            return {
                "attivo": True,
                "in_attesa": rows("WHERE stato=? ORDER BY id", (IN_ATTESA,)),
                "attivi": rows("WHERE stato=? ORDER BY dipendente", (ATTIVO,)),
                "condivisi": rows("WHERE stato=? ORDER BY id", (CONDIVISO,)),
            }
        finally:
            conn.close()


def decidi(azione: str, rid: int, admin: str, device_id: str = "", user_agent: str = "") -> dict:
    """approva / rifiuta una richiesta, dissocia un telefono, attiva questo dispositivo come tablet condiviso."""
    if not attivo():
        return {"ok": False, "error": "Disco non collegato: controllo telefoni spento"}
    with timbra_log._LOCK:
        conn = _conn()
        try:
            if azione == "condividi":
                device_id = (device_id or "").strip()[:64]
                if not device_id:
                    return {"ok": False, "error": "Chiave dispositivo mancante"}
                conn.execute("UPDATE dispositivi SET stato=?, deciso_da=?, deciso_il=? WHERE device_id=? AND stato IN (?,?)",
                             (SOSTITUITO, admin, _now(), device_id, ATTIVO, IN_ATTESA))
                conn.execute(
                    "INSERT INTO dispositivi (dipendente, device_id, stato, descrizione, creato_il, deciso_da, deciso_il) VALUES (?,?,?,?,?,?,?)",
                    ("*tablet condiviso*", device_id, CONDIVISO, _descr(user_agent), _now(), admin, _now()),
                )
                conn.commit()
                return {"ok": True}
            r = conn.execute("SELECT dipendente, stato FROM dispositivi WHERE id=?", (rid,)).fetchone()
            if not r:
                return {"ok": False, "error": "Voce non trovata"}
            if azione == "approva" and r[1] == IN_ATTESA:
                conn.execute("UPDATE dispositivi SET stato=?, deciso_da=?, deciso_il=? WHERE dipendente=? AND stato=?",
                             (SOSTITUITO, admin, _now(), r[0], ATTIVO))
                conn.execute("UPDATE dispositivi SET stato=?, deciso_da=?, deciso_il=? WHERE id=?", (ATTIVO, admin, _now(), rid))
            elif azione == "rifiuta" and r[1] == IN_ATTESA:
                conn.execute("UPDATE dispositivi SET stato=?, deciso_da=?, deciso_il=? WHERE id=?", (RIFIUTATO, admin, _now(), rid))
            elif azione == "dissocia" and r[1] in (ATTIVO, CONDIVISO):
                # Il prossimo telefono con cui timbra diventerà automaticamente il suo
                conn.execute("UPDATE dispositivi SET stato=?, deciso_da=?, deciso_il=? WHERE id=?", (SOSTITUITO, admin, _now(), rid))
            else:
                return {"ok": False, "error": "Operazione non valida (forse già decisa)"}
            conn.commit()
            return {"ok": True}
        finally:
            conn.close()
