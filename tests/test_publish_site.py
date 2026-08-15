import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import publish_site


class SafePublishTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.root,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.root,
            check=True,
        )
        (self.root / "index.html").write_text("first", encoding="utf-8")
        (self.root / "source.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        )
        self.project_patch = patch.object(publish_site, "PROJECT_DIR", self.root)
        self.project_patch.start()

    def tearDown(self) -> None:
        self.project_patch.stop()
        self.temporary_directory.cleanup()

    def test_accepts_only_unstaged_index_change(self) -> None:
        self.assertFalse(publish_site._validate_pipeline_changes())
        (self.root / "index.html").write_text("second", encoding="utf-8")
        self.assertTrue(publish_site._validate_pipeline_changes())

    def test_rejects_source_or_untracked_changes(self) -> None:
        (self.root / "source.py").write_text("value = 2\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "index.html 以外"):
            publish_site._validate_pipeline_changes()
        with self.assertRaisesRegex(RuntimeError, "工作目錄不是乾淨狀態"):
            publish_site._require_clean_worktree()


if __name__ == "__main__":
    unittest.main()
