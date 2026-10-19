"""Run unittest and function checks in separate processes per test module.

Usage: python scripts/run_tests.py [tests/test_name.py ...]
The JSON report counts executed tests, failures and skips, not definitions.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import importlib.util
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT = Path(__file__).resolve().parents[1]


def run_function(function) -> None:
    """Support the existing function tests' explicit temporary-path fixture."""
    required = [parameter for parameter in inspect.signature(function).parameters.values()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}]
    if not required:
        function()
        return
    if len(required) != 1 or required[0].name != "tmp_path":
        raise TypeError(f"Unsupported test fixture in {function.__name__}; only tmp_path is supported")
    with tempfile.TemporaryDirectory(prefix="video-scrap-test-") as directory:
        function(tmp_path=Path(directory))


def worker(path: Path, report: Path) -> int:
    sys.path[:0] = [str(PROJECT), str(PROJECT / "tests")]
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    suite = unittest.TestSuite()
    for name, value in sorted(vars(module).items()):
        if getattr(value, "__module__", None) != module.__name__:
            continue
        if inspect.isclass(value) and issubclass(value, unittest.TestCase):
            suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(value))
        elif name.startswith("test_") and inspect.isfunction(value):
            suite.addTest(unittest.FunctionTestCase(lambda value=value: run_function(value), description=name))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    data = {"file": str(path.relative_to(PROJECT)), "executed": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}
    report.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return 0 if result.wasSuccessful() and result.testsRun > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*")
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker.resolve(), args.report)
    paths = [Path(name).resolve() for name in args.files] if args.files else sorted((PROJECT / "tests").glob("test_*.py"))
    if not paths or any(path.parent != PROJECT / "tests" or not path.is_file() for path in paths):
        parser.error("Select existing test files from this project's tests directory")
    output = (args.output or PROJECT / "build" / "test-results" / datetime.now().strftime("%Y%m%d-%H%M%S")).resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    bundled = PROJECT / "build" / ("windows-app-assets" if sys.platform == "win32" else "app-assets") / "bin"
    env["PATH"] = str(bundled) + os.pathsep + env.get("PATH", "")

    def run(path):
        report = output / (path.stem + ".json")
        report.unlink(missing_ok=True)
        started = time.monotonic()
        log = output / (path.stem + ".log")
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--worker", str(path), "--report", str(report)],
                                       cwd=PROJECT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                                       start_new_session=sys.platform != "win32")
            try:
                code = process.wait(timeout=max(1, args.timeout))
                status = "passed" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                # Kill only this runner and its own descendants.
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code, status = process.returncode, "timeout"
        data = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"file": str(path.relative_to(PROJECT)), "executed": 0}
        if data["executed"] == 0:
            status = "failed"
        data.update(status=status, exit_code=code, seconds=round(time.monotonic() - started, 3), log=str(log))
        print(f"{status.upper()}: {data['file']} ({data['executed']} executed)", flush=True)
        return data

    with ThreadPoolExecutor(max_workers=max(1, min(4, args.jobs))) as executor:
        results = list(executor.map(run, paths))
    summary = {"python": sys.version, "platform": sys.platform, "files": results,
               "executed": sum(row["executed"] for row in results),
               "skipped": sum(row.get("skipped", 0) for row in results),
               "passed": all(row["status"] == "passed" for row in results)}
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"Executed {summary['executed']} tests, skipped {summary['skipped']}. Report: {output / 'summary.json'}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
