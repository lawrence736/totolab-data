"""
TotoLab draw scraper — runs as a GitHub Action.

Source: magayo.com (Singapore Pools is JS-rendered + Cloudflare; never scrape it).

Each run:
  1. Reads the latest draw (ball-image section) AND the ~30-draw recent list
     (plain text once HTML tags are stripped).
  2. Inserts every draw missing from draws.json — so a skipped cron, a
     Friday cascade draw or a slow source never leaves a permanent gap.
  3. Assigns draw numbers from draws already in the file (magayo shows no
     draw numbers), and refuses to write if that mapping disagrees with
     any existing entry.
"""

import html
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

DRAWS_FILE = Path('draws.json')
SGT        = timezone(timedelta(hours=8))
DRAW_DAYS  = {0, 3}  # Mon, Thu — only used by the last-resort estimator

MAGAYO_URL = 'https://www.magayo.com/lotto/singapore/toto-results/'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-SG,en;q=0.9',
    'Referer': 'https://www.google.com.sg/',
    'Cache-Control': 'no-cache',
}

WEEKDAYS = r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'

# Last-resort anchor, used only if draws.json shares no date with magayo
ANCHOR_NO   = 4178
ANCHOR_DATE = datetime(2026, 4, 30, tzinfo=SGT).date()


# ── Parsing ──────────────────────────────────────────────────────────────

def to_iso(date_raw):
    return datetime.strptime(date_raw.strip(), '%d %B %Y').strftime('%Y-%m-%d')


def parse_recent_list(raw_html):
    """'5 October 2026  Monday  03 13 20 30 48 49 Additional 23' — after tag strip."""
    text = html.unescape(re.sub(r'<[^>]+>', ' ', raw_html))
    pattern = re.compile(
        r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s+' + WEEKDAYS + r'\s+'
        r'((?:\d{1,2}\s+){5}\d{1,2})\s+Additional\s+(\d{1,2})',
        re.IGNORECASE,
    )
    out = []
    for date_raw, nums_raw, bonus_raw in pattern.findall(text):
        try:
            out.append({
                'date':    to_iso(date_raw),
                'numbers': sorted(int(n) for n in nums_raw.split()),
                'bonus':   int(bonus_raw),
            })
        except ValueError:
            continue
    return out


def parse_latest(raw_html):
    """Latest-result block: '8 October 2026 (Thursday)' followed by ball images."""
    m = re.search(r'(\d{1,2}\s+[A-Za-z]+\s+\d{4})\s*\(' + WEEKDAYS + r'\)', raw_html)
    if not m:
        return None
    end   = raw_html.find('Next Toto', m.end())
    block = raw_html[m.end(): end if end != -1 else m.end() + 1500]
    main  = [int(x) for x in re.findall(r'p1=M&(?:amp;)?p2=(\d{1,2})', block)]
    bonus = [int(x) for x in re.findall(r'p1=B&(?:amp;)?p2=(\d{1,2})', block)]
    print(f'Latest block: {len(main)} winning, {len(bonus)} bonus')
    if len(main) != 6 or not bonus:
        return None
    return {'date': to_iso(m.group(1)), 'numbers': sorted(main), 'bonus': bonus[0]}


