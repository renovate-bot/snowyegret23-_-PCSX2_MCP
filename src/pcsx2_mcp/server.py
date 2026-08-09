"""MCP tools for PCSX2 GDB debugging and automation."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from . import background_input
from . import keyboard_input
from . import log_reader
from . import process as pcsx2_process
from . import window_capture
from .gdb_client import BP_TYPE_MAP, GDBClient, GDBError, REG_NAMES

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORTS = {"ee": 10501, "iop": 10502}

MEMORY_REGIONS: dict[str, tuple[str, int, int]] = {
    "ee_ram": ("ee", 0x00000000, 0x02000000),
    "ee_scratch": ("ee", 0x70000000, 0x00004000),
    "ee_bios": ("ee", 0x1FC00000, 0x00400000),
    "iop_ram_from_ee": ("ee", 0x1C000000, 0x00200000),
    "iop_ram": ("iop", 0x00000000, 0x00200000),
    "iop_bios": ("iop", 0x1FC00000, 0x00400000),
}

mcp = MCPServer("pcsx2-mcp")

_clients: dict[str, GDBClient] = {"ee": GDBClient("ee"), "iop": GDBClient("iop")}
_trace_workers: dict[str, "TraceWorker"] = {}
_rewind_workers: dict[str, "RewindWorker"] = {}
_memory_snapshots: dict[tuple[str, str, int, int], bytes] = {}
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_NATIVE_BUTTON_BINDINGS = {
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "triangle": "Triangle",
    "circle": "Circle",
    "cross": "Cross",
    "square": "Square",
    "select": "Select",
    "start": "Start",
    "l1": "L1",
    "l2": "L2",
    "l3": "L3",
    "r1": "R1",
    "r2": "R2",
    "r3": "R3",
}


def _ok(**kw: Any) -> dict[str, Any]:
    return {"ok": True, **kw}


def _err(message: str, **kw: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **kw}


def _native_input_client() -> tuple[str, GDBClient] | None:
    for target in ("ee", "iop"):
        client = _clients[target]
        if client.connected:
            return target, client
    return None


def _select_input_backend(backend: str) -> tuple[str, str | None]:
    requested = backend.strip().lower()
    if requested not in {"auto", "native", "keyboard", "xinput"}:
        raise ValueError("backend must be 'auto', 'native', 'keyboard', or 'xinput'")
    native = _native_input_client()
    if requested == "native":
        if native is None:
            raise GDBError("native input requires an active PCSX2 GDB connection")
        return "native", None
    if requested == "auto" and native is not None:
        return "native", None
    selected, fallback_reason = background_input.choose_backend(requested)
    if requested == "auto" and fallback_reason is None:
        fallback_reason = "native input unavailable because no PCSX2 GDB client is connected"
    elif requested == "auto":
        fallback_reason = (
            "native input unavailable because no PCSX2 GDB client is connected; "
            f"{fallback_reason}"
        )
    return selected, fallback_reason


def _native_input_set(controller: int, binding: str, value: float) -> tuple[str, str]:
    if controller < 0:
        raise ValueError("controller must be non-negative")
    if not 0.0 <= value <= 1.0:
        raise ValueError("native input value must be between 0.0 and 1.0")
    native = _native_input_client()
    if native is None:
        raise GDBError("native input requires an active PCSX2 GDB connection")
    target, client = native
    value_milli = int(round(value * 1000.0))
    reply = client.pcsx2_command(f"input_set {controller} {binding} {value_milli}")
    if not reply.startswith("OK"):
        raise GDBError(reply)
    return target, reply


def _native_button_name(button: str) -> tuple[str, str]:
    normalized = background_input.normalize_button(button)
    binding = _NATIVE_BUTTON_BINDINGS.get(normalized)
    if binding is None:
        raise ValueError(f"button {button!r} is not available on a DualShock 2 controller")
    return normalized, binding


def _native_analog_values(x: float, y: float, stick: str) -> dict[str, float]:
    side = stick.strip().lower()
    if side not in {"left", "right"}:
        raise ValueError("stick must be 'left' or 'right'")
    if not -1.0 <= x <= 1.0 or not -1.0 <= y <= 1.0:
        raise ValueError("x and y must be between -1.0 and 1.0")
    prefix = "L" if side == "left" else "R"
    return {
        f"{prefix}Left": max(0.0, -x),
        f"{prefix}Right": max(0.0, x),
        f"{prefix}Up": max(0.0, y),
        f"{prefix}Down": max(0.0, -y),
    }


def _target(target: str) -> str:
    target = target.lower()
    if target not in _clients:
        raise ValueError("target must be 'ee' or 'iop'")
    return target


def _client(target: str) -> GDBClient:
    return _clients[_target(target)]


def _parse_addr(value: int | str) -> int:
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = value.strip()
    return int(text, 16 if text.lower().startswith("0x") else 0) & 0xFFFFFFFF


def _format_registers(registers: dict[str, int]) -> dict[str, str]:
    return {name: f"0x{value:08X}" for name, value in registers.items()}


def _hexdump(data: bytes, base: int) -> str:
    lines: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        left = " ".join(f"{byte:02x}" for byte in chunk[:8])
        right = " ".join(f"{byte:02x}" for byte in chunk[8:])
        if right:
            hex_part = f"{left:<23}  {right:<23}"
        else:
            hex_part = f"{left:<23}  {'':<23}"
        ascii_part = "".join(chr(byte) if 0x20 <= byte < 0x7F else "." for byte in chunk)
        lines.append(f"{base + offset:08X}  {hex_part}  |{ascii_part}|")
    return "\n".join(lines)


def _u32_array(data: bytes) -> list[str]:
    return [f"0x{int.from_bytes(data[offset:offset + 4], 'little'):08x}" for offset in range(0, len(data) - 3, 4)]


def _parse_pattern(pattern: str) -> list[int | None]:
    compact = re.sub(r"\s+", "", pattern)
    if len(compact) % 2:
        raise ValueError("pattern must contain full bytes")
    out: list[int | None] = []
    for offset in range(0, len(compact), 2):
        part = compact[offset : offset + 2]
        if part == "??":
            out.append(None)
            continue
        try:
            out.append(int(part, 16))
        except ValueError as e:
            raise ValueError(f"invalid pattern byte: {part!r}") from e
    if not out:
        raise ValueError("pattern must not be empty")
    return out


def _find_pattern_offsets(data: bytes, pattern: list[int | None], base: int) -> list[int]:
    if not pattern or len(pattern) > len(data):
        return []
    matches: list[int] = []
    last_start = len(data) - len(pattern)
    for offset in range(last_start + 1):
        for index, expected in enumerate(pattern):
            if expected is not None and data[offset + index] != expected:
                break
        else:
            matches.append(base + offset)
    return matches


def _diff_memory(before: bytes, after: bytes, base: int) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    limit = min(len(before), len(after))
    for offset in range(limit):
        if before[offset] == after[offset]:
            continue
        changes.append(
            {
                "offset": offset,
                "address": f"0x{base + offset:08X}",
                "before": f"0x{before[offset]:02x}",
                "after": f"0x{after[offset]:02x}",
            }
        )
    return changes


def _encode_debug_text_argument(text: str) -> str:
    return text.encode("utf-8").hex()


def _default_screenshot_path(now: float | None = None) -> Path:
    timestamp = time.time() if now is None else now
    when = time.localtime(timestamp)
    millis = int((timestamp - int(timestamp)) * 1000)
    stamp = f"{time.strftime('%Y%m%d-%H%M%S', when)}-{millis:03d}"
    return _PROJECT_ROOT / "logs" / "screenshots" / f"pcsx2-{stamp}.png"


def _prepare_screenshot_path(file_path: str | None) -> Path:
    if file_path is None or not file_path.strip():
        out = _default_screenshot_path()
    else:
        out = Path(file_path).expanduser()
        if not out.suffix:
            out = out.with_suffix(".png")

    if out.suffix.lower() not in _SCREENSHOT_EXTENSIONS:
        valid = ", ".join(sorted(_SCREENSHOT_EXTENSIONS))
        raise ValueError(f"unsupported screenshot extension {out.suffix!r}; valid: {valid}")

    out.parent.mkdir(parents=True, exist_ok=True)
    return out.resolve()


def _parse_debug_header(line: str) -> dict[str, str]:
    if line.startswith("ERR"):
        raise GDBError(line)
    if not line.startswith("OK"):
        raise GDBError(f"unexpected debug reply: {line}")
    values: dict[str, str] = {}
    for part in line.split()[1:]:
        if "=" in part:
            key, value = part.split("=", 1)
            values[key] = value
    return values


def _debug_reply_lines(reply: str) -> list[str]:
    lines = reply.splitlines()
    if not lines:
        raise GDBError("empty debug reply")
    _parse_debug_header(lines[0])
    return lines


def _parse_debug_registers(reply: str) -> dict[str, Any]:
    lines = _debug_reply_lines(reply)
    header = _parse_debug_header(lines[0])
    parsed: dict[str, Any] = {
        "pc": header.get("pc"),
        "cycles": int(header.get("cycles", "0")),
        "hi": None,
        "lo": None,
        "registers": [],
    }
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == "HI":
            parsed["hi"] = parts[1]
        elif len(parts) == 2 and parts[0] == "LO":
            parsed["lo"] = parts[1]
        elif len(parts) >= 8 and parts[0] == "REG":
            parsed["registers"].append(
                {
                    "category": int(parts[1]),
                    "category_name": parts[2],
                    "bits": int(parts[3]),
                    "index": int(parts[4]),
                    "name": parts[5],
                    "value": parts[6],
                    "display": parts[7],
                }
            )
    return parsed


def _parse_screenshot_reply(reply: str) -> dict[str, Any]:
    lines = _debug_reply_lines(reply)
    header = _parse_debug_header(lines[0])
    parsed: dict[str, Any] = {
        "width": int(header.get("width", "0")),
        "height": int(header.get("height", "0")),
        "raw_path": None,
    }
    for line in lines[1:]:
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0] == "PATH":
            parsed["raw_path"] = parts[1]
    return parsed


def _parse_debug_disassembly(reply: str) -> list[dict[str, str]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, str]] = []
    for line in lines[1:]:
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"address": parts[0], "opcode": parts[1], "disasm": parts[2]})
    return out


def _parse_debug_breakpoints(reply: str) -> list[dict[str, Any]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        while len(parts) < 7:
            parts.append("")
        out.append(
            {
                "address": parts[0],
                "enabled": parts[1] == "1",
                "temporary": parts[2] == "1",
                "stepping": parts[3] == "1",
                "has_condition": parts[4] == "1",
                "condition": parts[5],
                "description": parts[6],
            }
        )
    return out


def _parse_debug_watchpoints(reply: str) -> list[dict[str, Any]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        while len(parts) < 11:
            parts.append("")
        out.append(
            {
                "start": parts[0],
                "end": parts[1],
                "condition_type": int(parts[2] or "0"),
                "result_type": int(parts[3] or "0"),
                "has_condition": parts[4] == "1",
                "condition": parts[5],
                "description": parts[6],
                "hits": int(parts[7] or "0"),
                "last_pc": parts[8],
                "last_address": parts[9],
                "last_size": int(parts[10] or "0"),
            }
        )
    return out


def _parse_debug_threads(reply: str) -> list[dict[str, Any]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 7:
            out.append(
                {
                    "id": int(parts[0]),
                    "pc": parts[1],
                    "status": int(parts[2]),
                    "wait": int(parts[3]),
                    "wait_id": int(parts[4]),
                    "entry": parts[5],
                    "priority": int(parts[6]),
                }
            )
    return out


def _parse_debug_modules(reply: str) -> list[dict[str, Any]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) >= 2:
            out.append({"name": parts[0], "version": int(parts[1])})
    return out


def _parse_debug_backtrace(reply: str) -> list[dict[str, Any]]:
    lines = _debug_reply_lines(reply)
    out: list[dict[str, Any]] = []
    for line in lines[1:]:
        parts = line.split("\t", 4)
        if len(parts) >= 5:
            out.append({"entry": parts[0], "pc": parts[1], "sp": parts[2], "stack_size": int(parts[3]), "disasm": parts[4]})
    return out


def _pcsx2_debug_command(target: str, command: str) -> str:
    reply = _client(target).pcsx2_command(command)
    _debug_reply_lines(reply)
    return reply


def _config_candidates() -> list[Path]:
    docs = Path.home() / "Documents" / "PCSX2"
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "PCSX2"
    return [docs / "PCSX2.ini", docs / "inis" / "PCSX2.ini", local / "PCSX2.ini"]


def _background_input_context(pid: int | None, config_path: str | None) -> tuple[dict[str, Any], Path | None]:
    processes = pcsx2_process.find_processes() if pid is None else [{"pid": pid}]
    if not processes:
        raise ValueError("no PCSX2 process found")
    window = window_capture.find_window([int(process["pid"]) for process in processes])
    input_window = window_capture.find_input_window(int(window["hwnd"], 16))
    path = Path(config_path) if config_path else next(
        (candidate for candidate in background_input.default_config_candidates() if candidate.exists()),
        None,
    )
    return input_window, path


def _write_simple_ini_values(path: Path, section: str, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.exists() else []
    out: list[str] = []
    in_section = False
    seen_section = False
    written = set()

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section:
                for key, value in values.items():
                    if key not in written:
                        out.append(f"{key} = {value}")
                written.clear()
            in_section = stripped[1:-1] == section
            seen_section = seen_section or in_section
            out.append(line)
            continue

        if in_section and "=" in line:
            key = line.split("=", 1)[0].strip()
            if key in values:
                out.append(f"{key} = {values[key]}")
                written.add(key)
            else:
                out.append(line)
        else:
            out.append(line)

    if in_section:
        for key, value in values.items():
            if key not in written:
                out.append(f"{key} = {value}")
    elif not seen_section:
        if out and out[-1].strip():
            out.append("")
        out.append(f"[{section}]")
        for key, value in values.items():
            out.append(f"{key} = {value}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="")


@dataclass
class TraceWorker:
    trace_id: str
    target: str
    interval_ms: int
    registers: list[str] = field(default_factory=list)
    memory_ranges: list[dict[str, Any]] = field(default_factory=list)
    output_path: str | None = None
    max_samples: int = 0
    samples: int = 0
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"pcsx2-trace-{self.trace_id}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                record = sample_state(self.target, self.registers, self.memory_ranges)
                record["trace_id"] = self.trace_id
                if self.output_path:
                    out = Path(self.output_path)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with out.open("a", encoding="utf-8", newline="") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                self.samples += 1
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
            if self.max_samples and self.samples >= self.max_samples:
                break
            self.stop_event.wait(max(0.01, self.interval_ms / 1000.0))


@dataclass
class RewindWorker:
    rewind_id: str
    target: str
    interval_sec: float
    slots: int
    directory: str
    captures: list[dict[str, Any]] = field(default_factory=list)
    total_captures: int = 0
    last_error: str | None = None
    started_at: float = field(default_factory=time.time)
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"pcsx2-rewind-{self.rewind_id}", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=10.0)

    def _run(self) -> None:
        Path(self.directory).mkdir(parents=True, exist_ok=True)
        while not self.stop_event.is_set():
            try:
                client = _client(self.target)
                serial = _current_serial(self.target) or self.rewind_id
                safe_time = time.strftime("%H-%M-%S", time.localtime())
                display_name = f"{serial}_{safe_time.replace('-', ':')}"
                path = Path(self.directory) / f"{serial}_{safe_time}.p2s"
                client.pcsx2_command(f"save_file {path}", timeout=90.0)
                client.pcsx2_command("wait_savestate_flush", timeout=90.0)
                capture = {"name": display_name, "path": str(path), "time": time.time()}
                self.captures.append(capture)
                while len(self.captures) > self.slots:
                    old = self.captures.pop(0)
                    try:
                        Path(old["path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
                self.total_captures += 1
                self.last_error = None
            except Exception as e:
                self.last_error = str(e)
            self.stop_event.wait(max(0.25, self.interval_sec))


def sample_state(target: str, registers: list[str] | None = None, memory_ranges: list[dict[str, Any]] | None = None) -> dict:
    client = _client(target)
    record: dict[str, Any] = {"time": time.time(), "target": target}
    if registers:
        regs = client.get_registers()
        record["registers"] = {name: f"0x{regs[name]:08X}" for name in registers if name in regs}
    if memory_ranges:
        dumps: dict[str, Any] = {}
        for item in memory_ranges:
            label = str(item.get("label") or item.get("address"))
            address = _parse_addr(item["address"])
            length = int(item["length"])
            data = client.read_memory_chunked(address, length)
            dumps[label] = {"address": f"0x{address:08X}", "length": len(data), "hex": data.hex()}
        record["memory"] = dumps
    return record


def _current_serial(target: str) -> str | None:
    try:
        reply = _client(target).pcsx2_command("status", timeout=10.0)
    except Exception:
        return None
    for part in reply.split():
        if part.startswith("serial="):
            serial = part.split("=", 1)[1].strip()
            return serial or None
    return None


@mcp.tool()
def connect(target: str = "ee", port: int | None = None, host: str = DEFAULT_HOST, auto_resume: bool = False) -> dict:
    """Connect to PCSX2's EE or IOP GDB server. PCSX2 pauses the VM on connect."""
    try:
        tgt = _target(target)
        client = _client(tgt)
        client.connect(host=host, port=port or DEFAULT_PORTS[tgt])
        if auto_resume:
            client.resume()
    except (OSError, GDBError, ValueError) as e:
        return _err(f"connect failed: {e}", hint="Enable EmuCore EnableEEGDBServer/EnableIOPGDBServer and restart or reload settings.")
    return _ok(**client.status())


