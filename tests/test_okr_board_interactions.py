import asyncio
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

import okr_api
import okr_db
from okr_models import EmployeeBoardNoteUpdate, KeyResultCreate, ObjectiveCreate


class OkrBoardInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = okr_db.DB_PATH
        okr_db.DB_PATH = str(Path(self.tempdir.name) / "okr.db")
        okr_db.init_db()
        with okr_db.get_db() as db:
            self.company_id = db.execute(
                "INSERT INTO companies(name, status) VALUES ('测试公司', 'active')"
            ).lastrowid
            self.dev_id = db.execute(
                "INSERT INTO departments(name, company_id) VALUES ('开发部', ?)",
                (self.company_id,),
            ).lastrowid
            self.test_id = db.execute(
                "INSERT INTO departments(name, company_id) VALUES ('测试部', ?)",
                (self.company_id,),
            ).lastrowid
            self.company_head_id = self._insert_user(
                db, "13810000001", "公司负责人", "dept_head", self.dev_id
            )
            self.dev_head_id = self._insert_user(
                db, "13810000002", "开发负责人", "dept_head", self.dev_id
            )
            self.member_id = self._insert_user(
                db, "13810000003", "同名成员", "user", self.dev_id, "wecom-member"
            )
            self.local_duplicate_id = self._insert_user(
                db, "13810000004", "同名成员", "user", self.dev_id
            )
            self.other_member_id = self._insert_user(
                db, "13810000005", "测试成员", "user", self.test_id
            )
            db.execute(
                "UPDATE companies SET head_user_id = ? WHERE id = ?",
                (self.company_head_id, self.company_id),
            )
            db.execute(
                "UPDATE departments SET head_user_id = ? WHERE id = ?",
                (self.dev_head_id, self.dev_id),
            )
        self.company_head = self._user(self.company_head_id, "公司负责人", "dept_head", self.dev_id, True)
        self.dev_head = self._user(self.dev_head_id, "开发负责人", "dept_head", self.dev_id)
        self.member = self._user(self.member_id, "同名成员", "user", self.dev_id)
        self.local_duplicate = self._user(self.local_duplicate_id, "同名成员", "user", self.dev_id)
        self.other_member = self._user(self.other_member_id, "测试成员", "user", self.test_id)

    def tearDown(self):
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _insert_user(self, db, phone, name, role, department_id, wecom_userid=None):
        return db.execute(
            "INSERT INTO users(phone,password_hash,name,role,company_id,department_id,status,wecom_userid) "
            "VALUES (?, 'x', ?, ?, ?, ?, 'active', ?)",
            (phone, name, role, self.company_id, department_id, wecom_userid),
        ).lastrowid

    def _user(self, user_id, name, role, department_id, is_company_head=False):
        return {
            "id": user_id,
            "name": name,
            "role": role,
            "base_role": role,
            "company_id": self.company_id,
            "department_id": department_id,
            "phone_verified": True,
            "is_company_head": is_company_head,
        }

    def _create_personal_objective(self, owner, title="提升交付质量"):
        return asyncio.run(okr_api.create_objective(
            ObjectiveCreate(title=title, type="personal", period="2026-08"), owner
        ))["id"]

    def test_new_key_result_and_startup_repair_keep_objective_progress_consistent(self):
        objective_id = self._create_personal_objective(self.member)
        asyncio.run(okr_api.create_key_result(
            objective_id,
            KeyResultCreate(title="完成验收", target_value=100, current_value=100),
            self.member,
        ))
        with okr_db.get_db() as db:
            progress = db.execute(
                "SELECT progress FROM okr_objectives WHERE id = ?", (objective_id,)
            ).fetchone()[0]
            kr_progress = db.execute(
                "SELECT progress FROM okr_key_results WHERE objective_id = ?", (objective_id,)
            ).fetchone()[0]
            self.assertEqual(progress, 100)
            self.assertEqual(kr_progress, 100)
            db.execute("UPDATE okr_key_results SET progress = 0 WHERE objective_id = ?", (objective_id,))
            db.execute("UPDATE okr_objectives SET progress = 0 WHERE id = ?", (objective_id,))
            okr_db._repair_progress_snapshots(db)
            repaired = db.execute(
                "SELECT progress FROM okr_objectives WHERE id = ?", (objective_id,)
            ).fetchone()[0]
        self.assertEqual(repaired, 100)

    def test_employee_note_permissions_and_monthly_board_payload(self):
        asyncio.run(okr_api.update_employee_board_note(
            self.member_id,
            EmployeeBoardNoteUpdate(period="2026-08", note="持续关注跨组协作"),
            self.dev_head,
        ))
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.update_employee_board_note(
                self.other_member_id,
                EmployeeBoardNoteUpdate(period="2026-08", note="越权备注"),
                self.dev_head,
            ))
        asyncio.run(okr_api.update_employee_board_note(
            self.other_member_id,
            EmployeeBoardNoteUpdate(period="2026-08", note="公司级跟进"),
            self.company_head,
        ))
        board = asyncio.run(okr_api.board_monthly_company(
            "2026-08", self.company_id, self.company_head
        ))
        member = next(item for item in board["members"] if item["user"]["id"] == self.member_id)
        self.assertEqual(member["user"]["note"], "持续关注跨组协作")
        self.assertTrue(member["user"]["can_edit_note"])

    def test_rejection_reason_and_encouragement_are_attached_to_object(self):
        objective_id = self._create_personal_objective(self.member)
        with okr_db.get_db() as db:
            db.execute("UPDATE okr_objectives SET status = 'rejected' WHERE id = ?", (objective_id,))
            db.execute(
                "INSERT INTO okr_reviews(objective_id, reviewer_id, status, comment) "
                "VALUES (?, ?, 'rejected', '请拆分过多的 KR')",
                (objective_id, self.dev_head_id),
            )
        first = asyncio.run(okr_api.toggle_objective_encouragement(
            objective_id, self.local_duplicate
        ))
        self.assertTrue(first["encouraged"])
        self.assertEqual(first["encouragement_count"], 1)
        self.assertEqual(first["recent_encouragement"]["employee_name"], "同名成员")
        self.assertEqual(first["recent_encouragement"]["objective_id"], objective_id)
        self.assertIsNone(first["removed_encouragement_key"])
        with self.assertRaisesRegex(HTTPException, "不能给自己的"):
            asyncio.run(okr_api.toggle_objective_encouragement(objective_id, self.member))
        board = asyncio.run(okr_api.board_monthly_company(
            "2026-08", self.company_id, self.local_duplicate
        ))
        item = next(
            objective
            for member in board["members"]
            for objective in member["okrs"]
            if objective["id"] == objective_id
        )
        self.assertEqual(item["latest_review"]["comment"], "请拆分过多的 KR")
        self.assertEqual(item["encouragement_count"], 1)
        self.assertTrue(item["encouraged_by_me"])
        self.assertEqual(len(board["recent_encouragements"]), 1)
        self.assertEqual(board["recent_encouragements"][0]["objective_id"], objective_id)

        second = asyncio.run(okr_api.toggle_objective_encouragement(
            objective_id, self.local_duplicate
        ))
        self.assertFalse(second["encouraged"])
        self.assertIsNone(second["recent_encouragement"])
        self.assertEqual(
            second["removed_encouragement_key"],
            f"{objective_id}:{self.local_duplicate_id}",
        )
        after_cancel = asyncio.run(okr_api.board_monthly_company(
            "2026-08", self.company_id, self.local_duplicate
        ))
        self.assertEqual(after_cancel["recent_encouragements"], [])

    def test_monthly_board_limits_recent_encouragements_to_latest_ten(self):
        objective_ids = []
        for index in range(11):
            objective_id = self._create_personal_objective(
                self.member, title=f"目标 {index + 1}"
            )
            objective_ids.append(objective_id)
            asyncio.run(okr_api.toggle_objective_encouragement(
                objective_id, self.local_duplicate
            ))
        board = asyncio.run(okr_api.board_monthly_company(
            "2026-08", self.company_id, self.company_head
        ))
        recent = board["recent_encouragements"]
        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0]["objective_id"], objective_ids[-1])
        self.assertNotIn(objective_ids[0], [item["objective_id"] for item in recent])

    def test_statistics_marks_duplicate_accounts_and_never_returns_null_counts(self):
        rows = asyncio.run(okr_api.statistics_department(
            self.dev_id, "2026-08", self.company_head
        ))
        duplicates = [row for row in rows if row["user_name"] == "同名成员"]
        self.assertEqual(len(duplicates), 2)
        self.assertTrue(all(row["duplicate_name"] for row in duplicates))
        self.assertEqual({row["account_source"] for row in duplicates}, {"wecom", "local"})
        self.assertTrue(all(row["completed_objectives"] == 0 for row in duplicates))

    def test_frontend_exposes_note_rejection_and_encouragement_controls(self):
        root = Path(__file__).parents[1]
        html = (root / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        mindmap = (root / "static" / "okr" / "okr-mindmap.js").read_text(encoding="utf-8")
        charts = (root / "static" / "okr" / "okr-charts.js").read_text(encoding="utf-8")
        self.assertIn('id="employeeNoteModal"', html)
        self.assertIn("onEditEmployeeNote: openEmployeeNoteModal", html)
        self.assertIn("onToggleEncouragement: toggleObjectiveEncouragement", html)
        encouragement_handler = html.split(
            "async function toggleObjectiveEncouragement", 1
        )[1].split("function resetDeptOkrDetailsToggle", 1)[0]
        self.assertNotIn("loadDeptBoard", encouragement_handler)
        self.assertIn("getObjectiveEncouragement", encouragement_handler)
        self.assertIn("updateObjectiveEncouragement", encouragement_handler)
        self.assertIn("previousState", encouragement_handler)
        self.assertIn('id="encouragementTicker"', html)
        self.assertIn("position: fixed", html)
        self.assertIn("ENCOURAGEMENT_TICKER_INTERVAL_MS = 4000", html)
        self.assertIn("setEncouragementTicker(data.recent_encouragements || [])", html)
        self.assertIn("reconcileEncouragementTicker(", html)
        self.assertIn("translateY(-22px)", html)
        self.assertIn("驳回原因", mindmap)
        self.assertIn("encouragement-control", mindmap)
        self.assertIn("getObjectiveEncouragement(containerId, objectiveId)", mindmap)
        self.assertIn("updateObjectiveEncouragement(containerId, objectiveId, nextState)", mindmap)
        self.assertIn("encouragementPending", mindmap)
        self.assertIn("管理备注", mindmap)
        self.assertIn("未绑定企业微信", charts)

    def test_monthly_board_has_person_filter_and_split_employee_node_layout(self):
        root = Path(__file__).parents[1]
        html = (root / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        mindmap = (root / "static" / "okr" / "okr-mindmap.js").read_text(encoding="utf-8")

        self.assertIn('id="deptBoardPersonFilter"', html)
        self.assertIn("function filteredMonthlyCompanyBoardData(data)", html)
        self.assertIn("members: [selectedMember]", html)
        self.assertIn("alignedObjectiveIds.has(Number(objective.id))", html)
        filter_handler = html.split("function onDeptBoardPersonFilterChange()", 1)[1].split(
            "function openEmployeeNoteModal", 1
        )[0]
        self.assertIn("renderMonthlyCompanyBoardData()", filter_handler)
        self.assertNotIn("api(", filter_handler)

        self.assertIn("const employeeSize = { w: 176, h: 64", mindmap)
        self.assertIn("employee-avatar-zone", mindmap)
        self.assertIn("employee-avatar-initial", mindmap)
        self.assertIn("employee-info-name", mindmap)
        self.assertIn("employee-info-department", mindmap)
        self.assertIn("employee-info-progress", mindmap)
        self.assertIn("? 1.15", mindmap)


if __name__ == "__main__":
    unittest.main()
