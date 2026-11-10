"""
assistente_timbra.py - Assistente Live 24/7 per i Dipendenti AMR (Testo + Vocale + 1-Click)
===========================================================================================
Consente agli operai (autenticati con PIN nell'app /timbra) di:
1. Confermare con 1 tocco al mattino un'uscita o pausa pranzo dimenticata il giorno prima.
2. Scrivere o inviare un messaggio vocale (es. "Ieri ho dimenticato di timbrare l'uscita alle 17",
   "Stamattina sono arrivato alle 7 ma non entrava nell'app", "Venerdì pausa dalle 12 alle 13").

Regole di Sicurezza (Guardrail):
- Ogni dipendente opera ESCLUSIVAMENTE sulle proprie timbrature.
- AUTO-APPROVAZIONE IMMEDIATA se:
  * La data è negli ultimi 3 giorni (fino a 4 giorni il lunedì per coprire il venerdì);
  * L'orario richiesto rientra nella fascia standard 06:00 – 17:30.
- AVVISO AD ADMIN (senza modifica automatica) se:
  * L'orario richiesto è fuori dalla fascia 06:00 – 17:30 (straordinari importanti);
  * La data è più vecchia di 3-4 giorni.
- NOTIFICA EMAIL AUTOMATICA a amministrazione@amrrecchia.it e info@amrrecchia.it
  ad ogni correzione applicata o richiesta fuori standard.
"""

from __future__ import annotations
import os
import re
import json
import logging
import smtplib
import threading
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from zoneinfo import ZoneInfo
import requests

logger = logging.getLogger("assistente_timbra")
ROME_TZ = ZoneInfo("Europe/Rome")

GIORNI_SETTIMANA_IT = [
    "Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"
]


def _parse_hhmm_to_minutes(hhmm: str) -> int | None:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(hhmm or ""))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return h * 60 + mi


def _parse_ddmmyyyy(d_str: str):
    m = re.match(r"^\s*(\d{2})/(\d{2})/(\d{4})\s*$", str(d_str or ""))
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=ROME_TZ).date()
    except ValueError:
        return None


def _is_within_guardrails(data_str: str, orario_str: str, now_dt: datetime) -> tuple[bool, str]:
    """
    Verifica se la correzione richiesta rientra nei paletti di auto-approvazione:
    1) Data non futura e <= 3 giorni fa (<= 4 giorni se oggi è Lunedì o Martedì per coprire Venerdì).
    2) Orario compreso tra le 06:00 e le 17:30.
    """
    target_date = _parse_ddmmyyyy(data_str)
    if not target_date:
        return False, f"Data non valida ({data_str})"

    today_date = now_dt.date()
    diff_days = (today_date - target_date).days
    if diff_days < 0:
        return False, "Non è possibile inserire timbrature per giorni futuri"

    # Il lunedì (weekday=0) o martedì (weekday=1) permettiamo fino a 4 giorni indietro (per coprire il venerdì)
    max_days = 4 if today_date.weekday() in (0, 1) else 3
    if diff_days > max_days:
        return False, f"La data richiesta ({data_str}) è più vecchia di {max_days} giorni"

    mins = _parse_hhmm_to_minutes(orario_str)
    if mins is None:
        return False, f"Orario non valido ({orario_str})"

    # Fascia standard auto-approvata: 06:00 (360 min) -> 17:30 (1050 min)
    if mins < 6 * 60 or mins > 17 * 60 + 30:
        return False, f"L'orario {orario_str} è fuori dalla fascia standard (06:00 – 17:30)"

    return True, "OK"


