import asyncio
import tempfile
import unittest
from pathlib import Path

import okr_api
import okr_db
from fastapi import HTTPException


class OkrSafeDeleteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = okr_db.DB_PATH
        okr_db.DB_PATH = str(Path(self.tempdir.name) / "okr.db")
        okr_db.init_db()
        self.actor = {
            "id": 99999,
            "role": "superAdmin",
            "company_id": None,
        }

    def tearDown(self):
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def create_user(self, suffix: str, role: str = "user") -> int:
        with okr_db.get_db() as db:
            cursor = db.execute(
                "INSERT INTO users(phone, password_hash, name, role, status) "
                "VALUES (?, 'hash', ?, ?, 'active')",
                (f"1390000{suffix}", f"user-{suffix}", role),
            )
            return cursor.lastrowid

    def test_database_delete_reports_okr_dependencies_and_keeps_user(self):
        uid = self.create_user("0001")
        with okr_db.get_db() as db:
            db.execute(
                "INSERT INTO okr_objectives(title, type, owner_id, period) "
                "VALUES ('test objective', 'personal', ?, '2026-Q3')",
                (uid,),
            )

        with self.assertRaises(HTTPException) as context:
            asyncio.run(okr_api.admin_db_delete_row("users", uid, self.actor))

        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("OKR \u76ee\u6807 1 \u6761", context.exception.detail)
        self.assertIn("disabled", context.exception.detail)
        with okr_db.get_db() as db:
            self.assertIsNotNone(
                db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
            )

    def test_user_management_delete_uses_same_dependency_policy(self):
        uid = self.create_user("0002")
        with okr_db.get_db() as db:
            db.execute(
                "INSERT INTO audit_logs(user_id, user_name, action, target_type) "
                "VALUES (?, 'test', 'login', 'user')",
                (uid,),
            )

        with self.assertRaises(HTTPException) as context:
            asyncio.run(okr_api.admin_delete_user(uid, self.actor))

        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("\u64cd\u4f5c\u65e5\u5fd7 1 \u6761", context.exception.detail)

    def test_unreferenced_user_can_be_deleted(self):
        uid = self.create_user("0003")
        result = asyncio.run(okr_api.admin_db_delete_row("users", uid, self.actor))
        self.assertTrue(result["ok"])
        with okr_db.get_db() as db:
            self.assertIsNone(
                db.execute("SELECT id FROM users WHERE id = ?", (uid,)).fetchone()
            )

    def test_database_manager_cannot_delete_superadmin(self):
        uid = self.create_user("0004", "superAdmin")
        with self.assertRaises(HTTPException) as context:
            asyncio.run(okr_api.admin_db_delete_row("users", uid, self.actor))
        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("\u4e0d\u53ef\u5220\u9664", context.exception.detail)


if __name__ == "__main__":
    unittest.main()
