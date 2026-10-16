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
    not sys.platform.startswith(("linux", "darwin")) or shutil.which("bash") is None or shutil.which("git") is None
    or not (ROOT / ".git").exists(),
    reason="publish_sync.sh exports from this git checkout (Linux/macOS, bash, git, Python)")
needs_listing = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="read-only export listing needs bash and git (no Linux deployment tools)")


def run(*args, timeout=300):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def git(*args, cwd=ROOT):
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, capture_output=True, text=True,
                          check=True).stdout


def _commit_fixture(root, message):
    git("-c", "user.name=Aaron Wen", "-c", "user.email=siyuanwe@g.ucla.edu",
        "-c", "commit.gpgsign=false", "commit", "-qm", message, cwd=root)
    return git("rev-parse", "HEAD", cwd=root).strip()


@pytest.fixture
def readme_history(tmp_path):
    """Selected-commit fixtures, with both README names before the public rename."""
    root = tmp_path / "source"
    root.mkdir()
    # Minimal files for every mandatory export path; no live runtime or dependencies.
    paths = (
        "agent/hok/policy.py", "altruagent/__init__.py", "assets/art.txt", "data/cards.json",
        "deploy/Dockerfile", "scripts/run_agent.sh", "scripts/selfcheck.py", "scripts/check_connection.py",
        "scripts/replay_real.py", "scripts/bundle.sh", "local/js/package.json", "local/js/package-lock.json",
        "docs/deploy.md", "docs/restart_load.md", "pyproject.toml", "requirements.lock", ".env.example",
        ".gitignore", ".gitattributes", ".dockerignore", "GAMES.md", "NOTICE",
    )
    for name in paths:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    shutil.copyfile(SCRIPT, root / "scripts" / "publish_sync.sh")
    (root / "README.md").write_text("Legacy internal README: must never be exported.\n", encoding="utf-8")
    (root / "PUBLIC_README.md").write_text("Legacy public README.\n", encoding="utf-8")
    git("init", "-q", cwd=root)
    git("add", "-A", cwd=root)
    legacy = _commit_fixture(root, "Legacy public README")
    (root / "PUBLIC_README.md").unlink()
    (root / "README.md").write_text("Current public README.\n", encoding="utf-8")
    (root / "LICENSE").write_text("Committed license fixture.\n", encoding="utf-8")
    git("add", "-A", cwd=root)
    current = _commit_fixture(root, "Rename public README and add license")
    # Neither a dirty README/LICENSE nor an untracked legacy filename may influence selection.
    for name in ("README.md", "PUBLIC_README.md", "LICENSE"):
        (root / name).write_text(f"Working-tree-only {name}.\n", encoding="utf-8")
    return root, legacy, current


