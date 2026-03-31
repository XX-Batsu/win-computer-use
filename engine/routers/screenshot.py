"""
routers/screenshot.py — POST /screenshot

Captures a screen region (or full virtual screen) using mss, converts to PNG,
and returns base64-encoded image data together with DPI metadata.

Coordinates in the request body are logical pixels; the engine converts them to
physical pixels before passing them to mss.
"""

import base64
import io
from typing import Optional

import mss
import mss.tools
from fastapi import APIRouter
from PIL import Image
from pydantic import BaseModel

from engine import dpi_utils

router = APIRouter()


class ScreenshotRequest(BaseModel):
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
                # Capture entire virtual desktop
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

            # Convert mss ScreenShot → PIL Image → PNG bytes → base64
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")

        return {
            "success": True,
            "data": {
                "image": b64,
                "dpi_scale": dpi_scale,
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
