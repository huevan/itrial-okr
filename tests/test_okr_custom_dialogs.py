import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
OKR_STATIC = ROOT / "static" / "okr"
PAGE_FILES = ("index.html", "admin.html", "login.html")
NATIVE_DIALOG_CALL = re.compile(r"\b(?:window\.)?(?:alert|confirm|prompt)\s*\(")


class OkrCustomDialogTests(unittest.TestCase):
    def test_okr_frontend_has_no_native_browser_dialog_calls(self):
        offenders = []
        for path in OKR_STATIC.iterdir():
            if path.suffix not in {".html", ".js"}:
                continue
            text = path.read_text(encoding="utf-8")
            if NATIVE_DIALOG_CALL.search(text):
                offenders.append(path.name)
        self.assertEqual([], offenders)

    def test_shared_confirm_component_has_accessible_interactions(self):
        common_js = (OKR_STATIC / "okr-common.js").read_text(encoding="utf-8")
        common_css = (OKR_STATIC / "okr-common.css").read_text(encoding="utf-8")

        self.assertIn("function confirmAction(options = {})", common_js)
        self.assertIn("return new Promise(resolve =>", common_js)
        self.assertIn('role="dialog"', common_js)
        self.assertIn("aria-modal", common_js)
        self.assertIn("event.key === 'Escape'", common_js)
        self.assertIn("event.key !== 'Tab'", common_js)
        self.assertIn("previousFocus.focus()", common_js)
        self.assertIn(".confirm-layer", common_css)
        self.assertIn(".confirm-dialog", common_css)
        self.assertIn(".confirm-danger", common_css)
        self.assertIn(".confirm-warning", common_css)

    def test_all_okr_pages_load_the_versioned_shared_dialog_assets(self):
        expected_version = "20260816-confirm-dialog"
        for filename in PAGE_FILES:
            html = (OKR_STATIC / filename).read_text(encoding="utf-8")
            self.assertIn(f"okr-common.css?v={expected_version}", html)
            self.assertIn(f"okr-common.js?v={expected_version}", html)

    def test_all_state_changing_confirmations_use_the_shared_component(self):
        index_html = (OKR_STATIC / "index.html").read_text(encoding="utf-8")
        admin_html = (OKR_STATIC / "admin.html").read_text(encoding="utf-8")

        self.assertEqual(8, index_html.count("await confirmAction("))
        self.assertEqual(6, admin_html.count("await confirmAction("))
        self.assertNotIn("withdrawReviewModal", index_html)
        self.assertNotIn("openWithdrawReviewModal", index_html)


if __name__ == "__main__":
    unittest.main()
