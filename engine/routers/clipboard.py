"""
routers/clipboard.py — GET /clipboard, POST /clipboard

Reads and writes the system clipboard via pyperclip.
Retries up to 3 times with 100 ms back-off on clipboard contention errors.
"""

import asyncio
from typing import Optional

import pyperclip
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()

_MAX_RETRIES = 3
_RETRY_DELAY = 0.1  # seconds


async def _clipboard_read_with_retry() -> str:
    last_err: Exception = RuntimeError("unknown clipboard error")
    for attempt in range(_MAX_RETRIES):
        try:
            return pyperclip.paste()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_DELAY)
    raise last_err


async def _clipboard_write_with_retry(text: str) -> None:
    last_err: Exception = RuntimeError("unknown clipboard error")
    for attempt in range(_MAX_RETRIES):
        try:
            pyperclip.copy(text)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_RETRY_DELAY)
    raise last_err


@router.get("/clipboard")
async def clipboard_read():
    try:
        text = await _clipboard_read_with_retry()
        return {
            "success": True,
            "data": {"text": text},
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


class ClipboardWriteRequest(BaseModel):
    text: str


@router.post("/clipboard")
async def clipboard_write(req: ClipboardWriteRequest):
    try:
        await _clipboard_write_with_retry(req.text)
        return {
            "success": True,
            "data": {"written": True},
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
