import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
  TextContent,
  ImageContent,
} from "@modelcontextprotocol/sdk/types.js";
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

// ── Config resolution ────────────────────────────────────────────────────────

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function resolveEngineUrl(): string {
  if (process.env.ENGINE_URL) {
    return process.env.ENGINE_URL;
  }
  try {
    // config.json lives one directory above src/
    const configPath = join(__dirname, "..", "config.json");
    const raw = readFileSync(configPath, "utf-8");
    const cfg = JSON.parse(raw) as { engine_url?: string };
    if (cfg.engine_url) return cfg.engine_url;
  } catch {
    // fall through to default
  }
  return "http://127.0.0.1:8765";
}

const ENGINE_URL = resolveEngineUrl().replace(/\/$/, "");
const ENGINE_SECRET = process.env.ENGINE_SECRET ?? "";

// ── Engine HTTP helpers ──────────────────────────────────────────────────────

interface EngineResponse<T = unknown> {
  success: boolean;
  data: T;
  error: string | null;
  timed_out: boolean;
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  if (ENGINE_SECRET) {
    headers["Authorization"] = `Bearer ${ENGINE_SECRET}`;
  }
  return headers;
}

async function engineGet<T>(path: string): Promise<EngineResponse<T>> {
  let res: Response;
  try {
    res = await fetch(`${ENGINE_URL}${path}`, {
      method: "GET",
      headers: authHeaders(),
    });
  } catch (err) {
    throw new Error(`Engine unreachable: ${(err as Error).message}`);
  }
  if (res.status === 401) {
    throw new Error("Engine returned 401 Unauthorized — check ENGINE_SECRET");
  }
  return (await res.json()) as EngineResponse<T>;
}

async function enginePost<T>(
  path: string,
  body: unknown
): Promise<EngineResponse<T>> {
  let res: Response;
  try {
    res = await fetch(`${ENGINE_URL}${path}`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new Error(`Engine unreachable: ${(err as Error).message}`);
  }
  if (res.status === 401) {
    throw new Error("Engine returned 401 Unauthorized — check ENGINE_SECRET");
  }
  return (await res.json()) as EngineResponse<T>;
}

async function engineDelete<T>(path: string): Promise<EngineResponse<T>> {
  let res: Response;
  try {
    res = await fetch(`${ENGINE_URL}${path}`, {
      method: "DELETE",
      headers: authHeaders(),
    });
  } catch (err) {
    throw new Error(`Engine unreachable: ${(err as Error).message}`);
  }
  if (res.status === 401) {
    throw new Error("Engine returned 401 Unauthorized — check ENGINE_SECRET");
  }
  return (await res.json()) as EngineResponse<T>;
}

// Return a standard MCP error content block
function errorContent(message: string): { content: TextContent[]; isError: true } {
  return {
    content: [{ type: "text", text: message }],
    isError: true,
  };
}

function textContent(text: string): { content: TextContent[] } {
  return { content: [{ type: "text", text }] };
}

// ── Coordinate remapping ────────────────────────────────────────────────────
// Screenshots are resized to fit within 1920×1080 for Claude-friendly token usage.
// imageScale converts image-pixel coords back to logical coords: logical = image / imageScale.

let imageScale = 1.0;

/** Map a coordinate from the (possibly resized) screenshot space to logical pixels. */
function toLogical(v: number): number {
  return imageScale === 1.0 ? v : Math.round(v / imageScale);
}

/** Map a coordinate from logical pixels back to screenshot image space. */
function toImage(v: number): number {
  return imageScale === 1.0 ? v : Math.round(v * imageScale);
}

/** Remap bounding_rect and center in element data from logical → image coords. */
function remapElementCoords(data: unknown): unknown {
  if (data === null || typeof data !== "object") return data;
  if (Array.isArray(data)) return (data as unknown[]).map(remapElementCoords);
  const obj = data as Record<string, unknown>;
  const result: Record<string, unknown> = { ...obj };
  if (obj.center !== null && typeof obj.center === "object") {
    const c = obj.center as { x: number; y: number };
    result.center = { x: toImage(c.x), y: toImage(c.y) };
  }
  if (obj.bounding_rect !== null && typeof obj.bounding_rect === "object") {
    const r = obj.bounding_rect as { left: number; top: number; right: number; bottom: number };
    result.bounding_rect = {
      left: toImage(r.left),
      top: toImage(r.top),
      right: toImage(r.right),
      bottom: toImage(r.bottom),
    };
  }
  return result;
}

/** Initialize imageScale at startup from /screen-info. Non-fatal on failure.
 *  Uses a manual fetch with AbortController timeout instead of engineGet()
 *  because engineGet() does not support per-call timeouts. */
async function initImageScale(): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 3000);
  try {
    const res = await fetch(`${ENGINE_URL}/screen-info`, {
      method: "GET",
      headers: authHeaders(),
      signal: controller.signal,
    });
    clearTimeout(timer);
    const body = (await res.json()) as EngineResponse<{ logical_width: number; logical_height: number }>;
    if (body.success) {
      const { logical_width: lw, logical_height: lh } = body.data;
      if (lw > 1920 || lh > 1080) {
        imageScale = Math.min(1920 / lw, 1080 / lh);
      }
      console.error(`imageScale initialized: ${imageScale.toFixed(4)} (screen ${lw}×${lh} logical)`);
    } else {
      console.error(`WARNING: initImageScale — engine returned success=false: ${body.error ?? "unknown"}`);
    }
  } catch (err) {
    clearTimeout(timer);
    const msg = (err as Error).name === "AbortError"
      ? "timed out after 3s"
      : (err as Error).message;
    console.error(`WARNING: initImageScale failed (${msg}) — imageScale stays 1.0`);
  }
}

