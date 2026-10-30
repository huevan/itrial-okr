import asyncio
import tempfile
import unittest
from pathlib import Path

import okr_api
import okr_auth
import okr_db
from fastapi import HTTPException, Request
from okr_models import KeyResultCreate, ObjectiveCreate, ObjectiveUpdate, ReviewAction


class OkrCompanyModelTests(unittest.TestCase):
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
            self.head_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,department_id,status) "
                "VALUES ('13811110001','x','负责人','superAdmin',?,?,'active')",
                (self.company_id, self.dev_id),
            ).lastrowid
            self.member_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,department_id,status) "
                "VALUES ('13811110002','x','成员甲','user',?,?,'active')",
                (self.company_id, self.test_id),
            ).lastrowid
            db.execute("UPDATE companies SET head_user_id=? WHERE id=?", (self.head_id, self.company_id))
            db.execute("UPDATE departments SET head_user_id=? WHERE id=?", (self.head_id, self.dev_id))
        self.head = {
            "id": self.head_id, "name": "负责人", "role": "dept_head",
            "base_role": "superAdmin", "company_id": self.company_id,
            "department_id": self.dev_id, "phone_verified": True,
            "is_company_head": True,
        }
        self.member = {
            "id": self.member_id, "name": "成员甲", "role": "user",
            "base_role": "user", "company_id": self.company_id,
            "department_id": self.test_id, "phone_verified": True,
        }

    def tearDown(self):
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def test_annual_monthly_and_company_reviewer_flow(self):
        annual = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="年度增长", type="company", company_id=self.company_id,
            period="2026-08",
        ), self.head))
        monthly = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="八月交付", type="department", company_id=self.company_id,
            period="2026-08", department_id=self.dev_id,
            aligned_objective_id=annual["id"],
        ), self.head))
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="完成测试", type="personal", period="2026-08",
            aligned_objective_id=monthly["id"],
        ), self.member))
        asyncio.run(okr_api.create_key_result(
            personal["id"], KeyResultCreate(
                title="完成自动化回归", target_value=100, current_value=60, unit="%"
            ), self.member
        ))
        asyncio.run(okr_api.submit_review(personal["id"], self.member))

        with okr_db.get_db() as db:
            annual_row = db.execute("SELECT period,department_id FROM okr_objectives WHERE id=?", (annual["id"],)).fetchone()
            monthly_row = db.execute("SELECT period,department_id FROM okr_objectives WHERE id=?", (monthly["id"],)).fetchone()
            review = db.execute("SELECT reviewer_id FROM okr_reviews WHERE objective_id=?", (personal["id"],)).fetchone()
        self.assertEqual(annual_row["period"], "2026")
        self.assertIsNone(annual_row["department_id"])
        self.assertEqual(monthly_row["period"], "2026-08")
        self.assertIsNone(monthly_row["department_id"])
        self.assertEqual(review["reviewer_id"], self.head_id)

        annual_board = asyncio.run(okr_api.board_company("2026-11", self.company_id, self.head))
        self.assertEqual([item["id"] for item in annual_board["company_okrs"]], [annual["id"]])
        self.assertEqual(annual_board["departments"], [])
        monthly_board = asyncio.run(okr_api.board_monthly_company("2026-08", self.company_id, self.head))
        self.assertEqual([item["id"] for item in monthly_board["dept_okrs"]], [monthly["id"]])
        member = next(item for item in monthly_board["members"] if item["user"]["id"] == self.member_id)
        self.assertEqual(member["user"]["department_name"], "测试部")
        self.assertEqual([item["id"] for item in member["okrs"]], [personal["id"]])
        self.assertEqual(member["okrs"][0]["key_results"][0]["title"], "完成自动化回归")

    def test_new_month_clones_company_goals_but_not_personal_goals(self):
        monthly = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="八月交付", type="department", company_id=self.company_id, period="2026-08",
        ), self.head))
        asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="个人八月目标", type="personal", period="2026-08", aligned_objective_id=monthly["id"],
        ), self.member))
        board = asyncio.run(okr_api.board_monthly_company("2026-09", self.company_id, self.head))
        self.assertEqual(board["cloned_from_previous"], 1)
        self.assertEqual([item["title"] for item in board["dept_okrs"]], ["八月交付"])
        self.assertTrue(all(not item["okrs"] for item in board["members"]))

    def test_wecom_root_department_is_not_persisted(self):
        with okr_db.get_db() as db:
            mapping = okr_api._sync_wecom_departments(db, self.company_id, [
                {"id": 1, "name": "测试公司", "parentid": 0},
                {"id": 7, "name": "开发部"},
            ])
            root = db.execute(
                "SELECT id FROM departments WHERE company_id=? AND wecom_department_id=1",
                (self.company_id,),
            ).fetchone()
        self.assertNotIn(1, mapping)
        self.assertIsNone(root)
        self.assertEqual(mapping[7], self.dev_id)

    def test_company_head_role_has_company_wide_admin_permissions(self):
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="成员目标", type="personal", period="2026-08",
        ), self.member))
        asyncio.run(okr_api.update_objective(
            personal["id"], ObjectiveUpdate(title="公司负责人已维护"), self.head
        ))
        asyncio.run(okr_api.create_key_result(
            personal["id"], KeyResultCreate(title="公司级维护 KR"), self.head
        ))
        with okr_db.get_db() as db:
            title = db.execute("SELECT title FROM okr_objectives WHERE id=?", (personal["id"],)).fetchone()[0]
            kr_count = db.execute("SELECT COUNT(*) FROM okr_key_results WHERE objective_id=?", (personal["id"],)).fetchone()[0]
        self.assertEqual(title, "公司负责人已维护")
        self.assertEqual(kr_count, 1)

        admin_guard = okr_auth.require_role("superAdmin", "admin")
        self.assertEqual(asyncio.run(admin_guard(self.head))["id"], self.head_id)
        company_reviews = asyncio.run(okr_api.list_monthly_reviews(
            "2026-08", None, None, self.head
        ))
        self.assertIn(personal["id"], [item["objective_id"] for item in company_reviews["dept_reviews"]])

    def test_company_head_can_switch_personal_view_and_create_for_employee_without_approval(self):
        employees = asyncio.run(okr_api.list_personal_view_employees(self.head))
        self.assertEqual(employees[0]["id"], self.head_id)
        self.assertIn(self.member_id, [item["id"] for item in employees])

        created = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="负责人代建目标", type="personal", period="2026-08",
            owner_id=self.member_id,
        ), self.head))
        self.assertTrue(created["auto_activated"])
        self.assertEqual(created["status"], "active")

        board = asyncio.run(okr_api.board_personal("2026-08", self.head, self.member_id))
        self.assertEqual(board["view_user"]["id"], self.member_id)
        self.assertTrue(board["can_manage"])
        self.assertEqual([item["id"] for item in board["objectives"]], [created["id"]])

        with okr_db.get_db() as db:
            objective = db.execute(
                "SELECT owner_id, company_id, department_id, status FROM okr_objectives WHERE id=?",
                (created["id"],),
            ).fetchone()
            reviews = db.execute(
                "SELECT status, reviewer_id FROM okr_reviews WHERE objective_id=?",
                (created["id"],),
            ).fetchall()
        self.assertEqual(objective["owner_id"], self.member_id)
        self.assertEqual(objective["company_id"], self.company_id)
        self.assertEqual(objective["department_id"], self.test_id)
        self.assertEqual(objective["status"], "active")
        self.assertEqual([(row["status"], row["reviewer_id"]) for row in reviews], [("approved", self.head_id)])

    def test_company_head_edit_activates_pending_employee_objective(self):
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="员工待审目标", type="personal", period="2026-08",
        ), self.member))
        asyncio.run(okr_api.submit_review(personal["id"], self.member))

        result = asyncio.run(okr_api.update_objective(
            personal["id"], ObjectiveUpdate(title="负责人已代为修改"), self.head
        ))
        self.assertTrue(result["auto_activated"])
        with okr_db.get_db() as db:
            objective = db.execute(
                "SELECT title, status FROM okr_objectives WHERE id=?", (personal["id"],)
            ).fetchone()
            review = db.execute(
                "SELECT status, reviewer_id, comment FROM okr_reviews WHERE objective_id=? ORDER BY id DESC LIMIT 1",
                (personal["id"],),
            ).fetchone()
        self.assertEqual(objective["title"], "负责人已代为修改")
        self.assertEqual(objective["status"], "active")
        self.assertEqual(review["status"], "approved")
        self.assertEqual(review["reviewer_id"], self.head_id)
        self.assertIn("直接生效", review["comment"])

    def test_regular_employee_cannot_switch_or_assign_personal_owner(self):
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.list_personal_view_employees(self.member))
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.board_personal("2026-08", self.member, self.head_id))
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.create_objective(ObjectiveCreate(
                title="越权代建", type="personal", period="2026-08", owner_id=self.head_id,
            ), self.member))

    def test_company_head_cannot_manage_employee_in_another_company(self):
        with okr_db.get_db() as db:
            other_company_id = db.execute(
                "INSERT INTO companies(name, status) VALUES ('其他公司', 'active')"
            ).lastrowid
            other_user_id = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) "
                "VALUES ('13811110003','x','外部员工','user',?,'active')",
                (other_company_id,),
            ).lastrowid
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.board_personal("2026-08", self.head, other_user_id))
        with self.assertRaisesRegex(HTTPException, "只有公司负责人"):
            asyncio.run(okr_api.create_objective(ObjectiveCreate(
                title="跨公司越权代建", type="personal", period="2026-08", owner_id=other_user_id,
            ), self.head))

    def test_company_head_personal_view_controls_are_present(self):
        html = (Path(__file__).parents[1] / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="personalViewUserSelect"', html)
        self.assertIn("/api/okr/personal-view/employees", html)
        self.assertIn("&user_id=${_personalViewUserId}", html)
        self.assertIn("body.owner_id = selectedPersonalViewUser().id", html)
        self.assertIn("保存后直接生效，不进入审批队列", html)

    def test_personal_okr_creation_guide_is_available_before_create_action(self):
        html = (Path(__file__).parents[1] / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        help_link = html.index('id="okrGuideLink"')
        create_button = html.index('id="btnCreatePersonalOkr"')

        self.assertLess(help_link, create_button)
        self.assertIn('id="okrGuideModal"', html)
        self.assertIn('aria-labelledby="okrGuideModalTitle"', html)
        self.assertIn("O = 清晰方向 + 业务价值", html)
        self.assertIn("KR = 可量化结果 + 明确标准", html)
        self.assertIn("将【指标】从【当前值】提升/降低到【目标值】", html)
        self.assertIn("hideModal('okrGuideModal');showOkrModal('personal')", html)

    def test_monthly_review_payload_and_layout_include_person_objective_krs(self):
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="评估目标", description="评估目标说明", type="personal",
            period="2026-08", weight=2,
        ), self.member))
        asyncio.run(okr_api.create_key_result(
            personal["id"],
            KeyResultCreate(
                title="评估关键结果", target_value=10, current_value=6, unit="项", weight=1,
            ),
            self.member,
        ))

        payload = asyncio.run(okr_api.list_monthly_reviews("2026-08", None, None, self.head))
        item = next(row for row in payload["dept_reviews"] if row["objective_id"] == personal["id"])
        self.assertEqual(item["objective_description"], "评估目标说明")
        self.assertEqual(item["objective_weight"], 2)
        self.assertEqual(len(item["key_results"]), 1)
        self.assertEqual(item["key_results"][0]["title"], "评估关键结果")
        self.assertEqual(item["key_results"][0]["progress"], 60)

        html = (Path(__file__).parents[1] / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        self.assertIn('class="okr-period-weight-row"', html)
        self.assertIn('id="deptReviewPersonTree"', html)
        self.assertIn('id="deptReviewPersonDetail"', html)
        self.assertIn("function _renderDeptReviewPersonDetail()", html)
        self.assertIn("item.key_results || []", html)

    def test_company_head_can_review_any_pending_objective_in_own_company(self):
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="待审目标", type="personal", period="2026-08",
        ), self.member))
        with okr_db.get_db() as db:
            db.execute("UPDATE okr_objectives SET status='pending' WHERE id=?", (personal["id"],))
            review_id = db.execute(
                "INSERT INTO okr_reviews(objective_id, reviewer_id, status) VALUES (?, ?, 'pending')",
                (personal["id"], self.member_id),
            ).lastrowid
        pending = asyncio.run(okr_api.list_pending_reviews(self.head))
        self.assertEqual([item["id"] for item in pending], [review_id])
        asyncio.run(okr_api.review_action(
            review_id, ReviewAction(status="approved", comment="通过"), self.head
        ))
        with okr_db.get_db() as db:
            status = db.execute("SELECT status FROM okr_objectives WHERE id=?", (personal["id"],)).fetchone()[0]
        self.assertEqual(status, "active")

    def test_rejected_objective_shows_reason_and_can_be_resubmitted(self):
        personal = asyncio.run(okr_api.create_objective(ObjectiveCreate(
            title="需要调整的目标", type="personal", period="2026-08",
        ), self.member))
        asyncio.run(okr_api.submit_review(personal["id"], self.member))
        with okr_db.get_db() as db:
            first_review_id = db.execute(
                "SELECT id FROM okr_reviews WHERE objective_id=? ORDER BY id DESC LIMIT 1",
                (personal["id"],),
            ).fetchone()[0]

        with self.assertRaisesRegex(HTTPException, "驳回时必须填写驳回意见"):
            asyncio.run(okr_api.review_action(
                first_review_id, ReviewAction(status="rejected", comment="   "), self.head
            ))
        with self.assertRaisesRegex(HTTPException, "只有草稿或已驳回"):
            asyncio.run(okr_api.submit_review(personal["id"], self.member))

        rejection_reason = "请补充可量化的 KR，并明确本月目标值。"
        asyncio.run(okr_api.review_action(
            first_review_id,
            ReviewAction(status="rejected", comment=f"  {rejection_reason}  "),
            self.head,
        ))
        board = asyncio.run(okr_api.board_personal("2026-08", self.member))
        objective = board["objectives"][0]
        self.assertEqual(objective["status"], "rejected")
        self.assertEqual(objective["latest_review"]["status"], "rejected")
        self.assertEqual(objective["latest_review"]["comment"], rejection_reason)

        asyncio.run(okr_api.update_objective(
            personal["id"], ObjectiveUpdate(title="已补充量化 KR 的目标"), self.member
        ))
        asyncio.run(okr_api.submit_review(personal["id"], self.member))
        with okr_db.get_db() as db:
            reviews = db.execute(
                "SELECT status, comment FROM okr_reviews WHERE objective_id=? ORDER BY id",
                (personal["id"],),
            ).fetchall()
            objective_status = db.execute(
                "SELECT status FROM okr_objectives WHERE id=?", (personal["id"],)
            ).fetchone()[0]
        self.assertEqual([row["status"] for row in reviews], ["rejected", "pending"])
        self.assertEqual(reviews[0]["comment"], rejection_reason)
        self.assertEqual(objective_status, "pending")

    def test_authenticated_company_head_keeps_switch_role_and_scope_marker(self):
        token = okr_auth.create_token(
            self.head_id, "dept_head", "负责人", self.company_id, base_role="superAdmin"
        )
        request = Request({
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        })
        authenticated = asyncio.run(okr_auth.get_authenticated_user(request))
        self.assertEqual(authenticated["role"], "dept_head")
        self.assertTrue(authenticated["is_company_head"])
        self.assertEqual(authenticated["available_roles"], ["dept_head", "superAdmin"])

    def test_review_center_is_a_dedicated_company_head_workflow(self):
        html = (Path(__file__).parents[1] / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-tab="tab-audit"', html)
        self.assertIn('id="auditTabCount"', html)
        self.assertIn('id="auditDepartmentFilter"', html)
        self.assertIn('id="auditPeriodFilter"', html)
        self.assertIn('id="auditReviewModal"', html)
        personal_panel = html.split('id="tab-personal"', 1)[1].split('id="tab-audit"', 1)[0]
        self.assertNotIn('id="reviewPanel"', personal_panel)
        self.assertNotIn("prompt('请输入驳回原因:')", html)
        self.assertIn("status === 'rejected' && !comment", html)
        self.assertIn('class="okr-rejection-notice"', html)
        self.assertIn("重新提交审核", html)

    def test_monthly_company_view_can_expand_every_employee_objective_and_kr(self):
        root = Path(__file__).parents[1]
        html = (root / "static" / "okr" / "index.html").read_text(encoding="utf-8")
        mindmap = (root / "static" / "okr" / "okr-mindmap.js").read_text(encoding="utf-8")
        self.assertIn('id="btnToggleDeptDetails"', html)
        self.assertIn("toggleDeptOkrDetails()", html)
        self.assertIn("type: 'kr-detail'", mindmap)
        self.assertIn("expandAllDetails(containerId)", mindmap)
        self.assertIn("collapseAllDetails(containerId)", mindmap)
        self.assertIn("children: krChildren", mindmap)


if __name__ == "__main__":
    unittest.main()
