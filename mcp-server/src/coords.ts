// ── Screenshot size limit ────────────────────────────────────────────────────
// Maximum image dimensions returned to Claude. Shared by engine and MCP layer.

export const SCREENSHOT_MAX_W = 1568;
export const SCREENSHOT_MAX_H = 882;

// ── Pure coordinate math ─────────────────────────────────────────────────────
// No state. All functions take explicit scale/offset parameters.
// The MCP layer wraps these with stateful helpers that close over current state.

/**
 * Convert an image-pixel coordinate to logical pixels using only scale.
 * Use for full-screen context (no crop offset) or dimension values.
 */
export function toLogicalCoord(imagePixel: number, scale: number): number {
  if (scale <= 0) return imagePixel;
  return scale === 1.0 ? imagePixel : Math.round(imagePixel / scale);
}

/**
 * Convert an image-pixel position to full-screen logical pixels.
 * Applies scale division then adds the crop offset (0 for full-screen).
 */
export function toLogicalPos(imagePixel: number, scale: number, offset: number): number {
  if (scale <= 0) return imagePixel + offset;
  return (scale === 1.0 ? imagePixel : Math.round(imagePixel / scale)) + offset;
}

/**
 * Convert an image-pixel dimension (width/height/radius) to logical pixels.
 * Uses the current screenshot's scale — same math as toLogicalCoord but
 * semantically distinct: dimensions never have an offset.
 */
export function toLogicalDimCoord(imagePixel: number, scale: number): number {
  if (scale <= 0) return imagePixel;
  return scale === 1.0 ? imagePixel : Math.round(imagePixel / scale);
}

/**
 * Convert a full-screen logical pixel position back to current-screenshot image pixels.
 * Inverse of toLogicalPos: subtract offset, then multiply by scale.
 */
export function toImageCoord(logicalPixel: number, scale: number, offset: number): number {
  if (scale <= 0) return logicalPixel - offset;
  const v = logicalPixel - offset;
  return scale === 1.0 ? v : Math.round(v * scale);
}

/**
 * Convert a logical dimension back to image pixels.
 * No offset — dimensions don't have one.
 */
export function toImageDimCoord(logicalDim: number, scale: number): number {
  if (scale <= 0) return logicalDim;
  return scale === 1.0 ? logicalDim : Math.round(logicalDim * scale);
}
