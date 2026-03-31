"""
dpi_utils.py — Per-monitor DPI map builder and logical→physical coordinate
               conversion utilities.

The monitor map is built at startup by enumerating monitors via the Win32
EnumDisplayMonitors API through ctypes.  Each entry stores both logical and
physical rectangle edges so that the correct per-monitor DPI scale can be
applied to any incoming logical coordinate.

Module-level state
------------------
_monitor_map : list[dict]
    Built once at startup, refreshed on POST /reload-dpi.

Map entry schema (all values are integers except scale which is float):
{
    "logical_left":   int,
    "logical_top":    int,
    "logical_right":  int,
    "logical_bottom": int,
    "physical_left":  int,
    "physical_top":   int,
    "scale":          float,
}
"""

import ctypes
import ctypes.wintypes
import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# Module-level cache
_monitor_map: List[Dict] = []


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

# EnumDisplayMonitors callback type
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_ulong,   # HMONITOR
    ctypes.c_ulong,   # HDC
    ctypes.POINTER(ctypes.wintypes.RECT),  # lprcMonitor (logical coords)
    ctypes.c_int64,   # dwData (LPARAM, 64-bit on 64-bit Windows)
)

_shcore = None

def _get_shcore():
    global _shcore
    if _shcore is None:
        try:
            _shcore = ctypes.WinDLL("Shcore.dll")
        except OSError:
            pass
    return _shcore


def _get_monitor_scale(hmonitor: int) -> float:
    """Return DPI scale for *hmonitor* (1.0 = 100 % / 96 DPI)."""
    shcore = _get_shcore()
    if shcore is None:
        return 1.0
    try:
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        # MDT_EFFECTIVE_DPI = 0
        hr = shcore.GetDpiForMonitor(hmonitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        if hr == 0:  # S_OK
            return dpi_x.value / 96.0
    except Exception:  # noqa: BLE001
        pass
    return 1.0


def _get_physical_rect(hmonitor: int) -> Tuple[int, int] | None:
    """Return (physical_left, physical_top) for *hmonitor* via MONITORINFO.

    Returns None if GetMonitorInfoW fails so callers can skip the monitor
    rather than using garbage data from an uninitialised buffer.
    """
    info = ctypes.create_string_buffer(40)  # MONITORINFO: cbSize=40
    ctypes.c_uint.from_buffer(info, 0).value = 40  # cbSize
    try:
        result = ctypes.windll.user32.GetMonitorInfoW(hmonitor, info)
        if not result:
            logger.warning("GetMonitorInfoW failed for hmonitor=%s", hmonitor)
            return None
        # rcMonitor is at offset 4: RECT { left, top, right, bottom } each LONG (4 bytes)
        left = ctypes.c_long.from_buffer_copy(info, 4).value
        top  = ctypes.c_long.from_buffer_copy(info, 8).value
        return left, top
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Map builder
# ---------------------------------------------------------------------------

def build_monitor_map() -> List[Dict]:
    """
    Enumerate all monitors and build the DPI map.

    Returns a list of dicts with keys:
        logical_left, logical_top, logical_right, logical_bottom,
        physical_left, physical_top, scale
    """
    entries: List[Dict] = []

    def _callback(hmonitor, _hdc, lp_rect, _data):
        rect = lp_rect.contents
        scale = _get_monitor_scale(hmonitor)
        phys_rect = _get_physical_rect(hmonitor)
        if phys_rect is None:
            logger.warning("Skipping monitor hmonitor=%s: could not retrieve physical rect", hmonitor)
            return True  # continue enumeration; skip this monitor
        phys_left, phys_top = phys_rect

        entries.append({
            "logical_left":   rect.left,
            "logical_top":    rect.top,
            "logical_right":  rect.right,
            "logical_bottom": rect.bottom,
            "physical_left":  phys_left,
            "physical_top":   phys_top,
            "scale":          scale,
        })
        return True  # continue enumeration

    cb = _MonitorEnumProc(_callback)
    ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)

    if not entries:
        logger.warning("EnumDisplayMonitors returned no monitors; using fallback (1×1 at scale 1.0)")
        entries.append({
            "logical_left": 0, "logical_top": 0,
            "logical_right": 65535, "logical_bottom": 65535,
            "physical_left": 0, "physical_top": 0,
            "scale": 1.0,
        })

    logger.info("DPI map built: %d monitor(s)", len(entries))
    for i, m in enumerate(entries):
        logger.info(
            "  monitor %d: logical=(%d,%d)-(%d,%d) physical=(%d,%d) scale=%.2f",
            i,
            m["logical_left"], m["logical_top"],
            m["logical_right"], m["logical_bottom"],
            m["physical_left"], m["physical_top"],
            m["scale"],
        )
    return entries


def reload_monitor_map() -> List[Dict]:
    """Rebuild and cache the monitor map. Returns the new map."""
    global _monitor_map
    _monitor_map = build_monitor_map()
    return _monitor_map


def get_monitor_map() -> List[Dict]:
    """Return the cached monitor map (built at startup)."""
    return _monitor_map


def get_primary_scale(monitor_map: List[Dict]) -> float:
    """Return the DPI scale of the primary (first) monitor."""
    if not monitor_map:
        return 1.0
    return monitor_map[0]["scale"]


# ---------------------------------------------------------------------------
# Coordinate conversion
# ---------------------------------------------------------------------------

def logical_to_physical(
    logical_x: int,
    logical_y: int,
    monitor_map: List[Dict],
) -> Tuple[int, int, float]:
    """
    Convert logical pixel coordinates to physical pixel coordinates.

    Returns (physical_x, physical_y, scale).

    If the point falls outside every known monitor rectangle the primary
    monitor's scale is used and a warning is logged.
    """
    for m in monitor_map:
        if (m["logical_left"] <= logical_x < m["logical_right"] and
                m["logical_top"] <= logical_y < m["logical_bottom"]):
            scale = m["scale"]
            phys_x = m["physical_left"] + int((logical_x - m["logical_left"]) * scale)
            phys_y = m["physical_top"]  + int((logical_y - m["logical_top"])  * scale)
            return phys_x, phys_y, scale

    # Outside all monitors — fall back to primary scale
    logger.warning(
        "Coordinate (%d, %d) is outside all known monitors; using primary scale",
        logical_x, logical_y,
    )
    primary = monitor_map[0] if monitor_map else {
        "physical_left": 0, "physical_top": 0,
        "logical_left": 0, "logical_top": 0,
        "scale": 1.0,
    }
    scale = primary["scale"]
    phys_x = primary["physical_left"] + int((logical_x - primary["logical_left"]) * scale)
    phys_y = primary["physical_top"]  + int((logical_y - primary["logical_top"])  * scale)
    return phys_x, phys_y, scale