@needs_listing
@pytest.mark.parametrize("revision,readme,licensed", [(1, "PUBLIC_README.md", False), (2, "README.md", True)])
def test_listing_selects_readme_and_optional_license_from_the_requested_commit(readme_history, revision, readme,
                                                                            licensed):
    root = readme_history[0]
    before = git("status", "--porcelain", cwd=root)
    result = subprocess.run(
        ["bash", str(root / "scripts" / "publish_sync.sh"), "--list", "--commit", readme_history[revision]],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert f"README.md  (from {readme})" in lines
    assert ("LICENSE" in lines) is licensed
    assert {"agent/hok/policy.py", "assets/art.txt", "NOTICE"} <= set(lines)
    assert "PUBLIC_README.md" not in lines
    assert git("status", "--porcelain", cwd=root) == before


@needs_listing
def test_missing_committed_readme_is_not_replaced_from_the_working_tree(readme_history):
    root = readme_history[0]
    git("rm", "-f", "README.md", cwd=root)
    commit = _commit_fixture(root, "No public README in this commit")
    (root / "README.md").write_text("Untracked README must not repair the commit.\n", encoding="utf-8")
    assert (root / "PUBLIC_README.md").is_file()  # both names exist only in the working tree
    result = subprocess.run(
        ["bash", str(root / "scripts" / "publish_sync.sh"), "--list", "--commit", commit],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert f"README.md is not in commit {commit[:12]}" in result.stderr
    assert not result.stdout


@needs_git
@pytest.mark.parametrize("revision,readme,licensed", [(1, "PUBLIC_README.md", False), (2, "README.md", True)])
def test_export_uses_committed_readme_license_and_not_dirty_files(readme_history, tmp_path, revision, readme, licensed):
    root = readme_history[0]
    commit = readme_history[revision]
    out = tmp_path / "export"
    result = subprocess.run(
        ["bash", str(root / "scripts" / "publish_sync.sh"), "--commit", commit, "--out", str(out), "--no-verify"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scan: clean" in result.stdout and "READY:" in result.stdout
    assert "every exported path is identical to commit" in result.stdout
    assert (out / "README.md").read_text(encoding="utf-8") == git("show", f"{commit}:{readme}", cwd=root)
    assert not (out / "PUBLIC_README.md").exists()
    assert (out / "LICENSE").exists() is licensed
    if licensed:
        assert (out / "LICENSE").read_text(encoding="utf-8") == git("show", f"{commit}:LICENSE", cwd=root)
    assert (out / "assets/art.txt").is_file() and (out / "NOTICE").is_file()
    assert not (out / ".env").exists()
    tree = re.search(r"^  tree ([0-9a-f]{40})$", result.stdout, re.M).group(1)
    git("init", "-q", cwd=out)
    git("add", "-A", cwd=out)
    assert git("write-tree", cwd=out).strip() == tree


@needs_git
def test_optional_license_is_not_exempt_from_the_secret_scan(readme_history, tmp_path):
    root = readme_history[0]
    fake_key = "eak_live_" + "ab" * 32
    (root / "LICENSE").write_text(f"License fixture with a planted key: {fake_key}\n", encoding="utf-8")
    git("add", "LICENSE", cwd=root)
    commit = _commit_fixture(root, "License containing a planted test secret")
    result = subprocess.run(
        ["bash", str(root / "scripts" / "publish_sync.sh"), "--commit", commit,
         "--out", str(tmp_path / "export"), "--no-verify"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "KEY       LICENSE:1" in result.stdout and "NOT READY" in result.stdout
    assert fake_key not in result.stdout + result.stderr


@needs_git
@pytest.mark.parametrize("kind", ["relative", "missing", "dots", "symlink", "dangling", "symlink-parent", "spaces",
                                 "source-prefix-sibling", "via-source-parent"])
def test_output_canonicalization_preserves_safe_destinations(readme_history, tmp_path, kind):
    root, _, commit = readme_history
    destination = tmp_path / "destination"
    destination.mkdir()
    expected = destination / "export"
    if kind == "relative":
        supplied = "../destination/export"
    elif kind == "missing":
        expected = destination / "not-created" / "export"
        supplied = str(expected)
    elif kind == "dots":
        supplied = str(destination / "not-created" / ".." / "export")
    elif kind == "spaces":
        expected = destination / "with spaces" / "export"
        supplied = str(expected)
    elif kind == "source-prefix-sibling":
        expected = tmp_path / "source-export"
        supplied = str(expected)
    elif kind == "via-source-parent":
        supplied = str(root / "agent" / ".." / ".." / "destination" / "export")
    else:
        link = tmp_path / "alias"
        if kind == "symlink":
            link.symlink_to(destination, target_is_directory=True)
            supplied = str(link / "export")
        elif kind == "dangling":
            link.symlink_to(destination / "not-created", target_is_directory=True)
            expected = destination / "not-created" / "export"
            supplied = str(link / "export")
        else:
            (destination / "nested").mkdir()
            link.symlink_to(destination / "nested", target_is_directory=True)
            supplied = str(link / ".." / "export")
    # Where GNU realpath is installed, require agreement with the former implementation.
    if sys.platform.startswith("linux") and shutil.which("realpath"):
        former = subprocess.run(["realpath", "-m", "--", supplied], cwd=root, capture_output=True, text=True,
                                check=True).stdout.strip()
        assert former == str(expected)
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit, "--out", supplied, "--no-verify"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"into {expected}:" in result.stdout
    assert (expected / "README.md").read_text(encoding="utf-8") == git("show", f"{commit}:README.md", cwd=root)


@needs_git
@pytest.mark.parametrize("kind", ["source", "inside", "ancestor", "source-alias", "dangling-inside",
                                 "symlink-parent", "missing-before-alias", "home-alias"])
def test_output_guards_resolve_aliases_before_any_replacement(readme_history, tmp_path, kind):
    root, _, commit = readme_history
    home = tmp_path / "home"
    home.mkdir()
    sentinel = home / "keep.txt"
    sentinel.write_text("keep home intact\n", encoding="utf-8")
    if kind == "source":
        supplied = "."
    elif kind == "inside":
        supplied = "agent/../not-created/export"
    elif kind == "ancestor":
        supplied = ".."
    else:
        link = tmp_path / "alias"
        target = home if kind == "home-alias" else root
        if kind == "dangling-inside":
            target = root / "not-created"
        elif kind == "symlink-parent":
            target = root / "agent"
        link.symlink_to(target, target_is_directory=True)
        if kind == "symlink-parent":
            supplied = str(link / ".." / "not-created")
        elif kind == "missing-before-alias":
            supplied = str(tmp_path / "not-created" / ".." / "alias" / "export")
        else:
            supplied = str(link)
    before = git("status", "--porcelain", cwd=root)
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit, "--out", supplied,
         "--no-verify", "--replace"],
        cwd=root, env=dict(os.environ, HOME=str(home)), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "refusing --out" in result.stderr
    assert git("status", "--porcelain", cwd=root) == before
    assert sentinel.read_text(encoding="utf-8") == "keep home intact\n"
    assert not (root / "not-created").exists()


@needs_git
@pytest.mark.parametrize("target", ["/", "/home", "/tmp"])
def test_system_directory_aliases_are_refused_before_export(readme_history, tmp_path, target):
    root, _, commit = readme_history
    alias = tmp_path / "system-alias"
    alias.symlink_to(target, target_is_directory=True)
    # Never pass --replace for a real system directory, even in a regression test.
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit,
         "--out", str(alias), "--no-verify"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "system directory" in result.stderr


@needs_git
def test_symlink_loop_is_refused_before_creating_an_output(readme_history, tmp_path):
    root, _, commit = readme_history
    loop = tmp_path / "loop"
    loop.symlink_to(loop.name, target_is_directory=True)
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit,
         "--out", str(loop / "export"), "--no-verify", "--replace"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot resolve" in result.stderr
    assert loop.is_symlink()


@needs_git
@pytest.mark.parametrize("kind", ["file-ancestor", "newline"])
def test_invalid_output_paths_fail_closed(readme_history, tmp_path, kind):
    root, _, commit = readme_history
    if kind == "file-ancestor":
        ancestor = tmp_path / "not-a-directory"
        ancestor.write_text("keep intact\n", encoding="utf-8")
        supplied = str(ancestor / "export")
    else:
        supplied = str(tmp_path / "export\n")
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit,
         "--out", supplied, "--no-verify", "--replace"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "cannot resolve" in result.stderr
    assert not (tmp_path / "export").exists()
    if kind == "file-ancestor":
        assert ancestor.read_text(encoding="utf-8") == "keep intact\n"


@needs_git
@pytest.mark.skipif(shutil.which("npm") is None, reason="minimal install-check fixture needs npm")
def test_install_check_canonicalizes_its_worktree_before_checking_imports(readme_history, tmp_path):
    root = readme_history[0]
    # Zero-dependency fixture: run the real verifier without a network install or live agent.
    (root / "agent/__init__.py").write_text("", encoding="utf-8")
    (root / "scripts/selfcheck.py").write_text("print('selfcheck: ready')\n", encoding="utf-8")
    for directory in ("agent/js", "local/js"):
        package = root / directory
        package.mkdir(parents=True, exist_ok=True)
        (package / "package.json").write_text('{"name":"fixture","version":"1.0.0"}\n', encoding="utf-8")
        (package / "package-lock.json").write_text(
            '{"name":"fixture","version":"1.0.0","lockfileVersion":3,'
            '"packages":{"":{"name":"fixture","version":"1.0.0"}}}\n', encoding="utf-8")
    git("add", "agent/__init__.py", "scripts/selfcheck.py", "agent/js", "local/js", cwd=root)
    commit = _commit_fixture(root, "Minimal offline install-check fixture")
    temp = tmp_path / "real-temp"
    temp.mkdir()
    alias = tmp_path / "temp-alias"
    alias.symlink_to(temp, target_is_directory=True)
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit,
         "--out", str(tmp_path / "export"), "--no-tests"],
        cwd=root, env=dict(os.environ, HOME=str(home), TMPDIR=str(alias), PYTHON=sys.executable,
                          PIP_NO_INDEX="1", npm_config_offline="true"),
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "install check: passed (selfcheck ready; tests not run)" in result.stdout
    assert "resolves outside the export copy" not in result.stdout


@needs_git
def test_export_statistics_count_regular_files_not_links_or_preserved_git(readme_history, tmp_path):
    root = readme_history[0]
    (root / "assets/payload.txt").write_text("asset payload\n", encoding="utf-8")
    (root / "assets/alias.txt").symlink_to("payload.txt")
    git("add", "assets", cwd=root)
    commit = _commit_fixture(root, "Regular asset with a relative symlink")
    out = tmp_path / "export"
    (out / ".git").mkdir(parents=True)
    marker = out / ".git/preserved"
    marker.write_bytes(b"x" * 4096)
    result = subprocess.run(
        ["bash", str(root / "scripts/publish_sync.sh"), "--commit", commit,
         "--out", str(out), "--no-verify"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (out / "assets/alias.txt").is_symlink()
    assert marker.read_bytes() == b"x" * 4096
    files = [path for path in out.rglob("*") if path.relative_to(out).parts[0] != ".git"
             and path.is_file() and not path.is_symlink()]
    counts = re.search(r"^into .*: (\d+) files, (\d+) bytes ", result.stdout, re.M)
    assert counts, result.stdout
    assert tuple(map(int, counts.groups())) == (len(files), sum(len(path.read_bytes()) for path in files))


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
    assert (out / "README.md").read_text(encoding="utf-8") == git("show", "HEAD:README.md")
    for p in ("agent/agent.py", "agent/js/search.js", "altruagent/runner.py", "data/vgc_cards.json",
              "deploy/Dockerfile", "scripts/run_agent.sh", "scripts/selfcheck.py", "scripts/bundle.sh",
              "local/js/package-lock.json", "docs/deploy.md", "NOTICE", "requirements.lock", ".env.example",
              ".gitignore", "tests/conftest.py"):
        assert (out / p).is_file(), p
    assert (out / "agent/hok/policy.py").is_file()
    for image in re.findall(r"!\[[^\]]*\]\((assets/[^)\s]+)\)", git("show", "HEAD:README.md")):
        assert (out / image).is_file(), image
    if "LICENSE" in git("ls-tree", "--name-only", "HEAD").splitlines():
        assert (out / "LICENSE").read_text(encoding="utf-8") == git("show", "HEAD:LICENSE")
    else:
        assert not (out / "LICENSE").exists()
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
    assert "agent/agent.py" in lines and "scripts/run_agent.sh" in lines and "README.md  (from README.md)" in lines
    assert not any(x.startswith(("local/", "docs/")) and x not in (
        "local/js/package.json", "local/js/package-lock.json", "docs/deploy.md", "docs/restart_load.md")
        for x in lines)
    assert "scripts/publish_sync.sh" not in lines and "CLAUDE.md" not in lines
