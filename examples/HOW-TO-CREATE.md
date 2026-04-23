# How to Create an Agent Skill for Grinning Cat

## Prerequisites

- A working Grinning Cat plugin (Python package in the `plugins/` directory)
- The `agent_skills` plugin installed and active

## Step 1 – Create the skills directory

Inside your existing plugin folder:

```
plugins/
└── my_plugin/
    ├── my_plugin.py
    └── skills/           ← add this
        └── my-skill/     ← one directory per skill
            └── SKILL.md  ← required
```

## Step 2 – Write the SKILL.md

### Required frontmatter fields

| Field         | Description                                      |
|---------------|--------------------------------------------------|
| `name`        | Lowercase, hyphens only, max 64 chars            |
| `description` | What the skill does and *when to use it*         |

### Optional frontmatter fields

| Field           | Description                                     |
|-----------------|-------------------------------------------------|
| `license`       | License identifier (e.g. Apache-2.0)            |
| `compatibility` | Environment requirements                        |
| `metadata`      | Arbitrary key-value pairs (author, version, …)  |
| `allowed-tools` | Space-separated pre-approved tool list          |

### Writing good instructions

- Be explicit about step-by-step actions.
- Include examples of input → output.
- Document edge cases.
- Reference supporting files with relative paths.

## Step 3 – Add supporting files (optional)

```
my-skill/
├── SKILL.md
├── scripts/
│   └── process.py      ← Python script the LLM can run
└── references/
    └── schema.md       ← extra documentation loaded on demand
```

Reference them inside SKILL.md:
```markdown
Run the processing script:
  scripts/process.py

See [schema reference](references/schema.md) for field definitions.
```

## Step 4 – Reload the registry

After installing your plugin, either:
- Restart Grinning Cat, or
- Call the REST endpoint: `POST /custom/agent-skills/reload`, or
- Ask the cat: "reload skills"

## Step 5 – Test your skill

Ask the cat something that matches your skill's description. Watch it:
1. Read the catalog (Tier 1 – already in system prompt)
2. Call `activate_skill` with your skill name (Tier 2)
3. Load any scripts/references it needs (Tier 3)

## Tips

- Keep `SKILL.md` under 500 lines. Move details to `references/`.
- The `description` field is the most critical for trigger accuracy.
  Include specific keywords users will actually say.
- Scripts should include error handling and clear output messages.
