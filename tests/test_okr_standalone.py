import contextlib
import io
import json
from pathlib import Path
import secrets
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app
from okr import db


class StandaloneTests(unittest.TestCase):
    def test_missing_config_fails_before_initializing_a_database(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(app, "CONFIG_PATH", Path(temp) / "missing.json"):
                with self.assertRaisesRegex(RuntimeError, "setup.py"):
                    with TestClient(app.app):
                        pass
            self.assertEqual([], list(Path(temp).iterdir()))

    def test_standalone_startup_pages_and_bootstrap_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"jwt_secret": secrets.token_urlsafe(48)}), encoding="utf-8")
            chosen_password = secrets.token_urlsafe(24)
            with patch.object(app, "CONFIG_PATH", config), patch.object(db, "DB_DIR", temp), \
                 patch.object(db, "DB_PATH", str(Path(temp) / "okr.db")), \
                 patch.dict("os.environ", {"OKR_BOOTSTRAP_PHONE": "13900000001", "OKR_BOOTSTRAP_PASSWORD": chosen_password}):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), TestClient(app.app) as client:
                    self.assertEqual({"status": "ok"}, client.get("/healthz").json())
                    self.assertEqual("/okr", client.get("/", follow_redirects=False).headers["location"])
                    self.assertEqual(200, client.get("/okr").status_code)
                    self.assertEqual(200, client.get("/static/okr/logo.svg").status_code)
                    self.assertEqual(401, client.get("/api/okr/auth/me").status_code)
                    self.assertEqual(404, client.get("/crm").status_code)
                    self.assertEqual(404, client.get("/config.json").status_code)
                self.assertNotIn(chosen_password, output.getvalue())
                with db.get_db() as connection:
                    row = connection.execute("SELECT phone,password_hash FROM users WHERE role='superAdmin'").fetchone()
                from okr.auth import verify_password
                self.assertEqual("13900000001", row["phone"])
                self.assertTrue(verify_password(chosen_password, row["password_hash"]))
