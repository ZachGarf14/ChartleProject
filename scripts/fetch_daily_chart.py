#!/usr/bin/env python3
"""
Fetches the trailing-12-month daily closes for "today's" ticker and writes
a static JSON file the live site can read with zero API keys or live calls.

Run daily (see .github/workflows/fetch-daily-chart.yml). Safe to re-run —
it's idempotent for a given UTC date.

Ticker selection uses the exact same seeded-hash logic as the browser's
JS (see index.html), keyed off the UTC calendar date, so server and
client always agree on which ticker is "today's answer" without either
side needing to tell the other.
"""

import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = ROOT / "data" / "tickers.json"
OUTPUT_DIR = ROOT / "data"

MASK = 0xFFFFFFFF


# ---------------------------------------------------------------------------
# Mulberry32-derived hash, reimplemented bit-for-bit to match the JS in
# index.html. Given the same UTC date string, this MUST produce the same
# answerIdx client-side and server-side, or the puzzle desyncs.
# ---------------------------------------------------------------------------
def imul(a: int, b: int) -> int:
    return ((a & MASK) * (b & MASK)) & MASK


def hash_seed(s: str):
    h = (1779033703 ^ len(s)) & MASK
    for ch in s:
        h = imul(h ^ ord(ch), 3432918353)
        h = ((h << 13) & MASK) | (h >> 19)

    state = {"h": h}

    def rand():
        hv = state["h"]
        hv = imul(hv ^ (hv >> 16), 2246822519)
        hv = imul(hv ^ (hv >> 13), 3266489917)
        hv = (hv ^ (hv >> 16)) & MASK
        state["h"] = hv
        return hv / 4294967296

    return rand


def utc_date_str(d: datetime) -> str:
    # Canonical seed format: zero-padded ISO date, UTC. Must match
    # index.html's todayStr() exactly.
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Stooq symbol mapping. Stooq uses '-' where the standard ticker uses '.'
# (e.g. BRK.B -> brk-b.us). Add overrides here as mismatches are found —
# Stooq's coverage/naming isn't 100% consistent and this list will need
# occasional care.
# ---------------------------------------------------------------------------
STOOQ_OVERRIDES = {
    "BRK.B": "brk-b.us",
    "BF.B": "bf-b.us",
}


def stooq_symbol(ticker: str) -> str:
    if ticker in STOOQ_OVERRIDES:
        return STOOQ_OVERRIDES[ticker]
    return ticker.lower().replace(".", "-") + ".us"


def fetch_stooq_csv(ticker: str) -> str:
    sym = stooq_symbol(ticker)
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def parse_stooq_csv(csv_text: str, cutoff_date: datetime):
    """Returns list of (date_str, close_float) for rows on/after cutoff_date."""
    lines = csv_text.strip().splitlines()
    if len(lines) < 2 or not lines[0].startswith("Date"):
        raise ValueError("Unexpected Stooq CSV format — no header row found")

    rows = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        date_str, _open, _high, _low, close = parts[:5]
        try:
            row_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            close_val = float(close)
        except ValueError:
            continue
        if row_date >= cutoff_date:
            rows.append((date_str, close_val))
    return rows


def build_series(rows):
    """Normalize closes to % change from the first value in the window."""
    if not rows:
        return [], []
    base = rows[0][1]
    dates = [r[0] for r in rows]
    pct = [round((r[1] - base) / base * 100, 4) for r in rows]
    return dates, pct


def main():
    now = datetime.now(timezone.utc)
    today_str = utc_date_str(now)
    cutoff = now - timedelta(days=366)

    output_path = OUTPUT_DIR / f"{today_str}.json"
    if output_path.exists():
        print(f"{output_path} already exists — skipping (idempotent).")
        return 0

    with open(TICKERS_PATH) as f:
        tickers = json.load(f)

    rand = hash_seed(today_str)
    idx = int(rand() * len(tickers))
    answer = tickers[idx]
    ticker = answer["t"]

    print(f"Date (UTC): {today_str}")
    print(f"Selected ticker index {idx} of {len(tickers)}: {ticker} ({answer['name']})")

    try:
        csv_text = fetch_stooq_csv(ticker)
        rows = parse_stooq_csv(csv_text, cutoff)
    except (urllib.error.URLError, ValueError) as e:
        print(f"ERROR fetching/parsing {ticker}: {e}", file=sys.stderr)
        return 1

    if len(rows) < 30:
        print(f"ERROR: only got {len(rows)} rows for {ticker} — refusing to publish a thin series", file=sys.stderr)
        return 1

    dates, pct = build_series(rows)
    last_close = rows[-1][1]

    payload = {
        "date": today_str,
        "ticker": ticker,
        "dates": dates,
        "changePct": pct,
        "lastClose": round(last_close, 2),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload, f, separators=(",", ":"))

    print(f"Wrote {output_path} with {len(dates)} data points.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
