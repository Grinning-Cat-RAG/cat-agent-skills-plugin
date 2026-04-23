"""
Agent Skills Plugin for Grinning Cat
=====================================
Integrates the open Agent Skills standard (agentskills.io) into Grinning Cat.

This plugin acts as a universal interpreter: any other Grinning Cat plugin can
ship one or more SKILL.md-based skills inside a `skills/` sub-directory, and
this plugin will automatically discover them, build a catalog, and inject it
into the system prompt — following the full progressive-disclosure lifecycle.

Usage for plugin authors
------------------------
Inside your own plugin folder, create:

    my_plugin/
    ├── my_plugin.py        ← hooks, tools, forms as usual
    └── skills/
        └── my-skill/
            ├── SKILL.md    ← required (frontmatter + instructions)
            ├── scripts/    ← optional executable helpers
            └── references/ ← optional supporting docs

The Agent Skills plugin will pick them up automatically.

Skill activation
----------------
Skills are activated in two ways:
  1. **Model-driven**: the LLM reads the catalog and calls `activate_skill`
     when it decides a skill is relevant.
  2. **User-explicit**: the user types `/skill <name>` in the chat.
"""
import asyncio
from pathlib import Path
from pydantic import BaseModel, Field

from cat import hook, log, tool, plugin, StrayCat, UserMessage, CheshireCat, BillTheLizard
from cat.services.python_security import SecurityError
from cat.utils import get_plugins_path

from .skill_loader import SkillRegistry, SkillRecord
from .sandbox import run_skill_script, SandboxMode


# ---------------------------------------------------------------------------
# Plugin settings
# ---------------------------------------------------------------------------

class AgentSkillsSettings(BaseModel):
    enable_user_slash_command: bool = Field(
        default=True,
        title="Enable /skill slash command",
        description="Allow users to activate skills explicitly with '/skill <name>'.",
    )
    max_scan_depth: int = Field(
        default=5,
        title="Max directory scan depth",
        description="How deep to recurse when looking for SKILL.md files.",
        ge=1,
        le=10,
    )
    sandbox_mode: str = Field(
        default="docker",
        title="Sandbox mode",
        description=(
            "How to execute skill scripts: 'docker' (recommended for production, "
            "full isolation) or 'subprocess' (development/environments without Docker)."
        ),
    )
    sandbox_timeout_seconds: int = Field(
        default=60,
        title="Sandbox timeout (seconds)",
        description="Maximum wall-clock time allowed for a skill script to run.",
        ge=5,
        le=300,
    )
    sandbox_max_memory: str = Field(
        default="128m",
        title="Sandbox max memory (Docker mode)",
        description=(
            "Memory limit for Docker containers running skill scripts "
            "(e.g. '128m', '512m', '1g')."
        ),
    )


@plugin
def settings_model():
    return AgentSkillsSettings


# ---------------------------------------------------------------------------
# Registry – one per agent (stray cat) to support multi-tenancy
# ---------------------------------------------------------------------------

# We store one registry per agent_id in a module-level dict.
# Grinning Cat is multi-tenant: each StrayCat has its own agent_id.
_registries: dict[str, SkillRegistry] = {}
# One populate task per agent_id to avoid concurrent discovery races.
_populate_tasks: dict[str, asyncio.Task] = {}


async def _get_registry(cat: CheshireCat) -> SkillRegistry:
    """Return (or lazily create) the SkillRegistry for this agent."""
    agent_id = cat.agent_key
    if agent_id not in _registries:
        _registries[agent_id] = SkillRegistry()

    if agent_id not in _populate_tasks:
        _populate_tasks[agent_id] = asyncio.create_task(_populate_registry(_registries[agent_id], cat))

    try:
        await _populate_tasks[agent_id]
    except Exception as exc:
        # Keep behavior resilient: avoid crashing hooks/tools if discovery fails.
        log.error(f"agent_skills: registry population failed for agent '{agent_id}' - {exc}")
    finally:
        task = _populate_tasks.get(agent_id)
        if task is not None and task.done():
            _populate_tasks.pop(agent_id, None)

    return _registries[agent_id]


