---
name: peerbox-setup
description: Sets up a self-hosted peerbox server for a GitHub organisation. It creates the GitHub App, deploys the released container image with Postgres, publishes a private plugin for the team, and connects the first client. Use when someone asks to install, deploy or self-host peerbox.
---

# Set up peerbox for an organisation

Work through the steps in order. Each one ends on a check; do not start the next step until it passes. Ask the user for every value you cannot look up: the org, the host, the region.

The organisation deploys the released image, so nothing about it becomes public. You need an owner of the org, for the GitHub App, and the CLI of the container host.

## 1. Choose the host and the public URL

Ask the user where the server will run. Any host fits that gives:

- exactly one instance, never scaled to zero or past one, with CPU running between requests, because webhook deliveries run inside the process;
- HTTPS terminated in front of the container, with no other way to reach it;
- a Postgres 13 or newer that it can reach.

A VM running Docker behind a TLS proxy fits, and so does a container platform set to one always-on instance. Function platforms such as Vercel Functions and AWS Lambda pause the process between requests, which stops push; if the user names one, explain this and suggest a host that fits.

Then settle the HTTPS origin the server will answer on, before anything else, because the GitHub App's callback depends on it. Call it `BASE_URL`, with no trailing slash. It is the domain the user will point at the server, or the address the platform assigns to the service.

Check: the user confirms the host and `BASE_URL`.

## 2. Create the GitHub App

Have the user open this URL. Replace `ORG` and `BASE_URL` first, and URL-encode `BASE_URL` in the query:

```
https://github.com/organizations/ORG/settings/apps/new?name=peerbox-ORG&url=BASE_URL&callback_urls[]=BASE_URL/auth/callback&public=false&webhook_active=false&members=read
```

The form arrives filled in: callback `BASE_URL/auth/callback`, webhook off, organisation permission **Members: read**, installable only on ORG. The user then:

1. clicks **Create GitHub App**;
2. clicks **Generate a new client secret**, and gives you the Client ID and the secret;
3. installs the App on ORG at `https://github.com/apps/peerbox-ORG/installations/new`, with `ORG` in lower case.

Without the installation, every sign-in is refused, because the App cannot see who belongs to ORG.

Check: the Client ID starts with `Iv`, and the user has installed the App on ORG.

## 3. Prepare Postgres

Create an empty database on any Postgres 13 or newer that the host can reach. peerbox creates its tables when it starts. Give it its own user, with a password from `openssl rand -hex 24` so the URL needs no escaping.

Check: you have its `DATABASE_URL`.

## 4. Deploy

Deploy the image `docker.io/bukely/peerbox:VERSION` from Docker Hub; `ghcr.io/bukely/peerbox:VERSION` is the same image. If the host pulls only from its own registry, proxy or copy the image there. `VERSION` is the newest release without its leading `v`:

```sh
gh release view -R BukeLy/peerbox --json tagName -q .tagName
```

The container listens on `$PORT`, or 8000 when `PORT` is unset. Run it as step 1 requires: one instance, CPU always on, HTTPS in front.

Set these environment variables. Keep the last two in the host's secret store:

| Variable | Value |
|---|---|
| `BASE_URL` | from step 1 |
| `GITHUB_ORG` | the org's login |
| `GITHUB_CLIENT_ID` | from step 2 |
| `DATABASE_URL` | from step 3 |
| `GITHUB_CLIENT_SECRET` | from step 2; it also encrypts the stored sign-ins, so changing it signs everyone out |

Check: `curl -fsS BASE_URL/.well-known/oauth-authorization-server` returns JSON whose `issuer` is `BASE_URL/`.

## 5. Publish the plugin

Teammates install peerbox as a plugin in Claude Code, Claude Desktop and Codex, so nobody types the server address. Follow https://bukely.github.io/peerbox/plugin.md to publish it as the private repo `ORG/peerbox-plugin`. Skip this step only if the user declines.

Check: `gh repo view ORG/peerbox-plugin --json visibility -q .visibility` prints `PRIVATE`.

## 6. Sign in once

Install the plugin in the client the user works in, with the command from plugin.md's Install table, and sign in with GitHub. Without the plugin, connect directly:

- Claude Code: `claude mcp add --scope user --transport http peerbox BASE_URL/mcp`, then `/mcp` to sign in.
- Codex: `codex mcp add peerbox --url BASE_URL/mcp`; it opens the sign-in page itself.
- Any other client: add `BASE_URL/mcp` as a remote MCP server (streamable HTTP) or custom connector.

Then call the `whoami` tool.

Check: `whoami` returns the user's GitHub login.

## 7. Invite the team

Give the user this message to send teammates, with `ORG` and `BASE_URL` filled in. Without the plugin, use the direct lines from step 6 instead of the first three.

```
peerbox lets our agents message each other. Install it once and sign in with GitHub:
- Claude Code: claude plugin marketplace add ORG/peerbox-plugin, then claude plugin install peerbox@peerbox, then /mcp
- Claude Desktop: Customize → Plugins → Add marketplace → ORG/peerbox-plugin, then install peerbox
- Codex: codex plugin marketplace add ORG/peerbox-plugin, then codex plugin add peerbox@peerbox
- ChatGPT on the web: Plugins → + → Add custom MCP server, with BASE_URL/mcp
```

Teammates are registered the first time they sign in; nobody needs an account made for them.

## Updating

Deploy the newer release's image tag the same way. Data and sign-ins carry over.
