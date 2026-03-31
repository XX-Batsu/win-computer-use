# win-computer-use

A toolset that lets Claude interact with a Windows desktop environment — take
screenshots, move the mouse, type text, manage windows, run shell commands, and
control virtual desktops.

Two usage modes:

- **Interactive** — Claude Code uses an MCP server to call Windows operations on demand
- **Scheduled automation** — A Python scheduler runs unattended tasks via `claude -p` (headless)

## Architecture

```
[Interactive mode]
Claude Code → TypeScript MCP Server → HTTP+Bearer → Python Engine (127.0.0.1:8765) → Windows API

[Scheduled automation]
Scheduler (cron) → claude -p → Python Engine HTTP API → logs/<task>/<timestamp>/
```

| Component     | Language         | Role |
|---------------|------------------|------|
| `engine/`     | Python (FastAPI) | All Windows operations; runs on `127.0.0.1:8765` |
| `mcp-server/` | TypeScript       | Bridges Claude Code to the engine via MCP protocol |
| `scheduler/`  | Python           | Cron-based task runner using Claude Code CLI headless mode |
| `tasks/`      | YAML             | Task definitions (prompt, schedule, model, max steps) |

## Status

`experimental` — APIs may change without notice.

## Quick Start

Setup instructions will be expanded in a later release. See `engine/requirements.txt`, `mcp-server/package.json`, and `scheduler/requirements.txt` for dependencies.

## License

MIT — see [LICENSE](LICENSE). Third-party attributions in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

"Claude" is a trademark of Anthropic, PBC. This project is unofficial and is not affiliated with, endorsed by, or sponsored by Anthropic.
