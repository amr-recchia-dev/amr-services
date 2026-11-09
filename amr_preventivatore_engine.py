#!/usr/bin/env python3
"""
AMR OS — Motore Preventivatore Veloce 3D, Parser Tempi Macchina (KUKA KRL & OSAI G-Code) & Connettore Monday.com
=================================================================================================================
- Calcola tempi ciclo reali dai programmi generati da Roboticom ARPP v3.27.59 (.src) per i 2 KUKA KR16/KR16F (KRC2)
- Calcola tempi ciclo reali dai programmi G-Code ISO (.txt) per il Pantografo CNC OSAI (WinMedia 192.168.0.2)
- Ricerca storica preventivi per similitudine (dimensioni, volume m³, finitura, cliente) su 81+ preventivi Eureka
- Integrazione bidirezionale sicura con Monday.com (esclusivamente su NEW COMMERCIALE 2133436509 e GP NEW 2136092569)
- Autenticazione ad alta sicurezza (PBKDF2-HMAC-SHA256 + Sessioni firmate HMAC + Rate Limiting brute-force)
  riservata esclusivamente a:
  1) riccardo.g@zaffiroweb.com
  2) ordini@amrrecchia.it
  3) info@amrrecchia.it
"""

from __future__ import annotations
import os
import re
import json
import math
import time
import hmac
import hashlib
import secrets
from datetime import datetime
from typing import Dict, Any, List, Optional
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORICO_JSON_PATH = os.path.join(BASE_DIR, "amr_storico_preventivi.json")

# Board Monday.com autorizzate
BOARD_COMMERCIALE_NEW = "2133436509"
BOARD_PROGETTI_NEW = "2136092569"
FORBIDDEN_BOARDS = {"1865049112", "1865197409", "1865050352", "1988908927", "1865824381"}

# ---------------------------------------------------------------------------
# 1. AUTENTICAZIONE AD ALTA SICUREZZA (SOLO 3 UTENTI AUTORIZZATI)
# ---------------------------------------------------------------------------
ALLOWED_USERS: Dict[str, Dict[str, str]] = {
    "riccardo.g@zaffiroweb.com": {
        "name": "Riccardo Gazzola (Zaffiroweb)",
        "role": "Super Admin",
        "salt": "9f4d8a2e1b7c6d5f",
        # Default password: AMR-Riccardo-2026!
        "pwd_hash": hashlib.pbkdf2_hmac("sha256", b"AMR-Riccardo-2026!", b"9f4d8a2e1b7c6d5f", 310000).hex(),
    },
    "ordini@amrrecchia.it": {
        "name": "Gary Innocente (AMR Ordini & Produzione)",
        "role": "Responsabile Produzione",
        "salt": "3c8e7a1f5b2d9e4a",
        # Default password: AMR-Ordini-2026!
        "pwd_hash": hashlib.pbkdf2_hmac("sha256", b"AMR-Ordini-2026!", b"3c8e7a1f5b2d9e4a", 310000).hex(),
    },
    "info@amrrecchia.it": {
        "name": "Massimo Recchia (AMR Direzione)",
        "role": "Direzione Commerciale",
        "salt": "7b1d4f9a3e6c8b2d",
        # Default password: AMR-Massimo-2026!
        "pwd_hash": hashlib.pbkdf2_hmac("sha256", b"AMR-Massimo-2026!", b"7b1d4f9a3e6c8b2d", 310000).hex(),
    },
}

_SESSION_SECRET = os.getenv("AMR_PREVENTIVATORE_SECRET", "amr_os_vault_kuka_osai_2026_99f8a7b6c5d4e3f2").encode("utf-8")
_LOGIN_ATTEMPTS: Dict[str, List[float]] = {}
_REVOKED_TOKENS: set[str] = set()


def check_rate_limit_login(key: str, max_attempts: int = 5, window_sec: int = 900) -> tuple[bool, int]:
    now = time.time()
    attempts = [t for t in _LOGIN_ATTEMPTS.get(key, []) if now - t < window_sec]
    _LOGIN_ATTEMPTS[key] = attempts
    if len(attempts) >= max_attempts:
        retry_after = int(window_sec - (now - attempts[0]))
        return False, max(retry_after, 1)
    return True, 0


def record_failed_login(key: str) -> None:
    now = time.time()
    _LOGIN_ATTEMPTS.setdefault(key, []).append(now)


