# 用自己的海外服务器搭建 Clash 节点

一次 Ubuntu + sing-box + Trojan TLS + Clash Verge 的中文实战记录，附可复用脚本与示例配置。

**已有海外服务器，就可以把它作为自己的代理出口，不需要另外开发 APP。** Clash Verge 负责本机开关和选节点；服务器负责接收加密连接、访问目标网站并返回结果。

本仓库公开教程和模板，不提供公共节点、订阅服务或可直接使用的密码。

```text
浏览器 / 遵循系统代理的应用
            │
            ▼
       Clash Verge
            │ Trojan + TLS，TCP 8443
            ▼
     自己的海外服务器
            │
            ▼
         目标网站
```

## 适用范围与验证记录

本次实战使用腾讯云海外服务器、Ubuntu 22.04、Nginx 和 Windows Clash Verge（Mihomo 内核）。服务器原先已经运行网站，80/443 被占用，因此代理使用独立的 TCP 8443 端口。

| 检查项 | 本次实战结果 |
| --- | --- |
| 电脑到代理服务器的 TLS 证书校验 | 通过，没有关闭证书验证 |
| 带正确密码的实际转发 | 通过，出口 IP 与服务器公网 IP 一致 |
| 错误密码 | 连接被关闭，未得到转发响应 |
| 回环目标测试 | 未得到响应；不等于验证了所有内网域名都被阻止 |
| Clash 连接记录 | 显示请求经过自建节点 |
| 浏览器出口查询 | 用户确认返回服务器公网 IP |
| 原有五个网站首页 | 部署后均返回 HTTP 200；没有完整回归全部业务功能 |
| 证书续期 | 已配置 Certbot 定时任务与续期钩子；尚未等待实际自然续期 |

**公开脚本经过参数、协议报文、预览模式等本地测试；实战部署使用的是它的前身版本。公开版本增加了参数化和部署前检查，尚未在第二台全新服务器上完整部署验收。** 请先读脚本、运行预览，再在适合的环境实际部署。

Android 端可以使用支持 Trojan TLS 的客户端，但本次没有进行 Android 验收。

## 这套配置能做什么？

- 启用系统代理后，遵循系统代理设置的应用可经自己的服务器访问网络。
- 可以手动切换代理节点或选择 DIRECT，也可以关闭系统代理。
- 可以修改域名、服务器端口、导出配置的用户。
- 使用独立服务，与原有 Nginx 网站共存；不会覆盖已有站点文件。

这是一套**应用代理**配置。系统代理并不接管所有程序；TUN 可用于接管更多流量，本仓库不自动开启或配置 TUN。

示例使用“规则模式”：局域网地址及代理域名直连，其余匹配 `MATCH` 的流量走所选节点。**它尚未实现国内网站自动直连，也没有实现“先直连、失败后再切换代理”。** 切换为“全局”模式后，规则不再控制流量，需要在 GLOBAL 组明确选择自己的节点。

所有被转发的流量都会消耗服务器带宽和流量额度；实际成本以你的云服务器套餐为准。目标网站也可能对数据中心 IP 有自己的访问限制。

## 仓库结构

```text
README.md                      教程与实际验证边界
scripts/deploy.py              默认预览，--apply 才实际部署
scripts/verify.py              从另一台设备验证 TLS 和实际出口
examples/clash.example.yaml    只有占位符的公开配置
tests/test_scripts.py          参数及协议报文测试
.github/workflows/checks.yml   GitHub Actions 基础检查
```

部署后生成的 `private/`、私人 YAML/JSON、证书和私钥均被 `.gitignore` 排除。

## 部署前准备

需要有 sudo 权限的 Ubuntu 服务器、可访问的公网 IPv4、独立子域名，以及 Windows 上已有的 Clash Verge。

脚本面向**已安装 Nginx、Certbot、UFW，并启用 certbot.timer** 的 Ubuntu 环境，不会自动安装软件、开启 UFW 或修改云平台规则。没有这些依赖时，请先准备环境。全新 Ubuntu 22.04 可参考：

```bash
sudo apt update
sudo apt install nginx certbot ufw python3 ca-certificates
sudo systemctl enable --now nginx certbot.timer
```

已有生产服务器如果使用其他来源的 Certbot 或其他证书定时任务，应先检查现有配置，避免混装。本脚本只支持 Nginx 的 `sites-available` / `sites-enabled` 目录布局。

### 1. 添加域名解析

以下都是占位符，请换成自己的信息：

| 字段 | 值 |
| --- | --- |
| 记录类型 | A |
| 主机记录 | proxy |
| 记录值 | 你的服务器公网 IPv4 |
| 完整域名 | proxy.example.com |

