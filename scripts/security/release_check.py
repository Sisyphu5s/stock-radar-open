"""Reject obvious secrets, machine paths, and runtime artifacts in tracked files.

This is a release guard, not a replacement for credential rotation or human review.
"""

from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
FILES = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
FORBIDDEN_PARTS = {".venv", "node_modules", "dist", ".pytest_cache", "__pycache__"}
FORBIDDEN_SUFFIXES = (
    ".db", ".sqlite", ".sqlite3", ".pkl", ".pickle", ".parquet",
    ".npz", ".feather", ".arrow", ".dylib", ".session", ".tar.gz",
    ".pem", ".key", ".p8", ".p12", ".pfx", ".log", ".tmp", ".bak",
)
PATTERNS = {
    "local filesystem path": re.compile(rb"/(?:Users|Volumes)/[A-Za-z0-9_.-]+/"),
    "private network address": re.compile(rb"(?:192\.168|100\.123)\.\d{1,3}\.\d{1,3}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub credential": re.compile(rb"(?:github_pat_|ghp_)[A-Za-z0-9_]{20,}"),
    "AWS access key": re.compile(rb"AKIA[0-9A-Z]{16}"),
    "API credential": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Bearer credential": re.compile(rb"\bBearer [A-Za-z0-9._~-]{20,}\b"),
}
issues = []
for name in filter(None, FILES):
    rel = PurePosixPath(name)
    path = ROOT / name
    if path.is_symlink():
        issues.append((name, "symbolic link"))
        continue
    if set(rel.parts) & FORBIDDEN_PARTS or rel.name == ".env" or (
        rel.name.startswith(".env.") and rel.name != ".env.example"
    ) or rel.name.endswith(FORBIDDEN_SUFFIXES) or ".db-" in rel.name:
        issues.append((name, "runtime or private file"))
        continue
    data = path.read_bytes()
    if len(data) > 5_000_000 or b"\0" in data:
        issues.append((name, "large or binary file"))
        continue
    for label, pattern in PATTERNS.items():
        if pattern.search(data):
            issues.append((name, label))
for name, reason in issues:
    print(f"{name}: {reason}", file=sys.stderr)
if issues:
    raise SystemExit(f"release check failed: {len(issues)} issue(s)")
print(f"release check passed: {len(FILES) - 1} tracked files")
