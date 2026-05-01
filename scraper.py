"""
TotoLab draw scraper — runs as GitHub Action
Sources (in priority order):
  1. magayo.com  — parses plain-text recent draws table (robust to bot detection)
  2. Manual entry fallback with clear instructions
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

DRAWS_FILE = Path('draws.json')
SGT        = timezone(timedelta(hours=8))
DRAW_DAYS  = {0, 3}  # Monday=0, Thursday=3

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

# Known draw anchors — update this list periodically
KNOWN_DRAWS = {
    '2026-04-30': 4178,
    '2026-04-27': 4177,
    '2026-04-23': 4176,
    '2026-04-20': 4175,
    '2026-04-16': 4174,
    '2026-04-13': 4173,
    '2026-04-09': 4172,
    '2026-04-06': 4171,
    '2026-04-02': 4170,
    '2026-03-30': 4169,
}

ANCHOR_NO   = 4178
ANCHOR_DATE = datetime(2026, 4, 30, tzinfo=SGT).date()


def estimate_draw_no(date_str):
    if date_str in KNOWN_DRAWS:
        return KNOWN_DRAWS[date_str]
    target = datetime.strptime(date_str, '%Y-%m-%d').date()
    delta  = (target - ANCHOR_DATE).days
    count  = 0
    step   = 1 if delta > 0 else -1
    for i in range(abs(delta)):
        d = ANCHOR_DATE + timedelta(days=(i + 1) * step)
        if d.weekday() in DRAW_DAYS:
            count += step
    return ANCHOR_NO + count


def fetch_magayo():
    """
    Parse magayo.com recent draws table — plain text section:

      27 April 2026
      Monday
      03 11 13 22 28 48 Additional 21

    This section is plain text and survives bot detection even when
    ball images are blocked.
    """
    print('Fetching magayo.com...')
    r = requests.get(MAGAYO_URL, headers=HEADERS, timeout=25)
    r.raise_for_status()
    text = r.text

    print(f'Page size: {len(text)} chars')

    # Strategy 1: Parse plain-text recent draws section
    # Look for pattern: date line, day line, number line with "Additional"
    # Example: "27 April 2026\nMonday\n03 11 13 22 28 48 Additional 21"
    pattern = re.compile(
        r'(\d{1,2}\s+\w+\s+\d{4})\s*'           # date: "27 April 2026"
        r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*'
        r'((?:\d{2}\s+){5}\d{2})\s*'             # 6 numbers: "03 11 13 22 28 48"
        r'Additional\s+(\d{2})',                  # bonus: "21"
        re.IGNORECASE
    )

    matches = pattern.findall(text)
    print(f'Found {len(matches)} draw matches in recent table')

    if matches:
        # First match = most recent draw
        date_raw, nums_raw, bonus_raw = matches[0]
        date_str = datetime.strptime(date_raw.strip(), '%d %B %Y').strftime('%Y-%m-%d')
        numbers  = sorted([int(n) for n in nums_raw.strip().split()])
        bonus    = int(bonus_raw.strip())
        draw_no  = estimate_draw_no(date_str)

        return {
            'drawNo':    draw_no,
            'date':      date_str,
            'numbers':   numbers,
            'bonus':     bonus,
            'prizePool': None,
        }

    # Strategy 2: ball image URLs (works when page is fully rendered)
    print('Trying ball image URL strategy...')
    latest_date_m = re.search(
        r'(\d{1,2}\s+\w+\s+\d{4})\s*'
        r'\((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\)',
        text
    )
    if latest_date_m:
        block_start  = latest_date_m.end()
        block_end    = text.find('Next Toto', block_start)
        block        = text[block_start: block_end if block_end != -1 else block_start + 1000]
        winning_nums = [int(m) for m in re.findall(r'p1=M&(?:amp;)?p2=(\d{2})', block)]
        bonus_nums   = [int(m) for m in re.findall(r'p1=B&(?:amp;)?p2=(\d{2})', block)]

        print(f'Ball strategy: {len(winning_nums)} winning, {len(bonus_nums)} bonus')

        if len(winning_nums) == 6 and bonus_nums:
            date_str = datetime.strptime(
                latest_date_m.group(1).strip(), '%d %B %Y'
            ).strftime('%Y-%m-%d')
            draw_no = estimate_draw_no(date_str)
            return {
                'drawNo':    draw_no,
                'date':      date_str,
                'numbers':   sorted(winning_nums),
                'bonus':     bonus_nums[0],
                'prizePool': None,
            }

    # Debug: print snippet so we can see what we got
    print('--- Page snippet (first 2000 chars) ---')
    print(text[:2000])
    print('--- End snippet ---')

    raise ValueError('Could not parse any draw data from magayo.com')


def validate(record):
    nums  = record['numbers']
    bonus = record['bonus']
    assert record['drawNo'] > 0,                            f"Invalid drawNo: {record['drawNo']}"
    assert re.match(r'\d{4}-\d{2}-\d{2}', record['date']), f"Invalid date: {record['date']}"
    assert len(nums) == 6,                                  f"Expected 6 numbers, got {len(nums)}"
    assert len(set(nums)) == 6,                             f"Duplicate numbers: {nums}"
    assert all(1 <= n <= 49 for n in nums),                 f"Number out of range: {nums}"
    assert 1 <= bonus <= 49,                                f"Invalid bonus: {bonus}"
    assert bonus not in nums,                               f"Bonus {bonus} duplicates winning number"


def is_recent(record):
    today     = datetime.now(SGT).date()
    rec_date  = datetime.strptime(record['date'], '%Y-%m-%d').date()
    days_diff = (today - rec_date).days
    if days_diff <= 1:
        return True
    if today.weekday() not in DRAW_DAYS and days_diff <= 4:
        return True
    return False


def load_draws():
    if DRAWS_FILE.exists():
        return json.loads(DRAWS_FILE.read_text())
    return []


def save_draws(draws):
    DRAWS_FILE.write_text(json.dumps(draws, indent=2))
    print(f'Saved {len(draws)} draws to {DRAWS_FILE}')


def main():
    now_sgt = datetime.now(SGT)
    print(f'Scraper started: {now_sgt.strftime("%Y-%m-%d %H:%M SGT")}')

    record = None
    for fetch_fn in [fetch_magayo]:
        try:
            record = fetch_fn()
            print(f'Result: {record}')
            break
        except Exception as e:
            print(f'{fetch_fn.__name__} failed: {e}')

    if not record:
        print('ERROR: All sources failed — check page snippet above for debug info')
        sys.exit(1)

    try:
        validate(record)
        print('Validation passed')
    except AssertionError as e:
        print(f'ERROR: Validation failed — {e}')
        sys.exit(1)

    if not is_recent(record):
        print(f"Draw #{record['drawNo']} ({record['date']}) is stale")
        print('Exiting without changes — next cron will retry')
        sys.exit(0)

    existing = load_draws()
    if existing and existing[0]['drawNo'] == record['drawNo']:
        print(f"Draw #{record['drawNo']} already recorded — no change needed")
        sys.exit(0)

    save_draws([record] + existing)
    print(f"Successfully recorded draw #{record['drawNo']} ({record['date']})")


if __name__ == '__main__':
    main()
