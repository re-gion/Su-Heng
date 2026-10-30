# 公网 Linux 服务器部署教程

本文给出单台 Linux 服务器运行舆情 Agent 的 Compose 部署步骤，使用本地构建的应用镜像，不要求把整个代码仓库放到服务器。它适合作为首版测试环境，不是高可用方案，也不代表公网开放前的全部产品与合规准备已完成。

## 先确认公开上线条件

若服务器选择新加坡、曼谷等中国大陆以外地域，腾讯云当前说明可直接解析域名，通常无需为该境外实例办理大陆 ICP 备案；若之后迁到中国大陆地域，则需先完成备案。境外部署不代表自动免除其他适用要求：个人信息保护法也适用于以向境内自然人提供产品或服务为目的、在境外处理其个人信息的情形。项目面向中国大陆用户处理邮箱、账号和研究任务数据，公开开放前仍需完成隐私说明、服务条款、数据保存和投诉处理等准备。以办理时腾讯云与主管部门的最新要求为准。

公众注册还需要明确并发布隐私说明、服务条款、数据保存和投诉处理方式。当前代码已有邮箱验证码、账号隔离、基础额度限制和自助删除账号/任务数据；但还没有面向公众展示隐私说明和服务条款的页面。因此，当前配置可用于受控测试；相关页面和必要的业务信息补齐前不要向公众开放注册。

上线前需要准备：

1. 已注册并完成实名要求的域名，以及一台可运行 Docker Compose 的 Linux 服务器。
2. 域名的 A 记录指向服务器公网 IPv4；若服务器没有可用公网 IPv6，不要配置 AAAA 记录。
3. 按服务器地域处理备案要求（境外地域通常无需大陆 ICP 备案），再在云安全组和系统防火墙开放 TCP 80、443。SSH 端口限制为自己的来源 IP。
4. 一个可用的 SMTP 发信账号；发件人地址必须由邮件服务授权。
5. 管理员邮箱、模型服务和至少一个搜索服务的 API Key。密钥只写到服务器上的 `.env.production` 或登录后的管理员配置页，不要贴进聊天或提交到 Git。

