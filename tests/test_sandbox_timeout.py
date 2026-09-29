"""A script that runs too long is stopped, and only its own container: the other scripts running on the machine (of
the other agents too) go on."""
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SANDBOX = None


def _load():
    global SANDBOX
    from cat.looking_glass.mad_hatter.plugin import Plugin

    plugin = Plugin(str(PLUGIN_DIR))
    plugin._load_decorated_functions()
    SANDBOX = sys.modules[f"cat.plugins.{PLUGIN_DIR.name}.sandbox"]


class SandboxTimeoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load()

    def test_a_timeout_stops_only_the_container_of_the_script(self):
        # regression: after a timeout every sandbox container of the machine was killed, the ones of other agents too
        commands = []

        def run(cmd, **kwargs):
            commands.append(list(cmd))
            if cmd[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        script = PLUGIN_DIR / "tests" / "scripts" / "slow.py"
        with patch.object(SANDBOX, "_docker_available", return_value=True), \
                patch.object(SANDBOX, "ast_scan", return_value=None), \
                patch.object(SANDBOX.subprocess, "run", side_effect=run):
            result = SANDBOX.run_docker(script, [], timeout_seconds=1)

        self.assertTrue(result.timed_out)
        docker_run, = [c for c in commands if c[:2] == ["docker", "run"]]
        name = next(arg.split("=", 1)[1] for arg in docker_run if arg.startswith("--name="))
        others = [c for c in commands if c[:2] != ["docker", "run"]]
        self.assertEqual(others, [["docker", "kill", name]])
        self.assertFalse(any("ps" in c for c in others), "the containers of the other scripts are never listed")

    def test_every_run_has_its_own_container(self):
        names = set()

        def run(cmd, **kwargs):
            names.add(next(arg for arg in cmd if arg.startswith("--name=")))
            return subprocess.CompletedProcess(cmd, 0, stdout=b"ok", stderr=b"")

        with patch.object(SANDBOX, "_docker_available", return_value=True), \
                patch.object(SANDBOX, "ast_scan", return_value=None), \
                patch.object(SANDBOX.subprocess, "run", side_effect=run):
            for _ in range(3):
                SANDBOX.run_docker(PLUGIN_DIR / "tests" / "scripts" / "slow.py", [], timeout_seconds=1)
        self.assertEqual(len(names), 3)

    def test_a_failed_kill_is_logged(self):
        def run(cmd, **kwargs):
            raise OSError("docker down")

        with patch.object(SANDBOX.subprocess, "run", side_effect=run):
            SANDBOX._kill_container("agent-skills-x")  # logged, not raised


if __name__ == "__main__":
    unittest.main()
