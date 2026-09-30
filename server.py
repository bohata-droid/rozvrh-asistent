"""Webová aplikace pro evidenci asistenta pedagoga.

Používá jen standardní knihovnu Pythonu. Spuštění: ``python server.py``.
"""

from __future__ import annotations

import datetime as dt
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import smtplib
import sqlite3
import ssl
import threading
import time
from email.message import EmailMessage
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fetch_timetable import fetch_week, monday_of


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ASSISTANT_DB_PATH", BASE_DIR / "assistant.db"))
ADMIN_PASSWORD = os.environ.get("ASSISTANT_ADMIN_PASSWORD", "zmenit-me")
HOST = os.environ.get("ASSISTANT_HOST", "127.0.0.1")
PORT = int(os.environ.get("ASSISTANT_PORT", os.environ.get("PORT", "8000")))
try:
    PRAGUE = ZoneInfo("Europe/Prague")
except ZoneInfoNotFoundError:
    class PragueTimezone(dt.tzinfo):
        """Europe/Prague bez externího balíčku tzdata (pravidla EU od roku 1996)."""

        def _bounds(self, year: int) -> tuple[dt.datetime, dt.datetime]:
            march_last = dt.date(year, 3, 31)
            march_last -= dt.timedelta(days=(march_last.weekday() + 1) % 7)
            october_last = dt.date(year, 10, 31)
            october_last -= dt.timedelta(days=(october_last.weekday() + 1) % 7)
            return (
                dt.datetime.combine(march_last, dt.time(2)),
                dt.datetime.combine(october_last, dt.time(3)),
            )

        def utcoffset(self, value: dt.datetime | None) -> dt.timedelta:
            return dt.timedelta(hours=1) + self.dst(value)

        def dst(self, value: dt.datetime | None) -> dt.timedelta:
            if value is None:
                return dt.timedelta(0)
            start, end = self._bounds(value.year)
            naive = value.replace(tzinfo=None)
            return dt.timedelta(hours=1) if start <= naive < end else dt.timedelta(0)

        def tzname(self, value: dt.datetime | None) -> str:
            return "CEST" if self.dst(value) else "CET"

        def fromutc(self, value: dt.datetime) -> dt.datetime:
            naive_utc = value.replace(tzinfo=None)
            start, end = self._bounds(value.year)
            start_utc = start - dt.timedelta(hours=1)
            end_utc = end - dt.timedelta(hours=2)
            offset = dt.timedelta(hours=2 if start_utc <= naive_utc < end_utc else 1)
            return (naive_utc + offset).replace(tzinfo=self)

    PRAGUE = PragueTimezone()
SESSIONS: dict[str, tuple[int, float]] = {}
SESSION_LOCK = threading.Lock()
CACHE_SECONDS = 60 * 60


def now_local() -> dt.datetime:
    return dt.datetime.now(PRAGUE)


def date_cz(value: dt.date) -> str:
    return f"{value.day}. {value.month}. {value.year}"


def datetime_cz(value: dt.datetime) -> str:
    value = value.astimezone(PRAGUE)
    return f"{value.day}. {value.month}. {value.year} {value:%H:%M}"


class ClosingConnection(sqlite3.Connection):
    """Po dokončení transakce spojení také zavře, nejen commitne."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_database() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_connect() as db:
        db.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                email TEXT NOT NULL DEFAULT '',
                pin_hash TEXT NOT NULL,
                can_edit INTEGER NOT NULL DEFAULT 0,
                notify INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                role TEXT NOT NULL DEFAULT 'assistant',
                system INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS selections (
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                lesson_key TEXT NOT NULL,
                lesson_label TEXT NOT NULL,
                group_label TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (lesson_date, period)
            );
            CREATE TABLE IF NOT EXISTS whole_class_exclusions (
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                lesson_key TEXT NOT NULL,
                lesson_label TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (lesson_date, period)
            );
            CREATE TABLE IF NOT EXISTS note (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                text TEXT NOT NULL DEFAULT '',
                user_id INTEGER REFERENCES users(id),
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );
            INSERT OR IGNORE INTO note (id, text) VALUES (1, '');
            CREATE TABLE IF NOT EXISTS teacher_message (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                text TEXT NOT NULL DEFAULT '',
                user_id INTEGER REFERENCES users(id),
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            );
            INSERT OR IGNORE INTO teacher_message (id, text) VALUES (1, '');
            CREATE TABLE IF NOT EXISTS absences (
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                lesson_key TEXT NOT NULL,
                lesson_label TEXT NOT NULL,
                group_label TEXT NOT NULL DEFAULT '',
                user_id INTEGER REFERENCES users(id),
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (lesson_date, period, lesson_key)
            );
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                description TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'legacy',
                lesson_date TEXT,
                period TEXT,
                lesson_key TEXT,
                lesson_label TEXT,
                group_label TEXT,
                presence INTEGER,
                message_kind TEXT,
                message_text TEXT,
                notified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS timetable_cache (
                week_start TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            """
        )
        role_was_missing = "role" not in {
            row["name"] for row in db.execute("PRAGMA table_info(users)")
        }
        migrations = {
            "users": {
                "role": "TEXT NOT NULL DEFAULT 'assistant'",
                "system": "INTEGER NOT NULL DEFAULT 0",
            },
            "selections": {"updated_by": "TEXT NOT NULL DEFAULT ''"},
            "whole_class_exclusions": {"updated_by": "TEXT NOT NULL DEFAULT ''"},
            "note": {"updated_by": "TEXT NOT NULL DEFAULT ''"},
            "change_log": {
                "event_type": "TEXT NOT NULL DEFAULT 'legacy'",
                "lesson_date": "TEXT",
                "period": "TEXT",
                "lesson_key": "TEXT",
                "lesson_label": "TEXT",
                "group_label": "TEXT",
                "presence": "INTEGER",
                "message_kind": "TEXT",
                "message_text": "TEXT",
            },
        }
        for table, columns in migrations.items():
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

        # Dosavadní zapisující uživatelé jsou po migraci asistenti, ostatní učitelé.
        if role_was_missing:
            db.execute("UPDATE users SET role=CASE WHEN can_edit=1 THEN 'assistant' ELSE 'teacher' END")
        else:
            db.execute(
                "UPDATE users SET role='assistant' WHERE role NOT IN ('assistant', 'teacher') OR role IS NULL"
            )
        db.execute(
            """UPDATE selections SET updated_by=COALESCE(
                   (SELECT name FROM users WHERE users.id=selections.user_id), '')
               WHERE updated_by=''"""
        )
        db.execute(
            """UPDATE whole_class_exclusions SET updated_by=COALESCE(
                   (SELECT name FROM users WHERE users.id=whole_class_exclusions.user_id), '')
               WHERE updated_by=''"""
        )
        db.execute(
            """UPDATE note SET updated_by=COALESCE(
                   (SELECT name FROM users WHERE users.id=note.user_id), '')
               WHERE updated_by=''"""
        )


