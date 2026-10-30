import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import okr_api
import okr_db


class OkrWithdrawReviewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = okr_db.DB_PATH
        okr_db.DB_PATH = str(Path(self.tempdir.name) / "okr.db")
        okr_db.init_db()

        with okr_db.get_db() as db:
            self.company_id = db.execute(
                "INSERT INTO companies(name, status) VALUES ('撤回测试公司', 'active')"
            ).lastrowid
            self.head_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) "
                "VALUES ('13820000001','x','审核人','dept_head',?,'active')",
                (self.company_id,),
            ).lastrowid
            self.owner_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) "
                "VALUES ('13820000002','x','提交人','user',?,'active')",
                (self.company_id,),
            ).lastrowid
            self.other_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) "
                "VALUES ('13820000003','x','其他成员','user',?,'active')",
                (self.company_id,),
            ).lastrowid
            db.execute(
                "UPDATE companies SET head_user_id = ? WHERE id = ?",
                (self.head_id, self.company_id),
            )
            self.objective_id = db.execute(
                "INSERT INTO okr_objectives(title,type,owner_id,company_id,status,period) "
                "VALUES ('待撤回目标','personal',?,?,'pending','2026-08')",
                (self.owner_id, self.company_id),
            ).lastrowid
            self.kr_id = db.execute(
                "INSERT INTO okr_key_results(objective_id,title) VALUES (?, '保留的 KR')",
                (self.objective_id,),
            ).lastrowid
            self.review_id = db.execute(
                "INSERT INTO okr_reviews(objective_id,reviewer_id,status) "
                "VALUES (?,?,'pending')",
                (self.objective_id, self.head_id),
            ).lastrowid

        self.owner = {
            "id": self.owner_id,
            "name": "提交人",
            "role": "user",
            "company_id": self.company_id,
        }
        self.other = {
            "id": self.other_id,
            "name": "其他成员",
            "role": "user",
            "company_id": self.company_id,
        }
        app = FastAPI()
        app.include_router(okr_api.okr_router)
        app.dependency_overrides[okr_api.get_current_user] = lambda: self.owner
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_withdraw_keeps_objective_and_kr_then_allows_resubmit(self):
        response = self.client.post(
            f"/api/okr/objectives/{self.objective_id}/withdraw-review"
        )
        self.assertEqual(200, response.status_code)
        self.assertIn("退回草稿", response.json()["msg"])

        with okr_db.get_db() as db:
            objective = db.execute(
                "SELECT status FROM okr_objectives WHERE id = ?", (self.objective_id,)
            ).fetchone()
            kr = db.execute(
                "SELECT id FROM okr_key_results WHERE id = ?", (self.kr_id,)
            ).fetchone()
            pending_review = db.execute(
                "SELECT id FROM okr_reviews WHERE id = ?", (self.review_id,)
            ).fetchone()
        self.assertEqual("draft", objective["status"])
        self.assertIsNotNone(kr)
        self.assertIsNone(pending_review)

        asyncio.run(okr_api.submit_review(self.objective_id, self.owner))
        with okr_db.get_db() as db:
            status = db.execute(
                "SELECT status FROM okr_objectives WHERE id = ?", (self.objective_id,)
            ).fetchone()[0]
            pending_count = db.execute(
                "SELECT COUNT(*) FROM okr_reviews WHERE objective_id = ? AND status = 'pending'",
                (self.objective_id,),
            ).fetchone()[0]
        self.assertEqual("pending", status)
        self.assertEqual(1, pending_count)

    def test_other_user_cannot_withdraw_review(self):
        with self.assertRaisesRegex(HTTPException, "只能撤回自己的") as context:
            asyncio.run(okr_api.withdraw_review(self.objective_id, self.other))
        self.assertEqual(403, context.exception.status_code)

    def test_pending_personal_okr_cannot_bypass_withdraw_via_delete(self):
        response = self.client.delete(f"/api/okr/objectives/{self.objective_id}")
        self.assertEqual(403, response.status_code)
        self.assertIn("请先撤回审核", response.json()["detail"])
        with okr_db.get_db() as db:
            objective = db.execute(
                "SELECT status FROM okr_objectives WHERE id = ?", (self.objective_id,)
            ).fetchone()
        self.assertEqual("pending", objective["status"])

    def test_non_pending_objective_cannot_be_withdrawn(self):
        with okr_db.get_db() as db:
            db.execute(
                "UPDATE okr_objectives SET status = 'draft' WHERE id = ?",
                (self.objective_id,),
            )
        with self.assertRaisesRegex(HTTPException, "只有待审核") as context:
            asyncio.run(okr_api.withdraw_review(self.objective_id, self.owner))
        self.assertEqual(400, context.exception.status_code)

    def test_personal_board_uses_unambiguous_withdraw_action(self):
        html = (
            Path(__file__).parents[1] / "static" / "okr" / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn(">撤回审核</button>", html)
        self.assertIn("/withdraw-review", html)
        self.assertIn("OKR 将保留并退回草稿状态", html)
        self.assertNotIn("撤回删除", html)


if __name__ == "__main__":
    unittest.main()
