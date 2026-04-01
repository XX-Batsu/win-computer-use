"""
routers/mouse.py — POST /mouse

Supports actions: move, click, drag, scroll.
All input coordinates are logical pixels; converted to physical before calling
pyautogui (which operates in physical pixel space).
"""

import asyncio
import time
from typing import Literal, Optional

import pyautogui
from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine import dpi_utils

router = APIRouter()


class MouseRequest(BaseModel):
    action: Literal["move", "click", "drag", "scroll"]
    x: int
    y: int
    button: Optional[str] = "left"
    x2: Optional[int] = None
    y2: Optional[int] = None
    amount: Optional[int] = None
    # Drag-specific parameters (ignored for other actions)
    duration: float = Field(0.5, ge=0.0, le=10.0)
    hold_before: float = Field(0.2, ge=0.0, le=5.0)
    steps: int = Field(20, ge=1, le=200)


@router.post("/mouse")
async def mouse_action(req: MouseRequest):
    try:
        monitor_map = dpi_utils.get_monitor_map()
        phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)

        if req.action == "move":
            pyautogui.moveTo(phys_x, phys_y)
            return {
                "success": True,
                "data": {"moved_to": {"x": phys_x, "y": phys_y}},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "click":
            button = req.button or "left"
            pyautogui.click(phys_x, phys_y, button=button)
            return {
                "success": True,
                "data": {"clicked_at": {"x": phys_x, "y": phys_y}, "button": button},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "drag":
            if req.x2 is None or req.y2 is None:
                return {
                    "success": False,
                    "data": None,
                    "error": "drag requires x2 and y2",
                    "timed_out": False,
                }
            phys_x2, phys_y2, _ = dpi_utils.logical_to_physical(req.x2, req.y2, monitor_map)
            button = req.button or "left"

            def _execute_drag():
                pyautogui.moveTo(phys_x, phys_y)
                pyautogui.mouseDown(button=button)
                time.sleep(req.hold_before)

                for i in range(1, req.steps + 1):
                    t = i / req.steps
                    ix = phys_x + (phys_x2 - phys_x) * t
                    iy = phys_y + (phys_y2 - phys_y) * t
                    pyautogui.moveTo(int(ix), int(iy))
                    time.sleep(req.duration / req.steps)

                time.sleep(0.1)
                pyautogui.mouseUp(button=button)

            await asyncio.to_thread(_execute_drag)
            return {
                "success": True,
                "data": {
                    "dragged_from": {"x": phys_x, "y": phys_y},
                    "dragged_to": {"x": phys_x2, "y": phys_y2},
                },
                "error": None,
                "timed_out": False,
            }

        elif req.action == "scroll":
            if req.amount is None:
                return {
                    "success": False,
                    "data": None,
                    "error": "scroll requires amount",
                    "timed_out": False,
                }
            pyautogui.scroll(req.amount, x=phys_x, y=phys_y)
            return {
                "success": True,
                "data": {"scrolled_at": {"x": phys_x, "y": phys_y}, "amount": req.amount},
                "error": None,
                "timed_out": False,
            }

        else:
            return {
                "success": False,
                "data": None,
                "error": f"Unknown mouse action: {req.action!r}",
                "timed_out": False,
            }

    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
