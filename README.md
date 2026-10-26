<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" width="360" alt="Porthole">
  </picture>
</p>
<p align="center"><b>Let a normal ChatGPT chat use your Mac's files and terminal, like Claude Code.</b><br>
You approve every change. Close the window and the access is gone.</p>

## Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/learningnihongo934-byte/porthole/main/install.sh | bash
```

## Use it

```bash
porthole start
```

This shares `~/Documents`, opens a secure public link to your Mac and copies it. Then in ChatGPT:

1. **Settings → Apps → Advanced settings → Developer mode** (switch it on), then **Create app**.
2. Name it Porthole, paste the link, choose **No authentication**, confirm, and create it.
3. In a chat, open **+ → Developer mode**, pick **Porthole**, and ask for something like *"Find the TODOs in my Mnemo project and fix the easy ones."*

OpenAI moves these menus around now and then. If you can't find them, look for "Developer mode" in ChatGPT's settings.

**Which ChatGPT plans can do what.** OpenAI's help page currently lists full MCP (which includes edits and commands) as a beta for Business, Enterprise and Edu. On Plus and Pro, Developer mode may only allow the read-only tools. In that case ChatGPT can still read and search your files.

## What ChatGPT can do

| Tool | What it does | Asks you first |
|---|---|---|
| `get_status` | Shows the shared folders and what's installed | |
| `list_dir`, `read_file` | Browses folders and reads files with line numbers | |
| `find_files`, `search_text` | Finds files by name and searches inside them | |
| `edit_file` | Makes a precise change and shows you the diff | ✓ |
| `write_file` | Creates or replaces a file | ✓ |
| `run_command` | Runs a terminal command in a shared folder | ✓ |
| `take_screenshot` | Lets ChatGPT see your screen | ✓ |

## Safety

Porthole gives a website access to your computer, so it is strict by design:

- **Secret link.** A new 256-bit secret is in every link. Anyone without it gets a 404. The link dies when you close Porthole.
- **Only folders you share.** The default is `~/Documents`. It refuses to share your whole home folder or disk.
- **Never shared, even inside a shared folder:** `~/.ssh`, `~/.aws`, `~/.gnupg`, keychains, cookies, Mail, Messages, browser profiles, `.env` files and private keys.
- **A popup for every change.** Each edit, file write, command and screenshot shows a native macOS dialog with the exact diff or command. *Deny* is the default and it gives up after 2 minutes. *Allow for 10 min* exists for long sessions, and the dashboard shows a countdown while it's on.
- **Read-only mode.** `porthole start --read-only` means nothing can change.
- **A log of everything.** The terminal shows each action live, and `porthole log` keeps the history in `~/.porthole/log.jsonl`.
- **Kill switch.** Press Ctrl-C or close the window, or run `porthole stop` from anywhere.

Prompt injection is real. A web page or file ChatGPT reads could try to trick it into asking for something harmful. That's why you approve each change: read what the popup says before you click Allow.

## Commands

```bash
porthole start [--allow FOLDER]... [--read-only] [--keep-link] [--local]
porthole stop           # close it from another terminal
porthole status         # open or closed, folders, mode
porthole link           # print and copy the current link
porthole log [-n 40]    # what ChatGPT did
porthole folders add ~/Projects     # remember extra folders
```

`--local` skips the tunnel, for MCP clients on your own Mac.

## How it works

Porthole is a [Model Context Protocol](https://modelcontextprotocol.io) server (Streamable HTTP) built on the official Python SDK. A [Cloudflare quick tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/) gives it a temporary `https://….trycloudflare.com` address, which needs no Cloudflare account. ChatGPT's Developer mode connects to that address like any other app.

## Build and test

```bash
./build.sh            # end-to-end test on this Mac, then build
./build.sh --tunnel   # the same test over a real public HTTPS tunnel
./build.sh --install  # install the porthole command from this folder
```

The test starts a real server, calls every tool through an MCP client, and checks approvals, denials and blocked paths.

## Uninstall

```bash
curl -fsSL https://raw.githubusercontent.com/learningnihongo934-byte/porthole/main/install.sh | bash -s -- --uninstall
```

---

Made by Virat Reddy Kaukuntla · [rennko.tech](https://rennko.tech)
