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
from PIL import Image, ImageDraw, ImageColor
from pydantic import BaseModel, Field, field_validator

from engine import dpi_utils

SCREENSHOT_MAX_W = 1568
SCREENSHOT_MAX_H = 882

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


class ScreenshotRequest(BaseModel):
    top: Optional[int] = None
    left: Optional[int] = None
    width: Optional[int] = Field(None, ge=1)
    height: Optional[int] = Field(None, ge=1)


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

            # Convert mss ScreenShot → PIL Image (physical pixels)
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
