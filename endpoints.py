"""
Agent Skills – REST API endpoints
==================================
Exposes CRUD-style endpoints so the Admin Panel (and external clients) can
manage Agent Skills without going through the chat interface.

All endpoints are protected with the standard Grinning Cat permission check.
"""
from fastapi import HTTPException

from cat import endpoint
from cat.auth.connection import AuthorizedInfo
from cat.auth.permissions import AuthPermission, AuthResource, check_permissions

from .agent_skills import _get_registry, _registries, _populate_tasks

# ---------------------------------------------------------------------------
# GET /custom/agent-skills/ – list all discovered skills
# ---------------------------------------------------------------------------

@endpoint.get("/agent-skills/")
async def list_agent_skills(info: AuthorizedInfo = check_permissions(AuthResource.PLUGIN, AuthPermission.READ)):
    """
    Return the full catalog of discovered Agent Skills for the current agent.
    """
    assert info.cheshire_cat is not None, "AuthorizedInfo must include cheshire_cat instance"

    registry = await _get_registry(info.cheshire_cat)
    skills = registry.all_skills()

    return {
        "skills": [
            {
                "name": s.name,
                "description": s.description,
                "location": s.location,
                "license": s.license,
                "compatibility": s.compatibility,
                "metadata": s.metadata,
                "allowed_tools": s.allowed_tools,
                "resources": s.bundled_resources(),
            }
            for s in skills
        ],
        "total": len(skills),
    }


# ---------------------------------------------------------------------------
# GET /custom/agent-skills/{name} – get a single skill's full content
# ---------------------------------------------------------------------------

@endpoint.get("/agent-skills/{skill_name}")
async def get_agent_skill(
    skill_name: str,
    info: AuthorizedInfo = check_permissions(AuthResource.PLUGIN, AuthPermission.READ),
):
    """
    Return full metadata and body content of a specific Agent Skill.
    """
    assert info.cheshire_cat is not None, "AuthorizedInfo must include cheshire_cat instance"

    registry = await _get_registry(info.cheshire_cat)
    record = registry.get(skill_name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    return {
        "name": record.name,
        "description": record.description,
        "location": record.location,
        "body": record.body,
        "license": record.license,
        "compatibility": record.compatibility,
        "metadata": record.metadata,
        "allowed_tools": record.allowed_tools,
        "resources": record.bundled_resources(),
    }


# ---------------------------------------------------------------------------
# POST /custom/agent-skills/activate/{name}  – activate a skill via API
# ---------------------------------------------------------------------------

@endpoint.post("/agent-skills/activate/{skill_name}")
async def activate_agent_skill_api(
    skill_name: str,
    info: AuthorizedInfo = check_permissions(AuthResource.PLUGIN, AuthPermission.READ),
):
    """
    Activate a skill and return its full content (Tier 2 disclosure).
    This is the API counterpart of the `activate_skill` tool.
    """
    assert info.cheshire_cat is not None, "AuthorizedInfo must include cheshire_cat instance"

    registry = await _get_registry(info.cheshire_cat)
    content = registry.activate(skill_name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Skill '{skill_name}' not found")

    return {"skill_name": skill_name, "content": content}


# ---------------------------------------------------------------------------
# POST /custom/agent-skills/reload – force rescan
# ---------------------------------------------------------------------------

@endpoint.post("/agent-skills/reload")
async def reload_agent_skills(info: AuthorizedInfo = check_permissions(AuthResource.PLUGIN, AuthPermission.WRITE)):
    """
    Force a re-discovery of all Agent Skills (e.g. after installing a new plugin).
    """
    assert info.cheshire_cat is not None, "AuthorizedInfo must include cheshire_cat instance"

    if info.cheshire_cat.agent_key in _registries:
        del _registries[info.cheshire_cat.agent_key]
    _populate_tasks.pop(info.cheshire_cat.agent_key, None)

    registry = await _get_registry(info.cheshire_cat)
    skills = registry.all_skills()

    return {
        "message": "Agent Skills reloaded",
        "total": len(skills),
        "skills": [s.name for s in skills],
    }


# ---------------------------------------------------------------------------
# GET /custom/agent-skills/catalog/xml – raw XML catalog (debug)
# ---------------------------------------------------------------------------

@endpoint.get("/agent-skills/catalog/xml")
async def get_catalog_xml(info: AuthorizedInfo = check_permissions(AuthResource.PLUGIN, AuthPermission.READ)):
    """
    Return the raw XML catalog injected into the system prompt.
    Useful for debugging skill discovery.
    """
    assert info.cheshire_cat is not None, "AuthorizedInfo must include cheshire_cat instance"

    registry = await _get_registry(info.cheshire_cat)
    xml = registry.catalog_xml()
    return {"catalog_xml": xml, "skill_count": len(registry)}
