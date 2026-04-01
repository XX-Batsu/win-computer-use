"""
routers/mouse.py — POST /mouse

Supports actions: move, click, double_click, drag, scroll, mousedown, mouseup.
All input coordinates are logical pixels; converted to physical before calling
pyautogui (which operates in physical pixel space).
"""

import asyncio
import math
import time
from typing import Dict, List, Literal, Optional

import pyautogui
from fastapi import APIRouter
from pydantic import BaseModel, Field

from engine import dpi_utils

router = APIRouter()


class MouseRequest(BaseModel):
    action: Literal["move", "click", "double_click", "drag", "scroll", "mousedown", "mouseup"]
    x: Optional[int] = None
    y: Optional[int] = None
    button: Optional[str] = "left"
    x2: Optional[int] = None
    y2: Optional[int] = None
    amount: Optional[int] = None
    # Drag-specific parameters (ignored for other actions)
    duration: float = Field(0.5, ge=0.0, le=10.0)
    hold_before: float = Field(0.2, ge=0.0, le=5.0)
    steps: int = Field(20, ge=1, le=200)
    waypoints: Optional[List[Dict[str, int]]] = None


@router.post("/mouse")
async def mouse_action(req: MouseRequest):
    try:
        monitor_map = dpi_utils.get_monitor_map()

        if req.action == "move":
            if req.x is None or req.y is None:
                return {"success": False, "data": None, "error": "move requires x and y", "timed_out": False}
            phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
            pyautogui.moveTo(phys_x, phys_y)
            return {
                "success": True,
                "data": {"moved_to": {"x": phys_x, "y": phys_y}},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "click":
            if req.x is None or req.y is None:
                return {"success": False, "data": None, "error": "click requires x and y", "timed_out": False}
            phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
            button = req.button or "left"
            pyautogui.click(phys_x, phys_y, button=button)
            return {
                "success": True,
                "data": {"clicked_at": {"x": phys_x, "y": phys_y}, "button": button},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "double_click":
            if req.x is None or req.y is None:
                return {"success": False, "data": None, "error": "double_click requires x and y", "timed_out": False}
            phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
            button = req.button or "left"
            pyautogui.doubleClick(phys_x, phys_y, button=button)
            return {
                "success": True,
                "data": {"double_clicked_at": {"x": phys_x, "y": phys_y}, "button": button},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "drag":
            if req.x is None or req.y is None or req.x2 is None or req.y2 is None:
                return {
                    "success": False,
                    "data": None,
                    "error": "drag requires x, y, x2, and y2",
                    "timed_out": False,
                }
            button = req.button or "left"

            def _execute_drag():
                # Build logical point list: start → waypoints → end
                logical_points: List[tuple] = [(req.x, req.y)]
                if req.waypoints:
                    for wp in req.waypoints:
                        logical_points.append((wp["x"], wp["y"]))
                logical_points.append((req.x2, req.y2))

                # Convert all points to physical coords
                phys_points = []
                for lx, ly in logical_points:
                    px, py, _ = dpi_utils.logical_to_physical(lx, ly, monitor_map)
                    phys_points.append((px, py))

                # Allocate steps per segment proportional to Euclidean distance
                distances = [
                    math.sqrt(
                        (phys_points[i + 1][0] - phys_points[i][0]) ** 2
                        + (phys_points[i + 1][1] - phys_points[i][1]) ** 2
                    )
                    for i in range(len(phys_points) - 1)
                ]
                total_dist = sum(distances) or 1.0  # avoid div-by-zero for zero-length drag
                seg_steps = [max(1, round(req.steps * d / total_dist)) for d in distances]
                total_steps = sum(seg_steps)
                step_sleep = req.duration / total_steps if total_steps > 0 else 0.0

                pyautogui.moveTo(phys_points[0][0], phys_points[0][1])
                pyautogui.mouseDown(button=button)
                time.sleep(req.hold_before)

                for (sx, sy), (ex, ey), steps in zip(phys_points[:-1], phys_points[1:], seg_steps):
                    for i in range(1, steps + 1):
                        t = i / steps
                        pyautogui.moveTo(int(sx + (ex - sx) * t), int(sy + (ey - sy) * t))
                        time.sleep(step_sleep)

                time.sleep(0.1)
                pyautogui.mouseUp(button=button)

            await asyncio.to_thread(_execute_drag)
            return {
                "success": True,
                "data": {
                    "dragged_from": {"x": req.x, "y": req.y},
                    "dragged_to": {"x": req.x2, "y": req.y2},
                    "waypoints": len(req.waypoints) if req.waypoints else 0,
                },
                "error": None,
                "timed_out": False,
            }

        elif req.action == "scroll":
            if req.x is None or req.y is None:
                return {"success": False, "data": None, "error": "scroll requires x and y", "timed_out": False}
            if req.amount is None:
                return {"success": False, "data": None, "error": "scroll requires amount", "timed_out": False}
            phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
            pyautogui.scroll(req.amount, x=phys_x, y=phys_y)
            return {
                "success": True,
                "data": {"scrolled_at": {"x": phys_x, "y": phys_y}, "amount": req.amount},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "mousedown":
            if req.x is None or req.y is None:
                return {"success": False, "data": None, "error": "mousedown requires x and y", "timed_out": False}
            phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
            button = req.button or "left"
            pyautogui.moveTo(phys_x, phys_y)
            pyautogui.mouseDown(button=button)
            return {
                "success": True,
                "data": {"pressed_at": {"x": phys_x, "y": phys_y}, "button": button},
                "error": None,
                "timed_out": False,
            }

        elif req.action == "mouseup":
            button = req.button or "left"
            if req.x is not None and req.y is not None:
                phys_x, phys_y, _ = dpi_utils.logical_to_physical(req.x, req.y, monitor_map)
                pyautogui.moveTo(phys_x, phys_y)
            pyautogui.mouseUp(button=button)
            return {
                "success": True,
                "data": {"button": button},
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