async def _get_registry_from_stray_cat(stray_cat: StrayCat) -> SkillRegistry | None:
    """Return (or lazily create) the SkillRegistry for this agent."""
    lizard = BillTheLizard()
    cat = await lizard.get_cheshire_cat(stray_cat.agent_key)
    if not cat:
        return None

    return await _get_registry(cat)


def _parse_skill_slash_command(text: str) -> tuple[str, str] | None:
    """Parse '/skill <name> [optional user request]' and return (name, remaining_text)."""
    stripped = text.strip()
    if not stripped.lower().startswith("/skill"):
        return None

    tail = stripped[len("/skill"):].strip()
    if not tail:
        return None

    parts = tail.split(maxsplit=1)
    skill_name = parts[0].strip()
    remaining_request = parts[1].strip() if len(parts) > 1 else ""
    return skill_name, remaining_request


def _resolve_skill_script_path(record: SkillRecord, script_filename: str) -> tuple[Path | None, str | None]:
    """Resolve script path inside `<skill>/scripts` and block traversal/absolute paths."""
    rel = Path(script_filename.strip())

    if not script_filename.strip() or rel.is_absolute() or any(part == ".." for part in rel.parts):
        return None, "[sandbox] Invalid script path. Use a file inside the skill's scripts/ folder."

    scripts_root = (record.base_dir / "scripts").resolve()
    script_path = (scripts_root / rel).resolve()

    try:
        script_path.relative_to(scripts_root)
    except ValueError:
        return None, "[sandbox] Script path escapes the skill scripts directory."

    if not script_path.is_file():
        scripts = [r for r in record.bundled_resources() if r.startswith("scripts/")]
        return None, (
            f"[sandbox] Script '{script_filename}' not found in skill '{record.name}'.\n"
            f"Available scripts: {', '.join(scripts) or 'none'}"
        )

    return script_path, None


async def _populate_registry(registry: SkillRegistry, cat: CheshireCat) -> None:
    """Scan all known skill directories and load into *registry*."""
    settings = await cat.mad_hatter.get_plugin().load_settings()
    max_depth = int(settings.get("max_scan_depth", 5))

    plugins_dir = Path(get_plugins_path())
    for plugin_dir in cat.mad_hatter.active_plugins:
        skills_subdir = plugins_dir / plugin_dir / "skills"
        if skills_subdir.is_dir():
            count = registry.load_from_directory(skills_subdir, max_depth=max_depth)
            if count:
                log.debug(f"agent_skills: loaded {count} skill(s) from {skills_subdir}")
        elif (plugins_dir / plugin_dir).is_dir():
            # Plugin exists but has no skills/ folder: debug-level info
            log.debug(f"agent_skills: plugin '{plugin_dir}' has no skills/ directory – skipping")

    total = len(registry)
    log.debug(f"agent_skills: registry ready – {total} skill(s) discovered")


# ---------------------------------------------------------------------------
# Hook: inject skill catalog into the system prompt (Tier 1)
# ---------------------------------------------------------------------------

@hook(priority=1)
async def agent_prompt_prefix(prefix: str, cat: StrayCat) -> str:
    """
    Append the Agent Skills catalog to the system prompt prefix.

    This is Tier 1 of the progressive-disclosure model: the LLM sees the
    compact catalog (name + description, ~50-100 tokens per skill) from the
    start of every conversation.
    """
    registry = await _get_registry_from_stray_cat(cat)

    if not registry or not len(registry):
        return prefix

    catalog_xml = registry.catalog_xml()
    settings = await cat.mad_hatter.get_plugin().load_settings()
    slash_hint = ""
    if settings.get("enable_user_slash_command", True):
        slash_hint = " Users can also explicitly activate a skill by writing '/skill <name>'."

    skill_block = f"""
## Agent Skills

You have access to the following Agent Skills. Each skill provides specialised
instructions and resources for a specific task domain.

**How to use skills:**
When you identify that a task matches a skill's description, call the
`activate_skill` tool with the skill's name to load its full instructions
before proceeding.{slash_hint}

{catalog_xml}
"""
    return prefix + skill_block


