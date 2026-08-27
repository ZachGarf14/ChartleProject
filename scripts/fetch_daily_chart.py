#!/usr/bin/env python3
"""
Fetches the trailing-12-month daily closes for "today's" ticker and writes
a static JSON file the live site can read with zero API keys or live calls
on the client side.

Run daily (see .github/workflows/fetch-daily-chart.yml). Safe to re-run —
it's idempotent for a given UTC date.

Ticker selection uses the exact same seeded-hash logic as the browser's
JS (see index.html), keyed off the puzzle date (rolls over 3:00 AM ET,
not UTC midnight — see puzzle_date_str() below), so server and
client always agree on which ticker is "today's answer" without either
side needing to tell the other.

Data source: Twelve Data's /time_series endpoint (free tier — 800
requests/day, this script makes exactly 1/day). Requires an API key,
passed via the TWELVEDATA_API_KEY environment variable (see README for
how to get one and add it as a repo secret).

History: this originally used Stooq's anonymous CSV download, then
briefly Alpha Vantage. Stooq began gating cloud/CI IP ranges (like
GitHub Actions runners) behind a CAPTCHA-only API key that can't be
obtained or renewed headlessly. Alpha Vantage's free tier turned out to
cap daily-history requests at the last 100 data points (~4-5 months) —
"outputsize=full" is premium-only there — which is too thin for a
"trailing 12 months" chart. Twelve Data's free tier doesn't have that
restriction for daily-interval data (up to 5,000 points/request), so it
covers a full year comfortably within a single free-tier call.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TICKERS_PATH = ROOT / "data" / "tickers.json"
OUTPUT_DIR = ROOT / "data"

MASK = 0xFFFFFFFF

API_KEY_ENV_VAR = "TWELVEDATA_API_KEY"
TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"

# Requested daily bars — comfortably more than ~252 trading days/year to
# leave headroom for holidays/gaps, well within Twelve Data's free-tier
# 5,000-points-per-request cap.
OUTPUT_SIZE = 300


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


def puzzle_date_str(now_utc: datetime) -> str:
    """Puzzle "day" rolls over at 3:00 AM US Eastern Time, not UTC midnight
    — matches index.html's todayStrPuzzle() exactly, via zoneinfo (stdlib,
    correctly tracks EDT/EST) instead of a fixed offset that would drift
    twice a year. Both sides MUST agree, or the puzzle desyncs.
    """
    et_now = now_utc.astimezone(ZoneInfo("America/New_York"))
    shifted = et_now - timedelta(hours=3)
    return shifted.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Twelve Data fetch/parse. Ticker symbols match what's already in
# tickers.json as-is (e.g. "BRK.B", "BF.B") — no conversion table needed.
# ---------------------------------------------------------------------------
def fetch_twelve_data_json(ticker: str, api_key: str) -> dict:
    params = urllib.parse.urlencode({
        "symbol": ticker,
        "interval": "1day",
        "outputsize": OUTPUT_SIZE,
        "apikey": api_key,
    })
    url = f"{TWELVE_DATA_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_twelve_data(payload: dict, cutoff_date: datetime):
    """Returns list of (date_str, close_float) for rows on/after cutoff_date,
    in ascending chronological order.

    Twelve Data returns HTTP 200 even for errors (bad symbol, rate limit,
    invalid key) — the failure shows up as {"status": "error", ...} in the
    body instead of an HTTP-level error, so that's checked explicitly.
    """
    if payload.get("status") == "error":
        raise ValueError(payload.get("message", "Twelve Data returned an error with no message"))

    values = payload.get("values")
    if not values:
        raise ValueError(f"Unexpected Twelve Data response — no 'values' array found: {json.dumps(payload)[:300]}")

    rows = []
    for v in values:
        date_str = v.get("datetime", "")[:10]  # tolerate either date or datetime strings
        try:
            row_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            close_val = float(v["close"])
        except (ValueError, KeyError, TypeError):
            continue
        if row_date >= cutoff_date:
            rows.append((date_str, close_val))

    rows.sort(key=lambda r: r[0])  # ascending — Twelve Data gives newest-first
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
    api_key = os.environ.get(API_KEY_ENV_VAR)
    if not api_key:
        print(f"ERROR: {API_KEY_ENV_VAR} environment variable is not set.", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    today_str = puzzle_date_str(now)
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
        payload = fetch_twelve_data_json(ticker, api_key)
        rows = parse_twelve_data(payload, cutoff)
    except (urllib.error.URLError, ValueError) as e:
        print(f"ERROR fetching/parsing {ticker}: {e}", file=sys.stderr)
        return 1

    if len(rows) < 30:
        print(f"ERROR: only got {len(rows)} rows for {ticker} — refusing to publish a thin series", file=sys.stderr)
        return 1

    dates, pct = build_series(rows)
    last_close = rows[-1][1]

    payload_out = {
        "date": today_str,
        "ticker": ticker,
        "dates": dates,
        "changePct": pct,
        "lastClose": round(last_close, 2),
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(payload_out, f, separators=(",", ":"))

    print(f"Wrote {output_path} with {len(dates)} data points.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
