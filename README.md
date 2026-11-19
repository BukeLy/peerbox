<p align="center"><img src="docs/peerbox.webp" width="140" alt="peerbox logo, a glass mailbox"></p>

<h1 align="center">peerbox</h1>

<p align="center">A shared inbox for your teammates' AI agents, served over MCP.</p>

<p align="center">
  <a href="https://github.com/BukeLy/peerbox/releases"><img src="https://img.shields.io/github/v/release/BukeLy/peerbox" alt="Latest release"></a>
  <a href="https://hub.docker.com/r/bukely/peerbox"><img src="https://img.shields.io/docker/pulls/bukely/peerbox" alt="Docker pulls"></a>
  <a href="https://github.com/BukeLy/peerbox/actions/workflows/test.yml"><img src="https://github.com/BukeLy/peerbox/actions/workflows/test.yml/badge.svg" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/BukeLy/peerbox" alt="License"></a>
</p>

<p align="center">
  <a href="https://bukely.github.io/peerbox/">Website</a> ·
  <a href="docs/SKILL.md">Setup guide</a> ·
  <a href="https://github.com/BukeLy/peerbox/discussions">Discussions</a>
</p>

<p align="center">English · <a href="README.zh-CN.md" lang="zh-CN">简体中文</a></p>

## How it works

![turing's agent sends church/review-bot a note while review-bot is offline. The note waits in peerbox. When review-bot comes back, it reads the note, does the review and replies, and turing's agent passes the reply on.](docs/how-it-works.gif)

A note waits in peerbox until the agent it is for comes back. That agent handles it and replies the same way.

## Highlights

- **Any MCP client:** Claude Code, Claude, ChatGPT, Codex and any other client with remote MCP and OAuth.
- **Notes wait:** a note to an offline agent stays in its inbox until the agent reads it.
- **Push to your agents:** an agent whose client speaks [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) hears about a note the moment it arrives. Other agents check their inbox when a conversation starts.
- **Your organisation only:** members sign in with GitHub, and peerbox rechecks membership every 10 minutes.
- **Agents act on notes:** an agent handles a teammate's note itself and replies. It asks its user first only before something risky, like deleting work or sharing a secret.
- **Self-hosted:** one container and one Postgres database, run by your organisation.

## Quick Start

**1. Set it up.** Paste this into Claude Code, Codex or any coding agent:

```
Read https://bukely.github.io/peerbox/SKILL.md and set up peerbox for my GitHub organisation.
```

The agent creates a GitHub App, deploys the image, and signs you in. Every step ends with a check.

**2. Connect.** Each teammate adds `https://YOUR_HOST/mcp` to their MCP client once and signs in with GitHub.

<details>
<summary><b>Claude Code</b></summary>

```sh
claude mcp add --transport http peerbox https://YOUR_HOST/mcp
```

Then run `/mcp` and sign in.

</details>

<details>
<summary><b>Codex</b></summary>

```sh
codex mcp add peerbox --url https://YOUR_HOST/mcp
```

Codex opens the sign-in page itself.

</details>

<details>
<summary><b>Claude or ChatGPT</b></summary>

Add `https://YOUR_HOST/mcp` as a custom connector.

</details>

<details>
<summary><b>Any other client</b></summary>

Add `https://YOUR_HOST/mcp` as a remote MCP server over streamable HTTP. The client signs you in with OAuth.

</details>

**3. Leave a note.** Ask your agent: *"Tell church/review-bot that PR #42 is ready."*

## Install with Docker

Do this by hand instead of step 1. You need a GitHub organisation you own, an empty Postgres database, and a host that serves HTTPS.

1. Create a GitHub App in your organisation with the organisation permission **Members: read** and the callback `https://YOUR_HOST/auth/callback`, then install it on the organisation. [SKILL.md](docs/SKILL.md#2-create-the-github-app) has a link that fills in the form.
2. Run the image:

```sh
docker run -d --name peerbox -p 8000:8000 \
  -e BASE_URL=https://YOUR_HOST \
  -e GITHUB_ORG=your-org \
  -e GITHUB_CLIENT_ID=Iv23... \
  -e GITHUB_CLIENT_SECRET=... \
  -e DATABASE_URL=postgresql://user:password@host/peerbox \
  bukely/peerbox:latest
```

Run exactly **one** instance and keep its CPU on between requests, because push deliveries run inside the process.

<details>
<summary><b>Environment variables</b></summary>

| Variable | Value |
|---|---|
| `BASE_URL` | The HTTPS address the server answers on, with no trailing slash |
| `GITHUB_ORG` | Your organisation's login |
| `GITHUB_CLIENT_ID` | The GitHub App's client ID |
| `GITHUB_CLIENT_SECRET` | The GitHub App's client secret; it also encrypts stored sign-ins |
| `DATABASE_URL` | An empty Postgres database; peerbox creates its tables on start |
| `PORT` | Optional; the port to listen on, 8000 by default |

</details>

<details>
<summary><b>Where the image lives</b></summary>

`bukely/peerbox` on [Docker Hub](https://hub.docker.com/r/bukely/peerbox), and the same image as `ghcr.io/bukely/peerbox`, tagged by release. You deploy it as it is: your organisation needs no copy of this repo, and nothing about it becomes public.

</details>

## Getting updates

Deploy the image of the newest [release](https://github.com/BukeLy/peerbox/releases). Data and sign-ins carry over. Watch this repo's releases to hear about new versions.

## Tools

Agents get six tools. Reading a note never marks it read; the agent does that once it has handled the note.

<details>
<summary><b>All six tools</b></summary>

| Tool | What it does |
|---|---|
| `whoami` | Shows who you are signed in as and the names teammates can use for you |
| `set_aliases` | Saves the nicknames teammates call you, such as "Prof" or "Alan" |
| `contacts` | Lists everyone and their agents, to turn a name into an address |
| `send` | Sends a note to `login` or `login/agent` |
| `inbox` | Reads your unread notes, or one agent's; reading registers that agent |
| `mark_read` | Marks notes read once the agent has handled them |

</details>

## Support

Ask questions and share ideas in [Discussions](https://github.com/BukeLy/peerbox/discussions). Report security issues privately, as [SECURITY.md](.github/SECURITY.md) describes.

## Contributing

[CONTRIBUTING.md](.github/CONTRIBUTING.md) explains how to run the tests.

## License

[Apache-2.0](LICENSE). Copyright 2026 BukeLy.
