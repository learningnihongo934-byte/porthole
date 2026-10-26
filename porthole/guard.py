"""Safety layer: which folders ChatGPT may touch, approval popups, and the action log."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

HOME = Path.home()
STATE = HOME / ".porthole"
CONFIG = STATE / "config.json"
LOG = STATE / "log.jsonl"
RUN = STATE / "run.json"

# Never readable or writable, even inside an allowed folder.
BLOCKED = [
    HOME / ".ssh", HOME / ".gnupg", HOME / ".aws", HOME / ".kube", HOME / ".docker",
    HOME / ".config" / "gh", HOME / ".netrc", HOME / ".git-credentials", HOME / ".npmrc", HOME / ".pypirc",
    HOME / "Library" / "Keychains", HOME / "Library" / "Cookies", HOME / "Library" / "Messages",
    HOME / "Library" / "Mail", HOME / "Library" / "Safari",
    HOME / "Library" / "Application Support" / "Google" / "Chrome",
    HOME / "Library" / "Application Support" / "Firefox",
    HOME / "Library" / "Application Support" / "com.apple.TCC",
    STATE,
]
SECRET_NAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_ed25519", "credentials.json", "secrets.json"}

DEFAULTS = {"allow": [str(HOME / "Documents")], "mode": "ask", "token": None}


def load_config() -> dict:
    try:
        return {**DEFAULTS, **json.loads(CONFIG.read_text())}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_config(cfg: dict) -> None:
    STATE.mkdir(mode=0o700, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))
    CONFIG.chmod(0o600)


class Denied(Exception):
    """Raised when a request is outside what the user allowed. The message goes back to ChatGPT."""


def mac_dialog(title: str, detail: str) -> str:
    """Shows a native approval dialog and returns 'allow', 'allow10' or 'deny'. Text is passed as argv, never as code."""
    detail = detail if len(detail) < 1800 else detail[:1800] + "\n… (truncated)"
    script = [
        "on run argv",
        'set r to display dialog (item 2 of argv) with title (item 1 of argv) '
        'buttons {"Deny", "Allow for 10 min", "Allow"} default button "Deny" cancel button "Deny" '
        "giving up after 120 with icon caution",
        "if gave up of r then return \"deny\"",
        "return button returned of r",
        "end run",
    ]
    args = ["osascript"] + sum([["-e", line] for line in script], []) + [title, detail]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=130).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "deny"
    return {"Allow": "allow", "Allow for 10 min": "allow10"}.get(out, "deny")


@dataclass
class Guard:
    roots: list[Path]
    mode: str = "ask"                               # "ask" or "readonly"
    approver: Callable[[str, str], str] = mac_dialog
    events: deque = field(default_factory=lambda: deque(maxlen=200))
    trusted_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_config(cls, cfg: dict, **kw) -> "Guard":
        roots = [Path(p).expanduser().resolve() for p in cfg["allow"]]
        return cls(roots=roots, mode=cfg.get("mode", "ask"), **kw)

    # ---- paths

    def resolve(self, raw: str | None) -> Path:
        raw = (raw or ".").strip()
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        p = p.resolve()
        if not any(p == r or p.is_relative_to(r) for r in self.roots):
            raise Denied(f"{p} is outside the folders you shared: {', '.join(map(str, self.roots))}")
        if any(p == b or p.is_relative_to(b) for b in BLOCKED) or p.name in SECRET_NAMES:
            raise Denied(f"{p} is a private location (keys, passwords or app data) that Porthole never shares.")
        return p

    def show(self, p: Path) -> str:
        """Paths in replies are shown relative to home, so ChatGPT never needs the full username path."""
        return "~/" + str(p.relative_to(HOME)) if p.is_relative_to(HOME) else str(p)

    # ---- approvals

    def approve(self, action: str, detail: str) -> None:
        if self.mode == "readonly":
            raise Denied("Porthole is in read-only mode, so nothing can be changed or run. Ask the user to restart it without --read-only.")
        with self._lock:                            # one popup at a time
            if time.time() < self.trusted_until:
                return
            answer = self.approver(f"ChatGPT wants to {action}", detail)
        if answer == "allow10":
            self.trusted_until = time.time() + 600
        elif answer != "allow":
            raise Denied("The user said no. Don't retry the same thing; ask them what they'd like instead.")

    # ---- log

    def log(self, tool: str, target: str, ok: bool = True, note: str = "") -> None:
        ev = {"t": time.time(), "tool": tool, "target": target, "ok": ok, "note": note}
        self.events.append(ev)
        try:
            STATE.mkdir(mode=0o700, exist_ok=True)
            with LOG.open("a") as f:
                f.write(json.dumps(ev) + "\n")
            LOG.chmod(0o600)
        except OSError:
            pass


def read_run() -> dict | None:
    try:
        info = json.loads(RUN.read_text())
        os.kill(info["pid"], 0)
        return info
    except (OSError, ValueError, KeyError):
        return None