def hash_pin(pin: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 240_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_pin(pin: str, encoded: str) -> bool:
    try:
        salt_hex, expected = encoded.split("$", 1)
        actual = hash_pin(pin, bytes.fromhex(salt_hex)).split("$", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def public_user(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "email": row["email"],
        "role": row["role"],
        "can_edit": row["role"] == "assistant",
        "notify": bool(row["notify"]),
        "active": bool(row["active"]),
    }


def deleted_user_id(db: sqlite3.Connection) -> int:
    """Vrátí interního vlastníka záznamů po smazaném uživateli."""
    row = db.execute("SELECT id FROM users WHERE system=1 LIMIT 1").fetchone()
    if row:
        return row["id"]
    cursor = db.execute(
        """INSERT INTO users
           (name, email, pin_hash, can_edit, notify, active, role, system, created_at)
           VALUES (?, '', ?, 0, 0, 0, 'assistant', 1, ?)""",
        (f"__smazaný_uživatel_{secrets.token_hex(6)}", hash_pin(secrets.token_urlsafe(24)), now_local().isoformat()),
    )
    return cursor.lastrowid


def log_schedule_change(
    db: sqlite3.Connection,
    user: sqlite3.Row,
    stamp: str,
    description: str,
    lesson_date: dt.date,
    period: str,
    lesson_key: str,
    lesson_label: str,
    group_label: str,
    present: bool,
) -> None:
    db.execute(
        """INSERT INTO change_log
           (created_at, user_id, description, event_type, lesson_date, period,
            lesson_key, lesson_label, group_label, presence)
           VALUES (?, ?, ?, 'schedule', ?, ?, ?, ?, ?, ?)""",
        (
            stamp, user["id"], description, lesson_date.isoformat(), period,
            lesson_key, lesson_label, group_label, int(present),
        ),
    )


def get_cached_week(start: dt.date, *, refresh: bool = False) -> tuple[dict, bool]:
    start = monday_of(start)
    stale_payload = None
    with db_connect() as db:
        row = db.execute(
            "SELECT payload, fetched_at FROM timetable_cache WHERE week_start = ?",
            (start.isoformat(),),
        ).fetchone()
    if row:
        stale_payload = json.loads(row["payload"])
        age = now_local() - dt.datetime.fromisoformat(row["fetched_at"])
        if not refresh and age.total_seconds() < CACHE_SECONDS:
            return stale_payload, True
    try:
        payload = fetch_week(start)
    except Exception:
        if stale_payload is not None:
            stale_payload["cache_warning"] = "EduPage není dostupný, zobrazuji poslední uloženou verzi."
            return stale_payload, True
        static_file = BASE_DIR / "timetable.json"
        if static_file.exists():
            static_payload = json.loads(static_file.read_text(encoding="utf-8"))
            if static_payload.get("range_start") == start.isoformat():
                static_payload["cache_warning"] = "EduPage není dostupný, zobrazuji poslední statický export."
                return static_payload, True
        raise
    with db_connect() as db:
        db.execute(
            """INSERT INTO timetable_cache (week_start, payload, fetched_at)
               VALUES (?, ?, ?)
               ON CONFLICT(week_start) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at""",
            (start.isoformat(), json.dumps(payload, ensure_ascii=False), now_local().isoformat()),
        )
    return payload, False


def build_month_export(month: str) -> tuple[bytes, dict[str, int]]:
    """Vytvoří excelově kompatibilní CSV s měsíčním souhrnem a detailem hodin."""
    try:
        first_day = dt.date.fromisoformat(month + "-01")
    except ValueError as exc:
        raise ValueError("Neplatný měsíc. Použijte formát RRRR-MM.") from exc
    if first_day.strftime("%Y-%m") != month:
        raise ValueError("Neplatný měsíc. Použijte formát RRRR-MM.")
    next_month = (
        first_day.replace(year=first_day.year + 1, month=1)
        if first_day.month == 12
        else first_day.replace(month=first_day.month + 1)
    )
    last_day = next_month - dt.timedelta(days=1)

    with db_connect() as db:
        selections = {
            (row["lesson_date"], str(row["period"])): dict(row)
            for row in db.execute(
                """SELECT selections.*,
                          COALESCE(NULLIF(selections.updated_by, ''), users.name) AS user_name
                   FROM selections LEFT JOIN users ON users.id=selections.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (first_day.isoformat(), last_day.isoformat()),
            )
        }
        exclusions = {
            (row["lesson_date"], str(row["period"]), row["lesson_key"]): dict(row)
            for row in db.execute(
                "SELECT * FROM whole_class_exclusions WHERE lesson_date BETWEEN ? AND ?",
                (first_day.isoformat(), last_day.isoformat()),
            )
        }
        absence_rows = [
            dict(row)
            for row in db.execute(
                """SELECT absences.*,
                          COALESCE(NULLIF(absences.updated_by, ''), users.name) AS user_name
                   FROM absences LEFT JOIN users ON users.id=absences.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (first_day.isoformat(), last_day.isoformat()),
            )
        ]
    absences = {
        (row["lesson_date"], str(row["period"]), row["lesson_key"]): row
        for row in absence_rows
    }

    cells: dict[tuple[str, str], dict] = {}
    week_start = monday_of(first_day)
    while week_start <= last_day:
        week, _ = get_cached_week(week_start)
        periods = week.get("periods") or []
        period_indexes = {str(period["period"]): index for index, period in enumerate(periods)}
        for lesson in week.get("lessons") or []:
            lesson_date = str(lesson.get("date", ""))
            if not lesson_date.startswith(month) or lesson.get("removed"):
                continue
            start_period = str(lesson.get("period", ""))
            start_index = period_indexes.get(start_period)
            if start_index is None:
                continue
            grouped = bool(lesson.get("groups"))
            selection = selections.get((lesson_date, start_period))
            absence = absences.get((lesson_date, start_period, lesson["key"]))
            if grouped:
                present = bool(selection and selection["lesson_key"] == lesson["key"] and not absence)
                assistant_writer = selection["user_name"] if present else ""
            else:
                excluded = (lesson_date, start_period, lesson["key"]) in exclusions
                present = not excluded and not absence
                assistant_writer = "výchozí přítomnost celé třídy" if present else ""

            duration = max(1, int(lesson.get("duration_periods") or 1))
            for offset in range(duration):
                if start_index + offset >= len(periods):
                    break
                period = periods[start_index + offset]
                period_number = str(period["period"])
                cell = cells.setdefault(
                    (lesson_date, period_number),
                    {
                        "date": lesson_date,
                        "period": period_number,
                        "order": start_index + offset,
                        "start": period.get("start") or "",
                        "end": period.get("end") or "",
                        "counted": True,
                        "lessons": {},
                        "present": False,
                        "assistant_writers": set(),
                        "absence_lessons": set(),
                        "absence_teachers": set(),
                        "absence_times": set(),
                    },
                )
                lesson_name = lesson.get("subject") or "Hodina"
                groups = ", ".join(lesson.get("groups") or [])
                if groups:
                    lesson_name += f" ({groups})"
                cell["lessons"][lesson["key"]] = {
                    "name": lesson_name,
                    "teacher": lesson.get("teacher") or "",
                }
                cell["present"] = cell["present"] or present
                if assistant_writer:
                    cell["assistant_writers"].add(assistant_writer)
                if absence:
                    cell["absence_lessons"].add(lesson_name)
                    cell["absence_teachers"].add(absence["user_name"] or "neznámý učitel")
                    cell["absence_times"].add(
                        datetime_cz(dt.datetime.fromisoformat(absence["updated_at"]))
                    )
        week_start += dt.timedelta(days=7)

    # Zachováme i označenou absenci hodiny, kterou EduPage později změnil nebo odstranil.
    for absence in absence_rows:
        key = (absence["lesson_date"], str(absence["period"]))
        cell = cells.setdefault(
            key,
            {
                "date": absence["lesson_date"],
                "period": str(absence["period"]),
                "order": int(absence["period"]) if str(absence["period"]).isdigit() else 999,
                "start": "",
                "end": "",
                "counted": False,
                "lessons": {},
                "present": False,
                "assistant_writers": set(),
                "absence_lessons": set(),
                "absence_teachers": set(),
                "absence_times": set(),
            },
        )
        label = absence["lesson_label"] or "Hodina"
        if absence["group_label"]:
            label += f" ({absence['group_label']})"
        cell["lessons"].setdefault(absence["lesson_key"], {"name": label, "teacher": ""})
        cell["absence_lessons"].add(label)
        cell["absence_teachers"].add(absence["user_name"] or "neznámý učitel")
        cell["absence_times"].add(datetime_cz(dt.datetime.fromisoformat(absence["updated_at"])))

    ordered_cells = sorted(cells.values(), key=lambda cell: (cell["date"], cell["order"], cell["period"]))
    stats = {
        "total": sum(bool(cell["counted"]) for cell in ordered_cells),
        "present": sum(bool(cell["counted"] and cell["present"]) for cell in ordered_cells),
        "absent_marked": sum(
            bool(cell["counted"] and cell["absence_teachers"]) for cell in ordered_cells
        ),
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=";", lineterminator="\r\n")
    writer.writerow(["PŘEHLED PŘÍTOMNOSTI ASISTENTA", month])
    writer.writerow(["Odučené hodiny v rozvrhu", stats["total"]])
    writer.writerow(["Hodiny s přítomností asistenta", stats["present"]])
    share = (100 * stats["present"] / stats["total"]) if stats["total"] else 0
    writer.writerow(["Podíl přítomnosti", f"{share:.1f} %".replace(".", ",")])
    writer.writerow(["Hodiny s označenou absencí", stats["absent_marked"]])
    writer.writerow([])
    writer.writerow(
        [
            "Datum", "Den", "Hodina", "Čas", "Započteno do souhrnu", "Předmět / skupina",
            "Vyučující v rozvrhu", "Stav asistenta", "Zapsal asistenta",
            "Absence u předmětu / skupiny", "Absenci označil učitel", "Absence označena",
        ]
    )
    day_names = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
    for cell in ordered_cells:
        lesson_date = dt.date.fromisoformat(cell["date"])
        lessons = list(cell["lessons"].values())
        if cell["present"]:
            status = "PŘÍTOMEN"
        elif cell["absence_teachers"]:
            status = "NEPŘÍTOMEN – označeno učitelem"
        else:
            status = "NEPŘÍTOMEN – nezapsán"
        writer.writerow(
            [
                date_cz(lesson_date),
                day_names[lesson_date.weekday()],
                cell["period"],
                f"{cell['start']}–{cell['end']}" if cell["start"] or cell["end"] else "",
                "ANO" if cell["counted"] else "NE – hodina již není v aktuálním rozvrhu",
                " | ".join(item["name"] for item in lessons),
                " | ".join(filter(None, (item["teacher"] for item in lessons))),
                status,
                ", ".join(sorted(cell["assistant_writers"])),
                ", ".join(sorted(cell["absence_lessons"])),
                ", ".join(sorted(cell["absence_teachers"])),
                ", ".join(sorted(cell["absence_times"])),
            ]
        )
    return output.getvalue().encode("utf-8-sig"), stats


def smtp_ready() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def build_digest_lines(changes: list[sqlite3.Row], note: sqlite3.Row, teacher_message: sqlite3.Row) -> list[str]:
    lines = ["Souhrn změn v docházce asistenta pedagoga", ""]
    schedule_changes: dict[tuple[str, str, str], sqlite3.Row] = {}
    legacy_changes = []
    for change in changes:
        if change["event_type"] == "schedule" and change["lesson_date"]:
            key = (change["lesson_date"], change["period"] or "", change["lesson_key"] or "")
            schedule_changes[key] = change
        elif change["event_type"] == "legacy":
            legacy_changes.append(change)

    if schedule_changes:
        lines.append("ZMĚNY V ROZVRHU")
        for change in schedule_changes.values():
            lesson_date = dt.date.fromisoformat(change["lesson_date"])
            label = change["lesson_label"] or "Hodina"
            if change["group_label"]:
                label += f" – skupina {change['group_label']}"
            presence = "PŘÍTOMEN" if change["presence"] else "NEPŘÍTOMEN"
            lines.append(
                f"• {date_cz(lesson_date)}, {change['period']}. hodina – {label}: asistent bude {presence}."
            )
        lines.append("")

    lines.append("POZNÁMKA ASISTENTA")
    lines.append(note["text"] or "Bez aktuální poznámky.")
    if note["text"] and note["updated_by"]:
        lines.append(f"Naposledy upravil/a: {note['updated_by']}")
    lines.extend(["", "ZPRÁVA UČITELE PRO ASISTENTA"])
    lines.append(teacher_message["text"] or "Bez aktuální zprávy.")
    if teacher_message["text"] and teacher_message["updated_by"]:
        lines.append(f"Naposledy upravil/a: {teacher_message['updated_by']}")

    if legacy_changes:
        lines.extend(["", "STARŠÍ ZMĚNY"])
        lines.extend(f"• {change['description']}" for change in legacy_changes)
    lines.extend(["", "Toto je automatický denní souhrn."])
    return lines


def send_digest() -> bool:
    """Odešle všechny dosud neodeslané změny. Vrací True pouze po odeslání."""
    if not smtp_ready():
        return False
    with db_connect() as db:
        changes = db.execute(
            "SELECT * FROM change_log WHERE notified_at IS NULL ORDER BY id"
        ).fetchall()
        recipients = list(dict.fromkeys(
            row["email"]
            for row in db.execute(
                "SELECT email FROM users WHERE active=1 AND system=0 AND notify=1 AND email <> '' ORDER BY name"
            )
        ))
        note = db.execute("SELECT * FROM note WHERE id=1").fetchone()
        teacher_message = db.execute("SELECT * FROM teacher_message WHERE id=1").fetchone()
    if not changes or not recipients:
        return False

    message = EmailMessage()
    message["Subject"] = f"Asistent pedagoga – souhrn změn {date_cz(now_local().date())}"
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = os.environ["SMTP_FROM"]
    lines = build_digest_lines(changes, note, teacher_message)
    message.set_content("\n".join(lines))

    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    use_ssl = os.environ.get("SMTP_SSL", "0") == "1"
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with smtp_class(host, port, timeout=30) as smtp:
        if not use_ssl and os.environ.get("SMTP_TLS", "1") == "1":
            smtp.starttls(context=ssl.create_default_context())
        if os.environ.get("SMTP_USER"):
            smtp.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        smtp.send_message(message, to_addrs=recipients)
    sent_at = now_local().isoformat()
    with db_connect() as db:
        db.executemany(
            "UPDATE change_log SET notified_at=? WHERE id=?",
            [(sent_at, change["id"]) for change in changes],
        )
    return True


def notification_worker() -> None:
    last_attempt_date: dt.date | None = None
    while True:
        current = now_local()
        if current.hour >= 16 and current.date() != last_attempt_date:
            last_attempt_date = current.date()
            try:
                send_digest()
            except Exception as exc:
                print(f"E-mailový souhrn se nepodařilo odeslat: {exc}", flush=True)
        time.sleep(30)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AsistentRozvrhu/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, data: dict | list, status: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_bytes(body, "application/json; charset=utf-8", status, headers)

    def send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = 200,
        headers: dict | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 100_000:
            raise ValueError("Požadavek je příliš velký.")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Neplatný JSON požadavek.") from exc
        if not isinstance(value, dict):
            raise ValueError("Očekáván je JSON objekt.")
        return value

    def session_user(self) -> sqlite3.Row | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("assistant_session")
        if not token:
            return None
        with SESSION_LOCK:
            session = SESSIONS.get(token.value)
            if not session or session[1] < time.time():
                SESSIONS.pop(token.value, None)
                return None
        with db_connect() as db:
            return db.execute("SELECT * FROM users WHERE id=? AND active=1", (session[0],)).fetchone()

    def require_role(self, role: str) -> sqlite3.Row | None:
        user = self.session_user()
        if user is None:
            self.error_json("Nejprve se přihlaste.", HTTPStatus.UNAUTHORIZED)
            return None
        if user["role"] != role:
            label = "asistent" if role == "assistant" else "učitel"
            self.error_json(f"Tuto změnu může provést pouze {label}.", HTTPStatus.FORBIDDEN)
            return None
        return user

    def require_admin(self) -> bool:
        supplied = self.headers.get("X-Admin-Password", "")
        if not hmac.compare_digest(supplied, ADMIN_PASSWORD):
            self.error_json("Nesprávné heslo správce.", HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            if parsed.path == "/api/status":
                return self.get_status()
            if parsed.path == "/api/week":
                return self.get_week(parse_qs(parsed.query))
            if parsed.path == "/api/admin/settings":
                return self.get_admin_settings()
            if parsed.path == "/api/admin/months":
                return self.get_admin_months()
            if parsed.path == "/api/admin/export":
                return self.get_admin_export(parse_qs(parsed.query))
            if parsed.path == "/api/health":
                return self.send_json({"ok": True})
            return self.serve_static(parsed.path)
        except (ValueError, KeyError) as exc:
            self.error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Chyba GET {self.path}: {exc}", flush=True)
            self.error_json("Požadavek se nepodařilo zpracovat.", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/login":
                return self.post_login()
            if path == "/api/logout":
                return self.post_logout()
            if path == "/api/selection":
                return self.post_selection()
            if path == "/api/whole-class":
                return self.post_whole_class()
            if path == "/api/note":
                return self.post_note()
            if path == "/api/teacher-message":
                return self.post_teacher_message()
            if path == "/api/absence":
                return self.post_absence()
            if path == "/api/admin/users":
                return self.post_admin_user()
            if path == "/api/admin/send-digest":
                return self.post_send_digest()
            self.error_json("Adresa nebyla nalezena.", HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError) as exc:
            self.error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except sqlite3.IntegrityError:
            self.error_json("Uživatel s tímto jménem už existuje.", HTTPStatus.CONFLICT)
        except Exception as exc:
            print(f"Chyba POST {self.path}: {exc}", flush=True)
            self.error_json("Požadavek se nepodařilo zpracovat.", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/account":
                return self.patch_account()
            if path.startswith("/api/admin/users/"):
                return self.patch_admin_user(int(path.rsplit("/", 1)[1]))
            self.error_json("Adresa nebyla nalezena.", HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError) as exc:
            self.error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except sqlite3.IntegrityError:
            self.error_json("Uživatel s tímto jménem už existuje.", HTTPStatus.CONFLICT)
        except Exception as exc:
            print(f"Chyba PATCH {self.path}: {exc}", flush=True)
            self.error_json("Požadavek se nepodařilo zpracovat.", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/admin/users/"):
                return self.delete_admin_user(int(path.rsplit("/", 1)[1]))
            if path.startswith("/api/admin/months/"):
                return self.delete_admin_month(path.rsplit("/", 1)[1])
            self.error_json("Adresa nebyla nalezena.", HTTPStatus.NOT_FOUND)
        except (ValueError, KeyError) as exc:
            self.error_json(str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            print(f"Chyba DELETE {self.path}: {exc}", flush=True)
            self.error_json("Požadavek se nepodařilo zpracovat.", HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, path: str) -> None:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/timetable-data.js": ("timetable-data.js", "text/javascript; charset=utf-8"),
            "/timetable.json": ("timetable.json", "application/json; charset=utf-8"),
        }
        if path not in files:
            return self.error_json("Adresa nebyla nalezena.", HTTPStatus.NOT_FOUND)
        name, content_type = files[path]
        body = (BASE_DIR / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def get_status(self) -> None:
        user = self.session_user()
        with db_connect() as db:
            users = db.execute(
                "SELECT id, name, role FROM users WHERE active=1 AND system=0 ORDER BY name"
            ).fetchall()
            note = db.execute(
                """SELECT note.text, note.updated_at, COALESCE(NULLIF(note.updated_by, ''), users.name) AS user_name
                   FROM note LEFT JOIN users ON users.id=note.user_id WHERE note.id=1"""
            ).fetchone()
            teacher_message = db.execute(
                """SELECT teacher_message.text, teacher_message.updated_at,
                          COALESCE(NULLIF(teacher_message.updated_by, ''), users.name) AS user_name
                   FROM teacher_message LEFT JOIN users ON users.id=teacher_message.user_id
                   WHERE teacher_message.id=1"""
            ).fetchone()
        self.send_json(
            {
                "current_user": public_user(user),
                "login_users": [dict(row) for row in users],
                "note": dict(note) if note else {"text": ""},
                "teacher_message": dict(teacher_message) if teacher_message else {"text": ""},
                "smtp_ready": smtp_ready(),
                "admin_uses_default_password": ADMIN_PASSWORD == "zmenit-me",
            }
        )

    def get_week(self, query: dict) -> None:
        requested = query.get("start", [now_local().date().isoformat()])[0]
        start = monday_of(dt.date.fromisoformat(requested))
        refresh = query.get("refresh", ["0"])[0] == "1"
        payload, cached = get_cached_week(start, refresh=refresh)
        with db_connect() as db:
            selected = db.execute(
                """SELECT selections.*,
                          COALESCE(NULLIF(selections.updated_by, ''), users.name) AS user_name
                   FROM selections LEFT JOIN users ON users.id=selections.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (start.isoformat(), (start + dt.timedelta(days=4)).isoformat()),
            ).fetchall()
            exclusions = db.execute(
                """SELECT whole_class_exclusions.*,
                          COALESCE(NULLIF(whole_class_exclusions.updated_by, ''), users.name) AS user_name
                   FROM whole_class_exclusions LEFT JOIN users ON users.id=whole_class_exclusions.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (start.isoformat(), (start + dt.timedelta(days=4)).isoformat()),
            ).fetchall()
            absences = db.execute(
                """SELECT absences.*,
                          COALESCE(NULLIF(absences.updated_by, ''), users.name) AS user_name
                   FROM absences LEFT JOIN users ON users.id=absences.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (start.isoformat(), (start + dt.timedelta(days=4)).isoformat()),
            ).fetchall()
        payload["selections"] = [dict(row) for row in selected]
        payload["whole_class_exclusions"] = [dict(row) for row in exclusions]
        payload["absences"] = [dict(row) for row in absences]
        payload["from_cache"] = cached
        self.send_json(payload)

    def get_admin_settings(self) -> None:
        if not self.require_admin():
            return
        with db_connect() as db:
            users = db.execute("SELECT * FROM users WHERE system=0 ORDER BY name").fetchall()
        self.send_json({"users": [public_user(row) for row in users], "smtp_ready": smtp_ready()})

    def get_admin_months(self) -> None:
        if not self.require_admin():
            return
        current_month = now_local().date().replace(day=1).isoformat()[:7]
        with db_connect() as db:
            rows = db.execute(
                """SELECT substr(lesson_date, 1, 7) AS month, count(*) AS records
                   FROM (
                     SELECT lesson_date FROM selections
                     UNION ALL
                     SELECT lesson_date FROM whole_class_exclusions
                     UNION ALL
                     SELECT lesson_date FROM absences
                   )
                   WHERE substr(lesson_date, 1, 7) < ?
                   GROUP BY month ORDER BY month DESC""",
                (current_month,),
            ).fetchall()
        self.send_json({"months": [dict(row) for row in rows]})

    def get_admin_export(self, query: dict) -> None:
        if not self.require_admin():
            return
        month = query.get("month", [""])[0]
        body, _ = build_month_export(month)
        self.send_bytes(
            body,
            "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="asistent-{month}.csv"'},
        )

    def post_login(self) -> None:
        data = self.read_json()
        user_id = int(data.get("user_id", 0))
        pin = str(data.get("pin", ""))
        with db_connect() as db:
            user = db.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
        if user is None or not verify_pin(pin, user["pin_hash"]):
            return self.error_json("Nesprávný uživatel nebo PIN.", HTTPStatus.UNAUTHORIZED)
        token = secrets.token_urlsafe(32)
        with SESSION_LOCK:
            SESSIONS[token] = (user["id"], time.time() + 30 * 24 * 3600)
        self.send_json(
            {"user": public_user(user)},
            headers={"Set-Cookie": f"assistant_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000"},
        )

    def post_logout(self) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        token = cookie.get("assistant_session")
        if token:
            with SESSION_LOCK:
                SESSIONS.pop(token.value, None)
        self.send_json(
            {"ok": True},
            headers={"Set-Cookie": "assistant_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"},
        )

    def post_selection(self) -> None:
        user = self.require_role("assistant")
        if user is None:
            return
        data = self.read_json()
        lesson_date = dt.date.fromisoformat(str(data["date"]))
        period = str(data["period"])
        lesson_key = data.get("lesson_key")
        group_label = lesson_label = ""
        if lesson_key:
            week, _ = get_cached_week(monday_of(lesson_date))
            lesson = next(
                (
                    item for item in week["lessons"]
                    if item["key"] == lesson_key and item["date"] == lesson_date.isoformat()
                    and str(item["period"]) == period and item.get("groups")
                    and not item.get("removed")
                ),
                None,
            )
            if lesson is None:
                raise ValueError("Vybranou dělenou hodinu se nepodařilo ověřit.")
            group_label = ", ".join(lesson["groups"])
            lesson_label = lesson["subject"]
        stamp = now_local().isoformat()
        with db_connect() as db:
            previous = db.execute(
                "SELECT lesson_key, lesson_label, group_label FROM selections WHERE lesson_date=? AND period=?",
                (lesson_date.isoformat(), period),
            ).fetchone()
            if lesson_key:
                db.execute(
                    """INSERT INTO selections
                       (lesson_date, period, lesson_key, lesson_label, group_label, user_id, updated_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_date, period) DO UPDATE SET
                         lesson_key=excluded.lesson_key, lesson_label=excluded.lesson_label,
                         group_label=excluded.group_label, user_id=excluded.user_id,
                         updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
                    (
                        lesson_date.isoformat(), period, lesson_key, lesson_label,
                        group_label, user["id"], user["name"], stamp,
                    ),
                )
                description = (
                    f"{user['name']} zapsal asistenta do skupiny {group_label} "
                    f"({lesson_label}), {date_cz(lesson_date)}, {period}. hodina."
                )
                if previous is not None and previous["lesson_key"] != lesson_key:
                    log_schedule_change(
                        db, user, stamp,
                        f"{user['name']} zrušil asistenta ve skupině {previous['group_label']} "
                        f"({previous['lesson_label']}), {date_cz(lesson_date)}, {period}. hodina.",
                        lesson_date, period, previous["lesson_key"],
                        previous["lesson_label"], previous["group_label"], False,
                    )
                absent = db.execute(
                    """SELECT 1 FROM absences
                       WHERE lesson_date=? AND period=? AND lesson_key=?""",
                    (lesson_date.isoformat(), period, lesson_key),
                ).fetchone()
                changed_key = lesson_key
                changed_label = lesson_label
                changed_group = group_label
                present = absent is None
            else:
                db.execute(
                    "DELETE FROM selections WHERE lesson_date=? AND period=?",
                    (lesson_date.isoformat(), period),
                )
                if previous is None:
                    return self.send_json({"ok": True})
                description = (
                    f"{user['name']} zrušil asistenta ve skupině {previous['group_label']} "
                    f"({previous['lesson_label']}), {date_cz(lesson_date)}, {period}. hodina."
                )
                changed_key = previous["lesson_key"]
                changed_label = previous["lesson_label"]
                changed_group = previous["group_label"]
                present = False
            log_schedule_change(
                db, user, stamp, description, lesson_date, period, changed_key,
                changed_label, changed_group, present,
            )
        self.send_json({"ok": True})

    def post_note(self) -> None:
        user = self.require_role("assistant")
        if user is None:
            return
        text = str(self.read_json().get("text", "")).strip()
        if len(text) > 2000:
            raise ValueError("Poznámka může mít nejvýše 2000 znaků.")
        stamp = now_local().isoformat()
        with db_connect() as db:
            old = db.execute("SELECT text FROM note WHERE id=1").fetchone()["text"]
            if old == text:
                return self.send_json({"ok": True})
            db.execute(
                "UPDATE note SET text=?, user_id=?, updated_by=?, updated_at=? WHERE id=1",
                (text, user["id"], user["name"], stamp),
            )
            action = "upravil poznámku" if text else "smazal poznámku"
            db.execute(
                """INSERT INTO change_log
                   (created_at, user_id, description, event_type, message_kind, message_text)
                   VALUES (?, ?, ?, 'message', 'assistant_note', ?)""",
                (stamp, user["id"], f"{user['name']} {action}: {text or '—'}", text),
            )
        self.send_json({"ok": True})

    def post_teacher_message(self) -> None:
        user = self.require_role("teacher")
        if user is None:
            return
        text = str(self.read_json().get("text", "")).strip()
        if len(text) > 2000:
            raise ValueError("Zpráva může mít nejvýše 2000 znaků.")
        stamp = now_local().isoformat()
        with db_connect() as db:
            old = db.execute("SELECT text FROM teacher_message WHERE id=1").fetchone()["text"]
            if old == text:
                return self.send_json({"ok": True})
            db.execute(
                """UPDATE teacher_message
                   SET text=?, user_id=?, updated_by=?, updated_at=? WHERE id=1""",
                (text, user["id"], user["name"], stamp),
            )
            action = "upravil/a zprávu" if text else "smazal/a zprávu"
            db.execute(
                """INSERT INTO change_log
                   (created_at, user_id, description, event_type, message_kind, message_text)
                   VALUES (?, ?, ?, 'message', 'teacher_message', ?)""",
                (stamp, user["id"], f"{user['name']} {action} pro asistenta: {text or '—'}", text),
            )
        self.send_json({"ok": True})

    def post_absence(self) -> None:
        user = self.require_role("teacher")
        if user is None:
            return
        data = self.read_json()
        lesson_date = dt.date.fromisoformat(str(data["date"]))
        period = str(data["period"])
        lesson_key = str(data["lesson_key"])
        absent = bool(data.get("absent"))
        week, _ = get_cached_week(monday_of(lesson_date))
        lesson = next(
            (
                item for item in week["lessons"]
                if item["key"] == lesson_key and item["date"] == lesson_date.isoformat()
                and str(item["period"]) == period and not item.get("removed")
            ),
            None,
        )
        if lesson is None:
            raise ValueError("Hodinu se nepodařilo ověřit.")
        group_label = ", ".join(lesson.get("groups") or [])
        stamp = now_local().isoformat()
        with db_connect() as db:
            existing = db.execute(
                """SELECT 1 FROM absences
                   WHERE lesson_date=? AND period=? AND lesson_key=?""",
                (lesson_date.isoformat(), period, lesson_key),
            ).fetchone()
            if absent:
                db.execute(
                    """INSERT INTO absences
                       (lesson_date, period, lesson_key, lesson_label, group_label,
                        user_id, updated_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_date, period, lesson_key) DO UPDATE SET
                         lesson_label=excluded.lesson_label, group_label=excluded.group_label,
                         user_id=excluded.user_id, updated_by=excluded.updated_by,
                         updated_at=excluded.updated_at""",
                    (
                        lesson_date.isoformat(), period, lesson_key, lesson["subject"],
                        group_label, user["id"], user["name"], stamp,
                    ),
                )
                if existing is not None:
                    return self.send_json({"ok": True})
                present = False
                action = "označil/a nepřítomnost asistenta"
            else:
                db.execute(
                    "DELETE FROM absences WHERE lesson_date=? AND period=? AND lesson_key=?",
                    (lesson_date.isoformat(), period, lesson_key),
                )
                if existing is None:
                    return self.send_json({"ok": True})
                if lesson.get("groups"):
                    selection = db.execute(
                        """SELECT 1 FROM selections
                           WHERE lesson_date=? AND period=? AND lesson_key=?""",
                        (lesson_date.isoformat(), period, lesson_key),
                    ).fetchone()
                    present = selection is not None
                else:
                    exclusion = db.execute(
                        """SELECT 1 FROM whole_class_exclusions
                           WHERE lesson_date=? AND period=? AND lesson_key=?""",
                        (lesson_date.isoformat(), period, lesson_key),
                    ).fetchone()
                    present = exclusion is None
                action = "zrušil/a označení nepřítomnosti asistenta"
            description = (
                f"{user['name']} {action} ({lesson['subject']}), "
                f"{date_cz(lesson_date)}, {period}. hodina."
            )
            log_schedule_change(
                db, user, stamp, description, lesson_date, period, lesson_key,
                lesson["subject"], group_label, present,
            )
        self.send_json({"ok": True})

    def patch_account(self) -> None:
        user = self.session_user()
        if user is None:
            return self.error_json("Nejprve se přihlaste.", HTTPStatus.UNAUTHORIZED)
        data = self.read_json()
        fields = []
        values = []
        if "notify" in data:
            fields.append("notify=?")
            values.append(int(bool(data["notify"])))
        new_pin = str(data.get("new_pin", ""))
        if new_pin:
            if not verify_pin(str(data.get("current_pin", "")), user["pin_hash"]):
                return self.error_json("Současný PIN není správný.", HTTPStatus.UNAUTHORIZED)
            if len(new_pin) < 4:
                raise ValueError("Nový PIN musí mít alespoň 4 znaky.")
            fields.append("pin_hash=?")
            values.append(hash_pin(new_pin))
        if not fields:
            raise ValueError("Není co změnit.")
        values.append(user["id"])
        with db_connect() as db:
            db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
            updated = db.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        self.send_json({"user": public_user(updated)})

    def post_whole_class(self) -> None:
        user = self.require_role("assistant")
        if user is None:
            return
        data = self.read_json()
        lesson_date = dt.date.fromisoformat(str(data["date"]))
        period = str(data["period"])
        lesson_key = str(data["lesson_key"])
        present = bool(data.get("present"))
        week, _ = get_cached_week(monday_of(lesson_date))
        lesson = next(
            (
                item for item in week["lessons"]
                if item["key"] == lesson_key and item["date"] == lesson_date.isoformat()
                and str(item["period"]) == period and not item.get("groups")
                and not item.get("removed")
            ),
            None,
        )
        if lesson is None:
            raise ValueError("Hodinu celé třídy se nepodařilo ověřit.")
        stamp = now_local().isoformat()
        with db_connect() as db:
            existing = db.execute(
                """SELECT 1 FROM whole_class_exclusions
                   WHERE lesson_date=? AND period=? AND lesson_key=?""",
                (lesson_date.isoformat(), period, lesson_key),
            ).fetchone()
            if present:
                db.execute(
                    "DELETE FROM whole_class_exclusions WHERE lesson_date=? AND period=?",
                    (lesson_date.isoformat(), period),
                )
                if existing is None:
                    return self.send_json({"ok": True})
                description = (
                    f"{user['name']} znovu zapsal asistenta do celé třídy "
                    f"({lesson['subject']}), {date_cz(lesson_date)}, {period}. hodina."
                )
            else:
                db.execute(
                    """INSERT INTO whole_class_exclusions
                       (lesson_date, period, lesson_key, lesson_label, user_id, updated_by, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_date, period) DO UPDATE SET
                         lesson_key=excluded.lesson_key, lesson_label=excluded.lesson_label,
                         user_id=excluded.user_id, updated_by=excluded.updated_by,
                         updated_at=excluded.updated_at""",
                    (
                        lesson_date.isoformat(), period, lesson_key,
                        lesson["subject"], user["id"], user["name"], stamp,
                    ),
                )
                if existing is not None:
                    return self.send_json({"ok": True})
                description = (
                    f"{user['name']} odhlásil asistenta z hodiny celé třídy "
                    f"({lesson['subject']}), {date_cz(lesson_date)}, {period}. hodina."
                )
            absent = db.execute(
                """SELECT 1 FROM absences
                   WHERE lesson_date=? AND period=? AND lesson_key=?""",
                (lesson_date.isoformat(), period, lesson_key),
            ).fetchone()
            log_schedule_change(
                db, user, stamp, description, lesson_date, period, lesson_key,
                lesson["subject"], "", present and absent is None,
            )
        self.send_json({"ok": True})

    def post_admin_user(self) -> None:
        if not self.require_admin():
            return
        data = self.read_json()
        name = str(data.get("name", "")).strip()
        pin = str(data.get("pin", "")).strip()
        role = str(data.get("role", ""))
        if not name or len(name) > 100:
            raise ValueError("Zadejte jméno uživatele.")
        if len(pin) < 4:
            raise ValueError("PIN musí mít alespoň 4 znaky.")
        if role not in {"assistant", "teacher"}:
            raise ValueError("Vyberte roli asistent nebo učitel.")
        with db_connect() as db:
            cursor = db.execute(
                """INSERT INTO users
                   (name, email, pin_hash, can_edit, notify, active, role, system, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, 0, ?)""",
                (
                    name,
                    str(data.get("email", "")).strip(),
                    hash_pin(pin),
                    int(role == "assistant"),
                    int(bool(data.get("notify"))),
                    role,
                    now_local().isoformat(),
                ),
            )
            row = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        self.send_json({"user": public_user(row)}, HTTPStatus.CREATED)

    def patch_admin_user(self, user_id: int) -> None:
        if not self.require_admin():
            return
        data = self.read_json()
        allowed = {"name", "email", "notify", "role"}
        fields = []
        values = []
        for key in allowed:
            if key in data:
                if key == "role" and data[key] not in {"assistant", "teacher"}:
                    raise ValueError("Vyberte roli asistent nebo učitel.")
                fields.append(f"{key}=?")
                values.append(int(bool(data[key])) if key == "notify" else str(data[key]).strip())
        if "role" in data:
            fields.append("can_edit=?")
            values.append(int(data["role"] == "assistant"))
        if data.get("pin"):
            if len(str(data["pin"])) < 4:
                raise ValueError("PIN musí mít alespoň 4 znaky.")
            fields.append("pin_hash=?")
            values.append(hash_pin(str(data["pin"])))
        if not fields:
            raise ValueError("Není co změnit.")
        values.append(user_id)
        with db_connect() as db:
            db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id=?", values)
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            return self.error_json("Uživatel nebyl nalezen.", HTTPStatus.NOT_FOUND)
        self.send_json({"user": public_user(row)})

    def delete_admin_user(self, user_id: int) -> None:
        if not self.require_admin():
            return
        with db_connect() as db:
            user = db.execute(
                "SELECT id FROM users WHERE id=? AND system=0", (user_id,)
            ).fetchone()
            if user is None:
                return self.error_json("Uživatel nebyl nalezen.", HTTPStatus.NOT_FOUND)
            tombstone_id = deleted_user_id(db)
            db.execute("UPDATE selections SET user_id=? WHERE user_id=?", (tombstone_id, user_id))
            db.execute(
                "UPDATE whole_class_exclusions SET user_id=? WHERE user_id=?",
                (tombstone_id, user_id),
            )
            db.execute("UPDATE absences SET user_id=NULL WHERE user_id=?", (user_id,))
            db.execute("UPDATE change_log SET user_id=NULL WHERE user_id=?", (user_id,))
            db.execute("UPDATE note SET user_id=NULL WHERE user_id=?", (user_id,))
            db.execute("UPDATE teacher_message SET user_id=NULL WHERE user_id=?", (user_id,))
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
        with SESSION_LOCK:
            for token, session in list(SESSIONS.items()):
                if session[0] == user_id:
                    SESSIONS.pop(token, None)
        self.send_json({"ok": True})

    def delete_admin_month(self, month: str) -> None:
        if not self.require_admin():
            return
        try:
            month_start = dt.date.fromisoformat(month + "-01")
        except ValueError as exc:
            raise ValueError("Neplatný měsíc.") from exc
        current_start = now_local().date().replace(day=1)
        if month_start >= current_start:
            raise ValueError("Aktuální ani budoucí měsíc nelze smazat.")
        with db_connect() as db:
            selected = db.execute("DELETE FROM selections WHERE substr(lesson_date, 1, 7)=?", (month,)).rowcount
            excluded = db.execute(
                "DELETE FROM whole_class_exclusions WHERE substr(lesson_date, 1, 7)=?", (month,)
            ).rowcount
            absent = db.execute(
                "DELETE FROM absences WHERE substr(lesson_date, 1, 7)=?", (month,)
            ).rowcount
            db.execute(
                "DELETE FROM change_log WHERE substr(created_at, 1, 7)=? AND notified_at IS NOT NULL",
                (month,),
            )
        self.send_json({"ok": True, "deleted": selected + excluded + absent})

    def post_send_digest(self) -> None:
        if not self.require_admin():
            return
        if not smtp_ready():
            raise ValueError("SMTP není nastaveno.")
        sent = send_digest()
        self.send_json({"ok": True, "sent": sent})


def run() -> None:
    init_database()
    threading.Thread(target=notification_worker, name="email-digest", daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), AppHandler)
    print(f"Asistent pedagoga běží na http://{HOST}:{PORT}", flush=True)
    if ADMIN_PASSWORD == "zmenit-me":
        print("UPOZORNĚNÍ: používá se výchozí heslo správce 'zmenit-me'.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer ukončen.")
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
