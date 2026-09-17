"""Webová aplikace pro evidenci asistenta pedagoga.

Používá jen standardní knihovnu Pythonu. Spuštění: ``python server.py``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
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


def db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=10)
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
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS selections (
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                lesson_key TEXT NOT NULL,
                lesson_label TEXT NOT NULL,
                group_label TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (lesson_date, period)
            );
            CREATE TABLE IF NOT EXISTS whole_class_exclusions (
                lesson_date TEXT NOT NULL,
                period TEXT NOT NULL,
                lesson_key TEXT NOT NULL,
                lesson_label TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id),
                updated_at TEXT NOT NULL,
                PRIMARY KEY (lesson_date, period)
            );
            CREATE TABLE IF NOT EXISTS note (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                text TEXT NOT NULL DEFAULT '',
                user_id INTEGER REFERENCES users(id),
                updated_at TEXT
            );
            INSERT OR IGNORE INTO note (id, text) VALUES (1, '');
            CREATE TABLE IF NOT EXISTS change_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id),
                description TEXT NOT NULL,
                notified_at TEXT
            );
            CREATE TABLE IF NOT EXISTS timetable_cache (
                week_start TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            """
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
        "can_edit": bool(row["can_edit"]),
        "notify": bool(row["notify"]),
        "active": bool(row["active"]),
    }


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