# ---------------------------------------------------------------------------
# Hook: reset activations at session start (avoid stale context) and handle
#       /skill slash command (user-explicit activation)
# ---------------------------------------------------------------------------

@hook
async def before_cat_reads_message(user_message: UserMessage, cat: StrayCat) -> UserMessage:
    """
    Reset the activation tracking set at the start of each new conversation
    turn so the model can always re-activate a skill if needed.

    Note: we do *not* clear the registry itself — that is session-persistent.
    """
    # Only reset if this is a brand-new conversation (no prior messages)
    registry = await _get_registry_from_stray_cat(cat)
    if not registry:
        return user_message

    if len(cat.working_memory.history) == 0:
        registry.reset_activations()

    # Intercept /skill <name> commands from the user.
    # When the user writes '/skill pdf-processing', this hook injects the full skill content into the working memory so
    # the LLM already has it available when generating the response.
    settings = await cat.mad_hatter.get_plugin().load_settings()
    if not settings.get("enable_user_slash_command", True):
        return user_message

    parsed = _parse_skill_slash_command(user_message.text)
    if parsed is None:
        return user_message

    skill_name, remaining_request = parsed
    content = registry.activate(skill_name)

    if content is None:
        available = ", ".join(s.name for s in registry.all_skills()) or "none"
        user_message.text = (
            f"[Agent Skills] Skill '{skill_name}' not found. "
            f"Available skills: {available}."
        )
        return user_message

    # Prepend the skill content to the user message so the LLM sees it
    if remaining_request:
        user_message.text = (
            f"[Agent Skills - skill '{skill_name}' activated]\n\n"
            f"{content}\n\n"
            f"[User request]\n{remaining_request}"
        )
    else:
        user_message.text = (
            f"[Agent Skills - skill '{skill_name}' activated]\n\n"
            f"{content}\n\n"
            f"Now proceed with the user's request using this skill."
        )
    return user_message


# ---------------------------------------------------------------------------
# Tool: activate_skill (model-driven activation – Tier 2)
# ---------------------------------------------------------------------------

@tool
async def activate_skill(skill_name: str, cat: StrayCat) -> str:
    """
    Load and return the full instructions for an Agent Skill.

    Use this tool when a user's request matches a skill description from the
    available_skills catalog. Pass the skill's exact 'name' as the input.

    Returns the complete SKILL.md content (Tier 2) plus a listing of any
    bundled resource files (Tier 3 discovery, not yet loaded).

    Input is the skill name (e.g. 'pdf-processing').
    """
    registry = await _get_registry_from_stray_cat(cat)
    if not registry:
        return "[Agent Skills] Skills not available. This plugin is not active in this session."
    content = registry.activate(skill_name)

    if content is None:
        available = ", ".join(s.name for s in registry.all_skills()) or "none"
        return (
            f"[Agent Skills] Skill '{skill_name}' not found. "
            f"Available skills: {available}."
        )

    return content


# ---------------------------------------------------------------------------
# Tool: list_skills  (introspection helper)
# ---------------------------------------------------------------------------

