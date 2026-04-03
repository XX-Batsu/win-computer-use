"""
routers/screenshot.py — POST /screenshot

Captures a screen region (or full virtual screen) using mss, converts to PNG,
and returns base64-encoded image data together with coordinate metadata.

Coordinate pipeline:
  1. Input coords (logical pixels) → physical pixels via dpi_utils.logical_to_physical
  2. Captured image (physical pixels) → logical pixels by dividing by dpi_scale
  3. Logical image → further downscaled to fit within SCREENSHOT_MAX_W×SCREENSHOT_MAX_H if needed (image_scale)

The returned image is in "image pixel" space.  image_scale is the factor between
image pixels and logical pixels: logical = image / image_scale.  The MCP layer
uses image_scale to remap coordinates transparently so callers always work in
image pixels.
"""

import base64
import io
from typing import Optional

import mss
import mss.tools
from fastapi import APIRouter
from PIL import Image, ImageDraw, ImageColor, ImageFont
from pydantic import BaseModel, Field, field_validator

from engine import dpi_utils

SCREENSHOT_MAX_W = 1568
SCREENSHOT_MAX_H = 882
RULER_WIDTH = 28   # Y-axis strip (right side, px)
RULER_HEIGHT = 24  # X-axis strip (bottom, px)

router = APIRouter()


def _validate_color(color: str) -> str:
    """Return color if PIL recognizes it, else fall back to 'red'."""
    try:
        ImageColor.getcolor(color, "RGBA")
        return color
    except Exception:  # noqa: BLE001
        return "red"


def _has_monitor_overlap(left: int, top: int, width: int, height: int, monitor_map: list) -> bool:
    """Return True if the logical rect [left, top, left+width, top+height) overlaps any monitor."""
    right = left + width
    bottom = top + height
    for m in monitor_map:
        if (right > m["logical_left"] and left < m["logical_right"] and
                bottom > m["logical_top"] and top < m["logical_bottom"]):
            return True
    return False


def _draw_annotation(
    draw: "ImageDraw.ImageDraw",
    img_x: int,
    img_y: int,
    img_w: int,
    img_h: int,
    marker_type: str,
    color: str,
    radius: int,
) -> None:
    """Draw a single annotation marker onto draw at (img_x, img_y).

    marker_type: "crosshair" | "circle" | "both"
    crosshair — full-width/height lines with a hollow circle gap at center.
    circle    — hollow circle only.
    both      — crosshair + circle.
    """
    if marker_type in ("crosshair", "both"):
        # Horizontal line — left segment (stops at gap)
        if img_x - radius >= 0:
            draw.line([(0, img_y), (img_x - radius, img_y)], fill=color, width=1)
        # Horizontal line — right segment
        if img_x + radius < img_w:
            draw.line([(img_x + radius, img_y), (img_w - 1, img_y)], fill=color, width=1)
        # Vertical line — top segment
        if img_y - radius >= 0:
            draw.line([(img_x, 0), (img_x, img_y - radius)], fill=color, width=1)
        # Vertical line — bottom segment
        if img_y + radius < img_h:
            draw.line([(img_x, img_y + radius), (img_x, img_h - 1)], fill=color, width=1)
    # Hollow circle — drawn for all marker types:
    # acts as the gap for crosshair, the main marker for circle, both for "both"
    draw.ellipse(
        [(img_x - radius, img_y - radius), (img_x + radius, img_y + radius)],
        outline=color,
    )


