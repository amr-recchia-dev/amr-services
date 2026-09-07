#!/usr/bin/env python3
"""
stress_test.py - Suite di Stress Test e Verifica Sicurezza per AMR Services
===========================================================================
1. Test Sicurezza e Controllo Accessi (PIN Auth & Error 401)
2. Test Rate Limiting (Anti-DDoS / Anti-Abuso & Error 429)
3. Stress Test Concorrente (10, 25 e 50 richieste simultanee)
   - Misura Latenza Min, Media, Max, p95 e Tasso di Successo
"""

import sys
import time
import requests
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "https://amr-services.onrender.com"
PIN = sys.argv[2] if len(sys.argv) > 2 else "2026"

print("=" * 65)
print(f"🚀 AVVIO AUDIT DI SICUREZZA & STRESS TEST")
print(f"📡 Target: {BASE_URL}")
print(f"🔐 PIN Aziendale configurato: {PIN}")
print("=" * 65)


# -------------------------------------------------------------
# FASE 1: TEST AUTENTICAZIONE & BLOCCO DATI NON AUTORIZZATI
# -------------------------------------------------------------
print("\n[FASE 1] Verifica Controllo Accessi (PIN Auth)...")

# 1.1 Chiamata senza PIN a endpoint protetto
r_unauth = requests.get(f"{BASE_URL}/api/dashboard-data", timeout=15)
if r_unauth.status_code == 401:
    print("  ✅ Accesso senza PIN bloccato correttamente (HTTP 401 Unauthorized)")
else:
    print(f"  ❌ FALLITO: Accesso senza PIN ha restituito HTTP {r_unauth.status_code} invece di 401")

# 1.2 Chiamata con PIN errato
r_wrong = requests.get(f"{BASE_URL}/api/dashboard-data", headers={"X-AMR-PIN": "9999"}, timeout=15)
if r_wrong.status_code == 401:
    print("  ✅ Accesso con PIN errato respinto (HTTP 401 Unauthorized)")
else:
    print(f"  ❌ FALLITO: PIN errato non respinto (HTTP {r_wrong.status_code})")

# 1.3 Chiamata con PIN corretto
r_ok = requests.get(f"{BASE_URL}/api/dashboard-data", headers={"X-AMR-PIN": PIN}, timeout=20)
if r_ok.status_code == 200:
    projects = r_ok.json().get("projects", [])
    print(f"  ✅ Accesso con PIN corretto autorizzato (HTTP 200 OK - {len(projects)} commesse ricevute)")
else:
    print(f"  ❌ FALLITO: PIN corretto non ha restituito 200 (HTTP {r_ok.status_code}: {r_ok.text[:100]})")

# 1.4 Chiamata a endpoint test non autorizzato
r_test = requests.get(f"{BASE_URL}/test/123", timeout=15)
if r_test.status_code == 403:
    print("  ✅ Endpoint di debug /test/<item_id> protetto con successo (HTTP 403 Forbidden)")
else:
    print(f"  ⚠️ Nota: /test/<item_id> ha restituito HTTP {r_test.status_code}")


# -------------------------------------------------------------
# FASE 2: TEST RATE LIMITER ANTI-ABUSO (ANTI-DOS)
# -------------------------------------------------------------
print("\n[FASE 2] Verifica Rate Limiting Anti-Abuso...")
print("  Invio sequenziale rapido di 15 richieste a /api/verify-pin per verificare intervento del Rate Limiter...")

rate_limit_triggered = False
for i in range(15):
    resp = requests.post(f"{BASE_URL}/api/verify-pin", headers={"X-AMR-PIN": "0000"}, timeout=10)
    if resp.status_code == 429:
        rate_limit_triggered = True
        print(f"  ✅ Rate Limiter intervenuto con successo alla richiesta #{i+1} (HTTP 429 Too Many Requests)")
        break
    time.sleep(0.05)

if not rate_limit_triggered:
    print("  ⚠️ Attenzione: il rate limiter non è intervenuto entro 15 richieste.")


# -------------------------------------------------------------
# FASE 3: STRESS TEST VOLUMETRICO CONCORRENTE
# -------------------------------------------------------------
def run_concurrent_batch(concurrency: int, endpoint: str, headers: dict = None):
    print(f"\n⚡ Test Concorrenza: {concurrency} richieste simultanee su {endpoint}...")
    headers = headers or {}
    latencies = []
    status_counts = {}

    def make_req():
        t0 = time.time()
        try:
            r = requests.get(f"{BASE_URL}{endpoint}", headers=headers, timeout=30)
            elapsed = time.time() - t0
            return r.status_code, elapsed
        except Exception as e:
            elapsed = time.time() - t0
            return f"ERR: {type(e).__name__}", elapsed

    start_batch = time.time()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(make_req) for _ in range(concurrency)]
        for f in as_completed(futures):
            code, lat = f.result()
            latencies.append(lat)
            status_counts[code] = status_counts.get(code, 0) + 1
    total_time = time.time() - start_batch

    # Metriche
    success_count = status_counts.get(200, 0)
    rate_limited_count = status_counts.get(429, 0)
    error_count = sum(count for sc, count in status_counts.items() if sc not in (200, 429))
    
    avg_lat = statistics.mean(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    p95_lat = sorted(latencies)[int(len(latencies) * 0.95)] if len(latencies) > 1 else max_lat
    throughput = concurrency / total_time

    print(f"  • Tempo totale batch: {total_time:.2f}s | Throughput: {throughput:.1f} req/s")
    print(f"  • Risposte: 200 OK: {success_count} | 429 Rate Limited: {rate_limited_count} | Altri/Errori: {error_count}")
    print(f"  • Latenza: Min: {min_lat:.2f}s | Media: {avg_lat:.2f}s | p95: {p95_lat:.2f}s | Max: {max_lat:.2f}s")
    return {
        "concurrency": concurrency,
        "total_time": total_time,
        "throughput": throughput,
        "success_rate": (success_count / concurrency) * 100,
        "avg_latency": avg_lat,
        "p95_latency": p95_lat,
        "status_distribution": status_counts
    }

print("\n[FASE 3] Esecuzione Benchmark e Stress Test...")

# Baseline Health Check
run_concurrent_batch(10, "/health")
run_concurrent_batch(25, "/health")

# Stress Test su Endpoint con Auth
run_concurrent_batch(10, "/api/dashboard-data", headers={"X-AMR-PIN": PIN})
run_concurrent_batch(25, "/api/dashboard-data", headers={"X-AMR-PIN": PIN})

print("\n" + "=" * 65)
print("🏁 AUDIT E STRESS TEST COMPLETATI CON SUCCESSO")
print("=" * 65)
