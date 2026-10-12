"""
presenze_service.py - Servizio di Calcolo Presenze, Ore e Anomalie AMR Recchia
==============================================================================
Elabora i dati in tempo reale dal Google Spreadsheet "Registro delle timbrature"
e "Registro Permessi", gestendo sia il vecchio formato a riga singola che il nuovo
formato ad eventi separati (Entrata / Uscita su righe distinte).
Include caching intelligente a 60 secondi e autenticazione tramite PIN dipendente.
"""

from __future__ import annotations
import os
import time
import json
import csv
import io
import urllib.parse
import subprocess
import datetime
from collections import defaultdict
import user_auth

SPREADSHEET_ID = "1Uul8Juug4uIzKes4HBGHRDAOT6OhHOumuodZ0YmFg2w"

# Cache in memoria
_CACHE = {
    "timbrature": None,
    "permessi": None,
    "timestamp": 0
}
CACHE_TTL_SECONDS = 60

SEDE_AMR_LAT = 45.626
SEDE_AMR_LNG = 12.225
SOGLIA_SEDE_KM = 0.250

ITALIAN_DAYS = {
    0: "Lunedì", 1: "Martedì", 2: "Mercoledì", 3: "Giovedì",
    4: "Venerdì", 5: "Sabato", 6: "Domenica"
}

ITALIAN_MONTHS = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre"
}


def fetch_sheet_csv(sheet_name: str) -> list[list[str]]:
    """Scarica i dati della scheda Google Sheets via GViz in formato CSV."""
    url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(sheet_name)}"
    try:
        res = subprocess.run(["curl", "-s", "-L", url], capture_output=True, text=True, timeout=12)
        if res.returncode == 0 and res.stdout:
            reader = csv.reader(io.StringIO(res.stdout))
            return list(reader)
    except Exception as e:
        print(f"Errore download {sheet_name}: {e}")
    return []


def get_cached_sheets_data():
    """Recupera i dati con cache a 60s per massime prestazioni."""
    now = time.time()
    if _CACHE["timbrature"] is not None and (now - _CACHE["timestamp"] < CACHE_TTL_SECONDS):
        return _CACHE["timbrature"], _CACHE["permessi"]

    timb_data = fetch_sheet_csv("Registro delle timbrature")
    perm_data = fetch_sheet_csv("Registro Permessi")

    if timb_data:
        _CACHE["timbrature"] = timb_data
        _CACHE["permessi"] = perm_data or []
        _CACHE["timestamp"] = now

    return _CACHE["timbrature"] or [], _CACHE["permessi"] or []


def parse_time_to_minutes(t_str: str) -> float | None:
    if not t_str:
        return None
    p = t_str.strip().split(":")
    if len(p) >= 2:
        try:
            sec = float(p[2]) if len(p) > 2 else 0.0
            return int(p[0]) * 60 + int(p[1]) + sec / 60.0
        except ValueError:
            return None
    return None


