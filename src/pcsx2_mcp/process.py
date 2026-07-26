"""PCSX2 process helpers."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import psutil


def _hidden_startupinfo() -> subprocess.STARTUPINFO | None:
    startupinfo_type = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_type is None:
        return None
    startupinfo = startupinfo_type()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return startupinfo


def _matches(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return "pcsx2" in lowered


def find_processes() -> list[dict]:
    out: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"]):
        try:
            info = proc.info
            if _matches(info.get("name")) or _matches(info.get("exe")):
                out.append(
                    {
                        "pid": info["pid"],
                        "name": info.get("name"),
                        "exe": info.get("exe"),
                        "cmdline": info.get("cmdline"),
                        "created": info.get("create_time"),
                    }
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def terminate_processes(force: bool = False, timeout: float = 5.0) -> dict:
    procs: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if _matches(proc.info.get("name")) or _matches(proc.info.get("exe")):
                procs.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not procs:
        return {"killed": [], "failed": [], "note": "no PCSX2 processes found"}

    killed: list[int] = []
    failed: list[dict] = []

    for proc in procs:
        try:
            if force:
                proc.kill()
            else:
                proc.terminate()
        except psutil.NoSuchProcess:
            killed.append(proc.pid)
        except Exception as e:
            failed.append({"pid": proc.pid, "error": str(e)})

    deadline = time.monotonic() + timeout
    for proc in procs:
        if proc.pid in killed:
            continue
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            killed.append(proc.pid)
        except psutil.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2.0)
                killed.append(proc.pid)
            except Exception as e:
                failed.append({"pid": proc.pid, "error": f"kill escalation failed: {e}"})
        except psutil.NoSuchProcess:
            killed.append(proc.pid)
        except Exception as e:
            failed.append({"pid": proc.pid, "error": str(e)})

    return {"killed": killed, "failed": failed}


def launch(exe_path: str, game_path: str | None = None, extra_args: list[str] | None = None) -> dict:
    exe = Path(exe_path)
    if not exe.exists():
        return {"ok": False, "error": f"PCSX2 executable not found: {exe}"}

    args = [str(exe)]
    if game_path:
        args.append(str(Path(game_path)))
    if extra_args:
        args.extend(extra_args)

    log_dir = Path(__file__).resolve().parents[2] / "logs"
    log_dir.mkdir(exist_ok=True)
    log_path = log_dir / f"pcsx2-{time.strftime('%Y%m%d-%H%M%S')}.log"
    creationflags = 0
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            args,
            cwd=str(exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
            startupinfo=_hidden_startupinfo(),
        )
    finally:
        log.close()
    return {"ok": True, "pid": proc.pid, "args": args, "log_path": str(log_path)}
