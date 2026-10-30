import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import okr_api
import okr_db
from fastapi import HTTPException, Request
from okr_models import PhoneVerifyRequest, RoleSwitchRequest, SendSmsRequest
from okr_sms import TencentSmsConfig, build_tencent_sms_headers


class OkrWecomAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_db_path = okr_db.DB_PATH
        okr_db.DB_PATH = str(Path(self.tempdir.name) / "okr.db")
        okr_db.init_db()
        with okr_db.get_db() as db:
            db.execute("INSERT INTO companies(name, status) VALUES ('测试企业', 'active')")
            self.company_id = db.execute("SELECT id FROM companies WHERE name='测试企业'").fetchone()[0]
        self.wecom_config = SimpleNamespace(company_id=self.company_id, corp_id="ww-test", corp_name="测试企业", leader_role="superAdmin")

    def tearDown(self):
        okr_db.DB_PATH = self.original_db_path
        self.tempdir.cleanup()

    def wecom_user(self, leader=True):
        return {"userid":"zhangsan","name":"张三","mobile":"13811112222","main_department":10,"department_details":[{"id":10,"name":"研发部","is_leader":leader},{"id":11,"name":"项目组","is_leader":leader}]}

    def test_leader_gets_superadmin_and_phone_requires_sms(self):
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            user = okr_api._upsert_wecom_account(self.wecom_user())
        self.assertEqual(user["role"], "superAdmin")
        self.assertEqual(user["status"], "active")
        self.assertEqual(user["phone_verified"], 0)
        with okr_db.get_db() as db:
            heads = db.execute("SELECT COUNT(*) FROM departments WHERE head_user_id=? AND wecom_department_id IN (10,11)",(user["id"],)).fetchone()[0]
        self.assertEqual(heads, 2)

    def test_managed_superadmin_is_demoted_after_losing_leader_status(self):
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            leader = okr_api._upsert_wecom_account(self.wecom_user(True))
            member = okr_api._upsert_wecom_account(self.wecom_user(False))
        self.assertEqual(leader["role"], "superAdmin")
        self.assertEqual(leader["wecom_role_managed"], 1)
        self.assertEqual(member["role"], "user")
        self.assertEqual(member["wecom_role_managed"], 1)

    def test_first_wecom_link_for_legacy_user_still_requires_sms(self):
        with okr_db.get_db() as db:
            db.execute("INSERT INTO users(phone,password_hash,name,role,company_id,status) VALUES (?,?,?,?,?,'active')",("13811112222","hash","历史张三","user",self.company_id))
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            user = okr_api._upsert_wecom_account(self.wecom_user(False))
        self.assertEqual(user["phone"], "13811112222")
        self.assertEqual(user["phone_verified"], 0)
        self.assertEqual(user["wecom_userid"], "zhangsan")

    def test_sms_send_and_verify_gate(self):
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            user = okr_api._upsert_wecom_account(self.wecom_user())
        auth_user={"id":user["id"],"wecom_userid":user["wecom_userid"],"phone_verified":False,"role":user["role"],"name":user["name"],"company_id":user["company_id"],"department_id":user["department_id"]}
        async def fake_send(phone, code):
            self.sent_code=code
            return "request-test"
        sms_config=SimpleNamespace(ttl_minutes=5)
        with patch.object(okr_api,"send_tencent_verification_code",side_effect=fake_send),patch.object(okr_api,"get_tencent_sms_config",return_value=sms_config):
            result=asyncio.run(okr_api.auth_send_sms(SendSmsRequest(phone="13811112222"),auth_user))
        self.assertTrue(result["ok"])
        with okr_db.get_db() as db:
            saved=db.execute("SELECT code,request_id FROM sms_codes WHERE user_id=?",(user["id"],)).fetchone()
        self.assertNotEqual(saved["code"],self.sent_code)
        self.assertEqual(saved["request_id"],"request-test")
        verified=asyncio.run(okr_api.auth_verify_phone(PhoneVerifyRequest(phone="13811112222",code=self.sent_code),auth_user))
        self.assertTrue(verified["user"]["phone_verified"])
        self.assertEqual(verified["user"]["phone"],"13811112222")

    def test_phone_verification_merges_temporary_wecom_user_into_legacy_account(self):
        wecom_user = self.wecom_user(False)
        wecom_user["mobile"] = ""
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            temporary = okr_api._upsert_wecom_account(wecom_user)
        with okr_db.get_db() as db:
            cursor = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) VALUES (?,?,?,?,?,'active')",
                ("13811112222", "hash", "历史张三", "user", self.company_id),
            )
            legacy_id = cursor.lastrowid
            db.execute(
                "INSERT INTO sms_codes(phone,code,user_id,purpose,expires_at) "
                "VALUES (?,?,?,?,datetime('now','+5 minutes'))",
                ("13811112222", okr_api._sms_code_hash("13811112222", "123456"),
                 temporary["id"], okr_api._SMS_PURPOSE),
            )
        auth_user = {
            "id": temporary["id"], "wecom_userid": temporary["wecom_userid"],
            "phone_verified": False, "role": temporary["role"], "name": temporary["name"],
            "company_id": temporary["company_id"], "department_id": temporary["department_id"],
        }
        result = asyncio.run(okr_api.auth_verify_phone(
            PhoneVerifyRequest(phone="13811112222", code="123456"), auth_user
        ))
        self.assertEqual(result["user"]["id"], legacy_id)
        self.assertTrue(result["user"]["phone_verified"])
        with okr_db.get_db() as db:
            self.assertIsNone(db.execute("SELECT id FROM users WHERE id=?", (temporary["id"],)).fetchone())
            merged = db.execute("SELECT wecom_userid FROM users WHERE id=?", (legacy_id,)).fetchone()
        self.assertEqual(merged["wecom_userid"], "zhangsan")

    def test_dual_role_leader_defaults_to_department_head_and_can_switch(self):
        with patch.object(okr_api, "get_okr_wecom_config", return_value=self.wecom_config):
            user = okr_api._upsert_wecom_account(self.wecom_user())
        with okr_db.get_db() as db:
            db.execute("UPDATE users SET phone_verified=1 WHERE id=?", (user["id"],))

        initial = okr_api._token_response(user)
        self.assertEqual(initial["user"]["role"], "dept_head")
        self.assertEqual(initial["user"]["base_role"], "superAdmin")
        self.assertEqual(initial["user"]["available_roles"], ["dept_head", "superAdmin"])

        request = Request({
            "type": "http",
            "headers": [(b"authorization", f"Bearer {initial['token']}".encode())],
        })
        authenticated = asyncio.run(okr_api.get_authenticated_user(request))
        self.assertEqual(authenticated["role"], "dept_head")

        switched = asyncio.run(okr_api.auth_switch_role(
            RoleSwitchRequest(role="superAdmin"), authenticated
        ))
        self.assertEqual(switched["user"]["role"], "superAdmin")
        switched_request = Request({
            "type": "http",
            "headers": [(b"authorization", f"Bearer {switched['token']}".encode())],
        })
        switched_user = asyncio.run(okr_api.get_authenticated_user(switched_request))
        self.assertEqual(switched_user["role"], "superAdmin")

        switched_back = asyncio.run(okr_api.auth_switch_role(
            RoleSwitchRequest(role="dept_head"), switched_user
        ))
        self.assertEqual(switched_back["user"]["role"], "dept_head")

    def test_department_head_without_superadmin_grant_cannot_escalate(self):
        with okr_db.get_db() as db:
            cursor = db.execute(
                "INSERT INTO users(phone,password_hash,name,role,company_id,status) "
                "VALUES (?,?,?,?,?,'active')",
                ("13899990000", "hash", "Department Head", "dept_head", self.company_id),
            )
            user_id = int(cursor.lastrowid)
        current = {
            "id": user_id, "phone_verified": True, "role": "dept_head",
            "base_role": "dept_head", "available_roles": ["dept_head"],
        }
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(okr_api.auth_switch_role(
                RoleSwitchRequest(role="superAdmin"), current
            ))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_unverified_user_is_blocked_from_business_dependencies(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(okr_api.get_current_user({"phone_verified":False}))
        self.assertEqual(ctx.exception.status_code,403)

    def test_tencent_tc3_headers_are_complete(self):
        config=TencentSmsConfig(secret_id="id",secret_key="key",sdk_app_id="app",sign_name="sign",template_id="template",template_params=("code",),region="ap-guangzhou",ttl_minutes=5)
        headers=build_tencent_sms_headers(config,b"{}",timestamp=1700000000)
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 Credential=id/"))
        self.assertEqual(headers["X-TC-Action"],"SendSms")
        self.assertEqual(headers["X-TC-Version"],"2021-01-11")


if __name__ == "__main__":
    unittest.main()
