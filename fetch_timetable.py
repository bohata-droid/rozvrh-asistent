"""Stažení týdenního rozvrhu třídy z veřejného zobrazení EduPage."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SUBDOMAIN = "arcig"
TARGET_CLASS = "2.A"
TIMEOUT = 30
OUTPUT_FILE = Path(__file__).with_name("timetable.json")
SCRIPT_FILE = Path(__file__).with_name("timetable-data.js")

# Časy odpovídají zvonění zobrazenému v rozvrhu školy. EduPage posílá časy
# také u jednotlivých karet; tato tabulka zajistí, že se zobrazí i prázdné hodiny.
PERIODS = [
    {"period": "0", "start": "07:25", "end": "08:00"},
    {"period": "1", "start": "08:15", "end": "09:00"},
    {"period": "2", "start": "09:10", "end": "09:55"},
    {"period": "3", "start": "10:15", "end": "11:00"},
    {"period": "4", "start": "11:10", "end": "11:55"},
    {"period": "5", "start": "12:05", "end": "12:50"},
    {"period": "6", "start": "13:00", "end": "13:45"},
    {"period": "7", "start": "13:50", "end": "14:35"},
    {"period": "8", "start": "14:40", "end": "15:25"},
    {"period": "9", "start": "15:30", "end": "16:15"},
]


def post_rpc(path: str, function: str, args: list) -> dict:
    """Zavolá veřejné JSON-RPC rozhraní používané webem EduPage."""
    url = f"https://{SUBDOMAIN}.edupage.org/{path}?__func={function}"
    payload = {"__args": [None, *args], "__gsh": "00000000"}
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (asistent-rozvrhu)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=TIMEOUT) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"EduPage vrátil HTTP chybu {exc.code}.") from exc
    except URLError as exc:
        raise RuntimeError(f"K EduPage se nepodařilo připojit: {exc.reason}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"EduPage nevrátil platná JSON data: {body[:120]!r}") from exc
    result = data.get("r") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        raise RuntimeError(f"EduPage vrátil neočekávanou odpověď: {data}")
    if result.get("error"):
        raise RuntimeError(f"EduPage odmítl požadavek: {result['error']}")
    return result


def school_year(day: dt.date) -> int:
    return day.year if day.month >= 9 else day.year - 1


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def load_lookup_data(year: int, date_from: dt.date, date_to: dt.date) -> dict:
    columns = {
        "classes": ["__name", "name", "short"],
        "subjects": ["__name", "name", "short"],
        "teachers": ["__name", "firstname", "lastname", "short"],
        "classrooms": ["__name", "name", "short"],
    }
    request = {
        "op": "fetch",
        "tables": [],
        "columns": [],
        "needed_part": columns,
        "needed_combos": {},
        "client_filter": {},
        "info_tables": [],
        "info_columns": [],
    }
    result = post_rpc(
        "rpr/server/maindbi.js",
        "mainDBIAccessor",
        [
            year,
            {"vt_filter": {"datefrom": date_from.isoformat(), "dateto": date_to.isoformat()}},
            request,
        ],
    )
    return {
        table["id"]: table.get("data_rows", [])
        for table in result.get("tables", [])
        if "id" in table
    }


def row_name(row: dict) -> str:
    short = row.get("short") or row.get("name")
    if short:
        return str(short)
    return " ".join(filter(None, (row.get("firstname"), row.get("lastname"))))


def make_lookup(rows: list[dict]) -> dict[str, str]:
    return {str(row["id"]): row_name(row) for row in rows if "id" in row}


def _lesson_key(item: dict, date: str, period: str) -> str:
    identity = json.dumps(
        [
            date,
            period,
            item.get("subjectid"),
            sorted(map(str, item.get("groupnames", []))),
            sorted(map(str, item.get("teacherids", []))),
            sorted(map(str, item.get("classroomids", []))),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]


def fetch_week(week_start: dt.date | None = None, *, write_files: bool = False) -> dict:
    """Vrátí rozvrh pondělí–pátek týdne a volitelně aktualizuje statický export."""
    week_start = monday_of(week_start or dt.date.today())
    week_end = week_start + dt.timedelta(days=4)
    year = school_year(week_start)
    tables = load_lookup_data(year, week_start, week_end)

    classes = make_lookup(tables.get("classes", []))
    target_id = next(
        (
            class_id
            for class_id, name in classes.items()
            if name.strip().casefold() == TARGET_CLASS.strip().casefold()
        ),
        None,
    )
    if target_id is None:
        raise RuntimeError(
            f"Třída {TARGET_CLASS!r} nebyla nalezena. "
            f"Dostupné třídy: {', '.join(classes.values()) or 'žádné'}"
        )

    result = post_rpc(
        "timetable/server/currenttt.js",
        "curentttGetData",
        [
            {
                "year": year,
                "datefrom": week_start.isoformat(),
                "dateto": week_end.isoformat(),
                "table": "classes",
                "id": target_id,
                "showColors": True,
                "showIgroupsInClasses": True,
                "showOrig": True,
                "log_module": "CurrentTTView",
            }
        ],
    )

    subjects = make_lookup(tables.get("subjects", []))
    teachers = make_lookup(tables.get("teachers", []))
    classrooms = make_lookup(tables.get("classrooms", []))
    lessons = []
    for item in result.get("ttitems", []):
        if item.get("type") != "card":
            continue
        date = str(item.get("date") or week_start.isoformat())
        period = str(item.get("uniperiod") or "")
        groups = [str(group) for group in item.get("groupnames", []) if group]
        lessons.append(
            {
                "key": _lesson_key(item, date, period),
                "date": date,
                "period": period,
                "start": item.get("starttime"),
                "end": item.get("endtime"),
                "duration_periods": max(1, int(item.get("durationperiods") or 1)),
                "subject": subjects.get(str(item.get("subjectid")), ""),
                "groups": groups,
                "teacher": ", ".join(
                    filter(None, (teachers.get(str(value)) for value in item.get("teacherids", [])))
                ),
                "classroom": ", ".join(
                    filter(None, (classrooms.get(str(value)) for value in item.get("classroomids", [])))
                ),
                "changed": bool(item.get("changed")),
                "removed": bool(item.get("removed")),
            }
        )

    lessons.sort(key=lambda card: (card["date"], card["period"], card["subject"], card["key"]))
    output = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "range_start": week_start.isoformat(),
        "range_end": week_end.isoformat(),
        "class": TARGET_CLASS,
        "periods": PERIODS,
        "lessons": lessons,
    }
    if write_files:
        json_text = json.dumps(output, ensure_ascii=False, indent=2)
        OUTPUT_FILE.write_text(json_text + "\n", encoding="utf-8")
        SCRIPT_FILE.write_text(
            "window.TIMETABLE_DATA = " + json_text.replace("</", "<\\/") + ";\n",
            encoding="utf-8",
        )
    return output


def fetch_timetable(day: dt.date | None = None) -> dict:
    """Zpětně kompatibilní vstupní bod použitý workflow a staršími voláními."""
    return fetch_week(day, write_files=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Stáhne týdenní rozvrh třídy z EduPage.")
    parser.add_argument("--date", help="libovolný den týdne ve formátu RRRR-MM-DD")
    args = parser.parse_args()
    day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    data = fetch_week(day, write_files=True)
    print(
        f"Hotovo: {len(data['lessons'])} položek pro {data['class']} "
        f"({data['range_start']} až {data['range_end']})."
    )


if __name__ == "__main__":
    main()
