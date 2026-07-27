# PCSX2 MCP

**ENGLISH | [KOREAN](README_ko.md)**


PCSX2 MCP is a Windows-focused Model Context Protocol server for controlling and debugging PCSX2 through its EE and IOP GDB Remote Serial Protocol endpoints. It exposes emulator control, memory inspection, debugger operations, tracing, savestates, cheats, screenshots, process management, and log access as MCP tools.

> [!IMPORTANT]
> Basic register and memory operations require PCSX2's GDB servers. Extended tools such as emulator commands, debugger expressions, cheat control, screenshots, and savestate automation require a compatible PCSX2 build that implements the `qPcsx2` commands used by this server.

### Features

- Connect to the EE or IOP GDB server and inspect connection status.
- Pause, resume, single-step, reset, and advance frames.
- Read and write registers, memory, strings, and files.
- Dump memory regions, search byte patterns with wildcards, and compare snapshots.
- Disassemble code, evaluate debugger expressions, and inspect threads, modules, and backtraces.
- Add, remove, list, and clear breakpoints and watchpoints.
- Save and load states, capture screenshots, and maintain a rewind ring buffer.
- Reload patches and list, enable, or disable cheats at runtime.
- Record periodic register and memory traces to JSONL.
- Launch, list, and terminate PCSX2 processes.
- Locate, tail, and filter PCSX2 logs.
- Capture and control PCSX2 while its window remains inactive.
- Use targeted keyboard messages or an optional virtual XInput controller.

### Requirements

- Windows
- Python 3.10 or newer
- PCSX2 with the EE/IOP GDB servers enabled
- An MCP client that supports local stdio servers

The default endpoints are:

| Target | Host | Port |
| --- | --- | ---: |
| EE | `127.0.0.1` | `10501` |
| IOP | `127.0.0.1` | `10502` |

### Installation

Open PowerShell in the repository directory:

```powershell
python -m pip install -e .
```

Keyboard input has no extra dependency. Optional XInput support uses
`vgamepad` and an installed ViGEmBus driver:

```powershell
python -m pip install -e ".[xinput]"
```

Run the server directly:

```powershell
python -m pcsx2_mcp
```

The server communicates over stdio, so it normally runs under an MCP client rather than in a standalone interactive terminal.

### Codex MCP configuration

To register PCSX2 MCP as a local stdio server in Codex, add the following blocks to `%USERPROFILE%\.codex\config.toml`. Replace `C:\path\to\PCSX2_MCP` with the path where you cloned this repository.

```toml
[mcp_servers.pcsx2]
command = "python"
args = ["-m", "pcsx2_mcp"]
cwd = 'C:\path\to\PCSX2_MCP'

[mcp_servers.pcsx2.env]
PYTHONPATH = 'C:\path\to\PCSX2_MCP\src'
```

The `python` command must resolve to the interpreter where the project dependencies were installed. Restart Codex after changing `config.toml`.

### PCSX2 setup

1. Start the MCP server in your client.
2. Run the `configure_gdb_settings` tool once, or enable the EE and IOP GDB servers manually in the compatible PCSX2 configuration.
3. Restart PCSX2 after changing the GDB settings.
4. Start a game, then call `connect` with `target="ee"` or `target="iop"`.
5. Call `status` or `pcsx2_status` before using debugging and automation tools.

### Background capture and input

`take_screenshot` uses the custom `qPcsx2 screenshot_file` command when the
GDB client is connected, so it captures the emulator render buffer without
touching the desktop focus. When disconnected, PNG requests fall back to
`capture_pcsx2_window`, which uses `PrintWindow` without restoring or
activating the window.

`configure_background_input` preserves existing Pad 1 bindings and adds
keyboard plus XInput mappings. Input tools accept `backend="auto"`,
`"keyboard"`, or `"xinput"`. `auto` falls back to targeted `PostMessageW`
keyboard events when `vgamepad` or its driver is unavailable.

PCSX2's top-level Qt key filter may request activation. The MCP keyboard
backend therefore sends only to a verified native render child. If that child
cannot be identified, it returns an error instead of sending to the top-level
window.

### Typical workflow

1. Use `launch_pcsx2` or start PCSX2 yourself.
2. Use `connect` for the required target.
3. Inspect state with `get_registers`, `read_memory`, `disassemble`, or `evaluate`.
4. Control execution with `pause`, `step`, `resume`, breakpoints, or watchpoints.
5. Call `disconnect` before shutting down PCSX2.

### Safety

This server can modify emulated memory, overwrite memory from a file, change registers, load savestates, control cheats, and terminate PCSX2 processes. Keep it bound to local MCP clients and review tool arguments before approving destructive operations.

Generated logs, screenshots, traces, rewind captures, virtual environments, bytecode, and build outputs are excluded from version control.

### License

This project is distributed under the terms in [LICENSE](LICENSE).
