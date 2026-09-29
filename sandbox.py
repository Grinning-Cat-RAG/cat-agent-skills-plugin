"""
sandbox.py – Agent Skills Script Sandbox
===========================================
Three layers of defense in depth to safely run third-party Python scripts from SKILL.md-based skills.

Layer 1 – AST static scan: Blocks prohibited static patterns before starting any process.
Layer 2 – Isolated subprocess: Lightweight fallback, useful for development/testing. Enforces timeouts, memory limits
          (ulimit), reduced environment.
Layer 3 – Docker sandbox: Hard security boundary for production. Network disabled, read-only filesystem, no privileges,
          disposable container.

How to choose the layer:
- Local development        → SandboxMode.SUBPROCESS (no Docker required)
- Staging / CI             → SandboxMode.SUBPROCESS + AST required
- Production               → SandboxMode.DOCKER (recommended)
- High-security production → SandboxMode.DOCKER + custom seccomp profile

Plugin Integration:
See the "How to Use" section at the bottom of the file.
"""
import shutil
import subprocess
import uuid
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path

from cat import log
from cat.services.python_security import ast_scan


def _apply_resource_limits(max_memory_mb: int, max_file_mb: int) -> None:
    """
    Plugin Integration: See the "How to Use" section at the bottom of the file.
    """
    try:
        import resource  # not available on Windows
    except ImportError:
        return

    mem_bytes = max_memory_mb * 1024 * 1024
    file_bytes = max_file_mb * 1024 * 1024

    limits = [
        (resource.RLIMIT_AS, (mem_bytes,  mem_bytes)),   # virtual memory
        (resource.RLIMIT_DATA, (mem_bytes,  mem_bytes)),   # heap
        (resource.RLIMIT_FSIZE, (file_bytes, file_bytes)),  # max file size
        (resource.RLIMIT_NPROC, (0, 0)),  # no fork
        (resource.RLIMIT_CPU, (30, 35)),  # max 30s CPU
    ]
    for res, (soft, hard) in limits:
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, resource.error):
            pass  # ignore if the limit is not supported on the current kernel


def run_subprocess(
    script_path: Path,
    args: list[str] | None = None,
    *,
    timeout_seconds: int = 30,
    max_output_bytes: int = 512 * 1024,
    max_memory_mb: int = 256,
    max_file_mb: int = 10,
    skip_ast_scan: bool = False,
) -> "SandboxResult":
    """
    Runs *script_path* in a subprocess with ulimit and a reduced environment.

    Useful for local development or environments without Docker.
    This is NOT a complete security boundary: a sufficiently creative script
    can still cause damage. Use run_docker() in production.
    """
    if not skip_ast_scan:
        ast_scan(script_path)  # Layer 1 before any execution

    cmd = [sys.executable, str(script_path)] + (args or [])

    safe_env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        # No HOME, USER, cloud credentials, API keys, etc.
    }

    preexec = None
    if sys.platform != "win32":
        def preexec():
            _apply_resource_limits(max_memory_mb, max_file_mb)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_seconds,
            env=safe_env,
            preexec_fn=preexec,
            cwd=tempfile.gettempdir(),  # neutral CWD, not the plugin dir
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr=f"[sandbox] Timeout: script ended after {timeout_seconds}s",
            timed_out=True,
        )
    except Exception as exc:
        return SandboxResult(returncode=-1, stdout="", stderr=f"[sandbox] Error: {exc}")

    stdout = proc.stdout[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = proc.stderr[:max_output_bytes].decode("utf-8", errors="replace")

    return SandboxResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)


# ===========================================================================
# LAYER 3 – DOCKER SANDBOX (production)
# ===========================================================================

# Label applied to every container we launch so we can identify and kill orphans.
_SANDBOX_LABEL = "agent-skills-sandbox=1"
# In production, you could build a custom image with only the packages needed for the skills (pandas, requests, etc.)
# and without system tools.
_DEFAULT_DOCKER_IMAGE = "python:3.12-slim"

