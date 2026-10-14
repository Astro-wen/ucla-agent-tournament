import hashlib
import json
import zipfile

import pytest

from scripts import archive_werewolf_research as module


def report(tmp_path, files):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"source_manifests": {"snapshot": {"files": files}}}))
    return path


def test_archive_preserves_exact_source_bytes_and_refuses_overwrite(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "agent.py").write_text("value = 7\n")
    monkeypatch.setattr(module, "ROOT", root)
    digest = hashlib.sha256(b"value = 7\n").hexdigest()
    source = report(tmp_path, {"agent.py": digest})
    output = tmp_path / "source.zip"
    result = module.archive(source, output)
    assert result["files"] == 1
    with zipfile.ZipFile(output) as bundle:
        assert bundle.read("agent.py") == b"value = 7\n"
    before = output.read_bytes()
    with pytest.raises(ValueError):
        module.archive(source, output)
    assert output.read_bytes() == before


@pytest.mark.parametrize("name", ["../outside.py", "/outside.py", ".env", "agent/.env.local"])
def test_archive_rejects_unsafe_or_credential_paths_before_reading(tmp_path, monkeypatch, name):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="unsafe"):
        module.archive(report(tmp_path, {name: "unused"}), tmp_path / "source.zip")


def test_archive_rejects_symlink_escape_even_with_a_matching_digest(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside repository")
    (root / "link.py").symlink_to(outside)
    monkeypatch.setattr(module, "ROOT", root)
    source = report(tmp_path, {"link.py": hashlib.sha256(outside.read_bytes()).hexdigest()})
    with pytest.raises(ValueError, match="unsafe"):
        module.archive(source, tmp_path / "source.zip")


def test_archive_rejects_changed_source_without_creating_a_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "ROOT", tmp_path)
    (tmp_path / "agent.py").write_text("changed")
    output = tmp_path / "source.zip"
    with pytest.raises(ValueError, match="changed"):
        module.archive(report(tmp_path, {"agent.py": "bad"}), output)
    assert not output.exists()
