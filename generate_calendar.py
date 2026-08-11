from __future__ import annotations

import hashlib, json, re, unicodedata
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparser
from icalendar import Calendar, Event

OUTPUT = Path('vermont-events.ics')
VERMONT_COM = 'https://vermont.com/calendar/'
VERMONT_PUBLIC = 'https://www.vermontpublic.org/vermont-events-calendar'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/142.0.0.0 Safari/537.36'
    ),
    'Accept': (
        'text/html,application/xhtml+xml,application/xml;q=0.9,'
        'image/avif,image/webp,image/apng,*/*;q=0.8'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Cache-Control': 'no-cache',
    'Pragma': 'no-cache',
}

MONTHS = 'January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec'


def clean(v):
    return re.sub(r'\s+', ' ', v or '').strip()


def norm(v):
    v = unicodedata.normalize('NFKD', v or '')
    v = ''.join(c for c in v if not unicodedata.combining(c)).lower()
    v = re.sub(r'&', ' and ', v)
    return clean(re.sub(r'[^a-z0-9]+', ' ', v))


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    return r


def day(item):
    s = item['start']
    return s.date() if isinstance(s, datetime) else s


def parse_json_ld(soup, source, fallback_url):
    out = []
    for tag in soup.find_all('script', attrs={'type':'application/ld+json'}):
        raw = tag.string or tag.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        expanded = []
        for obj in objs:
            if isinstance(obj, dict) and isinstance(obj.get('@graph'), list):
                expanded.extend(obj['@graph'])
            else:
                expanded.append(obj)
        for obj in expanded:
            if not isinstance(obj, dict):
                continue
            types = obj.get('@type', [])
            if isinstance(types, str): types = [types]
            if not any('Event' in str(t) for t in types):
                continue
            title = clean(obj.get('name'))
            if not title or not obj.get('startDate'):
                continue
            try:
                start = dtparser.parse(str(obj['startDate']))
                if start.tzinfo: start = start.replace(tzinfo=None)
            except Exception:
                continue
            try:
                end = dtparser.parse(str(obj.get('endDate'))) if obj.get('endDate') else start + timedelta(hours=2)
                if isinstance(end, datetime) and end.tzinfo: end = end.replace(tzinfo=None)
            except Exception:
                end = start + timedelta(hours=2)
            loc = obj.get('location')
            location = ''
            if isinstance(loc, dict):
                parts = [clean(loc.get('name'))]
                addr = loc.get('address')
                if isinstance(addr, dict):
                    parts += [clean(addr.get('streetAddress')), clean(addr.get('addressLocality')), clean(addr.get('addressRegion')), clean(addr.get('postalCode'))]
                elif isinstance(addr, str):
                    parts.append(clean(addr))
                location = ', '.join(x for x in parts if x)
            elif isinstance(loc, str):
                location = clean(loc)
            desc = clean(BeautifulSoup(str(obj.get('description','')), 'html.parser').get_text(' '))
            out.append({'title':title,'start':start,'end':end,'location':location,'description':desc,'url':clean(obj.get('url')) or fallback_url,'sources':[source]})
    return out


def discover(base, path_fragment):
    soup = BeautifulSoup(get(base).text, 'html.parser')
    urls = set()
    for a in soup.find_all('a', href=True):
        u = urljoin(base, a['href']).split('?')[0].split('#')[0].rstrip('/')
        p = urlparse(u)
        if path_fragment in p.path and p.path.rstrip('/') != urlparse(base).path.rstrip('/'):
            urls.add(u)
    return sorted(urls)


def fallback_vermont_com(soup, url):
    h1 = soup.find('h1')
    title = clean(h1.get_text(' ', strip=True)) if h1 else ''
    text = clean(soup.get_text(' ', strip=True))
    m = re.search(rf'\b({MONTHS})\s+(\d{{1,2}}),\s+(20\d{{2}})\s*\|\s*(\d{{1,2}}(?::\d{{2}})?\s*(?:am|pm))', text, re.I)
    if not (title and m): return []
    try: start = dtparser.parse(f'{m.group(1)} {m.group(2)}, {m.group(3)} {m.group(4)}')
    except Exception: return []
    meta = soup.find('meta', attrs={'name':'description'})
    return [{'title':title,'start':start,'end':start+timedelta(hours=2),'location':'','description':clean(meta.get('content')) if meta and meta.get('content') else '','url':url,'sources':['Vermont.com']}]


def fetch_vermont_com():
    urls = discover(VERMONT_COM, '/calendar/')
    print(f'Vermont.com discovered {len(urls)} pages')
    out = []
    for u in urls:
        try:
            soup = BeautifulSoup(get(u).text, 'html.parser')
            items = parse_json_ld(soup, 'Vermont.com', u) or fallback_vermont_com(soup, u)
            out.extend(items)
        except Exception as exc:
            print('Vermont.com skip:', u, exc)
    print(f'Vermont.com: {len(out)} events')
    return out