def clear_failed_login(key: str) -> None:
    _LOGIN_ATTEMPTS.pop(key, None)


def verify_user_credentials(email_addr: str, password: str) -> Optional[Dict[str, str]]:
    email_clean = (email_addr or "").strip().lower()
    if email_clean not in ALLOWED_USERS:
        return None
    u = ALLOWED_USERS[email_clean]
    computed = hashlib.pbkdf2_hmac(
        "sha256",
        (password or "").encode("utf-8"),
        u["salt"].encode("utf-8"),
        310000,
    ).hex()
    if hmac.compare_digest(computed, u["pwd_hash"]):
        return {"email": email_clean, "name": u["name"], "role": u["role"]}
    return None


def create_session_token(email_addr: str, ttl_sec: int = 43200) -> str:
    email_clean = email_addr.strip().lower()
    u = ALLOWED_USERS[email_clean]
    exp = int(time.time()) + ttl_sec
    nonce = secrets.token_hex(8)
    payload = json.dumps({"email": email_clean, "name": u["name"], "role": u["role"], "exp": exp, "nonce": nonce})
    import base64
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")
    sig = hmac.new(_SESSION_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_session_token(token: str) -> Optional[Dict[str, Any]]:
    if not token or "." not in token or token in _REVOKED_TOKENS:
        return None
    try:
        import base64
        b64, sig = token.split(".", 1)
        expected_sig = hmac.new(_SESSION_SECRET, b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
        if data.get("exp", 0) < time.time():
            return None
        if data.get("email") not in ALLOWED_USERS:
            return None
        return data
    except Exception:
        return None


def revoke_session_token(token: str) -> None:
    if token:
        _REVOKED_TOKENS.add(token)


# ---------------------------------------------------------------------------
# 2. PARSER TEMPI MACCHINA: KUKA KRL (.SRC da Roboticom ARPP 3.27.59) & OSAI (.TXT)
# ---------------------------------------------------------------------------
def parse_kuka_src(src_content: str, override_pct: float = 100.0) -> Dict[str, Any]:
    vel_cp = 0.05
    ptp_time_est_s = 1.5
    last_pos = None
    total_dist_mm = 0.0
    total_time_s = 0.0
    lin_count = 0
    ptp_count = 0
    vel_samples = []
    min_bounds = {"X": 1e9, "Y": 1e9, "Z": 1e9}
    max_bounds = {"X": -1e9, "Y": -1e9, "Z": -1e9}

    re_vel = re.compile(r"\$VEL\.CP\s*=\s*([0-9.]+)", re.IGNORECASE)
    re_motion = re.compile(r"^\s*(LIN|PTP)\s+\{E6POS:\s*([^}]+)\}", re.IGNORECASE)
    re_coord = re.compile(r"([XYZABCE1]+)\s+([-+]?[0-9]*\.?[0-9]+)", re.IGNORECASE)

    for line in src_content.splitlines():
        m_vel = re_vel.search(line)
        if m_vel:
            try:
                v = float(m_vel.group(1))
                if v > 0:
                    vel_cp = v
                    vel_samples.append(v)
            except ValueError:
                pass
            continue

        m_mot = re_motion.search(line)
        if m_mot:
            mtype = m_mot.group(1).upper()
            coords_str = m_mot.group(2)
            coords = {k.upper(): float(v) for k, v in re_coord.findall(coords_str)}
            if "X" in coords and "Y" in coords and "Z" in coords:
                cur = (coords["X"], coords["Y"], coords["Z"], coords.get("E1", 0.0))
                for idx, axis in enumerate(("X", "Y", "Z")):
                    min_bounds[axis] = min(min_bounds[axis], cur[idx])
                    max_bounds[axis] = max(max_bounds[axis], cur[idx])
                if last_pos is not None:
                    dx = cur[0] - last_pos[0]
                    dy = cur[1] - last_pos[1]
                    dz = cur[2] - last_pos[2]
                    dist_mm = math.sqrt(dx * dx + dy * dy + dz * dz)
                    total_dist_mm += dist_mm
                    if mtype == "LIN":
                        lin_count += 1
                        speed_mm_s = max(vel_cp * 1000.0, 1.0)
                        seg_time = (dist_mm / speed_mm_s) * 1.12
                        total_time_s += seg_time
                    else:
                        ptp_count += 1
                        total_time_s += ptp_time_est_s
                else:
                    if mtype == "LIN":
                        lin_count += 1
                    else:
                        ptp_count += 1
                last_pos = cur

    factor = max(override_pct, 1.0) / 100.0
    adjusted_time_s = total_time_s / factor
    bbox = {
        axis: round(max_bounds[axis] - min_bounds[axis], 1) if max_bounds[axis] > min_bounds[axis] else 0.0
        for axis in ("X", "Y", "Z")
    }
    avg_vel = sum(vel_samples) / len(vel_samples) if vel_samples else vel_cp

    return {
        "machine": "KUKA KRC2 (KR16 / KR16F) - Roboticom ARPP 3.27.59",
        "lin_segments": lin_count,
        "ptp_segments": ptp_count,
        "total_distance_m": round(total_dist_mm / 1000.0, 2),
        "avg_vel_cp_m_s": round(avg_vel, 4),
        "override_pct": override_pct,
        "estimated_time_seconds": round(adjusted_time_s, 1),
        "estimated_time_minutes": round(adjusted_time_s / 60.0, 1),
        "estimated_time_hours": round(adjusted_time_s / 3600.0, 2),
        "work_envelope_mm": bbox,
    }


def parse_osai_gcode(gcode_content: str, override_pct: float = 90.0) -> Dict[str, Any]:
    feed_mm_min = 2500.0
    rapid_mm_min = 10000.0
    cur_mode = "G0"
    last_pos = [0.0, 0.0, 0.0]
    has_pos = False
    total_cut_mm = 0.0
    total_rapid_mm = 0.0
    total_time_s = 0.0
    moves = 0

    re_word = re.compile(r"([GXYZF])\s*([-+]?[0-9]*\.?[0-9]+)", re.IGNORECASE)
    for raw_line in gcode_content.splitlines():
        line = raw_line.split(";")[0].split("(")[0].strip()
        if not line:
            continue
        words = re_word.findall(line)
        if not words:
            continue
        new_pos = list(last_pos)
        moved = False
        for letter, val_s in words:
            L = letter.upper()
            v = float(val_s)
            if L == "G":
                gi = int(v)
                if gi in (0, 1, 2, 3):
                    cur_mode = f"G{gi}"
            elif L == "F" and v > 0:
                feed_mm_min = v
            elif L == "X":
                new_pos[0] = v
                moved = True
            elif L == "Y":
                new_pos[1] = v
                moved = True
            elif L == "Z":
                new_pos[2] = v
                moved = True
        if moved:
            moves += 1
            if has_pos:
                dist = math.sqrt(sum((new_pos[i] - last_pos[i]) ** 2 for i in range(3)))
                if cur_mode == "G0":
                    total_rapid_mm += dist
                    total_time_s += (dist / (rapid_mm_min / 60.0)) * 1.15
                else:
                    total_cut_mm += dist
                    total_time_s += (dist / (max(feed_mm_min, 10.0) / 60.0)) * 1.10
            last_pos = new_pos
            has_pos = True

    factor = max(override_pct, 1.0) / 100.0
    adj_s = total_time_s / factor
    return {
        "machine": "Pantografo CNC OSAI (WinMedia 192.168.0.2)",
        "total_moves": moves,
        "cut_distance_m": round(total_cut_mm / 1000.0, 2),
        "rapid_distance_m": round(total_rapid_mm / 1000.0, 2),
        "override_pct": override_pct,
        "estimated_time_seconds": round(adj_s, 1),
        "estimated_time_minutes": round(adj_s / 60.0, 1),
        "estimated_time_hours": round(adj_s / 3600.0, 2),
    }


# ---------------------------------------------------------------------------
# 3. RICERCA STORICA PREVENTIVI EUREKA
# ---------------------------------------------------------------------------
def load_historical_quotes() -> List[Dict[str, Any]]:
    if not os.path.exists(STORICO_JSON_PATH):
        return []
    try:
        with open(STORICO_JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "quotes" in data:
            return data["quotes"]
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def search_similar_historical_quotes(query: str = "", limit: int = 50) -> List[Dict[str, Any]]:
    quotes = load_historical_quotes()
    if not query:
        return quotes[:limit]
    terms = [t.lower() for t in query.split() if len(t) >= 2]
    scored = []
    for q in quotes:
        blob = json.dumps(q, ensure_ascii=False).lower()
        score = sum(3 if t in (q.get("cliente", "") + " " + q.get("oggetto", "")).lower() else (1 if t in blob else 0) for t in terms)
        if score > 0:
            scored.append((score, q))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in scored[:limit]]


# ---------------------------------------------------------------------------
# 4. INTEGRAZIONE LIVE MONDAY.COM (SOLO NEW COMMERCIALE 2133436509)
# ---------------------------------------------------------------------------
def _get_monday_token() -> str:
    tok = os.getenv("MONDAY_API_TOKEN", "").strip()
    if not tok:
        env_path = os.path.join(BASE_DIR, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MONDAY_API_TOKEN="):
                        tok = line.split("=", 1)[1].strip().strip('"').strip("'")
    return tok


def _monday_graphql(query: str, variables: dict = None) -> dict:
    for fb in FORBIDDEN_BOARDS:
        if fb in query or (variables and any(fb in str(v) for v in variables.values())):
            raise ValueError(f"CRITICAL SAFETY: Accesso bloccato a board storica proibita {fb}")
    tok = _get_monday_token()
    if not tok:
        raise RuntimeError("MONDAY_API_TOKEN non configurato.")
    body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.monday.com/v2",
        data=body,
        headers={
            "Authorization": tok,
            "Content-Type": "application/json",
            "API-Version": "2024-10",
            "User-Agent": "AMR-OS-Preventivatore/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_monday_commercial_items(limit: int = 60) -> List[Dict[str, Any]]:
    """Legge le richieste/preventivi attivi dalla board NEW COMMERCIALE (2133436509)."""
    q = f"""
    query {{
      boards(ids: ["{BOARD_COMMERCIALE_NEW}"]) {{
        items_page(limit: {int(limit)}) {{
          items {{
            id
            name
            state
            created_at
            column_values {{
              id
              text
            }}
          }}
        }}
      }}
    }}
    """
    res = _monday_graphql(q)
    items = res.get("data", {}).get("boards", [{}])[0].get("items_page", {}).get("items", [])
    out = []
    for it in items:
        if it.get("state") != "active":
            continue
        cols = {c["id"]: (c.get("text") or "").strip() for c in it.get("column_values", [])}
        out.append({
            "id": it["id"],
            "cliente": it["name"],
            "progetto": cols.get("testo_mkn1sqb4", ""),
            "commessa": cols.get("text_mm51yvbk", f"COMM-{it['id']}"),
            "referente": cols.get("dup__of_nome_referente_mkn3gf63", ""),
            "email": cols.get("email_mkn1v37b", ""),
            "telefono": cols.get("telefono_mkn3gd8v", ""),
            "specifiche": cols.get("testo_lungo_mkn1ydyz") or cols.get("text_mkypgtb8", ""),
            "budget": cols.get("numeri_mkn1w8z2", ""),
            "preventivo_accettato": cols.get("label_mkn37zp7", ""),
            "data_preventivo": cols.get("date_mknkb99e", ""),
            "data_consegna": cols.get("date4", ""),
            "fresa": cols.get("color_mknsezab", ""),
            "taglio": cols.get("color_mkns43r0", ""),
            "finitura": cols.get("color_mknsghqz", ""),
            "disegno_tecnico": cols.get("color_mkrkwak4", ""),
        })
    return out


def publish_quote_to_monday(payload: Dict[str, Any], author_email: str = "") -> Dict[str, Any]:
    """
    Crea o aggiorna una riga preventivo in NEW COMMERCIALE (2133436509)
    e aggiunge un Update con il dettaglio completo del preventivo e dell'analisi costi/tempi.
    """
    item_id = str(payload.get("monday_item_id") or "").strip()
    cliente = (payload.get("cliente") or "Nuovo Cliente").strip()
    oggetto = (payload.get("oggetto") or "Preventivo AMR OS").strip()
    referente = (payload.get("referente") or "").strip()
    email_cl = (payload.get("email") or "").strip()
    totale_imponibile = float(payload.get("totale_imponibile") or 0.0)
    fresa_flag = bool(payload.get("fresa", True))
    taglio_flag = bool(payload.get("taglio", True))
    finitura_flag = bool(payload.get("finitura", False))
    disegno_flag = bool(payload.get("disegno_tecnico", True))
    specifiche = (payload.get("specifiche") or "").strip()
    righe = payload.get("righe") or []
    analisi = payload.get("analisi") or {}

    created_new = False
    if not item_id:
        q_create = """
        mutation CreateCommItem($board_id: ID!, $item_name: String!) {
          create_item(board_id: $board_id, item_name: $item_name) {
            id
          }
        }
        """
        r_cr = _monday_graphql(q_create, {"board_id": BOARD_COMMERCIALE_NEW, "item_name": cliente})
        item_id = str(r_cr.get("data", {}).get("create_item", {}).get("id", ""))
        if not item_id:
            raise RuntimeError(f"Errore creazione item su Monday: {r_cr}")
        created_new = True

    today_str = datetime.now().strftime("%Y-%m-%d")
    col_vals: Dict[str, Any] = {
        "testo_mkn1sqb4": oggetto,
        "date_mknkb99e": {"date": today_str},
        "numeri_mkn1w8z2": str(round(totale_imponibile, 2)),
    }
    if referente:
        col_vals["dup__of_nome_referente_mkn3gf63"] = referente
    if email_cl:
        col_vals["email_mkn1v37b"] = {"email": email_cl, "text": email_cl}
    if specifiche:
        col_vals["testo_lungo_mkn1ydyz"] = {"text": specifiche}
    if fresa_flag:
        col_vals["color_mknsezab"] = {"label": "SI"}
    if taglio_flag:
        col_vals["color_mkns43r0"] = {"label": "SI"}
    if finitura_flag:
        col_vals["color_mknsghqz"] = {"label": "SI"}
    if disegno_flag:
        col_vals["color_mkrkwak4"] = {"label": "SI"}

    q_upd = f"""
    mutation UpdCols($val: JSON!) {{
      change_multiple_column_values(board_id: {BOARD_COMMERCIALE_NEW}, item_id: {item_id}, column_values: $val) {{
        id
      }}
    }}
    """
    _monday_graphql(q_upd, {"val": json.dumps(col_vals)})

    # Costruisci Update HTML leggibile su Monday.com
    rows_html = ""
    for idx, r in enumerate(righe, 1):
        desc = r.get("desc") or r.get("descrizione") or ""
        qty = r.get("qty") or r.get("quantita") or 1
        pu = float(r.get("pu") or r.get("prezzo_unitario") or 0)
        tot = qty * pu
        rows_html += f"<li><b>Riga {idx} ({qty} NR):</b> {desc} — <b>€ {pu:,.2f}</b> (Tot: <b>€ {tot:,.2f}</b>)</li>"

    iva = totale_imponibile * 0.22
    lordo = totale_imponibile + iva
    update_body = (
        f"<h3>📐 Preventivo Generato da AMR OS Preventivatore 3D</h3>"
        f"<p><b>Cliente:</b> {cliente} | <b>Oggetto:</b> {oggetto}<br>"
        f"<b>Operatore AMR OS:</b> {author_email or 'Sistema AMR'}</p>"
        f"<ul>{rows_html}</ul>"
        f"<p>💰 <b>Totale Imponibile: € {totale_imponibile:,.2f}</b> + IVA 22% (€ {iva:,.2f}) = <b>€ {lordo:,.2f}</b></p>"
    )
    if analisi:
        update_body += (
            f"<hr><p><b>📊 Analisi Tecnica & Costi Interni:</b><br>"
            f"• Dimensioni / Volume: {analisi.get('dimensioni', '-')} ({analisi.get('volume_m3', '-')} m³ | Sup: {analisi.get('superficie_m2', '-')} m²)<br>"
            f"• Sezionamento KUKA: {analisi.get('blocchi_kuka', '-')} blocchi | Tempo Macchina stimato: {analisi.get('tempo_macchina', '-')}<br>"
            f"• Costo Vivo Industriale: <b>{analisi.get('costo_vivo', '-')}</b> | Margine stimato: <b>{analisi.get('margine', '-')}</b></p>"
        )

    q_note = """
    mutation AddQuoteUpdate($item_id: ID!, $body: String!) {
      create_update(item_id: $item_id, body: $body) {
        id
      }
    }
    """
    r_up = _monday_graphql(q_note, {"item_id": item_id, "body": update_body})
    update_id = r_up.get("data", {}).get("create_update", {}).get("id", "")

    return {
        "ok": True,
        "board_id": BOARD_COMMERCIALE_NEW,
        "item_id": item_id,
        "created_new": created_new,
        "update_id": update_id,
        "monday_url": f"https://amrrecchia.monday.com/boards/{BOARD_COMMERCIALE_NEW}/pulses/{item_id}",
    }
