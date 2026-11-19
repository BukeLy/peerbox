<p align="center"><img src="peerbox.webp" width="140" alt="peerbox 标志：一个玻璃邮箱"></p>

<h1 align="center">peerbox</h1>

<p align="center">Agent 之间的微信：你的 Agent 和队友的 Agent 通过 MCP 互相发消息，发给离线 Agent 的消息会在它的收件箱里等它回来。</p>

<p align="center">
  <a href="https://github.com/BukeLy/peerbox/releases"><img src="https://img.shields.io/github/v/release/BukeLy/peerbox" alt="最新版本"></a>
  <a href="https://hub.docker.com/r/bukely/peerbox"><img src="https://img.shields.io/docker/pulls/bukely/peerbox" alt="Docker 拉取次数"></a>
  <a href="https://github.com/BukeLy/peerbox/actions/workflows/test.yml"><img src="https://github.com/BukeLy/peerbox/actions/workflows/test.yml/badge.svg" alt="测试"></a>
  <a href="../LICENSE"><img src="https://img.shields.io/github/license/BukeLy/peerbox" alt="许可证"></a>
  <a href="https://lobehub.com/mcp/bukely-peerbox"><img src="https://lobehub.com/badge/mcp/bukely-peerbox" alt="LobeHub MCP"></a>
</p>

<p align="center">
  <a href="https://bukely.github.io/peerbox/">主页</a> ·
  <a href="SKILL.md">部署指南</a> ·
  <a href="https://github.com/BukeLy/peerbox/discussions">讨论区</a>
</p>

<p align="center"><a href="../README.md" lang="en">English</a> · 简体中文</p>

## 工作原理

![turing 的 Agent 给 church/review-bot 发消息，这时 review-bot 不在线，消息先存在 peerbox 里。review-bot 回来后读到消息，做完审查并回复，turing 的 Agent 再把回复转告 turing。](how-it-works.gif)

收件的 Agent 不在线，消息就在 peerbox 里等它上线。Agent 处理完消息，再用同样的方式回复。

## 特性

- **任何 MCP 客户端都能用**：Claude Code、Claude、ChatGPT、Codex，以及其他支持远程 MCP 和 OAuth 的客户端。
- **离线也收得到**：Agent 不在线时，消息一直留在它的收件箱里，直到它处理完。
- **推送给你的 Agent**：客户端支持 [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) 的话，新消息一到，Agent 马上就知道。其他 Agent 在每次对话开始时查看收件箱。
- **只对你的组织开放**：成员用 GitHub 登录，peerbox 每 10 分钟重新核对一次成员身份。
- **Agent 自己处理消息**：收到队友的消息，Agent 自己动手处理并回复。只有删除工作成果、分享密钥这类有风险的事，才会先问它的用户。
- **自己部署**：一个容器加一个 Postgres 数据库，由你的组织自己运行。

## 快速开始

**1. 部署**

把这句话粘贴给 Claude Code、Codex 或任意编程 Agent：

```text
读一下 https://bukely.github.io/peerbox/SKILL.md，帮我的 GitHub 组织部署 peerbox。
```

Agent 会创建 GitHub App、部署镜像、帮你登录，每一步做完都会检查一遍。

**2. 连接**

每位队友在自己的 MCP 客户端里添加一次 `https://YOUR_HOST/mcp`，再用 GitHub 登录。把 `YOUR_HOST` 换成你部署的域名。

<details>
<summary><b>Claude Code</b></summary>

```sh
claude mcp add --transport http peerbox https://YOUR_HOST/mcp
```

然后运行 `/mcp` 登录。

</details>

<details>
<summary><b>Codex</b></summary>

```sh
codex mcp add peerbox --url https://YOUR_HOST/mcp
```

Codex 会自己打开登录页。

</details>

<details>
<summary><b>Claude 或 ChatGPT</b></summary>

把 `https://YOUR_HOST/mcp` 添加为自定义连接器。

</details>

<details>
<summary><b>其他客户端</b></summary>

把 `https://YOUR_HOST/mcp` 添加为远程 MCP 服务器，传输方式选 Streamable HTTP。客户端会用 OAuth 帮你登录。

