import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CiDependencyMatrixTests(unittest.TestCase):
    def test_ci_exercises_declared_os_matrix_and_dependency_health(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("runs-on: ${{ matrix.os }}", workflow)
        self.assertRegex(workflow, r"macos-[0-9]+|macos-latest")
        self.assertIn("pip check", workflow)
        self.assertIn("real_browser_smoke", workflow)

        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertRegex(requirements, r"(?m)^urllib3[<>=!~].*2\.5")

        matrix = (ROOT / "docs" / "dependency-matrix.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("LibreSSL", matrix)
        self.assertIn("Appium", matrix)
        self.assertIn("macOS", matrix)


if __name__ == "__main__":
    unittest.main()
