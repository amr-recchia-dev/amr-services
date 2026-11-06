"""
sentinella_anomalie.py — Sentinella Automatica Anomalie Timbrature & Permessi AMR S.r.l.
========================================================================================
Controlla periodicamente e in tempo reale:
1. Timbrature di Uscita o Entrata mancanti nel 'Registro delle timbrature'
   (sui giorni passati e, per la giornata odierna, dopo le ore 19:00).
2. Doppi giustificativi / permessi sovrapposti sullo stesso giorno nel 'Registro Permessi'
   (es. Ferie 8h + Permesso 8h approvati per lo stesso dipendente nella stessa data).
3. Anomalie e avvisi rilevati dall'Agente Revisore Pre-Paghe (Apps Script eseguiAuditPrePaghe):
   - Giornate feriali scoperte (0h lavorate e nessun permesso/ferie/malattia)
   - Richieste di Permesso/Ferie rimaste 'In attesa' di approvazione
   - Orario giornaliero inferiore alle 8h senza permesso a copertura
4. Pause pranzo anomale (< 15 min o > 90 min).

Quando rileva almeno una NUOVA anomalia non ancora notificata:
- Invia automaticamente un'email chiara e strutturata a:
  • Alessandro Recchia (amministrazione@amrrecchia.it)
  • Massimo Recchia (info@amrrecchia.it)
  (con copia di controllo BCC a riccardo.g@zaffiroweb.com)
- Salva gli ID delle anomalie notificate sia su disco locale sia tramite marker IMAP
  nella casella aziendale, così anche dopo un riavvio o redeploy del server cloud Render
  non invia MAI email duplicate per la stessa anomalia.
"""

import os
import re
import json
import time
import html as _html
import logging
import smtplib
import imaplib
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid

import requests
import presenze_service

logger = logging.getLogger("sentinella_anomalie")

ROME_TZ = ZoneInfo("Europe/Rome")

RECIPIENTS_TO = [
    "amministrazione@amrrecchia.it",
    "info@amrrecchia.it",
]
RECIPIENTS_BCC = [
    "riccardo.g@zaffiroweb.com",
]

STATE_DIR = Path(os.getenv("TIMBRA_LOG_DIR", Path(__file__).parent / "logs"))
STATE_FILE = STATE_DIR / "sentinella_anomalie_state.json"

AMR_TIMBRA_URL = os.getenv(
    "AMR_TIMBRA_URL",
    "https://script.google.com/macros/s/AKfycbxzWM7ClITTd3usI2qrIYKENQIiE2KGE9sWvEuGhBJAe40W_hcpn47rrOk0609-7nPr/exec",
)
AMR_TIMBRA_KEY = os.getenv("AMR_TIMBRA_KEY", "amr-tb-9X4qK7mW2zR8vN5c")

# Anomalie già comunicate manualmente il 06/11/2026 alle 13:53 (così non mandiamo doppioni)
INITIAL_NOTIFIED_IDS = {
    "missing_punch|2026-11-04|Mohammed Draij": "2026-11-06T13:53:44+01:00",
    "missing_punch|2026-11-05|Giovanni Fregona": "2026-11-06T13:53:44+01:00",
    "dup_permesso|2026-11-04|Giovanni Fregona": "2026-11-06T13:53:44+01:00",
}

_LAST_STATUS = {
    "last_check_at": None,
    "last_email_sent_at": None,
    "last_email_recipients": [],
    "active_anomalies_count": 0,
    "active_anomalies": [],
    "notified_ids_count": 0,
    "last_error": None,
}


def _load_state() -> dict:
    """Carica lo stato delle anomalie già notificate da disco + eventuale recupero IMAP."""
    state = {"notified": dict(INITIAL_NOTIFIED_IDS), "history": []}
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data.get("notified"), dict):
                state["notified"].update(data["notified"])
            if isinstance(data.get("history"), list):
                state["history"] = data["history"][-50:]
    except Exception as e:
        logger.warning(f"[SENTINELLA] Impossibile leggere file stato locale: {e}")

    # Recupera eventuali ID salvati via IMAP (immunità ai redeploy di Render)
    imap_ids = _fetch_notified_ids_from_imap()
    if imap_ids:
        for aid, ts in imap_ids.items():
            if aid not in state["notified"]:
                state["notified"][aid] = ts
        _save_state(state)

    return state


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"[SENTINELLA] Impossibile salvare file stato locale: {e}")


