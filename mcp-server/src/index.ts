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
import { spawn } from "child_process";
import {
  registerScreenshot,
  resolveCoords,
  resolveDim,
  lookupTransform,
  type ScreenshotTransform,
} from "./registry.js";

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
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Engine returned HTTP ${res.status}: ${text}`);
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
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Engine returned HTTP ${res.status}: ${text}`);
  }
  return (await res.json()) as EngineResponse<T>;
}

// ── P2 helpers ───────────────────────────────────────────────────────────────

interface ZoomData {
  image: string;
  dpi_scale: number;
  image_scale: number;
  logical_size: [number, number];
  image_size: [number, number];
  physical_size: [number, number];
  virtual_origin: { x: number; y: number };
}

async function fetchZoomRaw(logicalX: number, logicalY: number): Promise<EngineResponse<ZoomData>> {
  return enginePost<ZoomData>("/screenshot/zoom", {
    x: logicalX,
    y: logicalY,
    width: 160,
    height: 80,
    annotate: true,
  });
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
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Engine returned HTTP ${res.status}: ${text}`);
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

// ── Arg coercion ─────────────────────────────────────────────────────────────
// MCP tool call arguments arrive as `unknown`. TypeScript's `as number` is a
// compile-time assertion only — it does NOT convert a string at runtime.
// Claude occasionally sends numeric coordinates as JSON strings (e.g. "200"
// instead of 200), which causes `"200" + offset` to concatenate instead of add.
// numArg() converts to a JS number so all downstream math stays numeric.
function numArg(v: unknown): number {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
}

// ── Coordinate remapping ────────────────────────────────────────────────────
// Coordinates in tool arguments are in "screenshot image pixels" of the referenced
// screenshot. The MCP layer uses the screenshot registry (resolveCoords / resolveDim)
// to convert them to logical pixels before sending to the engine.


/**
 * Remap bounding_rect and center in element data from logical → image pixels.
 * When transform is provided, converts logical coords to image pixels of that screenshot.
 * When undefined (find_element called without screenshot_id), returns logical coords as-is.
 */
function remapElementCoords(data: unknown, t?: ScreenshotTransform): unknown {
  if (data === null || typeof data !== "object") return data;
  if (Array.isArray(data)) return (data as unknown[]).map(item => remapElementCoords(item, t));
  const obj = data as Record<string, unknown>;
  const result: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(obj)) {
    if (key === "center" && val !== null && typeof val === "object" && !Array.isArray(val)) {
      const c = val as { x: number; y: number };
      if (t) {
        result.center = {
          x: t.scale === 1.0 ? c.x - t.originX : Math.round((c.x - t.originX) * t.scale),
          y: t.scale === 1.0 ? c.y - t.originY : Math.round((c.y - t.originY) * t.scale),
        };
      } else {
        result.center = c; // no transform: return logical coords as-is
      }
    } else if (key === "bounding_rect" && val !== null && typeof val === "object" && !Array.isArray(val)) {
      const r = val as { left: number; top: number; right: number; bottom: number };
      if (t) {
        result.bounding_rect = {
          left:   t.scale === 1.0 ? r.left   - t.originX : Math.round((r.left   - t.originX) * t.scale),
          top:    t.scale === 1.0 ? r.top    - t.originY : Math.round((r.top    - t.originY) * t.scale),
          right:  t.scale === 1.0 ? r.right  - t.originX : Math.round((r.right  - t.originX) * t.scale),
          bottom: t.scale === 1.0 ? r.bottom - t.originY : Math.round((r.bottom - t.originY) * t.scale),
        };
      } else {
        result.bounding_rect = r; // no transform: return logical coords as-is
      }
    } else {
      result[key] = remapElementCoords(val, t);
    }
  }
  return result;
}


// ── Tool definitions ─────────────────────────────────────────────────────────

const COORD_NOTE =
  "All coordinates are in screenshot image pixels of the referenced screenshot. " +
  "REQUIRED: pass the screenshot_id returned by the screenshot tool that produced the image " +
  "you are reading coordinates from. Using the wrong screenshot_id will cause the click to " +
  "land in the wrong place.";

export const TOOLS: Tool[] = [
  {
    name: "screenshot",
    description:
      "Capture a screenshot of the screen or a region. " +
      "If you haven't seen the screen recently, take a full-screen screenshot to orient yourself, " +
      "then take crop screenshots of the specific region you need to interact with — crops provide " +
      "higher effective resolution and reduce token usage. " +
      "Omit all parameters for full-screen. To crop, provide top, left, width, and height together " +
      "(width and height must both be provided or both omitted; providing one without the other returns an error). " +
      "(top/left without width/height are ignored and result in a full-screen capture). " +
      "Coordinates from the most recent screenshot (whether full-screen or crop) can be passed " +
      "directly to mouse and keyboard tools; the MCP layer remaps them automatically. " +
      COORD_NOTE,
    inputSchema: {
      type: "object",
      properties: {
        top:    { type: "number", description: "Top edge of capture region in screenshot pixels (requires width+height)" },
        left:   { type: "number", description: "Left edge of capture region in screenshot pixels (requires width+height)" },
        width:  { type: "number", description: "Width of capture region in screenshot pixels (must be paired with height)" },
        height: { type: "number", description: "Height of capture region in screenshot pixels (must be paired with width)" },
        screenshot_id: {
          type: "string",
          description: "Required when taking a crop (width+height provided). ID of the screenshot from which left/top/width/height were read.",
        },
      },
    },
  },
  {
    name: "screenshot_zoom",
    description:
      "Crop a close-up screenshot region centered at (x, y). No upscaling — returns\n" +
      "the cropped area at its original screen resolution.\n\n" +
      "This tool returns a new screenshot_id. Use coordinates read from THIS zoom image\n" +
      "together with THIS screenshot_id for subsequent mouse/keyboard actions.\n" +
      "To return to full-screen coordinates, take a new full-screen screenshot.\n\n" +
      "Use after element_at returns no useful element — game UI, images, canvas,\n" +
      "web pages, map coordinates. Gives a close-up pixel-level view of a single target.\n\n" +
      "Use width/height to control the crop area (default 200×200 screenshot pixels).\n" +
      "annotate is true by default and draws a small filled dot (not a crosshair) at the exact center point.\n\n" +
      "If the crop region extends beyond the screen edge, the out-of-screen portion\n" +
      "appears as black pixels — this is expected and does not indicate a wrong coordinate.\n\n" +
      "Prefer screenshot_annotate when you need to see where a coordinate sits in\n" +
      "full-screen context, or when verifying multiple coordinates at once.",
    inputSchema: {
      type: "object",
      properties: {
        x:        { type: "number",  description: "Center X coordinate in screenshot pixels" },
        y:        { type: "number",  description: "Center Y coordinate in screenshot pixels" },
        width:    { type: "number",  description: "Crop width in screenshot pixels (default 200)" },
        height:   { type: "number",  description: "Crop height in screenshot pixels (default 200)" },
        annotate: { type: "boolean", description: "Draw filled dot at center (default true)" },
        screenshot_id: {
          type: "string",
          description: "ID of the screenshot from which x,y coordinates were read.",
        },
      },
      required: ["x", "y", "screenshot_id"],
    },
  },
  {
    name: "screenshot_annotate",
    description:
      "VERIFICATION ONLY — never use this image as a coordinate source.\n\n" +
      "Draws markers on a screenshot to confirm where coordinates will land. " +
      "The annotated image is for human/model visual inspection only — " +
      "DO NOT read pixel coordinates from it and pass them to mouse/keyboard tools.\n\n" +
      "ALL actionable coordinates must come from screenshot or screenshot_zoom. " +
      "The screenshot_id returned by this tool is the same as the one you passed in; " +
      "it refers to the original screenshot's coordinate space, not this annotated image.\n\n" +
      "Best for: verifying multiple coordinates at once, or when you need to see\n" +
      "where a point falls within the full-screen context. Prefer screenshot_zoom\n" +
      "for isolated single-target close-up verification.\n\n" +
      "Marker types (use \"crosshair\" by default for precision):\n" +
      "- \"crosshair\": full-width + full-height lines with hollow circle gap at center (sniper-scope style)\n" +
      "- \"circle\": hollow circle only — use if crosshair lines obscure important content\n" +
      "- \"both\": crosshair + circle — use when extra visual confirmation is needed\n\n" +
      "Supports optional crop region (same as screenshot tool).\n" +
      "If you use a crop with this tool, the returned image's pixel coordinates are NOT usable\n" +
      "for mouse actions — take a regular screenshot crop of the same region if you need\n" +
      "actionable coordinates.\n\n" +
      "Returns skipped_annotations with the indices of any annotations outside the image bounds.\n" +
      "If skipped_annotations is non-empty, those coordinates were off-screen or outside the\n" +
      "crop region — re-check the coordinates before acting on them.",
    inputSchema: {
      type: "object",
      properties: {
        annotations: {
          type: "array",
          items: {
            type: "object",
            properties: {
              x:           { type: "number", description: "X coordinate in screenshot pixels" },
              y:           { type: "number", description: "Y coordinate in screenshot pixels" },
              marker_type: { type: "string", enum: ["crosshair", "circle", "both"], description: "Marker type (default: crosshair)" },
              color:       { type: "string", description: "PIL named color or hex (default: red)" },
              radius:      { type: "number", description: "Gap/circle radius in image pixels (default: 10)" },
            },
            required: ["x", "y"],
          },
          description: "Array of annotation markers to draw",
        },
        top:    { type: "number", description: "Top edge of crop region in screenshot pixels" },
        left:   { type: "number", description: "Left edge of crop region in screenshot pixels" },
        width:  { type: "number", description: "Width of crop region in screenshot pixels" },
        height: { type: "number", description: "Height of crop region in screenshot pixels" },
        screenshot_id: {
          type: "string",
          description: "ID of the screenshot from which annotation coordinates were read.",
        },
      },
      required: ["annotations", "screenshot_id"],
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
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "screenshot_id"],
    },
  },
  {
    name: "mouse_click",
    description: `Click the mouse at (x, y). ${COORD_NOTE}\n\nREQUIRED BEFORE clicking: (1) use screenshot_zoom to inspect the target area up close, (2) use screenshot_annotate to confirm the coordinate lands on the intended element. Do NOT estimate visually.`,
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
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "screenshot_id"],
    },
  },
  {
    name: "mouse_double_click",
    description: `Double-click the mouse at (x, y). Use instead of two sequential mouse_click calls to avoid OS double-click threshold issues. ${COORD_NOTE}\n\nREQUIRED BEFORE double-clicking: (1) use screenshot_zoom to inspect the target area up close, (2) use screenshot_annotate to confirm the coordinate lands on the intended element. Do NOT estimate visually.`,
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
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "screenshot_id"],
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
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are reading drag coordinates from. Required.",
        },
      },
      required: ["x1", "y1", "x2", "y2", "screenshot_id"],
    },
  },
  {
    name: "mouse_down",
    description: `Press and hold a mouse button at (x, y) without releasing. Use only when you need to interleave other operations while the button is held (e.g. screenshot to verify state, keyboard_type, mouse_move to multiple stops). For continuous A→B drags use mouse_drag; for multi-segment paths use mouse_drag with waypoints. CRITICAL: if you do not call mouse_up, the button remains held indefinitely and all subsequent mouse operations will behave as drags. Always follow with mouse_up to release. ${COORD_NOTE}`,
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
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "screenshot_id"],
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
        screenshot_id: {
          type: "string",
          description: "Required when x and y are provided. ID of the screenshot from which the coordinates were read.",
        },
      },
      required: [],
      // NOTE: JSON Schema cannot express conditional required (x+y+screenshot_id together-or-none).
      // The description above documents the contract; the handler enforces it at runtime.
    },
  },
  {
    name: "mouse_scroll",
    description: `Scroll the mouse wheel at (x, y). amount is in wheel notches (integer): positive scrolls up, negative scrolls down. 3 notches ≈ one page in most apps. ${COORD_NOTE}`,
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        amount: {
          type: "integer",
          description: "Scroll wheel notches (integer): positive = up, negative = down",
        },
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "amount", "screenshot_id"],
    },
  },
  {
    name: "keyboard_type",
    description:
      "Type literal text characters using the keyboard. " +
      "Text is sent to the currently focused window/control — click on the target input field first " +
      "to ensure it has focus before calling this tool. " +
      "ASCII characters are typed as individual keystrokes; non-ASCII characters (accented letters, CJK, emoji) " +
      "are automatically pasted via the clipboard (WARNING: this overwrites the current clipboard contents). " +
      "For special keys (Enter, Tab, Escape, arrows) or modifier combos (Ctrl+C), use keyboard_hotkey instead — " +
      "this tool types literal characters only, not key names.",
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
    description:
      'Press a keyboard hotkey combination, e.g. ["ctrl", "c"] for copy. ' +
      'Also use for single special keys: ["enter"], ["tab"], ["escape"], ["backspace"], ["delete"], arrow keys, etc. ' +
      'Key names follow pyautogui conventions: ctrl, alt, shift, win, enter, tab, escape, backspace, delete, space, up, down, left, right, f1-f12, home, end, pageup, pagedown, insert, etc.',
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
    description:
      "Press and hold a key without releasing. Key names follow pyautogui conventions " +
      "(e.g., 'shift', 'ctrl', 'alt', 'win'). Always follow with keyup for the same key to avoid " +
      "leaving keys stuck. Use keyboard_hotkey for simple combos — keydown/keyup is only needed when " +
      "you must hold a key across multiple other operations (e.g., Shift+click on several items). " +
      "Also available as `keyboard_keydown`.",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "Key name to press and hold (e.g., 'shift', 'ctrl', 'alt')" },
      },
      required: ["key"],
    },
  },
  {
    name: "keyup",
    description:
      "Release a held key. Must always be paired with a prior keydown call for the same key. " +
      "Also available as `keyboard_keyup`.",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "Key name to release" },
      },
      required: ["key"],
    },
  },
  {
    name: "keyboard_keydown",
    description:
      "Alias for `keydown`. Press and hold a key without releasing. Key names follow pyautogui conventions " +
      "(e.g., 'shift', 'ctrl', 'alt', 'win'). Always follow with keyboard_keyup for the same key to avoid " +
      "leaving keys stuck. Use keyboard_hotkey for simple combos — keyboard_keydown/keyboard_keyup is only needed when " +
      "you must hold a key across multiple other operations (e.g., Shift+click on several items).",
    inputSchema: {
      type: "object",
      properties: {
        key: { type: "string", description: "Key name to press and hold (e.g., 'shift', 'ctrl', 'alt')" },
      },
      required: ["key"],
    },
  },
  {
    name: "keyboard_keyup",
    description:
      "Alias for `keyup`. Release a held key. Must always be paired with a prior keyboard_keydown call for the same key.",
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
      "Run a shell command and return its output. Default shell is cmd; pass shell='powershell' " +
      "for PowerShell commands. Use PowerShell for complex tasks (JSON parsing, regex, object pipelines). " +
      "Timeout is 1–300 seconds (default 30). Output is returned in full — pipe to findstr (cmd) or " +
      "Select-String (powershell) to filter large outputs. " +
      "Large outputs may be truncated by the AI context window. Filter or limit results for commands likely to produce extensive output.",
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
        env_extra: {
          type: "object",
          additionalProperties: { type: "string" },
          description: "Extra environment variables to set for the command (keys must be UPPER_CASE, some system keys are blocked)",
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
      "REQUIRED: at least one target (hwnd or title) AND at least one filter (name, control_type, " +
      "or automation_id) — omitting both will return an error. " +
      "Use this for precise coordinates of small, dense, or hard-to-distinguish " +
      "elements (list items, menu entries, toolbar buttons). Use list_windows first to get the " +
      "window handle, or pass a title substring. Prefer screenshot + visual coordinate picking for " +
      "large, obvious targets. " +
      "Pass screenshot_id (from a recent screenshot) to get element center and bounding_rect " +
      "remapped to screenshot image pixels — ready to pass directly to mouse tools. " +
      "Without screenshot_id, coordinates are returned in logical pixels (not usable with mouse tools " +
      "until you take a screenshot and pass its ID).",
    inputSchema: {
      type: "object",
      properties: {
        hwnd: { type: "integer", description: "Window handle from list_windows (REQUIRED if title not provided)" },
        title: { type: "string", description: "Window title substring (REQUIRED if hwnd not provided)" },
        name: { type: "string", description: "Element name to search for (partial match, case-insensitive) — at least one filter required" },
        control_type: {
          type: "string",
          description: "UI Automation control type, e.g. Button, ListItem, Edit, CheckBox, MenuItem — at least one filter required",
        },
        automation_id: { type: "string", description: "Automation ID (exact match) — at least one filter required" },
        max_depth: { type: "integer", description: "Search depth limit (default 5, max 20)" },
        max_results: { type: "integer", description: "Maximum elements to return (default 20, max 100)" },
        timeout: { type: "number", description: "Search timeout in seconds (default 5.0, max 30)" },
        screenshot_id: {
          type: "string",
          description: "ID of a recent screenshot. When provided, returned center and bounding_rect are in screenshot image pixels for direct use with mouse tools.",
        },
      },
    },
  },
  {
    name: "element_at",
    description:
      "Identify the UI element at given coordinates using Windows UI Automation.\n" +
      "Returns element name, control_type, automation_id, and exact bounding_rect.\n\n" +
      "Consider this tool for small or ambiguous UI targets.\n" +
      "Fast and returns semantic info (you know what the element IS, not just what it looks like).\n\n" +
      "Best for: standard UI controls — buttons, inputs, checkboxes, menus, list items.\n\n" +
      "Not suitable for: game canvases, browser content, map tiles, custom-drawn areas,\n" +
      "or any region where UI Automation returns nothing useful.\n" +
      "Fall back to screenshot_zoom or screenshot_annotate if this returns an empty\n" +
      "result or a generic/unhelpful element type.",
    inputSchema: {
      type: "object",
      properties: {
        x: { type: "number", description: "X coordinate in screenshot pixels" },
        y: { type: "number", description: "Y coordinate in screenshot pixels" },
        screenshot_id: {
          type: "string",
          description: "ID returned by the screenshot tool that produced the image you are clicking on. Required.",
        },
      },
      required: ["x", "y", "screenshot_id"],
    },
  },
];

// ── Tool handler ─────────────────────────────────────────────────────────────

type Args = Record<string, unknown>;

export async function handleTool(
  name: string,
  args: Args
): Promise<
  | { content: (TextContent | ImageContent)[]; isError?: boolean }
> {
  try {
    switch (name) {
      // ── Screenshot ──────────────────────────────────────────────────────
      case "screenshot": {
        const isCrop = args.width !== undefined && args.height !== undefined;
        const body: Record<string, unknown> = {};
        let logicalLeft = 0, logicalTop = 0;

        if (isCrop) {
          const screenshotId = args.screenshot_id as string | undefined;
          if (!screenshotId) {
            return errorContent(
              "screenshot_id is required when taking a crop screenshot. " +
              "Take a full-screen screenshot first to get a valid ID."
            );
          }
          const t = lookupTransform(screenshotId);
          if (!t) {
            return errorContent(`screenshot: Screenshot ID '${screenshotId}' not found. Take a new screenshot first.`);
          }
          if (args.left !== undefined) {
            logicalLeft = t.originX + (t.scale === 1.0 ? numArg(args.left) : Math.round(numArg(args.left) / t.scale));
            body.left = logicalLeft;
          }
          if (args.top !== undefined) {
            logicalTop = t.originY + (t.scale === 1.0 ? numArg(args.top) : Math.round(numArg(args.top) / t.scale));
            body.top = logicalTop;
          }
          body.width  = resolveDim(args.width  as number | string, screenshotId);
          body.height = resolveDim(args.height as number | string, screenshotId);
        }

        const resp = await enginePost<{
          image: string;
          dpi_scale: number;
          image_scale: number;
          logical_size: [number, number];
          image_size: [number, number];
          physical_size: [number, number];
          virtual_origin: unknown;
          ruler_width?: number;
          ruler_height?: number;
        }>("/screenshot", body);
        if (!resp.success) {
          return errorContent(`screenshot failed: ${resp.error ?? "unknown error"}`);
        }

        const [lw, lh] = resp.data.logical_size;
        const [iw, ih] = resp.data.image_size;
        const [pw, ph] = resp.data.physical_size;

        // Register in screenshot registry
        const transform: ScreenshotTransform = {
          originX: logicalLeft,
          originY: logicalTop,
          scale: resp.data.image_scale,
          contentW: lw,
          contentH: lh,
        };
        const screenshotId = registerScreenshot(transform);
        const idLine = `Screenshot ID: ${screenshotId}`;

        let meta: string;
        if (isCrop) {
          meta =
            `${idLine}\n` +
            `Crop screenshot (${iw}\u00d7${ih} image pixels). ` +
            `Coordinates from this image are automatically remapped — just pass this screenshot_id.`;
        } else {
          meta =
            `${idLine}\n` +
            `Screenshot: ${iw}\u00d7${ih} image pixels.`;
        }

        return {
          content: [
            { type: "text", text: meta } as TextContent,
            { type: "image", data: resp.data.image, mimeType: "image/png" } as ImageContent,
          ],
        };
      }

      // ── Screenshot zoom ─────────────────────────────────────────────────
      case "screenshot_zoom": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required for screenshot_zoom. " +
            "Take a screenshot first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number, logicalW: number, logicalH: number;
        try {
          const coords = resolveCoords(args.x as number | string, args.y as number | string, screenshotId);
          logicalX = coords.logicalX;
          logicalY = coords.logicalY;
          logicalW = resolveDim(args.width !== undefined ? args.width as number | string : 200, screenshotId);
          logicalH = resolveDim(args.height !== undefined ? args.height as number | string : 200, screenshotId);
        } catch (e) { return errorContent((e as Error).message); }

        const zoomBody = {
          x: logicalX,
          y: logicalY,
          width: logicalW,
          height: logicalH,
          annotate: (args.annotate as boolean | undefined) ?? true,
        };

        const resp = await enginePost<ZoomData>("/screenshot/zoom", zoomBody);

        if (!resp.success) {
          return errorContent(`screenshot_zoom failed: ${resp.error ?? "unknown error"}`);
        }

        // Use engine-reported origin (clamped at screen edges) rather than self-calculating,
        // so the registry transform matches the actual image region when the crop is clipped.
        const cropOriginX = resp.data.virtual_origin.x;
        const cropOriginY = resp.data.virtual_origin.y;

        const [lw, lh] = resp.data.logical_size;
        const [iw, ih] = resp.data.image_size;
        const [pw, ph] = resp.data.physical_size;

        // Register new screenshot_id for this zoom
        const newTransform: ScreenshotTransform = {
          originX: cropOriginX,
          originY: cropOriginY,
          scale: resp.data.image_scale,
          contentW: lw,
          contentH: lh,
        };
        const newId = registerScreenshot(newTransform);

        const meta =
          `Screenshot ID: ${newId}\n` +
          `Zoom screenshot (${iw}\u00d7${ih} image pixels). ` +
          `Use this screenshot\u2019s ID with mouse tools \u2014 coordinates are automatically remapped.`;

        return {
          content: [
            { type: "text", text: meta } as TextContent,
            { type: "image", data: resp.data.image, mimeType: "image/png" } as ImageContent,
          ],
        };
      }

      // ── Screenshot annotate ─────────────────────────────────────────────
      case "screenshot_annotate": {
        interface AnnotationInput {
          x: number;
          y: number;
          marker_type?: string;
          color?: string;
          radius?: number;
        }

        const annotScreenshotId = args.screenshot_id as string | undefined;
        if (!annotScreenshotId) {
          return errorContent(
            "screenshot_id is required for screenshot_annotate. " +
            "Take a screenshot first to get a valid ID."
          );
        }
        const annotations = (args.annotations as AnnotationInput[]).map(a => {
          const c = resolveCoords(numArg(a.x), numArg(a.y), annotScreenshotId);
          return { ...a, x: c.logicalX, y: c.logicalY };
        });
        const body: Record<string, unknown> = { annotations };
        // Convert crop params using registry transform
        const t = lookupTransform(annotScreenshotId);
        if (t) {
          if (args.top    !== undefined) body.top    = t.originY + (t.scale === 1.0 ? numArg(args.top)    : Math.round(numArg(args.top)    / t.scale));
          if (args.left   !== undefined) body.left   = t.originX + (t.scale === 1.0 ? numArg(args.left)   : Math.round(numArg(args.left)   / t.scale));
          if (args.width  !== undefined) body.width  = t.scale === 1.0 ? numArg(args.width)  : Math.round(numArg(args.width)  / t.scale);
          if (args.height !== undefined) body.height = t.scale === 1.0 ? numArg(args.height) : Math.round(numArg(args.height) / t.scale);
        }

        const resp = await enginePost<{
          image: string;
          dpi_scale: number;
          image_scale: number;
          logical_size: [number, number];
          image_size: [number, number];
          physical_size: [number, number];
          virtual_origin: unknown;
          skipped_annotations: number[];
        }>("/screenshot/annotate", body);

        if (!resp.success) {
          return errorContent(`screenshot_annotate failed: ${resp.error ?? "unknown error"}`);
        }

        // screenshot_annotate does NOT update crop state — it is a verification tool.
        const [lw, lh] = resp.data.logical_size;
        const [iw, ih] = resp.data.image_size;
        const skipped = resp.data.skipped_annotations;

        // Return same screenshot_id — coordinate space unchanged.
        // The shorter format (no origin/scale/content) is intentional: annotate does not
        // register a new transform, so there is no new metadata to report. The original
        // screenshot's transform is still in the registry under the same ID.
        const idPrefix = annotScreenshotId ? `Screenshot ID: ${annotScreenshotId}\n` : "";
        let meta =
          `${idPrefix}Annotated screenshot: ${iw}\u00d7${ih} image pixels` +
          // ` (logical ${lw}\u00d7${lh}, image scale ${resp.data.image_scale.toFixed(4)})` +
          `.`;
        if (skipped.length > 0) {
          meta += ` WARNING: annotations at indices [${skipped.join(", ")}] were outside the image bounds — re-check those coordinates.`;
        }

        return {
          content: [
            { type: "text", text: meta } as TextContent,
            { type: "image", data: resp.data.image, mimeType: "image/png" } as ImageContent,
          ],
        };
      }

      // ── Mouse ────────────────────────────────────────────────────────────
      case "mouse_move": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number;
        try {
          ({ logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId));
        } catch (e) {
          return errorContent((e as Error).message);
        }
        const resp = await enginePost("/mouse", { action: "move", x: logicalX, y: logicalY });
        if (!resp.success) return errorContent(`mouse_move failed: ${resp.error}`);
        return textContent("Mouse moved.");
      }

      case "mouse_click": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number;
        try {
          ({ logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId));
        } catch (e) {
          return errorContent((e as Error).message);
        }
        const clickResp = await enginePost("/mouse", {
          action: "click",
          x: logicalX,
          y: logicalY,
          button: args.button ?? "left",
        });
        if (!clickResp.success) return errorContent(`mouse_click failed: ${clickResp.error}`);

        const confirmText = `Mouse clicked at logical (${logicalX}, ${logicalY}).`;
        let zoomResp: EngineResponse<ZoomData>;
        try {
          zoomResp = await fetchZoomRaw(logicalX, logicalY);
        } catch {
          return textContent(`${confirmText}\n[Verification zoom unavailable — verification service unreachable]`);
        }
        if (!zoomResp.success || !zoomResp.data) {
          return textContent(`${confirmText}\n[Verification zoom unavailable — click was at screen edge]`);
        }
        return {
          content: [
            { type: "text", text: `${confirmText}\n[Verification zoom — does NOT change coordinate context]` } as TextContent,
            { type: "image", data: zoomResp.data.image, mimeType: "image/png" } as ImageContent,
          ],
        };
      }

      case "mouse_double_click": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number;
        try {
          ({ logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId));
        } catch (e) {
          return errorContent((e as Error).message);
        }
        const clickResp = await enginePost("/mouse", {
          action: "double_click",
          x: logicalX,
          y: logicalY,
          button: args.button ?? "left",
        });
        if (!clickResp.success) return errorContent(`mouse_double_click failed: ${clickResp.error}`);

        const confirmText = `Mouse double-clicked at logical (${logicalX}, ${logicalY}).`;
        let zoomResp: EngineResponse<ZoomData>;
        try {
          zoomResp = await fetchZoomRaw(logicalX, logicalY);
        } catch {
          return textContent(`${confirmText}\n[Verification zoom unavailable — verification service unreachable]`);
        }
        if (!zoomResp.success || !zoomResp.data) {
          return textContent(`${confirmText}\n[Verification zoom unavailable — click was at screen edge]`);
        }
        return {
          content: [
            { type: "text", text: `${confirmText}\n[Verification zoom — does NOT change coordinate context]` } as TextContent,
            { type: "image", data: zoomResp.data.image, mimeType: "image/png" } as ImageContent,
          ],
        };
      }

      case "mouse_drag": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let lx1: number, ly1: number, lx2: number, ly2: number;
        try {
          ({ logicalX: lx1, logicalY: ly1 } = resolveCoords(args.x1 as number | string, args.y1 as number | string, screenshotId));
          ({ logicalX: lx2, logicalY: ly2 } = resolveCoords(args.x2 as number | string, args.y2 as number | string, screenshotId));
        } catch (e) { return errorContent((e as Error).message); }
        const body: Record<string, unknown> = { action: "drag", x: lx1, y: ly1, x2: lx2, y2: ly2 };
        if (args.button    !== undefined) body.button     = args.button;
        if (args.duration  !== undefined) body.duration   = args.duration;
        if (args.hold_before !== undefined) body.hold_before = args.hold_before;
        if (args.steps     !== undefined) body.steps      = args.steps;
        if (args.waypoints !== undefined) {
          // Use resolveCoords for each waypoint — consistent with start/end coord handling above.
          try {
            body.waypoints = (args.waypoints as Array<{ x: number; y: number }>).map(wp => {
              const { logicalX, logicalY } = resolveCoords(wp.x, wp.y, screenshotId);
              return { x: logicalX, y: logicalY };
            });
          } catch (e) { return errorContent((e as Error).message); }
        }
        const resp = await enginePost("/mouse", body);
        if (!resp.success) return errorContent(`mouse_drag failed: ${resp.error}`);
        return textContent("Mouse dragged.");
      }

      case "mouse_down": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number;
        try {
          ({ logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId));
        } catch (e) {
          return errorContent((e as Error).message);
        }
        const resp = await enginePost("/mouse", { action: "mousedown", x: logicalX, y: logicalY, button: args.button ?? "left" });
        if (!resp.success) return errorContent(`mouse_down failed: ${resp.error}`);
        return textContent("Mouse button pressed.");
      }

      case "mouse_up": {
        const hasX = args.x !== undefined;
        const hasY = args.y !== undefined;
        if (hasX !== hasY) {
          return errorContent("mouse_up: x and y must be provided together — one without the other is invalid.");
        }
        const body: Record<string, unknown> = { action: "mouseup", button: args.button ?? "left" };
        if (hasX && hasY) {
          const screenshotId = args.screenshot_id as string | undefined;
          if (!screenshotId) {
            return errorContent("screenshot_id is required when providing x and y coordinates to mouse_up.");
          }
          try {
            const { logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId);
            body.x = logicalX;
            body.y = logicalY;
          } catch (e) { return errorContent((e as Error).message); }
        }
        const resp = await enginePost("/mouse", body);
        if (!resp.success) return errorContent(`mouse_up failed: ${resp.error}`);
        return textContent("Mouse button released.");
      }

      case "mouse_scroll": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let logicalX: number, logicalY: number;
        try {
          ({ logicalX, logicalY } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId));
        } catch (e) {
          return errorContent((e as Error).message);
        }
        const resp = await enginePost("/mouse", { action: "scroll", x: logicalX, y: logicalY, amount: args.amount });
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

      case "keydown":
      case "keyboard_keydown": {
        const resp = await enginePost("/keyboard", {
          action: "keydown",
          key: args.key,
        });
        if (!resp.success) return errorContent(`keydown failed: ${resp.error}`);
        return textContent(`Key down: ${String(args.key)}`);
      }

      case "keyup":
      case "keyboard_keyup": {
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
        if (args.env_extra !== undefined) body.env_extra = args.env_extra;
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
        const resp = await enginePost<{ new_index: number | null }>("/desktop/create", {});
        if (!resp.success) return errorContent(`create_desktop failed: ${resp.error}`);
        return textContent(`Created desktop at index ${String(resp.data?.new_index ?? "unknown")}.`);
      }

      case "delete_desktop": {
        const resp = await engineDelete<unknown>(`/desktop/${String(args.index)}`);
        if (!resp.success) return errorContent(`delete_desktop failed: ${resp.error}`);
        return textContent(`Desktop ${String(args.index)} deleted.`);
      }

      // ── UI Automation ───────────────────────────────────────────────────
      case "find_element": {
        if (args.hwnd === undefined && args.title === undefined) {
          return errorContent(
            "find_element requires at least one target: provide hwnd (from list_windows) or title (window title substring)."
          );
        }
        if (args.name === undefined && args.control_type === undefined && args.automation_id === undefined) {
          return errorContent(
            "find_element requires at least one filter: provide name, control_type, or automation_id."
          );
        }
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
        const findScreenshotId = args.screenshot_id;
        const findT = (typeof findScreenshotId === "string" && findScreenshotId)
          ? lookupTransform(findScreenshotId)
          : undefined;
        return textContent(JSON.stringify(remapElementCoords(resp.data, findT), null, 2));
      }

      case "element_at": {
        const screenshotId = args.screenshot_id as string | undefined;
        if (!screenshotId) {
          return errorContent(
            "screenshot_id is required. Call screenshot or screenshot_zoom first to get a valid ID."
          );
        }
        let lx: number, ly: number;
        try { ({ logicalX: lx, logicalY: ly } = resolveCoords(args.x as number | string, args.y as number | string, screenshotId)); }
        catch (e) { return errorContent((e as Error).message); }
        const resp = await enginePost<unknown>("/element_at", { x: lx, y: ly });
        if (!resp.success) return errorContent(`element_at failed: ${resp.error}`);
        if (resp.data === null) return textContent(resp.error ?? "No element found at this coordinate.");
        const t = lookupTransform(screenshotId);
        return textContent(JSON.stringify(remapElementCoords(resp.data, t), null, 2));
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

// ── Auto-start engine ────────────────────────────────────────────────────────

const PROJECT_ROOT = join(__dirname, "..", "..");
const ENGINE_DIR = join(PROJECT_ROOT, "engine");
const PYTHON_EXE = join(ENGINE_DIR, "venv", "Scripts", "python.exe");

async function isEngineRunning(): Promise<boolean> {
  try {
    const res = await fetch(`${ENGINE_URL}/health`, {
      headers: authHeaders(),
      signal: AbortSignal.timeout(2000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

async function killEngineOnPort(): Promise<void> {
  try {
    // Find PID listening on engine port
    const url = new URL(ENGINE_URL);
    const port = url.port || "8765";
    const { execSync } = await import("child_process");
    const out = execSync(
      `netstat -ano | findstr ":${port} " | findstr "LISTENING"`,
      { encoding: "utf-8", timeout: 5000, windowsHide: true },
    ).trim();
    const pids = new Set(
      out.split("\n").map((l) => l.trim().split(/\s+/).pop()).filter(Boolean),
    );
    for (const pid of pids) {
      try {
        execSync(`taskkill /PID ${pid} /F`, { stdio: "ignore", timeout: 5000, windowsHide: true });
        console.error(`[MCP] Killed old engine process (PID ${pid}).`);
      } catch { /* already gone */ }
    }
    // Brief pause to let the port release
    if (pids.size > 0) await new Promise((r) => setTimeout(r, 1000));
  } catch {
    // No process found on port — nothing to kill
  }
}

async function ensureEngineRunning(): Promise<void> {
  // Always kill old engine so we start fresh with latest code
  await killEngineOnPort();

  console.error("[MCP] Starting engine...");

  const child = spawn(PYTHON_EXE, ["main.py"], {
    cwd: ENGINE_DIR,
    stdio: "ignore",
    windowsHide: true,
    env: { ...process.env, ENGINE_SECRET },
  });
  child.unref();

  // Poll until engine is ready (up to 15 seconds)
  const maxWait = 15_000;
  const interval = 500;
  const deadline = Date.now() + maxWait;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, interval));
    if (await isEngineRunning()) {
      console.error("[MCP] Engine started successfully.");
      return;
    }
  }

  console.error("[MCP] WARNING: Engine did not respond within 15 s — continuing anyway.");
}

// ── Entry point ──────────────────────────────────────────────────────────────

if (!ENGINE_SECRET) {
  console.error("WARNING: ENGINE_SECRET is not set. All requests to the engine will fail with 401.");
}

await ensureEngineRunning();

const transport = new StdioServerTransport();
await server.connect(transport);
