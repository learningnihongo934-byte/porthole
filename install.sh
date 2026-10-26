#!/usr/bin/env bash
# Porthole installer for macOS: lets ChatGPT use your Mac's files and terminal, with your approval for every change.
#   curl -fsSL https://raw.githubusercontent.com/learningnihongo934-byte/porthole/main/install.sh | bash
#   ... | bash -s -- --uninstall
set -euo pipefail
REPO="learningnihongo934-byte/porthole"

if [[ -t 1 ]]; then B=$'\033[1m'; D=$'\033[2m'; R=$'\033[31m'; Y=$'\033[33m'; G=$'\033[32m'; C=$'\033[36m'; X=$'\033[0m'; else B= D= R= Y= G= C= X=; fi
step() { printf "%s▸%s %s\n" "$C" "$X" "$*"; }
ok()   { printf "%s✓%s %s\n" "$G" "$X" "$*"; }
die()  { printf "%s✗ %s%s\n" "$R" "$*" "$X" >&2; exit 1; }

printf "\n  %s╭───────╮%s\n %s╭┤%s %s▸ _%s   %s├╮%s  %sPorthole%s\n %s╰┤%s       %s├╯%s  %slet ChatGPT use your Mac, safely%s\n  %s╰───────╯%s\n\n" \
  "$Y" "$X" "$Y" "$X" "$C" "$X" "$Y" "$X" "$B" "$X" "$Y" "$X" "$Y" "$X" "$D" "$X" "$Y" "$X"

if [[ "${1:-}" == "--uninstall" ]]; then
  porthole stop >/dev/null 2>&1 || true
  uv tool uninstall porthole >/dev/null 2>&1 && ok "Removed the porthole command" || true
  rm -rf "$HOME/.porthole" && ok "Removed settings and the action log"
  exit 0
fi

[[ "$(uname -s)" == "Darwin" ]] || die "Porthole is for macOS (it uses native approval popups)."

if ! command -v uv >/dev/null 2>&1; then
  step "Installing uv (Python tool manager)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
  export PATH="$HOME/.local/bin:$PATH"
fi

if ! command -v cloudflared >/dev/null 2>&1 && [[ ! -x "$HOME/.porthole/bin/cloudflared" ]]; then
  step "Installing cloudflared (makes the secure public link)"
  if command -v brew >/dev/null 2>&1; then
    brew install cloudflared >/dev/null
  else
    arch=$([[ "$(uname -m)" == "arm64" ]] && echo arm64 || echo amd64)
    mkdir -p "$HOME/.porthole/bin"
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-$arch.tgz" | tar -xz -C "$HOME/.porthole/bin"
    chmod +x "$HOME/.porthole/bin/cloudflared"
  fi
fi

step "Installing Porthole"
uv tool install --force --quiet "git+https://github.com/$REPO" >/dev/null
uv tool update-shell >/dev/null 2>&1 || true
ok "Installed. Run ${B}porthole start${X} to open it."

cat <<MSG

${B}How it works${X}
  1. ${B}porthole start${X} shares ~/Documents and copies a secret link.
     Share other folders with ${B}porthole start --allow ~/Projects${X}, or look only with ${B}--read-only${X}.
  2. In ChatGPT: Settings → Apps → Advanced settings → Developer mode → Create app.
     Paste the link, choose No authentication, and create it.
  3. In a chat, pick Developer mode → Porthole and ask away.
     Every edit and command pops up on your Mac for you to Allow or Deny.
  4. Close the terminal window (or run ${B}porthole stop${X}) and the link stops working.

${D}Uninstall: curl -fsSL https://raw.githubusercontent.com/$REPO/main/install.sh | bash -s -- --uninstall${X}
MSG
