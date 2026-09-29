"""A skill script runs in the sandbox (Docker or a subprocess, up to its timeout) without blocking the event loop of
the instance (every agent and chat it serves).

Run from the core root: ``python -m pytest cat/plugins/cat-agent-skills-plugin/tests``.

The Cat imports every ``.py`` file of the plugin, tests included: at import time this module needs only the stdlib,
the Cat and the plugin are loaded in ``setUpClass``.
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

PLUGIN_DIR = Path(__file__).resolve().parents[1]
LOADED = set()

MODULE = SANDBOX = None
TOOL = None


def _load():
    """Loads the plugin once, from the test classes, as the Cat loads it (manifest included)."""
    global MODULE, SANDBOX, TOOL
    if str(PLUGIN_DIR) in LOADED:
        return
    from cat.looking_glass.mad_hatter.plugin import Plugin

    plugin = Plugin(str(PLUGIN_DIR))
    plugin._load_decorated_functions()
    TOOL = next(t for t in plugin.tools if t.name == "run_skill_script_tool").func
    MODULE = sys.modules[TOOL.__module__]
    SANDBOX = sys.modules[f"{TOOL.__module__.rsplit('.', 1)[0]}.sandbox"]
    LOADED.add(str(PLUGIN_DIR))


def _cat(settings=None):
    cat = MagicMock()
    cat.agent_key = "agent"
    cat.mad_hatter.get_plugin.return_value.load_settings = AsyncMock(return_value=settings or {})
    return cat


def _registry(*names):
    records = {}
    for name in names:
        record = MagicMock()
        record.name = name
        records[name] = record
    registry = MagicMock()
    registry.get.side_effect = records.get
    registry.all_skills.return_value = list(records.values())
    return registry


SCRIPT = Path("/skills/csv/scripts/inspect.py")


class TestRunSkillScriptTool(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _load()

    def call(self, tool_input, run=None, registry="default", settings=None, resolved=(SCRIPT, None)):
        registry = _registry("csv") if registry == "default" else registry
        run = run or MagicMock(return_value=SANDBOX.SandboxResult(returncode=0, stdout="rows: 3", stderr=""))
        with patch.object(MODULE, "_get_registry_from_stray_cat", AsyncMock(return_value=registry)), \
                patch.object(MODULE, "_resolve_skill_script_path", MagicMock(return_value=resolved)), \
                patch.object(MODULE, "run_skill_script", run):
            return asyncio.run(self.with_heartbeat(TOOL(tool_input, _cat(settings))))

    @staticmethod
    async def with_heartbeat(coro):
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            return await coro, ticks
        finally:
            beat.cancel()

    def test_a_running_script_never_blocks_the_event_loop(self):
        """Regression: the sandbox (``subprocess.run`` of Docker or of Python, up to its timeout) ran inside the
        asynchronous tool: the event loop, and so every request served by the instance, was blocked until the script
        ended."""

        def slow_script(*args, **kwargs):
            time.sleep(0.3)
            return SANDBOX.SandboxResult(returncode=0, stdout="rows: 3", stderr="")

        begin = time.monotonic()
        answer, ticks = self.call("csv:inspect.py data.csv", run=slow_script)
        self.assertGreaterEqual(time.monotonic() - begin, 0.3)
        self.assertEqual(answer, "rows: 3")
        self.assertGreater(ticks, 15, "other coroutines run while the script is running")

    def test_the_sandbox_and_its_limits_are_kept(self):
        run = MagicMock(return_value=SANDBOX.SandboxResult(returncode=0, stdout="ok", stderr=""))
        self.call("csv:inspect.py data.csv --sep ';'", run=run,
                  settings={"sandbox_mode": "docker", "sandbox_timeout_seconds": 42, "sandbox_max_memory": "256m"})
        run.assert_called_once_with(
            SCRIPT, ["data.csv", "--sep", ";"], mode=SANDBOX.SandboxMode.DOCKER, timeout_seconds=42, max_memory="256m",
        )
        run.reset_mock()
        self.call("csv:inspect.py", run=run, settings={"sandbox_mode": "subprocess"})
        run.assert_called_once_with(
            SCRIPT, [], mode=SANDBOX.SandboxMode.SUBPROCESS, timeout_seconds=60, max_memory="128m",
        )

    def test_a_script_blocked_by_the_security_analysis(self):
        from cat.services.python_security import SecurityError

        answer, _ = self.call("csv:inspect.py", run=MagicMock(side_effect=SecurityError("import forbidden: socket")))
        self.assertIn("Script blocked", answer)
        self.assertIn("import forbidden: socket", answer)

    def test_an_unexpected_error_of_the_sandbox(self):
        answer, _ = self.call("csv:inspect.py", run=MagicMock(side_effect=RuntimeError("docker is gone")))
        self.assertIn("Unexpected error: docker is gone", answer)

    def test_a_script_that_fails_or_times_out(self):
        failed = SANDBOX.SandboxResult(returncode=2, stdout="", stderr="boom")
        answer, _ = self.call("csv:inspect.py", run=MagicMock(return_value=failed))
        self.assertIn("exit code 2", answer)
        timed_out = SANDBOX.SandboxResult(returncode=-1, stdout="", stderr="", timed_out=True)
        answer, _ = self.call("csv:inspect.py", run=MagicMock(return_value=timed_out))
        self.assertIn("Timeout", answer)

    def test_inputs_that_run_nothing(self):
        run = MagicMock()
        cases = [
            ("csv:inspect.py", None, "not active"),
            ("'unclosed", "default", "Input not valid"),
            ("   ", "default", "Empty input"),
            ("inspect.py", "default", "Wrong format"),
            ("pdf:inspect.py", "default", "Skill 'pdf' not found. Available: csv."),
            ("pdf:inspect.py", _registry(), "Available: none."),
        ]
        for tool_input, registry, expected in cases:
            with self.subTest(tool_input=tool_input):
                answer, _ = self.call(tool_input, run=run, registry=registry)
                self.assertIn(expected, answer)
        answer, _ = self.call("csv:../x.py", run=run, resolved=(None, "[sandbox] Invalid script path."))
        self.assertEqual(answer, "[sandbox] Invalid script path.")
        run.assert_not_called()


class TestPluginSafety(unittest.TestCase):
    def test_every_file_passes_the_scanner_of_the_core(self):
        from cat.looking_glass.mad_hatter.plugin_extractor import PluginExtractor

        self.assertTrue(PluginExtractor._is_safe_plugin(str(PLUGIN_DIR)))

    def test_tests_import_only_the_stdlib_at_import_time(self):
        import ast

        for test_file in sorted(Path(__file__).parent.glob("*.py")):
            tree = ast.parse(test_file.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    for name in names:
                        self.assertIn(name.split(".")[0], sys.stdlib_module_names, (test_file.name, name))


if __name__ == "__main__":
    unittest.main()
