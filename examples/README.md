# Agent Skills – Example Plugins

This directory contains pre-built ZIP packages of example plugins that demonstrate
how to ship Agent Skills alongside a Grinning Cat plugin.

## Available examples

| File                                 | Description                                                                                                  |
|--------------------------------------|--------------------------------------------------------------------------------------------------------------|
| `example_consumer_skills_plugin.zip` | Full example: plugin with `@hook`, `plugin.json`, and a real CSV-analysis skill with scripts and references. |

## How to install

### Via Admin Panel
1. Open the Grinning Cat Admin Panel → **Plugins → Install plugin**
2. Upload the `.zip` file
3. Enable the plugin from the plugin list

### Via REST API
```bash
curl -X POST "http://localhost:1865/plugins/install/upload" \
  -H "Authorization: Bearer <your_token>" \
  -F "file=@example_consumer_skills_plugin.zip"
```

## How to rebuild the ZIPs

The source lives in `grinning-cat-core` under `cat/plugins/example_*`.
Run from the root of `grinning-cat-core`:

```bash
make build-example-skill-plugins
```