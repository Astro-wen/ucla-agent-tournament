"""scripts/publish_sync.sh: the export of one commit's competition code for a separate public repository (Official
Rules section 8; option C of the 10-08 pre-publication scan). Only the fast paths run here (--scan-only, --list and
--no-verify exports of HEAD); the install check (clean venv, npm ci, selfcheck, the exported tests) is the dry run
recorded in docs/progress.md. Planted keys and addresses are built at run time so this file never holds one."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "publish_sync.sh"
needs_git = pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bash") is None or shutil.which("git") is None
    or shutil.which("realpath") is None or not (ROOT / ".git").exists(),
    reason="publish_sync.sh exports from this git checkout (Linux, bash, git)")


def run(*args, timeout=300):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def git(*args, cwd=ROOT):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


@needs_git
def test_the_scan_flags_planted_secrets_and_leaves_a_clean_tree_alone(tmp_path):
    clean = tmp_path / "clean"
    (clean / "agent").mkdir(parents=True)
    (clean / "agent" / "x.py").write_text('KEY = "eak_live_your_key_here"\n# shown to spectators next to the move\n'
                                          'AUTHOR = "202232165+someone@users.noreply.github.com"\n')
    (clean / ".env.example").write_text("ALTRUAGENT_OFFICIAL_AGENT_KEY=eak_live_your_key_here\n")
    r = run("--scan-only", str(clean))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "scan: clean" in r.stdout

    dirty = tmp_path / "dirty"
    (dirty / "a").mkdir(parents=True)
    key = "eak_live_" + "ab" * 32
    (dirty / "a" / "k.py").write_text(f'K = "{key}"\n')
    (dirty / "a" / "m.md").write_text("write to some.one@" + "ucla.edu\n")
    (dirty / "a" / "n.md").write_text("as " + "Sandra said\n")
    (dirty / "a" / "p.txt").write_text("/home/" + "someuser/x\n")
    (dirty / "a" / "w.py").write_text("# the spectate " + "feed shows it\n")
    (dirty / ".env").write_text("X=1\n")
    r = run("--scan-only", str(dirty))
    assert r.returncode == 3, r.stdout + r.stderr
    for tag in ("KEY FILE  .env", "KEY       a/k.py:1", "EMAIL     a/m.md:1", "PRIVATE   a/n.md:1",
                "PATH      a/p.txt:1", "ISSUE     a/w.py:1"):
        assert tag in r.stdout, tag
    assert key not in r.stdout and "scan: 6 finding(s)" in r.stdout


@needs_git
def test_the_export_holds_only_the_competition_files_of_the_commit(tmp_path):
    out = tmp_path / "pub"
    r = run("--out", str(out), "--no-verify")
    # 3 = findings are reported (e.g. wording still to neutralize in agent/); never a crash or a refusal
    assert r.returncode in (0, 3), r.stdout + r.stderr
    assert (out / "README.md").read_text(encoding="utf-8") == git("show", "HEAD:PUBLIC_README.md")
    for p in ("agent/agent.py", "agent/js/search.js", "altruagent/runner.py", "data/vgc_cards.json",
              "deploy/Dockerfile", "scripts/run_agent.sh", "scripts/selfcheck.py", "scripts/bundle.sh",
              "local/js/package-lock.json", "docs/deploy.md", "NOTICE", "requirements.lock", ".env.example",
              ".gitignore", "tests/conftest.py"):
        assert (out / p).is_file(), p
    for p in ("CLAUDE.md", "PUBLIC_README.md", "scripts/publish_sync.sh", "scripts/freeze_champion.sh",
              "docs/HANDOFF.md", "docs/competition_brief.md", "docs/progress.md", "docs/platform", "champion",
              "versions", "examples", "local/fake_platform.py", "local/js/bridge.js", ".env", ".github"):
        assert not (out / p).exists(), p
    assert os.access(out / "scripts" / "run_agent.sh", os.X_OK)
    # one whole sample match (both seats) and only tests that need nothing outside the export
    assert len(list((out / "logs" / "yoriko").glob("*.jsonl"))) == 1
    assert len(list((out / "logs" / "apocalypse").glob("*.jsonl"))) == 1
    tests = sorted(p.relative_to(out).as_posix() for p in (out / "tests").rglob("test_*.py"))
    assert tests and "tests/test_deploy.py" not in tests and "tests/test_publish_sync.py" not in tests
    for t in tests:
        body = (out / t).read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\s+(local|champion|versions|examples)\b", body, re.M), t
        # a test another exported test imports is exported too (test_console / test_lease import test_tournament)
        for m in re.findall(r"^\s*(?:from|import)\s+(?:tests(?:\.\w+)*\.)?(test_\w+)", body, re.M):
            assert list((out / "tests").rglob(f"{m}.py")), (t, m)
        for m in re.findall(r"^\s*from\s+tests(?:\.\w+)*\s+import\s+(test_\w+)", body, re.M):
            assert list((out / "tests").rglob(f"{m}.py")), (t, m)
    # the tree hash it prints is what a commit of exactly these files records, and the code equals the commit's
    tree = re.search(r"^  tree ([0-9a-f]{40})$", r.stdout, re.M).group(1)
    assert "every exported path is identical to commit" in r.stdout
    git("init", "-q", cwd=out)
    git("add", "-A", cwd=out)
    assert git("write-tree", cwd=out).strip() == tree
    assert git("rev-parse", "HEAD:agent").strip() == git("rev-parse", f"{tree}:agent", cwd=out).strip()


@needs_git
def test_it_refuses_unsafe_targets_and_replace_keeps_a_clones_git(tmp_path):
    inside = ROOT / "publish-export-test-dir"
    r = run("--out", str(inside), "--no-verify")
    assert r.returncode == 2 and "refusing" in r.stderr
    assert not inside.exists()                          # (refused before anything was created)
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (clone / "old.txt").write_text("from an earlier sync\n")
    r = run("--out", str(clone), "--no-verify")
    assert r.returncode == 2 and "not empty" in r.stderr
    assert (clone / "old.txt").exists()
    r = run("--out", str(clone), "--no-verify", "--replace")
    assert r.returncode in (0, 3), r.stdout + r.stderr
    assert not (clone / "old.txt").exists() and (clone / "agent" / "agent.py").is_file()
    assert (clone / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"


@needs_git
def test_list_names_the_exported_paths_without_writing_anything(tmp_path):
    r = run("--list")
    assert r.returncode == 0, r.stdout + r.stderr
    lines = r.stdout.splitlines()
    assert "agent/agent.py" in lines and "scripts/run_agent.sh" in lines and "README.md  (from PUBLIC_README.md)" in lines
    assert not any(x.startswith(("local/", "docs/")) and x not in (
        "local/js/package.json", "local/js/package-lock.json", "docs/deploy.md", "docs/restart_load.md")
        for x in lines)
    assert "scripts/publish_sync.sh" not in lines and "CLAUDE.md" not in lines
