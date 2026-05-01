"""
TotoLab draw scraper — runs as GitHub Action
Sources (in priority order):
  1. yelu.sg          — clean static HTML, SG-based
  2. lotteryextreme   — fallback static HTML
Validates draw is recent before writing.
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DRAWS_FILE  = Path('draws.json')
SGT         = timezone(timedelta(hours=8))
DRAW_DAYS   = {0, 3}   # Monday=0, Thursday=3

YELU_URL    = 'https://www.yelu.sg/lottery/results/singapore-pools-toto'
LE_URL      = 'https://www.lotteryextreme.com/singapore/toto-results'

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


def fetch_yelu():
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

    numbers    = sorted(unique[:6])
    bonus      = unique[6]
    prize_m    = re.search(r'GROUP 1 PRIZE[^\$]*\$([\d,]+)', text, re.I)
    prize_pool = int(prize_m.group(1).replace(',', '')) if prize_m else None

    return {
        'drawNo':    draw_no,
        'date':      date_str or datetime.now(SGT).strftime('%Y-%m-%d'),
        'numbers':   numbers,
        'bonus':     bonus,
        'prizePool': prize_pool,
    }


def fetch_le():
    print('Fetching lotteryextreme.com...')
    r = requests.get(LE_URL, headers=HEADERS, timeout=20)
    r.raise_for_status()

    m = re.search(r'Toto\s+(\d{2}/\d{2}/\d{4})\s+\w+\s+\((\d+)\)', r.text)
    if not m:
        raise ValueError('Could not parse draw header from lotteryextreme.com')

    dd, mm, yyyy = m.group(1).split('/')
    date_str     = f'{yyyy}-{mm}-{dd}'
    draw_no      = int(m.group(2))
    block        = r.text[m.end():m.end() + 300]
    all_nums     = [int(n) for n in re.findall(r'\*\s*(\d{1,2})', block) if 1 <= int(n) <= 49]

    if len(all_nums) < 7:
        raise ValueError(f'Only {len(all_nums)} numbers found on lotteryextreme')

    return {
        'drawNo':    draw_no,
        'date':      date_str,
        'numbers':   sorted(all_nums[:6]),
        'bonus':     all_nums[6],
        'prizePool': None,
    }


def validate(record):
    nums  = record['numbers']
    bonus = record['bonus']
    assert record['drawNo'] > 0,                         f"Invalid drawNo: {record['drawNo']}"
    assert re.match(r'\d{4}-\d{2}-\d{2}', record['date']), f"Invalid date: {record['date']}"
    assert len(nums) == 6,                               f"Expected 6 numbers, got {len(nums)}"
    assert len(set(nums)) == 6,                          f"Duplicate numbers: {nums}"
    assert all(1 <= n <= 49 for n in nums),              f"Number out of range: {nums}"
    assert 1 <= bonus <= 49,                             f"Invalid bonus: {bonus}"
    assert bonus not in nums,                            f"Bonus {bonus} duplicates winning number"


def is_recent(record):
    now_sgt     = datetime.now(SGT)
    today_sgt   = now_sgt.date()
    record_date = datetime.strptime(record['date'], '%Y-%m-%d').date()
    days_diff   = (today_sgt - record_date).days
    # Accept if within 1 day, or if today is not a draw day and within 4 days
    if days_diff <= 1:
        return True
    if today_sgt.weekday() not in DRAW_DAYS and days_diff <= 4:
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

    for fetch_fn in [fetch_yelu, fetch_le]:
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
        print(f"Draw #{record['drawNo']} ({record['date']}) is stale — sources not updated yet")
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
