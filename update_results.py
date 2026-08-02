#!/usr/bin/env python3
"""Update finished matches in the subscribed ICS calendar."""
import json, re, sys, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ICS = Path("spartak-moscow.ics")
TEAM_ID = 2323
UA = "spartak-calendar/1.0"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()

def matches():
    out = []
    for page in (0, 1):
        url = f"https://www.sofascore.com/api/v1/team/{TEAM_ID}/events/last/{page}"
        for e in json.loads(fetch(url)).get("events", []):
            hs, aws = e.get("homeScore", {}).get("current"), e.get("awayScore", {}).get("current")
            if e.get("status", {}).get("type") == "finished" and hs is not None and aws is not None:
                out.append({"date": datetime.fromtimestamp(e["startTimestamp"], timezone.utc).date(),
                    "home": e["homeTeam"]["name"], "away": e["awayTeam"]["name"], "hs": hs, "as": aws})
    return out

ALIASES = {"fcspartakmoscow":"спартак","spartakmoscow":"спартак","akhmatgrozny":"ахмат",
"fcorenburg":"оренбург","krasnodar":"краснодар","baltikakaliningrad":"балтика",
"rubinkazan":"рубин","zenitstpetersburg":"зенит","dynamomoscow":"динамо",
"rostov":"ростов","rodinamoscow":"родина","krylyasovetovsamara":"крыльясоветов",
"cskamoscow":"цска","lokomotivmoscow":"локомотив","dynamomakhachkala":"динамомахачкала",
"fakelvoronezh":"факел","akrontolyatti":"акрон"}

def norm(s):
    key = re.sub(r"[^a-zа-я0-9]", "", s.lower().replace("ё", "е"))
    return ALIASES.get(key, key)

def unesc(s):
    return s.replace("\\,", ",").replace("\\n", "\n").replace("\\;", ";").replace("\\\\", "\\")

def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

def video(query, full=False):
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    try:
        root = ET.fromstring(fetch(url))
    except Exception as err:
        print(f"Video search skipped: {err}", file=sys.stderr); return None
    for item in root.findall(".//item"):
        title, link = (item.findtext("title") or "").lower(), item.findtext("link") or ""
        if urllib.parse.urlparse(link).hostname not in {"matchtv.ru", "www.matchtv.ru"}: continue
        if full and "_translation_" in link: return link
        if not full and "_clip_" in link and ("лучшие моменты" in title or "голы" in title): return link

def event_match(block, recent):
    dm, sm = re.search(r"^DTSTART(?:;[^:]*)?:(\d{8})", block, re.M), re.search(r"^SUMMARY:(.+)$", block, re.M)
    if not dm or not sm: return None
    pair = unesc(sm.group(1)).replace("⏳", "").split(" (", 1)[0].split(" — ", 1)
    if len(pair) != 2: return None
    date, wanted = datetime.strptime(dm.group(1), "%Y%m%d").date(), {norm(pair[0]), norm(pair[1])}
    return next((m for m in recent if abs((m["date"]-date).days) <= 1 and {norm(m["home"]),norm(m["away"])} == wanted), None)

def update(block, m):
    score = f'{m["home"]} — {m["away"]} {m["hs"]}:{m["as"]}'
    sm = re.search(r"^SUMMARY:(.+)$", block, re.M).group(1)
    sm = re.sub(r"\s+\[\d+[:–-]\d+\]$", "", sm) + f' [{m["hs"]}:{m["as"]}]'
    block = re.sub(r"^SUMMARY:.+$", "SUMMARY:"+sm, block, flags=re.M)
    dm = re.search(r"^DESCRIPTION:(.*)$", block, re.M)
    desc = unesc(dm.group(1)) if dm else ""
    desc += f"\nРезультат: {score}"
    pair = f'{m["home"]} {m["away"]}'
    review = video(f'site:matchtv.ru/football "{pair}" "Голы и лучшие моменты"')
    full = video(f'site:matchtv.ru/football "{pair}" полная трансляция', True)
    if review: desc += "\nОбзор: " + review
    if full: desc += "\nПолный матч: " + full
    block = re.sub(r"^DESCRIPTION:.*$", "DESCRIPTION:"+esc(desc), block, flags=re.M)
    return re.sub(r"^DTSTAMP:.+$", "DTSTAMP:"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), block, flags=re.M)

def main():
    original, recent, count = ICS.read_text(encoding="utf-8"), matches(), 0
    def repl(hit):
        nonlocal count
        block = hit.group(0)
        if "Результат:" in unesc(block): return block
        m = event_match(block, recent)
        if not m: return block
        count += 1; return update(block, m)
    changed = re.sub(r"BEGIN:VEVENT.*?END:VEVENT", repl, original, flags=re.S)
    if count: ICS.write_text(changed, encoding="utf-8")
    print(f"Updated events: {count}")

if __name__ == "__main__": main()
