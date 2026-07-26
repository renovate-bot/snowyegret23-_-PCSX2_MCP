"""Best-effort PCSX2 log discovery and filtering."""

from __future__ import annotations

import os
import re
from pathlib import Path

DOCUMENTS = Path.home() / "Documents" / "PCSX2"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))

DEFAULT_CANDIDATES = (
    DOCUMENTS / "logs" / "emulog.txt",
    DOCUMENTS / "logs" / "pcsx2.log",
    DOCUMENTS / "emulog.txt",
    LOCALAPPDATA / "PCSX2" / "logs" / "emulog.txt",
    LOCALAPPDATA / "PCSX2" / "pcsx2.log",
)

LEVEL_RE = re.compile(r"\b(error|warning|warn|info|debug|trace)\b", re.IGNORECASE)


def resolve_log_path(custom: str | None = None) -> Path:
    if custom:
        return Path(custom)
    for candidate in DEFAULT_CANDIDATES:
        if candidate.exists():
            return candidate
    return DEFAULT_CANDIDATES[0]


def _read_last_lines(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        data = b""
        chunk = 8192
        while size > 0 and data.count(b"\n") <= lines:
            take = min(chunk, size)
            size -= take
            f.seek(size)
            data = f.read(take) + data
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


def tail_log(lines: int = 100, path: str | None = None) -> dict:
    p = resolve_log_path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "lines": []}
    return {"path": str(p), "exists": True, "lines": _read_last_lines(p, lines)}


def filter_log(
    levels: tuple[str, ...] = ("error", "warning"),
    lines: int = 200,
    contains: str | None = None,
    path: str | None = None,
) -> dict:
    p = resolve_log_path(path)
    if not p.exists():
        return {"path": str(p), "exists": False, "lines": []}

    wanted = {level.lower() for level in levels}
    if "warning" in wanted:
        wanted.add("warn")
    needle = contains.lower() if contains else None

    kept: list[str] = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n")
            match = LEVEL_RE.search(line)
            if wanted and (not match or match.group(1).lower() not in wanted):
                continue
            if needle and needle not in line.lower():
                continue
            kept.append(line)

    return {"path": str(p), "exists": True, "total_matches": len(kept), "lines": kept[-lines:] if lines > 0 else kept}
