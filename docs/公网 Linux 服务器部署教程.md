# 公网 Linux 服务器部署教程

本文给出单台 Linux 服务器运行舆情 Agent 的 Compose 部署步骤，适合作为首版测试环境。它不是高可用方案，也不代表公网开放前的全部产品与合规准备已完成。

## 先确认公开上线条件

项目面向中国大陆公众访问时，先确认域名、服务器和备案要求。腾讯云当前说明：中国大陆境内服务器开办网站或 APP，需先完成 ICP 备案；腾讯云轻量应用服务器用于备案时，需购买中国大陆节点、包年包月至少 3 个月，且备案期间剩余有效期至少 1 个月。以办理时腾讯云与属地通信管理局的最新要求为准。

公众注册还需要明确并发布隐私说明、服务条款、数据保存和投诉处理方式。当前代码已有邮箱验证码、账号隔离、基础额度限制和自助删除账号/任务数据；但还没有面向公众展示隐私说明和服务条款的页面。因此，当前配置可用于受控测试；相关页面和必要的业务信息补齐前不要向公众开放注册。

上线前需要准备：

1. 已注册并完成实名要求的域名，以及一台可运行 Docker Compose 的 Linux 服务器。
2. 域名的 A 记录指向服务器公网 IPv4；若服务器没有可用公网 IPv6，不要配置 AAAA 记录。
3. 完成需要的 ICP 备案后，再在云安全组和系统防火墙开放 TCP 80、443。SSH 端口限制为自己的来源 IP。
4. 一个可用的 SMTP 发信账号；发件人地址必须由邮件服务授权。
5. 管理员邮箱、模型服务和至少一个搜索服务的 API Key。密钥只写到服务器上的 `.env.production` 或登录后的管理员配置页，不要贴进聊天或提交到 Git。

腾讯云备案入口与云资源条件见[备案快速入门](https://cloud.tencent.com/document/product/243/39038)和[备案云资源要求](https://cloud.tencent.com/document/product/243/18908)。

## 配置服务器

在服务器安装 Docker Engine 和 Docker Compose 插件，然后将仓库代码放到服务器。先不要开放公网访问。进入仓库目录后：

```bash
cp .env.production.example .env.production
chmod 600 .env.production
```

编辑 `.env.production`：

- `YUQING_DOMAIN`：只填域名，例如 `opinion.example.com`。
- `YUQING_PUBLIC_ORIGIN`：填对应的完整 HTTPS 地址，例如 `https://opinion.example.com`。
- `YUQING_ADMIN_EMAILS`：填你本人能收验证码的邮箱；该邮箱首次验证登录后会成为管理员。
- `YUQING_AUTH_SESSION_SECRET`：填至少 32 字节的随机值，可在服务器运行 `openssl rand -hex 32` 生成。不要复用 API Key 或提交到 Git。
- `YUQING_SMTP_*`：按邮件服务商给出的主机、端口、安全方式、账号、密码和发件人填写。优先使用 STARTTLS 或 SSL。
- `DEFAULT_API_KEY`、`DEFAULT_BASE_URL`、`DEFAULT_MODEL` 和搜索 Key：也可在管理员登录后从配置页填写；首次登录和基础页面不依赖模型 Key。

生产 Compose 强制启用认证并关闭演示模式。不要把 `YUQING_AUTH_REQUIRED` 改成 `false`，也不要把应用的 8000 端口发布到公网。

## 检查并启动

完成备案、DNS 和防火墙准备后，在仓库根目录执行：

```bash
docker compose --env-file .env.production -f compose.production.yml config -q
docker compose --env-file .env.production -f compose.production.yml up -d --build
docker compose --env-file .env.production -f compose.production.yml ps
docker compose --env-file .env.production -f compose.production.yml logs --tail=100 app gateway
```

`config -q` 检查 Compose 配置；`up -d --build` 构建并后台启动服务。应用容器只加入 Compose 内部网络，不映射宿主机端口；Caddy 对外提供 80/443 并转发到应用。Caddy 会自动申请和续期 HTTPS 证书，但前提是域名 DNS 指向服务器、外部 80/443 可达，且证书数据卷保持持久。详见 [Caddy 自动 HTTPS 文档](https://caddyserver.com/docs/automatic-https)。

检查 `ps` 中 `app` 为 healthy、`gateway` 为 running，然后访问 `https://你的域名/api/health`。启用账号模式后，健康接口只返回最小状态和版本信息。

首次打开网站时，用 `YUQING_ADMIN_EMAILS` 中的邮箱收验证码并登录。管理员在配置页填写模型和搜索服务配置，再用快速模式完成一项真实任务；验证码邮件、外部模型与搜索服务都需要在实际服务器上分别验收。

## 更新与数据保护

代码更新后，在服务器拉取经审查的版本，再运行：

```bash
docker compose --env-file .env.production -f compose.production.yml up -d --build
```

数据库、报告和证据快照都保存在 `yuqing-data` 命名卷中。`docker compose down` 会停止容器但保留该卷；不要使用 `down -v`，除非明确要删除全部持久数据。上线前设置并演练该卷的定期异机备份与恢复。Caddy 的证书状态也由命名卷保存。

## 当前验收边界

本地已验证的代码与配置不等于云服务器公网验收。正式开放前还要在实际服务器验证：SMTP 邮件投递、HTTPS 证书续期条件、跨账号任务/报告隔离、备份恢复、资源与费用上限、自助删除流程，以及公开使用所需的页面说明和投诉处理流程。
