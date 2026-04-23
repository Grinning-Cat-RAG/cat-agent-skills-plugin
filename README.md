# Agent Skills Plugin for Grinning Cat

[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-compatible-blue)](https://agentskills.io)
[![Standard](https://img.shields.io/badge/standard-open-green)](https://github.com/agentskills/agentskills)

Integrates the open [Agent Skills standard](https://agentskills.io) into [Grinning Cat](https://github.com/matteocacciola/grinning-cat-core).

This plugin is the **universal interpreter**: any other Grinning Cat plugin can ship skills in `SKILL.md` format, and this plugin automatically discovers them, builds a compact catalog, injects it into the system prompt, and loads full instructions on demand — all following the progressive-disclosure model from the spec.

---

## What are Agent Skills?

Agent Skills are folders of instructions, scripts, and resources that agents discover and use to perform tasks more accurately and efficiently. The format is a simple `SKILL.md` file with YAML frontmatter + Markdown instructions, optionally extended with scripts, references, and assets.

The standard was created by Anthropic and released as an open format in December 2025. It has since been adopted by Claude Code, OpenAI Codex CLI, and many community tools.

---

## Architecture

```
┌────────────────────────────────────────────────┐
│                 Grinning Cat                   │
│                                                │
│  ┌──────────────────────────────────────────┐  │
│  │         agent_skills plugin              │  │
│  │                                          │  │
│  │  skill_loader.py   ──► discover / parse  │  │
│  │  agent_skills.py   ──► hooks + @tool     │  │
│  │  sandbox.py        ──► script execution  │  │
│  │  endpoints.py      ──► REST API          │  │
│  └─────────────┬────────────────────────────┘  │
│                │  scans                        │
│                ▼                               │
│  ┌──────────────────────────────────────────┐  │
│  │  plugins/                                │  │
│  │  ├── my_plugin/                          │  │
│  │  │   ├── my_plugin.py  (@hook, @tool…)   │  │
│  │  │   └── skills/                         │  │
│  │  │       └── my-skill/                   │  │
│  │  │           ├── SKILL.md  ◄── parsed    │  │
│  │  │           ├── scripts/                │  │
│  │  │           └── references/             │  │
│  │  └── another_plugin/                     │  │
│  │      └── skills/                         │  │
│  │          └── another-skill/SKILL.md      │  │
│  └──────────────────────────────────────────┘  │
└────────────────────────────────────────────────┘
```

### Progressive disclosure (3 tiers)

| Tier | Content                          | When loaded                      | Token cost                 |
|------|----------------------------------|----------------------------------|----------------------------|
| 1    | `name` + `description` per skill | At startup, in the system prompt | ~50–100 tokens per skill   |
| 2    | Full `SKILL.md` body             | When the LLM activates the skill | < 5 000 tokens recommended |
| 3    | Scripts, references, assets      | On demand, only if referenced    | Variable                   |

---

## Installation

1. Copy the `agent_skills_plugin/` folder into your Grinning Cat `plugins/` directory.
2. Enable the plugin via the Admin Panel or REST API.
3. (Re)start Grinning Cat — skill discovery runs automatically at boot.

```
plugins/
└── agent_skills_plugin/        ← this plugin
    ├── agent_skills.py         ← hooks + tools
    ├── endpoints.py            ← REST API
    ├── sandbox.py              ← 3-layer sandboxed script execution
    ├── skill_loader.py         ← core discovery engine
    ├── plugin.json
    ├── README.md
    └── examples/               ← ready-to-install example plugins (ZIP)
        ├── README.md
        ├── example_dummy_skills_plugin.zip
        └── example_consumer_skills_plugin.zip
```

### Example plugins

The `examples/` directory contains two pre-built consumer plugins you can install immediately to try the system:

| ZIP                                  | Description                                                                              |
|--------------------------------------|------------------------------------------------------------------------------------------|
| `example_dummy_skills_plugin.zip`    | Minimal plugin that ships a single `SKILL.md` skill and no Python code.                  |
| `example_consumer_skills_plugin.zip` | Full example: a CSV analyser skill with a `scripts/` folder and a companion Python tool. |

Install them from the Admin Panel (*Plugins → Upload*) or via the REST API:

```bash
curl -X POST http://localhost:1865/plugins/upload \
  -H "Authorization: Bearer <token>" \
  -F "file=@examples/example_consumer_skills_plugin.zip"
```

---

## How to ship skills with your own plugin

You don't need to modify this plugin at all. Just add a `skills/` directory inside your plugin:

```
plugins/
└── my_plugin/
    ├── my_plugin.py          ← your hooks, tools, forms as usual
    └── skills/
        └── my-skill/
            ├── SKILL.md      ← required
            ├── scripts/      ← optional: Python helpers the LLM can run
            └── references/   ← optional: supporting docs, schemas, samples
```

### Minimal SKILL.md

```markdown
---
name: my-skill
description: Extracts and transforms sales data from CSV files. Use when
  the user uploads a CSV or mentions sales reports.
version: "1.0"
---

# My Skill

## Instructions

1. Ask the user for the CSV file if not already provided.
2. Run `scripts/process.py` on the file.
3. Summarise the results in a table.
```

That's it. On next startup (or after calling `POST /custom/agent-skills/reload`), the skill will appear in the catalog.

---

## Features

### Hooks (automatic)

| Hook                       | Effect                                                                                                  |
|----------------------------|---------------------------------------------------------------------------------------------------------|
| `agent_prompt_prefix`      | Injects the Tier-1 skill catalog into every system prompt                                               |
| `before_cat_reads_message` | Handles `/skill <name>` slash command; resets activation tracking at the start of each new conversation |

### Tools (LLM-callable)

| Tool                    | Description                                                               |
|-------------------------|---------------------------------------------------------------------------|
| `activate_skill`        | Load full skill instructions + resource listing (Tier 2)                  |
| `list_skills`           | Show all available skills with names and descriptions                     |
| `reload_skills`         | Force re-scan of all plugin directories to pick up newly installed skills |
| `run_skill_script_tool` | Safely execute a Python script bundled in a skill inside the sandbox      |

#### `run_skill_script_tool` input format

```
skill_name:script_filename [arg1 arg2 ...]
```

Examples:

```
csv-analyzer:inspect_csv.py /data/report.csv
csv-analyzer:stats.py /data/report.csv --columns revenue,cost
```

### REST endpoints

| Method | Path                                   | Description                         |
|--------|----------------------------------------|-------------------------------------|
| `GET`  | `/custom/agent-skills/`                | List all skills                     |
| `GET`  | `/custom/agent-skills/{name}`          | Get skill details + full body       |
| `POST` | `/custom/agent-skills/activate/{name}` | Activate a skill via API            |
| `POST` | `/custom/agent-skills/reload`          | Force re-discovery of all skills    |
| `GET`  | `/custom/agent-skills/catalog/xml`     | Raw XML Tier-1 catalog (debug/test) |

### Plugin settings

| Setting                     | Default      | Description                                                             |
|-----------------------------|--------------|-------------------------------------------------------------------------|
| `enable_user_slash_command` | `true`       | Allow users to explicitly activate a skill by typing `/skill <name>`   |
| `max_scan_depth`            | `5`          | Max directory depth when recursively searching for `SKILL.md` files    |
| `sandbox_mode`              | `"docker"`   | Script execution mode: `"docker"` (production) or `"subprocess"` (dev) |
| `sandbox_timeout_seconds`   | `60`         | Maximum wall-clock time (seconds) allowed for a skill script           |
| `sandbox_max_memory`        | `"128m"`     | Memory limit for Docker containers (e.g. `"128m"`, `"512m"`, `"1g"`)  |

---

## Sandbox

Skill scripts are executed through a **three-layer defence-in-depth sandbox** (`sandbox.py`):

```
Layer 1 – AST scan
  Static analysis of the Python AST before execution.
  Blocks dangerous imports (os.system, subprocess, socket, etc.) and
  unsafe builtins (__import__, eval, exec, open, …).

Layer 2 – Docker container  (sandbox_mode = "docker")
  Executes the script in an isolated container with:
    --read-only filesystem
    --cap-drop=ALL
    --network=none
    --user=nobody
    --pids-limit=64
    --tmpfs /tmp (512 KB)
    --memory=<sandbox_max_memory>
  Falls back to Layer 3 if Docker is unavailable.

Layer 3 – subprocess with resource limits  (sandbox_mode = "subprocess")
  Runs the script in a child process with:
    ulimit -v (virtual memory)
    ulimit -t (CPU time)
    timeout <sandbox_timeout_seconds>
```

> **Recommendation:** keep `sandbox_mode = "docker"` in production. Use `"subprocess"` only in development environments where Docker is not available.

---

## Multi-tenancy

Grinning Cat is a multi-tenant framework. This plugin maintains **one `SkillRegistry` per `agent_id`**, so each chatbot instance has its own isolated skill catalog. Skills are discovered once at first access and cached in memory for the lifetime of the process.

---

## Security

- Skills are discovered **only from directories that belong to installed plugins**, which must be explicitly installed by an admin — so untrusted `SKILL.md` injection (a risk highlighted in the spec for local filesystem tools) does not apply here.
- All REST endpoints respect Grinning Cat's standard authentication and permission model.
- Skill scripts are always executed inside the sandbox regardless of their source.

---

## Compatibility

This plugin implements the [Agent Skills specification](https://agentskills.io/specification). Any skill folder that is valid for Claude Code or another spec-compliant client can be placed in a Grinning Cat plugin's `skills/` subdirectory and will work without modification.

---

## License

GPL-3.0 — consistent with the Grinning Cat Core license.
