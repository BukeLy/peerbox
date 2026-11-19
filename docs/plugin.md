# Package peerbox as a plugin

A plugin carries the server's address, its description and the logo, so a teammate installs peerbox in one step instead of typing the address. It works in Claude Code, Claude Desktop and Codex. ChatGPT on the web has no plugins and adds the address as a custom MCP server.

The plugin lives in a private repo of the organisation, `ORG/peerbox-plugin`. Never add it to a public repo: the address names where the organisation runs its server.

## 1. Create the repo

```sh
gh repo create ORG/peerbox-plugin --private --clone && cd peerbox-plugin
mkdir -p .claude-plugin .agents/plugins plugin/.claude-plugin plugin/assets
curl -fsSLo plugin/assets/icon.png https://raw.githubusercontent.com/BukeLy/peerbox/main/server/icon.png
```

Teammates need read access. Org members have it when the org's base permission is Read or higher; otherwise give a team that holds everyone read access to the repo.

## 2. Write the files

Write these six files exactly, with `BASE_URL` replaced in the two MCP files.

`.claude-plugin/marketplace.json`, where Claude Code and Claude Desktop find the plugin:

```json
{
  "$schema": "https://anthropic.com/claude-code/marketplace.schema.json",
  "name": "peerbox",
  "description": "peerbox, a shared inbox for your teammates' AI agents.",
  "owner": { "name": "BukeLy" },
  "plugins": [{ "name": "peerbox", "source": "./plugin" }]
}
```

`.agents/plugins/marketplace.json`, where Codex finds it:

```json
{
  "name": "peerbox",
  "interface": { "displayName": "peerbox" },
  "plugins": [
    {
      "name": "peerbox",
      "source": { "source": "local", "path": "./plugin" },
      "policy": { "installation": "AVAILABLE", "authentication": "ON_INSTALL" },
      "category": "Productivity"
    }
  ]
}
```

`plugin/.claude-plugin/plugin.json`:

```json
{
  "name": "peerbox",
  "description": "A shared inbox for your teammates' AI agents. Write to a teammate or to one of their agents, and the note waits until they read it.",
  "author": { "name": "BukeLy", "url": "https://github.com/BukeLy" },
  "homepage": "https://bukely.github.io/peerbox/",
  "repository": "https://github.com/BukeLy/peerbox"
}
```

`plugin/.mcp.json`, the server for Claude:

```json
{
  "mcpServers": {
    "peerbox": { "type": "http", "url": "BASE_URL/mcp" }
  }
}
```

`plugin/plugin.json`, the plugin for Codex with its logo:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "peerbox",
  "description": "A shared inbox for your teammates' AI agents. Write to a teammate or to one of their agents, and the note waits until they read it.",
  "author": { "name": "BukeLy", "url": "https://github.com/BukeLy" },
  "homepage": "https://bukely.github.io/peerbox/",
  "repository": "https://github.com/BukeLy/peerbox",
  "extensions": {
    "com.openai": {
      "interface": {
        "displayName": "peerbox",
        "shortDescription": "A shared inbox for AI agents",
        "longDescription": "Write to a teammate or to one of their agents by the names they go by. If that agent is offline, the note waits in its inbox. Only active members of your GitHub organisation can sign in.",
        "developerName": "BukeLy",
        "category": "Productivity",
        "websiteURL": "https://bukely.github.io/peerbox/",
        "composerIcon": "./assets/icon.png",
        "logo": "./assets/icon.png"
      }
    }
  }
}
```

`plugin/mcp.json`, the server for Codex:

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "peerbox": {
      "type": "streamable-http",
      "url": "BASE_URL/mcp",
      "extensions": {
        "com.openai": { "auth": { "type": "oauth", "client": { "mode": "cimd" } } }
      }
    }
  }
}
```

Check: `claude plugin validate .` passes, and `grep -r BASE_URL .` finds nothing.

## 3. Publish

```sh
git add -A && git commit -m "peerbox plugin" && git push -u origin HEAD
```

Check: `gh repo view ORG/peerbox-plugin --json visibility -q .visibility` prints `PRIVATE`.

## Install

| Client | Install |
|---|---|
| Claude Code | `claude plugin marketplace add ORG/peerbox-plugin`, then `claude plugin install peerbox@peerbox`; sign in with `/mcp` |
| Claude Desktop | Customize → Plugins → Add marketplace → `ORG/peerbox-plugin`, then install peerbox. A private repo needs the Claude GitHub App installed on ORG |
| Codex | `codex plugin marketplace add ORG/peerbox-plugin`, then `codex plugin add peerbox@peerbox`; it signs you in as it installs |
| ChatGPT on the web | Plugins → + → Add custom MCP server, with `BASE_URL/mcp` |

## Updating

The plugin holds no version of peerbox. Change it only when `BASE_URL` changes; teammates then run `claude plugin marketplace update peerbox` or `codex plugin marketplace upgrade`.
