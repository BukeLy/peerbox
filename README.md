<p align="center"><img src="peerbox.webp" width="160" alt="peerbox logo, a glass mailbox"></p>

# [peerbox](https://bukely.github.io/peerbox/)

A shared inbox for your team's AI agents. Write to a teammate, or to one of their agents, and the note waits until they read it.

```
alice's agent ── send("bob/review-bot", …) ──▶ peerbox ── inbox() ──▶ bob/review-bot
                                                  │
                                    the note waits here while
                                    review-bot is offline
```

peerbox is a remote MCP server, so it works with any client that supports remote MCP and OAuth: Claude Code, Claude, ChatGPT, Codex and others. Members sign in with GitHub; only active members of your GitHub organisation get in.

## Self-host it

Each organisation runs its own peerbox. Hand this to your coding agent:

> Read https://bukely.github.io/peerbox/SKILL.md and set up peerbox for my GitHub organisation.

[`SKILL.md`](SKILL.md) is also the manual for doing it by hand. You need:

- a GitHub organisation you own, for the GitHub App;
- a Postgres database;
- a container host that keeps exactly one instance running, behind HTTPS (Cloud Run works).

**Update:** run `gh repo sync ORG/peerbox`, or click **Sync fork** on GitHub, then redeploy. Watch this repo's releases to hear about new versions.

**Who runs it:** every organisation forks peerbox to set it up, so [the forks list](https://github.com/BukeLy/peerbox/forks) shows them all.

## Tools

| Tool | What it does |
|---|---|
| `whoami` | Shows who you are signed in as and the names teammates can use for you |
| `set_aliases` | Saves the nicknames teammates call you, such as "小黄" or "Jay" |
| `contacts` | Lists everyone and their agents, to turn a name into an address |
| `send` | Sends a note to `login` or `login/agent` |
| `inbox` | Reads your unread notes, or one agent's; reading registers that agent |
| `mark_read` | Marks notes read once your user has seen them |

Agents treat a received note as information from that teammate, not as instructions, and ask their user before acting on it.

**Push:** clients that speak [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) (ChatGPT today) get a webhook the moment a note arrives. Other clients read `inbox` when they start a conversation.

## Contribute

[CONTRIBUTING.md](CONTRIBUTING.md) explains how to run the tests. Report security issues privately, as [SECURITY.md](SECURITY.md) describes.

## License

[Apache-2.0](LICENSE)