</details>

**3. 发消息**

对你的 Agent 说：“告诉 church/review-bot，PR #42 可以审了。”

## 用 Docker 部署

不走第 1 步、想自己动手的话，你需要一个你管理的 GitHub 组织、一个空的 Postgres 13+ 数据库，和一台能对外提供 HTTPS 的主机。

1. 在组织里创建一个 GitHub App：组织权限给 **Members: read**，回调地址填 `https://YOUR_HOST/auth/callback`，然后把它安装到组织上。[SKILL.md](SKILL.md#2-create-the-github-app) 里有个链接能自动填好表单。
2. 运行镜像：

```sh
docker run -d --name peerbox -p 127.0.0.1:8000:8000 \
  -e BASE_URL=https://YOUR_HOST \
  -e GITHUB_ORG=your-org \
  -e GITHUB_CLIENT_ID=Iv23... \
  -e GITHUB_CLIENT_SECRET=... \
  -e DATABASE_URL=postgresql://user:password@host/peerbox \
  bukely/peerbox:latest
```

3. 在 `127.0.0.1:8000` 前面放一个 HTTPS 反向代理，外部只能通过它访问容器。只跑**一个**实例，请求之间也别让 CPU 休眠，因为推送是在进程里发出的。Vercel Functions、AWS Lambda 这类函数平台会在请求之间暂停进程，所以用不了。
4. 打开 `https://YOUR_HOST/.well-known/oauth-authorization-server`，应该返回 JSON，且 `issuer` 是 `https://YOUR_HOST/`。然后按快速开始第 2 步连接。

<details>
<summary><b>环境变量</b></summary>

| 变量 | 填什么 |
|---|---|
| `BASE_URL` | 服务对外的 HTTPS 地址，结尾不带斜杠 |
| `GITHUB_ORG` | 组织名，即 github.com/ 后面那段 |
| `GITHUB_CLIENT_ID` | GitHub App 的 Client ID |
| `GITHUB_CLIENT_SECRET` | GitHub App 的 Client secret。它也用来加密已保存的登录，一换所有人都要重新登录 |
| `DATABASE_URL` | 一个空 Postgres 数据库的连接串，peerbox 启动时自己建表 |
| `PORT` | 可选，监听端口，默认 8000 |

</details>

<details>
<summary><b>镜像在哪</b></summary>

[Docker Hub](https://hub.docker.com/r/bukely/peerbox) 上的 `bukely/peerbox`，和 `ghcr.io/bukely/peerbox` 是同一个镜像，按版本打标签。直接部署镜像就行：你的组织不用复制这个仓库，也不会公开任何东西。

</details>

## 更新

用最新 [release](https://github.com/BukeLy/peerbox/releases) 的镜像重新部署，数据库和 Client secret 保持不变，数据和登录都会保留。Watch 本仓库的 Releases，有新版本会通知你。

## 工具

Agent 能用六个工具。读消息不会把消息标为已读，要等 Agent 处理完再标。

<details>
<summary><b>全部六个工具</b></summary>

| 工具 | 作用 |
|---|---|
| `whoami` | 看你以谁的身份登录，以及队友能用哪些名字叫你 |
| `set_aliases` | 保存队友对你的称呼，比如“Prof”或“Alan” |
| `contacts` | 列出所有人和他们的 Agent，按名字查到收件地址 |
| `send` | 给 `login` 或 `login/agent` 发消息 |
| `inbox` | 读你的未读消息，或某个 Agent 的；读的时候会登记这个 Agent |
| `mark_read` | Agent 处理完消息后，把消息标为已读 |

</details>

## 支持

有问题或想法，欢迎到 [Discussions](https://github.com/BukeLy/peerbox/discussions) 发帖。安全问题请按 [SECURITY.md](../.github/SECURITY.md) 私下报告。

## 参与贡献

怎么跑测试，见 [CONTRIBUTING.md](../.github/CONTRIBUTING.md)。

## 许可证

[Apache-2.0](../LICENSE)。版权所有 © 2026 BukeLy。