# Minimal seccomp profile: blocks dangerous syscalls even if the process manages to do something ulimit doesn't cover.
# You can replace with a .json file for a custom profile.
_SECCOMP_PROFILE = "default"  # uses the default Docker profile (already restrictive)


def _docker_available() -> bool:
    """Check if the `docker` binary is in PATH and the daemon is reachable."""
    if not shutil.which("docker"):
        return False
    try:
        subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return True
    except Exception:
        return False


def run_docker(
    script_path: Path,
    args: list[str] | None = None,
    *,
    # --- Image and resources ---
    image: str = _DEFAULT_DOCKER_IMAGE,
    timeout_seconds: int = 60,
    max_memory: str = "128m",       # Docker format: "128m", "1g", etc.
    max_cpus: str = "0.5",          # CPU fraction (0.5 = half core)
    # --- Output ---
    max_output_bytes: int = 512 * 1024,
    # --- Additional security ---
    skip_ast_scan: bool = False,
    extra_pip_packages: list[str] | None = None,
    readonly_mounts: dict[Path, str] | None = None,
    # --- Network ---
    allow_network: bool = False,
) -> "SandboxResult":
    """
    Run *script_path* in a disposable Docker container, completely
    isolated from the host system.

    Parameters
    ----------
    script_path       : absolute path to the Python script to execute.
    args              : command-line arguments passed to the script.
    image             : Docker image to use (default: python:3.12-slim).
    timeout_seconds   : kill the container after N seconds (wall clock).
    max_memory        : container RAM memory limit (Docker format).
    max_cpus          : CPU fraction assigned to the container.
    max_output_bytes  : truncation of stdout/stderr to avoid OOM in the Cat.
    skip_ast_scan     : if True, skips Layer 1 (not recommended).
    extra_pip_packages: install additional packages before running
                        the script (e.g. ["pandas", "requests"]).
    readonly_mounts   : dict {host_path: "/container/path"} of additional
                        directories to mount read-only in the container.
                        Useful to give the script access to data files
                        that the user has already uploaded to the Cat.
    allow_network     : if True, removes --network=none.
                        Use with extreme caution and only for trusted skills.

    Returns
    -------
    SandboxResult with returncode, stdout, stderr, timed_out.

    Example
    -------
    result = run_docker(
        script_path=skill_dir / "scripts" / "analyze.py",
        args=["--input", "/data/report.csv"],
        readonly_mounts={Path("/app/uploads/report.csv"): "/data/report.csv"},
        extra_pip_packages=["pandas"],
    )
    if result.ok:
        return result.stdout
    else:
        return f"Script error: {result.stderr}"
    """
    # --- Pre-flight checks --------------------------------------------------

    if not _docker_available():
        log.warning("sandbox: Docker not available, falling back to subprocess")
        return run_subprocess(
            script_path, args,
            timeout_seconds=timeout_seconds,
            skip_ast_scan=skip_ast_scan,
        )

    if not skip_ast_scan:
        ast_scan(script_path)  # Layer 1

    skill_dir = script_path.parent.parent  # scripts/ → skill root
    container_skill_dir = "/skill"
    container_script = f"{container_skill_dir}/scripts/{script_path.name}"

    # --- Build the Docker command -------------------------------------------

    # a name of its own: on a timeout only this container is stopped, never the ones of the other scripts (of other
    # agents too) running on the machine
    container_name = f"agent-skills-{uuid.uuid4().hex}"

    cmd: list[str] = [
        "docker", "run",

        # Disposable container: automatically removed upon exit
        "--rm",
        f"--name={container_name}",

        # No interactive TTY / stdin
        "--interactive=false",

        # Label for orphan tracking
        f"--label={_SANDBOX_LABEL}",

        # --- Network isolation ----------------------------------------------
        *([] if allow_network else ["--network=none"]),

        # --- Resource limits ------------------------------------------------
        f"--memory={max_memory}",
        f"--memory-swap={max_memory}",   # swap = RAM limit → no swap
        f"--cpus={max_cpus}",
        "--pids-limit=64",               # max 64 processes within the container

        # --- Filesystem security --------------------------------------------
        "--read-only",                   # read-only filesystem root
        "--tmpfs=/tmp:size=32m,noexec",  # /tmp writable but no exec bit
        "--tmpfs=/root:size=8m",         # minimal home dir

        # --- Capability security --------------------------------------------
        "--cap-drop=ALL",                # remove all Linux capabilities
        "--security-opt=no-new-privileges",
        f"--security-opt=seccomp={_SECCOMP_PROFILE}",

        # --- Non-root user --------------------------------------------------
        # python:3.12-slim has the "nobody" user (uid 65534)
        "--user=nobody",

        # --- Mount the skill (read-only) ------------------------------------
        "-v", f"{skill_dir}:{container_skill_dir}:ro",

        # --- Additional mounts (e.g. user data files) -----------------------
        *_build_volume_args(readonly_mounts),

        # --- Image ----------------------------------------------------------
        image,

        # --- Command to run in the container --------------------------------
        # If extra packages are needed, install them in a temporary layer
        # using sh -c "pip install X && python script.py args"
        *_build_container_cmd(
            container_script,
            args or [],
            extra_pip_packages or [],
        ),
    ]

    log.debug(f"sandbox: docker run → {' '.join(cmd)}")

    # --- Execute the container ------------------------------------------------

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_seconds,
            # No special env: Docker does not inherit the host env by default
        )
    except subprocess.TimeoutExpired:
        # Docker doesn't automatically kill the container when the subprocess times out: we kill it, by its name.
        _kill_container(container_name)
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr=f"[sandbox] Timeout: container terminated after {timeout_seconds}s",
            timed_out=True,
        )
    except FileNotFoundError:
        return SandboxResult(
            returncode=-1,
            stdout="",
            stderr="[sandbox] Command 'docker' not found within the PATH",
        )
    except Exception as exc:
        return SandboxResult(returncode=-1, stdout="", stderr=f"[sandbox] Error Docker: {exc}")

    stdout = proc.stdout[:max_output_bytes].decode("utf-8", errors="replace")
    stderr = proc.stderr[:max_output_bytes].decode("utf-8", errors="replace")

    # Docker's stderr includes image pull logs: we filter
    # lines starting with "Unable to find image" or "Pulling from"
    stderr = _filter_docker_noise(stderr)

    return SandboxResult(returncode=proc.returncode, stdout=stdout, stderr=stderr)