@mcp.tool()
def disconnect(target: str = "ee") -> dict:
    """Disconnect one GDB target."""
    try:
        _client(target).disconnect()
    except ValueError as e:
        return _err(str(e))
    return _ok(**_client(target).status())


@mcp.tool()
def status() -> dict:
    """Return MCP-side connection status for both EE and IOP."""
    return _ok(targets={name: client.status() for name, client in _clients.items()})


@mcp.tool()
def pcsx2_status(target: str = "ee") -> dict:
    """Ask PCSX2 for VM status, title, serial, and CRCs via qPcsx2."""
    try:
        reply = _client(target).pcsx2_command("status")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def pause(target: str = "ee") -> dict:
    """Interrupt/pause the VM through GDB Ctrl-C."""
    try:
        reply = _client(target).pause()
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply, **_client(target).status())


@mcp.tool()
def resume(target: str = "ee") -> dict:
    """Resume the VM."""
    try:
        _client(target).resume()
    except (GDBError, ValueError) as e:
        return _err(str(e))
    return _ok(**_client(target).status())


@mcp.tool()
def step(target: str = "ee") -> dict:
    """Single-step one guest instruction and stop again."""
    try:
        reply = _client(target).step()
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply, **_client(target).status())


@mcp.tool()
def reset(target: str = "ee") -> dict:
    """Request a VM reset."""
    try:
        reply = _client(target).pcsx2_command("reset", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def frame_advance(frames: int = 1, target: str = "ee") -> dict:
    """Advance the VM by one or more frames and pause."""
    try:
        reply = _client(target).pcsx2_command(f"frameadvance {int(frames)}", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def save_state_slot(slot: int, target: str = "ee") -> dict:
    """Save a PCSX2 savestate slot synchronously."""
    try:
        reply = _client(target).pcsx2_command(f"save_slot {int(slot)}", timeout=60.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def load_state_slot(slot: int, target: str = "ee", backup: bool = False) -> dict:
    """Load a PCSX2 savestate slot."""
    command = "load_backup_slot" if backup else "load_slot"
    try:
        reply = _client(target).pcsx2_command(f"{command} {int(slot)}", timeout=60.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def save_state_file(file_path: str, target: str = "ee") -> dict:
    """Save a savestate to an explicit file path."""
    try:
        reply = _client(target).pcsx2_command(f"save_file {file_path}", timeout=60.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def load_state_file(file_path: str, target: str = "ee") -> dict:
    """Load a savestate from an explicit file path."""
    try:
        reply = _client(target).pcsx2_command(f"load_file {file_path}", timeout=60.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def wait_savestate_flush(target: str = "ee") -> dict:
    """Wait until async savestate compression has flushed."""
    try:
        reply = _client(target).pcsx2_command("wait_savestate_flush", timeout=60.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


@mcp.tool()
def take_screenshot(file_path: str | None = None, target: str = "ee") -> dict:
    """Save the current PCSX2 render frame to an image file, even if the emulator window is inactive or minimized."""
    try:
        out = _prepare_screenshot_path(file_path)
        if not _client(target).connected:
            if out.suffix.lower() != ".png":
                return _err("the disconnected inactive-window fallback supports PNG output only", path=str(out))
            result = capture_pcsx2_window(str(out))
            return {**result, "target": target, "fallback": "inactive-window"}
        encoded_path = _encode_debug_text_argument(str(out))
        reply = _client(target).pcsx2_command(f"screenshot_file {encoded_path}", timeout=90.0)
        parsed = _parse_screenshot_reply(reply)
        if not out.exists():
            return _err("screenshot command succeeded but the file was not created", path=str(out), reply=reply, **parsed)
        size = out.stat().st_size
    except (OSError, GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(
        target=target,
        path=str(out),
        exists=True,
        file_size=size,
        reply=reply,
        method="qPcsx2 screenshot_file",
        foreground_activation=False,
        **parsed,
    )


@mcp.tool()
def get_registers(target: str = "ee") -> dict:
    """Return guest registers for EE or IOP."""
    try:
        regs = _client(target).get_registers()
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(registers=regs, hex=_format_registers(regs))


@mcp.tool()
def write_register(register: str, value: int | str, target: str = "ee") -> dict:
    """Overwrite one register by name or numeric index."""
    try:
        val = _parse_addr(value)
        index_or_name: str | int = int(register) if register.isdigit() else register
        _client(target).write_register(index_or_name, val)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, register=register, value=f"0x{val:08X}")


@mcp.tool()
def get_debug_registers(target: str = "ee", category: int = -1) -> dict:
    """Return full-width PCSX2 debug registers through qPcsx2."""
    try:
        command = "debug_registers" if int(category) < 0 else f"debug_registers {int(category)}"
        reply = _pcsx2_debug_command(target, command)
        parsed = _parse_debug_registers(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, raw=reply, **parsed)


@mcp.tool()
def write_debug_register(category: int, index: int, value: int | str, target: str = "ee") -> dict:
    """Write a full-width PCSX2 debug register by category and index."""
    try:
        if isinstance(value, int):
            value_text = f"0x{value:x}"
        else:
            value_text = value.strip()
        reply = _pcsx2_debug_command(target, f"debug_write_register {int(category)} {int(index)} {value_text}")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, reply=reply)


@mcp.tool()
def set_pc(address: int | str, target: str = "ee") -> dict:
    """Set the current program counter through PCSX2's debug interface."""
    try:
        addr = _parse_addr(address)
        reply = _pcsx2_debug_command(target, f"debug_set_pc 0x{addr:08x}")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", reply=reply)


@mcp.tool()
def disassemble(address: int | str, count: int = 20, target: str = "ee") -> dict:
    """Disassemble guest instructions using PCSX2's native debugger disassembler."""
    try:
        addr = _parse_addr(address)
        qty = max(1, min(500, int(count)))
        reply = _pcsx2_debug_command(target, f"debug_disasm 0x{addr:08x} {qty}")
        instructions = _parse_debug_disassembly(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", count=len(instructions), instructions=instructions, raw=reply)


@mcp.tool()
def evaluate(expression: str, target: str = "ee") -> dict:
    """Evaluate a PCSX2 debugger expression."""
    try:
        reply = _pcsx2_debug_command(target, f"debug_eval {expression}")
        values = _parse_debug_header(reply.splitlines()[0])
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, expression=expression, reply=reply, **values)


@mcp.tool()
def read_memory(address: int | str, length: int, target: str = "ee", as_format: str = "hex") -> dict:
    """Read guest memory as hex, base64, ascii, hexdump, or u32_array."""
    try:
        addr = _parse_addr(address)
        data = _client(target).read_memory_chunked(addr, int(length))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))

    payload: dict[str, Any] = {"target": target, "address": f"0x{addr:08X}", "length": len(data)}
    if as_format == "hex":
        payload["hex"] = data.hex()
    elif as_format == "base64":
        payload["base64"] = base64.b64encode(data).decode("ascii")
    elif as_format == "ascii":
        payload["ascii"] = data.decode("ascii", errors="replace")
    elif as_format == "hexdump":
        payload["hexdump"] = _hexdump(data, addr)
    elif as_format == "u32_array":
        payload["u32_array"] = _u32_array(data)
    else:
        return _err(f"unknown as_format: {as_format}")
    return _ok(**payload)


@mcp.tool()
def read_string(address: int | str, max_length: int = 256, target: str = "ee", encoding: str = "ascii") -> dict:
    """Read a null-terminated guest string."""
    try:
        addr = _parse_addr(address)
        data = _client(target).read_memory_chunked(addr, max(1, int(max_length)))
        end = data.find(b"\x00")
        raw = data[: end if end >= 0 else len(data)]
        text = raw.decode(encoding, errors="replace")
    except (LookupError, GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", string=text, length=len(text), raw_length=len(raw))


@mcp.tool()
def write_memory(address: int | str, data_hex: str, target: str = "ee") -> dict:
    """Overwrite guest memory with hex bytes."""
    try:
        addr = _parse_addr(address)
        raw = bytes.fromhex(data_hex.replace(" ", ""))
        _client(target).write_memory(addr, raw)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", written=len(raw))


@mcp.tool()
def overwrite_memory_from_file(address: int | str, file_path: str, target: str = "ee", max_bytes: int | None = None) -> dict:
    """Overwrite guest memory with bytes read from a local file."""
    try:
        addr = _parse_addr(address)
        data = Path(file_path).read_bytes()
        if max_bytes is not None:
            data = data[: int(max_bytes)]
        _client(target).write_memory(addr, data)
    except (OSError, GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", source=file_path, written=len(data))


@mcp.tool()
def dump_memory(address: int | str, length: int, file_path: str, target: str = "ee") -> dict:
    """Dump guest memory to a local file."""
    try:
        addr = _parse_addr(address)
        data = _client(target).read_memory_chunked(addr, int(length))
        out = Path(file_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    except (OSError, GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", length=len(data), path=str(out.resolve()))


@mcp.tool()
def dump_region(region: str, file_path: str) -> dict:
    """Dump a named memory region. Use list_memory_regions for names."""
    if region not in MEMORY_REGIONS:
        return _err(f"unknown region {region!r}; valid: {list(MEMORY_REGIONS)}")
    target, address, length = MEMORY_REGIONS[region]
    return dump_memory(address, length, file_path, target)


@mcp.tool()
def list_memory_regions() -> dict:
    """List predefined dump regions."""
    return _ok(regions={name: {"target": target, "address": f"0x{addr:08X}", "length": length} for name, (target, addr, length) in MEMORY_REGIONS.items()})


@mcp.tool()
def find_pattern(
    pattern: str,
    start: int | str = 0x00100000,
    end: int | str = 0x02000000,
    target: str = "ee",
    max_results: int = 20,
    chunk_size: int = 0x4000,
) -> dict:
    """Search guest memory for a hex pattern. Use ?? as a wildcard byte."""
    try:
        pat = _parse_pattern(pattern)
        start_addr = _parse_addr(start)
        end_addr = _parse_addr(end)
        if end_addr <= start_addr:
            return _err("end must be greater than start")
        max_hits = max(1, int(max_results))
        chunk = max(len(pat), min(max(0x100, int(chunk_size)), 0x100000))
        overlap = max(0, len(pat) - 1)
        client = _client(target)
        hits: list[int] = []
        skipped_chunks = 0
        cur = start_addr
        while cur < end_addr and len(hits) < max_hits:
            take = min(chunk, end_addr - cur)
            read_len = min(take + overlap, end_addr - cur)
            try:
                data = client.read_memory_chunked(cur, read_len)
            except (GDBError, TimeoutError):
                skipped_chunks += 1
                cur = (cur + take) & 0xFFFFFFFF
                continue
            for hit in _find_pattern_offsets(data, pat, cur):
                if hit >= cur + take:
                    continue
                hits.append(hit)
                if len(hits) >= max_hits:
                    break
            cur = (cur + take) & 0xFFFFFFFF
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(
        target=target,
        pattern=pattern,
        start=f"0x{start_addr:08X}",
        end=f"0x{end_addr:08X}",
        matches=[f"0x{hit:08X}" for hit in hits],
        count=len(hits),
        truncated=len(hits) >= max_hits,
        skipped_chunks=skipped_chunks,
    )


@mcp.tool()
def memory_diff(address: int | str, length: int, name: str = "default", target: str = "ee", reset: bool = False) -> dict:
    """Snapshot and diff a guest memory range. First call saves, second call compares and clears."""
    try:
        addr = _parse_addr(address)
        size = max(1, int(length))
        key = (_target(target), name, addr, size)
        if reset:
            _memory_snapshots.pop(key, None)
        data = _client(target).read_memory_chunked(addr, size)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))

    previous = _memory_snapshots.pop(key, None)
    if previous is None:
        _memory_snapshots[key] = data
        return _ok(target=target, name=name, address=f"0x{addr:08X}", length=len(data), snapshot_saved=True)

    changes = _diff_memory(previous, data, addr)
    return _ok(
        target=target,
        name=name,
        address=f"0x{addr:08X}",
        length=len(data),
        snapshot_saved=False,
        changed=len(changes),
        changes=changes,
    )


@mcp.tool()
def set_breakpoint(address: int | str, kind: str = "exec", target: str = "ee", size: int = 4) -> dict:
    """Set an exec/read/write/rw breakpoint or watchpoint."""
    try:
        addr = _parse_addr(address)
        _client(target).set_breakpoint(addr, kind, int(size))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", kind=kind, size=size)


@mcp.tool()
def remove_breakpoint(address: int | str, kind: str = "exec", target: str = "ee", size: int = 4) -> dict:
    """Remove a breakpoint or watchpoint."""
    try:
        addr = _parse_addr(address)
        _client(target).remove_breakpoint(addr, kind, int(size))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", kind=kind, size=size)


@mcp.tool()
def list_breakpoint_kinds() -> dict:
    """Return accepted breakpoint kind names and RSP types."""
    return _ok(kinds=dict(BP_TYPE_MAP))


@mcp.tool()
def set_debug_breakpoint(
    address: int | str,
    target: str = "ee",
    condition: str | None = None,
    temporary: bool = False,
    enabled: bool = True,
) -> dict:
    """Set a PCSX2 debugger breakpoint with an optional expression condition."""
    try:
        addr = _parse_addr(address)
        parts = [f"debug_set_breakpoint 0x{addr:08x}", "1" if temporary else "0", "1" if enabled else "0"]
        if condition:
            parts.append(_encode_debug_text_argument(condition))
        reply = _pcsx2_debug_command(target, " ".join(parts))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", condition=condition, reply=reply)


@mcp.tool()
def remove_debug_breakpoint(address: int | str, target: str = "ee") -> dict:
    """Remove a PCSX2 debugger breakpoint."""
    try:
        addr = _parse_addr(address)
        reply = _pcsx2_debug_command(target, f"debug_remove_breakpoint 0x{addr:08x}")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", reply=reply)


@mcp.tool()
def list_debug_breakpoints(target: str = "ee") -> dict:
    """List PCSX2 debugger breakpoints and conditions."""
    try:
        reply = _pcsx2_debug_command(target, "debug_list_breakpoints")
        breakpoints = _parse_debug_breakpoints(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, breakpoints=breakpoints, raw=reply)


@mcp.tool()
def set_watchpoint(
    address: int | str,
    size: int = 4,
    target: str = "ee",
    kind: str = "write",
    action: str = "break",
    condition: str | None = None,
) -> dict:
    """Set a PCSX2 debugger memory watchpoint."""
    try:
        addr = _parse_addr(address)
        end = (addr + max(1, int(size))) & 0xFFFFFFFF
        parts = [f"debug_set_watchpoint 0x{addr:08x}", f"0x{end:08x}", kind, action]
        if condition:
            parts.append(_encode_debug_text_argument(condition))
        reply = _pcsx2_debug_command(target, " ".join(parts))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", end=f"0x{end:08X}", kind=kind, action=action, condition=condition, reply=reply)


@mcp.tool()
def remove_watchpoint(address: int | str, size: int = 4, target: str = "ee") -> dict:
    """Remove a PCSX2 debugger memory watchpoint."""
    try:
        addr = _parse_addr(address)
        end = (addr + max(1, int(size))) & 0xFFFFFFFF
        reply = _pcsx2_debug_command(target, f"debug_remove_watchpoint 0x{addr:08x} 0x{end:08x}")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, address=f"0x{addr:08X}", end=f"0x{end:08X}", reply=reply)


@mcp.tool()
def list_watchpoints(target: str = "ee") -> dict:
    """List PCSX2 debugger memory watchpoints."""
    try:
        reply = _pcsx2_debug_command(target, "debug_list_watchpoints")
        watchpoints = _parse_debug_watchpoints(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, watchpoints=watchpoints, raw=reply)


@mcp.tool()
def clear_debug_breakpoints(target: str = "ee") -> dict:
    """Clear all PCSX2 debugger breakpoints and watchpoints."""
    try:
        reply = _pcsx2_debug_command(target, "debug_clear_breakpoints")
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, reply=reply)


@mcp.tool()
def get_threads(target: str = "ee") -> dict:
    """List BIOS threads through PCSX2's debugger."""
    try:
        reply = _pcsx2_debug_command(target, "debug_threads")
        threads = _parse_debug_threads(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, threads=threads, raw=reply)


@mcp.tool()
def get_modules(target: str = "iop") -> dict:
    """List IOP modules through PCSX2's debugger."""
    try:
        reply = _pcsx2_debug_command(target, "debug_modules")
        modules = _parse_debug_modules(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, modules=modules, raw=reply)


@mcp.tool()
def get_backtrace(target: str = "ee", max_frames: int = 32) -> dict:
    """Return a PCSX2 debugger stack backtrace."""
    try:
        frames = max(1, min(128, int(max_frames)))
        reply = _pcsx2_debug_command(target, f"debug_backtrace {frames}")
        backtrace = _parse_debug_backtrace(reply)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(target=target, frames=backtrace, raw=reply)


@mcp.tool()
def reload_patches(target: str = "ee", reload_files: bool = True) -> dict:
    """Reload PCSX2 cheats/patches from disk and refresh enabled lists."""
    try:
        reply = _client(target).pcsx2_command(f"patch_reload {1 if reload_files else 0}", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply)


def _parse_cheat_list(reply: str) -> dict[str, Any]:
    lines = reply.splitlines()
    header = lines[0] if lines else ""
    cheats_enabled = "cheats_enabled=1" in header
    unlabelled = 0
    for part in header.split():
        if part.startswith("unlabelled="):
            try:
                unlabelled = int(part.split("=", 1)[1])
            except ValueError:
                unlabelled = 0
    cheats = []
    for line in lines[1:]:
        cols = line.split("\t", 4)
        while len(cols) < 5:
            cols.append("")
        cheats.append(
            {
                "enabled": cols[0] == "1",
                "name": cols[1],
                "applied": cols[2],
                "author": cols[3],
                "description": cols[4],
            }
        )
    return {"cheats_enabled": cheats_enabled, "unlabelled": unlabelled, "cheats": cheats}


@mcp.tool()
def list_cheats(target: str = "ee") -> dict:
    """List loaded cheat groups and their current runtime enabled state."""
    try:
        reply = _client(target).pcsx2_command("cheat_list", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(raw=reply, **_parse_cheat_list(reply))


@mcp.tool()
def set_cheat(name: str, enabled: bool, target: str = "ee") -> dict:
    """Enable or disable one named cheat group at runtime."""
    try:
        encoded = name.encode("utf-8").hex()
        reply = _client(target).pcsx2_command(f"cheat_set {1 if enabled else 0} {encoded}", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply, name=name, enabled=enabled)


@mcp.tool()
def set_cheats_enabled(enabled: bool, target: str = "ee") -> dict:
    """Enable or disable the global cheats switch at runtime."""
    try:
        reply = _client(target).pcsx2_command(f"cheats_enable {1 if enabled else 0}", timeout=30.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))
    return _ok(reply=reply, enabled=enabled)


@mcp.tool()
def sample_trace(target: str = "ee", registers: list[str] | None = None, memory_ranges: list[dict] | None = None) -> dict:
    """Take one timestamped register/memory sample."""
    try:
        return _ok(sample=sample_state(target, registers or ["pc", "sp", "ra"], memory_ranges or []))
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e))


@mcp.tool()
def start_trace(
    trace_id: str,
    target: str = "ee",
    interval_ms: int = 100,
    registers: list[str] | None = None,
    memory_ranges: list[dict] | None = None,
    output_path: str | None = None,
    max_samples: int = 0,
) -> dict:
    """Start background register/memory tracing, optionally writing JSONL records."""
    if trace_id in _trace_workers:
        return _err(f"trace {trace_id!r} already exists")
    try:
        tgt = _target(target)
    except ValueError as e:
        return _err(str(e))
    worker = TraceWorker(
        trace_id=trace_id,
        target=tgt,
        interval_ms=max(10, int(interval_ms)),
        registers=registers or ["pc", "sp", "ra"],
        memory_ranges=memory_ranges or [],
        output_path=output_path,
        max_samples=max(0, int(max_samples)),
    )
    _trace_workers[trace_id] = worker
    worker.start()
    return _ok(trace=trace_info(worker))


@mcp.tool()
def stop_trace(trace_id: str) -> dict:
    """Stop a background trace."""
    worker = _trace_workers.pop(trace_id, None)
    if not worker:
        return _err(f"trace {trace_id!r} not found")
    worker.stop()
    return _ok(trace=trace_info(worker))


@mcp.tool()
def list_traces() -> dict:
    """List active background traces."""
    return _ok(traces={trace_id: trace_info(worker) for trace_id, worker in _trace_workers.items()})


def trace_info(worker: TraceWorker) -> dict:
    return {
        "trace_id": worker.trace_id,
        "target": worker.target,
        "interval_ms": worker.interval_ms,
        "samples": worker.samples,
        "output_path": worker.output_path,
        "last_error": worker.last_error,
        "running": worker.thread.is_alive() if worker.thread else False,
        "started_at": worker.started_at,
    }


def rewind_info(worker: RewindWorker) -> dict:
    ordered = sorted(worker.captures, key=lambda item: item.get("time", 0.0))
    return {
        "rewind_id": worker.rewind_id,
        "target": worker.target,
        "interval_sec": worker.interval_sec,
        "slots": worker.slots,
        "directory": worker.directory,
        "captures": ordered,
        "total_captures": worker.total_captures,
        "last_error": worker.last_error,
        "running": worker.thread.is_alive() if worker.thread else False,
        "started_at": worker.started_at,
    }


@mcp.tool()
def start_rewind_capture(
    rewind_id: str = "default",
    target: str = "ee",
    interval_sec: float = 5.0,
    slots: int = 12,
    directory: str | None = None,
) -> dict:
    """Start periodic savestate capture into a ring buffer for rewind."""
    if rewind_id in _rewind_workers:
        return _err(f"rewind {rewind_id!r} already exists")
    try:
        tgt = _target(target)
    except ValueError as e:
        return _err(str(e))

    if directory is None:
        directory = str(Path.home() / "Documents" / "PCSX2" / "rewind_mcp")
    worker = RewindWorker(
        rewind_id=rewind_id,
        target=tgt,
        interval_sec=max(1.0, float(interval_sec)),
        slots=max(2, int(slots)),
        directory=directory,
    )
    _rewind_workers[rewind_id] = worker
    worker.start()
    return _ok(rewind=rewind_info(worker))


@mcp.tool()
def stop_rewind_capture(rewind_id: str = "default") -> dict:
    """Stop a rewind capture worker but keep its files available."""
    worker = _rewind_workers.pop(rewind_id, None)
    if not worker:
        return _err(f"rewind {rewind_id!r} not found")
    worker.stop()
    return _ok(rewind=rewind_info(worker))


@mcp.tool()
def list_rewind_captures() -> dict:
    """List active rewind workers and captured savestate files."""
    return _ok(rewinds={rewind_id: rewind_info(worker) for rewind_id, worker in _rewind_workers.items()})


@mcp.tool()
def rewind_step(rewind_id: str = "default", steps: int = 1, target: str | None = None) -> dict:
    """Load a previous rewind savestate. steps=1 loads the newest capture."""
    worker = _rewind_workers.get(rewind_id)
    if not worker:
        return _err(f"rewind {rewind_id!r} not found")
    ordered = sorted(worker.captures, key=lambda item: item.get("time", 0.0))
    if not ordered:
        return _err(f"rewind {rewind_id!r} has no captures yet")
    index = max(0, len(ordered) - max(1, int(steps)))
    capture = ordered[index]
    tgt = target or worker.target
    try:
        reply = _client(tgt).pcsx2_command(f"load_file {capture['path']}", timeout=90.0)
    except (GDBError, ValueError, TimeoutError) as e:
        return _err(str(e), capture=capture)
    return _ok(reply=reply, capture=capture, steps=steps)


@mcp.tool()
def configure_gdb_settings(
    config_path: str | None = None,
    enable_ee: bool = True,
    enable_iop: bool = True,
    ee_port: int = 10501,
    iop_port: int = 10502,
) -> dict:
    """Write PCSX2.ini EmuCore GDB server keys for this fork."""
    path = Path(config_path) if config_path else next((p for p in _config_candidates() if p.exists()), _config_candidates()[0])
    values = {
        "EnableEEGDBServer": "true" if enable_ee else "false",
        "EnableIOPGDBServer": "true" if enable_iop else "false",
        "EEGDBServerPort": str(int(ee_port)),
        "IOPGDBServerPort": str(int(iop_port)),
    }
    try:
        _write_simple_ini_values(path, "EmuCore", values)
    except OSError as e:
        return _err(str(e))
    return _ok(path=str(path), values=values, note="Restart PCSX2 or reload settings after changing these keys.")


@mcp.tool()
def configure_background_input(config_path: str | None = None, device_index: int = 0) -> dict:
    """Preserve existing Pad 1 bindings and add keyboard plus optional XInput bindings."""
    try:
        return _ok(**background_input.configure_background_input(config_path, device_index))
    except (OSError, ValueError) as e:
        return _err(str(e), config_path=config_path, device_index=device_index)


@mcp.tool()
def background_input_status() -> dict:
    """Return native, keyboard, and optional XInput backend state without activating PCSX2."""
    native = _native_input_client()
    return _ok(
        default_backend="auto",
        auto_order=["native", "xinput", "keyboard"],
        native={
            "available": native is not None,
            "target": native[0] if native else None,
            "backend": "qPcsx2",
        },
        xinput_module_available=background_input.xinput_module_available(),
        xinput=background_input.gamepad.status(),
        keyboard_backend="PostMessageW/native-render-child",
        foreground_activation=False,
    )


@mcp.tool()
def list_pcsx2_windows(pid: int | None = None) -> dict:
    """List visible PCSX2 windows without changing focus or window state."""
    try:
        if pid is not None:
            return _ok(pid=pid, windows=window_capture.list_windows(pid))
        processes = pcsx2_process.find_processes()
        windows = [window for process in processes for window in window_capture.list_windows(int(process["pid"]))]
        return _ok(windows=windows)
    except OSError as e:
        return _err(str(e), pid=pid)


@mcp.tool()
def capture_pcsx2_window(output_path: str | None = None, pid: int | None = None) -> dict:
    """Capture an inactive or covered PCSX2 window without restoring or activating it."""
    try:
        processes = pcsx2_process.find_processes() if pid is None else [{"pid": pid}]
        if not processes:
            return _err("no PCSX2 process found")
        window = window_capture.find_window([int(process["pid"]) for process in processes])
        out = window_capture.default_output_path() if output_path is None else Path(output_path)
        if not out.is_absolute():
            out = Path.cwd() / out
        result = window_capture.capture_window(int(window["hwnd"], 16), out)
        return _ok(pid=window["pid"], title=window["title"], class_name=window["class_name"], **result)
    except (OSError, ValueError, RuntimeError) as e:
        return _err(str(e), pid=pid, output_path=output_path)


@mcp.tool()
def input_button_press(
    button: str,
    duration_ms: int = 100,
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Press and release a PS2 button through native input, keyboard, or optional XInput without focus."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            if not 1 <= duration_ms <= 10_000:
                raise ValueError("duration_ms must be between 1 and 10000")
            normalized, binding = _native_button_name(button)
            target, press_reply = _native_input_set(controller, binding, 1.0)
            try:
                time.sleep(duration_ms / 1000.0)
            finally:
                release_target, release_reply = _native_input_set(controller, binding, 0.0)
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                target=target,
                release_target=release_target,
                controller=controller,
                button=normalized,
                binding=binding,
                duration_ms=duration_ms,
                replies=[press_reply, release_reply],
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(
                selected_backend=selected,
                fallback_reason=fallback_reason,
                **background_input.gamepad.press(button, duration_ms),
            )
        input_window, path = _background_input_context(pid, config_path)
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.press(int(input_window["hwnd"], 16), button, duration_ms, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), button=button, backend=backend)


@mcp.tool()
def input_buttons_send(
    buttons: dict[str, bool | None],
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Set multiple PS2 button states through native input, keyboard, or optional XInput."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            target = None
            replies: dict[str, str] = {}
            for button, pressed in buttons.items():
                normalized, binding = _native_button_name(button)
                if pressed is None:
                    continue
                if not isinstance(pressed, bool):
                    raise ValueError(f"button state for {button!r} must be true, false, or null")
                target, replies[normalized] = _native_input_set(
                    controller,
                    binding,
                    1.0 if pressed else 0.0,
                )
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                target=target,
                controller=controller,
                replies=replies,
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(
                selected_backend=selected,
                fallback_reason=fallback_reason,
                **background_input.gamepad.set_buttons(buttons),
            )
        input_window, path = _background_input_context(pid, config_path)
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.set_buttons(int(input_window["hwnd"], 16), buttons, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), buttons=buttons, backend=backend)


@mcp.tool()
def input_analog_send(
    x: float,
    y: float,
    stick: str = "left",
    backend: str = "auto",
    pid: int | None = None,
    config_path: str | None = None,
    controller: int = 0,
) -> dict:
    """Set a PS2 analog stick through native input, XInput, or configured keyboard directions."""
    try:
        selected, fallback_reason = _select_input_backend(backend)
        if selected == "native":
            target = None
            values = _native_analog_values(x, y, stick)
            replies: dict[str, str] = {}
            for binding, value in values.items():
                target, replies[binding] = _native_input_set(controller, binding, value)
            return _ok(
                selected_backend="native",
                fallback_reason=fallback_reason,
                target=target,
                controller=controller,
                stick=stick.strip().lower(),
                x=x,
                y=y,
                replies=replies,
                foreground_activation=False,
            )
        if selected == "xinput":
            return _ok(
                selected_backend=selected,
                fallback_reason=fallback_reason,
                **background_input.gamepad.set_analog(x, y, stick),
            )
        input_window, path = _background_input_context(pid, config_path)
        return _ok(
            selected_backend=selected,
            fallback_reason=fallback_reason,
            input_target=input_window,
            **keyboard_input.keyboard.set_analog(int(input_window["hwnd"], 16), x, y, stick, path),
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), x=x, y=y, stick=stick, backend=backend)


@mcp.tool()
def input_reset(backend: str = "auto", pid: int | None = None, controller: int = 0) -> dict:
    """Release keyboard and/or virtual controller state without focusing PCSX2."""
    try:
        requested = backend.strip().lower()
        if requested not in {"auto", "native", "keyboard", "xinput"}:
            raise ValueError("backend must be 'auto', 'native', 'keyboard', or 'xinput'")
        native_result = None
        native_target = None
        if requested == "native" or (requested == "auto" and _native_input_client() is not None):
            native = _native_input_client()
            if native is None:
                raise GDBError("native input requires an active PCSX2 GDB connection")
            native_target, native_client = native
            native_result = native_client.pcsx2_command(f"input_reset {controller}")
            if not native_result.startswith("OK"):
                raise GDBError(native_result)
            if requested == "native":
                return _ok(
                    selected_backend="native",
                    target=native_target,
                    controller=controller,
                    reply=native_result,
                    foreground_activation=False,
                )
        if requested == "xinput":
            return _ok(selected_backend="xinput", **background_input.gamepad.reset())
        keyboard_result = None
        input_window = None
        try:
            input_window, _path = _background_input_context(pid, None)
            keyboard_result = keyboard_input.keyboard.reset(int(input_window["hwnd"], 16))
        except (OSError, ValueError):
            if requested == "keyboard":
                raise
        xinput_result = (
            background_input.gamepad.reset()
            if requested == "auto" and background_input.gamepad.status()["connected"]
            else None
        )
        return _ok(
            native=native_result,
            native_target=native_target,
            keyboard=keyboard_result,
            xinput=xinput_result,
            input_target=input_window,
            foreground_activation=False,
        )
    except (background_input.BackgroundInputError, GDBError, OSError, TimeoutError, ValueError) as e:
        return _err(str(e), backend=backend)


@mcp.tool()
def list_pcsx2_processes() -> dict:
    """List running PCSX2 processes."""
    return _ok(processes=pcsx2_process.find_processes())


@mcp.tool()
def kill_pcsx2(force: bool = False, timeout: float = 5.0) -> dict:
    """Terminate running PCSX2 processes."""
    for client in _clients.values():
        client.disconnect()
    return _ok(**pcsx2_process.terminate_processes(force=force, timeout=timeout))


@mcp.tool()
def launch_pcsx2(exe_path: str, game_path: str | None = None, extra_args: list[str] | None = None) -> dict:
    """Launch PCSX2, optionally with a game path."""
    return pcsx2_process.launch(exe_path, game_path, extra_args)


@mcp.tool()
def get_log_path(path: str | None = None) -> dict:
    """Resolve PCSX2 log path."""
    p = log_reader.resolve_log_path(path)
    return _ok(path=str(p), exists=p.exists())


@mcp.tool()
def tail_log_lines(lines: int = 100, path: str | None = None) -> dict:
    """Return trailing PCSX2 log lines."""
    return _ok(**log_reader.tail_log(lines=lines, path=path))


@mcp.tool()
def get_errors(lines: int = 200, include_warnings: bool = True, contains: str | None = None, path: str | None = None) -> dict:
    """Return recent error/warning log lines."""
    levels = ("error", "warning") if include_warnings else ("error",)
    return _ok(**log_reader.filter_log(levels=levels, lines=lines, contains=contains, path=path))


@mcp.tool()
def list_register_names() -> dict:
    """Return GDB register names in order."""
    return _ok(registers=list(REG_NAMES))