腾讯云关于境外实例和备案的说明见[接入备案常见问题](https://cloud.tencent.com/document/product/243/43878)；个人信息保护法的适用范围见[全国人大常委会公报](https://wb.flk.npc.gov.cn/flfg/PDF/f67af9f12e1b4c83a998cf5a876ce0e4.pdf)。

按你截图中的套餐，35 元/月只有 1GB 内存，40 元/月是 2GB。考虑到应用还包含 Python 后端和 Chromium，1GB 不建议作为这套系统的起步规格；预算不超过 50 元时，2GB 更适合作为受控试运行。当前默认额度是每账号每天 3 个任务、同时运行 1 个，但这是单账号限制，不是整台服务器的总并发上限；在开放公众注册前，还要增加或确认服务器级任务总量保护，并观察实际内存使用。否则多个账号仍可能同时把小规格服务器压满。

## 配置服务器

购买页的四种创建方式都是“服务器装机模板”，不是把你本机已有的应用镜像自动放到服务器：

- **应用镜像**：操作系统加预装好的常见软件，例如 WordPress；不是本项目的镜像。
- **操作系统镜像**：干净的 Ubuntu 等系统；本教程推荐它，再在服务器上安装 Docker Compose。
- **容器镜像**：预装 Docker 的系统，并可在腾讯云控制台配置启动一个容器，类似 `docker run`；控制台选镜像需要先把镜像放到腾讯云 TCR 个人版的公开仓库。当前项目需要 Compose 一起管理 app、Caddy、网络和持久卷，因此不走这个单容器向导。
- **自定义镜像**：从一台已经配置好的腾讯云轻量服务器制作整机模板，便于复制服务器；它不是 `docker save` 导出的 Docker 应用镜像。

上述定义详见[轻量应用服务器镜像说明](https://cloud.tencent.com/document/product/1207/79254)和[创建及管理 Docker 容器](https://cloud.tencent.com/document/product/1207/60329)。

创建服务器时建议选择“基于操作系统镜像”，使用 Ubuntu Linux。若购买页有 Ubuntu 24.04 LTS，可优先选它；Ubuntu 26.04 LTS 也在 Docker 官方支持范围内。选购时确认 CPU 架构为常见的 x86_64/amd64，方便直接使用 Windows Docker Desktop 构建的 Linux 镜像。首次进入 Ubuntu 后安装 Docker Engine 和 Compose 插件，按[Docker 官方 Ubuntu 安装文档](https://docs.docker.com/engine/install/ubuntu/)操作。Ubuntu 默认登录用户名通常是 `ubuntu`；普通用户执行 Docker 命令时加 `sudo`。

“登录方式”控制的是你如何远程登录这台 Linux 服务器，不是网站用户怎么登录舆情 Agent。**自动生成密码**由腾讯云生成并通过站内信发送；**自定义密码**由你设置；**SSH 密钥登录**把公钥配置到服务器，私钥留在自己的电脑上，建议优先选它；**扫码登录**用微信扫码进入服务器，适合临时手动登录。控制台里的“登录”按钮则是腾讯云网页终端入口。Ubuntu 默认用户名通常为 `ubuntu`，而且默认不允许用 root 密码直接 SSH 登录；需要管理员操作时用 `sudo`。登录方式说明见[腾讯云快速创建 Linux 实例](https://cloud.tencent.com/document/product/1207/44548)。

先在本机 Windows 工作区构建新镜像。确保代码已更新到准备部署的版本后运行：

```powershell
docker compose -f compose.demo.yml build app
docker pull caddy:2.10-alpine
docker image inspect opc-agent-app:latest --format '{{.Os}}/{{.Architecture}}'
docker tag opc-agent-app:latest opc-agent-app:production
$archive = Join-Path $env:TEMP 'opc-agent-app.tar'
docker image save --output $archive opc-agent-app:production caddy:2.10-alpine
Get-Item $archive | Select-Object FullName, Length
```

检查平台与服务器架构匹配：`linux/amd64` 对应 Linux 的 `x86_64`；若购买的是 ARM 服务器，需要重新按 `linux/arm64` 构建。归档内有两个镜像：包含后端、前端和运行依赖的 app，以及负责 HTTPS 和反向代理的 Caddy。数据库、报告、证据快照、密钥不在镜像内。

首次部署还要把 Compose 文件和 Caddy 配置放到服务器。先登录服务器建立目录，再回到本机 PowerShell 传文件。以下用 Ubuntu 默认用户 `ubuntu` 举例，把 IP、密钥路径换成自己的：

```powershell
$server = 'ubuntu@YOUR_SERVER_IP'
$key = 'PATH_TO_YOUR_PRIVATE_KEY'
$remoteDir = '/home/ubuntu/opc-agent'
ssh -i $key $server "mkdir -p $remoteDir/deploy"
scp -i $key .\compose.production.yml .\.env.production.example "${server}:$remoteDir/"
scp -i $key .\deploy\Caddyfile.production "${server}:$remoteDir/deploy/"
scp -i $key $archive "${server}:/tmp/opc-agent-app.tar"
```

服务器收到归档后，SSH 登录并运行：

```bash
sudo docker load -i /tmp/opc-agent-app.tar
cd ~/opc-agent
cp .env.production.example .env.production
chmod 600 .env.production
nano .env.production
```

镜像会带着 `opc-agent-app:production` 标签载入。保存 `nano` 修改时按 `Ctrl+O`、回车，再按 `Ctrl+X` 退出。服务器上的 `.env.production` 只填写真实配置，不要从本机复制含密钥的 `.env` 文件。

以上是“镜像归档 + SCP”方式：不需要镜像仓库，适合先部署或偶尔更新。`docker image save` 将镜像写入 tar 文件，服务器用 `docker load` 载回本机镜像列表；SCP 通过 SSH 加密传输。归档保存在本机临时目录，部署完成后可按需自行删除。

如果希望以后从服务器直接拉取新版本，可以改用 Docker Hub 或腾讯云 TCR。镜像仓库是可选的，不是必需条件。

如果选择 Docker Hub，先在本机登录并推送一个固定版本标签：

```powershell
docker login
docker tag opc-agent-app:production YOUR_DOCKERHUB_NAME/yuqing-agent:0.3.0
docker push YOUR_DOCKERHUB_NAME/yuqing-agent:0.3.0
```

然后在服务器登录镜像仓库（私有仓库需要登录），并在 `.env.production` 中将 `YUQING_APP_IMAGE` 改为相同地址，再执行：

```bash
sudo docker login
sudo docker compose --env-file .env.production -f compose.production.yml pull app
sudo docker compose --env-file .env.production -f compose.production.yml up -d
```

公共仓库中的镜像任何人都能拉取；私有仓库需要先在服务器执行 `docker login`。生产部署用明确版本标签，方便知道服务器运行的是哪次构建。公开镜像意味着任何人都可能下载镜像内容；不要把密钥放进镜像层。

服务器还需要 Docker Engine 与 Docker Compose 插件。镜像归档方式要上传的部署文件只有 `compose.production.yml`、`.env.production.example` 和 `deploy/Caddyfile.production`，不必上传其余源码。若尚未配置 DNS 和防火墙，先不要开放公网访问。

```bash
cd ~/opc-agent
```

编辑 `.env.production`：

- `YUQING_DOMAIN`：只填域名，例如 `opinion.example.com`。
- `YUQING_PUBLIC_ORIGIN`：填对应的完整 HTTPS 地址，例如 `https://opinion.example.com`。
- `YUQING_ADMIN_EMAILS`：填你本人能收验证码的邮箱；该邮箱首次验证登录后会成为管理员。
- `YUQING_AUTH_SESSION_SECRET`：填至少 32 字节的随机值，可在服务器运行 `openssl rand -hex 32` 生成。不要复用 API Key 或提交到 Git。
- `YUQING_SMTP_*`：按邮件服务商给出的主机、端口、安全方式、账号、密码和发件人填写。优先使用 STARTTLS 或 SSL。
- `YUQING_APP_IMAGE`：用 SCP/`docker load` 时保留 `opc-agent-app:production`；用镜像仓库时改成仓库地址与固定版本标签，例如 `docker.io/你的账号/yuqing-agent:0.3.0`。
- `DEFAULT_API_KEY`、`DEFAULT_BASE_URL`、`DEFAULT_MODEL` 和搜索 Key：也可在管理员登录后从配置页填写；首次登录和基础页面不依赖模型 Key。

生产 Compose 强制启用认证并关闭演示模式。不要把 `YUQING_AUTH_REQUIRED` 改成 `false`，也不要把应用的 8000 端口发布到公网。

## 检查并启动

完成适用的备案准备、DNS 和防火墙设置后，在服务器的 `~/opc-agent` 部署目录执行：

```bash
sudo docker compose --env-file .env.production -f compose.production.yml config -q
sudo docker compose --env-file .env.production -f compose.production.yml up -d
sudo docker compose --env-file .env.production -f compose.production.yml ps
sudo docker compose --env-file .env.production -f compose.production.yml logs --tail=100 app gateway
```

`config -q` 检查 Compose 配置；`up -d` 使用已载入的镜像或从仓库拉取镜像，并后台启动服务。生产 Compose 不在服务器上构建 app，也不需要源码目录。应用容器只加入 Compose 内部网络，不映射宿主机端口；Caddy 对外提供 80/443 并转发到应用。Caddy 会自动申请和续期 HTTPS 证书，但前提是域名 DNS 指向服务器、外部 80/443 可达，且证书数据卷保持持久。详见 [Caddy 自动 HTTPS 文档](https://caddyserver.com/docs/automatic-https)。

检查 `ps` 中 `app` 为 healthy、`gateway` 为 running，然后访问 `https://你的域名/api/health`。启用账号模式后，健康接口只返回最小状态和版本信息。

首次打开网站时，用 `YUQING_ADMIN_EMAILS` 中的邮箱收验证码并登录。管理员在配置页填写模型和搜索服务配置，再用快速模式完成一项真实任务；验证码邮件、外部模型与搜索服务都需要在实际服务器上分别验收。

## 更新与数据保护

代码更新后，在本机从经审查的版本重新构建镜像，再用 `docker save`/SCP/`docker load` 传入服务器；或者推送新版本到镜像仓库、更新 `YUQING_APP_IMAGE` 并先拉取新镜像。部署目录不需要完整源代码。准备好新镜像后运行：

```bash
sudo docker compose --env-file .env.production -f compose.production.yml up -d
```

数据库、报告和证据快照都保存在 `yuqing-data` 命名卷中。`docker compose down` 会停止容器但保留该卷；不要使用 `down -v`，除非明确要删除全部持久数据。上线前设置并演练该卷的定期异机备份与恢复。Caddy 的证书状态也由命名卷保存。

## 当前验收边界

本地已验证的代码与配置不等于云服务器公网验收。正式开放前还要在实际服务器验证：SMTP 邮件投递、HTTPS 证书续期条件、跨账号任务/报告隔离、备份恢复、资源与费用上限、自助删除流程，以及公开使用所需的页面说明和投诉处理流程。
