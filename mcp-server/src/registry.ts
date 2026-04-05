// mcp-server/src/registry.ts

/**
 * Screenshot registry for coordinate binding.
 * Maps screenshot_id → ScreenshotTransform (FIFO, max REGISTRY_MAX entries).
 *
 * Public API:
 *   registerScreenshot(t) → id      used by screenshot handlers
 *   resolveCoords(x, y, id)         used by coordinate tool handlers
 *   resolveDim(dim, id)             used for width/height conversion
 *   lookupTransform(id)             used by remapElementCoords
 */

export interface ScreenshotTransform {
  originX: number;   // logical pixel x-offset (0 for full-screen)
  originY: number;   // logical pixel y-offset (0 for full-screen)
  scale: number;     // image pixels → logical: logical = origin + round(imgPx / scale)
  contentW: number;  // content width in logical pixels (excludes ruler strips)
  contentH: number;  // content height in logical pixels (excludes ruler strips)
}

const _registry = new Map<string, ScreenshotTransform>();
export const REGISTRY_MAX = 20;
let _seq = 0;

export function registerScreenshot(transform: ScreenshotTransform): string {
  const id = `scr_${Date.now()}_${_seq++}`;
  if (_registry.size >= REGISTRY_MAX) {
    _registry.delete(_registry.keys().next().value!);
  }
  _registry.set(id, transform);
  return id;
}

function _getOrThrow(screenshotId: string): ScreenshotTransform {
  const t = _registry.get(screenshotId);
  if (!t) {
    throw new Error(
      `Screenshot ID '${screenshotId}' not found in registry ` +
      `(registry holds last ${REGISTRY_MAX} screenshots). ` +
      `Please take a new screenshot to get a valid ID before clicking.`
    );
  }
  return t;
}

/** Convert MCP tool arg to number. Throws for non-numeric inputs (NaN, Infinity, non-numeric strings)
 *  so that invalid coordinates produce an explicit error instead of silently clicking at origin. */
function _toNum(v: number | string): number {
  const n = Number(v);
  if (!Number.isFinite(n)) {
    throw new Error(`Invalid coordinate: expected a finite number, got ${JSON.stringify(v)}`);
  }
  return n;
}

export function resolveCoords(
  x: number | string,
  y: number | string,
  screenshotId: string
): { logicalX: number; logicalY: number } {
  const t = _getOrThrow(screenshotId);
  const nx = _toNum(x);
  const ny = _toNum(y);
  return {
    logicalX: t.originX + (t.scale === 1.0 ? nx : Math.round(nx / t.scale)),
    logicalY: t.originY + (t.scale === 1.0 ? ny : Math.round(ny / t.scale)),
  };
}

export function resolveDim(dim: number | string, screenshotId: string): number {
  const t = _getOrThrow(screenshotId);
  const n = _toNum(dim);
  return t.scale === 1.0 ? n : Math.round(n / t.scale);
}

export function lookupTransform(screenshotId: string): ScreenshotTransform | undefined {
  return _registry.get(screenshotId);
}

// ── Test helpers (never call from production code) ────────────────────────────
export function _clearRegistry(): void { _registry.clear(); _seq = 0; }
export function _registrySize(): number { return _registry.size; }
