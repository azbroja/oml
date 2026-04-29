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
import re
import sys
from statistics import median
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from pywebpush import webpush, WebPushException

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CFG_PATH = DATA / "config.json"
SUB_PATH = DATA / "subscription.json"
STATE_PATH = DATA / "last_run.json"
MICRO_PATH = DATA / "market_micro.json"

WARSAW = ZoneInfo("Europe/Warsaw")
TOLERANCE_MINUTES = 30  # GitHub Actions throttluje cron — daj zapas
HISTORY_DAYS = 5
MICRO_TIMEOUT = 20
MICRO_BOOK_LEVELS = 12
MICRO_TRADES_LIMIT = 25
MICRO_DELAY_MINUTES = 15
MICRO_HISTORY_POINTS = 72
MICRO_VOLUME_SPIKE_MULTIPLIER = 2.5
MICRO_VOLUME_SPIKE_MIN = 1500
MICRO_SOURCES = {
    "oml": {
        "symbol": "OML",
        "book_url": "https://gragieldowa.pl/spolka_arkusz_zl/spolka/oml",
        "trades_url": "https://gragieldowa.pl/spolka_transakcje/spolka/oml",
    }
}


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


@dataclass
class MicroTrade:
    time: str
    price: float
    volume: int
    timestamp: str


@dataclass
class MicroLevel:
    price: float
    volume: int
    value: float
    orders: int


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


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def parse_pl_float(text: str) -> float:
    cleaned = normalize_ws(text).replace(" ", "").replace(",", ".")
    return float(cleaned)


def parse_pl_int(text: str) -> int:
    cleaned = normalize_ws(text).replace(" ", "").replace(",", "")
    return int(cleaned)


def fetch_html(url: str) -> BeautifulSoup:
    log(f"GET {url}")
    r = requests.get(url, timeout=MICRO_TIMEOUT, headers={"User-Agent": "multi-alert/1.0"})
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def text_lines(text: str) -> list[str]:
    return [normalize_ws(line) for line in text.splitlines() if normalize_ws(line)]


def parse_book_table(soup: BeautifulSoup, table_id: str) -> list[MicroLevel]:
    table = soup.find("table", id=table_id)
    if table is None:
        return []

    levels: list[MicroLevel] = []
    for row in table.select("tbody tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        try:
            levels.append(MicroLevel(
                price=parse_pl_float(cells[0].get_text(" ", strip=True)),
                volume=parse_pl_int(cells[1].get("data-text") or cells[1].get_text(" ", strip=True)),
                value=parse_pl_float(cells[2].get_text(" ", strip=True)),
                orders=int(normalize_ws(cells[3].get_text(" ", strip=True))),
            ))
        except (TypeError, ValueError):
            continue
        if len(levels) >= MICRO_BOOK_LEVELS:
            break
    return levels


def parse_book_updated_at_from_soup(soup: BeautifulSoup, now: datetime) -> datetime:
    text = soup.get_text("\n")
    lines = text_lines(text)
    return parse_book_updated_at(lines, now)


def parse_trades_table(soup: BeautifulSoup, trade_date) -> list[MicroTrade]:
    table = None
    for candidate in soup.find_all("table", class_="data maintable"):
        headers = [normalize_ws(th.get_text(" ", strip=True)) for th in candidate.find_all("th")]
        if {"Czas", "Kurs", "Wolumen"}.issubset(set(headers)):
            table = candidate
            break
    if table is None:
        return []

    trades: list[MicroTrade] = []
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) != 4:
            continue
        time = normalize_ws(cells[1].get_text(" ", strip=True))
        price_raw = normalize_ws(cells[2].get_text(" ", strip=True))
        volume_raw = normalize_ws(cells[3].get_text(" ", strip=True))
        if not re.match(r"^\d{2}:\d{2}:\d{2}$", time):
            continue
        try:
            dt = datetime.strptime(f"{trade_date.isoformat()} {time}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=WARSAW)
            trades.append(MicroTrade(
                time=time,
                price=float(price_raw),
                volume=int(volume_raw),
                timestamp=dt.isoformat(),
            ))
        except ValueError:
            continue
    trades.sort(key=lambda t: t.timestamp)
    return trades


def parse_book_updated_at(lines: list[str], now: datetime) -> datetime:
    for line in lines:
        m = re.search(r"Ostatnia aktualizacja:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", line)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=WARSAW)
    return now


