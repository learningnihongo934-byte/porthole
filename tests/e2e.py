"""End-to-end test: real server + real Cloudflare tunnel + an MCP client calling every tool over HTTPS,
the same way ChatGPT does. Approvals are answered by a script instead of popups.
Run: uv run python tests/e2e.py            (add --local to skip the tunnel)"""
import asyncio
import secrets
import sys
import tempfile
import time
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from porthole.cli import Tunnel, serve
import porthole.guard
from porthole.guard import Guard

porthole.guard.LOG = Path(tempfile.gettempdir()) / "porthole-test-log.jsonl"   # keep test runs out of the real log

answers: list[str] = []          # what the fake user clicks, in order
asked: list[str] = []


def fake_dialog(title: str, detail: str) -> str:
    asked.append(title)
    return answers.pop(0) if answers else "deny"


def text(res) -> str:
    return "\n".join(getattr(c, "text", f"<{c.type}>") for c in res.content)


async def run(url: str, root: Path, guard: Guard) -> None:
    async with streamable_http_client(url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name: t for t in (await s.list_tools()).tools}
            assert set(tools) >= {"get_status", "list_dir", "read_file", "find_files", "search_text", "write_file", "edit_file", "run_command", "take_screenshot"}, tools
            assert tools["read_file"].annotations.read_only_hint and not tools["run_command"].annotations.read_only_hint

            call = lambda name, **kw: s.call_tool(name, kw)
            check = lambda cond, msg: (print(("  ✓ " if cond else "  ✗ ") + msg), cond or sys.exit(1))

            out = text(await call("get_status")); check("Shared folders" in out, "get_status")
            out = text(await call("list_dir")); check("notes.txt" in out and "src/" in out, "list_dir")
            out = text(await call("read_file", path="notes.txt")); check("1\thello" in out, "read_file with line numbers")
            out = text(await call("find_files", pattern="*.py")); check("app.py" in out, "find_files")
            out = text(await call("search_text", query="TODO")); check("app.py:2" in out, "search_text")

            answers.append("allow")
            out = text(await call("write_file", path="new/made.txt", content="made by chatgpt\n"))
            check("Created" in out and (root / "new/made.txt").read_text() == "made by chatgpt\n", "write_file after approval")

            answers.append("deny")
            out = text(await call("write_file", path="denied.txt", content="x"))
            check("said no" in out and not (root / "denied.txt").exists(), "write_file denied by the user")

            answers.append("allow")
            out = text(await call("edit_file", path="notes.txt", old_text="hello", new_text="hi there"))
            check("Edited" in out and (root / "notes.txt").read_text().startswith("hi there"), "edit_file after approval")

            answers.append("allow")
            out = text(await call("run_command", command="echo $((6*7)) && pwd"))
            check("exit code: 0" in out and "42" in out and str(root) in out, "run_command after approval")

            out = text(await call("read_file", path="../outside.txt")); check("outside the folders" in out, "blocks paths outside shared folders")
            out = text(await call("read_file", path="~/.ssh/id_rsa")); check("Not allowed" in out, "blocks ~/.ssh")
            (root / ".env").write_text("SECRET=1")
            out = text(await call("read_file", path=".env")); check("private location" in out, "blocks .env files")
            check(asked == ["ChatGPT wants to create a file", "ChatGPT wants to create a file", "ChatGPT wants to edit a file", "ChatGPT wants to run a command"],
                  f"asked exactly once per change ({len(asked)} popups)")

            # allow-everything mode
            guard.mode = "auto"; asked.clear()
            out = text(await call("write_file", path="auto.txt", content="no popup"))
            check("Created" in out and not asked, "allow-all: writes without a popup")
            out = text(await call("run_command", command="echo fine"))
            check("fine" in out and not asked, "allow-all: commands without a popup")
            answers.append("deny")
            out = text(await call("run_command", command="sudo -n true"))
            check("said no" in out and asked == ["ChatGPT wants to run a command"], "allow-all: sudo still asks")
            out = text(await call("read_file", path="~/.ssh/id_rsa")); check("Not allowed" in out, "allow-all: ~/.ssh still blocked")
            guard.auto_until = time.time() - 1; asked.clear(); answers.append("allow")
            await call("write_file", path="later.txt", content="x")
            check(guard.mode == "ask" and asked, "allow-all: goes back to asking when time runs out")
            guard.mode = "ask"; guard.auto_until = 0


def main() -> None:
    local = "--local" in sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve() / "shared"
        (root / "src").mkdir(parents=True)
        (root / "notes.txt").write_text("hello\nworld\n")
        (root / "src/app.py").write_text("print('hi')\n# TODO: ship it\n")
        (root.parent / "outside.txt").write_text("nope")

        guard = Guard(roots=[root], approver=fake_dialog)
        token, port = secrets.token_urlsafe(32), 8799
        serve(guard, token, port)
        base = f"http://127.0.0.1:{port}"
        if not local:
            t = Tunnel(port); t.start()
            for _ in range(1300):
                if t.ready or t.error:
                    break
                time.sleep(0.1)
            assert t.ready, t.error
            base = t.url
        print(f"Testing over {base}")
        try:
            asyncio.run(run(f"{base}/{token}/mcp", root, guard))
            # a wrong secret must get nothing
            import urllib.request, urllib.error
            try:
                urllib.request.urlopen(urllib.request.Request(f"{base}/wrong-secret/mcp", data=b"{}", method="POST"), timeout=20)
                print("  ✗ wrong link was accepted"); sys.exit(1)
            except urllib.error.HTTPError as e:
                print(f"  ✓ wrong link refused (HTTP {e.code})")
        finally:
            if not local:
                t.stop()
    print("All good.")


if __name__ == "__main__":
    main()