def _send_assistente_email(
    dipendente: str,
    messaggio_utente: str,
    azioni_eseguite: list[str],
    azioni_in_attesa: list[str],
) -> None:
    """Invia una breve nota email a amministrazione@amrrecchia.it e info@amrrecchia.it."""
    try:
        import html as _html
        sender_email = os.getenv("SENTINELLA_SENDER", "amministrazione@amrrecchia.it")
        password = (
            os.getenv("AMR_AMMINISTRAZIONE_PASSWORD")
            or os.getenv("EMAIL_PASSWORD")
            or "Amr_2025!"
        )
        recipients = ["amministrazione@amrrecchia.it", "info@amrrecchia.it"]
        bcc = ["riccardo.g@zaffiroweb.com"]

        esc = lambda s: _html.escape(str(s or ""))
        now_str = datetime.now(ROME_TZ).strftime("%d/%m/%Y %H:%M")

        if azioni_eseguite and not azioni_in_attesa:
            subject = f"🤖 Assistente Presenze: orario sistemato per {dipendente}"
            badge_bg = "#16a34a"
            badge_txt = "SISTEMATO IN AUTOMATICO"
        elif azioni_in_attesa and not azioni_eseguite:
            subject = f"⚠️ Richiesta Orario da Approvare: {dipendente}"
            badge_bg = "#d97706"
            badge_txt = "RICHIEDE CONFERMA ADMIN"
        else:
            subject = f"🤖 Assistente Presenze: aggiornamento {dipendente}"
            badge_bg = "#2563eb"
            badge_txt = "AGGIORNAMENTO ASSISTENTE"

        items_ok_html = "".join(
            f'<li style="margin:4px 0;color:#15803d;font-weight:600">✅ {esc(a)}</li>'
            for a in azioni_eseguite
        )
        items_wait_html = "".join(
            f'<li style="margin:4px 0;color:#b45309;font-weight:600">⏳ {esc(a)}</li>'
            for a in azioni_in_attesa
        )

        dash_url = "https://amr-services.onrender.com/admin-presenze"
        sheet_url = "https://docs.google.com/spreadsheets/d/1Uul8Juug4uIzKes4HBGHRDAOT6OhHOumuodZ0YmFg2w/edit"

        html_body = f"""
        <div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:560px;color:#0f172a;line-height:1.5">
          <div style="display:inline-block;background:{badge_bg};color:#fff;font-size:11px;font-weight:800;padding:4px 10px;border-radius:99px;letter-spacing:0.5px;margin-bottom:10px">
            {badge_txt}
          </div>
          <h3 style="margin:0 0 10px;color:#001C3A">Assistente Presenze AMR — {esc(dipendente)}</h3>
          <table style="border-collapse:collapse;width:100%;margin:10px 0;background:#f8fafc;border-radius:10px;padding:12px">
            <tr>
              <td style="padding:6px 12px;color:#64748b;width:130px">Dipendente</td>
              <td style="padding:6px 12px;font-weight:700">{esc(dipendente)}</td>
            </tr>
            <tr>
              <td style="padding:6px 12px;color:#64748b">Data/Ora richiesta</td>
              <td style="padding:6px 12px">{esc(now_str)}</td>
            </tr>
            <tr>
              <td style="padding:6px 12px;color:#64748b">Messaggio ricevuto</td>
              <td style="padding:6px 12px;font-style:italic">"{esc(messaggio_utente)}"</td>
            </tr>
          </table>
          {'<p style="margin:12px 0 4px;font-weight:700">Modifiche già registrate nel foglio:</p><ul style="margin:4px 0 12px;padding-left:20px">' + items_ok_html + '</ul>' if items_ok_html else ''}
          {'<p style="margin:12px 0 4px;font-weight:700;color:#b45309">Richieste fuori standard da verificare in Dashboard:</p><ul style="margin:4px 0 12px;padding-left:20px">' + items_wait_html + '</ul>' if items_wait_html else ''}
          <p style="margin-top:16px">
            <a href="{dash_url}" style="display:inline-block;background:#001C3A;color:#fff;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600;margin-right:8px">Apri Dashboard Presenze</a>
            <a href="{sheet_url}" style="display:inline-block;background:#e2e8f0;color:#0f172a;text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600">Apri Foglio Google</a>
          </p>
          <p style="color:#94a3b8;font-size:12px;margin-top:14px">
            Notifica automatica dell'Assistente Live Timbrature AMR.
          </p>
        </div>
        """

        plain_lines = [
            f"Assistente Presenze AMR - {dipendente} ({now_str})",
            f'Messaggio del dipendente: "{messaggio_utente}"',
            "",
        ]
        if azioni_eseguite:
            plain_lines.append("Modifiche già applicate nel foglio:")
            for a in azioni_eseguite:
                plain_lines.append(f"  - [OK] {a}")
        if azioni_in_attesa:
            plain_lines.append("Richieste fuori standard in attesa di verifica Admin:")
            for a in azioni_in_attesa:
                plain_lines.append(f"  - [DA VERIFICARE] {a}")
        plain_lines.append(f"\nDashboard: {dash_url}")

        msg = MIMEMultipart("alternative")
        msg["From"] = formataddr(("Assistente Presenze AMR", sender_email))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain="amrrecchia.it")
        msg.attach(MIMEText("\n".join(plain_lines), "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP_SSL("authsmtp.securemail.pro", 465, timeout=15) as s:
            s.login(sender_email, password)
            s.sendmail(sender_email, recipients + bcc, msg.as_string())
        logger.info(f"[ASSISTENTE] Email inviata a {recipients} per {dipendente}")
    except Exception as e:
        logger.warning(f"[ASSISTENTE] Errore invio email notifica: {e}")


def _call_gas_admin_rpc(amr_timbra_url: str, amr_timbra_key: str, fn: str, args: list) -> dict:
    payload = {
        "key": amr_timbra_key,
        "action": "admin_rpc",
        "fn": fn,
        "args": args,
    }
    r = requests.post(amr_timbra_url, json=payload, timeout=50)
    return r.json()


def esegui_quick_fix(
    pin: str,
    nome: str,
    data_str: str,
    tipo_fix: str,
    orario_str: str,
    amr_timbra_url: str,
    amr_timbra_key: str,
) -> dict:
    """
    Esegue la correzione rapida 1-click dal banner mattutino (es. Uscita 17:00 o Pausa 12:00-13:00).
    """
    now_dt = datetime.now(ROME_TZ)
    stamp = now_dt.strftime("%d/%m %H:%M")
    nota = f"Assistente AI (confermata da {nome.split()[0]} il {stamp})"

    azioni_eseguite = []
    azioni_in_attesa = []

    if tipo_fix == "manca_pausa":
        # Inserisce Uscita 12:00 ed Entrata 13:00 (o orario indicato tipo 12:00-12:30)
        parts = [p.strip() for p in str(orario_str or "12:00-13:00").split("-") if p.strip()]
        out_pausa = parts[0] if len(parts) >= 1 else "12:00"
        in_pausa = parts[1] if len(parts) >= 2 else "13:00"

        ok1, msg1 = _is_within_guardrails(data_str, out_pausa, now_dt)
        ok2, msg2 = _is_within_guardrails(data_str, in_pausa, now_dt)
        if not (ok1 and ok2):
            motivo = msg1 if not ok1 else msg2
            azioni_in_attesa.append(
                f"Pausa pranzo {out_pausa}–{in_pausa} del {data_str} ({motivo})"
            )
            threading.Thread(
                target=_send_assistente_email,
                args=(nome, f"Conferma pausa {out_pausa}-{in_pausa} del {data_str}", [], azioni_in_attesa),
                daemon=True,
            ).start()
            return {
                "ok": True,
                "auto_applied": False,
                "reply": f"Ho inviato la richiesta di pausa ({out_pausa}–{in_pausa} del {data_str}) in Amministrazione per la conferma ({motivo}).",
            }

        _call_gas_admin_rpc(amr_timbra_url, amr_timbra_key, "aggiungiTimbratura", [{
            "dipendente": nome,
            "pin": pin,
            "data": data_str,
            "tipo": "Uscita",
            "orario": out_pausa,
            "modalita": "Sede",
            "luogo": "Sede AMR · Pederobba (TV) · Via Cavasotta 20",
            "nota": nota,
            "_skipSync": True,
        }])
        _call_gas_admin_rpc(amr_timbra_url, amr_timbra_key, "aggiungiTimbratura", [{
            "dipendente": nome,
            "pin": pin,
            "data": data_str,
            "tipo": "Entrata",
            "orario": in_pausa,
            "modalita": "Sede",
            "luogo": "Sede AMR · Pederobba (TV) · Via Cavasotta 20",
            "nota": nota,
        }])
        azioni_eseguite.append(f"Registrata pausa pranzo {out_pausa}–{in_pausa} sul giorno {data_str}")
        threading.Thread(
            target=_send_assistente_email,
            args=(nome, f"Conferma rapida da app: pausa {out_pausa}–{in_pausa} del {data_str}", azioni_eseguite, []),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "auto_applied": True,
            "reply": f"✅ Fatto {nome.split()[0]}! Ho registrato la tua pausa pranzo ({out_pausa}–{in_pausa}) per il {data_str}.",
        }

    # Default: manca_uscita (o entrata)
    azione_target = "Uscita" if tipo_fix != "manca_entrata" else "Entrata"
    ok_g, msg_g = _is_within_guardrails(data_str, orario_str, now_dt)
    if not ok_g:
        azioni_in_attesa.append(f"{azione_target} alle {orario_str} del {data_str} ({msg_g})")
        threading.Thread(
            target=_send_assistente_email,
            args=(nome, f"Richiesta da app: {azione_target} alle {orario_str} del {data_str}", [], azioni_in_attesa),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "auto_applied": False,
            "reply": f"⏳ Poiché l'orario {orario_str} è fuori dalla fascia standard (06:00–17:30), ho inviato la richiesta ad Alessandro e Massimo che la confermeranno a breve!",
        }

    res_rpc = _call_gas_admin_rpc(amr_timbra_url, amr_timbra_key, "aggiungiTimbratura", [{
        "dipendente": nome,
        "pin": pin,
        "data": data_str,
        "tipo": azione_target,
        "orario": orario_str,
        "modalita": "Sede",
        "luogo": "Sede AMR · Pederobba (TV) · Via Cavasotta 20",
        "nota": nota,
    }])
    if not res_rpc.get("ok") or not (res_rpc.get("result") or {}).get("ok", True):
        err = (res_rpc.get("result") or {}).get("error") or res_rpc.get("error") or "Errore salvataggio"
        return {"ok": False, "error": err}

    azioni_eseguite.append(f"Aggiunta {azione_target} alle {orario_str} sul giorno {data_str}")
    threading.Thread(
        target=_send_assistente_email,
        args=(nome, f"Conferma rapida da app: {azione_target} alle {orario_str} del {data_str}", azioni_eseguite, []),
        daemon=True,
    ).start()
    return {
        "ok": True,
        "auto_applied": True,
        "reply": f"✅ Perfetto {nome.split()[0]}! Ho registrato la tua {azione_target.lower()} delle {orario_str} per il giorno {data_str}.",
    }


def gestisci_messaggio_assistente(
    pin: str,
    nome: str,
    testo_utente: str,
    storico_recente: list[dict],
    amr_timbra_url: str,
    amr_timbra_key: str,
) -> dict:
    """
    Interpreta con Gemini 2.5 Flash il messaggio (testo o vocale trascritto) del dipendente,
    controlla i guardrail (ultimi 3-4 giorni, fascia 06:00-17:30), applica le modifiche
    consentite sul foglio Google e invia l'email di notifica ad amministrazione@ e info@.
    """
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("GEMINI_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not api_key:
        return {"ok": False, "error": "Servizio AI temporaneamente non disponibile"}

    now_dt = datetime.now(ROME_TZ)
    oggi_str = now_dt.strftime("%d/%m/%Y")
    ora_attuale = now_dt.strftime("%H:%M")

    # Costruisci mappa calendario degli ultimi 7 giorni così Gemini risolve "ieri", "venerdì", ecc. senza errori
    cal_lines = []
    for d_off in range(0, 8):
        dt = now_dt - timedelta(days=d_off)
        g_nome = GIORNI_SETTIMANA_IT[dt.weekday()]
        lbl = "OGGI" if d_off == 0 else ("IERI" if d_off == 1 else ("L'ALTRO IERI" if d_off == 2 else f"{d_off} giorni fa"))
        cal_lines.append(f"- {lbl}: {g_nome} {dt.strftime('%d/%m/%Y')}")
    calendario_txt = "\n".join(cal_lines)

    storico_txt = json.dumps(storico_recente or [], ensure_ascii=False, indent=2)

    prompt = f"""Sei l'Assistente Personale Orari e Timbrature di AMR Recchia Srl.
Stai parlando direttamente con il dipendente: {nome} (PIN verificato).
Data e ora attuale: {GIORNI_SETTIMANA_IT[now_dt.weekday()]} {oggi_str}, ore {ora_attuale}.

CALENDARIO DEGLI ULTIMI 7 GIORNI:
{calendario_txt}

TIMBRATURE ATTUALI DI {nome.upper()} NEGLI ULTIMI GIORNI (dal foglio Google):
{storico_txt}

MESSAGGIO DEL DIPENDENTE:
"{testo_utente}"

COMPITO:
1. Capisci cosa chiede {nome.split()[0]} (es. ha dimenticato di timbrare l'uscita ieri alle 17:00, oppure stamattina è arrivato alle 07:00 ma l'app ha timbrato più tardi o non entrava, oppure ha fatto pausa pranzo dalle 12:00 alle 13:00, oppure chiede solo informazioni sulle sue ore).
2. Se per quel giorno e fascia oraria ESISTE GIÀ una timbratura dello stesso tipo (es. stamattina c'è già Entrata alle 07:22 con rowIndex=2024 e il dipendente dice "sono arrivato alle 07:00"), usa l'operazione "modifica" indicando il `rowIndex` esistente invece di creare un doppione!
3. Se invece manca completamente la timbratura (es. manca l'Uscita delle 17:00 o mancano l'Uscita delle 12:00 e l'Entrata delle 13:00 per la pausa pranzo), usa l'operazione "aggiungi".
4. Rispondi SEMPRE in JSON puro con questo schema esatto:
{{
  "reply": "Risposta cordiale, chiara e concisa in italiano per {nome.split()[0]}",
  "operations": [
    {{
      "op": "aggiungi" | "modifica",
      "data": "DD/MM/YYYY",
      "azione": "Entrata" | "Uscita",
      "orario": "HH:MM",
      "rowIndex": numero_riga_se_modifica_altrimenti_null,
      "motivo": "breve descrizione"
    }}
  ]
}}
Se il dipendente fa solo una domanda senza chiedere modifiche, lascia `"operations": []`.
Restituisci SOLO il JSON valido, senza markdown."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
        },
    }

    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        cand = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(cand)
    except Exception as e:
        logger.error(f"[ASSISTENTE] Errore chiamata Gemini: {e}")
        return {"ok": False, "error": "Non sono riuscito a elaborare la richiesta, riprova tra qualche secondo."}

    reply_ai = str(parsed.get("reply") or "").strip()
    ops = parsed.get("operations") or []

    if not ops:
        return {
            "ok": True,
            "auto_applied": False,
            "reply": reply_ai or f"Ciao {nome.split()[0]}! Dimmi pure quale giorno e quale orario di entrata, pausa o uscita devo sistemarti.",
        }

    stamp = now_dt.strftime("%d/%m %H:%M")
    nota = f"Assistente AI (richiesta {nome.split()[0]} {stamp})"

    azioni_eseguite = []
    azioni_in_attesa = []

    for idx, op in enumerate(ops):
        op_type = str(op.get("op") or "aggiungi").strip().lower()
        data_s = str(op.get("data") or oggi_str).strip()
        azione_s = "Uscita" if str(op.get("azione") or "").strip().lower() == "uscita" else "Entrata"
        orario_s = str(op.get("orario") or "").strip()
        row_idx = op.get("rowIndex")
        is_last = (idx == len(ops) - 1)

        ok_g, msg_g = _is_within_guardrails(data_s, orario_s, now_dt)
        if not ok_g:
            azioni_in_attesa.append(
                f"{op_type.capitalize()} {azione_s} alle {orario_s} del {data_s} ({msg_g})"
            )
            continue

        # Verifica se nello storico recente c'è già una riga dello stesso giorno/azione entro 90 min da modificare
        if op_type == "aggiungi" and not row_idx:
            target_min = _parse_hhmm_to_minutes(orario_s)
            for ev in (storico_recente or []):
                if ev.get("data") == data_s and ev.get("azione") == azione_s and ev.get("rowIndex"):
                    ev_min = _parse_hhmm_to_minutes(ev.get("orario", ""))
                    if target_min is not None and ev_min is not None and abs(target_min - ev_min) <= 90:
                        op_type = "modifica"
                        row_idx = ev.get("rowIndex")
                        break

        if op_type == "modifica" and row_idx:
            mod_arg = {
                "rowIndex": int(row_idx),
                "azione": azione_s,
                "entrata": orario_s if azione_s == "Entrata" else "",
                "uscita": orario_s if azione_s == "Uscita" else "",
                "nota": nota,
                "_skipSync": not is_last,
            }
            res_rpc = _call_gas_admin_rpc(amr_timbra_url, amr_timbra_key, "modificaOrario", [mod_arg])
            if res_rpc.get("ok") and (res_rpc.get("result") or {}).get("ok", True):
                azioni_eseguite.append(f"Modificata {azione_s} del {data_s} a {orario_s} (riga {row_idx})")
            else:
                azioni_in_attesa.append(f"Modifica {azione_s} del {data_s} a {orario_s} (errore tecnico)")
        else:
            ins_arg = {
                "dipendente": nome,
                "pin": pin,
                "data": data_s,
                "tipo": azione_s,
                "orario": orario_s,
                "modalita": "Sede",
                "luogo": "Sede AMR · Pederobba (TV) · Via Cavasotta 20",
                "nota": nota,
                "_skipSync": not is_last,
            }
            res_rpc = _call_gas_admin_rpc(amr_timbra_url, amr_timbra_key, "aggiungiTimbratura", [ins_arg])
            if res_rpc.get("ok") and (res_rpc.get("result") or {}).get("ok", True):
                azioni_eseguite.append(f"Inserita {azione_s} alle {orario_s} per il giorno {data_s}")
            else:
                azioni_in_attesa.append(f"Inserimento {azione_s} alle {orario_s} del {data_s} (errore tecnico)")

    if azioni_eseguite or azioni_in_attesa:
        threading.Thread(
            target=_send_assistente_email,
            args=(nome, testo_utente, azioni_eseguite, azioni_in_attesa),
            daemon=True,
        ).start()

    if azioni_in_attesa and not azioni_eseguite:
        final_reply = (
            f"⏳ Ho preso in carico la tua richiesta ({', '.join(azioni_in_attesa)}) e l'ho inviata "
            f"ad Alessandro e Massimo in Amministrazione perché riguarda un orario fuori dalla fascia standard "
            f"(06:00–17:30) o una data meno recente."
        )
    elif azioni_in_attesa and azioni_eseguite:
        final_reply = (
            f"{reply_ai}\n\n✅ Ho già registrato: {', '.join(azioni_eseguite)}.\n"
            f"⏳ Inviato invece in Amministrazione per conferma: {', '.join(azioni_in_attesa)}."
        )
    else:
        final_reply = reply_ai or f"✅ Fatto {nome.split()[0]}! Ho aggiornato le tue timbrature ({', '.join(azioni_eseguite)})."

    return {
        "ok": True,
        "auto_applied": bool(azioni_eseguite),
        "azioni_eseguite": azioni_eseguite,
        "azioni_in_attesa": azioni_in_attesa,
        "reply": final_reply,
    }