@tool
async def list_skills(cat: StrayCat) -> str:
    """
    List all available Agent Skills with their names and descriptions.

    Use this when the user asks 'what skills do you have?', 'what can you do?',
    or any similar question about available capabilities.
    Input is always None.
    """
    registry = await _get_registry_from_stray_cat(cat)
    if not registry:
        return "[Agent Skills] Skills not available. This plugin is not active in this session."

    skills = registry.all_skills()

    if not skills:
        return "No Agent Skills are currently installed."

    lines = ["Available Agent Skills:\n"]
    for s in skills:
        compat = f" (requires: {s.compatibility})" if s.compatibility else ""
        lines.append(f"- **{s.name}**{compat}: {s.description}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool: reload_skills (admin / dev helper)
# ---------------------------------------------------------------------------

@tool
async def reload_skills(cat: StrayCat) -> str:
    """
    Force a re-scan of all plugin directories to pick up newly installed skills.

    Useful after installing a new plugin that ships Agent Skills.
    Input is always None.
    """
    agent_id = cat.agent_key
    # Clear existing registry for this agent
    if agent_id in _registries:
        del _registries[agent_id]
    _populate_tasks.pop(agent_id, None)

    # Re-populate
    registry = await _get_registry_from_stray_cat(cat)
    if not registry:
        return "[Agent Skills] Failed to reload skills. This plugin may not be active in this session."

    count = len(registry)
    names = ", ".join(s.name for s in registry.all_skills()) or "none"
    return f"Agent Skills reloaded. {count} skill(s) found: {names}."


# ---------------------------------------------------------------------------
# Tool: run_skill_script (safe execution of scripts bundled in a skill)
# ---------------------------------------------------------------------------

@tool
async def run_skill_script_tool(tool_input: str, cat: StrayCat) -> str:
    """
    Safely executes a Python script bundled in an Agent Skill.

    Uses the three-layer sandbox (AST scan → Docker → SandboxResult).

    The input format is: skill_name:script_filename [arg1 arg2 ...]
    Examples:
    "csv-analyzer:inspect_csv.py /data/report.csv"
    "csv-analyzer:stats.py /data/report.csv --columns revenue,cost"

    Use this tool when a skill's instructions ask you to
    run one of its scripts (typically in the scripts/ folder).
    """
    import shlex

    registry = await _get_registry_from_stray_cat(cat)
    if not registry:
        return "[sandbox] Skills not available. This plugin is not active in this session."

    # --- Parsing dell'input ------------------------------------------------
    try:
        parts = shlex.split(tool_input.strip())
    except ValueError as exc:
        return f"[sandbox] Input not valid: {exc}"

    if not parts:
        return "[sandbox] Empty input. Format: skill_name:script_filename [args...]"

    first = parts[0]
    if ":" not in first:
        return (
            "[sandbox] Wrong format. Use: skill_name:script_filename [args...]\n"
            "Example: csv-analyzer:inspect_csv.py /data/report.csv"
        )

    skill_name, script_filename = first.split(":", 1)
    script_args = parts[1:]

    # --- Resolve the real path of the script --------------------------------
    record = registry.get(skill_name.strip())
    if record is None:
        available = ", ".join(s.name for s in registry.all_skills()) or "none"
        return f"[sandbox] Skill '{skill_name}' not found. Available: {available}."

    script_path, path_error = _resolve_skill_script_path(record, script_filename)
    if path_error:
        return path_error
    assert script_path is not None

    # --- Read the settings of the plugin ----------------------------------
    settings = await cat.mad_hatter.get_plugin().load_settings()
    use_docker = settings.get("sandbox_mode", "docker") == "docker"
    mode = SandboxMode.DOCKER if use_docker else SandboxMode.SUBPROCESS
    timeout = int(settings.get("sandbox_timeout_seconds", 60))
    max_memory = str(settings.get("sandbox_max_memory", "128m"))

    # --- Execute within the sandbox ----------------------------------------------
    try:
        result = run_skill_script(
            script_path,
            script_args,
            mode=mode,
            timeout_seconds=timeout,
            max_memory=max_memory,
        )
    except SecurityError as exc:
        return f"[sandbox] ❌ Script blocked by the analysis of security:\n{exc}"
    except Exception as exc:
        return f"[sandbox] ❌ Unexpected error: {exc}"

    return result.to_llm_string()
