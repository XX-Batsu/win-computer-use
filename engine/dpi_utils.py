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
    "logical_left":    int,
    "logical_top":     int,
    "logical_right":   int,
    "logical_bottom":  int,
    "physical_left":   int,
    "physical_top":    int,
    "physical_right":  int,
    "physical_bottom": int,
    "scale":           float,
}
"""

import ctypes
import ctypes.wintypes
import logging
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DPI awareness — must be set before any Win32 display enumeration.
# PROCESS_PER_MONITOR_DPI_AWARE (2) ensures EnumDisplayMonitors returns
# logical coordinates and GetDpiForMonitor returns the true per-monitor DPI.
# ---------------------------------------------------------------------------
try:
    _shcore_init = ctypes.WinDLL("Shcore.dll")
    hr = _shcore_init.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    if hr == 0:
        logger.info("DPI awareness set to PROCESS_PER_MONITOR_DPI_AWARE")
    else:
        # E_ACCESSDENIED (0x80070005) means it was already set (e.g., via manifest)
        logger.debug("SetProcessDpiAwareness returned HRESULT 0x%08X (may already be set)", hr & 0xFFFFFFFF)
    _shcore = _shcore_init  # reuse the handle already opened above
except Exception:  # noqa: BLE001
    logger.warning("Could not set DPI awareness — using process default")
    _shcore = None

# Module-level cache
_monitor_map: List[Dict] = []


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

# EnumDisplayMonitors callback type
_MonitorEnumProc = ctypes.WINFUNCTYPE(
    ctypes.c_bool,
    ctypes.c_void_p,  # HMONITOR (pointer-sized handle)
    ctypes.c_void_p,  # HDC (pointer-sized handle)
    ctypes.POINTER(ctypes.wintypes.RECT),  # lprcMonitor (physical coords when called under PMA_V2)
    ctypes.wintypes.LPARAM,  # dwData
)

# SetThreadDpiAwarenessContext — available on Windows 10 1607+.
# We call EnumDisplayMonitors under DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 (-4)
# so the callback rect is guaranteed to be in physical pixels, regardless of the
# process-level DPI awareness set by the Python interpreter manifest.
try:
    _SetThreadDpiAwarenessContext = ctypes.windll.user32.SetThreadDpiAwarenessContext
    _SetThreadDpiAwarenessContext.restype = ctypes.c_ssize_t
    _SetThreadDpiAwarenessContext.argtypes = [ctypes.c_ssize_t]
except AttributeError:
    _SetThreadDpiAwarenessContext = None  # pre-1607 fallback

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

def _get_shcore():
    return _shcore


def _get_monitor_scale(hmonitor: int) -> float:
    """Return DPI scale for *hmonitor* (1.0 = 100 % / 96 DPI).

    Always returns >= 0.5 to prevent division-by-zero in downstream callers.
    """
    shcore = _get_shcore()
    if shcore is None:
        return 1.0
    try:
        dpi_x = ctypes.c_uint()
        dpi_y = ctypes.c_uint()
        # MDT_EFFECTIVE_DPI = 0
        hr = shcore.GetDpiForMonitor(hmonitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
        if hr == 0:  # S_OK
            scale = dpi_x.value / 96.0
            return max(scale, 0.5)  # guard against degenerate DPI values
    except Exception:  # noqa: BLE001
        pass
    return 1.0


# ---------------------------------------------------------------------------
# Map builder
# ---------------------------------------------------------------------------

def build_monitor_map() -> List[Dict]:
    """
    Enumerate all monitors and build the DPI map.

    Returns a list of dicts with keys:
        logical_left, logical_top, logical_right, logical_bottom,
        physical_left, physical_top, physical_right, physical_bottom, scale

    EnumDisplayMonitors is called under DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    so the callback rect is in physical pixels.  logical coords are derived by
    dividing physical coords by the per-monitor DPI scale (GetDpiForMonitor / 96).
    """
    entries: List[Dict] = []

    def _callback(hmonitor, _hdc, lp_rect, _data):
        rect = lp_rect.contents  # physical pixel rect under PMA_V2 context
        scale = _get_monitor_scale(hmonitor)

        phys_left   = rect.left
        phys_top    = rect.top
        phys_right  = rect.right
        phys_bottom = rect.bottom

        entries.append({
            "logical_left":   round(phys_left   / scale),
            "logical_top":    round(phys_top    / scale),
            "logical_right":  round(phys_right  / scale),
            "logical_bottom": round(phys_bottom / scale),
            "physical_left":  phys_left,
            "physical_top":   phys_top,
            "physical_right":  phys_right,
            "physical_bottom": phys_bottom,
            "scale":          scale,
        })
        return True  # continue enumeration

    cb = _MonitorEnumProc(_callback)

    if _SetThreadDpiAwarenessContext is not None:
        old_ctx = _SetThreadDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        try:
            ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)
        finally:
            _SetThreadDpiAwarenessContext(old_ctx)
    else:
        # Fallback for pre-Windows 10 1607: rect may not be physical pixels
        ctypes.windll.user32.EnumDisplayMonitors(None, None, cb, 0)

    if not entries:
        logger.warning("EnumDisplayMonitors returned no monitors; using fallback (1×1 at scale 1.0)")
        entries.append({
            "logical_left": 0, "logical_top": 0,
            "logical_right": 65535, "logical_bottom": 65535,
            "physical_left": 0, "physical_top": 0,
            "physical_right": 65535, "physical_bottom": 65535,
            "scale": 1.0,
        })

    logger.info("DPI map built: %d monitor(s)", len(entries))
    for i, m in enumerate(entries):
        logger.info(
            "  monitor %d: logical=(%d,%d)-(%d,%d) physical=(%d,%d)-(%d,%d) scale=%.2f",
            i,
            m["logical_left"], m["logical_top"],
            m["logical_right"], m["logical_bottom"],
            m["physical_left"], m["physical_top"],
            m["physical_right"], m["physical_bottom"],
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
            phys_x = m["physical_left"] + round((logical_x - m["logical_left"]) * scale)
            phys_y = m["physical_top"]  + round((logical_y - m["logical_top"])  * scale)
            return phys_x, phys_y, scale

    # Outside all monitors — fall back to the *nearest* monitor (by center distance)
    # to avoid large coordinate jumps at secondary monitor boundaries.
    logger.warning(
        "Coordinate (%d, %d) is outside all known monitors; using nearest monitor",
        logical_x, logical_y,
    )
    if not monitor_map:
        return logical_x, logical_y, 1.0

    def _center_dist(m: Dict) -> float:
        cx = (m["logical_left"] + m["logical_right"]) / 2
        cy = (m["logical_top"] + m["logical_bottom"]) / 2
        return (logical_x - cx) ** 2 + (logical_y - cy) ** 2

    nearest = min(monitor_map, key=_center_dist)
    scale = nearest["scale"]
    phys_x = nearest["physical_left"] + round((logical_x - nearest["logical_left"]) * scale)
    phys_y = nearest["physical_top"]  + round((logical_y - nearest["logical_top"])  * scale)
    return phys_x, phys_y, scale


def physical_to_logical(
    phys_x: int,
    phys_y: int,
    monitor_map: List[Dict],
) -> Tuple[int, int, float]:
    """
    Convert physical pixel coordinates to logical pixel coordinates.

    Returns (logical_x, logical_y, scale).

    Inverse of logical_to_physical(). If the point falls outside every
    known monitor rectangle the primary monitor's scale is used and a
    warning is logged.
    """
    for m in monitor_map:
        if (m["physical_left"] <= phys_x < m["physical_right"] and
                m["physical_top"] <= phys_y < m["physical_bottom"]):
            scale = m["scale"]
            logical_x = m["logical_left"] + round((phys_x - m["physical_left"]) / scale)
            logical_y = m["logical_top"]  + round((phys_y - m["physical_top"])  / scale)
            return logical_x, logical_y, scale

    # Outside all monitors — fall back to the *nearest* monitor
    logger.warning(
        "Physical coordinate (%d, %d) is outside all known monitors; using nearest monitor",
        phys_x, phys_y,
    )
    if not monitor_map:
        return phys_x, phys_y, 1.0

    def _center_dist(m: Dict) -> float:
        cx = (m["physical_left"] + m["physical_right"]) / 2
        cy = (m["physical_top"] + m["physical_bottom"]) / 2
        return (phys_x - cx) ** 2 + (phys_y - cy) ** 2

    nearest = min(monitor_map, key=_center_dist)
    scale = nearest["scale"]
    logical_x = nearest["logical_left"] + round((phys_x - nearest["physical_left"]) / scale)
    logical_y = nearest["logical_top"]  + round((phys_y - nearest["physical_top"])  / scale)
    return logical_x, logical_y, scale
