#!/usr/bin/env python3
"""Update Spartak Moscow fixtures and results in the subscribed ICS calendar.

Sources:
- Championat tournament calendars for structured RPL/Cup fixtures and scores.
- RFS Cup pages as an official confirmation layer for Cup results.

Safety:
- future schedule changes need two identical observations;
- finished Cup results confirmed by RFS may be applied immediately;
- existing UIDs are preserved;
- the generated ICS is validated before writing.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ICS_PATH = Path("spartak-moscow.ics")
STATE_PATH = Path(".github/calendar-state.json")
MOSCOW = ZoneInfo("Europe/Moscow")
SEASON_START = date(2026, 7, 1)
SEASON_END = date(2027, 6, 30)

RPL_URLS = (
    "https://www.championat.com/football/_russiapl/tournament/7096/calendar/",
    "https://www.championat.ru/football/_russiapl/tournament/7096/calendar/",
)
CUP_URLS = (
    "https://www.championat.com/football/_russiacup/tournament/7094/calendar/",
    "https://www.championat.ru/football/_russiacup/tournament/7094/calendar/",
)
RFS_CUP_PAST_URL = (
    "https://www.rfs.ru/cup/tournament/matches?"
    "TournamentMatchesFilter%5Bdate%5D=before"
)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

TEAM_NAMES = (
    "Крылья Советов", "Динамо Махачкала", "Локомотив М", "Спартак М",
    "Динамо Мх", "Динамо М", "Краснодар", "Оренбург", "Балтика",
    "Родина", "Зенит", "Рубин", "Ростов", "Факел", "Ахмат", "Акрон", "ЦСКА",
)
TEAM_PATTERN = "|".join(re.escape(name) for name in sorted(TEAM_NAMES, key=len, reverse=True))

CANONICAL = {
    "Спартак М": "spartak", "Спартак": "spartak", "Оренбург": "orenburg",
    "Рубин": "rubin", "Краснодар": "krasnodar", "Зенит": "zenit",
    "Ахмат": "akhmat", "Родина": "rodina", "Балтика": "baltika",
    "Динамо М": "dynamo", "Динамо": "dynamo", "Динамо Мх": "dynamo_makhachkala",
    "Динамо Махачкала": "dynamo_makhachkala", "Локомотив М": "lokomotiv",
    "Локомотив": "lokomotiv", "ЦСКА": "cska", "Ростов": "rostov",
    "Акрон": "akron", "Крылья Советов": "krylya", "Факел": "fakel",
}
DISPLAY = {
    "spartak": "Спартак", "orenburg": "Оренбург", "rubin": "Рубин",
    "krasnodar": "Краснодар", "zenit": "Зенит", "akhmat": "Ахмат",
    "rodina": "Родина", "baltika": "Балтика", "dynamo": "Динамо",
    "dynamo_makhachkala": "Динамо Махачкала", "lokomotiv": "Локомотив",
    "cska": "ЦСКА", "rostov": "Ростов", "akron": "Акрон",
    "krylya": "Крылья Советов", "fakel": "Факел",
}
ALIASES = {
    "spartak": ("спартак", "спартакм", "спартакмосква"),
    "orenburg": ("оренбург",), "rubin": ("рубин",),
    "krasnodar": ("краснодар",), "zenit": ("зенит",), "akhmat": ("ахмат",),
    "rodina": ("родина",), "baltika": ("балтика",),
    "dynamo_makhachkala": ("динамомх", "динамомахачкала"),
    "dynamo": ("динамом", "динамомосква", "динамо"),
    "lokomotiv": ("локомотив", "локомотивм"), "cska": ("цска",),
    "rostov": ("ростов",), "akron": ("акрон",),
    "krylya": ("крыльясоветов",), "fakel": ("факел",),
}
MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5,
    "июня": 6, "июля": 7, "августа": 8, "сентября": 9, "октября": 10,
    "ноября": 11, "декабря": 12,
}


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        elif tag in {"br", "p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1
        elif tag in {"p", "div", "tr", "td", "li", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", html.unescape(" ".join(self.parts))).strip()


def compact(value: str) -> str:
    return re.sub(r"[^а-яa-z0-9]+", "", value.lower().replace("ё", "е"))


def canonical_team(value: str) -> str:
    if value in CANONICAL:
        return CANONICAL[value]
    key = compact(value)
    candidates: list[tuple[int, str]] = []
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if alias and alias in key:
                candidates.append((len(alias), canonical))
    return max(candidates)[1] if candidates else key


def fetch_text(urls: tuple[str, ...] | list[str] | str) -> tuple[str, str]:
    if isinstance(urls, str):
        urls = (urls,)
    errors: list[str] = []
    for url in urls:
        for attempt in range(3):
            try:
                request = urllib.request.Request(url, headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
                    "Cache-Control": "no-cache",
                })
                with urllib.request.urlopen(request, timeout=35) as response:
                    raw = response.read()
                    encoding = response.headers.get_content_charset() or "utf-8"
                    return raw.decode(encoding, errors="replace"), response.geturl()
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
    raise RuntimeError("Не удалось загрузить источник: " + " | ".join(errors[-4:]))


def visible_text(raw_html: str) -> str:
    parser = VisibleTextParser()
    parser.feed(raw_html)
    return parser.text()


def parse_championat_calendar(urls: tuple[str, ...], competition: str) -> list[dict[str, Any]]:
    raw, resolved_url = fetch_text(urls)
    text = visible_text(raw)
    if "Спартак М" not in text:
        raise RuntimeError(f"Страница {resolved_url} не содержит календарь Спартака")
    row_pattern = re.compile(
        rf"Тур\s+(?P<round>\d+)\s+"
        rf"(?P<date>\d{{2}}\.\d{{2}}\.\d{{4}})\s+"
        rf"(?P<time>\d{{2}}:\d{{2}})\s+"
        rf"(?P<home>{TEAM_PATTERN})\s+[–—-]\s+"
        rf"(?P<away>{TEAM_PATTERN})\s+"
        rf"(?P<home_score>\d+|[–—-])\s*:\s*"
        rf"(?P<away_score>\d+|[–—-])"
        rf"(?:\s+(?P<pen_home>\d+)\s*:\s*(?P<pen_away>\d+))?"
    )
    events: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in row_pattern.finditer(text):
        home_raw, away_raw = match.group("home"), match.group("away")
        home_key, away_key = canonical_team(home_raw), canonical_team(away_raw)
        if "spartak" not in {home_key, away_key}:
            continue
        start = datetime.strptime(
            f'{match.group("date")} {match.group("time")}', "%d.%m.%Y %H:%M"
        ).replace(tzinfo=MOSCOW)
        if not (SEASON_START <= start.date() <= SEASON_END):
            continue
        score_home, score_away = match.group("home_score"), match.group("away_score")
        finished = score_home.isdigit() and score_away.isdigit()
        source_id = f"championat-{competition}-r{match.group('round')}-{home_key}-{away_key}"
        if source_id in seen:
            continue
        seen.add(source_id)
        events.append({
            "id": source_id, "start": start, "home_key": home_key, "away_key": away_key,
            "home_name": DISPLAY.get(home_key, home_raw.replace(" М", "")),
            "away_name": DISPLAY.get(away_key, away_raw.replace(" М", "")),
            "competition": competition, "round": int(match.group("round")),
            "status": "finished" if finished else "scheduled",
            "score_home": int(score_home) if finished else None,
            "score_away": int(score_away) if finished else None,
            "pen_home": int(match.group("pen_home")) if match.group("pen_home") else None,
            "pen_away": int(match.group("pen_away")) if match.group("pen_away") else None,
            "source_url": resolved_url, "official_confirmed": False,
        })
    if not events:
        raise RuntimeError(f"Не удалось распознать матчи Спартака на {resolved_url}")
    return events


def parse_rfs_cup_results() -> set[tuple[date, str, str, int, int]]:
    try:
        raw, _ = fetch_text(RFS_CUP_PAST_URL)
    except Exception as exc:
        print(f"РФС недоступен, подтверждение результатов отложено: {exc}", file=sys.stderr)
        return set()
    parser = VisibleTextParser()
    parser.feed(raw)
    tokens = [
        re.sub(r"\s+", " ", html.unescape(token)).strip()
        for token in parser.parts
        if re.sub(r"\s+", " ", html.unescape(token)).strip()
    ]
    exact_date: date | None = None
    results: set[tuple[date, str, str, int, int]] = set()
    date_re = re.compile(r"^(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(2026|2027)$", re.I)
    score_re = re.compile(r"^(\d+)\s*:\s*(\d+)(?:\s+\d+\s*:\s*\d+)?(?:\s*ТП)?$")
    for index, token in enumerate(tokens):
        date_match = date_re.match(token)
        if date_match:
            exact_date = date(int(date_match.group(3)), MONTHS[date_match.group(2).lower()], int(date_match.group(1)))
            continue
        score_match = score_re.match(token)
        if not score_match or exact_date is None or index == 0 or index + 1 >= len(tokens):
            continue
        home_key, away_key = canonical_team(tokens[index - 1]), canonical_team(tokens[index + 1])
        if "spartak" not in {home_key, away_key} or home_key not in DISPLAY or away_key not in DISPLAY:
            continue
        results.add((exact_date, home_key, away_key, int(score_match.group(1)), int(score_match.group(2))))
    return results


def mark_official_cup_results(events: list[dict[str, Any]], official: set[tuple[date, str, str, int, int]]) -> None:
    for event in events:
        if event["competition"] != "cup" or event["status"] != "finished":
            continue
        signature = (event["start"].date(), event["home_key"], event["away_key"], event["score_home"], event["score_away"])
        event["official_confirmed"] = signature in official
        if event["official_confirmed"]:
            event["source_url"] = RFS_CUP_PAST_URL


def event_fingerprint(event: dict[str, Any]) -> str:
    payload: dict[str, Any] = {}
    for key in ("start", "home_key", "away_key", "competition", "round", "status", "score_home", "score_away", "pen_home", "pen_away"):
        value = event.get(key)
        payload[key] = value.isoformat() if isinstance(value, datetime) else value
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 2, "events": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 2, "events": {}}
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        return {"version": 2, "events": {}}
    return state


def update_state(state: dict[str, Any], events: list[dict[str, Any]]) -> tuple[dict[str, Any], set[str]]:
    entries = state.setdefault("events", {})
    stable: set[str] = set()
    active_ids = {event["id"] for event in events}
    now = datetime.now(timezone.utc)
    for event in events:
        source_id, fingerprint = event["id"], event_fingerprint(event)
        previous = entries.get(source_id) or {}
        observations = min(int(previous.get("observations", 0)) + 1, 2) if previous.get("fingerprint") == fingerprint else 1
        entries[source_id] = {"fingerprint": fingerprint, "observations": observations, "last_seen": now.isoformat(timespec="seconds")}
        if event.get("official_confirmed") or observations >= 2:
            stable.add(source_id)
    for source_id in list(entries):
        if source_id not in active_ids and entries[source_id].get("last_seen"):
            try:
                if now - datetime.fromisoformat(entries[source_id]["last_seen"]) > timedelta(days=60):
                    del entries[source_id]
            except Exception:
                pass
    state["version"] = 2
    return state, stable


@dataclass
class ExistingEvent:
    block: str
    uid: str
    summary: str
    description: str
    location: str
    url: str
    dtstart_line: str
    dtend_line: str
    start_date: date | None
    end_date: date | None
    sequence: int
    status: str
    transp: str
    source_id: str | None
    opponent: str | None
    competition: str | None


def property_value(block: str, name: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(name)}(?:;[^:]*)?:(.*)$", block, flags=re.M)
    return match.group(1).strip() if match else default


def unescape_ics(value: str) -> str:
    return value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def escape_ics(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def parse_date_line(line: str) -> date | None:
    match = re.search(r"(\d{8})", line)
    return datetime.strptime(match.group(1), "%Y%m%d").date() if match else None


def detect_competition(text: str, uid: str) -> str | None:
    lowered = f"{text} {uid}".lower()
    if "supercup" in lowered or "суперкуб" in lowered: return "supercup"
    if "cup" in lowered or "кубок" in lowered: return "cup"
    if "rpl" in lowered or "рпл" in lowered or "премьер" in lowered: return "rpl"
    return None


def detect_opponent(text: str) -> str | None:
    key = compact(text)
    candidates: list[tuple[int, str]] = []
    for canonical, aliases in ALIASES.items():
        if canonical == "spartak": continue
        for alias in aliases:
            if alias in key: candidates.append((len(alias), canonical))
    return max(candidates)[1] if candidates else None


def parse_existing_events(calendar_text: str) -> list[ExistingEvent]:
    events: list[ExistingEvent] = []
    for match in re.finditer(r"BEGIN:VEVENT\n.*?\nEND:VEVENT", calendar_text, flags=re.S):
        block = match.group(0)
        uid = property_value(block, "UID")
        summary = unescape_ics(property_value(block, "SUMMARY"))
        description = unescape_ics(property_value(block, "DESCRIPTION"))
        dtstart = re.search(r"^DTSTART(?:;[^:]*)?:.*$", block, flags=re.M)
        dtend = re.search(r"^DTEND(?:;[^:]*)?:.*$", block, flags=re.M)
        source_id = property_value(block, "X-SOURCE-ID") or None
        events.append(ExistingEvent(
            block=block, uid=uid, summary=summary, description=description,
            location=unescape_ics(property_value(block, "LOCATION")), url=property_value(block, "URL"),
            dtstart_line=dtstart.group(0) if dtstart else "", dtend_line=dtend.group(0) if dtend else "",
            start_date=parse_date_line(dtstart.group(0) if dtstart else ""), end_date=parse_date_line(dtend.group(0) if dtend else ""),
            sequence=int(property_value(block, "SEQUENCE", "0") or 0), status=property_value(block, "STATUS", "CONFIRMED"),
            transp=property_value(block, "TRANSP", "OPAQUE"), source_id=source_id,
            opponent=detect_opponent(summary), competition=detect_competition(summary + " " + description, uid),
        ))
    return events


def source_opponent(event: dict[str, Any]) -> str:
    return event["away_key"] if event["home_key"] == "spartak" else event["home_key"]


def match_existing(source: dict[str, Any], existing: list[ExistingEvent], used: set[str]) -> ExistingEvent | None:
    for item in existing:
        if item.uid not in used and item.source_id == source["id"]:
            return item
    opponent, source_date = source_opponent(source), source["start"].date()
    candidates: list[tuple[int, ExistingEvent]] = []
    for item in existing:
        if item.uid in used or item.opponent != opponent or item.competition != source["competition"] or not item.start_date:
            continue
        if item.dtstart_line.startswith("DTSTART;VALUE=DATE"):
            end = item.end_date or item.start_date
            if item.start_date - timedelta(days=1) <= source_date <= end + timedelta(days=1):
                candidates.append((0, item))
        else:
            distance = abs((item.start_date - source_date).days)
            if distance <= 5: candidates.append((distance, item))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def competition_label(code: str) -> str:
    return {"rpl": "РПЛ", "cup": "Кубок России", "supercup": "Суперкубок России"}[code]


def competition_full(code: str) -> str:
    return {"rpl": "Альфа-Банк Российская Премьер-Лига 2026/27", "cup": "FONBET Кубок России 2026/27", "supercup": "OLIMPBET Суперкубок России 2026"}[code]


def duration_for(code: str) -> timedelta:
    return timedelta(minutes=135 if code == "rpl" else 150)


def media_lines(description: str) -> list[str]:
    output: list[str] = []
    for label in ("Видеообзор", "Обзор", "Полный матч", "Полная запись"):
        for match in re.finditer(rf"{label}:\s*(https?://\S+)", description):
            line = f"{label}: {match.group(1).rstrip('.,;')}"
            if line not in output: output.append(line)
    return output


def result_phrase(event: dict[str, Any]) -> str:
    home_score, away_score = event["score_home"], event["score_away"]
    spartak_score = home_score if event["home_key"] == "spartak" else away_score
    opponent_score = away_score if event["home_key"] == "spartak" else home_score
    outcome = "победа Спартака" if spartak_score > opponent_score else "поражение Спартака" if spartak_score < opponent_score else "ничья"
    return f"Результат: {outcome} {spartak_score}:{opponent_score}."


def desired_fields(event: dict[str, Any], old: ExistingEvent | None) -> dict[str, Any]:
    start, finished = event["start"], event["status"] == "finished"
    end = start + duration_for(event["competition"])
    if finished:
        summary = f'{event["home_name"]} {event["score_home"]}:{event["score_away"]} {event["away_name"]} ({competition_label(event["competition"])})'
        if event["pen_home"] is not None and event["pen_away"] is not None:
            summary += f' ({event["pen_home"]}:{event["pen_away"]} пен.)'
    else:
        summary = f'{event["home_name"]} — {event["away_name"]} ({competition_label(event["competition"])})'
    relation = "Домашний матч." if event["home_key"] == "spartak" else "Выездной матч."
    base = f'{competition_full(event["competition"])}, {event["round"]}-й тур. {relation}'
    base += " " + result_phrase(event) if finished else " Время московское."
    existing_media = media_lines(old.description) if old else []
    for line in existing_media: base += " " + line
    base += f' Источник: {event["source_url"]}'
    if old and old.location and "уточняется" not in old.location.lower(): location = old.location
    elif event["home_key"] == "spartak": location = "Лукойл Арена, Волоколамское шоссе, 69, Москва"
    else: location = old.location if old and old.location else "Место проведения уточняется"
    url = old.url if old and old.url else event["source_url"]
    if finished and existing_media:
        media_match = re.search(r"https?://\S+", existing_media[0])
        if media_match: url = media_match.group(0).rstrip(".,;")
    return {
        "summary": summary, "description": base, "location": location, "url": url,
        "dtstart": f"DTSTART;TZID=Europe/Moscow:{start.strftime('%Y%m%dT%H%M%S')}",
        "dtend": f"DTEND;TZID=Europe/Moscow:{end.strftime('%Y%m%dT%H%M%S')}",
        "status": "CONFIRMED", "transp": "TRANSPARENT" if finished else "OPAQUE",
        "alarms": not finished and start > datetime.now(MOSCOW), "source_id": event["id"],
    }


def existing_signature(item: ExistingEvent) -> dict[str, Any]:
    return {"summary": item.summary, "description": item.description, "location": item.location, "url": item.url,
            "dtstart": item.dtstart_line, "dtend": item.dtend_line, "status": item.status, "transp": item.transp,
            "alarms": "BEGIN:VALARM" in item.block, "source_id": item.source_id}


def render_event(uid: str, sequence: int, fields: dict[str, Any], stamp: str) -> str:
    lines = ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}", f"SEQUENCE:{sequence}", f"LAST-MODIFIED:{stamp}",
             fields["dtstart"], fields["dtend"], "SUMMARY:" + escape_ics(fields["summary"]),
             "LOCATION:" + escape_ics(fields["location"]), "DESCRIPTION:" + escape_ics(fields["description"]),
             "URL:" + fields["url"], "X-SOURCE-ID:" + fields["source_id"], "STATUS:" + fields["status"],
             "TRANSP:" + fields["transp"]]
    if fields["alarms"]:
        lines.extend(["BEGIN:VALARM", "TRIGGER:-PT1H", "ACTION:DISPLAY", "DESCRIPTION:Матч Спартака начнётся через 1 час", "END:VALARM",
                      "BEGIN:VALARM", "TRIGGER:-PT5M", "ACTION:DISPLAY", "DESCRIPTION:Матч Спартака начнётся через 5 минут", "END:VALARM"])
    lines.append("END:VEVENT")
    return "\n".join(lines)


def new_uid(event: dict[str, Any]) -> str:
    return f'{event["start"].strftime("%Y%m%d")}-spartak-{source_opponent(event).replace("_", "-")}-{event["competition"]}@spartak-calendar'


def apply_events(original: str, existing: list[ExistingEvent], events: list[dict[str, Any]], stable_ids: set[str]) -> tuple[str, int]:
    used: set[str] = set()
    replacements: list[tuple[str, str]] = []
    additions: list[str] = []
    changed = 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for event in sorted(events, key=lambda item: item["start"]):
        if event["id"] not in stable_ids: continue
        old = match_existing(event, existing, used)
        if old: used.add(old.uid)
        fields = desired_fields(event, old)
        if old and existing_signature(old) == fields: continue
        block = render_event(old.uid if old else new_uid(event), old.sequence + 1 if old else 0, fields, stamp)
        replacements.append((old.block, block)) if old else additions.append(block)
        changed += 1
    updated = original
    for old_block, new_block in replacements: updated = updated.replace(old_block, new_block, 1)
    if additions: updated = updated.replace("END:VCALENDAR", "\n".join(additions) + "\nEND:VCALENDAR")
    return updated, changed


def validate_calendar(text: str) -> None:
    if not text.startswith("BEGIN:VCALENDAR") or not text.rstrip().endswith("END:VCALENDAR"):
        raise ValueError("Некорректные границы VCALENDAR")
    if text.count("BEGIN:VEVENT") != text.count("END:VEVENT"):
        raise ValueError("Количество BEGIN:VEVENT и END:VEVENT не совпадает")
    events = parse_existing_events(text)
    uids = [item.uid for item in events]
    if any(not uid for uid in uids): raise ValueError("Найдено событие без UID")
    duplicates = sorted({uid for uid in uids if uids.count(uid) > 1})
    if duplicates: raise ValueError("Дублирующиеся UID: " + ", ".join(duplicates))
    source_ids = [item.source_id for item in events if item.source_id]
    duplicate_sources = sorted({sid for sid in source_ids if source_ids.count(sid) > 1})
    if duplicate_sources: raise ValueError("Дублирующиеся X-SOURCE-ID: " + ", ".join(duplicate_sources))
    for item in events:
        if not item.summary or not item.dtstart_line: raise ValueError(f"Событие {item.uid} без SUMMARY или DTSTART")


def main() -> int:
    if not ICS_PATH.exists(): raise FileNotFoundError(f"Не найден {ICS_PATH}")
    rpl = parse_championat_calendar(RPL_URLS, "rpl")
    cup = parse_championat_calendar(CUP_URLS, "cup")
    official_cup = parse_rfs_cup_results()
    mark_official_cup_results(cup, official_cup)
    events = rpl + cup
    if len(rpl) < 20: raise RuntimeError(f"Слишком мало матчей РПЛ: {len(rpl)}")
    if len(cup) < 4: raise RuntimeError(f"Слишком мало матчей Кубка: {len(cup)}")
    state, stable_ids = update_state(load_state(), events)
    original = ICS_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    updated, changed = apply_events(original, parse_existing_events(original), events, stable_ids)
    validate_calendar(updated)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state_text = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous_state = STATE_PATH.read_text(encoding="utf-8") if STATE_PATH.exists() else ""
    if state_text != previous_state: STATE_PATH.write_text(state_text, encoding="utf-8")
    if updated != original: ICS_PATH.write_text(updated, encoding="utf-8")
    print(f"Матчей РПЛ: {len(rpl)}")
    print(f"Матчей Кубка: {len(cup)}")
    print(f"Результатов Кубка подтверждено РФС: {sum(1 for e in cup if e.get('official_confirmed'))}")
    print(f"Ожидают второй одинаковой проверки: {len(events) - len(stable_ids)}")
    print(f"Изменено событий календаря: {changed}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Ошибка обновления календаря: {error}", file=sys.stderr)
        raise