// ── Tool definitions ─────────────────────────────────────────────────────────

const COORD_NOTE =
  "All coordinates are in screenshot pixels (mapped automatically to logical pixels).";

const TOOLS: Tool[] = [
  {
    name: "screenshot",
    description: `Capture a screenshot of the screen or a region. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        top: { type: "number", description: "Top edge of capture region in screenshot pixels" },
        left: { type: "number", description: "Left edge of capture region in screenshot pixels" },
        width: { type: "number", description: "Width of capture region in screenshot pixels" },
        height: { type: "number", description: "Height of capture region in screenshot pixels" },
      },
    },
  },
  {
    name: "mouse_move",
    description: `Move the mouse cursor to (x, y). ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
      },
      required: ["x", "y"],
    },
  },
  {
    name: "mouse_click",
    description: `Click the mouse at (x, y). ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        button: {
          type: "string",
          enum: ["left", "right", "middle"],
          description: "Mouse button to click (default: left)",
        },
      },
      required: ["x", "y"],
    },
  },
  {
    name: "mouse_double_click",
    description: `Double-click the mouse at (x, y). Use instead of two sequential mouse_click calls to avoid OS double-click threshold issues. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        button: {
          type: "string",
          enum: ["left", "right", "middle"],
          description: "Mouse button to double-click (default: left)",
        },
      },
      required: ["x", "y"],
    },
  },
  {
    name: "mouse_drag",
    description: `Drag the mouse from (x1, y1) to (x2, y2), optionally via intermediate waypoints. Use waypoints when the path is not a straight line (L-shaped, Z-shaped, curved brush strokes, obstacle avoidance). For simple A→B drags omit waypoints. Supports cross-window OLE drag-and-drop. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x1: { type: "number", description: "Start X coordinate in screenshot pixels" },
        y1: { type: "number", description: "Start Y coordinate in screenshot pixels" },
        x2: { type: "number", description: "End X coordinate in screenshot pixels" },
        y2: { type: "number", description: "End Y coordinate in screenshot pixels" },
        button: {
          type: "string",
          enum: ["left", "right", "middle"],
          description: "Mouse button to use for dragging (default: left)",
        },
        duration: { type: "number", description: "Total drag movement time in seconds (default 0.5)" },
        hold_before: { type: "number", description: "Delay after mouseDown before moving, for DnD init (default 0.2)" },
        steps: { type: "integer", description: "Number of interpolation steps total across all segments (default 20)" },
        waypoints: {
          type: "array",
          items: {
            type: "object",
            properties: {
              x: { type: "number", description: "X coordinate in screenshot pixels" },
              y: { type: "number", description: "Y coordinate in screenshot pixels" },
            },
            required: ["x", "y"],
          },
          description: "Optional intermediate waypoints in screenshot pixels. Mouse follows (x1,y1) → waypoints[0] → ... → (x2,y2) without releasing the button. Empty array treated same as omitting.",
        },
      },
      required: ["x1", "y1", "x2", "y2"],
    },
  },
  {
    name: "mouse_down",
    description: `Press and hold a mouse button at (x, y) without releasing. Use only when you need to interleave other operations while the button is held (e.g. screenshot to verify state, keyboard_type, mouse_move to multiple stops). For continuous A→B drags use mouse_drag; for multi-segment paths use mouse_drag with waypoints. Always follow with mouse_up to release. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        button: {
          type: "string",
          enum: ["left", "right", "middle"],
          description: "Mouse button to press and hold (default: left)",
        },
      },
      required: ["x", "y"],
    },
  },
  {
    name: "mouse_up",
    description: `Release a held mouse button. Always pair with mouse_down. If x and y are both provided, mouse moves to that position before releasing (x and y must be provided together — one without the other is invalid). If neither x nor y is given, releases at the current cursor position. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate to move to before releasing (optional, must be paired with y)" },
        y: { type: "number", description: "Y coordinate to move to before releasing (optional, must be paired with x)" },
        button: {
          type: "string",
          enum: ["left", "right", "middle"],
          description: "Mouse button to release (default: left)",
        },
      },
      required: [],
    },
  },
  {
    name: "mouse_scroll",
    description: `Scroll the mouse wheel at (x, y). Positive amount scrolls up, negative scrolls down. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        amount: {
          type: "number",
          description: "Scroll amount (positive = up, negative = down)",
        },
      },
      required: ["x", "y", "amount"],
    },
  },
  {
    name: "keyboard_type",
    description: "Type text using the keyboard.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Text to type" },
        interval: {
          type: "number",
          description: "Seconds between keystrokes (optional)",
        },
      },
      required: ["text"],
    },
  },
  {
    name: "keyboard_hotkey",
    description: 'Press a keyboard hotkey combination, e.g. ["ctrl", "c"] for copy.',
    inputSchema: {
      type: "object",
      properties: {
        keys: {
          type: "array",
          items: { type: "string" },
          description: 'Array of key names to press simultaneously, e.g. ["ctrl", "shift", "esc"]',
        },
      },
      required: ["keys"],
    },
  },
  {
    name: "keydown",
    description: "Press and hold a key.",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "Key name to press and hold" },
      },
      required: ["key"],
    },
  },
  {
    name: "keyup",
    description: "Release a held key.",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "Key name to release" },
      },
      required: ["key"],
    },
  },
  {
    name: "list_windows",
    description: "List all visible windows with their handle (hwnd), title, and PID.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "focus_window",
    description: "Bring a window to the foreground by its window handle (hwnd).",
    inputSchema: {
      type: "object",
      properties: {
        hwnd: { type: "integer", description: "Window handle (hwnd) as an integer" },
      },
      required: ["hwnd"],
    },
  },
  {
    name: "set_window_state",
    description: "Maximize, minimize, or restore a window by its handle (hwnd).",
    inputSchema: {
      type: "object",
      properties: {
        hwnd: { type: "integer", description: "Window handle (hwnd) as an integer" },
        state: {
          type: "string",
          enum: ["maximize", "minimize", "restore"],
          description: "Desired window state",
        },
      },
      required: ["hwnd", "state"],
    },
  },
  {
    name: "get_clipboard",
    description: "Get the current text content of the clipboard.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "set_clipboard",
    description: "Set the clipboard to the given text.",
    inputSchema: {
      type: "object",
      properties: {
        text: { type: "string", description: "Text to place on the clipboard" },
      },
      required: ["text"],
    },
  },
  {
    name: "run_shell",
    description:
      "Run a shell command and return its output. Timeout is 1–300 seconds (default 30).",
    inputSchema: {
      type: "object",
      properties: {
        command: { type: "string", description: "Command to execute" },
        shell: {
          type: "string",
          enum: ["cmd", "powershell"],
          description: "Shell to use (cmd or powershell)",
        },
        cwd: { type: "string", description: "Working directory for the command" },
        timeout: {
          type: "number",
          description: "Timeout in seconds (1–300, default 30)",
        },
      },
      required: ["command"],
    },
  },
  {
    name: "list_desktops",
    description: "List all virtual desktops with their index and name.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "switch_desktop",
    description: "Switch to a virtual desktop by its index.",
    inputSchema: {
      type: "object",
      properties: {
        index: { type: "integer", description: "Zero-based desktop index" },
      },
      required: ["index"],
    },
  },
  {
    name: "create_desktop",
    description: "Create a new virtual desktop.",
    inputSchema: {
      type: "object",
      properties: {},
    },
  },
  {
    name: "delete_desktop",
    description: "Delete a virtual desktop by its index.",
    inputSchema: {
      type: "object",
      properties: {
        index: { type: "integer", description: "Zero-based desktop index to delete" },
      },
      required: ["index"],
    },
  },
  {
    name: "find_element",
    description:
      "Search for UI elements by name, type, or automation ID within a window. " +
      "Use this for precise coordinates of small, dense, or hard-to-distinguish elements " +
      "(list items, menu entries, toolbar buttons). Use list_windows first to get the window " +
      "handle, or pass a title substring. Prefer screenshot + visual coordinate picking for " +
      "large, obvious targets. Returned element center and bounding_rect are in screenshot pixels. " +
      `Take a screenshot first to ensure coordinate alignment. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        hwnd: { type: "integer", description: "Window handle from list_windows" },
        title: { type: "string", description: "Window title substring (alternative to hwnd)" },
        name: { type: "string", description: "Element name to search for (partial match, case-insensitive)" },
        control_type: {
          type: "string",
          description: "UI Automation control type, e.g. Button, ListItem, Edit, CheckBox, MenuItem",
        },
        automation_id: { type: "string", description: "Automation ID (exact match)" },
        max_depth: { type: "integer", description: "Search depth limit (default 5, max 20)" },
        max_results: { type: "integer", description: "Maximum elements to return (default 20, max 100)" },
        timeout: { type: "number", description: "Search timeout in seconds (default 5.0, max 30)" },
      },
    },
  },
  {
    name: "element_at",
    description:
      "Identify the UI element at a given screenshot coordinate. Use after taking a screenshot " +
      `to understand what a specific UI element is before clicking it. Returned center and bounding_rect are also in screenshot pixels. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
      },
      required: ["x", "y"],
    },
  },
];

