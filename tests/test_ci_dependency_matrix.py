import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CiDependencyMatrixTests(unittest.TestCase):
    def test_four_minor_constraints_and_windows_gate_are_declared(self):
        for minor in ("3.9", "3.10", "3.11", "3.12"):
            path = ROOT / "constraints" / ("python%s.txt" % minor)
            self.assertTrue(path.is_file(), path)
            self.assertIn("playwright==", path.read_text(encoding="utf-8"))

        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("windows-latest", workflow)
        self.assertIn("constraints/python3.11.txt", workflow)
        self.assertIn("-c ${{ matrix.constraint }}", workflow)

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