def parse_book_side(lines: list[str], marker: str, stop_marker: str | None) -> list[MicroLevel]:
    started = False
    levels: list[MicroLevel] = []
    pattern = re.compile(r"^(\d+[.,]\d+)\s+([\d ]+)\s+([\d ]+,\d+)\s+(\d+)\s+[\d.,]+\s*%$")

    for line in lines:
        if not started:
            if marker in line:
                started = True
            continue
        if stop_marker and stop_marker in line:
            break
        m = pattern.match(line)
        if not m:
            continue
        levels.append(MicroLevel(
            price=parse_pl_float(m.group(1)),
            volume=parse_pl_int(m.group(2)),
            value=parse_pl_float(m.group(3)),
            orders=int(m.group(4)),
        ))
        if len(levels) >= MICRO_BOOK_LEVELS:
            break
    return levels


def parse_trade_date(lines: list[str], now: datetime) -> datetime.date:
    for line in lines:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", line)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    return now.date()


def parse_trades(lines: list[str], trade_date) -> list[MicroTrade]:
    trades: list[MicroTrade] = []
    pattern = re.compile(r"^\d+\s+(\d{2}:\d{2}:\d{2})\s+(\d+[.,]\d+)\s+([\d ]+)$")

    for line in lines:
        m = pattern.match(line)
        if not m:
            continue
        dt = datetime.strptime(f"{trade_date.isoformat()} {m.group(1)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=WARSAW)
        trades.append(MicroTrade(
            time=m.group(1),
            price=parse_pl_float(m.group(2)),
            volume=parse_pl_int(m.group(3)),
            timestamp=dt.isoformat(),
        ))
    trades.sort(key=lambda t: t.timestamp)
    return trades


def level_to_dict(level: MicroLevel) -> dict:
    return {
        "price": level.price,
        "volume": level.volume,
        "value": level.value,
        "orders": level.orders,
    }


def trade_to_dict(trade: MicroTrade) -> dict:
    return {
        "time": trade.time,
        "price": trade.price,
        "volume": trade.volume,
        "timestamp": trade.timestamp,
    }


def summarize_pressure(trades: list[MicroTrade], latest_dt: datetime, window: timedelta) -> dict:
    window_start = latest_dt - window
    recent = [t for t in trades if datetime.fromisoformat(t.timestamp) >= window_start]

    uptick_volume = 0
    downtick_volume = 0
    flat_volume = 0
    previous_price = None
    last_direction = 0

    for trade in recent:
        if previous_price is None:
            flat_volume += trade.volume
        elif trade.price > previous_price:
            uptick_volume += trade.volume
            last_direction = 1
        elif trade.price < previous_price:
            downtick_volume += trade.volume
            last_direction = -1
        else:
            if last_direction > 0:
                uptick_volume += trade.volume
            elif last_direction < 0:
                downtick_volume += trade.volume
            else:
                flat_volume += trade.volume
        previous_price = trade.price

    total_volume = sum(t.volume for t in recent)
    trade_count = len(recent)
    avg_trade_size = (total_volume / trade_count) if trade_count else 0.0
    dominance = 0.0
    directional_total = uptick_volume + downtick_volume
    if directional_total > 0:
        dominance = (uptick_volume - downtick_volume) / directional_total

    return {
        "windowStart": window_start.isoformat(),
        "windowEnd": latest_dt.isoformat(),
        "tradeCount": trade_count,
        "totalVolume": total_volume,
        "avgTradeSize": round(avg_trade_size, 2),
        "uptickVolume": uptick_volume,
        "downtickVolume": downtick_volume,
        "flatVolume": flat_volume,
        "dominance": round(dominance, 4),
        "cumulativeDelta": uptick_volume - downtick_volume,
        "latestTrades": [trade_to_dict(t) for t in recent[-MICRO_TRADES_LIMIT:]][::-1],
    }


def build_book_wall_signal(side: str, levels: list[MicroLevel]) -> dict | None:
    if len(levels) < 3:
        return None
    volumes = [level.volume for level in levels]
    baseline = median(volumes)
    strongest = max(levels, key=lambda level: level.volume)
    if baseline <= 0 or strongest.volume < baseline * 3:
        return None
    return {
        "side": side,
        "price": strongest.price,
        "volume": strongest.volume,
        "orders": strongest.orders,
        "strengthVsMedian": round(strongest.volume / baseline, 2),
        "message": f"{side.upper()} wall detected at {strongest.price:.2f}",
    }


def build_volume_spike_signal(current_volume: int, prior_volumes: list[int]) -> dict | None:
    prior = [v for v in prior_volumes if v > 0]
    if len(prior) < 3 or current_volume < MICRO_VOLUME_SPIKE_MIN:
        return None
    baseline = median(prior)
    if baseline <= 0:
        return None
    ratio = current_volume / baseline
    if ratio < MICRO_VOLUME_SPIKE_MULTIPLIER:
        return None
    return {
        "currentVolume": current_volume,
        "baselineVolume": round(baseline, 2),
        "ratio": round(ratio, 2),
        "message": "abnormal volume detected",
    }


def snapshot_from_entry(entry: dict) -> dict:
    metrics = entry.get("metrics", {})
    signals = entry.get("signals", {})
    book_wall = signals.get("bookWall") or {}
    volume_spike = signals.get("volumeSpike") or {}
    return {
        "scrapedAt": entry.get("scrapedAt"),
        "updatedAt": entry.get("updatedAt"),
        "orderBookImbalance": metrics.get("orderBookImbalance"),
        "pressureDominance5m": metrics.get("pressureDominance5m"),
        "cumulativeDelta5m": metrics.get("cumulativeDelta5m"),
        "totalVolume5m": metrics.get("totalVolume5m"),
        "tradeCount5m": metrics.get("tradeCount5m"),
        "bookWallSide": book_wall.get("side"),
        "bookWallPrice": book_wall.get("price"),
        "volumeSpikeRatio": volume_spike.get("ratio"),
    }


def merge_micro_history(previous: dict | None, entry: dict) -> list[dict]:
    history = previous.get("snapshots", []) if isinstance(previous, dict) else []
    history = [point for point in history if point.get("scrapedAt") != entry.get("scrapedAt")]
    history.append(snapshot_from_entry(entry))
    history.sort(key=lambda point: point.get("scrapedAt", ""))
    return history[-MICRO_HISTORY_POINTS:]


def build_micro_entry(ticker_id: str, now: datetime, previous: dict | None = None) -> dict:
    src = MICRO_SOURCES[ticker_id]
    book_soup = fetch_html(src["book_url"])
    trades_soup = fetch_html(src["trades_url"])

    updated_at = parse_book_updated_at_from_soup(book_soup, now)
    bids = parse_book_table(book_soup, "arkusz_left")
    asks = parse_book_table(book_soup, "arkusz_right")
    if not bids or not asks:
        raise RuntimeError(f"Nie udało się sparsować arkusza zleceń dla {ticker_id}")

    trade_lines = text_lines(trades_soup.get_text("\n"))
    trade_date = parse_trade_date(trade_lines, updated_at)
    trades = parse_trades_table(trades_soup, trade_date)
    if not trades:
        raise RuntimeError(f"Nie udało się sparsować transakcji dla {ticker_id}")

    latest_trade_dt = datetime.fromisoformat(trades[-1].timestamp)
    pressure_1h = summarize_pressure(trades, latest_trade_dt, timedelta(hours=1))
    pressure_5m = summarize_pressure(trades, latest_trade_dt, timedelta(minutes=5))

    bid_volume_top = sum(l.volume for l in bids)
    ask_volume_top = sum(l.volume for l in asks)
    imbalance = 0.0
    if bid_volume_top + ask_volume_top > 0:
        imbalance = (bid_volume_top - ask_volume_top) / (bid_volume_top + ask_volume_top)

    best_bid = bids[0].price
    best_ask = asks[0].price
    book_wall = build_book_wall_signal("bid", bids) or build_book_wall_signal("ask", asks)

    provisional_entry = {
        "symbol": src["symbol"],
        "source": "gragieldowa.pl",
        "scrapedAt": now.isoformat(),
        "marketDate": trade_date.isoformat(),
        "updatedAt": updated_at.isoformat(),
        "delayMinutes": MICRO_DELAY_MINUTES,
        "book": {
            "bids": [level_to_dict(level) for level in bids],
            "asks": [level_to_dict(level) for level in asks],
            "topBid": best_bid,
            "topAsk": best_ask,
            "spread": round(best_ask - best_bid, 4),
        },
        "trades": {
            "latest": [trade_to_dict(t) for t in trades[-MICRO_TRADES_LIMIT:]][::-1],
            "latestTimestamp": trades[-1].timestamp,
        },
        "metrics": {
            "orderBookImbalance": round(imbalance, 4),
            "bidVolumeTop": bid_volume_top,
            "askVolumeTop": ask_volume_top,
            "tradeCount1h": pressure_1h["tradeCount"],
            "avgTradeSize1h": pressure_1h["avgTradeSize"],
            "uptickVolume1h": pressure_1h["uptickVolume"],
            "downtickVolume1h": pressure_1h["downtickVolume"],
            "flatVolume1h": pressure_1h["flatVolume"],
            "pressureDominance1h": pressure_1h["dominance"],
            "totalVolume1h": pressure_1h["totalVolume"],
            "tradeCount5m": pressure_5m["tradeCount"],
            "avgTradeSize5m": pressure_5m["avgTradeSize"],
            "uptickVolume5m": pressure_5m["uptickVolume"],
            "downtickVolume5m": pressure_5m["downtickVolume"],
            "pressureDominance5m": pressure_5m["dominance"],
            "totalVolume5m": pressure_5m["totalVolume"],
            "cumulativeDelta5m": pressure_5m["cumulativeDelta"],
        },
        "pressure1h": pressure_1h,
        "pressure5m": pressure_5m,
        "signals": {
            "bookWall": book_wall,
            "volumeSpike": None,
            "rollingImbalance5m": None,
        },
    }

    snapshots = merge_micro_history(previous, provisional_entry)
    previous_5m_volumes = [int(point.get("totalVolume5m") or 0) for point in snapshots[:-1]]
    volume_spike = build_volume_spike_signal(pressure_5m["totalVolume"], previous_5m_volumes)
    recent_snapshots = [
        point for point in snapshots
        if point.get("scrapedAt")
        and datetime.fromisoformat(point["scrapedAt"]) >= now - timedelta(minutes=25)
    ]
    rolling_imbalance = {
        "points": len(recent_snapshots),
        "sum": round(sum(float(point.get("orderBookImbalance") or 0.0) for point in recent_snapshots), 4),
        "avg": round(
            sum(float(point.get("orderBookImbalance") or 0.0) for point in recent_snapshots) / max(len(recent_snapshots), 1),
            4,
        ),
        "cumulativeDelta": sum(int(point.get("cumulativeDelta5m") or 0) for point in recent_snapshots),
    }
    if recent_snapshots:
        rolling_imbalance["message"] = (
            "buy pressure trend"
            if rolling_imbalance["sum"] > 0
            else "sell pressure trend"
            if rolling_imbalance["sum"] < 0
            else "neutral pressure trend"
        )

    provisional_entry["signals"]["volumeSpike"] = volume_spike
    provisional_entry["signals"]["rollingImbalance5m"] = rolling_imbalance
    provisional_entry["snapshots"] = snapshots
    return provisional_entry


def refresh_market_micro(now: datetime) -> None:
    current = load_json(MICRO_PATH, {})
    if not isinstance(current, dict):
        current = {}
    tickers = current.setdefault("tickers", {})

    for ticker_id in MICRO_SOURCES:
        try:
            previous = tickers.get(ticker_id)
            tickers[ticker_id] = build_micro_entry(ticker_id, now, previous)
            log(f"[{ticker_id}] market micro updated")
        except Exception as e:
            log(f"[{ticker_id}] market micro WARN: {e}")
            prev = tickers.get(ticker_id, {})
            if isinstance(prev, dict):
                prev["lastError"] = {"message": str(e), "at": now.isoformat()}
                tickers[ticker_id] = prev

    current["updatedAt"] = now.isoformat()
    save_json(MICRO_PATH, current)


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
    refresh_market_micro(now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
