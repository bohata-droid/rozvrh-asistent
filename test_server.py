import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
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

    def request(self, opener, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
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


if __name__ == "__main__":
    unittest.main()