def fallback_vt_public(soup, url):
    h1 = soup.find('h1')
    title = clean(h1.get_text(' ', strip=True)) if h1 else ''
    full = clean(soup.get_text(' ', strip=True))
    m = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM))\s*-\s*(\d{1,2}:\d{2}\s*(?:AM|PM)).*?\bon\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+(\d{1,2}\s+[A-Za-z]{3}\s+20\d{2})', full, re.I)
    if not (title and m): return []
    try:
        d = dtparser.parse(m.group(3)).date(); start = datetime.combine(d, dtparser.parse(m.group(1)).time()); end = datetime.combine(d, dtparser.parse(m.group(2)).time())
        if end <= start: end += timedelta(days=1)
    except Exception: return []
    lines = [clean(x) for x in soup.get_text('\n', strip=True).splitlines() if clean(x)]
    location = ''
    for i, line in enumerate(lines):
        if norm(line) == norm(title):
            for candidate in lines[i+1:i+6]:
                if re.search(r'\d{1,2}:\d{2}\s*(?:AM|PM)', candidate, re.I): break
                if not candidate.startswith('$') and len(candidate) < 120:
                    location = candidate; break
            break
    meta = soup.find('meta', attrs={'name':'description'})
    return [{'title':title,'start':start,'end':end,'location':location,'description':clean(meta.get('content')) if meta and meta.get('content') else '','url':url,'sources':['Vermont Public']}]


def fetch_vt_public():
    urls = discover(VERMONT_PUBLIC, '/vermont-events-calendar/event/')
    print(f'Vermont Public discovered {len(urls)} pages')
    out = []
    for u in urls:
        try:
            soup = BeautifulSoup(get(u).text, 'html.parser')
            out.extend(parse_json_ld(soup, 'Vermont Public', u) or fallback_vt_public(soup, u))
        except Exception as exc:
            print('Vermont Public skip:', u, exc)
    print(f'Vermont Public: {len(out)} events')
    return out


def dup(a,b):
    if day(a) != day(b): return False
    ts = SequenceMatcher(None, norm(a['title']), norm(b['title'])).ratio()
    la, lb = norm(a.get('location','')), norm(b.get('location',''))
    ls = 0.65 if not la or not lb else (1.0 if la in lb or lb in la else SequenceMatcher(None,la,lb).ratio())
    return ts >= .92 or (ts >= .80 and ls >= .78)


def dedupe(items):
    items.sort(key=lambda x:(day(x), norm(x['title'])))
    kept=[]; n=0
    for item in items:
        match=None
        for ex in reversed(kept):
            if day(item) != day(ex):
                if day(item) > day(ex): break
                continue
            if dup(ex,item): match=ex; break
        if match:
            n += 1
            if isinstance(item['start'], datetime) and not isinstance(match['start'], datetime): match['start'], match['end'] = item['start'], item['end']
            if len(clean(item.get('location'))) > len(clean(match.get('location'))): match['location'] = item['location']
            if len(clean(item.get('description'))) > len(clean(match.get('description'))): match['description'] = item['description']
            for s in item['sources']:
                if s not in match['sources']: match['sources'].append(s)
        else:
            kept.append(item)
    print(f'Deduplicated {n} overlaps')
    return kept


def build(items):
    cal = Calendar(); cal.add('prodid','-//Combined Vermont Events Calendar//EN'); cal.add('version','2.0'); cal.add('x-wr-calname','Vermont Events'); cal.add('x-wr-timezone','America/New_York')
    now = datetime.utcnow()
    for item in items:
        ev = Event(); uid = hashlib.sha256(f"{day(item)}|{norm(item['title'])}|{norm(item.get('location',''))}".encode()).hexdigest()[:30]+'@vermont-events'
        ev.add('uid',uid); ev.add('dtstamp',now); ev.add('summary',item['title']); ev.add('dtstart',item['start']); ev.add('dtend',item['end'])
        if item.get('location'): ev.add('location',item['location'])
        if item.get('url'): ev.add('url',item['url'])
        desc = clean(item.get('description','')); note='Sources: '+', '.join(item['sources']); ev.add('description', f'{desc}\n\n{note}' if desc else note)
        cal.add_component(ev)
    OUTPUT.write_bytes(cal.to_ical()); print(f'Wrote {OUTPUT} with {len(items)} unique events')


def main():
    all_items=[]
    
    for name, fn in [
        ('Vermont.com', fetch_vermont_com),
        ('Vermont Public', fetch_vt_public),
    ]:
        try: all_items.extend(fn())
        except Exception as exc: print(f'ERROR loading {name}: {exc}')
    if not all_items: raise RuntimeError('No events collected from any source')
    unique=dedupe(all_items)
    if len(unique)<10: raise RuntimeError(f'Only {len(unique)} unique events generated; refusing to publish a bad feed')
    build(unique)

if __name__=='__main__': main()
