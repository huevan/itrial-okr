"""Run architecture, publish-safety, syntax and isolated OKR regression checks."""
import argparse
import ast
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-only", action="store_true")
    args = parser.parse_args()
    for folder in (ROOT / "okr", ROOT / "scripts", ROOT / "tests"):
        for path in folder.glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    subprocess.run([sys.executable, "scripts/architecture_guard.py"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "scripts/secret_scan.py"], cwd=ROOT, check=True)
    if args.static_only:
        return
    import okr_api
    import okr_auth
    import okr_sms
    import okr_wecom
    with tempfile.TemporaryDirectory(prefix="okr-tests-") as temp:
        config = Path(temp) / "config.json"
        config.write_text(json.dumps({"jwt_secret": secrets.token_urlsafe(48)}), encoding="utf-8")
        okr_api._CFG_PATH = okr_auth._CFG_PATH = str(config)
        okr_sms.CONFIG_PATH = okr_wecom.CONFIG_PATH = config
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test_okr*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        if not result.wasSuccessful():
            raise SystemExit(1)


if __name__ == "__main__":
    main()