// ── Tool handler ─────────────────────────────────────────────────────────────

type Args = Record<string, unknown>;

async function handleTool(
  name: string,
  args: Args
): Promise<
  | { content: (TextContent | ImageContent)[]; isError?: boolean }
> {
  try {
    switch (name) {
      // ── Screenshot ──────────────────────────────────────────────────────
      case "screenshot": {
        // Match engine semantics: full_screen = (width is None and height is None)
        const isCrop = args.width !== undefined && args.height !== undefined;
        const body: Record<string, number> = {};
        if (args.top !== undefined) body.top = toLogical(args.top as number);
        if (args.left !== undefined) body.left = toLogical(args.left as number);
        if (args.width !== undefined) body.width = toLogical(args.width as number);
        if (args.height !== undefined) body.height = toLogical(args.height as number);

        const resp = await enginePost<{
          image: string;
          dpi_scale: number;
          image_scale: number;
          logical_size: [number, number];
          image_size: [number, number];
          physical_size: [number, number];
          virtual_origin: unknown;
        }>("/screenshot", body);
        if (!resp.success) {
          return errorContent(`screenshot failed: ${resp.error ?? "unknown error"}`);
        }

        // Only update imageScale from full-screen shots — cropped shots return scale=1.0 for the crop region
        if (!isCrop) {
          imageScale = resp.data.image_scale;
        }

        const [lw, lh] = resp.data.logical_size;
        const [iw, ih] = resp.data.image_size;
        const [pw, ph] = resp.data.physical_size;
        const meta =
          `Screenshot: ${iw}\u00d7${ih} image pixels (logical ${lw}\u00d7${lh}, physical ${pw}\u00d7${ph}, ` +
          `DPI scale ${resp.data.dpi_scale}, image scale ${resp.data.image_scale.toFixed(4)}). ` +
          `Use coordinates from this image directly — they are remapped automatically.`;

        const metaContent: TextContent = { type: "text", text: meta };
        const imageContent: ImageContent = {
          type: "image",
          data: resp.data.image,
          mimeType: "image/png",
        };
        return { content: [metaContent, imageContent] };
      }

      // ── Mouse ────────────────────────────────────────────────────────────
      case "mouse_move": {
        const resp = await enginePost("/mouse", {
          action: "move",
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
        });
        if (!resp.success) return errorContent(`mouse_move failed: ${resp.error}`);
        return textContent("Mouse moved.");
      }

      case "mouse_click": {
        const resp = await enginePost("/mouse", {
          action: "click",
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
          button: args.button ?? "left",
        });
        if (!resp.success) return errorContent(`mouse_click failed: ${resp.error}`);
        return textContent("Mouse clicked.");
      }

      case "mouse_double_click": {
        const resp = await enginePost("/mouse", {
          action: "double_click",
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
          button: args.button ?? "left",
        });
        if (!resp.success) return errorContent(`mouse_double_click failed: ${resp.error}`);
        return textContent("Mouse double-clicked.");
      }

      case "mouse_drag": {
        const body: Record<string, unknown> = {
          action: "drag",
          x: toLogical(args.x1 as number),
          y: toLogical(args.y1 as number),
          x2: toLogical(args.x2 as number),
          y2: toLogical(args.y2 as number),
        };
        if (args.button !== undefined) body.button = args.button;
        if (args.duration !== undefined) body.duration = args.duration;
        if (args.hold_before !== undefined) body.hold_before = args.hold_before;
        if (args.steps !== undefined) body.steps = args.steps;
        if (args.waypoints !== undefined) {
          body.waypoints = (args.waypoints as Array<{ x: number; y: number }>).map(wp => ({
            x: toLogical(wp.x),
            y: toLogical(wp.y),
          }));
        }
        const resp = await enginePost("/mouse", body);
        if (!resp.success) return errorContent(`mouse_drag failed: ${resp.error}`);
        return textContent("Mouse dragged.");
      }

      case "mouse_down": {
        const resp = await enginePost("/mouse", {
          action: "mousedown",
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
          button: args.button ?? "left",
        });
        if (!resp.success) return errorContent(`mouse_down failed: ${resp.error}`);
        return textContent("Mouse button pressed.");
      }

      case "mouse_up": {
        const body: Record<string, unknown> = {
          action: "mouseup",
          button: args.button ?? "left",
        };
        if (args.x !== undefined && args.y !== undefined) {
          body.x = toLogical(args.x as number);
          body.y = toLogical(args.y as number);
        }
        const resp = await enginePost("/mouse", body);
        if (!resp.success) return errorContent(`mouse_up failed: ${resp.error}`);
        return textContent("Mouse button released.");
      }

      case "mouse_scroll": {
        const resp = await enginePost("/mouse", {
          action: "scroll",
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
          amount: args.amount,
        });
        if (!resp.success) return errorContent(`mouse_scroll failed: ${resp.error}`);
        return textContent("Mouse scrolled.");
      }

      // ── Keyboard ─────────────────────────────────────────────────────────
      case "keyboard_type": {
        const body: Record<string, unknown> = {
          action: "type",
          text: args.text,
        };
        if (args.interval !== undefined) body.interval = args.interval;
        const resp = await enginePost("/keyboard", body);
        if (!resp.success) return errorContent(`keyboard_type failed: ${resp.error}`);
        return textContent("Text typed.");
      }

      case "keyboard_hotkey": {
        const resp = await enginePost("/keyboard", {
          action: "hotkey",
          keys: args.keys,
        });
        if (!resp.success) return errorContent(`keyboard_hotkey failed: ${resp.error}`);
        return textContent("Hotkey sent.");
      }

      case "keydown": {
        const resp = await enginePost("/keyboard", {
          action: "keydown",
          key: args.key,
        });
        if (!resp.success) return errorContent(`keydown failed: ${resp.error}`);
        return textContent(`Key down: ${String(args.key)}`);
      }

      case "keyup": {
        const resp = await enginePost("/keyboard", {
          action: "keyup",
          key: args.key,
        });
        if (!resp.success) return errorContent(`keyup failed: ${resp.error}`);
        return textContent(`Key up: ${String(args.key)}`);
      }

      // ── Windows ──────────────────────────────────────────────────────────
      case "list_windows": {
        const resp = await engineGet<Array<{ hwnd: number; title: string; pid: number }>>(
          "/windows"
        );
        if (!resp.success) return errorContent(`list_windows failed: ${resp.error}`);
        return textContent(JSON.stringify(resp.data, null, 2));
      }

      case "focus_window": {
        const resp = await enginePost("/window/focus", { hwnd: args.hwnd });
        if (!resp.success) return errorContent(`focus_window failed: ${resp.error}`);
        return textContent(`Window ${String(args.hwnd)} focused.`);
      }

      case "set_window_state": {
        const resp = await enginePost("/window/state", {
          hwnd: args.hwnd,
          state: args.state,
        });
        if (!resp.success) return errorContent(`set_window_state failed: ${resp.error}`);
        return textContent(`Window ${String(args.hwnd)} state set to ${String(args.state)}.`);
      }

      // ── Clipboard ────────────────────────────────────────────────────────
      case "get_clipboard": {
        const resp = await engineGet<{ text: string }>("/clipboard");
        if (!resp.success) return errorContent(`get_clipboard failed: ${resp.error}`);
        return textContent((resp.data as { text: string }).text ?? JSON.stringify(resp.data));
      }

      case "set_clipboard": {
        const resp = await enginePost("/clipboard", { text: args.text });
        if (!resp.success) return errorContent(`set_clipboard failed: ${resp.error}`);
        return textContent("Clipboard updated.");
      }

      // ── Shell ────────────────────────────────────────────────────────────
      case "run_shell": {
        const body: Record<string, unknown> = { command: args.command };
        if (args.shell !== undefined) body.shell = args.shell;
        if (args.cwd !== undefined) body.cwd = args.cwd;
        if (args.timeout !== undefined) body.timeout = args.timeout;
        const resp = await enginePost<unknown>("/shell", body);
        if (!resp.success) return errorContent(`run_shell failed: ${resp.error}`);
        return textContent(
          typeof resp.data === "string" ? resp.data : JSON.stringify(resp.data, null, 2)
        );
      }

      // ── Virtual Desktops ─────────────────────────────────────────────────
      case "list_desktops": {
        const resp = await engineGet<Array<{ index: number; name: string }>>("/desktops");
        if (!resp.success) return errorContent(`list_desktops failed: ${resp.error}`);
        return textContent(JSON.stringify(resp.data, null, 2));
      }

      case "switch_desktop": {
        const resp = await enginePost("/desktop/switch", { index: args.index });
        if (!resp.success) return errorContent(`switch_desktop failed: ${resp.error}`);
        return textContent(`Switched to desktop ${String(args.index)}.`);
      }

      case "create_desktop": {
        const resp = await enginePost<{ index: number }>("/desktop/create", {});
        if (!resp.success) return errorContent(`create_desktop failed: ${resp.error}`);
        return textContent(`Created desktop at index ${String(resp.data?.index ?? "unknown")}.`);
      }

      case "delete_desktop": {
        const resp = await engineDelete<unknown>(`/desktop/${String(args.index)}`);
        if (!resp.success) return errorContent(`delete_desktop failed: ${resp.error}`);
        return textContent(`Desktop ${String(args.index)} deleted.`);
      }

      // ── UI Automation ───────────────────────────────────────────────────
      case "find_element": {
        const body: Record<string, unknown> = {};
        if (args.hwnd !== undefined) body.hwnd = args.hwnd;
        if (args.title !== undefined) body.title = args.title;
        if (args.name !== undefined) body.name = args.name;
        if (args.control_type !== undefined) body.control_type = args.control_type;
        if (args.automation_id !== undefined) body.automation_id = args.automation_id;
        if (args.max_depth !== undefined) body.max_depth = args.max_depth;
        if (args.max_results !== undefined) body.max_results = args.max_results;
        if (args.timeout !== undefined) body.timeout = args.timeout;
        const resp = await enginePost<unknown>("/find_element", body);
        if (!resp.success) return errorContent(`find_element failed: ${resp.error}`);
        return textContent(JSON.stringify(remapElementCoords(resp.data), null, 2));
      }

      case "element_at": {
        const resp = await enginePost<unknown>("/element_at", {
          x: toLogical(args.x as number),
          y: toLogical(args.y as number),
        });
        if (!resp.success) return errorContent(`element_at failed: ${resp.error}`);
        if (resp.data === null) return textContent(resp.error ?? "No element found at this coordinate.");
        return textContent(JSON.stringify(remapElementCoords(resp.data), null, 2));
      }

      default:
        return errorContent(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return errorContent((err as Error).message);
  }
}

// ── MCP Server setup ─────────────────────────────────────────────────────────

const server = new Server(
  { name: "win-computer-use", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: TOOLS,
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  return handleTool(name, (args ?? {}) as Args);
});

// ── Entry point ──────────────────────────────────────────────────────────────

if (!ENGINE_SECRET) {
  console.error("WARNING: ENGINE_SECRET is not set. All requests to the engine will fail with 401.");
}

await initImageScale();
const transport = new StdioServerTransport();
await server.connect(transport);