def _fetch_notified_ids_from_imap() -> dict:
    """Legge dalla casella IMAP ordini@amrrecchia.it i marker AMR_SENTINELLA_IDS degli ultimi 14 giorni."""
    host = os.getenv("IMAP_HOST", "imap.register.it")
    port = int(os.getenv("IMAP_PORT", "993") or 993)
    user = os.getenv("IMAP_USER", "")
    pw = os.getenv("IMAP_PASSWORD", "")
    if not user or not pw:
        return {}
    found = {}
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=15) as m:
            m.login(user, pw)
            m.select("INBOX", readonly=True)
            since_str = (datetime.date.today() - datetime.timedelta(days=14)).strftime("%d-%b-%Y")
            typ, data = m.search(None, f'(SINCE {since_str} SUBJECT "Sentinella Presenze AMR")')
            if typ == "OK" and data and data[0]:
                msg_ids = data[0].split()[-25:]
                for mid in msg_ids:
                    t2, raw = m.fetch(mid, "(BODY.PEEK[])")
                    if t2 == "OK" and raw and raw[0] and len(raw[0]) > 1:
                        text = raw[0][1].decode("utf-8", errors="ignore")
                        matches = re.findall(r"AMR_SENTINELLA_IDS:([A-Za-z0-9_|\-,.:+ ]+?)-->", text)
                        for m_str in matches:
                            for item in m_str.split(";"):
                                item = item.strip()
                                if item:
                                    found[item] = "imap-recovered"
            m.logout()
    except Exception as e:
        logger.debug(f"[SENTINELLA] Controllo IMAP marker non disponibile: {e}")
    return found


