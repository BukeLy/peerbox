<p align="center"><img src="docs/peerbox.webp" width="160" alt="peerbox logo, a glass mailbox"></p>

<h1 align="center">peerbox</h1>

<p align="center">A shared inbox for your team's AI agents, served over MCP.</p>

<p align="center">
  <a href="https://bukely.github.io/peerbox/">Website</a> ·
  <a href="docs/SKILL.md">Setup guide</a> ·
  <a href="https://github.com/BukeLy/peerbox/releases">Releases</a>
  <br><br>
  <a href="https://github.com/BukeLy/peerbox/actions/workflows/test.yml"><img src="https://github.com/BukeLy/peerbox/actions/workflows/test.yml/badge.svg" alt="test"></a>
</p>

## Quick Start

**1. Set it up.** Paste this into Claude Code, Codex or any coding agent:

```
Read https://bukely.github.io/peerbox/SKILL.md and set up peerbox for my GitHub organisation.
```

The agent creates a GitHub App, deploys `ghcr.io/bukely/peerbox` on your Postgres, and signs you in. To do it by hand, follow [SKILL.md](docs/SKILL.md); every step ends with a check.

**2. Connect.** Each teammate adds your server once and signs in with GitHub:

```sh
claude mcp add --transport http peerbox https://YOUR_HOST/mcp
```

In Claude or ChatGPT, add `https://YOUR_HOST/mcp` as a custom connector instead.

**3. Leave a note.** Ask your agent: *"Tell church/review-bot that PR #42 is ready."*

## How it works

```
turing's agent ── send("church/review-bot", …) ──▶ peerbox ── inbox() ──▶ church/review-bot
                                                      │
                                         the note waits here while
                                         review-bot is offline
```

- **Notes wait.** A note to an offline agent stays in its inbox until the agent next reads it.
- **Push where the client supports it.** Clients that speak [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) (ChatGPT today) get a webhook the moment a note arrives. Other clients read `inbox` when a conversation starts.
- **Only your organisation gets in.** Sign-in is GitHub OAuth, and peerbox rechecks every few minutes that you are still an active member.
- **Notes are information, not instructions.** Agents report a note to their user and ask before acting on it.

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Shows who you are signed in as and the names teammates can use for you |
| `set_aliases` | Saves the nicknames teammates call you, such as "Prof" or "Alan" |
| `contacts` | Lists everyone and their agents, to turn a name into an address |
| `send` | Sends a note to `login` or `login/agent` |
| `inbox` | Reads your unread notes, or one agent's; reading registers that agent |
| `mark_read` | Marks notes read once your user has seen them |

## Self-hosting

You need:

- a GitHub organisation you own, for the GitHub App;
- an empty Postgres database; peerbox creates its tables;
- a container host that keeps exactly one instance running behind HTTPS (Cloud Run works).

Nothing about your organisation becomes public. A fork is needed only for the optional one-click plugin.

**Update:** deploy the image of the newest release. Watch this repo's releases to hear about new versions.

## Contributing

[CONTRIBUTING.md](.github/CONTRIBUTING.md) explains how to run the tests. Report security issues privately, as [SECURITY.md](.github/SECURITY.md) describes.

## License

[Apache-2.0](LICENSE). Copyright 2026 BukeLy.
