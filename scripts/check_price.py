#!/usr/bin/env python3
"""
Multi-ticker price checker — odpalany przez GitHub Actions.

- Czyta data/config.json: lista tickerów, każdy z własnym harmonogramem,
  progami, walutą, źródłem (stooq), opcją weekdaysOnly.
- Dla każdego tickera: jeśli aktualny czas Europe/Warsaw mieści się w jednym
  z jego slotów (z tolerancją 30 min) — pobiera kurs i ewentualnie wysyła push.
- Scheduled run może też nadrobić ostatni pominięty slot z bieżącego dnia,
  jeśli GitHub opóźni lub pominie cron.
- Workflow_dispatch (FORCE_NOTIFY=true) backfilluje najnowszy minięty slot dziś.
- Stan zapisywany do data/last_run.json pod kluczem ticker.id.

Wymagane zmienne środowiskowe (GitHub Secrets):
  VAPID_PRIVATE_KEY  — PEM
  VAPID_PUBLIC_KEY   — URL-safe base64 (informacyjnie)
  VAPID_SUBJECT      — np. mailto:owner@example.com
  FORCE_NOTIFY       — "true" wymusza wysyłkę dla testów (workflow_dispatch)
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
TOLERANCE_MINUTES = 30  # GitHub Actions throttluje cron — daj zapas
HISTORY_DAYS = 5


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
    print(f"[checker] {msg}", flush=True)


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


def normalize_config(cfg: dict) -> list[dict]:
    """Zwraca listę tickerów. Wspiera nowy (tickers: [...]) i stary (płaski) schemat."""
    if isinstance(cfg, dict) and isinstance(cfg.get("tickers"), list):
        return cfg["tickers"]
    # Stary, płaski schemat → wbuduj jednoelementową listę
    return [{
        "id": "oml",
        "name": cfg.get("name", "OML"),
        "ticker": cfg.get("ticker", "OML"),
        "source": cfg.get("source", "stooq"),
        "currency": cfg.get("currency", "PLN"),
        "lower": cfg.get("lower"),
        "upper": cfg.get("upper"),
        "schedule": cfg.get("schedule", ["09:30", "14:30", "17:30"]),
        "weekdaysOnly": cfg.get("weekdaysOnly", True),
    }]


def slot_for_ticker(now: datetime, t: dict) -> str | None:
    if t.get("weekdaysOnly", True) and now.weekday() >= 5:
        return None
    for slot in t.get("schedule", []):
        try:
            hh, mm = map(int, slot.split(":"))
        except ValueError:
            continue
        s = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        e = s + timedelta(minutes=TOLERANCE_MINUTES)
        if s <= now < e:
            return slot
    return None


def latest_past_slot_for_ticker(now: datetime, t: dict) -> str | None:
    if t.get("weekdaysOnly", True) and now.weekday() >= 5:
        return None
    past = []
    for slot in t.get("schedule", []):
        try:
            hh, mm = map(int, slot.split(":"))
        except ValueError:
            continue
        s = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if s <= now:
            past.append((s, slot))
    if not past:
        return None
    return sorted(past)[-1][1]


def latest_unprocessed_past_slot_for_ticker(now: datetime, t: dict, ts: dict) -> str | None:
    if t.get("weekdaysOnly", True) and now.weekday() >= 5:
        return None
    today = now.date().isoformat()
    last_by_slot = ts.get("lastBySlot", {})
    past = []
    for slot in t.get("schedule", []):
        try:
            hh, mm = map(int, slot.split(":"))
        except ValueError:
            continue
        s = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if s > now:
            continue
        entry = last_by_slot.get(slot)
        if entry and entry.get("checkedAt", "").startswith(today):
            continue
        past.append((s, slot))
    if not past:
        return None
    return sorted(past)[-1][1]


def fetch_quote_stooq(ticker: str) -> Quote | None:
    url = f"https://stooq.com/q/l/?s={ticker.lower()}&f=sd2t2ohlcv&h&e=csv"
    log(f"GET {url}")
    r = requests.get(url, timeout=15, headers={"User-Agent": "multi-alert/1.0"})
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        log(f"Brak danych dla {ticker}")
        return None
    row = rows[0]
    if (row.get("Close") or "").upper() in ("N/D", "", "N/A"):
        log(f"Stooq nie zwrócił close dla {ticker}: {row}")
        return None

    def f(x):
        try: return float(x)
        except (TypeError, ValueError): return float("nan")

    def i(x):
        try: return int(x)
        except (TypeError, ValueError): return 0

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


def fetch_quote(t: dict) -> Quote | None:
    src = (t.get("source") or "stooq").lower()
    sym = t.get("ticker", t.get("id", "")).strip()
    if src == "stooq":
        return fetch_quote_stooq(sym)
    log(f"Nieznane źródło: {src!r}")
    return None


def send_push(subscription: dict, payload: dict) -> None:
    priv = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    sub_claim = os.environ.get("VAPID_SUBJECT", "mailto:owner@example.com").strip()
    if not priv:
        raise RuntimeError("Brak VAPID_PRIVATE_KEY w zmiennych środowiskowych.")

    import tempfile
    fd, pem_path = tempfile.mkstemp(suffix=".pem", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(priv if priv.endswith("\n") else priv + "\n")
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=pem_path,
            vapid_claims={"sub": sub_claim},
            ttl=3600,
        )
    finally:
        try: os.unlink(pem_path)
        except OSError: pass


def fmt_price(value: float, currency: str) -> str:
    # USD ma 2 miejsca, PLN ma 2 miejsca, BTC w USD wyświetlamy bez groszy dla czytelności
    if currency.upper() == "USD" and value >= 1000:
        return f"{value:,.0f} {currency}".replace(",", " ")
    return f"{value:.2f} {currency}"


def update_history(ts: dict, slot: str, quote: Quote, now: datetime) -> None:
    today = now.date().isoformat()
    points = ts.setdefault("historyPoints", [])
    key = f"{today}@{slot}"
    point = {
        "key": key,
        "day": today,
        "slot": slot,
        "checkedAt": now.isoformat(),
        "marketDate": quote.date,
        "marketTime": quote.time,
        "close": quote.close,
        "open": quote.open_,
        "high": quote.high,
        "low": quote.low,
    }
    points = [p for p in points if p.get("key") != key]
    points.append(point)
    points.sort(key=lambda p: (p.get("day", ""), p.get("slot", ""), p.get("checkedAt", "")))

    days = []
    for p in reversed(points):
        day = p.get("day")
        if day and day not in days:
            days.append(day)
    keep_days = set(reversed(days[:HISTORY_DAYS]))
    ts["historyPoints"] = [p for p in points if p.get("day") in keep_days]


def build_message(t: dict, q: Quote) -> tuple[str, str, str] | None:
    """(title, body, kind) jeśli alert się należy, inaczej None."""
    name = t.get("name") or t.get("id") or t.get("ticker", "?")
    short = (t.get("id") or t.get("ticker") or "").upper()
    cur = t.get("currency", "")
    lower = float(t.get("lower"))
    upper = float(t.get("upper"))

    if q.close < lower:
        title = f"{short} ↓ {fmt_price(q.close, cur)}"
        body = f"{name}: spadek poniżej {fmt_price(lower, cur)}. Open {fmt_price(q.open_, cur)}, dzień: {fmt_price(q.low, cur)}–{fmt_price(q.high, cur)}."
        return title, body, "below"
    if q.close > upper:
        title = f"{short} ↑ {fmt_price(q.close, cur)}"
        body = f"{name}: wzrost powyżej {fmt_price(upper, cur)}. Open {fmt_price(q.open_, cur)}, dzień: {fmt_price(q.low, cur)}–{fmt_price(q.high, cur)}."
        return title, body, "above"
    return None


def process_ticker(t: dict, now: datetime, backfill: bool, test_push: bool,
                   subscription: dict | None, state: dict,
                   catch_up_missed_slots: bool) -> None:
    tid = t.get("id") or t.get("ticker", "?")
    tickers_state = state.setdefault("tickers", {})
    ts = tickers_state.setdefault(tid, {"lastBySlot": {}})
    slot = slot_for_ticker(now, t)
    if slot is None and backfill:
        slot = latest_past_slot_for_ticker(now, t)
    if slot is None and catch_up_missed_slots:
        slot = latest_unprocessed_past_slot_for_ticker(now, t, ts)
    log(f"[{tid}] slot={slot}")
    if slot is None:
        return

    today = now.date().isoformat()
    last_key = f"{today}@{slot}"
    if ts.get("lastKey") == last_key and not (backfill or test_push):
        log(f"[{tid}] slot {last_key} już obsłużony")
        return

    quote = fetch_quote(t)
    if quote is None:
        log(f"[{tid}] brak kursu, pomijam")
        return

    log(f"[{tid}] {quote.symbol} close={quote.close} ({quote.date} {quote.time})  thresholds: {t.get('lower')}/{t.get('upper')}")

    # Zapisz historię per slot
    ts.setdefault("lastBySlot", {})[slot] = {
        "date": quote.date,
        "time": quote.time,
        "close": quote.close,
        "open": quote.open_,
        "high": quote.high,
        "low": quote.low,
        "checkedAt": now.isoformat(),
    }
    update_history(ts, slot, quote, now)
    ts["lastKey"] = last_key
    ts["lastCheck"] = now.isoformat()
    ts["lastClose"] = quote.close

    msg = build_message(t, quote)
    should_push = msg is not None or (test_push and subscription)
    if not should_push:
        log(f"[{tid}] kurs w widełkach — push pominięty")
        return

    if msg is None:
        # FORCE — wyślij neutralną wiadomość bez progu
        cur = t.get("currency", "")
        short = (t.get("id") or t.get("ticker") or "").upper()
        title = f"{short} {fmt_price(quote.close, cur)}"
        body = f"Test: kurs w widełkach {fmt_price(float(t['lower']), cur)}–{fmt_price(float(t['upper']), cur)}."
        kind = "test"
    else:
        title, body, kind = msg

    if not subscription:
        log(f"[{tid}] brak subskrypcji — push pominięty (alert byłby: {title})")
        return

    payload = {
        "title": title,
        "body": body,
        "tag": f"{tid}-{kind}",
        "data": {"ticker": tid, "symbol": quote.symbol, "close": quote.close, "kind": kind},
    }
    try:
        send_push(subscription, payload)
        log(f"[{tid}] Push wysłany: {title}")
        ts["lastAlert"] = {"title": title, "body": body, "kind": kind, "at": now.isoformat()}
    except WebPushException as e:
        log(f"[{tid}] WebPushException: {e}")
        if hasattr(e, "response") and e.response is not None and e.response.status_code in (404, 410):
            log("Subskrypcja wygasła — wyczyść data/subscription.json i włącz powiadomienia w PWA ponownie.")


def main() -> int:
    now = datetime.now(tz=WARSAW)
    # Backward-compat: FORCE_NOTIFY=true → backfill + test_push
    legacy_force = os.environ.get("FORCE_NOTIFY", "false").lower() == "true"
    backfill = legacy_force or os.environ.get("BACKFILL", "false").lower() == "true"
    test_push = legacy_force or os.environ.get("TEST_PUSH", "false").lower() == "true"
    catch_up_missed_slots = os.environ.get("CATCH_UP_MISSED_SLOTS", "false").lower() == "true"
    log(
        f"Warsaw now: {now.isoformat()}  backfill={backfill}  "
        f"test_push={test_push}  catch_up_missed_slots={catch_up_missed_slots}"
    )

    cfg = load_json(CFG_PATH, None)
    if not cfg:
        log("Brak data/config.json — kończę.")
        return 0
    tickers = normalize_config(cfg)
    if not tickers:
        log("Konfig nie zawiera żadnego tickera.")
        return 0

    sub = load_json(SUB_PATH, None)
    if sub and not sub.get("endpoint"):
        sub = None
    if not sub:
        log("Brak subscription.json — push się nie wyśle, ale historia będzie zapisana.")

    state = load_json(STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}

    for t in tickers:
        try:
            process_ticker(t, now, backfill, test_push, sub, state, catch_up_missed_slots)
        except Exception as e:
            log(f"[{t.get('id', '?')}] BŁĄD: {e}")

    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