def smtp_ready() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send_digest() -> bool:
    """Odešle všechny dosud neodeslané změny. Vrací True pouze po odeslání."""
    if not smtp_ready():
        return False
    with db_connect() as db:
        changes = db.execute(
            "SELECT id, created_at, description FROM change_log WHERE notified_at IS NULL ORDER BY id"
        ).fetchall()
        recipients = [
            row["email"]
            for row in db.execute(
                "SELECT email FROM users WHERE active=1 AND notify=1 AND email <> '' ORDER BY name"
            )
        ]
    if not changes or not recipients:
        return False

    message = EmailMessage()
    message["Subject"] = f"Asistent pedagoga – souhrn změn {date_cz(now_local().date())}"
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = os.environ["SMTP_FROM"]
    lines = ["Dnes byly v zápisu asistenta pedagoga provedeny tyto změny:", ""]
    for change in changes:
        stamp = datetime_cz(dt.datetime.fromisoformat(change["created_at"]))
        lines.append(f"• {stamp} – {change['description']}")
    lines.extend(["", "Toto je automatický denní souhrn."])
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
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

    def require_editor(self) -> sqlite3.Row | None:
        user = self.session_user()
        if user is None:
            self.error_json("Nejprve se přihlaste.", HTTPStatus.UNAUTHORIZED)
            return None
        if not user["can_edit"]:
            self.error_json("Tento uživatel nemá právo zapisovat.", HTTPStatus.FORBIDDEN)
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
            users = db.execute("SELECT id, name, can_edit FROM users WHERE active=1 ORDER BY name").fetchall()
            note = db.execute(
                """SELECT note.text, note.updated_at, users.name AS user_name
                   FROM note LEFT JOIN users ON users.id=note.user_id WHERE note.id=1"""
            ).fetchone()
        self.send_json(
            {
                "current_user": public_user(user),
                "login_users": [dict(row) for row in users if row["can_edit"]],
                "note": dict(note) if note else {"text": ""},
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
                """SELECT selections.*, users.name AS user_name FROM selections
                   JOIN users ON users.id=selections.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (start.isoformat(), (start + dt.timedelta(days=4)).isoformat()),
            ).fetchall()
            exclusions = db.execute(
                """SELECT whole_class_exclusions.*, users.name AS user_name
                   FROM whole_class_exclusions
                   JOIN users ON users.id=whole_class_exclusions.user_id
                   WHERE lesson_date BETWEEN ? AND ?""",
                (start.isoformat(), (start + dt.timedelta(days=4)).isoformat()),
            ).fetchall()
        payload["selections"] = [dict(row) for row in selected]
        payload["whole_class_exclusions"] = [dict(row) for row in exclusions]
        payload["from_cache"] = cached
        self.send_json(payload)

    def get_admin_settings(self) -> None:
        if not self.require_admin():
            return
        with db_connect() as db:
            users = db.execute("SELECT * FROM users ORDER BY name").fetchall()
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
                   )
                   WHERE substr(lesson_date, 1, 7) < ?
                   GROUP BY month ORDER BY month DESC""",
                (current_month,),
            ).fetchall()
        self.send_json({"months": [dict(row) for row in rows]})

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
        user = self.require_editor()
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
                "SELECT lesson_label, group_label FROM selections WHERE lesson_date=? AND period=?",
                (lesson_date.isoformat(), period),
            ).fetchone()
            if lesson_key:
                db.execute(
                    """INSERT INTO selections
                       (lesson_date, period, lesson_key, lesson_label, group_label, user_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_date, period) DO UPDATE SET
                         lesson_key=excluded.lesson_key, lesson_label=excluded.lesson_label,
                         group_label=excluded.group_label, user_id=excluded.user_id, updated_at=excluded.updated_at""",
                    (lesson_date.isoformat(), period, lesson_key, lesson_label, group_label, user["id"], stamp),
                )
                description = (
                    f"{user['name']} zapsal asistenta do skupiny {group_label} "
                    f"({lesson_label}), {date_cz(lesson_date)}, {period}. hodina."
                )
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
            db.execute(
                "INSERT INTO change_log (created_at, user_id, description) VALUES (?, ?, ?)",
                (stamp, user["id"], description),
            )
        self.send_json({"ok": True})

    def post_note(self) -> None:
        user = self.require_editor()
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
            db.execute("UPDATE note SET text=?, user_id=?, updated_at=? WHERE id=1", (text, user["id"], stamp))
            action = "upravil poznámku" if text else "smazal poznámku"
            db.execute(
                "INSERT INTO change_log (created_at, user_id, description) VALUES (?, ?, ?)",
                (stamp, user["id"], f"{user['name']} {action}: {text or '—'}"),
            )
        self.send_json({"ok": True})

    def post_whole_class(self) -> None:
        user = self.require_editor()
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
                       (lesson_date, period, lesson_key, lesson_label, user_id, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(lesson_date, period) DO UPDATE SET
                         lesson_key=excluded.lesson_key, lesson_label=excluded.lesson_label,
                         user_id=excluded.user_id, updated_at=excluded.updated_at""",
                    (
                        lesson_date.isoformat(), period, lesson_key,
                        lesson["subject"], user["id"], stamp,
                    ),
                )
                if existing is not None:
                    return self.send_json({"ok": True})
                description = (
                    f"{user['name']} odhlásil asistenta z hodiny celé třídy "
                    f"({lesson['subject']}), {date_cz(lesson_date)}, {period}. hodina."
                )
            db.execute(
                "INSERT INTO change_log (created_at, user_id, description) VALUES (?, ?, ?)",
                (stamp, user["id"], description),
            )
        self.send_json({"ok": True})

    def post_admin_user(self) -> None:
        if not self.require_admin():
            return
        data = self.read_json()
        name = str(data.get("name", "")).strip()
        pin = str(data.get("pin", "")).strip()
        if not name or len(name) > 100:
            raise ValueError("Zadejte jméno uživatele.")
        if len(pin) < 4:
            raise ValueError("PIN musí mít alespoň 4 znaky.")
        with db_connect() as db:
            cursor = db.execute(
                """INSERT INTO users (name, email, pin_hash, can_edit, notify, active, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (
                    name,
                    str(data.get("email", "")).strip(),
                    hash_pin(pin),
                    int(bool(data.get("can_edit"))),
                    int(bool(data.get("notify"))),
                    now_local().isoformat(),
                ),
            )
            row = db.execute("SELECT * FROM users WHERE id=?", (cursor.lastrowid,)).fetchone()
        self.send_json({"user": public_user(row)}, HTTPStatus.CREATED)

    def patch_admin_user(self, user_id: int) -> None:
        if not self.require_admin():
            return
        data = self.read_json()
        allowed = {"name", "email", "can_edit", "notify", "active"}
        fields = []
        values = []
        for key in allowed:
            if key in data:
                fields.append(f"{key}=?")
                values.append(int(bool(data[key])) if key in {"can_edit", "notify", "active"} else str(data[key]).strip())
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
            used = db.execute(
                """SELECT 1 WHERE
                   EXISTS (SELECT 1 FROM selections WHERE user_id=?) OR
                   EXISTS (SELECT 1 FROM whole_class_exclusions WHERE user_id=?) OR
                   EXISTS (SELECT 1 FROM change_log WHERE user_id=?) OR
                   EXISTS (SELECT 1 FROM note WHERE user_id=?)""",
                (user_id, user_id, user_id, user_id),
            ).fetchone()
            if used:
                db.execute("UPDATE users SET active=0, can_edit=0, notify=0 WHERE id=?", (user_id,))
            else:
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
            db.execute(
                "DELETE FROM change_log WHERE substr(created_at, 1, 7)=? AND notified_at IS NOT NULL",
                (month,),
            )
        self.send_json({"ok": True, "deleted": selected + excluded})

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
