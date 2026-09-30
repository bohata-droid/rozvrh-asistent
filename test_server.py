import http.cookiejar
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import server


class QuietHandler(server.AppHandler):
    def log_message(self, fmt: str, *args) -> None:
        pass


class SharedStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.original_db_path = server.DB_PATH
        handle, db_path = tempfile.mkstemp(suffix=".db", dir=Path(__file__).parent)
        os.close(handle)
        self.db_path = Path(db_path)
        server.DB_PATH = self.db_path
        self.original_get_cached_week = server.get_cached_week
        server.get_cached_week = lambda start, refresh=False: (
            {
                "range_start": "2026-09-28",
                "range_end": "2026-10-02",
                "updated_at": server.now_local().isoformat(),
                "periods": [{"period": "1"}],
                "lessons": [
                    {
                        "date": "2026-09-28",
                        "period": "1",
                        "key": "lesson-a",
                        "subject": "Test",
                        "groups": ["A"],
                    }
                ],
            },
            False,
        )
        server.SESSIONS.clear()
        server.init_database()
        with server.db_connect() as db:
            db.execute(
                """INSERT INTO users
                   (name, email, pin_hash, can_edit, notify, active, created_at)
                   VALUES (?, '', ?, 1, 0, 1, ?)""",
                ("Testovací asistent", server.hash_pin("1234"), server.now_local().isoformat()),
            )
            self.user_id = db.execute("SELECT id FROM users").fetchone()["id"]
            db.execute(
                """INSERT INTO users
                   (name, email, pin_hash, can_edit, notify, active, role, created_at)
                   VALUES (?, ?, ?, 0, 0, 1, 'teacher', ?)""",
                (
                    "Testovací učitel",
                    "ucitel@example.test",
                    server.hash_pin("5678"),
                    server.now_local().isoformat(),
                ),
            )
            self.teacher_id = db.execute(
                "SELECT id FROM users WHERE role='teacher'"
            ).fetchone()["id"]

        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        for suffix in ("", "-shm", "-wal"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)
        server.DB_PATH = self.original_db_path
        server.get_cached_week = self.original_get_cached_week

    @staticmethod
    def opener() -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self,
        opener,
        path: str,
        body: dict | None = None,
        *,
        method: str | None = None,
        headers: dict | None = None,
    ) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request_headers = {"Content-Type": "application/json"} if data else {}
        request_headers.update(headers or {})
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers=request_headers,
            method=method,
        )
        with opener.open(request, timeout=2) as response:
            return json.load(response)

    def test_change_from_one_client_is_visible_to_another(self) -> None:
        writer = self.opener()
        reader = self.opener()

        self.request(writer, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(writer, "/api/note", {"text": "Sdílená poznámka"})

        status = self.request(reader, "/api/status")
        self.assertEqual(status["note"]["text"], "Sdílená poznámka")
        self.assertEqual(status["note"]["user_name"], "Testovací asistent")

    def test_schedule_change_from_one_client_is_visible_to_another(self) -> None:
        writer = self.opener()
        reader = self.opener()

        self.request(writer, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(
            writer,
            "/api/selection",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
        )

        week = self.request(reader, "/api/week?start=2026-09-28")
        self.assertEqual(len(week["selections"]), 1)
        self.assertEqual(week["selections"][0]["lesson_key"], "lesson-a")
        self.assertEqual(week["selections"][0]["user_name"], "Testovací asistent")

    def test_user_can_change_pin_and_email_subscription(self) -> None:
        client = self.opener()
        self.request(client, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        result = self.request(
            client,
            "/api/account",
            {"current_pin": "1234", "new_pin": "4321", "notify": True},
            method="PATCH",
        )
        self.assertTrue(result["user"]["notify"])

        fresh_client = self.opener()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(fresh_client, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.assertEqual(error.exception.code, 401)
        error.exception.close()
        login = self.request(fresh_client, "/api/login", {"user_id": self.user_id, "pin": "4321"})
        self.assertEqual(login["user"]["role"], "assistant")

    def test_teacher_can_write_message_and_mark_absence(self) -> None:
        teacher = self.opener()
        assistant = self.opener()
        reader = self.opener()
        self.request(teacher, "/api/login", {"user_id": self.teacher_id, "pin": "5678"})
        self.request(assistant, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(teacher, "/api/teacher-message", {"text": "Prosím o doprovod."})
        self.request(
            teacher,
            "/api/absence",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a", "absent": True},
        )
        # Pozdější zápis asistenta nesmí učitelovu absenci odstranit.
        self.request(
            assistant,
            "/api/selection",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
        )

        status = self.request(reader, "/api/status")
        week = self.request(reader, "/api/week?start=2026-09-28")
        self.assertEqual(status["teacher_message"]["text"], "Prosím o doprovod.")
        self.assertEqual(status["teacher_message"]["user_name"], "Testovací učitel")
        self.assertEqual(week["selections"][0]["lesson_key"], "lesson-a")
        self.assertEqual(week["absences"][0]["lesson_key"], "lesson-a")

    def test_admin_deletes_account_but_keeps_history(self) -> None:
        writer = self.opener()
        admin = self.opener()
        self.request(writer, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(
            writer,
            "/api/selection",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
        )
        self.request(
            admin,
            f"/api/admin/users/{self.user_id}",
            method="DELETE",
            headers={"X-Admin-Password": server.ADMIN_PASSWORD},
        )

        settings = self.request(
            admin,
            "/api/admin/settings",
            headers={"X-Admin-Password": server.ADMIN_PASSWORD},
        )
        week = self.request(admin, "/api/week?start=2026-09-28")
        self.assertNotIn(self.user_id, [user["id"] for user in settings["users"]])
        self.assertEqual(week["selections"][0]["user_name"], "Testovací asistent")
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(self.opener(), "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

    def test_admin_creates_and_changes_user_role(self) -> None:
        admin = self.opener()
        headers = {"X-Admin-Password": server.ADMIN_PASSWORD}
        created = self.request(
            admin,
            "/api/admin/users",
            {
                "name": "Nový učitel",
                "email": "novy@example.test",
                "pin": "9999",
                "role": "teacher",
                "notify": True,
            },
            headers=headers,
        )["user"]
        self.assertEqual(created["role"], "teacher")
        self.assertFalse(created["can_edit"])

        changed = self.request(
            admin,
            f"/api/admin/users/{created['id']}",
            {"role": "assistant"},
            method="PATCH",
            headers=headers,
        )["user"]
        self.assertEqual(changed["role"], "assistant")
        self.assertTrue(changed["can_edit"])

    def test_roles_have_separate_permissions(self) -> None:
        assistant = self.opener()
        teacher = self.opener()
        self.request(assistant, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(teacher, "/api/login", {"user_id": self.teacher_id, "pin": "5678"})

        forbidden_requests = [
            (assistant, "/api/teacher-message", {"text": "Nemá projít"}),
            (
                assistant,
                "/api/absence",
                {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a", "absent": True},
            ),
            (teacher, "/api/note", {"text": "Nemá projít"}),
            (
                teacher,
                "/api/selection",
                {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
            ),
        ]
        for client, path, body in forbidden_requests:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(client, path, body)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()

    def test_digest_contains_date_presence_note_and_teacher_message(self) -> None:
        teacher = self.opener()
        assistant = self.opener()
        self.request(teacher, "/api/login", {"user_id": self.teacher_id, "pin": "5678"})
        self.request(assistant, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(assistant, "/api/note", {"text": "Poznámka asistenta"})
        self.request(teacher, "/api/teacher-message", {"text": "Zpráva učitele"})
        self.request(
            teacher,
            "/api/absence",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a", "absent": True},
        )
        with server.db_connect() as db:
            changes = db.execute("SELECT * FROM change_log ORDER BY id").fetchall()
            note = db.execute("SELECT * FROM note WHERE id=1").fetchone()
            message = db.execute("SELECT * FROM teacher_message WHERE id=1").fetchone()
        digest = "\n".join(server.build_digest_lines(changes, note, message))
        self.assertIn("28. 9. 2026", digest)
        self.assertIn("NEPŘÍTOMEN", digest)
        self.assertIn("Poznámka asistenta", digest)
        self.assertIn("Zpráva učitele", digest)

    def test_month_export_counts_presence_and_identifies_absence_teacher(self) -> None:
        assistant = self.opener()
        teacher = self.opener()
        self.request(assistant, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(teacher, "/api/login", {"user_id": self.teacher_id, "pin": "5678"})
        self.request(
            assistant,
            "/api/selection",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
        )

        _, before = server.build_month_export("2026-09")
        self.assertEqual(before, {"total": 1, "present": 1, "absent_marked": 0})

        self.request(
            teacher,
            "/api/absence",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a", "absent": True},
        )
        csv_bytes, after = server.build_month_export("2026-09")
        csv_text = csv_bytes.decode("utf-8-sig")
        self.assertEqual(after, {"total": 1, "present": 0, "absent_marked": 1})
        self.assertIn("NEPŘÍTOMEN – označeno učitelem", csv_text)
        self.assertIn("Testovací učitel", csv_text)
        self.assertIn("28. 9. 2026", csv_text)

        request = urllib.request.Request(
            self.base_url + "/api/admin/export?month=2026-09",
            headers={"X-Admin-Password": server.ADMIN_PASSWORD},
        )
        with self.opener().open(request, timeout=2) as response:
            self.assertEqual(response.headers.get_content_type(), "text/csv")
            self.assertIn("asistent-2026-09.csv", response.headers["Content-Disposition"])
            self.assertTrue(response.read().startswith(b"\xef\xbb\xbf"))

    def test_month_export_counts_parallel_two_period_block_once(self) -> None:
        assistant = self.opener()
        self.request(assistant, "/api/login", {"user_id": self.user_id, "pin": "1234"})
        self.request(
            assistant,
            "/api/selection",
            {"date": "2026-09-28", "period": "1", "lesson_key": "lesson-a"},
        )
        current_fetch = server.get_cached_week
        server.get_cached_week = lambda start, refresh=False: (
            {
                "periods": [
                    {"period": "1", "start": "8:00", "end": "8:45"},
                    {"period": "2", "start": "8:55", "end": "9:40"},
                ],
                "lessons": [
                    {
                        "date": "2026-09-28", "period": "1", "key": "lesson-a",
                        "subject": "AJ", "groups": ["A"], "teacher": "Novák",
                        "duration_periods": 2, "removed": False,
                    },
                    {
                        "date": "2026-09-28", "period": "1", "key": "lesson-b",
                        "subject": "AJ", "groups": ["B"], "teacher": "Svobodová",
                        "duration_periods": 2, "removed": False,
                    },
                    {
                        "date": "2026-09-29", "period": "1", "key": "cancelled",
                        "subject": "M", "groups": [], "teacher": "Novák",
                        "duration_periods": 1, "removed": True,
                    },
                ],
            },
            False,
        )
        try:
            _, stats = server.build_month_export("2026-09")
        finally:
            server.get_cached_week = current_fetch
        self.assertEqual(stats, {"total": 2, "present": 2, "absent_marked": 0})

    def test_legacy_users_are_migrated_to_roles(self) -> None:
        handle, legacy_path = tempfile.mkstemp(suffix=".db", dir=Path(__file__).parent)
        os.close(handle)
        legacy_path = Path(legacy_path)
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """CREATE TABLE users (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                 email TEXT NOT NULL DEFAULT '',
                 pin_hash TEXT NOT NULL,
                 can_edit INTEGER NOT NULL DEFAULT 0,
                 notify INTEGER NOT NULL DEFAULT 0,
                 active INTEGER NOT NULL DEFAULT 1,
                 created_at TEXT NOT NULL
               );
               INSERT INTO users (name, pin_hash, can_edit, created_at)
               VALUES ('Původní asistent', 'x', 1, '2026-01-01');
               INSERT INTO users (name, pin_hash, can_edit, created_at)
               VALUES ('Původní příjemce', 'x', 0, '2026-01-01');"""
        )
        connection.commit()
        connection.close()
        current_path = server.DB_PATH
        try:
            server.DB_PATH = legacy_path
            server.init_database()
            with server.db_connect() as db:
                roles = {
                    row["name"]: row["role"]
                    for row in db.execute("SELECT name, role FROM users WHERE system=0")
                }
            self.assertEqual(roles["Původní asistent"], "assistant")
            self.assertEqual(roles["Původní příjemce"], "teacher")
        finally:
            server.DB_PATH = current_path
            for suffix in ("", "-shm", "-wal"):
                Path(f"{legacy_path}{suffix}").unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
