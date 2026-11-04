import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from register_translator import sync_agent_files


def test_sync_agent_files_copies_and_updates_existing_agent(tmp_path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "users" / "user-id" / "agents" / "kechuang"
    source_dir.mkdir()
    source_file = source_dir / "SOUL.md"
    source_file.write_text("first", encoding="utf-8")

    sync_agent_files(source_dir, target_dir)
    assert (target_dir / "SOUL.md").read_text(encoding="utf-8") == "first"

    source_file.write_text("updated", encoding="utf-8")
    sync_agent_files(source_dir, target_dir)
    assert (target_dir / "SOUL.md").read_text(encoding="utf-8") == "updated"
