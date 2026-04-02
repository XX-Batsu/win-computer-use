"""
protocol.py — Pydantic models for the Windows Computer Use scheduler.

Defines all 22 tool command models, the discriminated Command union,
ClaudeResponse, TaskDefinition, and the JSON command schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal, Optional, Union, Annotated


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

class NoArgs(BaseModel):
    pass


# ---------------------------------------------------------------------------
# Screenshot
# Full-screen rule: width is None OR height is None → capture full screen
# ---------------------------------------------------------------------------

class ScreenshotArgs(BaseModel):
    top: Optional[int] = None
    left: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


class ScreenshotCmd(BaseModel):
    tool: Literal["screenshot"]
    args: ScreenshotArgs


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------

class MouseMoveArgs(BaseModel):
    x: int
    y: int


class MouseMoveCmd(BaseModel):
    tool: Literal["mouse_move"]
    args: MouseMoveArgs


class MouseClickArgs(BaseModel):
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"


class MouseClickCmd(BaseModel):
    tool: Literal["mouse_click"]
    args: MouseClickArgs


class DragWaypoint(BaseModel):
    x: int
    y: int


class MouseDragArgs(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    button: Literal["left", "right", "middle"] = "left"
    duration: float = Field(0.5, ge=0.0, le=10.0)
    hold_before: float = Field(0.2, ge=0.0, le=5.0)
    steps: Optional[int] = Field(None, ge=1, le=200)
    waypoints: Optional[list[DragWaypoint]] = None


class MouseDragCmd(BaseModel):
    tool: Literal["mouse_drag"]
    args: MouseDragArgs


class MouseScrollArgs(BaseModel):
    x: int
    y: int
    amount: int  # positive = scroll up, negative = scroll down; unit: wheel notches


class MouseScrollCmd(BaseModel):
    tool: Literal["mouse_scroll"]
    args: MouseScrollArgs


class MouseDoubleClickArgs(BaseModel):
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"


class MouseDoubleClickCmd(BaseModel):
    tool: Literal["mouse_double_click"]
    args: MouseDoubleClickArgs


class MouseDownArgs(BaseModel):
    x: int
    y: int
    button: Literal["left", "right", "middle"] = "left"


class MouseDownCmd(BaseModel):
    tool: Literal["mouse_down"]
    args: MouseDownArgs


class MouseUpArgs(BaseModel):
    button: Literal["left", "right", "middle"] = "left"
    x: Optional[int] = None
    y: Optional[int] = None


class MouseUpCmd(BaseModel):
    tool: Literal["mouse_up"]
    args: MouseUpArgs


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

class KeyboardTypeArgs(BaseModel):
    text: str
    interval: float = 0.0


class KeyboardTypeCmd(BaseModel):
    tool: Literal["keyboard_type"]
    args: KeyboardTypeArgs


class KeyboardHotkeyArgs(BaseModel):
    keys: list[str]


class KeyboardHotkeyCmd(BaseModel):
    tool: Literal["keyboard_hotkey"]
    args: KeyboardHotkeyArgs


class KeyboardKeyArgs(BaseModel):
    key: str


class KeyboardKeydownCmd(BaseModel):
    tool: Literal["keydown"]
    args: KeyboardKeyArgs


class KeyboardKeyupCmd(BaseModel):
    tool: Literal["keyup"]
    args: KeyboardKeyArgs


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------

class FocusWindowArgs(BaseModel):
    hwnd: int


class FocusWindowCmd(BaseModel):
    tool: Literal["focus_window"]
    args: FocusWindowArgs


class SetWindowStateArgs(BaseModel):
    hwnd: int
    state: Literal["maximize", "minimize", "restore"]


class SetWindowStateCmd(BaseModel):
    tool: Literal["set_window_state"]
    args: SetWindowStateArgs


class ListWindowsCmd(BaseModel):
    tool: Literal["list_windows"]
    args: NoArgs = Field(default_factory=NoArgs)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------

class SetClipboardArgs(BaseModel):
    text: str


class SetClipboardCmd(BaseModel):
    tool: Literal["set_clipboard"]
    args: SetClipboardArgs


class GetClipboardCmd(BaseModel):
    tool: Literal["get_clipboard"]
    args: NoArgs = Field(default_factory=NoArgs)


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

class RunShellArgs(BaseModel):
    command: str
    shell: Literal["cmd", "powershell"] = "cmd"
    cwd: Optional[str] = None
    timeout: int = Field(30, ge=1, le=300)
    env_extra: Optional[dict[str, str]] = None


class RunShellCmd(BaseModel):
    tool: Literal["run_shell"]
    args: RunShellArgs


# ---------------------------------------------------------------------------
# Virtual Desktop
# ---------------------------------------------------------------------------

class DesktopIndexArgs(BaseModel):
    index: int = Field(..., ge=0)


class SwitchDesktopCmd(BaseModel):
    tool: Literal["switch_desktop"]
    args: DesktopIndexArgs


class DeleteDesktopCmd(BaseModel):
    tool: Literal["delete_desktop"]
    args: DesktopIndexArgs


class ListDesktopsCmd(BaseModel):
    tool: Literal["list_desktops"]
    args: NoArgs = Field(default_factory=NoArgs)


class CreateDesktopCmd(BaseModel):
    tool: Literal["create_desktop"]
    args: NoArgs = Field(default_factory=NoArgs)


# ---------------------------------------------------------------------------
# Discriminated Union over all 22 command types
# ---------------------------------------------------------------------------

Command = Annotated[
    Union[
        ScreenshotCmd,
        MouseMoveCmd,
        MouseClickCmd,
        MouseDragCmd,
        MouseScrollCmd,
        MouseDoubleClickCmd,
        MouseDownCmd,
        MouseUpCmd,
        KeyboardTypeCmd,
        KeyboardHotkeyCmd,
        KeyboardKeydownCmd,
        KeyboardKeyupCmd,
        ListWindowsCmd,
        FocusWindowCmd,
        SetWindowStateCmd,
        GetClipboardCmd,
        SetClipboardCmd,
        RunShellCmd,
        ListDesktopsCmd,
        SwitchDesktopCmd,
        CreateDesktopCmd,
        DeleteDesktopCmd,
    ],
    Field(discriminator="tool"),
]


# ---------------------------------------------------------------------------
# JSON command schema (passed to claude via --json-schema)
# ---------------------------------------------------------------------------

# Must stay in sync with the Command discriminated union above.
_VALID_TOOLS = [
    "screenshot",
    "mouse_move",
    "mouse_click",
    "mouse_drag",
    "mouse_scroll",
    "mouse_double_click",
    "mouse_down",
    "mouse_up",
    "keyboard_type",
    "keyboard_hotkey",
    "keydown",
    "keyup",
    "list_windows",
    "focus_window",
    "set_window_state",
    "get_clipboard",
    "set_clipboard",
    "run_shell",
    "list_desktops",
    "switch_desktop",
    "create_desktop",
    "delete_desktop",
]

COMMAND_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "protocol_version": {"type": "integer", "const": 1},
        "reasoning": {"type": "string", "maxLength": 2000},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "tool": {"type": "string", "enum": _VALID_TOOLS},
                    "args": {"type": "object"},
                },
                "required": ["tool", "args"],
            },
        },
        "done": {"type": "boolean"},
    },
    "required": ["protocol_version", "reasoning", "commands", "done"],
}


# ---------------------------------------------------------------------------
# ClaudeResponse — validated envelope returned by claude -p
# ---------------------------------------------------------------------------

class ClaudeResponse(BaseModel):
    protocol_version: int
    reasoning: str
    commands: list[Command] = Field(..., min_length=1)
    done: bool


# ---------------------------------------------------------------------------
# TaskDefinition — one entry in tasks.yaml
# ---------------------------------------------------------------------------

class TaskDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1)
    cron: str  # validated via croniter below
    model: str = "claude-sonnet-4-6"
    max_steps: int = Field(..., ge=1, le=100)
    step_timeout_seconds: int = Field(60, ge=5, le=300)
    notify_on_finish: bool = False
    prompt: str = Field(..., min_length=1)

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        from croniter import croniter  # local import keeps startup fast

        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v!r}")
        return v
