#!/usr/bin/env python3
"""
OML price checker — odpalany przez GitHub Actions (cron co 10 min w godz. 7-16 UTC).

- Sprawdza, czy aktualny czas Europe/Warsaw mieści się w jednym ze "slotów":
  09:30, 14:30, 17:30 (tolerancja: bieżąca godzina + minuty 30..39 → slot=HH:30).
- Pobiera kurs OML ze Stooq (CSV).
- Jeśli kurs poza progami z data/config.json — wysyła Web Push do data/subscription.json.
- Plik data/last_run.json zapobiega podwójnemu wysłaniu w ramach tego samego slotu.

Wymagane zmienne środowiskowe (GitHub Secrets):
  VAPID_PRIVATE_KEY  — PEM (cały blok)
  VAPID_PUBLIC_KEY   — URL-safe base64 (informacyjnie)
  VAPID_SUBJECT      — np. mailto:azbroja@outlook.com
  FORCE_NOTIFY       — "true" wymusza wysyłkę (dla workflow_dispatch / testów)
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from pywebpush import webpush, WebPushException

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG_PATH = DATA / "config.json"
SUB_PATH = DATA / "subscription.json"
STATE_PATH = DATA / "last_run.json"

WARSAW = ZoneInfo("Europe/Warsaw")
SLOTS = {"09:30", "14:30", "17:30"}
TOLERANCE_MINUTES = 10  # cron co 10 min — uznaj 09:30..09:39 za slot 09:30


@dataclass
class Quote:
    symbol: str
    date: str
    time: str
    close: float
    open_: float
    high: float
    low: float
    volume: int


def log(msg: str) -> None:
    print(f"[oml] {msg}", flush=True)


def current_slot(now: datetime) -> str | None:
    """Zwraca '09:30' / '14:30' / '17:30' jeśli now mieści się w slocie, inaczej None."""
    if now.weekday() >= 5:  # 5 = sobota, 6 = niedziela
        return None
    for slot in SLOTS:
        hh, mm = map(int, slot.split(":"))
        slot_start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        slot_end = slot_start + timedelta(minutes=TOLERANCE_MINUTES)
        if slot_start <= now < slot_end:
            return slot
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"WARN: nie mogę zparsować {path.name}: {e}")
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def fetch_quote(ticker: str = "oml") -> Quote | None:
    url = f"https://stooq.com/q/l/?s={ticker.lower()}&f=sd2t2ohlcv&h&e=csv"
    log(f"GET {url}")
    r = requests.get(url, timeout=15, headers={"User-Agent": "oml-alert/1.0"})
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        log("Brak danych w odpowiedzi Stooq")
        return None
    row = rows[0]

    def f(x):
        try: return float(x)
        except (TypeError, ValueError): return float("nan")

    def i(x):
        try: return int(x)
        except (TypeError, ValueError): return 0

    if row.get("Close", "").upper() in ("N/D", "", "N/A"):
        log(f"Stooq nie zwrócił close: {row}")
        return None

    return Quote(
        symbol=row.get("Symbol", ticker.upper()),
        date=row.get("Date", ""),
        time=row.get("Time", ""),
        close=f(row.get("Close")),
        open_=f(row.get("Open")),
        high=f(row.get("High")),
        low=f(row.get("Low")),
        volume=i(row.get("Volume")),
    )


def send_push(subscription: dict, payload: dict) -> None:
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    sub_claim = os.environ.get("VAPID_SUBJECT", "mailto:owner@example.com").strip()
    if not priv:
        raise RuntimeError("Brak VAPID_PRIVATE_KEY w zmiennych środowiskowych.")
    webpush(
        subscription_info=subscription,
        data=json.dumps(payload),
        vapid_private_key=priv,
        vapid_claims={"sub": sub_claim},
        ttl=3600,
    )


def build_message(q: Quote, lower: float, upper: float) -> tuple[str, str, str] | None:
    """Zwraca (title, body, kind) gdy alert się należy, inaczej None."""
    if q.close < lower:
        title = f"OML ↓ {q.close:.2f} PLN"
        body = f"Spadek poniżej {lower:.2f} PLN. Open {q.open_:.2f}, dzień: {q.low:.2f}–{q.high:.2f}."
        return title, body, "below"
    if q.close > upper:
        title = f"OML ↑ {q.close:.2f} PLN"
        body = f"Wzrost powyżej {upper:.2f} PLN. Open {q.open_:.2f}, dzień: {q.low:.2f}–{q.high:.2f}."
        return title, body, "above"
    return None


def main() -> int:
    now = datetime.now(tz=WARSAW)
    force = os.environ.get("FORCE_NOTIFY", "false").lower() == "true"
    slot = current_slot(now) or ("manual" if force else None)
    log(f"Warsaw now: {now.isoformat()}  slot={slot}  force={force}")
    if slot is None:
        log("Poza slotem — kończę.")
        return 0

    state = load_json(STATE_PATH, {})
    today = now.date().isoformat()
    last_key = f"{today}@{slot}"
    if state.get("lastKey") == last_key and not force:
        log(f"Slot {last_key} już obsłużony — kończę.")
        return 0

    cfg = load_json(CFG_PATH, None)
    if not cfg:
        log("Brak data/config.json — ustaw progi w PWA i spróbuj ponownie.")
        return 0
    sub = load_json(SUB_PATH, None)
    if not sub or not sub.get("endpoint"):
        log("Brak data/subscription.json — włącz powiadomienia w PWA.")
        return 0

    lower = float(cfg.get("lower"))
    upper = float(cfg.get("upper"))
    ticker = cfg.get("ticker", "OML").lower()

    quote = fetch_quote(ticker)
    if quote is None:
        log("Nie udało się pobrać kursu — kończę bez aktualizacji stanu.")
        return 0

    log(f"Quote: {quote.symbol} close={quote.close} ({quote.date} {quote.time})  thresholds: {lower}/{upper}")

    # Zaktualizuj historię "ostatni kurs per slot" (zawsze, niezależnie od alertu)
    last_by_slot = state.setdefault("lastBySlot", {})
    if slot in SLOTS:  # tylko dla realnych slotów; pomijamy "manual"
        last_by_slot[slot] = {
            "date": quote.date,
            "time": quote.time,
            "close": quote.close,
            "open": quote.open_,
            "high": quote.high,
            "low": quote.low,
            "checkedAt": now.isoformat(),
        }

    msg = build_message(quote, lower, upper)
    if msg is None and not force:
        log("Kurs w widełkach — alert pominięty.")
        state["lastKey"] = last_key
        state["lastCheck"] = now.isoformat()
        state["lastClose"] = quote.close
        save_json(STATE_PATH, state)
        return 0

    if msg is None:
        # FORCE_NOTIFY — wyślij info bez progu
        title = f"OML {quote.close:.2f} PLN"
        body = f"Test: kurs w widełkach {lower:.2f}–{upper:.2f}. Open {quote.open_:.2f}."
        kind = "test"
    else:
        title, body, kind = msg

    payload = {
        "title": title,
        "body": body,
        "tag": f"oml-{kind}",
        "data": {"symbol": quote.symbol, "close": quote.close, "date": quote.date, "kind": kind},
    }

    try:
        send_push(sub, payload)
        log(f"Push wysłany: {title}")
    except WebPushException as e:
        log(f"WebPushException: {e}")
        if hasattr(e, "response") and e.response is not None and e.response.status_code in (404, 410):
            log("Subskrypcja wygasła — wyczyść data/subscription.json i włącz powiadomienia w PWA ponownie.")

    state["lastKey"] = last_key
    state["lastCheck"] = now.isoformat()
    state["lastClose"] = quote.close
    state["lastAlert"] = {"title": title, "body": body, "kind": kind} if msg else state.get("lastAlert")
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