# --- Private Helpers --------------------------------------------------------

def _build_volume_args(mounts: dict[Path, str] | None) -> list[str]:
    """Convert the dict of mount in couples -v host:container:ro."""
    if not mounts:
        return []
    result = []
    for host_path, container_path in mounts.items():
        result += ["-v", f"{host_path}:{container_path}:ro"]
    return result


def _build_container_cmd(
    container_script: str,
    args: list[str],
    pip_packages: list[str],
) -> list[str]:
    """
    Builds the final command to pass to Docker.

    If no extra packages are needed → ["python", "script.py", ...args]
    If packages are needed → ["sh", "-c", "pip install X && python ..."]
    """
    script_cmd = " ".join(["python", container_script] + [_shell_quote(a) for a in args])

    if not pip_packages:
        # Simple form: no intermediate shell
        return ["python", container_script] + args

    # Install the packages in /tmp (read-only root, but /tmp is tmpfs)
    packages = " ".join(pip_packages)
    full_cmd = (
        f"pip install --quiet --target=/tmp/pylib {packages} && "
        f"PYTHONPATH=/tmp/pylib {script_cmd}"
    )
    return ["sh", "-c", full_cmd]


def _shell_quote(s: str) -> str:
    """Minimal escape for arguments in sh -c."""
    return "'" + s.replace("'", "'\\''") + "'"


def _kill_container(name: str) -> None:
    """
    Terminate the container of a script that timed out, and only that one: the containers of the other scripts running
    on the machine (of other agents too) go on.
    """
    try:
        subprocess.run(["docker", "kill", name], capture_output=True, timeout=10)
        log.debug(f"sandbox: killed the container {name}")
    except Exception as exc:
        log.warning(f"sandbox: could not kill the container {name} – {exc}")


