import shutil
import subprocess
import unittest
from pathlib import Path


class ProjectsSortTest(unittest.TestCase):
    def test_phase_sort_restores_server_order(self):
        if not shutil.which("node"):
            self.skipTest("Node is needed for the browser logic regression")
        root = Path(__file__).parents[1]
        r = subprocess.run(["node", "tests/console_projects_sort_test.js"], cwd=root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
