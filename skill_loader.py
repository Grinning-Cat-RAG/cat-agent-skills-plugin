"""
Agent Skills Loader
===================
Core engine for the Agent Skills open standard (agentskills.io).

Implements the full 5-step lifecycle:
  1. Discover  – scan plugin directories for SKILL.md files
  2. Parse     – extract YAML frontmatter + Markdown body
  3. Disclose  – inject a compact skill catalog into the system prompt (Tier 1)
  4. Activate  – load full SKILL.md body on demand (Tier 2)
  5. Manage    – deduplicate activations, surface bundled resources (Tier 3)
"""
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from cat import log


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """One discovered and parsed Agent Skill."""
    name: str
    description: str
    skill_md_path: Path          # absolute path to SKILL.md
    base_dir: Path               # parent directory of SKILL.md
    body: str = ""               # markdown body (without frontmatter)
    license: Optional[str] = None
    compatibility: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)

    @property
    def location(self) -> str:
        return str(self.skill_md_path)

    def bundled_resources(self) -> list[str]:
        """List all files inside the skill directory (excluding SKILL.md itself)."""
        resources = []
        for p in sorted(self.base_dir.rglob("*")):
            if p.is_file() and p.name != "SKILL.md":
                resources.append(str(p.relative_to(self.base_dir)))
        return resources


