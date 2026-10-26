"""The MCP server ChatGPT talks to: file and terminal tools, each checked by the Guard."""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import logging
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .guard import Denied, Guard

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache", "Library"}
MAX_READ = 400_000
MAX_OUT = 40_000

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)
RUN = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)

INSTRUCTIONS = """Porthole gives you the user's Mac: files in the folders they shared, and a terminal.
Start with `get_status` to see the shared folders. Relative paths start in the first shared folder.
Reading, listing and searching need no approval. Writing, editing and running commands each pop up
a dialog on the user's Mac that they must approve, so explain what you're about to do before you do it,
batch related edits, and never retry something the user denied. Prefer `edit_file` for small changes
to existing files over rewriting them with `write_file`."""


def build_server(guard: Guard) -> MCPServer:
    # request logs would print the secret link, so keep the SDK quiet
    for name in ("mcp", "httpx", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)
    mcp = MCPServer(name="Porthole", title="Porthole: your Mac", instructions=INSTRUCTIONS, version="1.0.0", log_level="WARNING")

    def walk(root: Path):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for name in sorted(filenames):
                yield Path(dirpath) / name

    async def guarded(tool: str, target: str, fn):
        try:
            result = await asyncio.to_thread(fn)
            guard.log(tool, target)
            return result
        except Denied as e:
            guard.log(tool, target, ok=False, note=str(e)[:120])
            return f"Not allowed: {e}"
        except FileNotFoundError:
            guard.log(tool, target, ok=False, note="not found")
            return f"Not found: {target}"
        except Exception as e:  # tool errors go back to ChatGPT instead of crashing the server
            guard.log(tool, target, ok=False, note=str(e)[:120])
            return f"Error: {type(e).__name__}: {e}"

    @mcp.tool(annotations=READ)
    async def get_status() -> str:
        """Shows the shared folders, whether changes are allowed, and basic facts about the Mac. Call this first."""
        def go():
            lines = [f"Shared folders (relative paths start in the first): " + ", ".join(guard.show(r) for r in guard.roots),
                     f"Mode: {'read-only' if guard.mode == 'readonly' else 'changes and commands need the user to approve each one'}",
                     f"macOS {platform.mac_ver()[0]} on {platform.machine()}, shell zsh, home is ~",
                     f"Tools on PATH: " + ", ".join(t for t in ["git", "python3", "node", "npm", "brew", "rg", "swift", "uv"] if shutil.which(t))]
            return "\n".join(lines)
        return await guarded("get_status", "", go)

    @mcp.tool(annotations=READ)
    async def list_dir(path: str = ".") -> str:
        """Lists a folder: names, sizes and when each was last changed. Folders end with /."""
        def go():
            p = guard.resolve(path)
            rows = []
            for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:500]:
                try:
                    st = c.stat()
                except OSError:
                    continue
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
                rows.append(f"{c.name}/" if c.is_dir() else f"{c.name}  ({st.st_size:,} bytes, {when})")
            return f"{guard.show(p)}\n" + ("\n".join(rows) or "(empty)")
        return await guarded("list_dir", path, go)

    @mcp.tool(annotations=READ)
    async def read_file(path: str, start_line: int = 1, max_lines: int = 2000) -> str:
        """Reads a text file with line numbers. Use start_line and max_lines to page through long files."""
        def go():
            p = guard.resolve(path)
            data = p.read_bytes()[:MAX_READ]
            if b"\0" in data[:8000]:
                return f"{guard.show(p)} is a binary file ({p.stat().st_size:,} bytes)."
            lines = data.decode("utf-8", errors="replace").splitlines()
            s = max(1, start_line)
            chunk = lines[s - 1 : s - 1 + max_lines]
            body = "\n".join(f"{i:>6}\t{l}" for i, l in enumerate(chunk, s))
            more = len(lines) - (s - 1 + len(chunk))
            return body + (f"\n… {more} more lines (use start_line={s + len(chunk)})" if more > 0 else "")
        return await guarded("read_file", path, go)

    @mcp.tool(annotations=READ)
    async def find_files(pattern: str, path: str = ".") -> str:
        """Finds files by name pattern, like '*.py' or 'README*', searching inside a folder."""
        def go():
            root = guard.resolve(path)
            hits = [guard.show(f) for f in walk(root) if fnmatch.fnmatch(f.name, pattern)][:500]
            return "\n".join(hits) or "No files match."
        return await guarded("find_files", f"{pattern} in {path}", go)

    @mcp.tool(annotations=READ)
    async def search_text(query: str, path: str = ".", file_pattern: str = "*", regex: bool = False, case_sensitive: bool = False) -> str:
        """Searches inside files for text (or a regex). Returns file:line: matching line, up to 200 matches."""
        def go():
            root = guard.resolve(path)
            flags = 0 if case_sensitive else re.IGNORECASE
            rx = re.compile(query if regex else re.escape(query), flags)
            out = []
            for f in walk(root):
                if not fnmatch.fnmatch(f.name, file_pattern):
                    continue
                try:
                    guard.resolve(str(f))
                    if f.stat().st_size > 2_000_000:
                        continue
                    with f.open(errors="ignore") as fh:
                        for n, line in enumerate(fh, 1):
                            if rx.search(line):
                                out.append(f"{guard.show(f)}:{n}: {line.rstrip()[:300]}")
                                if len(out) >= 200:
                                    return "\n".join(out) + "\n… stopped at 200 matches"
                except (Denied, OSError):
                    continue
            return "\n".join(out) or "No matches."
        return await guarded("search_text", f"“{query}” in {path}", go)

    @mcp.tool(annotations=WRITE)
    async def write_file(path: str, content: str) -> str:
        """Creates a file or replaces all of it. The user approves it first. For small changes use edit_file."""
        def go():
            p = guard.resolve(path)
            exists = p.exists()
            preview = "\n".join(content.splitlines()[:40])
            guard.approve(f"{'replace' if exists else 'create'} a file",
                          f"{guard.show(p)}\n{len(content):,} characters{' (replaces the current file)' if exists else ''}\n\n{preview}")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"{'Replaced' if exists else 'Created'} {guard.show(p)} ({len(content):,} characters)."
        return await guarded("write_file", path, go)

    @mcp.tool(annotations=WRITE)
    async def edit_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
        """Replaces exact text in a file. old_text must match exactly once unless replace_all is true. The user approves it first."""
        def go():
            p = guard.resolve(path)
            text = p.read_text()
            n = text.count(old_text)
            if not old_text or n == 0:
                return "old_text wasn't found. Read the file again and copy the text exactly, including spaces."
            if n > 1 and not replace_all:
                return f"old_text appears {n} times. Add surrounding lines to make it unique, or set replace_all."
            new = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
            diff = "".join(difflib.unified_diff(text.splitlines(True), new.splitlines(True), "before", "after", n=2))
            guard.approve("edit a file", f"{guard.show(p)}\n\n{diff}")
            p.write_text(new)
            return f"Edited {guard.show(p)} ({n if replace_all else 1} change{'s' if replace_all and n > 1 else ''})."
        return await guarded("edit_file", path, go)

    @mcp.tool(annotations=RUN)
    async def run_command(command: str, cwd: str = ".", timeout_seconds: int = 120) -> str:
        """Runs a shell command (zsh) in a shared folder and returns its output and exit code. The user approves it first."""
        def go():
            d = guard.resolve(cwd)
            guard.approve("run a command", f"In {guard.show(d)}:\n\n$ {command}")
            try:
                r = subprocess.run(["/bin/zsh", "-lc", command], cwd=d, capture_output=True, text=True,
                                   timeout=max(1, min(timeout_seconds, 1800)), stdin=subprocess.DEVNULL)
                out = (r.stdout + (("\n[stderr]\n" + r.stderr) if r.stderr.strip() else "")).strip()
                code = r.returncode
            except subprocess.TimeoutExpired as e:
                out, code = ((e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")), "timed out"
            if len(out) > MAX_OUT:
                out = out[:MAX_OUT // 2] + f"\n… {len(out) - MAX_OUT:,} characters cut …\n" + out[-MAX_OUT // 2:]
            return f"exit code: {code}\n{out or '(no output)'}"
        return await guarded("run_command", command[:120], go)

    @mcp.tool(annotations=WRITE)
    async def take_screenshot() -> Image | str:
        """Takes a screenshot of the user's screen so you can see what they see. The user approves it first."""
        def go():
            guard.approve("see your screen", "Take one screenshot of your main display and send it to ChatGPT.")
            with tempfile.TemporaryDirectory() as tmp:
                f = Path(tmp) / "shot.jpg"
                subprocess.run(["screencapture", "-x", "-t", "jpg", str(f)], check=True, timeout=20)
                subprocess.run(["sips", "-Z", "1600", str(f)], capture_output=True, timeout=20)
                return Image(data=f.read_bytes(), format="jpeg")
        return await guarded("take_screenshot", "screen", go)

    return mcp


def build_app(guard: Guard, token: str):
    """The HTTP app. The secret token is part of the path, so only someone with the full link can connect."""
    server = build_server(guard)
    # ChatGPT reaches us through a tunnel with its own Host header; the secret path is the access check.
    return server.streamable_http_app(
        streamable_http_path=f"/{token}/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
