import datetime
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SUBDOMAIN = "arcig"
TARGET_CLASS = "2.A"
TIMEOUT = 30


def post_rpc(path, function, args):
    """Zavolá veřejné JSON-RPC rozhraní, které používá web EduPage."""
    url = f"https://{SUBDOMAIN}.edupage.org/{path}?__func={function}"
    payload = {"__args": [None, *args], "__gsh": "00000000"}
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "Mozilla/5.0 (timetable updater)",
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


def school_year(day):
    return day.year if day.month >= 9 else day.year - 1


def load_lookup_data(year, day):
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
    date = day.isoformat()
    result = post_rpc(
        "rpr/server/maindbi.js",
        "mainDBIAccessor",
        [year, {"vt_filter": {"datefrom": date, "dateto": date}}, request],
    )
    return {
        table["id"]: table.get("data_rows", [])
        for table in result.get("tables", [])
        if "id" in table
    }


def row_name(row):
    short = row.get("short") or row.get("name")
    if short:
        return str(short)
    return " ".join(filter(None, (row.get("firstname"), row.get("lastname"))))


def make_lookup(rows):
    return {str(row["id"]): row_name(row) for row in rows if "id" in row}


def fetch_timetable(day=None):
    day = day or datetime.date.today()
    year = school_year(day)
    tables = load_lookup_data(year, day)

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

    date = day.isoformat()
    result = post_rpc(
        "timetable/server/currenttt.js",
        "curentttGetData",
        [
            {
                "year": year,
                "datefrom": date,
                "dateto": date,
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
    cards = []

    for item in result.get("ttitems", []):
        if item.get("type") != "card":
            continue
        subject = subjects.get(str(item.get("subjectid")), "")
        groups = [str(group) for group in item.get("groupnames", []) if group]
        if groups:
            subject = f"{subject} ({', '.join(groups)})" if subject else ", ".join(groups)
        cards.append(
            {
                "date": item.get("date", date),
                "period": item.get("uniperiod"),
                "start": item.get("starttime"),
                "end": item.get("endtime"),
                "subject": subject,
                "teacher": ", ".join(
                    filter(None, (teachers.get(str(value)) for value in item.get("teacherids", [])))
                ),
                "classroom": ", ".join(
                    filter(None, (classrooms.get(str(value)) for value in item.get("classroomids", [])))
                ),
                "changed": bool(item.get("changed")),
            }
        )

    cards.sort(key=lambda card: (card["start"] or "", str(card["period"] or ""), card["subject"]))
    output = {
        "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "date": date,
        "class": TARGET_CLASS,
        "lessons": cards,
    }
    with open("timetable.json", "w", encoding="utf-8", newline="\n") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)
        file.write("\n")

    print(f"Hotovo: {len(cards)} položek rozvrhu pro {TARGET_CLASS} na {date}.")


if __name__ == "__main__":
    fetch_timetable()