def _filter_docker_noise(stderr: str) -> str:
    """Remove Docker log lines that are not script errors."""
    noise_prefixes = (
        "Unable to find image",
        "Pulling from",
        "Pull complete",
        "Digest:",
        "Status:",
        "latest: Pulling",
    )
    lines = [ln for ln in stderr.splitlines() if not ln.startswith(noise_prefixes)]
    return "\n".join(lines).strip()


# ===========================================================================
# UNIFIED RESULT
# ===========================================================================

@dataclass
class SandboxResult:
    """
    Unified output from any execution layer.

    Attributes
    ----------
    returncode : process exit code (0 = success).
    stdout     : standard output of the script (already truncated).
    stderr     : error output (already filtered from Docker noise).
    timed_out  : True if the process was terminated due to a timeout.
    """
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """True if the script exited with code 0."""
        return self.returncode == 0

    def to_llm_string(self) -> str:
        """
        Format the result to be included in the LLM response.
        Includes stdout if ok, stderr if error.
        """
        if self.timed_out:
            return "[Skill script] ⏱ Timeout: the script took too long."
        if self.ok:
            return self.stdout or "[Skill script] Completed with no output."
        return (
            f"[Skill script] ❌ Error (exit code {self.returncode}):\n"
            f"{self.stderr or '(no error message)'}"
        )


# ===========================================================================
# PUBLIC FACADE
# ===========================================================================

class SandboxMode(Enum):
    SUBPROCESS = auto()   # Layer 2: development / environments without Docker
    DOCKER     = auto()   # Layer 3: production


def run_skill_script(
    script_path: Path,
    args: list[str] | None = None,
    *,
    mode: SandboxMode = SandboxMode.DOCKER,
    # Options passed to the chosen layer
    **kwargs,
) -> SandboxResult:
    """
    Main entry point for running a skill script.

    Automatically selects the layer based on *mode*.
    If mode=DOCKER but Docker is not available, falls back to SUBPROCESS
    with a warning in the log.

    Parameters
    ----------
    script_path : absolute path to the Python script to execute.
    args        : arguments passed to the script.
    mode        : SandboxMode.DOCKER (default) or SandboxMode.SUBPROCESS.
    **kwargs    : additional options passed to the layer (timeout, max_memory, etc.).

    Basic example
    -------------
    from pathlib import Path
    from cat.plugins.agent_skills_plugin.sandbox import run_skill_script, SandboxMode

    result = run_skill_script(
        Path("/app/cat/plugins/csv_analyzer/skills/csv-analyzer/scripts/inspect_csv.py"),
        args=["report.csv"],
    )
    return result.to_llm_string()

    Example with user file mount
    ----------------------------
    result = run_skill_script(
        script_path,
        mode=SandboxMode.DOCKER,
        readonly_mounts={Path("/app/uploads/data.csv"): "/data/input.csv"},
        extra_pip_packages=["pandas"],
    )
    """
    if mode == SandboxMode.DOCKER:
        return run_docker(script_path, args, **kwargs)
    else:
        return run_subprocess(script_path, args, **kwargs)


# ===========================================================================
# DIRECT SHELL USAGE (debug)
# ===========================================================================
#
# python -m cat.plugins.agent_skills_plugin.sandbox \
#        /path/to/script.py --arg1 val1
#
if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) < 2:
        print("Usage: python sandbox.py <script.py> [args...]")
        _sys.exit(1)

    _script = Path(_sys.argv[1]).resolve()
    _args   = _sys.argv[2:]

    print(f"[sandbox CLI] Running:    {_script}")
    print(f"[sandbox CLI] Arguments:  {_args}")
    print(f"[sandbox CLI] Docker:     {'available' if _docker_available() else 'not available'}")
    print()

    _result = run_skill_script(_script, _args)
    print("=== STDOUT ===")
    print(_result.stdout or "(empty)")
    print("=== STDERR ===")
    print(_result.stderr or "(empty)")
    print(f"=== Exit code: {_result.returncode} ===")
