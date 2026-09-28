import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("docs_check", ROOT / "tools" / "docs_check.py")
docs_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs_check)


def test_docs_only_change_is_fine():
    assert docs_check.evaluate(["README.md", "docs/adr/ADR-01-redpanda-protobuf.md"]) == ([], [])


def test_code_change_without_progress_and_chat_log_fails():
    errors, _ = docs_check.evaluate(["services/api/src/fs_api/main.py"])
    assert len(errors) == 2 and any("PROGRESS.md" in e for e in errors) and any("CHAT_LOG.md" in e for e in errors)


def test_code_change_with_living_docs_passes_with_hints():
    errors, warnings = docs_check.evaluate(
        ["db/migrations/0008_x.sql", "docs/PROGRESS.md", "docs/CHAT_LOG.md"])
    assert errors == []
    assert any("decisions.md" in w for w in warnings) and any("BUGS.md" in w for w in warnings)


def test_windows_paths_are_normalised():
    errors, _ = docs_check.evaluate(["services\\api\\x.py", "docs\\PROGRESS.md", "docs\\CHAT_LOG.md"])
    assert errors == []


def test_living_docs_exist_with_markers():
    assert docs_check.check_files_exist() == []
