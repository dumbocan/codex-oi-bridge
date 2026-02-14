import tempfile
import unittest
from pathlib import Path

from bridge.runner import _build_runner_env


class RunnerTests(unittest.TestCase):
    def test_build_runner_env_uses_writable_run_local_paths(self) -> None:
        with tempfile.TemporaryDirectory(dir=".") as tmp:
            run_dir = Path(tmp) / "runs" / "r1"
            run_dir.mkdir(parents=True)
            env = _build_runner_env(run_dir)
            self.assertTrue(env["HOME"].endswith(".oi_home"))
            self.assertTrue((run_dir / ".oi_home" / ".cache" / "open-interpreter").exists())
            self.assertTrue((run_dir / ".oi_home" / ".config" / "matplotlib").exists())
            self.assertEqual(env["XDG_CACHE_HOME"], str(run_dir / ".oi_home" / ".cache"))
            self.assertEqual(env["XDG_CONFIG_HOME"], str(run_dir / ".oi_home" / ".config"))


if __name__ == "__main__":
    unittest.main()