def _draw_rulers(img: Image.Image) -> Image.Image:
    """
    Expand img canvas by RULER_WIDTH px right and RULER_HEIGHT px bottom.
    Draw Y-axis ruler on the right strip and X-axis ruler on the bottom strip.
    Ruler labels are image-pixel coordinates (not logical coordinates).

    Must be called AFTER image_scale is finalised — i.e. after the
    SCREENSHOT_MAX downscaling step in take_screenshot.

    Returns the expanded image.
    """
    content_w, content_h = img.width, img.height
    new_w = content_w + RULER_WIDTH
    new_h = content_h + RULER_HEIGHT

    ruled = Image.new("RGB", (new_w, new_h), (0xE8, 0xE8, 0xE8))
    ruled.paste(img, (0, 0))
    draw = ImageDraw.Draw(ruled)

    try:
        font = ImageFont.truetype("arial.ttf", 11)
    except (IOError, OSError):
        font = ImageFont.load_default()

    # Y-axis ruler (right strip: x in [content_w, new_w - 1])
    # Ticks extend LEFT from the strip's left edge into screenshot content.
    for y_px in range(0, content_h, 50):
        is_major = (y_px % 100 == 0)
        tick_len = 12 if is_major else 6
        draw.line([(content_w - tick_len, y_px), (content_w - 1, y_px)], fill="black", width=1)
        if is_major:
            label = str(y_px)
            bbox = draw.textbbox((0, 0), label, font=font)
            label_w = bbox[2] - bbox[0]
            label_h = bbox[3] - bbox[1]
            lx = content_w + RULER_WIDTH - 4 - label_w  # right-aligned, 4px margin
            ly = y_px - label_h // 2
            # Clamp bottom first, then top (top takes priority)
            bottom_clamp = new_h - label_h - 2
            if bottom_clamp >= 0:
                ly = min(ly, bottom_clamp)
            ly = max(2, ly)
            draw.text((lx, ly), label, fill="black", font=font)

    # X-axis ruler (bottom strip: y in [content_h, new_h - 1])
    # Ticks extend UP from the strip's top edge into screenshot content.
    for x_px in range(0, content_w, 50):
        is_major = (x_px % 100 == 0)
        tick_len = 12 if is_major else 6
        draw.line([(x_px, content_h - tick_len), (x_px, content_h - 1)], fill="black", width=1)
        if is_major:
            label = str(x_px)
            bbox = draw.textbbox((0, 0), label, font=font)
            label_w = bbox[2] - bbox[0]
            label_h = bbox[3] - bbox[1]
            lx = x_px - label_w // 2
            # Clamp right first, then left (left takes priority)
            right_clamp = content_w - label_w - 2
            if right_clamp > 2:
                lx = min(lx, right_clamp)
            lx = max(2, lx)
            ly = content_h + (RULER_HEIGHT - label_h) // 2
            draw.text((lx, ly), label, fill="black", font=font)

    return ruled


class ScreenshotRequest(BaseModel):
    top: Optional[int] = None
    left: Optional[int] = None
    width: Optional[int] = Field(None, ge=1)
    height: Optional[int] = Field(None, ge=1)
    ruler: bool = False


class ZoomRequest(BaseModel):
    x: int
    y: int
    width: int = Field(200, ge=1)
    height: int = Field(200, ge=1)
    annotate: bool = True


class Annotation(BaseModel):
    x: int
    y: int
    marker_type: str = "crosshair"
    color: str = "red"
    radius: int = 10

    @field_validator("marker_type")
    @classmethod
    def validate_marker_type(cls, v: str) -> str:
        if v not in ("crosshair", "circle", "both"):
            raise ValueError("marker_type must be 'crosshair', 'circle', or 'both'")
        return v


class AnnotateRequest(BaseModel):
    annotations: list[Annotation]
    top: Optional[int] = None
    left: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None