使用 Cloudflare 时，把该记录设置成 **DNS only（灰色云朵）**。本方案需要客户端直接连接服务器，不使用 Cloudflare 普通 CDN 代理承载 Trojan 连接。

检查解析：

```bash
getent ahostsv4 proxy.example.com
```

确认结果是这台服务器的公网 IP。没有配置 IPv6 时，不要给这个子域名额外添加指向其他机器的 AAAA 记录。

### 2. 检查监听端口与云防火墙

```bash
sudo ss -lntup
sudo ufw status
```

8443 应当没有被其他服务占用。在腾讯云 CVM 安全组或轻量应用服务器防火墙中放行 **TCP 8443 入站**。域名验证还需要 TCP 80 从公网可达；已有网站通常已经开放。

如果 UFW 已启用，部署脚本会添加所选代理端口的允许规则。如果 UFW 未启用，脚本不会替你启用，也不会更改 SSH 规则。

Trojan 的 UDP 转发封装在客户端到服务端的 TCP 连接里；本方案不需要为了该协议额外开放 UDP 8443。

### 3. 获取脚本并预览

在服务器上克隆仓库：

```bash
git clone https://github.com/SummerInMarch/self-hosted-proxy-guide.git
cd self-hosted-proxy-guide
python3 scripts/deploy.py --domain proxy.example.com --port 8443 --client-user ubuntu
```

默认只输出计划，不修改文件，不申请证书，不启动服务。`--client-user` 必须是服务器上已有的普通用户，例如 Ubuntu 云镜像里的 `ubuntu`。

阅读 `scripts/deploy.py` 后再继续。脚本使用固定的 sing-box 1.14.3 官方发行版，并校验 GitHub 发行资产提供的 SHA-256 摘要；缺少摘要或校验失败时停止。

### 4. 实际部署

把域名、邮箱和普通用户名替换为自己的值：

```bash
sudo python3 scripts/deploy.py \
  --domain proxy.example.com \
  --port 8443 \
  --client-user ubuntu \
  --email you@example.com \
  --apply
```

`--apply` 会调用 Certbot 的 `--agree-tos`，即同意所用 ACME 服务的条款；邮箱用于证书账户联系。

脚本会新增：

| 内容 | 路径 / 名称 |
| --- | --- |
| 专用低权限用户 | selfhost-proxy |
| 独立二进制 | /usr/local/bin/selfhost-sing-box |
| 服务配置及证书副本 | /etc/selfhost-proxy/ |
| 证书验证用 HTTP 虚拟主机 | /etc/nginx/sites-available/selfhost-proxy-acme |
| HTTP 验证文件目录 | /var/www/selfhost-proxy-acme/ |
| systemd 服务 | selfhost-proxy.service |
| Certbot 续期钩子 | /etc/letsencrypt/renewal-hooks/deploy/selfhost-proxy |
| 私人客户端配置 | 普通用户家目录下的 private/clash.private.yaml |
| 私人验证凭据 | 普通用户家目录下的 private/credentials.private.json |

随机密码不会打印到终端。服务配置和证书私钥只允许 root 与专用服务组读取，客户端导出文件仅允许目标普通用户读取。

Nginx 只新增该域名的 HTTP 虚拟主机，用于证书签发和以后续期；其他路径返回 404。代理本身直接监听 8443，不经 Nginx 转发。

如果同名路径、同名服务用户或该域名的 Nginx 配置已经存在，脚本会拒绝覆盖。**这不是升级或迁移脚本，也不要在正在运行本项目的服务器上重复执行。**

### 5. 检查服务

```bash
systemctl is-active selfhost-proxy
sudo ss -lnt 'sport = :8443'
sudo nginx -t
systemctl is-active certbot.timer
```

服务应为 `active` 且 8443 有监听。脚本会等待端口就绪；启动失败时会禁用并停止本次新增的代理服务，避免无限后台重试。部署成功的代理属于正式服务，会保持运行。

续期钩子仅处理本代理域名的证书，复制新证书后重启本代理服务；不会重启其他应用。

## Windows：导入 Clash Verge

把服务器上的 `~/private/clash.private.yaml` 安全复制到自己的电脑，例如从 Windows PowerShell 执行：

```powershell
scp ubuntu@YOUR_SERVER_IP:~/private/clash.private.yaml .\clash.private.yaml
```

自定义 SSH 端口时，SCP 使用大写 `-P` 参数；使用 SSH 别名也可以。只复制配置文件，不需要把服务器 TLS 私钥下载到客户端。

在 Clash Verge 中：

1. 打开“订阅”，点击“新建”。
2. 类型选择 `Local`，填一个名称。
3. 点击“选择文件”，选中 `clash.private.yaml` 并保存。
4. 点击新配置卡片，启用它。
5. 在“代理”中，确认 `My-Proxy` 组选中了 `My-Server`。
6. 在“首页”开启“系统代理”。

