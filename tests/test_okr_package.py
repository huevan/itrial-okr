"""Regression coverage for package extraction and existing host integrations."""
import importlib
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from okr import api, auth, db, paths
from okr.models import UserCreate


class OkrPackageTests(unittest.TestCase):
    def test_public_model_qualified_name_is_preserved(self):
        self.assertEqual("okr_models", UserCreate.__module__)
        self.assertTrue(UserCreate.__pydantic_core_schema__["ref"].startswith("okr_models.UserCreate:"))

    def test_legacy_imports_reference_the_same_modules(self):
        for name in (
            "api", "auth", "db", "models", "evaluation", "evaluation_api",
            "evaluation_service", "evaluation_report", "progress", "sms", "wecom",
        ):
            with self.subTest(module=name):
                self.assertIs(importlib.import_module(f"okr_{name}"),
                              importlib.import_module(f"okr.{name}"))

    def test_legacy_database_and_auth_patches_reach_the_implementation(self):
        legacy_db = importlib.import_module("okr_db")
        legacy_auth = importlib.import_module("okr_auth")
        with patch.object(legacy_db, "DB_PATH", "isolated-test.db"):
            self.assertEqual("isolated-test.db", db.DB_PATH)
        with patch.object(legacy_auth, "_jwt_secret", return_value="test-only"):
            self.assertEqual("test-only", auth._jwt_secret())
        self.assertIs(legacy_auth.create_token, auth.create_token)

    def test_resources_still_resolve_to_the_original_deployment_root(self):
        root = Path(__file__).resolve().parents[1]
        self.assertEqual(root / "config.json", paths.CONFIG_PATH)
        self.assertEqual(root / "data" / "okr.db", Path(db.DB_PATH))
        self.assertEqual(root / "static" / "okr", Path(api._STATIC))

    def test_existing_pages_serve_the_original_static_files(self):
        app = FastAPI()
        app.include_router(api.okr_router)
        with TestClient(app) as client:
            for url, filename in (("/okr", "index.html"),
                                  ("/okr/login", "login.html"),
                                  ("/okr/admin", "admin.html")):
                with self.subTest(url=url):
                    response = client.get(url)
                    self.assertEqual(200, response.status_code)
                    self.assertEqual((paths.STATIC_DIR / filename).read_bytes(), response.content)
            self.assertEqual(401, client.get("/api/okr/auth/me").status_code)


if __name__ == "__main__":
    unittest.main()