# ---------------------------------------------------------------------------
# YAML frontmatter parser (lightweight, no external deps needed)
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    Extract YAML frontmatter and body from a SKILL.md string.

    Returns (frontmatter_dict, body_string).
    Falls back gracefully on malformed YAML.
    """
    if not text.startswith("---"):
        return {}, text.strip()

    # Find the closing ---
    end_idx = text.find("\n---", 3)
    if end_idx == -1:
        return {}, text.strip()

    yaml_block = text[3:end_idx].strip()
    body = text[end_idx + 4:].strip()

    frontmatter: dict = {}
    try:
        import yaml  # PyYAML is already a dependency of langchain
        frontmatter = yaml.safe_load(yaml_block) or {}
    except Exception:
        # Lenient fallback: try to fix unquoted colon values (common issue)
        try:
            import yaml
            fixed = re.sub(r"^(\s*\w[\w-]*:\s*)(.+:.+)$", r'\1"\2"', yaml_block, flags=re.MULTILINE)
            frontmatter = yaml.safe_load(fixed) or {}
        except Exception as exc:
            log.warning(f"agent_skills: could not parse frontmatter – {exc}")

    return frontmatter, body


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".env"}


def discover_skills(root: Path, max_depth: int = 5) -> list[SkillRecord]:
    """
    Walk *root* looking for subdirectories that contain a SKILL.md file.
    Returns a list of parsed SkillRecord objects, sorted by name.
    """
    if not root.exists() or not root.is_dir():
        return []

    records: dict[str, SkillRecord] = {}  # keyed by name for collision handling

    def _walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return

        for entry in entries:
            if not entry.is_dir() or entry.name in _SKIP_DIRS:
                continue
            skill_md = entry / "SKILL.md"
            if skill_md.is_file():
                record = _parse_skill_md(skill_md)
                if record is not None:
                    if record.name in records:
                        log.warning(f"agent_skills: name collision for '{record.name}' – keeping first found, ignoring {skill_md}")
                    else:
                        records[record.name] = record
                else:
                    # Parsing failed: provide better error context
                    log.debug(f"agent_skills: skipping malformed SKILL.md at {skill_md} – check name, description, and format")
            else:
                # Go deeper even if no SKILL.md at this level
                _walk(entry, depth + 1)

    _walk(root, 1)
    return sorted(records.values(), key=lambda r: r.name)


def _parse_skill_md(skill_md_path: Path) -> Optional[SkillRecord]:
    """Read and parse a single SKILL.md file into a SkillRecord."""
    try:
        raw = skill_md_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(f"agent_skills: cannot read {skill_md_path} – {exc}")
        return None

    frontmatter, body = _parse_frontmatter(raw)

    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()

    if not description:
        log.warning(f"agent_skills: missing or empty 'description' in {skill_md_path}")
        return None

    # Validate name: must not be empty and must match pattern
    if not name:
        name = skill_md_path.parent.name
        log.warning(f"agent_skills: missing 'name' in {skill_md_path}, using directory name '{name}'")

    if not re.match(r"^[a-z0-9-]+$", name.lower()):
        log.warning(f"agent_skills: invalid 'name' format in {skill_md_path} – must contain only lowercase alphanumeric and hyphens")
        return None

    # Validate description length
    if len(description) < 10:
        log.warning(f"agent_skills: 'description' too short in {skill_md_path} (min 10 chars)")
        return None
    if len(description) > 500:
        log.warning(f"agent_skills: 'description' too long in {skill_md_path} (max 500 chars)")
        return None

    # Sanitise name: must be lowercase, alphanumeric + hyphens
    name = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")

    allowed_tools_raw = frontmatter.get("allowed-tools", "")
    if isinstance(allowed_tools_raw, str):
        allowed_tools = allowed_tools_raw.split()
    elif isinstance(allowed_tools_raw, list):
        allowed_tools = [str(tool).strip() for tool in allowed_tools_raw if str(tool).strip()]
    else:
        allowed_tools = []

    metadata = frontmatter.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    return SkillRecord(
        name=name,
        description=description,
        skill_md_path=skill_md_path,
        base_dir=skill_md_path.parent,
        body=body,
        license=frontmatter.get("license"),
        compatibility=frontmatter.get("compatibility"),
        metadata=metadata,
        allowed_tools=allowed_tools,
    )


# ---------------------------------------------------------------------------
# Catalog builder (Tier 1 – disclose to model)
# ---------------------------------------------------------------------------

def build_skill_catalog_xml(skills: list[SkillRecord]) -> str:
    """
    Render the compact skill catalog as XML to be injected into the system prompt.
    Each skill contributes ~50-100 tokens (name + description only).
    """
    if not skills:
        return ""

    lines = ["<available_skills>"]
    for s in skills:
        lines.append(f"  <skill>")
        lines.append(f"    <n>{_xml_escape(s.name)}</n>")
        lines.append(f"    <description>{_xml_escape(s.description)}</description>")
        lines.append(f"    <location>{_xml_escape(s.location)}</location>")
        lines.append(f"  </skill>")
    lines.append("</available_skills>")
    return "\n".join(lines)


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# Skill activation (Tier 2)
# ---------------------------------------------------------------------------

def activate_skill(record: SkillRecord) -> str:
    """
    Return the full skill content wrapped in structured tags (Tier 2).
    Also lists bundled resources (Tier 3 discovery) without eagerly reading them.
    """
    resources = record.bundled_resources()
    resource_block = ""
    if resources:
        res_lines = ["<skill_resources>"]
        for r in resources:
            res_lines.append(f"  <file>{_xml_escape(r)}</file>")
        res_lines.append("</skill_resources>")
        resource_block = "\n" + "\n".join(res_lines)

    return (
        f'<skill_content name="{_xml_escape(record.name)}">\n'
        f"{record.body}\n\n"
        f"Skill directory: {_xml_escape(str(record.base_dir))}\n"
        f"Relative paths in this skill are relative to the skill directory."
        f"{resource_block}\n"
        f"</skill_content>"
    )


# ---------------------------------------------------------------------------
# Registry singleton (used by the plugin)
# ---------------------------------------------------------------------------

class SkillRegistry:
    """In-memory registry of discovered skills for the current session."""

    def __init__(self):
        self._skills: dict[str, SkillRecord] = {}
        self._activated: set[str] = set()
        self._activated_content: dict[str, str] = {}

    def load_from_directory(self, root: Path, max_depth: int = 5) -> int:
        """Discover and register skills from *root*. Returns number of new skills added."""
        found = discover_skills(root, max_depth=max_depth)
        added = 0
        for rec in found:
            if rec.name not in self._skills:
                self._skills[rec.name] = rec
                added += 1
        return added

    def all_skills(self) -> list[SkillRecord]:
        return sorted(self._skills.values(), key=lambda r: r.name)

    def get(self, name: str) -> Optional[SkillRecord]:
        return self._skills.get(name)

    def activate(self, name: str) -> Optional[str]:
        """Return activation content for *name*; None if not found. Caches within session."""
        rec = self._skills.get(name)
        if rec is None:
            return None

        # Return cached content if already activated in this session
        if name in self._activated:
            log.debug(f"agent_skills: '{name}' already activated – returning cached content")
            return self._activated_content.get(name, "")

        # Compute and cache the content
        content = activate_skill(rec)
        self._activated.add(name)
        self._activated_content[name] = content
        return content

    def reset_activations(self) -> None:
        self._activated.clear()
        self._activated_content.clear()

    def catalog_xml(self) -> str:
        return build_skill_catalog_xml(self.all_skills())

    def __len__(self) -> int:
        return len(self._skills)
