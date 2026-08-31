"""Check staged source for private files and build a ZIP from a clean commit."""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PARTS = {"data", ".venv", "venv", "__pycache__", "node_modules", "dist", "build", ".env", ".git", ".codex", ".agents"}
SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "github-token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "api-token": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "personal-windows-path": re.compile(rb"(?i)[a-z]:[\\/]Users[\\/](?!Public[\\/]|example[\\/]|user[\\/])[^\s\\/]+"),
}
PRIVATE_DOMAIN = re.compile(rb"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.ts\.net\b", re.I)


def git(*args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace").strip() or "Git command failed")
    return result.stdout


def unsafe_path(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    parts = {part.lower() for part in path.parts}
    base = path.name.lower()
    if path.is_absolute() or ".." in parts or parts & EXCLUDED_PARTS:
        return True
    if base.startswith(".env") and base != ".env.example":
        return True
    if base == "config.json" or (base.startswith("config.") and base.endswith(".json") and base != "config.example.json"):
        return True
    if base in {"remote-access.json", "news-agent.json", "news-agent-result.json"}:
        return True
    return bool(re.search(r"\.(?:pyc|pyo|log|db|sqlite\d*|key|pem|pfx|p12|pending|bak|backup|zip)(?:-.*)?$", base))


def findings(name: str, content: bytes) -> list[str]:
    problems = [f"{name}: excluded-private-or-generated-file"] if unsafe_path(name) else []
    for rule, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(content):
            line = content[:match.start()].count(b"\n") + 1
            problems.append(f"{name}:{line}: {rule}")
    for match in PRIVATE_DOMAIN.finditer(content):
        if not match.group().lower().endswith(b".example.ts.net"):
            line = content[:match.start()].count(b"\n") + 1
            problems.append(f"{name}:{line}: personal-tailscale-domain")
    return problems


def staged_files() -> list[tuple[str, str]]:
    rows = git("ls-files", "--stage", "-z").split(b"\0")
    files = []
    for row in rows:
        if not row:
            continue
        metadata, raw_name = row.split(b"\t", 1)
        mode, _oid, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise RuntimeError("Resolve merge conflicts before publishing")
        files.append((raw_name.decode("utf-8"), mode))
    if not files:
        raise RuntimeError("No staged/tracked source files. Review files and add them to Git first.")
    return files


def check() -> int:
    files = staged_files()
    problems = []
    for name, mode in files:
        if mode not in {"100644", "100755"}:
            problems.append(f"{name}: unsupported-link-or-submodule")
            continue
        problems.extend(findings(name, git("show", f":{name}")))
    if problems:
        print("Publish check failed (matched secret values are not displayed):", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"PASS: {len(files)} tracked source files; no prohibited paths or known secret patterns.")
    return 0


def build() -> int:
    if check():
        return 1
    if git("status", "--porcelain").strip():
        raise RuntimeError("Commit all intended source changes before building; the worktree must be clean.")
    commit = git("rev-parse", "HEAD").decode().strip()
    archive = git("archive", "--format=zip", "--prefix=stock-alert-desktop/", commit)
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        for entry in zipped.infolist():
            if entry.is_dir():
                continue
            relative = entry.filename.removeprefix("stock-alert-desktop/")
            if findings(relative, zipped.read(entry)):
                raise RuntimeError("Committed archive failed publication checks")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    target = output / f"stock-alert-desktop-{commit[:12]}.zip"
    # Never silently overwrite an existing release artifact.
    with target.open("xb") as stream:
        stream.write(archive)
    digest = hashlib.sha256(archive).hexdigest()
    target.with_suffix(".zip.sha256").write_text(f"{digest}  {target.name}\n", encoding="ascii")
    print(f"Created {target.name} ({len(archive)} bytes), commit {commit}")
    print(f"SHA256 {digest}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "build"], default="check", nargs="?")
    args = parser.parse_args()
    try:
        return build() if args.action == "build" else check()
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