@router.post("/screenshot")
async def take_screenshot(req: ScreenshotRequest):
    try:
        monitor_map = dpi_utils.get_monitor_map()

        with mss.mss() as sct:
            # Determine virtual screen origin from mss monitor index 0
            virtual_mon = sct.monitors[0]  # index 0 = bounding box of all monitors
            virtual_origin = {"x": virtual_mon["left"], "y": virtual_mon["top"]}

            if (req.width is None) != (req.height is None):
                return {
                    "success": False,
                    "data": None,
                    "error": "width and height must both be provided or both omitted",
                    "timed_out": False,
                }

            full_screen = (
                req.width is None
                and req.height is None
            )

            if full_screen:
                # Capture entire virtual desktop.
                # NOTE (mixed-DPI limitation): The primary monitor's DPI scale is
                # applied uniformly to the whole image.  On setups where monitors
                # have different DPI scales, coordinates on non-primary monitors
                # will be slightly misaligned.  Accurate multi-DPI support would
                # require per-monitor capture-and-stitch.
                monitor = virtual_mon
                dpi_scale = dpi_utils.get_primary_scale(monitor_map)
            else:
                # Convert logical region to physical region
                logical_left = req.left if req.left is not None else 0
                logical_top = req.top if req.top is not None else 0

                phys_left, phys_top, scale = dpi_utils.logical_to_physical(
                    logical_left, logical_top, monitor_map
                )
                # NOTE (cross-monitor DPI limitation): scale is taken from the monitor
                # containing the top-left corner of the region.  If the crop region spans
                # two monitors with different DPI scales, the single scale value will be
                # wrong for the portion that falls on the other monitor, producing incorrect
                # physical dimensions for that portion.  Per-monitor capture-and-stitch
                # would be required to fix this correctly — left as future work.
                phys_width = int(req.width * scale)
                phys_height = int(req.height * scale)

                monitor = {
                    "top": phys_top,
                    "left": phys_left,
                    "width": phys_width,
                    "height": phys_height,
                }
                dpi_scale = scale

            screenshot = sct.grab(monitor)

            # Convert mss ScreenShot → PIL Image (physical pixels).
            # mss guarantees BGRA byte order in screenshot.bgra; "BGRX" tells PIL to
            # treat the 4th byte as padding (ignored), yielding an RGB image.
            # If a future mss version changes the pixel format, images will be
            # silently garbled — verify screenshot.pixel_format == "BGRA" if debugging.
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

            # Record physical size before downscaling (for P3 metadata)
            physical_size = (img.width, img.height)

            # P0: Downscale to logical size so pixel coords = logical coords
            if dpi_scale != 1.0:
                logical_size = (round(img.width / dpi_scale), round(img.height / dpi_scale))
                img = img.resize(logical_size, Image.LANCZOS)

            logical_size = (img.width, img.height)

            # Resize to fit within SCREENSHOT_MAX_W×SCREENSHOT_MAX_H for Claude-friendly token usage.
            # image_scale lets callers map image-pixel coords back to logical coords.
            image_scale = 1.0
            if img.width > SCREENSHOT_MAX_W or img.height > SCREENSHOT_MAX_H:
                image_scale = min(SCREENSHOT_MAX_W / img.width, SCREENSHOT_MAX_H / img.height)
                new_size = (round(img.width * image_scale), round(img.height * image_scale))
                img = img.resize(new_size, Image.LANCZOS)

            # P3: Draw ruler strips AFTER image_scale is final.
            ruler_width = 0
            ruler_height = 0
            if req.ruler:
                img = _draw_rulers(img)
                ruler_width = RULER_WIDTH
                ruler_height = RULER_HEIGHT

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return {
            "success": True,
            "data": {
                "image": b64,
                "dpi_scale": dpi_scale,
                "image_scale": image_scale,
                "logical_size": list(logical_size),
                "image_size": [img.width, img.height],
                "physical_size": list(physical_size),
                "virtual_origin": virtual_origin,
                "ruler_width": ruler_width,
                "ruler_height": ruler_height,
            },
            "error": None,
            "timed_out": False,
        }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }


@router.post("/screenshot/zoom")
async def screenshot_zoom(req: ZoomRequest):
    try:
        monitor_map = dpi_utils.get_monitor_map()

        left = req.x - req.width // 2
        top = req.y - req.height // 2

        if not _has_monitor_overlap(left, top, req.width, req.height, monitor_map):
            return {
                "success": False,
                "data": None,
                "error": "crop region outside all monitors",
                "timed_out": False,
            }

        phys_left, phys_top, scale = dpi_utils.logical_to_physical(left, top, monitor_map)
        phys_width = int(req.width * scale)
        phys_height = int(req.height * scale)

        monitor = {
            "top": phys_top,
            "left": phys_left,
            "width": phys_width,
            "height": phys_height,
        }
        dpi_scale = scale

        with mss.mss() as sct:
            virtual_mon = sct.monitors[0]
            virtual_origin = {"x": virtual_mon["left"], "y": virtual_mon["top"]}
            screenshot = sct.grab(monitor)
            # mss BGRA pixel format; "BGRX" treats 4th byte as ignored padding → RGB
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        physical_size = (img.width, img.height)

        if dpi_scale != 1.0:
            logical_size = (round(img.width / dpi_scale), round(img.height / dpi_scale))
            img = img.resize(logical_size, Image.LANCZOS)

        logical_size = (img.width, img.height)

        image_scale = 1.0
        if img.width > SCREENSHOT_MAX_W or img.height > SCREENSHOT_MAX_H:
            image_scale = min(SCREENSHOT_MAX_W / img.width, SCREENSHOT_MAX_H / img.height)
            new_size = (round(img.width * image_scale), round(img.height * image_scale))
            img = img.resize(new_size, Image.LANCZOS)

        if req.annotate:
            draw = ImageDraw.Draw(img)
            cx, cy = img.width // 2, img.height // 2
            r = 5
            draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill="red")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return {
            "success": True,
            "data": {
                "image": b64,
                "dpi_scale": dpi_scale,
                "image_scale": image_scale,
                "logical_size": list(logical_size),
                "image_size": [img.width, img.height],
                "physical_size": list(physical_size),
                "virtual_origin": virtual_origin,
            },
            "error": None,
            "timed_out": False,
        }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }


@router.post("/screenshot/annotate")
async def screenshot_annotate(req: AnnotateRequest):
    try:
        monitor_map = dpi_utils.get_monitor_map()

        with mss.mss() as sct:
            virtual_mon = sct.monitors[0]
            virtual_origin = {"x": virtual_mon["left"], "y": virtual_mon["top"]}

            if (req.width is None) != (req.height is None):
                return {
                    "success": False,
                    "data": None,
                    "error": "width and height must both be provided or both omitted",
                    "timed_out": False,
                }

            full_screen = req.width is None and req.height is None

            if full_screen:
                monitor = virtual_mon
                dpi_scale = dpi_utils.get_primary_scale(monitor_map)
            else:
                logical_left = req.left if req.left is not None else 0
                logical_top = req.top if req.top is not None else 0
                phys_left, phys_top, scale = dpi_utils.logical_to_physical(
                    logical_left, logical_top, monitor_map
                )
                phys_width = int(req.width * scale)
                phys_height = int(req.height * scale)
                monitor = {
                    "top": phys_top,
                    "left": phys_left,
                    "width": phys_width,
                    "height": phys_height,
                }
                dpi_scale = scale

            screenshot = sct.grab(monitor)
            # mss BGRA pixel format; "BGRX" treats 4th byte as ignored padding → RGB
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        physical_size = (img.width, img.height)

        if dpi_scale != 1.0:
            logical_size = (round(img.width / dpi_scale), round(img.height / dpi_scale))
            img = img.resize(logical_size, Image.LANCZOS)

        logical_size = (img.width, img.height)

        image_scale = 1.0
        if img.width > SCREENSHOT_MAX_W or img.height > SCREENSHOT_MAX_H:
            image_scale = min(SCREENSHOT_MAX_W / img.width, SCREENSHOT_MAX_H / img.height)
            new_size = (round(img.width * image_scale), round(img.height * image_scale))
            img = img.resize(new_size, Image.LANCZOS)

        draw = ImageDraw.Draw(img)
        skipped_annotations: list[int] = []

        crop_left = req.left if req.left is not None else 0
        crop_top = req.top if req.top is not None else 0
        for i, ann in enumerate(req.annotations):
            img_x = round((ann.x - crop_left) * image_scale)
            img_y = round((ann.y - crop_top) * image_scale)

            if img_x < 0 or img_x >= img.width or img_y < 0 or img_y >= img.height:
                skipped_annotations.append(i)
                continue

            color = _validate_color(ann.color)
            _draw_annotation(draw, img_x, img_y, img.width, img.height,
                             ann.marker_type, color, ann.radius)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return {
            "success": True,
            "data": {
                "image": b64,
                "dpi_scale": dpi_scale,
                "image_scale": image_scale,
                "logical_size": list(logical_size),
                "image_size": [img.width, img.height],
                "physical_size": list(physical_size),
                "virtual_origin": virtual_origin,
                "skipped_annotations": skipped_annotations,
            },
            "error": None,
            "timed_out": False,
        }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
