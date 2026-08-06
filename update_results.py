#!/usr/bin/env python3
"""Synchronize the Spartak Moscow subscribed calendar with current fixtures/results.

Data source: Sofascore's public JSON endpoints. Calendar changes are conservative:
- completed results are applied immediately;
- schedule/status changes must be observed identically twice in a row;
- existing UIDs are preserved;
- the resulting ICS is validated before it is written.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ICS_PATH = Path("spartak-moscow.ics")
STATE_PATH = Path(".github/calendar-state.json")
TEAM_ID = 2323
SEASON_START = date(2026, 7, 1)
SEASON_END = date(2027, 6, 30)
MOSCOW = ZoneInfo("Europe/Moscow")
USER_AGENT = "naztutnet-spartak-calendar/2.0 (+https://github.com/naztutnet/spartak-calendar)"
API_ROOTS = (
    "https://api.sofascore.com/api/v1",
    "https://www.sofascore.com/api/v1",
)

DISPLAY_NAMES = {
    "spartak": "Спартак",
    "orenburg": "Оренбург",
    "rubin": "Рубин",
    "krasnodar": "Краснодар",
    "zenit": "Зенит",
    "akhmat": "Ахмат",
    "rodina": "Родина",
    "baltika": "Балтика",
    "dynamo": "Динамо",
    "dynamo_makhachkala": "Динамо Махачкала",
    "lokomotiv": "Локомотив",
    "cska": "ЦСКА",
    "rostov": "Ростов",
    "akron": "Акрон",
    "krylya": "Крылья Советов",
    "fakel": "Факел",
}

ALIAS_GROUPS = {
    "spartak": ("spartak", "spartak moscow", "fc spartak moscow", "спартак", "спартак москва"),
    "orenburg": ("orenburg", "fc orenburg", "оренбург"),
    "rubin": ("rubin", "rubin kazan", "рубин", "рубин казань"),
    "krasnodar": ("krasnodar", "fc krasnodar", "краснодар"),
    "zenit": ("zenit", "zenit st petersburg", "зенит", "зенит санкт петербург"),
    "akhmat": ("akhmat", "akhmat grozny", "ахмат", "ахмат грозный"),
    "rodina": ("rodina", "rodina moscow", "родина", "родина москва"),
    "baltika": ("baltika", "baltika kaliningrad", "балтика", "балтика калининград"),
    "dynamo_makhachkala": (
        "dynamo makhachkala",
        "dinamo makhachkala",
        "динамо махачкала",
    ),
    "dynamo": ("dynamo moscow", "dinamo moscow", "динамо москва", "динамо"),
    "lokomotiv": ("lokomotiv", "lokomotiv moscow", "локомотив", "локомотив москва"),
    "cska": ("cska", "cska moscow", "цска", "цска москва"),
    "rostov": ("rostov", "fc rostov", "ростов"),
    "akron": ("akron", "akron tolyatti", "акрон", "акрон тольятти"),
    "krylya": (
        "krylya sovetov",
        "krylya sovetov samara",
        "крылья советов",
        "крылья советов самара",
    ),
    "fakel": ("fakel", "fakel voronezh", "факел", "факел воронеж"),
}


def compact(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]+", "", value.lower().replace("ё", "е"))


ALIASES: dict[str, str] = {}
for canonical, aliases in ALIAS_GROUPS.items():
    for alias in aliases:
        ALIASES[compact(alias)] = canonical


def canonical_team(name: str) -> str:
    key = compact(name)
    return ALIASES.get(key, key)


def display_team(canonical: str, fallback: str) -> str:
    return DISPLAY_NAMES.get(canonical, fallback)


def http_json(path: str) -> dict[str, Any]:
    errors: list[str] = []
    for root in API_ROOTS:
        url = root + path
        for attempt in range(3):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": USER_AGENT,
                        "Accept": "application/json",
                    },
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    if response.status != 200:
                        raise RuntimeError(f"HTTP {response.status}")
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                errors.append(f"{url}: {exc}")
                if attempt < 2:
                    time.sleep(2**attempt)
    raise RuntimeError("Не удалось получить данные матчей: " + " | ".join(errors[-4:]))


def fetch_team_events() -> list[dict[str, Any]]:
    events: dict[int, dict[str, Any]] = {}
    for direction in ("last", "next"):
        for page in range(4):
            payload = http_json(f"/team/{TEAM_ID}/events/{direction}/{page}")
            page_events = payload.get("events") or []
            for event in page_events:
                event_id = event.get("id")
                if isinstance(event_id, int):
                    events[event_id] = event
            if not payload.get("hasNextPage") and len(page_events) < 30:
                break
    normalized = [normalize_source_event(event) for event in events.values()]
    return sorted((event for event in normalized if event), key=lambda item: item["start_ts"])


def competition_code(event: dict[str, Any]) -> str | None:
    tournament = event.get("tournament") or {}
    unique = tournament.get("uniqueTournament") or {}
    text = " ".join(
        str(value)
        for value in (
            tournament.get("name"),
            tournament.get("slug"),
            unique.get("name"),
            unique.get("slug"),
        )
        if value
    ).lower()
    if "friendly" in text or "товарищ" in text:
        return None
    if "super cup" in text or "supercup" in text or "суперкуб" in text:
        return "supercup"
    if "cup" in text or "кубок" in text:
        return "cup"
    if "premier league" in text or "рпл" in text:
        return "rpl"
    return None


def normalize_source_event(event: dict[str, Any]) -> dict[str, Any] | None:
    home = event.get("homeTeam") or {}
    away = event.get("awayTeam") or {}
    if TEAM_ID not in {home.get("id"), away.get("id")}:
        return None
    comp = competition_code(event)
    if comp is None:
        return None
    start_ts = event.get("startTimestamp")
    if not isinstance(start_ts, int):
        return None
    start = datetime.fromtimestamp(start_ts, timezone.utc).astimezone(MOSCOW)
    if not (SEASON_START <= start.date() <= SEASON_END):
        return None

    home_key = canonical_team(str(home.get("name") or home.get("shortName") or ""))
    away_key = canonical_team(str(away.get("name") or away.get("shortName") or ""))
    if "spartak" not in {home_key, away_key}:
        return None

    status = str((event.get("status") or {}).get("type") or "scheduled").lower()
    home_score = event.get("homeScore") or {}
    away_score = event.get("awayScore") or {}
    score_home = home_score.get("current")
    score_away = away_score.get("current")
    if not isinstance(score_home, (int, float)):
        score_home = None
    if not isinstance(score_away, (int, float)):
        score_away = None

    venue = event.get("venue") or {}
    city = venue.get("city") or {}
    round_info = event.get("roundInfo") or {}
    custom_id = event.get("customId")
    slug = event.get("slug")
    source_url = (
        f"https://www.sofascore.com/{slug}/{custom_id}#id:{event['id']}"
        if slug and custom_id
        else f"https://www.sofascore.com/event/{event['id']}"
    )

    return {
        "id": int(event["id"]),
        "start_ts": start_ts,
        "start": start.isoformat(),
        "home_key": home_key,
        "away_key": away_key,
        "home_name": display_team(home_key, str(home.get("shortName") or home.get("name") or "")),
        "away_name": display_team(away_key, str(away.get("shortName") or away.get("name") or "")),
        "competition": comp,
        "status": status,
        "score_home": int(score_home) if score_home is not None else None,
        "score_away": int(score_away) if score_away is not None else None,
        "pen_home": home_score.get("penalties"),
        "pen_away": away_score.get("penalties"),
        "round": round_info.get("round") or round_info.get("name"),
        "venue": venue.get("stadium", {}).get("name")
        if isinstance(venue.get("stadium"), dict)
        else venue.get("name"),
        "city": city.get("name") if isinstance(city, dict) else city,
        "source_url": source_url,
    }


def event_fingerprint(event: dict[str, Any]) -> str:
    relevant = {
        key: event.get(key)
        for key in (
            "start_ts",
            "home_key",
            "away_key",
            "competition",
            "status",
            "score_home",
            "score_away",
            "pen_home",
            "pen_away",
            "round",
            "venue",
            "city",
        )
    }
    payload = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"version": 1, "events": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "events": {}}
    if not isinstance(state, dict) or not isinstance(state.get("events"), dict):
        return {"version": 1, "events": {}}
    return state


def update_state(state: dict[str, Any], events: list[dict[str, Any]]) -> tuple[dict[str, Any], set[int]]:
    entries = state.setdefault("events", {})
    stable: set[int] = set()
    for event in events:
        key = str(event["id"])
        fingerprint = event_fingerprint(event)
        previous = entries.get(key) or {}
        observations = (
            min(int(previous.get("observations", 0)) + 1, 2)
            if previous.get("fingerprint") == fingerprint
            else 1
        )
        entries[key] = {
            "fingerprint": fingerprint,
            "observations": observations,
        }
        finished = event["status"] in {"finished", "afterpenalties", "afterextra"}
        if finished or observations >= 2:
            stable.add(event["id"])
    state["version"] = 1
    return state, stable


@dataclass
class ExistingEvent:
    block: str
    uid: str
    summary: str
    description: str
    location: str
    url: str
    dtstart_raw: str
    dtend_raw: str
    sequence: int
    status: str
    transp: str
    source_id: int | None
    opponent: str | None
    competition: str | None
    start_date: date | None
    end_date: date | None


def property_value(block: str, name: str, default: str = "") -> str:
    match = re.search(rf"^{re.escape(name)}(?:;[^:]*)?:(.*)$", block, flags=re.M)
    return match.group(1).strip() if match else default


def unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def escape_ics(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def parse_date_property(raw: str) -> date | None:
    match = re.search(r"(\d{8})", raw)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def detect_competition(text: str, uid: str = "") -> str | None:
    lowered = (text + " " + uid).lower()
    if "supercup" in lowered or "суперкуб" in lowered:
        return "supercup"
    if "cup" in lowered or "кубок" in lowered:
        return "cup"
    if "rpl" in lowered or "рпл" in lowered or "премьер" in lowered:
        return "rpl"
    return None


def detect_opponent(text: str) -> str | None:
    normalized = compact(text)
    candidates: list[tuple[int, str]] = []
    for alias, canonical in ALIASES.items():
        if canonical == "spartak":
            continue
        if alias and alias in normalized:
            candidates.append((len(alias), canonical))
    if not candidates:
        return None
    return max(candidates)[1]


def parse_existing_events(calendar_text: str) -> list[ExistingEvent]:
    output: list[ExistingEvent] = []
    for match in re.finditer(r"BEGIN:VEVENT\n.*?\nEND:VEVENT", calendar_text, flags=re.S):
        block = match.group(0)
        uid = property_value(block, "UID")
        summary = unescape_ics(property_value(block, "SUMMARY"))
        description = unescape_ics(property_value(block, "DESCRIPTION"))
        source_raw = property_value(block, "X-SOURCE-ID")
        source_match = re.search(r"sofascore-(\d+)", source_raw)
        dtstart_match = re.search(r"^DTSTART(?:;[^:]*)?:(.*)$", block, flags=re.M)
        dtend_match = re.search(r"^DTEND(?:;[^:]*)?:(.*)$", block, flags=re.M)
        dtstart_raw = dtstart_match.group(0) if dtstart_match else ""
        dtend_raw = dtend_match.group(0) if dtend_match else ""
        start_date = parse_date_property(dtstart_raw)
        end_date = parse_date_property(dtend_raw)
        output.append(
            ExistingEvent(
                block=block,
                uid=uid,
                summary=summary,
                description=description,
                location=unescape_ics(property_value(block, "LOCATION")),
                url=property_value(block, "URL"),
                dtstart_raw=dtstart_raw,
                dtend_raw=dtend_raw,
                sequence=int(property_value(block, "SEQUENCE", "0") or 0),
                status=property_value(block, "STATUS", "CONFIRMED"),
                transp=property_value(block, "TRANSP", "OPAQUE"),
                source_id=int(source_match.group(1)) if source_match else None,
                opponent=detect_opponent(summary),
                competition=detect_competition(summary + " " + description, uid),
                start_date=start_date,
                end_date=end_date,
            )
        )
    return output


def source_opponent(event: dict[str, Any]) -> str:
    return event["away_key"] if event["home_key"] == "spartak" else event["home_key"]


def match_existing(source: dict[str, Any], existing: list[ExistingEvent], used: set[str]) -> ExistingEvent | None:
    for item in existing:
        if item.uid in used:
            continue
        if item.source_id == source["id"]:
            return item

    opponent = source_opponent(source)
    source_date = datetime.fromtimestamp(source["start_ts"], timezone.utc).astimezone(MOSCOW).date()
    candidates: list[tuple[int, ExistingEvent]] = []
    for item in existing:
        if item.uid in used:
            continue
        if item.opponent != opponent or item.competition != source["competition"] or not item.start_date:
            continue
        if item.dtstart_raw.startswith("DTSTART;VALUE=DATE"):
            end = item.end_date or item.start_date
            if item.start_date - timedelta(days=1) <= source_date <= end + timedelta(days=1):
                candidates.append((0, item))
        else:
            distance = abs((item.start_date - source_date).days)
            if distance <= 3:
                candidates.append((distance, item))
    return min(candidates, key=lambda pair: pair[0])[1] if candidates else None


def competition_label(code: str) -> str:
    return {
        "rpl": "РПЛ",
        "cup": "Кубок России",
        "supercup": "Суперкубок России",
    }[code]


def competition_full_name(code: str) -> str:
    return {
        "rpl": "Альфа-Банк Российская Премьер-Лига 2026/27",
        "cup": "FONBET Кубок России 2026/27",
        "supercup": "OLIMPBET Суперкубок России 2026",
    }[code]


def official_source(code: str) -> str:
    return {
        "rpl": "https://premierliga.ru/",
        "cup": "https://www.rfs.ru/cup/tournament/matches",
        "supercup": "https://www.rfs.ru/",
    }[code]


def duration_for(code: str) -> timedelta:
    return timedelta(minutes=135 if code == "rpl" else 150)


def result_phrase(event: dict[str, Any]) -> str:
    home = event["score_home"]
    away = event["score_away"]
    if home is None or away is None:
        return ""
    spartak_home = event["home_key"] == "spartak"
    spartak_score = home if spartak_home else away
    opponent_score = away if spartak_home else home
    if spartak_score > opponent_score:
        outcome = "победа Спартака"
    elif spartak_score < opponent_score:
        outcome = "поражение Спартака"
    else:
        outcome = "ничья"
    return f"Результат: {outcome} {spartak_score}:{opponent_score}."


def existing_media_lines(description: str) -> list[str]:
    found: list[str] = []
    for label in ("Видеообзор", "Обзор", "Полный матч", "Полная запись"):
        for match in re.finditer(rf"{label}:\s*(https?://\S+)", description):
            url = match.group(1).rstrip(".,;")
            line = f"{label}: {url}"
            if line not in found:
                found.append(line)
    return found


def strip_generated_sources(description: str) -> str:
    cleaned = re.sub(
        r"\s*Источник расписания/результата:\s*https?://\S+",
        "",
        description,
    )
    cleaned = re.sub(
        r"\s*Официальный турнирный источник:\s*https?://\S+",
        "",
        cleaned,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def find_matchtv_media(event: dict[str, Any]) -> list[str]:
    if event["status"] not in {"finished", "afterpenalties", "afterextra"}:
        return []
    home, away = event["home_name"], event["away_name"]
    query = f'site:matchtv.ru/football "{home}" "{away}"'
    url = "https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(query)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=15) as response:
            root = ET.fromstring(response.read())
    except Exception as exc:
        print(f"Поиск видео пропущен: {exc}", file=sys.stderr)
        return []

    review = None
    full = None
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").lower()
        link = item.findtext("link") or ""
        host = urllib.parse.urlparse(link).hostname or ""
        if host not in {"matchtv.ru", "www.matchtv.ru"}:
            continue
        if not review and "_clip_" in link and ("гол" in title or "момент" in title or "обзор" in title):
            review = link
        if not full and ("_translation_" in link or "полный матч" in title or "полная трансляция" in title):
            full = link
    lines = []
    if review:
        lines.append("Видеообзор: " + review)
    if full:
        lines.append("Полный матч: " + full)
    return lines


def desired_fields(source: dict[str, Any], old: ExistingEvent | None) -> dict[str, Any]:
    start = datetime.fromtimestamp(source["start_ts"], timezone.utc).astimezone(MOSCOW)
    end = start + duration_for(source["competition"])
    finished = source["status"] in {"finished", "afterpenalties", "afterextra"}
    score_known = source["score_home"] is not None and source["score_away"] is not None

    if finished and score_known:
        summary = (
            f'{source["home_name"]} {source["score_home"]}:{source["score_away"]} '
            f'{source["away_name"]} ({competition_label(source["competition"])})'
        )
        if source.get("pen_home") is not None and source.get("pen_away") is not None:
            summary += f' ({source["pen_home"]}:{source["pen_away"]} пен.)'
    else:
        prefix = "⏳ " if source["status"] in {"postponed", "canceled", "cancelled"} else ""
        summary = (
            f'{prefix}{source["home_name"]} — {source["away_name"]} '
            f'({competition_label(source["competition"])})'
        )

    relation = "Домашний матч." if source["home_key"] == "spartak" else "Выездной матч."
    media = existing_media_lines(old.description) if old else []

    if old:
        description = strip_generated_sources(old.description)
        description = description.replace(" Время московское.", "").replace("Время московское.", "")
        description = re.sub(r"\s+", " ", description).strip()
    else:
        description_parts = [competition_full_name(source["competition"]) + "."]
        if source.get("round"):
            description_parts.append(f'Тур/этап: {source["round"]}.')
        description_parts.append(relation)
        description = " ".join(description_parts)

    if finished and score_known:
        result = result_phrase(source)
        if re.search(r"Результат:[^.]*\.", description):
            description = re.sub(r"Результат:[^.]*\.", result, description, count=1)
        elif result not in description:
            description = (description + " " + result).strip()
    elif "Время московское." not in description:
        description = (description + " Время московское.").strip()

    if finished and not media:
        media = find_matchtv_media(source)
    for line in media:
        if line not in description:
            description = (description + " " + line).strip()

    source_line = f'Источник расписания/результата: {source["source_url"]}'
    official_line = f'Официальный турнирный источник: {official_source(source["competition"])}'
    description = f"{description} {source_line} {official_line}".strip()

    venue_parts = [part for part in (source.get("venue"), source.get("city")) if part]
    source_location = ", ".join(dict.fromkeys(venue_parts))
    if old and old.location and "уточняется" not in old.location.lower():
        location = old.location
    else:
        location = source_location or (old.location if old else "") or "Место проведения уточняется"

    media_url = ""
    for line in media:
        match = re.search(r"https?://\S+", line)
        if match:
            media_url = match.group(0).rstrip(".,;")
            break
    if old and old.url:
        url = old.url
    else:
        url = media_url or source["source_url"]

    status = "TENTATIVE" if source["status"] in {"postponed", "canceled", "cancelled"} else "CONFIRMED"
    alarms = not finished and status == "CONFIRMED" and start > datetime.now(MOSCOW)
    return {
        "summary": summary,
        "description": description,
        "location": location,
        "url": url,
        "dtstart": f"DTSTART;TZID=Europe/Moscow:{start.strftime('%Y%m%dT%H%M%S')}",
        "dtend": f"DTEND;TZID=Europe/Moscow:{end.strftime('%Y%m%dT%H%M%S')}",
        "status": status,
        "transp": "TRANSPARENT" if finished else "OPAQUE",
        "alarms": alarms,
        "source_id": source["id"],
    }


def semantic_signature_from_existing(item: ExistingEvent) -> dict[str, Any]:
    return {
        "summary": item.summary,
        "description": item.description,
        "location": item.location,
        "url": item.url,
        "dtstart": item.dtstart_raw,
        "dtend": item.dtend_raw,
        "status": item.status,
        "transp": item.transp,
        "alarms": "BEGIN:VALARM" in item.block,
        "source_id": item.source_id,
    }


def render_event(uid: str, sequence: int, fields: dict[str, Any], stamp: str) -> str:
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"SEQUENCE:{sequence}",
        f"LAST-MODIFIED:{stamp}",
        fields["dtstart"],
        fields["dtend"],
        "SUMMARY:" + escape_ics(fields["summary"]),
        "LOCATION:" + escape_ics(fields["location"]),
        "DESCRIPTION:" + escape_ics(fields["description"]),
        "URL:" + fields["url"],
        f'X-SOURCE-ID:sofascore-{fields["source_id"]}',
        "STATUS:" + fields["status"],
        "TRANSP:" + fields["transp"],
    ]
    if fields["alarms"]:
        lines.extend(
            [
                "BEGIN:VALARM",
                "TRIGGER:-PT1H",
                "ACTION:DISPLAY",
                "DESCRIPTION:Матч Спартака начнётся через 1 час",
                "END:VALARM",
                "BEGIN:VALARM",
                "TRIGGER:-PT5M",
                "ACTION:DISPLAY",
                "DESCRIPTION:Матч Спартака начнётся через 5 минут",
                "END:VALARM",
            ]
        )
    lines.append("END:VEVENT")
    return "\n".join(lines)


def generate_uid(source: dict[str, Any]) -> str:
    start = datetime.fromtimestamp(source["start_ts"], timezone.utc).astimezone(MOSCOW)
    opponent = source_opponent(source).replace("_", "-")
    return f'{start.strftime("%Y%m%d")}-spartak-{opponent}-{source["competition"]}@spartak-calendar'


def apply_events(
    original: str,
    existing: list[ExistingEvent],
    source_events: list[dict[str, Any]],
    stable_ids: set[int],
) -> tuple[str, int]:
    replacements: dict[str, str] = {}
    additions: list[str] = []
    used: set[str] = set()
    changed_count = 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for source in source_events:
        if source["id"] not in stable_ids:
            continue
        old = match_existing(source, existing, used)
        if old:
            used.add(old.uid)
        fields = desired_fields(source, old)
        if old and semantic_signature_from_existing(old) == fields:
            continue

        uid = old.uid if old else generate_uid(source)
        sequence = (old.sequence + 1) if old else 0
        rendered = render_event(uid, sequence, fields, stamp)
        if old:
            replacements[old.block] = rendered
        else:
            additions.append(rendered)
        changed_count += 1

    updated = original
    for old_block, new_block in replacements.items():
        updated = updated.replace(old_block, new_block, 1)
    if additions:
        insertion = "\n".join(additions) + "\n"
        updated = updated.replace("END:VCALENDAR", insertion + "END:VCALENDAR")
    return updated, changed_count


def validate_calendar(text: str) -> None:
    if not text.startswith("BEGIN:VCALENDAR") or not text.rstrip().endswith("END:VCALENDAR"):
        raise ValueError("Некорректные границы VCALENDAR")
    if text.count("BEGIN:VEVENT") != text.count("END:VEVENT"):
        raise ValueError("Несовпадающее количество BEGIN:VEVENT и END:VEVENT")
    events = parse_existing_events(text)
    uids = [event.uid for event in events]
    if any(not uid for uid in uids):
        raise ValueError("Найдено событие без UID")
    duplicates = sorted({uid for uid in uids if uids.count(uid) > 1})
    if duplicates:
        raise ValueError("Дублирующиеся UID: " + ", ".join(duplicates))
    source_ids = [event.source_id for event in events if event.source_id is not None]
    duplicate_sources = sorted({sid for sid in source_ids if source_ids.count(sid) > 1})
    if duplicate_sources:
        raise ValueError("Дублирующиеся X-SOURCE-ID: " + ", ".join(map(str, duplicate_sources)))
    for event in events:
        if not event.summary or not event.dtstart_raw:
            raise ValueError(f"Событие {event.uid} не содержит SUMMARY или DTSTART")


def main() -> int:
    if not ICS_PATH.exists():
        raise FileNotFoundError(f"Не найден {ICS_PATH}")
    source_events = fetch_team_events()
    if not source_events:
        raise RuntimeError("Источник вернул пустой список матчей Спартака")

    state_before = load_state()
    state_after, stable_ids = update_state(state_before, source_events)

    original = ICS_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
    existing = parse_existing_events(original)
    updated, changed_count = apply_events(original, existing, source_events, stable_ids)
    validate_calendar(updated)

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state_text = json.dumps(state_after, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    previous_state_text = STATE_PATH.read_text(encoding="utf-8") if STATE_PATH.exists() else ""
    if state_text != previous_state_text:
        STATE_PATH.write_text(state_text, encoding="utf-8")

    if updated != original:
        ICS_PATH.write_text(updated, encoding="utf-8")

    staged = sum(1 for event in source_events if event["id"] not in stable_ids)
    print(f"Получено матчей: {len(source_events)}")
    print(f"Стабильных данных: {len(stable_ids)}")
    print(f"Ожидают второй проверки: {staged}")
    print(f"Обновлено событий календаря: {changed_count}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Ошибка обновления календаря: {error}", file=sys.stderr)
        raise
