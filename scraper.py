"""
TotoLab draw scraper — runs as GitHub Action
Sources (in priority order):
  1. magayo.com  — clean static HTML, confirmed accessible from GH Actions
  2. yelu.sg     — fallback
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
YELU_URL   = 'https://www.yelu.sg/lottery/results/singapore-pools-toto'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-SG,en;q=0.9',
    'Referer': 'https://www.google.com.sg/',
}

# Known draw number anchor for cross-referencing (update periodically)
# Draw #4178 = 30 April 2026
KNOWN_DRAWS = {
    '2026-04-30': 4178,
    '2026-04-27': 4177,
    '2026-04-23': 4176,
    '2026-04-20': 4175,
    '2026-04-16': 4174,
    '2026-04-13': 4173,
}

ANCHOR_NO   = 4178
ANCHOR_DATE = datetime(2026, 4, 30, tzinfo=SGT).date()


def estimate_draw_no(date_str):
    """Estimate draw number from date using anchor + Mon/Thu count."""
    target = datetime.strptime(date_str, '%Y-%m-%d').date()
    if date_str in KNOWN_DRAWS:
        return KNOWN_DRAWS[date_str]
    delta = (target - ANCHOR_DATE).days
    count = 0
    step  = 1 if delta > 0 else -1
    for i in range(abs(delta)):
        d = ANCHOR_DATE + timedelta(days=(i + 1) * step)
        if d.weekday() in DRAW_DAYS:
            count += step
    return ANCHOR_NO + count


def fetch_magayo():
    """
    Parse magayo.com Toto results page.
    Latest draw block:
      30 April 2026 (Thursday)
      [ball images with p2=02][p2=06]...[p2=39]
      Additional [p2=15]

    Recent draws listed as:
      27 April 2026
      Monday
      03 11 13 22 28 48 Additional 21
    """
    print('Fetching magayo.com...')
    r = requests.get(MAGAYO_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    text = r.text

    # ── Latest draw: extract from ball image URLs ──────────────────────────
    # Pattern: show_ball.php?p1=M&p2=02 for winning, p1=B for bonus
    # Find date of latest draw
    latest_date_m = re.search(
        r'(\d{1,2}\s+\w+\s+\d{4})\s*\((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\)',
        text
    )
    if not latest_date_m:
        raise ValueError('Could not find latest draw date on magayo.com')

    latest_date_str_raw = latest_date_m.group(1)
    try:
        latest_date = datetime.strptime(latest_date_str_raw, '%d %B %Y').strftime('%Y-%m-%d')
    except ValueError:
        raise ValueError(f'Could not parse date: {latest_date_str_raw}')

    # Extract ball numbers from image URLs in the block after the date
    block_start = latest_date_m.end()
    block_end   = text.find('Next Toto', block_start)
    if block_end == -1:
        block_end = block_start + 1000
    block = text[block_start:block_end]

    winning_nums = [int(m) for m in re.findall(r'p1=M&p2=(\d{2})', block)]
    bonus_nums   = [int(m) for m in re.findall(r'p1=B&p2=(\d{2})', block)]

    if len(winning_nums) != 6:
        raise ValueError(f'Expected 6 winning numbers, found {len(winning_nums)}')
    if not bonus_nums:
        raise ValueError('Could not find bonus number')

    draw_no = estimate_draw_no(latest_date)

    return {
        'drawNo':    draw_no,
        'date':      latest_date,
        'numbers':   sorted(winning_nums),
        'bonus':     bonus_nums[0],
        'prizePool': None,
    }


def fetch_yelu():
    """Parse yelu.sg results page as fallback."""
    print('Fetching yelu.sg...')
    r = requests.get(YELU_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()
    text = r.text

    draw_m = re.search(r'#(\d{4})', text)
    if not draw_m:
        raise ValueError('Could not find draw number on yelu.sg')
    draw_no = int(draw_m.group(1))

    date_m   = re.search(r'(\d{1,2}\s+\w+,\s+\d{4})', text)
    date_str = None
    if date_m:
        try:
            date_str = datetime.strptime(date_m.group(1), '%d %B, %Y').strftime('%Y-%m-%d')
        except ValueError:
            pass

    win_start = text.find('TOTO Winning Numbers')
    win_end   = text.find('GROUP 1', win_start)
    if win_start == -1 or win_end == -1:
        raise ValueError('Could not find winning numbers block on yelu.sg')

    block    = text[win_start:win_end]
    all_nums = [int(n) for n in re.findall(r'\b(\d{1,2})\b', block) if 1 <= int(n) <= 49]

    seen, unique = set(), []
    for n in all_nums:
        if n not in seen:
            seen.add(n)
            unique.append(n)

    if len(unique) < 7:
        raise ValueError(f'Only {len(unique)} numbers found on yelu.sg')

    prize_m    = re.search(r'GROUP 1 PRIZE[^\$]*\$([\d,]+)', text, re.I)
    prize_pool = int(prize_m.group(1).replace(',', '')) if prize_m else None

    return {
        'drawNo':    draw_no,
        'date':      date_str or datetime.now(SGT).strftime('%Y-%m-%d'),
        'numbers':   sorted(unique[:6]),
        'bonus':     unique[6],
        'prizePool': prize_pool,
    }


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
    # Not a draw day today — accept up to 4 days old
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
    for fetch_fn in [fetch_magayo, fetch_yelu]:
        try:
            record = fetch_fn()
            print(f'Result: {record}')
            break
        except Exception as e:
            print(f'{fetch_fn.__name__} failed: {e}')

    if not record:
        print('ERROR: All sources failed')
        sys.exit(1)

    try:
        validate(record)
        print('Validation passed')
    except AssertionError as e:
        print(f'ERROR: Validation failed — {e}')
        sys.exit(1)

    if not is_recent(record):
        print(f"Draw #{record['drawNo']} ({record['date']}) is stale — not yet updated")
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