本次实战用 Clash Verge 2.3.2 / Mihomo 1.19.11 完成；其他版本界面可能不同。配置端口为本机 `7897`，如与其他程序冲突，应在客户端同步调整本地端口。

## 确认流量真的经过服务器

### 浏览器验证

打开 [api.ipify.org](https://api.ipify.org/)，结果应是你的服务器公网 IP。

在 Clash 的“连接”页面查找刚访问的域名，代理链应包含 `My-Proxy / My-Server`。仅选中配置卡片不能证明请求已经成功转发。

### 脚本验证

把 `~/private/credentials.private.json` 安全复制到运行验证的电脑。不要发布这个文件。在**另一台设备**执行：

```bash
python scripts/verify.py \
  --credentials private/credentials.private.json \
  --expected-ip YOUR_SERVER_IP \
  --connect-ip YOUR_SERVER_IP
```

`--connect-ip` 可避免本机 DNS 或现有代理对域名解析造成干扰，TLS 证书仍按域名严格验证。

脚本直接建立 Trojan TLS 连接，检查实际出口、错误密码及回环地址访问。它只需 Python 标准库，不启动本地代理服务、不更改系统代理。出口查询使用第三方 HTTP 测试端点；它不是对目标站点 HTTPS 的完整安全审计。

## 开关与换服务器

平时可以保留该配置，需要时开启系统代理，不需要时关闭；也可以把代理组切换为 DIRECT。使用过 TUN 时，还需要按客户端设置关闭 TUN，单关系统代理不代表 TUN 已关闭。

以后迁移服务器，可以保持域名不变，但**修改 DNS 不会自动迁移服务**。新服务器仍需要配置同一协议、端口、认证密码和有效证书。公开脚本会生成新密码，因此直接在新机器运行它后，应重新导入新导出的配置。

## 排查与失败恢复

| 现象 | 检查方向 |
| --- | --- |
| 域名查不到 | DNS A 记录是否保存、是否生效 |
| 本机监听正常但远程超时 | 云安全组、服务器防火墙、IP/端口和本地网络 |
| 证书签发失败 | DNS 是否指向本机、TCP 80 是否开放、域名是否被旧站点配置接管 |
| TLS 校验失败 | 域名/SNI、证书有效期、设备时间；不要直接关闭证书验证 |
| 浏览器仍显示本地出口 | 系统代理是否开启、节点是否选对、是否走 DIRECT |
| 部分软件没有代理 | 软件是否遵循系统代理；再判断是否需要 TUN |
| systemd 启动时报 netlink 错误 | 服务隔离设置是否允许 AF_NETLINK |

服务日志：

```bash
sudo journalctl -u selfhost-proxy --no-pager -n 50
```

部署不是跨所有步骤的原子事务。证书申请失败时会移除本次新增的 Nginx 配置并 reload；其他阶段失败时，证书、用户或配置可能保留。先检查报错与上面的路径表，不要用递归删除命令清理整个 `/etc` 或 `/var/www`，也不要加“强制覆盖”反复重试。

需要暂停本代理服务时：

```bash
sudo systemctl stop selfhost-proxy
```

仅停止本项目的服务；原有网站、数据库及 Nginx 继续运行。需要彻底卸载时，先确认该服务对应端口和域名，按路径表逐项处理。本仓库不提供未经检查的一键卸载脚本。

## 本地检查与公开内容

```bash
python -m unittest discover -s tests -v
python -m compileall -q scripts tests
git diff --check
```

所有公开示例使用 `proxy.example.com`、`YOUR_SERVER_IP` 和密码占位符。上传前检查 Git 暂存区，`.gitignore` 不能保护已经跟踪或强制添加的秘密。

如果分享自己截图，请遮住密码、订阅链接中的令牌、私钥和账号信息。不要公开可用的客户端配置文件。

## 官方参考

- [sing-box Trojan 入站配置](https://sing-box.sagernet.org/configuration/inbound/trojan/)
- [sing-box TLS 配置](https://sing-box.sagernet.org/configuration/shared/tls/)
- [sing-box 路由规则](https://sing-box.sagernet.org/configuration/route/rule/)
- [Mihomo Trojan 节点字段](https://github.com/MetaCubeX/Meta-Docs/blob/main/docs/config/proxies/trojan.en.md)
- [Clash Verge Rev 快速入门](https://www.clashverge.dev/guide/quickstart.html)
- [Certbot 使用文档](https://eff-certbot.readthedocs.io/en/stable/using.html)

本仓库记录学习与自用部署经验。请遵守所在地的适用规定和服务条款。
