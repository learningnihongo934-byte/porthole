"""`porthole` command: start the server and tunnel, show a live dashboard, and manage settings."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .guard import HOME, LOG, RUN, STATE, Guard, load_config, read_run, save_config

console = Console()
NAVY, BRASS, SEA = "#1e3a5f", "#d4a24c", "#4fb3bf"
LOGO = f"""[{BRASS}]  ╭───────╮[/]
[{BRASS}] ╭┤[/][{SEA}] ▸ _   [/][{BRASS}]├╮[/]
[{BRASS}] ╰┤[/][{SEA}]       [/][{BRASS}]├╯[/]
[{BRASS}]  ╰───────╯[/]"""


def find_cloudflared() -> str | None:
    return shutil.which("cloudflared") or next((str(p) for p in [STATE / "bin" / "cloudflared"] if p.exists()), None)


class Tunnel:
    """A Cloudflare quick tunnel: a temporary public https URL pointing at the local server. No account needed."""

    def __init__(self, port: int):
        self.port, self.url, self.proc, self.error, self.ready = port, None, None, None, False

    def start(self) -> None:
        exe = find_cloudflared()
        if not exe:
            self.error = "cloudflared isn't installed. Run the installer again, or: brew install cloudflared"
            return
        self.proc = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--protocol", "http2", "--url", f"http://127.0.0.1:{self.port}"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not self.url:
                self.url = m.group(0)
                threading.Thread(target=self._wait_until_reachable, daemon=True).start()
        if not self.url:
            self.error = "The tunnel closed before it gave a link. Check your internet connection and try again."

    def _wait_until_reachable(self) -> None:
        """A new tunnel takes ~20s before the public internet can reach it. Check through public DNS (so this Mac
        doesn't cache a 'not found' answer) and only call it ready once a request really arrives here."""
        host = self.url.split("//", 1)[1]
        deadline = time.time() + 120
        while time.time() < deadline and self.proc.poll() is None:
            ips = subprocess.run(["dig", "+short", "@1.1.1.1", host], capture_output=True, text=True).stdout.split()
            ip = next((x for x in ips if re.fullmatch(r"[\d.]+", x)), None)
            if ip:
                code = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "8",
                                       "--resolve", f"{host}:443:{ip}", f"{self.url}/"], capture_output=True, text=True).stdout
                if code == "404":            # our server answered (the bare address is deliberately empty)
                    self.ready = True
                    return
            time.sleep(1.5)
        if not self.ready:
            self.error = "The tunnel never became reachable. Check your internet connection and try again."

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def serve(guard: Guard, token: str, port: int) -> "uvicorn.Server":
    import uvicorn
    from .server import build_app
    server = uvicorn.Server(uvicorn.Config(build_app(guard, token), host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server


def copy(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def dashboard(guard: Guard, link: str | None, status: str, started: float, copied: bool) -> Group:
    head = Table.grid(padding=(0, 2))
    head.add_column(); head.add_column()
    on = link is not None
    auto = guard.auto
    title = Text.from_markup(f"[bold white]Porthole[/]  [{'bold green' if on else 'yellow'}]● {'open' if on else status}[/]"
                             + ("  [bold white on red] ALLOW EVERYTHING [/]" if auto else "")
                             + f"\n[dim]ChatGPT can use your Mac through this window. Close it to cut access.[/]")
    head.add_row(Text.from_markup(LOGO), title)

    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim", justify="right"); info.add_column()
    if link:
        info.add_row("Link", Text(link, style=f"bold {SEA}"))
        info.add_row("", Text("copied to clipboard" if copied else "copy the link above", style="dim"))
    info.add_row("Folders", ", ".join(guard.show(r) for r in guard.roots))
    if guard.mode == "readonly":
        info.add_row("Mode", "[bold yellow]read-only[/]")
    elif auto:
        left = f" for {int((guard.auto_until - time.time()) // 60)}m {int(guard.auto_until - time.time()) % 60:02d}s more" if guard.auto_until else " until you close Porthole"
        info.add_row("Mode", f"[bold red]allow everything{left}[/] · no popups, except risky commands like sudo or rm -rf ~")
    else:
        info.add_row("Mode", "reads are free · [bold]every change and command asks you first[/]")
    if guard.trusted_until > time.time():
        info.add_row("", f"[bold red]auto-allowing changes for {int(guard.trusted_until - time.time())}s more[/]")
    up = int(time.time() - started)
    info.add_row("Open for", f"{up // 3600}h {up % 3600 // 60:02d}m {up % 60:02d}s" if up >= 3600 else f"{up // 60}m {up % 60:02d}s")

    acts = Table(box=None, padding=(0, 1), expand=True, show_header=True, header_style="dim")
    acts.add_column("time", no_wrap=True); acts.add_column("action", no_wrap=True); acts.add_column("on", ratio=1); acts.add_column("", no_wrap=True)
    for ev in list(guard.events)[-12:]:
        icon = "[green]✓[/]" if ev["ok"] else "[red]✕[/]"
        style = "bold" if ev["tool"] in ("write_file", "edit_file", "run_command", "take_screenshot") else ""
        acts.add_row(time.strftime("%H:%M:%S", time.localtime(ev["t"])), Text(ev["tool"], style=style), Text(ev["target"] or "", overflow="ellipsis", no_wrap=True), icon)
    if not guard.events:
        acts.add_row("", "[dim]Waiting for ChatGPT…[/]", "", "")

    steps = Text.from_markup(
        "[bold]Connect ChatGPT[/]  Settings → Apps → Advanced settings → turn on Developer mode → Create app.\n"
        "Name it Porthole, paste the link, choose [bold]No authentication[/], tick “I understand and want to continue”, Create.\n"
        "In a chat: + → Developer mode → Porthole.   [dim]Ctrl-C closes the porthole.[/]")
    return Group(Panel(head, border_style=BRASS, padding=(0, 1)),
                 Panel(info, title="[dim]connection[/]", title_align="left", border_style=NAVY),
                 Panel(acts, title="[dim]what ChatGPT did[/]", title_align="left", border_style=NAVY),
                 Padding(steps))


def Padding(r):  # noqa: N802 - small helper so the steps line up with the panels
    from rich.padding import Padding as P
    return P(r, (0, 1))


def cmd_start(a) -> int:
    if (info := read_run()):
        console.print(f"[yellow]Porthole is already open[/] (pid {info['pid']}). Link: {info.get('link')}\nClose it with [bold]porthole stop[/].")
        return 1
    cfg = load_config()
    if a.allow:
        cfg["allow"] = [str(Path(p).expanduser().resolve()) for p in a.allow]
    if a.read_only and a.allow_all:
        console.print("[red]Pick one: --read-only or --allow-all.[/]")
        return 1
    if a.read_only:
        cfg["mode"] = "readonly"
    elif a.ask or cfg.get("mode") == "auto":
        cfg["mode"] = "ask"                         # allow-everything is never remembered; it's chosen each time
    auto_until = 0.0
    if a.allow_all:
        if a.for_ and not (secs := parse_duration(a.for_)):
            console.print(f"[red]Can't read --for {a.for_}. Try 30m or 2h.[/]")
            return 1
        auto_until = time.time() + secs if a.for_ else 0.0
        console.print(Panel.fit(
            "[bold]Allow everything[/] lets ChatGPT edit files and run commands in your shared folders [bold]without asking[/].\n"
            "A web page or file it reads could trick it into doing something you didn't want.\n"
            "Risky commands (sudo, rm -rf ~, disk erase, curl | sh, force-push…) still pop up.",
            border_style="red", title="[bold red]Heads up[/]"))
        if not a.yes:
            if not sys.stdin.isatty():
                console.print("[red]Add --yes to confirm allow-everything when not running in a terminal.[/]")
                return 1
            try:
                answer = input("Type yes to turn it on: ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() != "yes":
                console.print("Okay, starting in the normal ask-first mode.")
                a.allow_all = False
    missing = [p for p in cfg["allow"] if not Path(p).is_dir()]
    if missing:
        console.print(f"[red]These folders don't exist:[/] {', '.join(missing)}")
        return 1
    if any(Path(p).resolve() in (HOME, Path("/")) for p in cfg["allow"]):
        console.print("[yellow]Sharing your whole home folder or disk is risky. Share specific folders with --allow instead.[/]")
        return 1
    token = cfg.get("token") if a.keep_link and cfg.get("token") else secrets.token_urlsafe(32)
    cfg["token"] = token if a.keep_link else None
    save_config(cfg)

    guard = Guard.from_config(cfg)
    if a.allow_all:
        guard.mode, guard.auto_until = "auto", auto_until
    server = serve(guard, token, a.port)
    tunnel = Tunnel(a.port)
    if not a.local:
        tunnel.start()
    started, link, copied = time.time(), None, False
    local_link = f"http://127.0.0.1:{a.port}/{token}/mcp"

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)

    try:
        with Live(dashboard(guard, None, "opening…", started, False), console=console, refresh_per_second=4, screen=False) as live:
            while True:
                if link is None:
                    if a.local:
                        link = local_link
                    elif tunnel.ready:
                        link = f"{tunnel.url}/{token}/mcp"
                    elif tunnel.error:
                        live.stop()
                        console.print(f"[red]{tunnel.error}[/]")
                        return 1
                    if link:
                        copied = copy(link)
                        RUN.write_text(json.dumps({"pid": os.getpid(), "link": link, "started": started, "mode": guard.mode, "auto_until": guard.auto_until}))
                        RUN.chmod(0o600)
                if tunnel.proc and tunnel.proc.poll() is not None and link:
                    live.stop()
                    console.print("[red]The tunnel closed unexpectedly.[/] Run porthole start again for a new link.")
                    return 1
                live.update(dashboard(guard, link, "making your link reachable (about 20s)…" if tunnel.url else "opening the tunnel…", started, copied))
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        tunnel.stop()
        server.should_exit = True
        RUN.unlink(missing_ok=True)
    console.print(f"[{BRASS}]Porthole closed.[/] That link no longer works.")
    return 0


def parse_duration(s: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", s.lower())
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)] if m else 0.0


def cmd_stop(_a) -> int:
    info = read_run()
    if not info:
        console.print("Porthole isn't open.")
        return 0
    os.kill(info["pid"], signal.SIGTERM)
    for _ in range(40):
        if not read_run():
            break
        time.sleep(0.1)
    console.print(f"[{BRASS}]Porthole closed.[/] ChatGPT can't reach your Mac any more.")
    return 0


def cmd_status(_a) -> int:
    cfg, info = load_config(), read_run()
    console.print(f"[bold]Porthole[/] {'[green]● open[/]' if info else '[dim]○ closed[/]'}")
    if info:
        console.print(f"  Link     {info['link']}")
    console.print(f"  Folders  {', '.join(cfg['allow'])}")
    mode, until = (info or {}).get("mode", cfg["mode"]), (info or {}).get("auto_until", 0)
    if mode == "auto" and until and time.time() >= until:
        mode = "ask"
    label = {"readonly": "read-only", "auto": "[bold red]allow everything[/]" + (f" until {time.strftime('%H:%M', time.localtime(until))}" if until else "")}
    console.print(f"  Mode     {label.get(mode, 'ask before every change')}")
    console.print(f"  Tunnel   {'ready' if find_cloudflared() else '[red]cloudflared missing[/]'}")
    return 0


def cmd_link(_a) -> int:
    info = read_run()
    if not info:
        console.print("Porthole isn't open. Start it with [bold]porthole start[/].")
        return 1
    print(info["link"])
    copy(info["link"])
    return 0


def cmd_log(a) -> int:
    try:
        lines = LOG.read_text().splitlines()[-a.n:]
    except OSError:
        console.print("Nothing logged yet.")
        return 0
    t = Table(box=None, padding=(0, 1), header_style="dim")
    for c in ("when", "action", "on", ""):
        t.add_column(c)
    for line in lines:
        ev = json.loads(line)
        t.add_row(time.strftime("%b %d %H:%M:%S", time.localtime(ev["t"])), ev["tool"], ev["target"][:80], "[green]✓[/]" if ev["ok"] else f"[red]✕ {ev.get('note', '')[:40]}[/]")
    console.print(t)
    return 0


def cmd_folders(a) -> int:
    cfg = load_config()
    if a.action == "add":
        for p in a.paths:
            r = str(Path(p).expanduser().resolve())
            if r not in cfg["allow"]:
                cfg["allow"].append(r)
    elif a.action == "remove":
        drop = {str(Path(p).expanduser().resolve()) for p in a.paths}
        cfg["allow"] = [p for p in cfg["allow"] if p not in drop] or cfg["allow"]
    save_config(cfg)
    for p in cfg["allow"]:
        console.print(f"  {p}")
    if read_run():
        console.print("[dim]Restart Porthole for changes to apply.[/]")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="porthole", description="Let ChatGPT use your Mac's files and terminal, with your approval for every change.")
    p.add_argument("--version", action="version", version="porthole 1.1.0")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("start", help="open the porthole and get a link for ChatGPT")
    s.add_argument("--allow", action="append", metavar="FOLDER", help="share this folder (repeatable; default ~/Documents)")
    s.add_argument("--read-only", action="store_true", help="ChatGPT can look but not change or run anything")
    s.add_argument("--ask", action="store_true", help="switch back from read-only to asking before changes")
    s.add_argument("--allow-all", action="store_true", help="no popups: changes and commands run without asking (risky commands still ask)")
    s.add_argument("--for", dest="for_", metavar="TIME", help="with --allow-all: go back to asking after this long, e.g. 30m or 2h")
    s.add_argument("--yes", action="store_true", help="skip the allow-all confirmation")
    s.add_argument("--keep-link", action="store_true", help="reuse the last secret (the tunnel address still changes)")
    s.add_argument("--local", action="store_true", help="no tunnel; only apps on this Mac can connect")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="close the porthole from another terminal").set_defaults(fn=cmd_stop)
    sub.add_parser("status", help="show whether it's open and what's shared").set_defaults(fn=cmd_status)
    sub.add_parser("link", help="print and copy the current link").set_defaults(fn=cmd_link)
    lg = sub.add_parser("log", help="show what ChatGPT did")
    lg.add_argument("-n", type=int, default=40)
    lg.set_defaults(fn=cmd_log)
    f = sub.add_parser("folders", help="list, add or remove shared folders")
    f.add_argument("action", nargs="?", choices=["list", "add", "remove"], default="list")
    f.add_argument("paths", nargs="*")
    f.set_defaults(fn=cmd_folders)
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        a = p.parse_args(["start"] + (argv or sys.argv[1:]))
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
