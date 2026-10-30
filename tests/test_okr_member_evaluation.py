import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import fitz

from fastapi import FastAPI
from fastapi.testclient import TestClient

import okr_api
import okr_auth
import okr_db
from okr_evaluation import member_scores, objective_score


class MemberScoringTests(unittest.TestCase):
    def test_objective_formula_uses_self_20_manager_80_without_progress(self):
        for progress in (None, 0, 30, 100):
            with self.subTest(progress=progress):
                self.assertEqual(90.6, objective_score(progress, 93, 90))
                self.assertEqual(90.6, okr_api._calc_composite(progress, 93, 90))
        self.assertEqual(80, objective_score(100, 0, 100))
        self.assertEqual(20, objective_score(100, 100, 0))
        self.assertEqual(0, objective_score(100, 0, 0))
        self.assertEqual(100, objective_score(0, 100, 100))
        self.assertIsNone(objective_score(100, None, 90))
        self.assertIsNone(objective_score(100, 90, None))

    def test_weights_behavior_and_redline(self):
        items = [
            {"status": "completed", "composite_score": 100, "objective_weight": 1},
            {"status": "completed", "composite_score": 60, "objective_weight": 3},
        ]
        assessment = {"satisfaction_score": 80, "initiative_score": 100, "redline_factor": 1}
        result = member_scores(items, assessment)
        self.assertEqual((70, 49, 90, 27, 76), tuple(result[k] for k in (
            "task_score", "task_contribution", "behavior_score", "behavior_contribution", "total_score")))
        self.assertEqual(0, member_scores(items, {**assessment, "redline_factor": 0})["total_score"])

    def test_incomplete_tasks_and_behavior_never_get_a_partial_final_score(self):
        items = [{"status": "completed", "composite_score": 100, "objective_weight": 1},
                 {"status": "pending", "composite_score": None, "objective_weight": 99}]
        assessment = {"satisfaction_score": 100, "initiative_score": 100, "redline_factor": 1}
        self.assertIsNone(member_scores(items, assessment)["total_score"])
        self.assertIsNone(member_scores(items[:1])["total_score"])
        self.assertEqual("no_objectives", member_scores([], assessment)["status"])

    def test_real_zero_and_unrounded_intermediate_values(self):
        item = {"status": "completed", "composite_score": 0, "objective_weight": 1}
        assessment = {"satisfaction_score": 0, "initiative_score": 0, "redline_factor": 1}
        self.assertEqual("completed", member_scores([item], assessment)["status"])
        self.assertEqual(0, member_scores([item], assessment)["total_score"])
        items = [{**item, "composite_score": 73.3},
                 {**item, "composite_score": 81.6, "objective_weight": 3}]
        assessment.update(satisfaction_score=80, initiative_score=81)
        self.assertEqual(round(((73.3 + 81.6 * 3) / 4 * .7 + 80.5 * .3), 1),
                         member_scores(items, assessment)["total_score"])


class MemberEvaluationApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = okr_db.DB_PATH
        okr_db.DB_PATH = str(Path(self.tempdir.name) / "okr.db")
        with contextlib.redirect_stdout(io.StringIO()):
            okr_db.init_db()
        with okr_db.get_db() as db:
            self.company = db.execute("INSERT INTO companies(name) VALUES ('测试企业甲')").lastrowid
            self.other_company = db.execute("INSERT INTO companies(name) VALUES ('测试企业乙')").lastrowid
            self.dept = db.execute("INSERT INTO departments(name,company_id) VALUES ('一部',?)", (self.company,)).lastrowid
            self.other_dept = db.execute("INSERT INTO departments(name,company_id) VALUES ('二部',?)", (self.company,)).lastrowid
            self.head = self._user(db, "主管", "dept_head", self.dept)
            self.company_head = self._user(db, "公司负责人", "dept_head", self.other_dept)
            self.admin = self._user(db, "管理员", "admin", self.dept)
            self.member = self._user(db, "员工甲", "user", self.dept)
            self.peer = self._user(db, "员工乙", "user", self.dept)
            self.outside = self._user(db, "其他部门", "user", self.other_dept)
            self.foreign = self._user(db, "其他企业", "user", None, self.other_company)
            self.no_department = self._user(db, "无部门主管", "dept_head", None)
            db.execute("UPDATE companies SET head_user_id=? WHERE id=?", (self.company_head["id"], self.company))
            self.company_head["is_company_head"] = True
            self.root = dict(db.execute("SELECT * FROM users WHERE role='superAdmin' LIMIT 1").fetchone())
        self.actor = self.company_head
        app = FastAPI()
        app.include_router(okr_api.okr_router)
        app.dependency_overrides[okr_auth.get_current_user] = lambda: self.actor
        self.client = TestClient(app)
        self.period = "2026-10"
        self.url = f"/api/okr/evaluations/monthly/{self.member['id']}"
        self.payload = {"year_month": self.period, "satisfaction_score": 80,
                        "initiative_score": 100, "redline_factor": 1}

    def tearDown(self):
        self.client.close()
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def _user(self, db, name, role, department_id, company_id=None):
        phone = str(13000000000 + db.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        uid = db.execute(
            "INSERT INTO users(phone,password_hash,name,role,company_id,department_id,status) "
            "VALUES (?,'unused',?,?,?,?, 'active')",
            (phone, name, role, company_id or self.company, department_id),
        ).lastrowid
        return {"id": uid, "name": name, "role": role, "company_id": company_id or self.company,
                "department_id": department_id, "phone_verified": True}

    def objective(self, owner=None, score=100, weight=1, completed=True, kind="personal", period=None):
        owner = owner or self.member
        period = period or self.period
        with okr_db.get_db() as db:
            oid = db.execute(
                "INSERT INTO okr_objectives(title,type,owner_id,company_id,department_id,period,progress,weight) "
                "VALUES ('测试任务',?,?,?,?,?,?,?)",
                (kind, owner["id"], owner["company_id"], owner["department_id"], period, score, weight),
            ).lastrowid
            db.execute(
                "INSERT INTO okr_key_results(objective_id,title,target_value,current_value,progress,unit) "
                "VALUES (?,'测试关键结果',100,?,?,'%')", (oid, score, score),
            )
            if completed:
                db.execute(
                    "INSERT INTO okr_monthly_reviews(objective_id,year_month,self_score,manager_score,status) "
                    "VALUES (?,?,?,?,'completed')", (oid, period, score, score),
                )
        return oid

    def member_result(self, member=None, **params):
        response = self.client.get('/api/okr/evaluations/monthly', params={"period": self.period, **params})
        self.assertEqual(200, response.status_code, response.text)
        return next(row for row in response.json()["members"] if row["user_id"] == (member or self.member)["id"])

    def test_end_to_end_scores_match_summary_and_both_export_layers(self):
        self.objective(score=100, weight=1)
        self.objective(score=60, weight=3)
        response = self.client.put(self.url, json=self.payload)
        self.assertEqual(200, response.status_code, response.text)
        member = self.member_result()
        self.assertEqual(76, member["total_score"])
        summary = self.client.get('/api/okr/reviews/monthly/summary', params={"period": self.period}).json()
        summary_member = next(m for dept in summary["departments"] for m in dept["members"] if m["user_id"] == self.member["id"])
        self.assertEqual(member["total_score"], summary_member["total_score"])
        self.assertEqual(70, summary_member["weighted_score"])
        export = self.client.get('/api/okr/reviews/monthly/export', params={"period": self.period}).json()
        exported = next(row for row in export["summary_rows"] if row["姓名"] == self.member["name"])
        self.assertEqual(76, exported["月度总分"])
        self.assertEqual(2, len(export["rows"]))

    def test_redline_zero_requires_reason_and_does_not_become_missing(self):
        self.objective()
        payload = {**self.payload, "redline_factor": 0}
        self.assertEqual(400, self.client.put(self.url, json=payload).status_code)
        self.assertEqual(400, self.client.put(self.url, json={**payload, "redline_reason": "  "}).status_code)
        self.assertEqual(200, self.client.put(self.url, json={**payload, "redline_reason": "具体事实"}).status_code)
        self.assertEqual(0, self.member_result()["total_score"])

    def test_2080_formula_matches_cards_summary_excel_pdf_and_ignores_kr_changes(self):
        first = self.objective(score=10, weight=1)
        second = self.objective(score=100, weight=3)
        with okr_db.get_db() as db:
            db.execute('UPDATE okr_monthly_reviews SET self_score=90,manager_score=80 WHERE objective_id=?', (first,))
            db.execute('UPDATE okr_monthly_reviews SET self_score=50,manager_score=60 WHERE objective_id=?', (second,))
            reviews = [tuple(row) for row in db.execute('SELECT * FROM okr_monthly_reviews ORDER BY id')]
        self.assertEqual(200, self.client.put(self.url, json=self.payload).status_code)
        member = self.member_result()
        self.assertEqual((64, 44.8, 27, 71.8), tuple(member[key] for key in (
            'task_score', 'task_contribution', 'behavior_contribution', 'total_score')))
        self.assertEqual({first: 82, second: 58}, {
            row['objective_id']: row['composite_score'] for row in member['objectives']})
        cards = self.client.get('/api/okr/reviews/monthly', params={'period': self.period}).json()['dept_reviews']
        self.assertEqual({first: 82, second: 58}, {
            row['objective_id']: row['composite_score'] for row in cards})
        summary = self.client.get('/api/okr/reviews/monthly/summary', params={'period': self.period}).json()
        result = next(row for dept in summary['departments'] for row in dept['members']
                      if row['user_id'] == self.member['id'])
        self.assertEqual((64, 71.8), (result['weighted_score'], result['total_score']))
        export = self.client.get('/api/okr/reviews/monthly/export', params={'period': self.period}).json()
        self.assertEqual([82, 58], [row['综合评分'] for row in export['rows']])
        exported_member = next(row for row in export['summary_rows'] if row['姓名'] == self.member['name'])
        self.assertEqual(71.8, exported_member['月度总分'])
        response = self.client.get(self.url + '/pdf', params={'period': self.period})
        self.assertEqual(200, response.status_code)
        with fitz.open(stream=response.content, filetype='pdf') as pdf:
            text = ''.join(''.join(page.get_text().split()) for page in pdf)
            self.assertIn('自评分×20%+主管评分×80%', text)
            self.assertIn('71.8', text)
            self.assertNotIn('KR完成率×60%', text)
        with okr_db.get_db() as db:
            db.execute('UPDATE okr_key_results SET current_value=90 WHERE objective_id=?', (first,))
        changed = self.member_result()
        self.assertEqual(71.8, changed['total_score'])
        self.assertEqual(90, changed['objectives'][0]['kr_completion_rate'])
        self.assertEqual(82, changed['objectives'][0]['composite_score'])
        with okr_db.get_db() as db:
            self.assertEqual(reviews, [tuple(row) for row in db.execute('SELECT * FROM okr_monthly_reviews ORDER BY id')])

    def test_partial_task_and_no_goal_members_do_not_publish_total(self):
        self.objective()
        self.objective(completed=False, weight=50)
        self.client.put(self.url, json=self.payload)
        result = self.member_result()
        self.assertEqual("pending_tasks", result["status"])
        self.assertEqual((1, 2), (result["completed_objective_count"], result["objective_count"]))
        self.assertIsNone(result["total_score"])
        self.assertEqual("no_objectives", self.member_result(self.peer)["status"])

    def test_company_goals_and_other_month_reviews_do_not_pollute_personal_score(self):
        oid = self.objective(score=60)
        self.objective(score=100, weight=99, kind="department")
        with okr_db.get_db() as db:
            db.execute("INSERT INTO okr_monthly_reviews(objective_id,year_month,self_score,manager_score,status) VALUES (?,'2026-09',100,100,'completed')", (oid,))
        self.assertEqual(60, self.member_result()["task_score"])
        listed = self.client.get('/api/okr/reviews/monthly', params={"period": self.period}).json()
        item = next(row for row in listed["dept_reviews"] if row["objective_id"] == oid)
        self.assertEqual(60, item["self_score"])

    def test_only_supervisors_in_scope_can_score(self):
        for actor, allowed in [(self.member, False), (self.peer, False), (self.head, True),
                               (self.admin, True), (self.company_head, True),
                               (self.foreign, False), (self.no_department, False), (self.root, True)]:
            with self.subTest(actor=actor["name"]):
                self.actor = actor
                with okr_db.get_db() as db:
                    db.execute('DELETE FROM okr_member_evaluations')
                self.assertEqual(200 if allowed else 403, self.client.put(self.url, json=self.payload).status_code)
        self.actor = self.head
        self.assertEqual(403, self.client.put(f"/api/okr/evaluations/monthly/{self.outside['id']}", json=self.payload).status_code)
        self.assertEqual(403, self.client.put(f"/api/okr/evaluations/monthly/{self.head['id']}", json=self.payload).status_code)
        self.actor = self.admin
        self.assertEqual(403, self.client.put(f"/api/okr/evaluations/monthly/{self.head['id']}", json=self.payload).status_code)

    def test_read_filters_cannot_expand_department_or_company_scope(self):
        for actor, params, allowed_ids in [
            (self.member, {}, {self.member["id"]}),
            (self.member, {"company_id": self.other_company}, {self.member["id"]}),
            (self.head, {"dept_id": self.other_dept}, set()),
            (self.no_department, {}, {self.no_department["id"]}),
            (self.admin, {"company_id": self.other_company}, {self.head["id"], self.company_head["id"], self.admin["id"], self.member["id"], self.peer["id"], self.outside["id"], self.no_department["id"]}),
        ]:
            with self.subTest(actor=actor["name"], params=params):
                self.actor = actor
                result = self.client.get('/api/okr/evaluations/monthly', params={"period": self.period, **params}).json()
                self.assertEqual(allowed_ids, {m["user_id"] for m in result["members"]})

    def test_company_head_can_create_and_correct_own_behavior_and_redline(self):
        self.objective(owner=self.company_head)
        url = f"/api/okr/evaluations/monthly/{self.company_head['id']}"
        self.assertTrue(self.member_result(self.company_head)["can_evaluate"])
        response = self.client.put(url, json=self.payload)
        self.assertEqual(200, response.status_code, response.text)
        first = response.json()["evaluation"]
        self.assertEqual(self.company_head["id"], first["reviewer_id"])
        self.assertEqual(97, self.member_result(self.company_head)["total_score"])
        correction = {**self.payload, "redline_factor": 0,
                      "redline_reason": "具体事实", "expected_revision": 1}
        response = self.client.put(url, json=correction)
        self.assertEqual(200, response.status_code, response.text)
        second = response.json()["evaluation"]
        self.assertEqual((1, 2), (first["revision"], second["revision"]))
        self.assertEqual(first["created_at"], second["created_at"])
        result = self.member_result(self.company_head)
        self.assertEqual(("completed", 0), (result["status"], result["total_score"]))
        self.assertEqual(409, self.client.put(url, json=correction).status_code)
        with okr_db.get_db() as db:
            logs = db.execute(
                "SELECT user_id, target_id, detail FROM audit_logs "
                "WHERE action='member_evaluation' ORDER BY id",
            ).fetchall()
        self.assertEqual(2, len(logs))
        self.assertTrue(all(row["user_id"] == row["target_id"] == self.company_head["id"]
                            for row in logs))
        self.assertIn('"before": {', logs[1]["detail"])

    def test_company_head_exception_does_not_allow_other_roles_to_score_self(self):
        for actor in (self.member, self.head, self.admin, self.root,
                      {**self.company_head, "is_company_head": False}):
            with self.subTest(role=actor["role"], company_head=actor.get("is_company_head")):
                self.actor = actor
                self.assertFalse(self.member_result(actor)["can_evaluate"])
                response = self.client.put(
                    f"/api/okr/evaluations/monthly/{actor['id']}", json=self.payload,
                )
                self.assertEqual(403, response.status_code)
        with okr_db.get_db() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM okr_member_evaluations').fetchone()[0])
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE action='member_evaluation'",
            ).fetchone()[0])

    def test_company_head_self_exception_preserves_company_scope(self):
        result = self.client.get('/api/okr/evaluations/monthly', params={
            "period": self.period, "company_id": self.other_company,
        }).json()
        self.assertNotIn(self.foreign["id"], {row["user_id"] for row in result["members"]})
        response = self.client.put(
            f"/api/okr/evaluations/monthly/{self.foreign['id']}", json=self.payload,
        )
        self.assertEqual(403, response.status_code)
        for company_id in (None, self.other_company):
            with self.subTest(company_id=company_id):
                self.actor = {**self.company_head, "company_id": company_id}
                response = self.client.put(
                    f"/api/okr/evaluations/monthly/{self.company_head['id']}", json=self.payload,
                )
                self.assertEqual(403, response.status_code)

    def test_edit_preserves_creation_time_audit_history_and_revision_conflicts(self):
        first = self.client.put(self.url, json=self.payload).json()["evaluation"]
        update = {**self.payload, "satisfaction_score": 50, "expected_revision": 1}
        second = self.client.put(self.url, json=update).json()["evaluation"]
        self.assertEqual((1, 2), (first["revision"], second["revision"]))
        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(409, self.client.put(self.url, json=update).status_code)
        self.assertEqual(50, self.member_result()["evaluation"]["satisfaction_score"])
        with okr_db.get_db() as db:
            logs = db.execute("SELECT detail FROM audit_logs WHERE action='member_evaluation'").fetchall()
        self.assertEqual(2, len(logs))
        self.assertIn('"before": {', logs[1]["detail"])

    def test_legacy_task_list_cannot_expand_department_scope(self):
        self.objective(self.outside, completed=False)
        self.actor = self.head
        response = self.client.get('/api/okr/reviews/monthly', params={"period": self.period, "dept_id": self.other_dept})
        self.assertEqual([], response.json()["reviews"])

    def test_task_review_creation_rejects_wrong_month(self):
        oid = self.objective(completed=False)
        self.actor = self.member
        response = self.client.post('/api/okr/reviews/monthly', json={"objective_id": oid, "year_month": "2026-09"})
        self.assertEqual(400, response.status_code)

    def test_stale_progress_snapshots_cannot_diverge_from_cards_or_export(self):
        oid = self.objective(score=60)
        with okr_db.get_db() as db:
            db.execute("UPDATE okr_objectives SET progress=100 WHERE id=?", (oid,))
            db.execute("UPDATE okr_key_results SET progress=100 WHERE objective_id=?", (oid,))
        self.client.put(self.url, json=self.payload)
        result = self.member_result()
        self.assertEqual(60, result["task_score"])
        card = self.client.get('/api/okr/reviews/monthly', params={"period": self.period}).json()["dept_reviews"][0]
        self.assertEqual(result["task_score"], card["composite_score"])
        export = self.client.get('/api/okr/reviews/monthly/export', params={"period": self.period}).json()
        self.assertEqual(60, export["rows"][0]["综合评分"])
        with okr_db.get_db() as db:
            self.assertEqual(100, db.execute("SELECT progress FROM okr_objectives WHERE id=?", (oid,)).fetchone()[0])

    def test_legacy_quarterly_task_assessments_remain_accessible(self):
        oid = self.objective(completed=False, period="2026-Q4")
        self.actor = self.member
        response = self.client.post('/api/okr/reviews/monthly', json={"objective_id": oid, "year_month": "2026-Q4"})
        self.assertEqual(200, response.status_code, response.text)
        self.actor = self.company_head
        response = self.client.get('/api/okr/reviews/monthly/export?period=2026-Q4')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], response.json()["summary_rows"])

    def test_month_boundaries_and_strict_score_validation(self):
        for field, value in [("satisfaction_score", -1), ("initiative_score", 101),
                             ("initiative_score", 90.5), ("satisfaction_score", "90"),
                             ("redline_factor", 2), ("redline_factor", True),
                             ("year_month", "2026-13"), ("year_month", "2026-Q4")]:
            with self.subTest(field=field, value=value):
                self.assertEqual(422, self.client.put(self.url, json={**self.payload, field: value}).status_code)
        self.assertEqual(422, self.client.get('/api/okr/evaluations/monthly?period=2026-99').status_code)
        self.assertEqual(200, self.client.put(self.url, json={**self.payload, "satisfaction_score": 0}).status_code)
        self.assertEqual(200, self.client.put(self.url, json={**self.payload, "year_month": "2026-11"}).status_code)
        self.assertEqual(0, self.member_result()["evaluation"]["satisfaction_score"])

    def test_reads_have_no_persistent_side_effects_and_migration_is_additive(self):
        self.objective()
        self.client.put(self.url, json=self.payload)
        with okr_db.get_db() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM okr_monthly_reviews')]
            evaluations = [tuple(row) for row in db.execute('SELECT * FROM okr_member_evaluations')]
        self.member_result()
        self.member_result()
        with contextlib.redirect_stdout(io.StringIO()):
            okr_db.init_db()
            okr_db.init_db()
        with okr_db.get_db() as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM okr_monthly_reviews')])
            self.assertEqual(evaluations, [tuple(row) for row in db.execute('SELECT * FROM okr_member_evaluations')])
            db.execute('DROP TABLE okr_member_evaluations')
        with contextlib.redirect_stdout(io.StringIO()):
            okr_db.init_db()
        with okr_db.get_db() as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM okr_monthly_reviews')])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM okr_member_evaluations').fetchone()[0])

    def test_completed_member_pdf_contains_only_selected_month_and_member_without_writes(self):
        self.objective(score=100, weight=1)
        self.objective(score=60, weight=3)
        self.objective(owner=self.peer, score=100)
        self.objective(period='2026-09')
        with okr_db.get_db() as db:
            db.execute("UPDATE okr_objectives SET title='其他成员不可混入' WHERE owner_id=?", (self.peer['id'],))
            db.execute("UPDATE okr_objectives SET title='其他月份不可混入' WHERE period='2026-09'")
            db.execute("UPDATE okr_monthly_reviews SET self_comment='个人说明', manager_comment='主管反馈' WHERE year_month=?", (self.period,))
        self.client.put(self.url, json=self.payload)
        with okr_db.get_db() as db:
            before = {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                      for table in ('okr_member_evaluations', 'okr_monthly_reviews', 'audit_logs')}
        response = self.client.get(f'{self.url}/pdf', params={'period': self.period})
        self.assertEqual(200, response.status_code, response.text[:100] if response.status_code != 200 else '')
        self.assertEqual('application/pdf', response.headers['content-type'])
        self.assertIn("filename*=UTF-8''", response.headers['content-disposition'])
        self.assertEqual('private, no-store', response.headers['cache-control'])
        with fitz.open(stream=response.content, filetype='pdf') as document:
            text = ''.join(page.get_text() for page in document)
            for expected in ('员工月度目标评估表', self.member['name'], self.period, '76', '49', '27',
                             '内外部满意度', '主动开展工作', '个人说明', '主管反馈', '测试关键结果'):
                self.assertIn(expected, text)
            self.assertNotIn('其他成员不可混入', text)
            self.assertNotIn('其他月份不可混入', text)
            self.assertTrue(all(page.rect.width < page.rect.height for page in document))
        with okr_db.get_db() as db:
            self.assertEqual(before, {table: [tuple(row) for row in db.execute('SELECT * FROM ' + table)]
                                     for table in before})

    def test_pdf_enforces_scope_completion_and_month_validation(self):
        self.objective()
        self.assertEqual(409, self.client.get(f'{self.url}/pdf', params={'period': self.period}).status_code)
        self.client.put(self.url, json=self.payload)
        for actor, allowed in ((self.member, True), (self.peer, False), (self.head, True),
                               (self.admin, True), (self.company_head, True), (self.foreign, False),
                               (self.no_department, False), (self.root, True)):
            with self.subTest(actor=actor['name']):
                self.actor = actor
                self.assertEqual(200 if allowed else 404, self.client.get(
                    f'{self.url}/pdf', params={'period': self.period}).status_code)
        self.actor = self.company_head
        self.assertEqual(409, self.client.get(f'{self.url}/pdf?period=2026-11').status_code)
        self.assertEqual(422, self.client.get(f'{self.url}/pdf?period=2026-Q4').status_code)
        self.assertEqual(404, self.client.get('/api/okr/evaluations/monthly/999999/pdf?period=2026-10').status_code)
        self.objective(completed=False)
        self.assertEqual(409, self.client.get(f'{self.url}/pdf', params={'period': self.period}).status_code)

    def test_zero_total_pdf_and_long_escaped_content_paginate_without_losing_tail(self):
        oid = self.objective()
        self.client.put(self.url, json={**self.payload, 'redline_factor': 0, 'redline_reason': '具体事实'})
        with okr_db.get_db() as db:
            db.execute("UPDATE okr_objectives SET title=?, description=? WHERE id=?",
                       ('目标 <script>字面文本</script> & 符号', '长描述内容。' * 1600 + '描述终点', oid))
            db.execute("UPDATE okr_monthly_reviews SET self_comment=? WHERE objective_id=?",
                       ('复盘说明。' * 1200 + '评语终点', oid))
        response = self.client.get(f'{self.url}/pdf', params={'period': self.period})
        self.assertEqual(200, response.status_code)
        with fitz.open(stream=response.content, filetype='pdf') as document:
            self.assertGreater(len(document), 2)
            text = ''.join(page.get_text() for page in document)
            for expected in ('<script>字面文本</script>', '描述终点', '评语终点', '具体事实', '月度总分归零'):
                self.assertIn(expected, text)
            self.assertFalse(any(page.get_links() for page in document))


if __name__ == '__main__':
    unittest.main()