def fetch_magayo():
    print('Fetching magayo.com...')
    r = requests.get(MAGAYO_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    raw = r.text
    print(f'Page size: {len(raw)} chars')

    by_date = {}
    for rec in parse_recent_list(raw):
        by_date.setdefault(rec['date'], rec)
    print(f'Recent list: {len(by_date)} draws')

    latest = parse_latest(raw)
    if latest:
        by_date[latest['date']] = latest
        print(f"Latest: {latest['date']}")

    if not by_date:
        print('--- Page snippet (first 2000 chars) ---')
        print(raw[:2000])
        print('--- End snippet ---')
        raise ValueError('Could not parse any draw data from magayo.com')

    # Oldest → newest
    return sorted(by_date.values(), key=lambda r: r['date'])


# ── Draw numbering ───────────────────────────────────────────────────────

def estimate_draw_no(date_str):
    """Counts Mon/Thu from the anchor. Wrong across irregular draws — fallback only."""
    target = datetime.strptime(date_str, '%Y-%m-%d').date()
    delta  = (target - ANCHOR_DATE).days
    step   = 1 if delta > 0 else -1
    count  = 0
    for i in range(abs(delta)):
        if (ANCHOR_DATE + timedelta(days=(i + 1) * step)).weekday() in DRAW_DAYS:
            count += step
    return ANCHOR_NO + count


def assign_draw_numbers(scraped, existing):
    """
    magayo lists consecutive draws, so draw numbers follow list position.
    Anchor on the newest date present in both lists, then verify every
    other overlapping date maps to the same number already on file.
    """
    known = {d['date']: d['drawNo'] for d in existing}
    dates = [r['date'] for r in scraped]
    shared = [i for i, d in enumerate(dates) if d in known]

    if shared:
        a = shared[-1]
        base = known[dates[a]] - a
        for i in shared:
            if known[dates[i]] != base + i:
                raise ValueError(
                    f'Numbering mismatch on {dates[i]}: file has #{known[dates[i]]}, '
                    f'list position implies #{base + i}. magayo list may have a hole — '
                    f'not writing.'
                )
        for i, rec in enumerate(scraped):
            rec['drawNo'] = base + i
        print(f'Numbered from anchor {dates[a]} = #{known[dates[a]]}')
    else:
        print('WARNING: no overlap with draws.json — using weekday estimator')
        for rec in scraped:
            rec['drawNo'] = estimate_draw_no(rec['date'])


# ── Validation / IO ──────────────────────────────────────────────────────

def validate(record):
    nums, bonus = record['numbers'], record['bonus']
    assert record['drawNo'] > 0,                            f"Invalid drawNo: {record['drawNo']}"
    assert re.match(r'\d{4}-\d{2}-\d{2}$', record['date']), f"Invalid date: {record['date']}"
    assert len(nums) == 6,                                  f"Expected 6 numbers, got {len(nums)}"
    assert len(set(nums)) == 6,                             f"Duplicate numbers: {nums}"
    assert all(1 <= n <= 49 for n in nums),                 f"Number out of range: {nums}"
    assert 1 <= bonus <= 49,                                f"Invalid bonus: {bonus}"
    assert bonus not in nums,                               f"Bonus {bonus} duplicates winning number"


def load_draws():
    return json.loads(DRAWS_FILE.read_text()) if DRAWS_FILE.exists() else []


def save_draws(draws):
    DRAWS_FILE.write_text(json.dumps(draws, indent=2))
    print(f'Saved {len(draws)} draws to {DRAWS_FILE}')


def find_gaps(draws):
    nos = {d['drawNo'] for d in draws}
    return sorted(set(range(min(nos), max(nos) + 1)) - nos) if nos else []


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    print(f'Scraper started: {datetime.now(SGT):%Y-%m-%d %H:%M} SGT')

    try:
        scraped = fetch_magayo()
    except Exception as e:
        print(f'ERROR: fetch failed — {e}')
        sys.exit(1)

    existing = load_draws()
    try:
        assign_draw_numbers(scraped, existing)
    except ValueError as e:
        print(f'ERROR: {e}')
        sys.exit(1)

    by_no = {d['drawNo']: d for d in existing}
    added = []
    for rec in scraped:
        try:
            validate(rec)
        except AssertionError as e:
            print(f"Skipping {rec['date']}: {e}")
            continue
        current = by_no.get(rec['drawNo'])
        if current:
            if (current['numbers'], current['bonus']) != (rec['numbers'], rec['bonus']):
                print(f"WARNING: #{rec['drawNo']} differs from magayo — file kept. "
                      f"file={current['numbers']}+{current['bonus']} "
                      f"magayo={rec['numbers']}+{rec['bonus']}")
            continue
        by_no[rec['drawNo']] = {
            'drawNo': rec['drawNo'], 'date': rec['date'],
            'numbers': rec['numbers'], 'bonus': rec['bonus'], 'prizePool': None,
        }
        added.append(rec['drawNo'])

    merged = sorted(by_no.values(), key=lambda d: -d['drawNo'])
    gaps = find_gaps(merged)
    if gaps:
        print(f'NOTE: gaps older than magayo\'s window remain: {gaps}')

    if not added:
        print(f"No new draws. Latest on file: #{merged[0]['drawNo']} ({merged[0]['date']})")
        sys.exit(0)

    save_draws(merged)
    print(f'Added draws: {sorted(added)}')


if __name__ == '__main__':
    main()
