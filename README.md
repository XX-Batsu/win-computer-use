# win-computer-use

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)
![Node 18+](https://img.shields.io/badge/node-18+-green.svg)
![Status: experimental](https://img.shields.io/badge/status-experimental-orange.svg)

A toolset that lets Claude interact with a Windows desktop environment — take
screenshots, move the mouse, type text, manage windows, run shell commands, and
control virtual desktops.

Two usage modes:

- **Interactive** — Claude Code / Claude Desktop uses an MCP server to call Windows operations on demand
- **Scheduled automation** — A Python scheduler runs unattended tasks via `claude -p` (headless), with full logging and screenshot recording per step

## Status

`experimental` — APIs may change without notice. Issues and PRs welcome.

## Architecture

```
[Interactive mode]
Claude → TypeScript MCP Server → HTTP+Bearer → Python Engine (127.0.0.1:8765) → Windows API

[Scheduled automation]
Scheduler (cron) → claude -p → Python Engine HTTP API → logs/<task>/<timestamp>/
```

| Component     | Language         | Role |
|---------------|------------------|------|
| `engine/`     | Python (FastAPI) | All Windows operations; runs on `127.0.0.1:8765` |
| `mcp-server/` | TypeScript       | Bridges Claude Code to the engine via MCP protocol |
| `scheduler/`  | Python           | Cron-based task runner using Claude Code CLI headless mode |
| `tasks/`      | YAML             | Task definitions (prompt, schedule, model, max steps) |

## Requirements

- Windows 10/11
- Python 3.12+
- Node.js 18+ (see [nvm-windows](https://github.com/coreybutler/nvm-windows))
- [Claude Code CLI](https://github.com/anthropics/claude-code) — required for scheduled mode

## Setup

### 1. Create `.env`

```bash
cp .env.example .env
```

Generate a strong random secret:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Set `ENGINE_SECRET` in `.env`. All API coordinates internally use logical pixels; the engine maps to physical via a per-monitor DPI map built at startup.

### 2. Install engine

```bash
cd engine
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python main.py   # binds to 127.0.0.1:8765; fails fast if ENGINE_SECRET missing
```

### 3. Build the MCP server

```bash
cd mcp-server
npm install
npm run build
```

## Interactive Mode (Claude Code + MCP)

**Claude Desktop** — edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "win-computer-use": {
      "command": "node",
      "args": ["C:\\path\\to\\win-computer-use\\mcp-server\\dist\\index.js"],
      "env": { "ENGINE_SECRET": "<your-secret>" }
    }
  }
}
```

After registering, enable tool permissions in **Settings → Connector → win-computer-use**.

**Claude Code CLI (global scope)**:

```bash
claude mcp add win-computer-use --transport stdio --scope user \
  -e ENGINE_SECRET=<your-secret> \
  -- node C:/path/to/win-computer-use/mcp-server/dist/index.js
```

Restart Claude after registering. You get 26 Windows tools (scheduled mode supports 22; see below).

### Available tools

| Category | Tools |
|----------|-------|
| Screenshot | `screenshot`, `screenshot_zoom`, `screenshot_annotate` |
| Mouse | `mouse_move`, `mouse_click`, `mouse_double_click`, `mouse_drag`, `mouse_down`, `mouse_up`, `mouse_scroll` |
| Keyboard | `keyboard_type`, `keyboard_hotkey`, `keydown`, `keyup` |
| Windows | `list_windows`, `focus_window`, `set_window_state` |
| Clipboard | `get_clipboard`, `set_clipboard` |
| Shell | `run_shell` |
| Virtual desktops | `list_desktops`, `switch_desktop`, `create_desktop`, `delete_desktop` |
| UI automation | `find_element`, `element_at` |

All coordinates are **screenshot image pixels**. The MCP server remaps them to logical pixels before forwarding to the engine; `element_at` and `find_element` also produce an `annotate_token` that click tools require for verification.

## Scheduled Automation Mode

```bash
cd scheduler
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Define tasks in `tasks/*.yaml` (see `tasks/example.yaml` for reference):

```yaml
- name: daily-screenshot
  cron: "0 9 * * *"
  model: claude-sonnet-4-6
  max_steps: 20
  prompt: Take a screenshot of the current desktop and save it to %USERPROFILE%/Documents/reports/.
```

Run once: `python scheduler/run.py --task daily-screenshot`
Run on schedule: `python scheduler/scheduler.py`

Scheduled mode supports 22 of the 26 tools. Interactive-only: `screenshot_zoom`, `screenshot_annotate`, `find_element`, `element_at`. These require stateful MCP context that the headless CLI does not provide.

### Logs

Each run creates `logs/<task-name>/<timestamp>/` containing `step_NN_pre.png`, `step_NN_prompt.txt`, `step_NN_response.json`, and `run.json`. `run.json` is updated after every step; `status: "in_progress"` with no `finished_at` indicates a crashed run.

## Run Engine as a Windows Service (NSSM)

Requires Administrator. The service **must** run as a normal user account logged into the interactive desktop — not as SYSTEM. Windows services run in Session 0 by default, which has no desktop access.

```bat
nssm install WinComputerUseEngine "C:\path\to\engine\venv\Scripts\python.exe" "C:\path\to\engine\main.py"
nssm set WinComputerUseEngine AppDirectory "C:\path\to\engine"
nssm set WinComputerUseEngine AppEnvironmentExtra "ENGINE_SECRET=<secret>"
nssm set WinComputerUseEngine ObjectName ".\<user>" "<password>"
nssm start WinComputerUseEngine
```

## Engine API

All endpoints require `Authorization: Bearer <ENGINE_SECRET>` and bind to `127.0.0.1:8765` (loopback only).

All responses follow:

```json
{ "success": true,  "data": <any>, "error": null, "timed_out": false }
{ "success": false, "data": null,  "error": "...", "timed_out": false }
```

Endpoint definitions live in [`engine/routers/`](engine/routers/). If you hotplug a monitor or change display scaling during a session, POST `/reload-dpi` to refresh the map.

## Security

- Binds to `127.0.0.1` only
- `ENGINE_SECRET` is the shared Bearer token between all components
- Never commit `.env` — it is gitignored; commit `.env.example` instead
- `/shell` does not use `shell=True`; `ENGINE_SECRET` is stripped from child process environment
- `pyautogui.FAILSAFE` is `False` — the mouse-corner escape hatch is disabled for unattended use
- In NSSM mode, `AppEnvironmentExtra` secrets are readable by local administrators via the registry; mitigate with a dedicated low-privilege user account

## Contributing

Issues and PRs welcome. This is an experimental project and direction may shift.

## License

MIT — see [LICENSE](LICENSE). Third-party attributions in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

## Acknowledgements

Built on [Model Context Protocol](https://github.com/modelcontextprotocol), [FastAPI](https://fastapi.tiangolo.com/), and [pywinauto](https://github.com/pywinauto/pywinauto).

"Claude" is a trademark of Anthropic, PBC. This project is unofficial and is not affiliated with, endorsed by, or sponsored by Anthropic.
