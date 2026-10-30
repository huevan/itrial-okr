"""Create local-only configuration and an operator-selected administrator."""
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    config_path = ROOT / "config.json"
    if config_path.exists() or (ROOT / "data" / "okr.db").exists():
        raise SystemExit("Configuration or database already exists; setup never overwrites existing data")
    phone = os.environ.get("OKR_BOOTSTRAP_PHONE") or input("Bootstrap administrator phone (11 digits): ").strip()
    password = os.environ.get("OKR_BOOTSTRAP_PASSWORD") or getpass.getpass("Bootstrap administrator password (12+ characters): ")
    if not re.fullmatch(r"1\d{10}", phone) or len(password) < 12:
        raise SystemExit("A valid 11-digit phone and a password of at least 12 characters are required")
    config = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    config["jwt_secret"] = secrets.token_urlsafe(48)
    descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    previous = {key: os.environ.get(key) for key in ("OKR_BOOTSTRAP_PHONE", "OKR_BOOTSTRAP_PASSWORD")}
    try:
        os.environ["OKR_BOOTSTRAP_PHONE"] = phone
        os.environ["OKR_BOOTSTRAP_PASSWORD"] = password
        from okr.public import init_db
        init_db()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    print("Local setup complete. Configure Enterprise WeChat and SMS before member login.")
    print("Start with: uvicorn app:app --host 127.0.0.1 --port 8888")


if __name__ == "__main__":
    main()
