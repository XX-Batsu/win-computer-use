"""
routers/shell.py — POST /shell

Runs a shell command in a subprocess and returns stdout, stderr, returncode.

Security model / trust boundary:
- This endpoint allows execution of arbitrary shell commands.
- It is designed exclusively for local trusted callers (127.0.0.1 only).
  It must never be exposed to untrusted networks or remote callers.
- `shell=False` is used when spawning the subprocess, but this does NOT
  provide injection protection: cmd.exe (/c) and powershell (-Command) both
  parse and interpret metacharacters in the command string themselves.
  Any shell metacharacter (pipes, redirects, & ^ % etc.) present in
  req.command will be interpreted by the respective shell interpreter.
- ENGINE_SECRET is stripped from the child environment.
- A denylist + allowlist pattern filter on env_extra keys limits environment
  variable injection, but does not constitute a security boundary on its own.
- cwd is validated to be an existing absolute path.
"""

import os
import re
import subprocess
from typing import Dict, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

# Keys that callers must not inject via env_extra.
# This list is explicitly not exhaustive; the allowlist pattern below is the
# primary defence against malformed or dangerous key names.
_ENV_DENYLIST = {
    "APPDATA",
    "COMSPEC",
    "LOCALAPPDATA",
    "NODE_OPTIONS",
    "PATH",
    "PATHEXT",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "VIRTUAL_ENV",
}

# env_extra keys must match this pattern: all-uppercase ASCII letters, digits,
# and underscores, starting with a letter or underscore.  Keys that do not
# match are rejected with HTTP 400.
_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ShellRequest(BaseModel):
    command: str
    shell: Literal["cmd", "powershell"] = "cmd"
    cwd: Optional[str] = None
    env_extra: Optional[Dict[str, str]] = None
    timeout: int = Field(30, ge=1, le=300)


@router.post("/shell")
async def run_shell(req: ShellRequest):
    try:
        env_extra = req.env_extra or {}

        # Validate env_extra keys: allowlist pattern first, then denylist.
        invalid_pattern = [k for k in env_extra if not _ENV_KEY_PATTERN.match(k)]
        if invalid_pattern:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "data": None,
                    "error": (
                        f"env_extra keys must match ^[A-Z_][A-Z0-9_]*$: "
                        f"{sorted(invalid_pattern)}"
                    ),
                    "timed_out": False,
                },
            )

        blocked = _ENV_DENYLIST.intersection(env_extra.keys())
        if blocked:
            raise HTTPException(
                status_code=400,
                detail={
                    "success": False,
                    "data": None,
                    "error": f"env_extra contains blocked keys: {sorted(blocked)}",
                    "timed_out": False,
                },
            )

        # Validate cwd
        if req.cwd is not None:
            if not os.path.isabs(req.cwd):
                return {
                    "success": False,
                    "data": None,
                    "error": f"cwd must be an absolute path, got: {req.cwd!r}",
                    "timed_out": False,
                }
            if not os.path.isdir(req.cwd):
                return {
                    "success": False,
                    "data": None,
                    "error": f"cwd does not exist: {req.cwd!r}",
                    "timed_out": False,
                }

        # Build child environment: strip ENGINE_SECRET, then apply extras
        child_env = {k: v for k, v in os.environ.items() if k != "ENGINE_SECRET"}
        child_env.update(env_extra)

        # Build command argument list (never shell=True)
        if req.shell == "powershell":
            cmd_list = ["powershell", "-NoProfile", "-Command", req.command]
        else:
            cmd_list = ["cmd", "/c", req.command]

        result = subprocess.run(
            cmd_list,
            capture_output=True,
            text=True,
            cwd=req.cwd,
            env=child_env,
            timeout=req.timeout,
            shell=False,
        )

        return {
            "success": True,
            "data": {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            },
            "error": None,
            "timed_out": False,
        }

    except subprocess.TimeoutExpired as exc:
        if exc.process is not None:
            try:
                exc.process.kill()
                exc.process.communicate()  # 清理 pipe buffer
            except OSError:
                pass
        return {
            "success": False,
            "data": None,
            "error": "operation timed out",
            "timed_out": True,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "data": None,
            "error": str(e),
            "timed_out": False,
        }
