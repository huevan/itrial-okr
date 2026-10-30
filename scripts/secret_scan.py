"""Scan publishable text only; report paths and rule names, never secret values."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "venv", "__pycache__", "data", "dist", "build"}
RULES = {
    "private-key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "github-token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b",
    "cloud-access-key": r"\b(?:AKIA[0-9A-Z]{16}|AKID[A-Za-z0-9]{20,})\b",
    "jwt-literal": r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b",
    "credential-url": r"https?://[^\s/:]+:[^\s/@]+@",
    "server-ip": r"(?<![\d.])(?!(?:127\.0\.0\.1|0\.0\.0\.0)\b)(?:\d{1,3}\.){3}\d{1,3}(?![\d.])",
}


def main():
    problems = []
    count = 0
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(part in SKIP for part in rel.parts):
            continue
        if path.name == "config.json" or path.name.startswith(".env") or path.suffix == ".log":
            continue  # Runtime-only files are excluded from Git and upload manifests.
        if path.suffix not in {".py", ".js", ".css", ".html", ".json", ".md", ".yml", ".yaml", ".txt", ".svg"}:
            continue
        content = path.read_text(encoding="utf-8")
        count += 1
        for rule, pattern in RULES.items():
            if rule == "server-ip" and path.name == "requirements.txt":
                continue  # Four-part package versions are not network addresses.
            if re.search(pattern, content):
                problems.append(f"{rel.as_posix()}: {rule}")
    if problems:
        raise SystemExit("Publish scan failed:\n" + "\n".join(problems))
    print(f"Publish scan passed ({count} text files)")


if __name__ == "__main__":
    main()
