"""
Modulo di Calcolo Orario Lavorativo (Business Hours) AMR Recchia
================================================================
Regole di fabbrica:
- Turno Mattina:   08:30 - 12:30 (4 ore)
- Pausa Pranzo:    12:30 - 13:30 (0 ore)
- Turno Pomeriggio:13:30 - 17:30 (4 ore)
- Totale Feriale:  8 ore lavorative al giorno (Lunedì - Venerdì)
- Weekend e Notti: Esclusi al 100% (0 ore)
"""

from datetime import datetime, time, timedelta
import zoneinfo

TZ_ROME = zoneinfo.ZoneInfo("Europe/Rome")

MORNING_START = time(8, 30)
MORNING_END = time(12, 30)
AFTERNOON_START = time(13, 30)
AFTERNOON_END = time(17, 30)


def calculate_business_minutes(start_dt: datetime, end_dt: datetime) -> int:
    """
    Calcola i minuti lavorativi effettivi compresi tra start_dt e end_dt.
    Esclude weekend (sabato/domenica), orari notturni e pausa pranzo.
    """
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=TZ_ROME)
    else:
        start_dt = start_dt.astimezone(TZ_ROME)

    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=TZ_ROME)
    else:
        end_dt = end_dt.astimezone(TZ_ROME)

    if end_dt <= start_dt:
        return 0

    total_minutes = 0
    current = start_dt

    while current.date() <= end_dt.date():
        # Salta weekend (5 = Sabato, 6 = Domenica)
        if current.weekday() < 5:
            day_date = current.date()
            m_start = datetime.combine(day_date, MORNING_START, tzinfo=TZ_ROME)
            m_end = datetime.combine(day_date, MORNING_END, tzinfo=TZ_ROME)
            a_start = datetime.combine(day_date, AFTERNOON_START, tzinfo=TZ_ROME)
            a_end = datetime.combine(day_date, AFTERNOON_END, tzinfo=TZ_ROME)

            # Finestra mattina
            win_start = max(start_dt, m_start)
            win_end = min(end_dt, m_end)
            if win_end > win_start:
                total_minutes += int((win_end - win_start).total_seconds() / 60)

            # Finestra pomeriggio
            win_start = max(start_dt, a_start)
            win_end = min(end_dt, a_end)
            if win_end > win_start:
                total_minutes += int((win_end - win_start).total_seconds() / 60)

        # Avanza al giorno successivo alle 00:00
        current = datetime.combine(current.date() + timedelta(days=1), time(0, 0), tzinfo=TZ_ROME)

    return total_minutes


def format_duration(minutes: int) -> str:
    """
    Formatta i minuti in una stringa leggibile per l'officina (es. '2h 30m', '45m', '8h').
    """
    if minutes <= 0:
        return "< 5m"
    hours = minutes // 60
    rem_min = minutes % 60
    if hours == 0:
        return f"{rem_min}m"
    if rem_min == 0:
        return f"{hours}h"
    return f"{hours}h {rem_min}m"


if __name__ == "__main__":
    # Test 1: Stesso giorno (09:00 -> 11:30 = 2h 30m)
    d1 = datetime(2026, 9, 4, 9, 0, tzinfo=TZ_ROME)
    d2 = datetime(2026, 9, 4, 11, 30, tzinfo=TZ_ROME)
    m = calculate_business_minutes(d1, d2)
    print(f"Test 1 (Stesso giorno mattina): {m} min -> {format_duration(m)} (Atteso: 150m, 2h 30m)")

    # Test 2: Attraverso pausa pranzo (11:30 -> 14:30 = 1h mattina + 1h pomeriggio = 2h)
    d3 = datetime(2026, 9, 4, 11, 30, tzinfo=TZ_ROME)
    d4 = datetime(2026, 9, 4, 14, 30, tzinfo=TZ_ROME)
    m2 = calculate_business_minutes(d3, d4)
    print(f"Test 2 (Attraverso pranzo): {m2} min -> {format_duration(m2)} (Atteso: 120m, 2h)")

    # Test 3: Attraverso notte (Giovedì 16:30 -> Venerdì 09:30 = 1h giovedì + 1h venerdì = 2h)
    d5 = datetime(2026, 9, 3, 16, 30, tzinfo=TZ_ROME)
    d6 = datetime(2026, 9, 4, 9, 30, tzinfo=TZ_ROME)
    m3 = calculate_business_minutes(d5, d6)
    print(f"Test 3 (Attraverso notte): {m3} min -> {format_duration(m3)} (Atteso: 120m, 2h)")

    # Test 4: Attraverso weekend (Venerdì 16:30 -> Lunedì 09:30 = 1h ven + 0 sab + 0 dom + 1h lun = 2h)
    d7 = datetime(2026, 9, 4, 16, 30, tzinfo=TZ_ROME)
    d8 = datetime(2026, 9, 7, 9, 30, tzinfo=TZ_ROME)
    m4 = calculate_business_minutes(d7, d8)
    print(f"Test 4 (Attraverso weekend): {m4} min -> {format_duration(m4)} (Atteso: 120m, 2h)")
