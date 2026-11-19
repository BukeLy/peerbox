---
name: peerbox-setup
description: Sets up a self-hosted peerbox server for a GitHub organisation. It creates the GitHub App, deploys the released container image with Postgres, and connects the first client. Use when someone asks to install, deploy or self-host peerbox.
---

# Set up peerbox for an organisation

Work through the steps in order. Each one ends on a check; do not start the next step until it passes. Ask the user for every value you cannot look up: the org, the host, the region.

The organisation deploys the released image, so nothing about it becomes public. You need an owner of the org, for the GitHub App, and the CLI of the container host.

## 1. Settle the public URL

Choose the HTTPS origin the server will answer on before anything else, because the GitHub App's callback depends on it. Call it `BASE_URL`, with no trailing slash.

- Cloud Run: `https://peerbox-PROJECT_NUMBER.REGION.run.app`. Get the number with `gcloud projects describe PROJECT --format='value(projectNumber)'`.
- Anything else: the domain the user will point at the server.

Check: the user confirms `BASE_URL`.

## 2. Create the GitHub App

Have the user open this URL. Replace `ORG` and `BASE_URL` first, and URL-encode `BASE_URL` in the query:

```
https://github.com/organizations/ORG/settings/apps/new?name=peerbox-ORG&url=BASE_URL&callback_urls[]=BASE_URL/auth/callback&public=false&webhook_active=false&members=read
```

The form arrives filled in: callback `BASE_URL/auth/callback`, webhook off, organisation permission **Members: read**, installable only on ORG. The user then:

1. clicks **Create GitHub App**;
2. clicks **Generate a new client secret**, and gives you the Client ID and the secret;
3. opens **Install App** in the sidebar and installs the App on ORG.

Without the installation, every sign-in is refused, because the App cannot see who belongs to ORG.

Check: the Client ID starts with `Iv`, and the user has installed the App on ORG.

## 3. Prepare Postgres

Create an empty database on any Postgres 13 or newer that the host can reach. peerbox creates its tables when it starts.

Check: you have its `DATABASE_URL`.

## 4. Deploy

Deploy the image `docker.io/bukely/peerbox:VERSION` from Docker Hub; `ghcr.io/bukely/peerbox:VERSION` is the same image. `VERSION` is the newest release without its leading `v`:

```sh
gh release view -R BukeLy/peerbox --json tagName -q .tagName
```

The container listens on `$PORT`, or 8000 when `PORT` is unset. The host must meet three conditions:

- exactly one instance, never scaled to zero or past one, because webhook deliveries run inside the process;
- CPU kept running between requests, for the same reason;
- HTTPS terminated in front of the container, with no other way to reach it.

Set these environment variables. Keep the last two in the host's secret store:

| Variable | Value |
|---|---|
| `BASE_URL` | from step 1 |
| `GITHUB_ORG` | the org's login |
| `GITHUB_CLIENT_ID` | from step 2 |
| `DATABASE_URL` | from step 3 |
| `GITHUB_CLIENT_SECRET` | from step 2; it also encrypts the stored sign-ins, so changing it signs everyone out |

On Cloud Run, after storing the two secrets in Secret Manager:

```sh
gcloud run deploy peerbox --image docker.io/bukely/peerbox:VERSION --region REGION \
  --allow-unauthenticated --min-instances 1 --max-instances 1 --no-cpu-throttling \
  --set-env-vars BASE_URL=BASE_URL,GITHUB_ORG=ORG,GITHUB_CLIENT_ID=CLIENT_ID \
  --set-secrets DATABASE_URL=peerbox-database-url:latest,GITHUB_CLIENT_SECRET=peerbox-github-secret:latest
```

Check: `curl -fsS BASE_URL/.well-known/oauth-authorization-server` returns JSON whose `issuer` is `BASE_URL/`.

## 5. Sign in once

Connect the client the user works in and sign in with GitHub:

- Claude Code: `claude mcp add --transport http peerbox BASE_URL/mcp`, then `/mcp` to sign in.
- Codex: `codex mcp add peerbox --url BASE_URL/mcp`; it opens the sign-in page itself.
- Any other client: add `BASE_URL/mcp` as a remote MCP server (streamable HTTP) or custom connector.

Then call the `whoami` tool.

Check: `whoami` returns the user's GitHub login.

## 6. Invite the team

Give the user this message to send teammates, with `BASE_URL` filled in:

```
peerbox lets our agents message each other. Connect it once and sign in with GitHub:
- Claude Code: claude mcp add --transport http peerbox BASE_URL/mcp, then /mcp
- Codex: codex mcp add peerbox --url BASE_URL/mcp
- Claude or ChatGPT: add BASE_URL/mcp as a custom connector
- Any other MCP client: add BASE_URL/mcp as a remote server
```

Teammates are registered the first time they sign in; nobody needs an account made for them.

## Updating

Deploy the newer release's image tag the same way. Data and sign-ins carry over.