def format_minutes_hhmm(mins: float) -> str:
    if mins <= 0:
        return "0h 00m"
    h = int(mins // 60)
    m = int(round(mins % 60))
    if m == 60:
        h += 1
        m = 0
    return f"{h}h {m:02d}m"


def iso_week_num(date_obj: datetime.date) -> int:
    return date_obj.isocalendar()[1]


def parse_date(date_str: str) -> datetime.date | None:
    if not date_str:
        return None
    s = date_str.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def get_employee_by_pin(pin: str) -> dict | None:
    """Restituisce il profilo utente associato al PIN o None se non valido."""
    if not pin:
        return None
    pin_clean = str(pin).strip()
    return user_auth.get_user_by_pin(pin_clean)


def calculate_employee_report(pin: str, target_month_str: str = None) -> dict:
    """
    Calcola il resoconto completo per un singolo dipendente:
    - Ore giornaliere e sessioni accoppiate (Entrata -> Uscita)
    - Totali settimanali
    - Totale mensile e saldo rispetto alle ore standard lavorabili
    - Eventuali anomalie (dovute al blocco del tablet)
    """
    user = get_employee_by_pin(pin)
    if not user:
        return {"error": "PIN non valido o dipendente non registrato", "authenticated": False}

    emp_name = user["name"]
    timb_rows, perm_rows = get_cached_sheets_data()

    if not timb_rows:
        return {"error": "Impossibile recuperare i dati dal registro presenze", "authenticated": True}

    header = timb_rows[0]
    data = timb_rows[1:]

    # Filtra timbrature per questo dipendente
    emp_records = []
    for r in data:
        if len(r) > 1 and r[1].strip().lower() == emp_name.lower():
            # Righe marcate "Doppione" (colonna L Note) restano nel foglio ma non contano.
            # I "Doppione probabile" invece contano finché non vengono verificati a mano.
            nota_l = r[11].lower() if len(r) > 11 else ""
            if "doppione" in nota_l and "probabile" not in nota_l:
                continue
            emp_records.append(r)

    # Identifica mesi disponibili
    months_available = set()
    for r in emp_records:
        d = parse_date(r[0])
        if d:
            m_key = f"{d.month:02d}/{d.year}"
            months_available.add((d.year, d.month, m_key))

    # Ordina mesi decrescenti
    sorted_months = sorted(list(months_available), key=lambda x: (x[0], x[1]), reverse=True)
    month_options = [{"key": m[2], "label": f"{ITALIAN_MONTHS.get(m[1], '')} {m[0]}"} for m in sorted_months]

    # Mese target di default: mese corrente o il più recente disponibile
    today = datetime.date.today()
    default_m_key = f"{today.month:02d}/{today.year}"
    if not target_month_str:
        target_month_str = default_m_key if any(m["key"] == default_m_key for m in month_options) else (month_options[0]["key"] if month_options else default_m_key)

    try:
        t_m, t_y = [int(p) for p in target_month_str.split("/")]
    except Exception:
        t_m, t_y = today.month, today.year

    # Raggruppa per data
    by_day = defaultdict(list)
    for r in emp_records:
        d = parse_date(r[0])
        if d and d.month == t_m and d.year == t_y:
            by_day[d].append(r)

    daily_list = []
    weekly_totals = defaultdict(float)
    monthly_minutes = 0.0
    anomalies_count = 0

    # Elabora ogni giorno del mese in ordine decrescente
    for d, d_rows in sorted(by_day.items(), key=lambda x: x[0], reverse=True):
        sessions = []
        unpaired_in = []
        unpaired_out = []

        for r in d_rows:
            in_str = r[4].strip() if len(r) > 4 else ""
            out_str = r[5].strip() if len(r) > 5 else ""
            tipo = r[10].strip() if len(r) > 10 else "Sede"
            gps = r[8].strip() if len(r) > 8 else ""
            luogo = r[9].strip() if len(r) > 9 else ""

            if in_str and out_str:
                m_in = parse_time_to_minutes(in_str)
                m_out = parse_time_to_minutes(out_str)
                dur = max(0, m_out - m_in) if (m_in and m_out) else 0
                sessions.append({
                    "in": in_str,
                    "out": out_str,
                    "mins": dur,
                    "duration_fmt": format_minutes_hhmm(dur),
                    "tipo": tipo or "Sede",
                    "status": "OK",
                    "gps": gps,
                    "luogo": luogo
                })
            elif in_str:
                unpaired_in.append({
                    "time": in_str,
                    "mins": parse_time_to_minutes(in_str) or 0,
                    "tipo": tipo or "Sede",
                    "gps": gps,
                    "luogo": luogo
                })
            elif out_str:
                unpaired_out.append({
                    "time": out_str,
                    "mins": parse_time_to_minutes(out_str) or 0,
                    "tipo": tipo or "Sede",
                    "gps": gps,
                    "luogo": luogo
                })

        # Ordina cronologicamente
        unpaired_in.sort(key=lambda x: x["mins"])
        unpaired_out.sort(key=lambda x: x["mins"])

        in_idx = 0
        out_idx = 0
        while in_idx < len(unpaired_in) and out_idx < len(unpaired_out):
            cur_in = unpaired_in[in_idx]
            cur_out = unpaired_out[out_idx]

            if cur_out["mins"] >= cur_in["mins"]:
                dur = cur_out["mins"] - cur_in["mins"]
                sessions.append({
                    "in": cur_in["time"],
                    "out": cur_out["time"],
                    "mins": dur,
                    "duration_fmt": format_minutes_hhmm(dur),
                    "tipo": cur_in["tipo"],
                    "status": "OK",
                    "gps": cur_in["gps"] or cur_out["gps"],
                    "luogo": cur_in["luogo"] or cur_out["luogo"]
                })
                in_idx += 1
                out_idx += 1
            else:
                sessions.append({
                    "in": None,
                    "out": cur_out["time"],
                    "mins": 0,
                    "duration_fmt": "0h 00m",
                    "tipo": cur_out["tipo"],
                    "status": "INGRESSO_MANCANTE",
                    "gps": cur_out["gps"],
                    "luogo": cur_out["luogo"]
                })
                anomalies_count += 1
                out_idx += 1

        # Ingressi rimasti senza uscita
        while in_idx < len(unpaired_in):
            cur_in = unpaired_in[in_idx]
            is_today = (d == today)
            st = "IN_CORSO" if is_today else "USCITA_MANCANTE"
            if st == "USCITA_MANCANTE":
                anomalies_count += 1
            sessions.append({
                "in": cur_in["time"],
                "out": None,
                "mins": 0,
                "duration_fmt": "In corso..." if st == "IN_CORSO" else "Mancante",
                "tipo": cur_in["tipo"],
                "status": st,
                "gps": cur_in["gps"],
                "luogo": cur_in["luogo"]
            })
            in_idx += 1

        # Uscite rimaste senza ingresso
        while out_idx < len(unpaired_out):
            cur_out = unpaired_out[out_idx]
            sessions.append({
                "in": None,
                "out": cur_out["time"],
                "mins": 0,
                "duration_fmt": "0h 00m",
                "tipo": cur_out["tipo"],
                "status": "INGRESSO_MANCANTE",
                "gps": cur_out["gps"],
                "luogo": cur_out["luogo"]
            })
            anomalies_count += 1
            out_idx += 1

        sessions.sort(key=lambda x: parse_time_to_minutes(x["in"]) if x["in"] else 9999)

        day_mins = sum(s["mins"] for s in sessions)
        monthly_minutes += day_mins
        w_num = iso_week_num(d)
        weekly_totals[w_num] += day_mins

        day_status = "REGOLARE"
        if any(s["status"] in ("USCITA_MANCANTE", "INGRESSO_MANCANTE") for s in sessions):
            day_status = "ANOMALIA"
        elif any(s["status"] == "IN_CORSO" for s in sessions):
            day_status = "IN_CORSO"
        elif any(s["tipo"].lower() == "trasferta" for s in sessions):
            day_status = "TRASFERTA"

        daily_list.append({
            "date": d.strftime("%d/%m/%Y"),
            "date_iso": d.isoformat(),
            "day_name": ITALIAN_DAYS.get(d.weekday(), ""),
            "week_num": w_num,
            "sessions": sessions,
            "total_mins": day_mins,
            "total_hours": round(day_mins / 60.0, 2),
            "total_fmt": format_minutes_hhmm(day_mins),
            "status": day_status
        })

    # Permessi e ferie del mese
    permessi_hours = 0.0
    ferie_hours = 0.0
    permessi_list = []

    if perm_rows and len(perm_rows) > 1:
        for pr in perm_rows[1:]:
            p_name = pr[0].strip() if len(pr) > 0 else ""
            p_tipo = pr[2].strip() if len(pr) > 2 else ""
            p_start = parse_date(pr[3]) if len(pr) > 3 else None
            p_end = parse_date(pr[4]) if len(pr) > 4 else p_start
            p_ore_str = pr[5].strip() if len(pr) > 5 else "8"
            p_stato = pr[7].strip() if len(pr) > 7 else ""
            p_note = pr[6].strip() if len(pr) > 6 else ""

            if p_name.lower() == emp_name.lower() and p_start and p_start.month == t_m and p_start.year == t_y:
                try:
                    p_ore = float(p_ore_str.replace(",", "."))
                except Exception:
                    p_ore = 8.0

                if p_stato.lower() == "approvato":
                    if "ferie" in p_tipo.lower():
                        ferie_hours += p_ore
                    else:
                        permessi_hours += p_ore

                permessi_list.append({
                    "tipo": p_tipo,
                    "data_inizio": pr[3],
                    "data_fine": pr[4],
                    "ore": p_ore,
                    "stato": p_stato,
                    "note": p_note
                })

    # Settimane formattate
    weeks_summary = []
    current_w_num = iso_week_num(today)
    for w_num in sorted(weekly_totals.keys(), reverse=True):
        mins = weekly_totals[w_num]
        weeks_summary.append({
            "week_num": w_num,
            "is_current": (w_num == current_w_num and t_y == today.year),
            "total_hours": round(mins / 60.0, 2),
            "total_fmt": format_minutes_hhmm(mins),
            "standard_target": 40.0
        })

    return {
        "authenticated": True,
        "employee": {
            "name": user["name"],
            "department": user["department"],
            "email": user["email"],
            "is_commercial": user.get("is_commercial", False),
            "label": user.get("label", user["department"])
        },
        "selected_month": {
            "key": target_month_str,
            "label": f"{ITALIAN_MONTHS.get(t_m, '')} {t_y}"
        },
        "month_options": month_options,
        "kpi": {
            "total_hours": round(monthly_minutes / 60.0, 2),
            "total_fmt": format_minutes_hhmm(monthly_minutes),
            "days_worked": len(daily_list),
            "anomalies_count": anomalies_count,
            "permessi_hours": permessi_hours,
            "ferie_hours": ferie_hours
        },
        "weeks": weeks_summary,
        "daily": daily_list,
        "permessi": permessi_list
    }


def get_all_employees_overview(target_month_str: str = None) -> list[dict]:
    """Restituisce la sintesi presenze di tutti i dipendenti per l'amministrazione."""
    timb_rows, _ = get_cached_sheets_data()
    overview = []
    for pin, u in user_auth.AUTH_USERS.items():
        if u["name"] in ["Operatore Produzione", "Operatore Officina"]:
            continue
        rep = calculate_employee_report(pin, target_month_str)
        if rep.get("authenticated"):
            overview.append({
                "pin": pin,
                "name": u["name"],
                "department": u["department"],
                "is_commercial": u.get("is_commercial", False),
                "kpi": rep["kpi"]
            })
    return overview