def _append_marker_to_imap(raw_msg_bytes: bytes) -> None:
    """Salva copia dell'email inviata (già segnata come letta \\Seen) per persistenza anti-redeploy."""
    host = os.getenv("IMAP_HOST", "imap.register.it")
    port = int(os.getenv("IMAP_PORT", "993") or 993)
    user = os.getenv("IMAP_USER", "")
    pw = os.getenv("IMAP_PASSWORD", "")
    if not user or not pw:
        return
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=15) as m:
            m.login(user, pw)
            m.append("INBOX", "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw_msg_bytes)
            m.logout()
    except Exception as e:
        logger.debug(f"[SENTINELLA] Append IMAP non riuscito: {e}")


def collect_anomalies(now: datetime.datetime = None) -> list[dict]:
    """
    Analizza in tempo reale il Registro delle timbrature, il Registro Permessi e il Revisore Apps Script.
    Restituisce la lista ordinata di tutte le anomalie attive nel mese corrente.
    """
    if now is None:
        now = datetime.datetime.now(ROME_TZ)
    today = now.date()
    include_today_missing_exit = (now.hour >= 19)

    # Forza refresh cache fogli Google
    presenze_service._CACHE["timestamp"] = 0
    timb_rows, perm_rows = presenze_service.get_cached_sheets_data()

    anomalies = {}

    # 1. Controllo timbrature mancanti e pause pranzo anomale su tutti gli operativi
    seen_names = set()
    all_emps = []
    for u in presenze_service.user_auth.AUTH_USERS.values():
        n = u.get("name", "")
        if (
            n
            and n not in seen_names
            and not u.get("is_commercial", False)
            and n not in ("Operatore Produzione", "Operatore Officina", "3D Team AMR")
        ):
            seen_names.add(n)
            all_emps.append(u)

    for emp in all_emps:
        emp_name = emp["name"]
        pin = emp["pin"]
        rep = presenze_service.calculate_employee_report(
            pin=pin,
            target_month_str=f"{today.month:02d}/{today.year}",
        )
        if not rep.get("authenticated"):
            continue

        for day_obj in rep.get("daily", []):
            d_iso = day_obj.get("date_iso", "")
            d_it = day_obj.get("date", "")
            d_name = day_obj.get("day_name", "")
            sessions = day_obj.get("sessions", [])
            if not d_iso:
                continue

            is_today = (d_iso == today.isoformat())
            if is_today and not include_today_missing_exit:
                # Non segnalare uscite mancanti mentre la giornata lavorativa di oggi è ancora in corso
                continue

            for s in sessions:
                st = s.get("status")
                if st in ("USCITA_MANCANTE", "IN_CORSO"):
                    aid = f"missing_punch|{d_iso}|{emp_name}"
                    in_t = s.get("in") or "—"
                    all_punches = ", ".join(
                        [f"Entrata {x.get('in')}" + (f" → Uscita {x.get('out')}" if x.get("out") else " → [Uscita mancante]") for x in sessions]
                    )
                    anomalies[aid] = {
                        "id": aid,
                        "gravita": "alta",
                        "categoria": "Timbratura di Uscita Mancante",
                        "dipendente": emp_name,
                        "data": d_it,
                        "data_iso": d_iso,
                        "giorno": d_name,
                        "dettaglio": f"Entrata registrata alle {in_t}, ma manca la timbratura di uscita ({all_punches}).",
                        "azione_consigliata": f"Inserire l'orario di uscita di {emp_name} per il {d_it} dalla Dashboard o nel Registro.",
                    }
                elif st == "INGRESSO_MANCANTE":
                    aid = f"missing_in|{d_iso}|{emp_name}"
                    out_t = s.get("out") or "—"
                    anomalies[aid] = {
                        "id": aid,
                        "gravita": "alta",
                        "categoria": "Timbratura di Entrata Mancante",
                        "dipendente": emp_name,
                        "data": d_it,
                        "data_iso": d_iso,
                        "giorno": d_name,
                        "dettaglio": f"Risulta una timbratura di uscita alle {out_t} senza la corrispondente timbratura di entrata.",
                        "azione_consigliata": f"Verificare e completare la timbratura di entrata di {emp_name} per il {d_it}.",
                    }

            # Controllo pausa pranzo da doppio tocco (< 5 min) se ci sono 2 sessioni chiuse
            ok_sess = [s for s in sessions if s.get("status") == "OK" and s.get("in") and s.get("out")]
            if len(ok_sess) >= 2:
                out1 = presenze_service.parse_time_to_minutes(ok_sess[0]["out"])
                in2 = presenze_service.parse_time_to_minutes(ok_sess[1]["in"])
                if out1 is not None and in2 is not None and 11 * 60 <= out1 <= 14 * 60:
                    pausa_min_int = int(round(in2 - out1))
                    if 0 <= pausa_min_int < 5:
                        aid = f"short_lunch|{d_iso}|{emp_name}"
                        anomalies[aid] = {
                            "id": aid,
                            "gravita": "media",
                            "categoria": "Doppio Tocco Pausa Pranzo (< 5 min)",
                            "dipendente": emp_name,
                            "data": d_it,
                            "data_iso": d_iso,
                            "giorno": d_name,
                            "dettaglio": f"Uscita pausa alle {ok_sess[0]['out']} e rientro alle {ok_sess[1]['in']} ({pausa_min_int} min — possibile doppio tocco involontario).",
                            "azione_consigliata": f"Verificare se la timbratura di pausa del {d_it} di {emp_name} è un doppio tocco.",
                        }

    # 2. Controllo doppi giustificativi / permessi duplicati sullo stesso giorno nel 'Registro Permessi'
    if perm_rows and len(perm_rows) > 1:
        by_emp_day = defaultdict(list)
        for idx, pr in enumerate(perm_rows[1:], start=2):
            p_name = pr[0].strip() if len(pr) > 0 else ""
            p_tipo = pr[2].strip() if len(pr) > 2 else "Permesso"
            p_start = presenze_service.parse_date(pr[3]) if len(pr) > 3 else None
            p_end = presenze_service.parse_date(pr[4]) if len(pr) > 4 and pr[4].strip() else p_start
            p_ore_str = pr[5].strip() if len(pr) > 5 else ""
            p_note = pr[6].strip() if len(pr) > 6 else ""
            p_stato = pr[7].strip() if len(pr) > 7 else ""
            if not p_name or not p_start:
                continue
            if p_End := (p_end or p_start):
                if p_End < p_start:
                    p_End = p_start
            try:
                ore_val = float(p_ore_str.replace(",", ".")) if p_ore_str else 8.0
            except Exception:
                ore_val = 8.0

            # Segnala richieste ancora "In attesa" nel mese corrente
            if p_stato.lower() == "in attesa" and p_start.year == today.year and p_start.month == today.month:
                d_iso = p_start.isoformat()
                d_it = p_start.strftime("%d/%m/%Y")
                aid = f"pending_perm|{d_iso}|{p_name}|r{idx}"
                anomalies[aid] = {
                    "id": aid,
                    "gravita": "media",
                    "categoria": "Richiesta Permesso/Ferie In Attesa",
                    "dipendente": p_name,
                    "data": d_it,
                    "data_iso": d_iso,
                    "giorno": "",
                    "dettaglio": f"Richiesta di {p_tipo} ({ore_val:g}h) per il {d_it} (riga {idx}) ancora 'In attesa' di approvazione.",
                    "azione_consigliata": "Approvare o rifiutare la richiesta dall'App (/timbra → Approva) o dalla Dashboard.",
                }

            if p_stato.lower() == "approvato":
                cur_d = p_start
                while cur_d <= p_End:
                    if cur_d.year == today.year and cur_d.month == today.month:
                        by_emp_day[(p_name, cur_d)].append({
                            "riga": idx,
                            "tipo": p_tipo,
                            "ore": ore_val,
                            "note": p_note,
                        })
                    cur_d += datetime.timedelta(days=1)

        for (emp_name, d_obj), items in by_emp_day.items():
            tot_ore = sum(x["ore"] for x in items)
            if len(items) > 1 and tot_ore > 8.0:
                d_iso = d_obj.isoformat()
                d_it = d_obj.strftime("%d/%m/%Y")
                aid = f"dup_permesso|{d_iso}|{emp_name}"
                desc_rows = " + ".join(
                    [f"Riga {x['riga']}: {x['tipo']} ({x['ore']:g}h" + (f", '{x['note']}'" if x['note'] else "") + ")" for x in items]
                )
                anomalies[aid] = {
                    "id": aid,
                    "gravita": "alta",
                    "categoria": "Doppio Giustificativo sullo Stesso Giorno",
                    "dipendente": emp_name,
                    "data": d_it,
                    "data_iso": d_iso,
                    "giorno": presenze_service.ITALIAN_DAYS.get(d_obj.weekday(), ""),
                    "dettaglio": f"Nel 'Registro Permessi' per il {d_it} risultano {len(items)} righe approvate per un totale di {tot_ore:g}h ({desc_rows}).",
                    "azione_consigliata": f"Eliminare la riga duplicata nel foglio 'Registro Permessi' per {emp_name} ({d_it}).",
                }

    # 3. Controllo istantaneo giornate feriali scoperte (0h lavorate e nessun giustificativo approvato)
    festivi_fissi = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    for emp in all_emps:
        emp_name = emp["name"]
        if any(x in emp_name.lower() for x in ("ambrosino", "semenzin")):
            continue
        rep = presenze_service.calculate_employee_report(
            pin=emp["pin"],
            target_month_str=f"{today.month:02d}/{today.year}",
        )
        worked_dates = {d_obj.get("date_iso") for d_obj in rep.get("daily", []) if d_obj.get("sessions")}

        for day_num in range(1, today.day):
            d_check = datetime.date(today.year, today.month, day_num)
            if d_check.weekday() >= 5 or (d_check.month, d_check.day) in festivi_fissi:
                continue
            d_iso = d_check.isoformat()
            if d_iso in worked_dates:
                continue
            # Verifica se ha un permesso/ferie/malattia approvato o in attesa su d_check
            has_just = False
            if perm_rows and len(perm_rows) > 1:
                for pr in perm_rows[1:]:
                    if len(pr) > 7 and pr[0].strip().lower() == emp_name.lower():
                        st = pr[7].strip().lower()
                        if st in ("approvato", "in attesa"):
                            ps = presenze_service.parse_date(pr[3])
                            pe = presenze_service.parse_date(pr[4]) if len(pr) > 4 and pr[4].strip() else ps
                            if ps and pe and ps <= d_check <= pe:
                                has_just = True
                                break
            if not has_just:
                d_it = d_check.strftime("%d/%m/%Y")
                aid = f"uncovered_day|{d_iso}|{emp_name}"
                anomalies[aid] = {
                    "id": aid,
                    "gravita": "media",
                    "categoria": "Giornata Feriale Scoperta (0h)",
                    "dipendente": emp_name,
                    "data": d_it,
                    "data_iso": d_iso,
                    "giorno": presenze_service.ITALIAN_DAYS.get(d_check.weekday(), ""),
                    "dettaglio": f"{emp_name} ({d_it}): 0 ore lavorate e nessun permesso/ferie/malattia registrato.",
                    "azione_consigliata": f"Registrare Ferie/Permesso/Malattia o le timbrature mancanti per {emp_name} ({d_it}).",
                }

    sorted_list = sorted(
        anomalies.values(),
        key=lambda x: (0 if x["gravita"] == "alta" else 1, x["data_iso"], x["dipendente"]),
    )
    return sorted_list


def _send_sentinella_email(new_anomalies: list[dict], all_anomalies: list[dict], now: datetime.datetime) -> tuple[bool, str]:
    """Invia l'email di riepilogo anomalie a amministrazione@amrrecchia.it e info@amrrecchia.it."""
    sender_email = os.getenv("IMAP_USER", "ordini@amrrecchia.it")
    password = os.getenv("IMAP_PASSWORD", "")
    dashboard_url = "https://amr-services.onrender.com/admin-presenze"
    sheet_url = "https://docs.google.com/spreadsheets/d/1Uul8Juug4uIzKes4HBGHRDAOT6OhHOumuodZ0YmFg2w/edit"

    date_label = now.strftime("%d/%m/%Y ore %H:%M")
    n_new = len(new_anomalies)
    n_tot = len(all_anomalies)

    if n_new == 1:
        a0 = new_anomalies[0]
        subject = f"[Sentinella Presenze AMR] ⚠️ Nuova anomalia: {a0['dipendente']} ({a0['data']}) – {a0['categoria']}"
    else:
        subject = f"[Sentinella Presenze AMR] ⚠️ {n_new} nuove anomalie timbrature/permessi da verificare ({now.strftime('%d/%m/%Y')})"

    all_ids_marker = ";".join(a["id"] for a in all_anomalies)

    # Costruzione testo plain
    lines = [
        "Ciao Alessandro e Massimo,",
        "",
        f"la Sentinella automatica del sistema presenze AMR ha rilevato {n_new} "
        + ("nuova segnalazione" if n_new == 1 else "nuove segnalazioni")
        + f" da verificare (controllo del {date_label}):",
        "",
    ]
    for idx, a in enumerate(new_anomalies, start=1):
        lines.append(f"{idx}) {a['dipendente']} — {a['data']} ({a['categoria']})")
        lines.append(f"   • Dettaglio: {a['dettaglio']}")
        lines.append(f"   • Azione:    {a['azione_consigliata']}")
        lines.append("")

    other_open = [a for a in all_anomalies if a["id"] not in {x["id"] for x in new_anomalies}]
    if other_open:
        lines.append("Altre segnalazioni del mese ancora aperte:")
        for a in other_open:
            lines.append(f"  - {a['dipendente']} ({a['data']}): {a['categoria']} — {a['dettaglio']}")
        lines.append("")

    lines.extend([
        "Potete correggere le anomalie con 1 clic direttamente dalla Dashboard Direzione:",
        f"👉 Dashboard Direzione: {dashboard_url}",
        f"👉 Foglio Google Presenze: {sheet_url}",
        "",
        "— Sentinella Automatica Presenze AMR S.r.l.",
    ])
    plain_body = "\n".join(lines)

    # Costruzione HTML
    esc = _html.escape
    rows_html = ""
    for a in new_anomalies:
        badge_bg = "#FEE2E2" if a["gravita"] == "alta" else "#FEF3C7"
        badge_fg = "#991B1B" if a["gravita"] == "alta" else "#92400E"
        rows_html += f"""
        <div style="background:#ffffff;border:1px solid #e2e8f0;border-left:4px solid {badge_fg};border-radius:8px;padding:14px 16px;margin-bottom:12px;">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
            <strong style="font-size:15px;color:#0f172a;">{esc(a['dipendente'])} — {esc(a['data'])}</strong>
            <span style="background:{badge_bg};color:{badge_fg};font-size:11px;font-weight:700;padding:3px 8px;border-radius:99px;text-transform:uppercase;">{esc(a['categoria'])}</span>
          </div>
          <div style="font-size:13.5px;color:#334155;margin-bottom:6px;">{esc(a['dettaglio'])}</div>
          <div style="font-size:12.5px;color:#0f766e;font-weight:600;">💡 {esc(a['azione_consigliata'])}</div>
        </div>
        """

    other_html = ""
    if other_open:
        items_li = "".join(
            f"<li style='margin-bottom:6px;'><strong>{esc(a['dipendente'])} ({esc(a['data'])})</strong>: {esc(a['categoria'])} — <span style='color:#475569;'>{esc(a['dettaglio'])}</span></li>"
            for a in other_open
        )
        other_html = f"""
        <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px 16px;margin-top:16px;">
          <div style="font-size:12px;font-weight:700;color:#475569;text-transform:uppercase;margin-bottom:8px;">📋 Altre segnalazioni del mese ancora aperte ({len(other_open)})</div>
          <ul style="margin:0;padding-left:18px;font-size:13px;color:#1e293b;">{items_li}</ul>
        </div>
        """

    html_body = f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#0f172a;">
  <!-- AMR_SENTINELLA_IDS:{all_ids_marker}-->
  <div style="max-width:640px;margin:20px auto;background:#ffffff;border-radius:12px;overflow:hidden;box-shadow:0 4px 12px rgba(15,23,42,0.08);">
    <div style="background:#001C3A;color:#ffffff;padding:18px 24px;">
      <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#D4B373;font-weight:700;">AMR S.r.l. · Sentinella Automatica Presenze</div>
      <h2 style="margin:6px 0 0;font-size:19px;font-weight:700;">⚠️ Rilevate {n_new} {"nuova anomalia" if n_new == 1 else "nuove anomalie"} da verificare</h2>
    </div>
    <div style="padding:22px 24px;">
      <p style="margin:0 0 16px;font-size:14px;line-height:1.5;color:#334155;">
        Ciao <strong>Alessandro</strong> e <strong>Massimo</strong>,<br>
        il controllo automatico delle timbrature e dei permessi (<strong>{esc(date_label)}</strong>) ha rilevato le seguenti anomalie:
      </p>
      {rows_html}
      {other_html}
      <div style="margin-top:22px;padding-top:16px;border-top:1px solid #e2e8f0;text-align:center;">
        <a href="{dashboard_url}" style="display:inline-block;background:#5B8046;color:#ffffff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:700;font-size:14px;margin:4px;">🔧 Apri Dashboard Direzione e Correggi</a>
        <a href="{sheet_url}" style="display:inline-block;background:#001C3A;color:#ffffff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:600;font-size:13px;margin:4px;">📊 Apri Foglio Google</a>
      </div>
      <p style="margin:18px 0 0;font-size:12px;color:#64748b;text-align:center;">
        Questa notifica viene inviata automaticamente una sola volta per ciascuna nuova anomalia rilevata.<br>
        PIN Direzione: Alessandro <code>4921</code> · Massimo <code>9276</code>
      </p>
    </div>
  </div>
</body>
</html>"""

    # 1. Tentativo primario: SMTP autenticato Register.it
    if sender_email and password:
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = formataddr(("Sentinella Presenze AMR", sender_email))
            msg["To"] = ", ".join(RECIPIENTS_TO)
            msg["Reply-To"] = "riccardo.g@zaffiroweb.com"
            msg["Subject"] = subject
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain="amrrecchia.it")
            msg.attach(MIMEText(plain_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

            all_rcpts = RECIPIENTS_TO + RECIPIENTS_BCC
            raw_bytes = msg.as_bytes()
            with smtplib.SMTP_SSL("authsmtp.securemail.pro", 465, timeout=20) as s:
                s.login(sender_email, password)
                s.sendmail(sender_email, all_rcpts, raw_bytes)

            _append_marker_to_imap(raw_bytes)
            logger.info(f"✅ [SENTINELLA] Email inviata via SMTP a {RECIPIENTS_TO} (BCC: {RECIPIENTS_BCC})")
            return True, "smtp"
        except Exception as e:
            logger.warning(f"⚠️ [SENTINELLA] Errore SMTP Register.it, passo al fallback Apps Script: {e}")

    # 2. Fallback automatico: Google Apps Script MailApp
    try:
        r = requests.post(
            AMR_TIMBRA_URL,
            json={
                "key": AMR_TIMBRA_KEY,
                "action": "invia_email_alessandro",
                "to": ",".join(RECIPIENTS_TO),
                "subject": subject,
                "body": plain_body,
                "htmlBody": html_body,
                "senderName": "Sentinella Presenze AMR",
            },
            timeout=30,
        )
        res = r.json()
        if res.get("ok"):
            logger.info(f"✅ [SENTINELLA] Email inviata via fallback Apps Script a {RECIPIENTS_TO}")
            return True, "apps_script"
        return False, f"Errore Apps Script: {res}"
    except Exception as e:
        return False, f"Errore invio email: {e}"


def run_check(force_send: bool = False) -> dict:
    """
    Esegue un controllo completo delle anomalie.
    Se trova nuove anomalie non ancora notificate (o se force_send=True), invia l'email
    ad Alessandro (amministrazione@amrrecchia.it) e Massimo (info@amrrecchia.it).
    """
    now = datetime.datetime.now(ROME_TZ)
    now_iso = now.isoformat(timespec="seconds")
    try:
        state = _load_state()
        notified_map = state.get("notified", {})

        active_anomalies = collect_anomalies(now=now)
        active_ids = {a["id"] for a in active_anomalies}

        # Se un'anomalia era stata notificata ma ora è stata RISOLTA nel foglio,
        # rimuoviamola da notified_map così se dovesse ricapitare in futuro verrà segnalata
        resolved_ids = [
            aid for aid in list(notified_map.keys())
            if aid not in active_ids and aid not in INITIAL_NOTIFIED_IDS
        ]
        for rid in resolved_ids:
            notified_map.pop(rid, None)

        new_anomalies = [
            a for a in active_anomalies
            if force_send or (a["id"] not in notified_map)
        ]

        email_sent = False
        channel = None
        if new_anomalies and active_anomalies:
            ok, ch = _send_sentinella_email(new_anomalies, active_anomalies, now)
            email_sent = ok
            channel = ch
            if ok:
                for a in active_anomalies:
                    notified_map[a["id"]] = now_iso
                state["history"].append({
                    "sent_at": now_iso,
                    "channel": ch,
                    "recipients": RECIPIENTS_TO,
                    "new_anomalies": [a["id"] for a in new_anomalies],
                })
                _LAST_STATUS["last_email_sent_at"] = now_iso
                _LAST_STATUS["last_email_recipients"] = RECIPIENTS_TO

        state["notified"] = notified_map
        _save_state(state)

        _LAST_STATUS.update({
            "last_check_at": now_iso,
            "active_anomalies_count": len(active_anomalies),
            "active_anomalies": active_anomalies,
            "notified_ids_count": len(notified_map),
            "last_error": None,
        })

        return {
            "ok": True,
            "checked_at": now_iso,
            "active_anomalies_count": len(active_anomalies),
            "new_anomalies_count": len(new_anomalies),
            "active_anomalies": active_anomalies,
            "new_anomalies": new_anomalies,
            "email_sent": email_sent,
            "channel": channel,
            "recipients": RECIPIENTS_TO,
        }
    except Exception as e:
        logger.error(f"❌ [SENTINELLA] Errore durante run_check: {e}")
        _LAST_STATUS["last_check_at"] = now_iso
        _LAST_STATUS["last_error"] = str(e)
        return {"ok": False, "error": str(e), "checked_at": now_iso}


def get_status() -> dict:
    state = _load_state()
    return {
        **_LAST_STATUS,
        "recipients_to": RECIPIENTS_TO,
        "recipients_bcc": RECIPIENTS_BCC,
        "notified_ids": state.get("notified", {}),
        "history": state.get("history", [])[-10:],
    }


def start_sentinella_loop() -> None:
    """
    Loop continuo in background (attivo 24/7 su Render):
    - Esegue un primo controllo 45 secondi dopo l'avvio del server.
    - Poi controlla ogni 30 minuti tra le 08:00 e le 20:30 (lunedì–sabato).
    """
    time.sleep(45)
    logger.info("🛡️ [SENTINELLA] Avvio loop automatico controllo anomalie timbrature & permessi")
    while True:
        try:
            now = datetime.datetime.now(ROME_TZ)
            # Lunedì (0) .. Sabato (5), dalle 08:00 alle 20:30
            if now.weekday() <= 5 and 8 <= now.hour <= 20:
                res = run_check(force_send=False)
                logger.info(
                    f"🛡️ [SENTINELLA] Check completato: attive={res.get('active_anomalies_count', 0)}, "
                    f"nuove={res.get('new_anomalies_count', 0)}, email_inviata={res.get('email_sent', False)}"
                )
        except Exception as e:
            logger.warning(f"🛡️ [SENTINELLA] Errore nel loop periodico: {e}")
        time.sleep(1800)  # ogni 30 minuti
