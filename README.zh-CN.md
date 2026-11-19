<p align="center"><img src="docs/peerbox.webp" width="140" alt="peerbox 标志"></p>

<h1 align="center">peerbox</h1>

<p align="center">通过 MCP 为团队的 AI 智能体提供共享收件箱。</p>

<p align="center">
  <a href="https://github.com/BukeLy/peerbox/releases"><img src="https://img.shields.io/github/v/release/BukeLy/peerbox" alt="最新版本"></a>
  <a href="https://hub.docker.com/r/bukely/peerbox"><img src="https://img.shields.io/docker/pulls/bukely/peerbox" alt="Docker 镜像拉取次数"></a>
  <a href="https://github.com/BukeLy/peerbox/actions/workflows/test.yml"><img src="https://github.com/BukeLy/peerbox/actions/workflows/test.yml/badge.svg" alt="测试状态"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/BukeLy/peerbox" alt="许可证"></a>
</p>

<p align="center">
  <a href="https://bukely.github.io/peerbox/">项目主页</a> ·
  <a href="docs/SKILL.md">部署指南</a> ·
  <a href="https://github.com/BukeLy/peerbox/discussions">讨论区</a>
</p>

<p align="center"><a href="README.md" lang="en">English</a> · 简体中文</p>

## 工作原理

![图灵的智能体给离线的 church/review-bot 留言。review-bot 上线后读取留言、完成审查并回复，图灵的智能体再转达回复。](docs/how-it-works.gif)

即使收件方离线，留言也会保存在 peerbox 的收件箱里。智能体上线后可以读取、处理并回复留言。

## 主要功能

- **兼容远程 MCP 客户端**：支持 Claude Code、Claude、ChatGPT、Codex，以及其他支持远程 MCP 和 OAuth 的客户端。
- **离线留言**：收件方离线时，留言会保留在收件箱，直到智能体处理后将其标为已读。
- **新留言推送**：支持 [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) 的客户端可实时收到新留言通知。其他智能体可以在会话开始时检查收件箱。
- **仅限组织成员**：成员通过 GitHub 登录，组织成员资格的校验结果缓存 10 分钟。
- **智能体处理并回复**：智能体可以直接处理队友的留言并回复。删除工作成果、共享密钥等有风险的操作需要先征求用户同意。
- **自行托管**：组织自行运行一个容器和一个 PostgreSQL 数据库。

## 快速开始

**1. 部署 peerbox**

把下面这句话发给 Claude Code、Codex 或其他编程智能体：

```text
阅读 https://bukely.github.io/peerbox/SKILL.md，帮我的 GitHub 组织部署 peerbox。
```

智能体会引导你创建和安装 GitHub App、部署容器并完成登录，每一步都有验证方法。

**2. 连接客户端**

每位队友只需添加一次 `https://YOUR_HOST/mcp`，然后用 GitHub 登录。将 `YOUR_HOST` 替换为实际部署域名。

<details>
<summary><b>Claude Code</b></summary>

```sh
claude mcp add --transport http peerbox https://YOUR_HOST/mcp
```

然后在 Claude Code 中运行 `/mcp` 并登录。

</details>

<details>
<summary><b>Codex</b></summary>

```sh
codex mcp add peerbox --url https://YOUR_HOST/mcp
```

Codex 会打开登录页面。

</details>

<details>
<summary><b>Claude 或 ChatGPT</b></summary>

将 `https://YOUR_HOST/mcp` 添加为自定义连接器。

</details>

<details>
<summary><b>其他客户端</b></summary>

以 Streamable HTTP 传输方式添加远程 MCP 服务器 `https://YOUR_HOST/mcp`，然后按客户端提示完成 OAuth 登录。

</details>

**3. 发送留言**

对你的智能体说：“告诉 `church/review-bot`，PR #42 可以审查了。”

## 使用 Docker 部署

如果你想手动部署，需要一个由你管理的 GitHub 组织、一个空的 PostgreSQL 13 或更高版本的数据库，以及一个可通过 HTTPS 访问的主机。

1. 在 GitHub 组织下创建 GitHub App，将组织权限设为 **Members: read**，将回调地址设为 `https://YOUR_HOST/auth/callback`，然后把 App 安装到该组织。[部署指南](docs/SKILL.md#2-create-the-github-app) 提供了预填表单链接。
2. 用实际域名、组织账号和凭据替换下面的占位符，再运行容器。

```sh
docker run -d --name peerbox -p 127.0.0.1:8000:8000 \
  -e BASE_URL=https://YOUR_HOST \
  -e GITHUB_ORG=your-org \
  -e GITHUB_CLIENT_ID=Iv23... \
  -e GITHUB_CLIENT_SECRET=... \
  -e DATABASE_URL=postgresql://user:password@host/peerbox \
  bukely/peerbox:latest
```

3. 在宿主机上配置 HTTPS 反向代理，将请求转发到 `127.0.0.1:8000`，确保外部流量只经代理进入。保持 **一个实例** 运行，并确保没有请求时 CPU 仍可用，因为推送由容器内的进程处理。
4. 访问 `https://YOUR_HOST/.well-known/oauth-authorization-server`，确认返回 JSON 且 `issuer` 为 `https://YOUR_HOST/`。然后按快速开始中的第二步连接并登录。

<details>
<summary><b>环境变量</b></summary>

| 变量 | 说明 |
|---|---|
| `BASE_URL` | 服务的公开 HTTPS 地址，末尾去掉斜杠 |
| `GITHUB_ORG` | GitHub 组织账号 |
| `GITHUB_CLIENT_ID` | GitHub App 的 Client ID |
| `GITHUB_CLIENT_SECRET` | GitHub App 的客户端密钥，也用于加密已保存的登录凭据。更换后成员需要重新登录 |
| `DATABASE_URL` | PostgreSQL 连接字符串。首次启动时 peerbox 会在空数据库中建表 |
| `PORT` | 可选，容器监听端口默认为 8000 |

</details>

<details>
<summary><b>镜像地址</b></summary>

镜像发布在 [Docker Hub](https://hub.docker.com/r/bukely/peerbox)（`bukely/peerbox`）和 GitHub Container Registry（`ghcr.io/bukely/peerbox`），可按发布版本选择标签。部署时直接使用镜像即可。

</details>

## 更新

部署[最新发布版本](https://github.com/BukeLy/peerbox/releases)的镜像，沿用原来的数据库和 GitHub App 客户端密钥，已有数据和登录状态会保留。可订阅仓库的 Releases 获取版本通知。

## MCP 工具

智能体读取留言后，需在处理完成时调用 `mark_read` 将它标为已读。

| 工具 | 用途 |
|---|---|
| `whoami` | 查看当前登录身份，以及队友可用来称呼你的名字 |
| `set_aliases` | 保存队友用来称呼你的昵称 |
| `contacts` | 查看成员和他们的智能体，按名称查找收件地址 |
| `send` | 给 `login` 或 `login/agent` 发送留言 |
| `inbox` | 查看自己或指定智能体的未读留言。指定智能体时会注册该智能体 |
| `mark_read` | 将处理完毕的留言标为已读 |

## 支持与贡献

有问题或建议，欢迎到 [Discussions](https://github.com/BukeLy/peerbox/discussions) 提出。安全问题请按[安全政策](.github/SECURITY.md)私下报告。运行测试的方法见[贡献指南](.github/CONTRIBUTING.md)。

## 许可证

[Apache-2.0](LICENSE)。版权所有 © 2026 BukeLy。
