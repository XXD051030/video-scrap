"""The shared runner must execute and clean up each supported test shape."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_tests import run_function


class RunnerTests(unittest.TestCase):
    def test_no_argument_function_runs(self):
        calls = []
        run_function(lambda: calls.append(True))
        self.assertEqual(calls, [True])

    def test_tmp_path_fixture_is_fresh_and_removed_after_each_call(self):
        paths = []
        def check(tmp_path):
            self.assertTrue(tmp_path.is_dir())
            self.assertEqual(list(tmp_path.iterdir()), [])
            (tmp_path / "proof").write_text("actually ran")
            paths.append(tmp_path)
        run_function(check); run_function(check)
        self.assertNotEqual(paths[0], paths[1])
        self.assertTrue(all(not path.exists() for path in paths))

    def test_unsupported_fixture_is_an_error(self):
        def unsupported(unknown_fixture):
            raise AssertionError("Runner must not silently ignore fixture arguments")
        with self.assertRaisesRegex(TypeError, "Unsupported test fixture"):
            run_function(unsupported)


if __name__ == "__main__":
    unittest.main(verbosity=2)
