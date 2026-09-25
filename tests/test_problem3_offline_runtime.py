"""The ZIP deployment path must not require a Git executable."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from audit_problem3_assets import git_head as audit_git_head
from run_problem3_g1 import git_head as g1_git_head


class OfflineRuntimeTests(unittest.TestCase):
    def test_missing_git_executable_is_optional_metadata(self):
        for function in (audit_git_head, g1_git_head):
            with patch("subprocess.run", side_effect=FileNotFoundError("git")):
                self.assertIsNone(function(ROOT))


if __name__ == "__main__":
    unittest.main()
