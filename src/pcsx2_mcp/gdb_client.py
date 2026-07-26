"""GDB Remote Serial Protocol client for PCSX2's EE/IOP GDB servers."""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass

REG_NAMES: tuple[str, ...] = (
    "zero", "at", "v0", "v1", "a0", "a1", "a2", "a3",
    "t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7",
    "s0", "s1", "s2", "s3", "s4", "s5", "s6", "s7",
    "t8", "t9", "k0", "k1", "gp", "sp", "fp", "ra",
    "status", "lo", "hi", "badvaddr", "cause", "pc",
    "f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7",
    "f8", "f9", "f10", "f11", "f12", "f13", "f14", "f15",
    "f16", "f17", "f18", "f19", "f20", "f21", "f22", "f23",
    "f24", "f25", "f26", "f27", "f28", "f29", "f30", "f31",
    "fcsr", "fir",
)

BP_TYPE_MAP = {
    "sw": 0,
    "exec": 1,
    "hw": 1,
    "write": 2,
    "read": 3,
    "rw": 4,
    "access": 4,
}


@dataclass
class PacketReply:
    payload: bytes

    def as_str(self) -> str:
        return self.payload.decode("ascii", errors="replace")


class GDBError(RuntimeError):
    pass


class GDBClient:
    def __init__(self, target: str) -> None:
        self.target = target
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._lock = threading.RLock()
        self._host: str | None = None
        self._port: int | None = None
        self._running = False

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def status(self) -> dict:
        return {
            "target": self.target,
            "connected": self.connected,
            "host": self._host,
            "port": self._port,
            "running": self._running,
        }

    def connect(self, host: str, port: int, timeout: float = 5.0) -> None:
        with self._lock:
            if self._sock is not None:
                raise GDBError(f"{self.target} already connected")
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.settimeout(timeout)
            self._sock = sock
            self._buffer = b""
            self._host = host
            self._port = port
            self._running = False

    def disconnect(self) -> None:
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None
            self._buffer = b""
            self._running = False

    @staticmethod
    def _checksum(payload: bytes) -> bytes:
        return f"{sum(payload) & 0xFF:02x}".encode("ascii")

    def _require_sock(self) -> socket.socket:
        if self._sock is None:
            raise GDBError(f"{self.target} is not connected")
        return self._sock

    def _send_raw(self, data: bytes) -> None:
        try:
            self._require_sock().sendall(data)
        except OSError as e:
            self.disconnect()
            raise GDBError(f"send failed: {e}") from e

    def _recv_more(self, timeout: float) -> None:
        sock = self._require_sock()
        sock.settimeout(max(0.05, timeout))
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise TimeoutError("timed out waiting for PCSX2 GDB server") from None
        except OSError as e:
            self.disconnect()
            raise GDBError(f"recv failed: {e}") from e
        if not chunk:
            self.disconnect()
            raise GDBError("PCSX2 GDB server closed the connection")
        self._buffer += chunk

    def _drain_acks(self) -> None:
        while self._buffer and self._buffer[0:1] in (b"+", b"-"):
            self._buffer = self._buffer[1:]

    def _read_packet(self, timeout: float = 5.0) -> bytes:
        deadline = time.monotonic() + timeout
        while True:
            self._drain_acks()
            if self._buffer.startswith(b"$"):
                hash_idx = self._buffer.find(b"#", 1)
                if hash_idx != -1 and len(self._buffer) >= hash_idx + 3:
                    payload = self._buffer[1:hash_idx]
                    checksum = self._buffer[hash_idx + 1 : hash_idx + 3]
                    self._buffer = self._buffer[hash_idx + 3 :]
                    expected = self._checksum(payload)
                    if checksum.lower() != expected.lower():
                        self._send_raw(b"-")
                        raise GDBError(f"bad checksum: got {checksum!r}, expected {expected!r}")
                    self._send_raw(b"+")
                    return payload
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out reading packet from PCSX2 GDB server")
            self._recv_more(remaining)

    def _send_packet(self, payload: bytes) -> None:
        self._send_raw(b"$" + payload + b"#" + self._checksum(payload))

    def _command(self, payload: bytes, timeout: float = 5.0) -> bytes:
        with self._lock:
            self._send_packet(payload)
            return self._read_packet(timeout=timeout)

    def pause(self, timeout: float = 5.0) -> str:
        with self._lock:
            self._send_raw(b"\x03")
            reply = self._read_packet(timeout=timeout)
            self._running = False
            return reply.decode("ascii", errors="replace")

    def resume(self) -> None:
        with self._lock:
            self._send_packet(b"c")
            self._running = True

    def step(self, timeout: float = 10.0) -> str:
        with self._lock:
            self._send_packet(b"s")
            reply = self._read_packet(timeout=timeout)
            self._running = False
            return reply.decode("ascii", errors="replace")

    def get_registers(self) -> dict[str, int]:
        reply = self._command(b"g", timeout=5.0)
        if len(reply) < len(REG_NAMES) * 8:
            raise GDBError(f"short register reply: {len(reply)} chars")
        values: dict[str, int] = {}
        for index, name in enumerate(REG_NAMES):
            raw_hex = reply[index * 8 : (index + 1) * 8].decode("ascii")
            values[name] = int.from_bytes(bytes.fromhex(raw_hex), "little")
        return values

    def write_register(self, name_or_index: str | int, value: int) -> None:
        if isinstance(name_or_index, int):
            index = name_or_index
        else:
            try:
                index = REG_NAMES.index(name_or_index)
            except ValueError as e:
                raise GDBError(f"unknown register {name_or_index!r}") from e
        payload = f"P{index:x}={int(value) & 0xFFFFFFFF:08x}".encode("ascii")
        # The RSP P packet wants little-endian hex bytes, not host-order text.
        le = int(value & 0xFFFFFFFF).to_bytes(4, "little").hex()
        payload = f"P{index:x}={le}".encode("ascii")
        reply = self._command(payload, timeout=5.0)
        if reply != b"OK":
            raise GDBError(f"write_register failed: {reply!r}")

    def read_memory(self, address: int, length: int, timeout: float = 15.0) -> bytes:
        if length <= 0:
            return b""
        cmd = f"m{address & 0xFFFFFFFF:x},{length:x}".encode("ascii")
        reply = self._command(cmd, timeout=timeout)
        if reply.startswith(b"E"):
            raise GDBError(f"read_memory failed: {reply.decode('ascii', errors='replace')}")
        return bytes.fromhex(reply.decode("ascii"))

    def read_memory_chunked(self, address: int, length: int, chunk: int = 0x800, timeout: float = 30.0) -> bytes:
        out = bytearray()
        cur = address
        remaining = length
        while remaining > 0:
            take = min(chunk, remaining)
            out.extend(self.read_memory(cur, take, timeout=timeout))
            cur = (cur + take) & 0xFFFFFFFF
            remaining -= take
        return bytes(out)

    def write_memory(self, address: int, data: bytes, timeout: float = 15.0) -> None:
        cmd = f"M{address & 0xFFFFFFFF:x},{len(data):x}:{data.hex()}".encode("ascii")
        reply = self._command(cmd, timeout=timeout)
        if reply != b"OK":
            raise GDBError(f"write_memory failed: {reply.decode('ascii', errors='replace')}")

    def set_breakpoint(self, address: int, kind: str = "exec", size: int = 4) -> None:
        bp_type = BP_TYPE_MAP.get(kind)
        if bp_type is None:
            raise GDBError(f"unknown breakpoint kind {kind!r}")
        reply = self._command(f"Z{bp_type},{address & 0xFFFFFFFF:x},{size:x}".encode("ascii"))
        if reply != b"OK":
            raise GDBError(f"set_breakpoint failed: {reply!r}")

    def remove_breakpoint(self, address: int, kind: str = "exec", size: int = 4) -> None:
        bp_type = BP_TYPE_MAP.get(kind)
        if bp_type is None:
            raise GDBError(f"unknown breakpoint kind {kind!r}")
        reply = self._command(f"z{bp_type},{address & 0xFFFFFFFF:x},{size:x}".encode("ascii"))
        if reply != b"OK":
            raise GDBError(f"remove_breakpoint failed: {reply!r}")

    def pcsx2_command(self, command: str, timeout: float = 30.0) -> str:
        hex_command = command.encode("utf-8").hex()
        reply = self._command(f"qPcsx2:{hex_command}".encode("ascii"), timeout=timeout)
        if reply.startswith(b"E"):
            raise GDBError(reply.decode("ascii", errors="replace"))
        try:
            return bytes.fromhex(reply.decode("ascii")).decode("utf-8", errors="replace")
        except ValueError as e:
            raise GDBError(f"invalid qPcsx2 reply: {reply!r}") from e
