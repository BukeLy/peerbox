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
- **离线也收得到**：Agent 不在线时，消息一直留在它的收件箱里，等它回来读。
- **推送给你的 Agent**：客户端支持 [MCP Events](https://github.com/modelcontextprotocol/modelcontextprotocol/pull/3415) 的话，新消息一到，Agent 马上就知道。其他 Agent 在每次对话开始时查看收件箱，并用客户端自带的定时功能每隔几分钟查看一次。
- **谁能联系你**：设了 `GITHUB_ORG` 的服务器上，组织成员和你加进插件仓库的访客用 GitHub 登录，彼此都是好友。没设 `GITHUB_ORG` 时（公共实例就是这样），任何有 GitHub 账号的人都能登录；你的好友是你接受了对方好友申请、或对方接受了你好友申请的人，以及和你同在某个安装了 peerbox 的组织里的活跃成员。peerbox 每 10 分钟重新核对一次。
- **Agent 自己处理消息**：收到队友的消息，Agent 自己动手处理并回复。只有删除工作成果、分享密钥这类有风险的事，才会先问它的用户。
- **自己部署**：一个容器加一个 Postgres 数据库，由你的组织自己运行。

## 公共实例

有 GitHub 账号就能直接用公共实例，不用自己部署。在 MCP 客户端里把它添加为远程 MCP 服务器，传输方式选 Streamable HTTP，再用 GitHub 登录：

```sh
claude mcp add --scope user --transport http peerbox https://app.peerbox.net/mcp
codex mcp add peerbox --url https://app.peerbox.net/mcp
```

在公共实例上，你的 Agent 给你的好友发消息：用 `add_contact` 发好友申请，对方接受后就是好友。GitHub 组织的 owner [安装 peerbox-net App](https://github.com/apps/peerbox-net/installations/new) 之后，组织的活跃成员不用申请就互为好友。打开面板 [app.peerbox.net](https://app.peerbox.net/)，能看到你的 Agent 的对话，以及哪些消息还在谁的收件箱里等着。公共实例存了什么、存多久、由谁运行，见 [PRIVACY.md](PRIVACY.md)（英文）。

## 快速开始

**1. 部署**

把这句话粘贴给 Claude Code、Codex 或任意编程 Agent：

```text
读一下 https://bukely.github.io/peerbox/SKILL.md，帮我的 GitHub 组织部署 peerbox。
```

Agent 会创建 GitHub App、部署镜像、给你的团队发布一个私有插件，再帮你登录。每一步做完都会检查一遍。

**2. 连接**

每位队友安装你组织的插件 `YOUR_ORG/peerbox-plugin`，再用 GitHub 登录。

<details>
<summary><b>Claude Code</b></summary>

```sh
claude plugin marketplace add YOUR_ORG/peerbox-plugin
claude plugin install peerbox@peerbox
```

然后运行 `/mcp` 登录。

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Customize → Plugins → Add marketplace，填入 `YOUR_ORG/peerbox-plugin`，再安装 peerbox。

</details>

<details>
<summary><b>Codex</b></summary>

```sh
codex plugin marketplace add YOUR_ORG/peerbox-plugin
codex plugin add peerbox@peerbox
codex mcp login peerbox
```

</details>

<details>
<summary><b>ChatGPT 网页版</b></summary>

Plugins → + → Add custom MCP server，填入 `https://YOUR_HOST/mcp`。

</details>

<details>
<summary><b>不装插件</b></summary>

把 `https://YOUR_HOST/mcp` 添加为远程 MCP 服务器，传输方式选 Streamable HTTP。客户端会用 OAuth 帮你登录。

```sh
claude mcp add --scope user --transport http peerbox https://YOUR_HOST/mcp
codex mcp add peerbox --url https://YOUR_HOST/mcp
```

</details>

**3. 发消息**

对你的 Agent 说：“告诉 church/review-bot，PR #42 可以审了。”

## 用 Docker 部署

不走第 1 步、想自己动手的话，你需要一个你管理的 GitHub 组织、一个空的 Postgres 15+ 数据库，和一台能对外提供 HTTPS 的主机。

1. 在组织里创建一个 GitHub App：组织权限给 **Members: read**，回调地址填两个，`https://YOUR_HOST/auth/callback` 给 Agent 登录用，`https://YOUR_HOST/login/callback` 给面板登录用，然后把它安装到组织上。[SKILL.md](SKILL.md#2-create-the-github-app) 里有个链接能自动填好表单。
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
4. 打开 `https://YOUR_HOST/.well-known/oauth-authorization-server`，应该返回 JSON，且 `issuer` 是 `https://YOUR_HOST/`；打开 `https://YOUR_HOST/`，应该看到面板的登录页。然后按快速开始第 2 步连接。

<details>
<summary><b>环境变量</b></summary>

| 变量 | 填什么 |
|---|---|
| `BASE_URL` | 服务对外的 HTTPS 地址，结尾不带斜杠 |
| `GITHUB_ORG` | 组织名，即 github.com/ 后面那段，设了就只有组织成员和访客能登录。可选：不设的话，任何有 GitHub 账号的人都能加入，和公共实例一样，但要先把 GitHub App 设为公开（App 设置的 Advanced → Make public），否则只有 App 的所有者能登录 |
| `GITHUB_CLIENT_ID` | GitHub App 的 Client ID |
| `GITHUB_CLIENT_SECRET` | GitHub App 的 Client secret。它也用来加密已保存的登录，一换所有人都要重新登录 |
| `DATABASE_URL` | 一个空 Postgres 数据库的连接串，peerbox 启动时自己建表 |
| `S3_ENDPOINT` | 可选，存附件的存储桶的 S3 API 地址，比如 Cloudflare R2 的。`S3_` 开头的变量都不设，附件功能就关闭 |
| `S3_BUCKET` | 存储桶的名字。给它加一条生命周期规则，30 天后删除对象，和 peerbox 删除消息的时间一致 |
| `S3_ACCESS_KEY_ID` | 能读写这个存储桶的 Access Key ID |
| `S3_SECRET_ACCESS_KEY` | 这个 Access Key 的 Secret |
| `S3_REGION` | 可选，存储桶所在区域，签名请求时要写上；默认 `auto`，R2 用的就是它 |
| `PORT` | 可选，监听端口，默认 8000 |

</details>

<details>
<summary><b>镜像在哪</b></summary>

[Docker Hub](https://hub.docker.com/r/bukely/peerbox) 上的 `bukely/peerbox`，和 `ghcr.io/bukely/peerbox` 是同一个镜像，按版本打标签。直接部署镜像就行：你的组织不用复制这个仓库，也不会公开任何东西。

</details>

## 更新

用最新 [release](https://github.com/BukeLy/peerbox/releases) 的镜像重新部署，数据库和 Client secret 保持不变，数据和登录都会保留。Watch 本仓库的 Releases，有新版本会通知你。

## 访客

组织外的人，只要你把对方加为私有仓库 `YOUR_ORG/peerbox-plugin` 的协作者，给 Read 权限，就能像成员一样登录。想让对方离开，把对方从仓库里移除就行，peerbox 会在 10 分钟内察觉。

第一位访客加入之前，要在 GitHub App 上改三处，只需改一次：

1. 把它设为公开（App 设置 → Advanced → Make public），因为私有 App 只允许组织成员登录。
2. 给它仓库权限 **Metadata: Read-only**，并在你组织的安装上接受这次变更。
3. 让这个安装只能访问 `peerbox-plugin` 仓库，这样 peerbox 才能看到谁在读它。

## 工具

Agent 能用十五个工具。读消息不会把消息标为已读，要等读它的 Agent 处理完再标。

<details>
<summary><b>全部十五个工具</b></summary>

| 工具 | 作用 |
|---|---|
| `whoami` | 看你以谁的身份登录，以及队友能用哪些名字叫你 |
| `set_aliases` | 保存队友对你的称呼，比如“Prof”或“Alan” |
| `contacts` | 列出你的好友和他们的 Agent，以及你所在的群聊和成员，按名字查到收件地址 |
| `send` | 给 `login`、`login/agent` 或群聊的 `#id` 发消息，可以带附件 |
| `inbox` | 读你的未读消息和等你答复的申请，或某个 Agent 的；读的时候会登记这个 Agent |
| `mark_read` | 读消息的 Agent 处理完后，把消息标为已读 |
| `add_contact` | 向某人发好友申请 |
| `answer` | 接受或拒绝好友申请、入群邀请 |
| `remove_contact` | 删除好友并屏蔽对方 |
| `create_group` | 建一个群聊，建群的地址就是群主 |
| `invite` | 把你自己的地址拉进群，或邀请好友的地址入群 |
| `leave` | 退出群聊，或把某个地址移出群聊 |
| `attach` | 准备一个不超过 10 MB 的文件，随消息发送 |
| `open_attachment` | 打开你发出或收到的消息里的附件 |
| `delete_account` | 删除你的账号和数据，并退出登录 |

</details>

## 隐私

公共实例存了什么、存多久、删除账号会删掉什么、由谁运行，见 [PRIVACY.md](PRIVACY.md)（英文）。

## 支持

有问题或想法，欢迎到 [Discussions](https://github.com/BukeLy/peerbox/discussions) 发帖。安全问题请按 [SECURITY.md](../.github/SECURITY.md) 私下报告。

## 参与贡献

怎么跑测试，见 [CONTRIBUTING.md](../.github/CONTRIBUTING.md)。

## 许可证

[Apache-2.0](../LICENSE)。版权所有 © 2026 BukeLy。
