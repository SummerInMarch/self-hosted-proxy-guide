#!/usr/bin/env python3
"""Ubuntu + Nginx 已有环境的自用代理部署；默认只显示计划。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

VERSION = "1.14.3"
SERVICE = "selfhost-proxy"
BASE = Path("/etc/selfhost-proxy")
BIN = Path("/usr/local/bin/selfhost-sing-box")
NGINX = Path("/etc/nginx/sites-available/selfhost-proxy-acme")
LINK = Path("/etc/nginx/sites-enabled/selfhost-proxy-acme")
WEBROOT = Path("/var/www/selfhost-proxy-acme")
UNIT = Path("/etc/systemd/system/selfhost-proxy.service")
HOOK = Path("/etc/letsencrypt/renewal-hooks/deploy/selfhost-proxy")


def domain_value(value):
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise argparse.ArgumentTypeError("请填写有效域名，不要包含协议、路径或端口")
    try:
        socket.inet_aton(value)
    except OSError:
        return value
    raise argparse.ArgumentTypeError("请使用域名而不是 IPv4 地址")


def port_value(value):
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("端口必须是整数") from error
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("请使用 1024 到 65535 的独立端口")
    return port


def run(*args, capture=False):
    return subprocess.run(args, check=True, text=True, capture_output=capture)


def write(path, content, mode=0o644):
    # 先限制权限，再写入内容，避免凭据短暂暴露。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def client_yaml(domain, port, password):
    return f'''# 包含私人代理密码；不要上传到公共仓库。
mixed-port: 7897
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
ipv6: false
proxies:
  - name: My-Server
    type: trojan
    server: {domain}
    port: {port}
    password: "{password}"
    sni: {domain}
    skip-cert-verify: false
    udp: true
proxy-groups:
  - name: My-Proxy
    type: select
    proxies: [My-Server, DIRECT]
rules:
  - DOMAIN,{domain},DIRECT
  - IP-CIDR,127.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,10.0.0.0/8,DIRECT,no-resolve
  - IP-CIDR,172.16.0.0/12,DIRECT,no-resolve
  - IP-CIDR,192.168.0.0/16,DIRECT,no-resolve
  - MATCH,My-Proxy
'''


def server_config(domain, port, password):
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "trojan", "tag": "private-in", "listen": "0.0.0.0",
            "listen_port": port,
            "users": [{"name": "owner", "password": password}],
            "tls": {"enabled": True, "server_name": domain,
                    "certificate_path": str(BASE / "fullchain.pem"),
                    "key_path": str(BASE / "privkey.pem")},
        }],
        "outbounds": [{"type": "direct", "tag": "internet"}],
        "route": {"rules": [{"ip_is_private": True, "action": "reject"}],
                  "final": "internet"},
    }


def unit_text():
    return f'''[Unit]
Description=Self-hosted private Trojan TLS proxy
After=network-online.target
Wants=network-online.target

[Service]
User={SERVICE}
Group={SERVICE}
ExecStart={BIN} run -c {BASE}/config.json
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# sing-box 查询网络接口需要 AF_NETLINK。
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
CapabilityBoundingSet=
UMask=0077
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
'''


def hook_text(domain):
    return f'''#!/bin/sh
set -eu
# 仅在本代理证书续期后复制证书并重启本服务。
[ "${{RENEWED_LINEAGE:-}}" = "/etc/letsencrypt/live/{domain}" ] || exit 0
install -o root -g {SERVICE} -m 640 "$RENEWED_LINEAGE/fullchain.pem" {BASE}/fullchain.pem
install -o root -g {SERVICE} -m 640 "$RENEWED_LINEAGE/privkey.pem" {BASE}/privkey.pem
systemctl try-restart {SERVICE}.service
'''


def download_binary(temp):
    arch = os.uname().machine
    architectures = {"x86_64": "amd64", "aarch64": "arm64"}
    if arch not in architectures:
        raise RuntimeError(f"不支持的架构：{arch}")
    stem = f"sing-box-{VERSION}-linux-{architectures[arch]}"
    request = urllib.request.Request(
        f"https://api.github.com/repos/SagerNet/sing-box/releases/tags/v{VERSION}",
        headers={"User-Agent": "self-hosted-proxy-guide"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next(a for a in release["assets"] if a["name"] == stem + ".tar.gz")
    expected = asset.get("digest", "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise RuntimeError("上游未提供 SHA-256 摘要，停止安装")
    archive = temp / asset["name"]
    with urllib.request.urlopen(asset["browser_download_url"], timeout=90) as response:
        archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected.split(":", 1)[1]:
        raise RuntimeError("官方二进制摘要校验失败")
    binary = temp / "sing-box"
    # 只读取已知文件，不解压整个压缩包。
    with tarfile.open(archive) as bundle:
        member = bundle.getmember(stem + "/sing-box")
        if not member.isfile():
            raise RuntimeError("压缩包中的二进制不是普通文件")
        with bundle.extractfile(member) as source, binary.open("wb") as target:
            shutil.copyfileobj(source, target)
    binary.chmod(0o755)
    return binary


def preflight(args):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("实际部署必须在 Linux 上用 sudo 运行")
    import pwd
    user = pwd.getpwnam(args.client_user)
    if user.pw_uid == 0:
        raise RuntimeError("client-user 必须是普通用户")
    home = Path(user.pw_dir)
    export_dir = home / "private"
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError("用户家目录不存在或为符号链接")
    for path in (BASE, BIN, NGINX, LINK, WEBROOT, UNIT, HOOK, export_dir):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"拒绝覆盖已有路径：{path}")
    try:
        pwd.getpwnam(SERVICE)
    except KeyError:
        pass
    else:
        raise RuntimeError("专用服务用户已存在，停止以避免覆盖旧部署")
    for command in ("nginx", "certbot", "systemctl", "ufw", "useradd", "runuser", "install", "ss"):
        if not shutil.which(command):
            raise RuntimeError(f"缺少依赖：{command}，请先按 README 准备环境")
    os_release = Path("/etc/os-release").read_text()
    if 'ID=ubuntu' not in os_release:
        raise RuntimeError("脚本面向 Ubuntu；其他发行版请手动适配")
    run("nginx", "-t")
    run("systemctl", "is-active", "nginx")
    run("systemctl", "is-active", "certbot.timer")
    nginx_text = run("nginx", "-T", capture=True).stdout
    for directive in re.findall(r"\bserver_name\s+([^;]+);", nginx_text):
        if args.domain in directive.split():
            raise RuntimeError("域名已有 Nginx 配置，停止以避免冲突")
    with socket.socket() as listener:
        listener.bind(("0.0.0.0", args.port))
    if not socket.getaddrinfo(args.domain, None, socket.AF_INET):
        raise RuntimeError("域名没有可用的 IPv4 解析")
    # 解析是否指向本机仍由部署者核对，不能根据内网网卡推断公网 IP。
    return user, export_dir


def apply(args):
    user, export_dir = preflight(args)
    with tempfile.TemporaryDirectory(prefix="selfhost-proxy-") as temp:
        binary = download_binary(Path(temp))
        print("官方二进制摘要校验通过", flush=True)
        WEBROOT.mkdir(mode=0o755)
        write(NGINX, f'''server {{
    listen 80;
    server_name {args.domain};
    location ^~ /.well-known/acme-challenge/ {{
        root {WEBROOT};
        default_type text/plain;
        try_files $uri =404;
    }}
    location / {{ return 404; }}
}}
''')
        LINK.symlink_to(NGINX)
        try:
            run("nginx", "-t")
            run("systemctl", "reload", "nginx")
            run("certbot", "certonly", "--webroot", "-w", str(WEBROOT),
                "-d", args.domain, "--cert-name", args.domain,
                "--non-interactive", "--agree-tos", "--email", args.email)
        except Exception:
            # 只撤销刚创建的虚拟主机，不修改已有站点。
            LINK.unlink(missing_ok=True)
            NGINX.unlink(missing_ok=True)
            run("nginx", "-t")
            run("systemctl", "reload", "nginx")
            shutil.rmtree(WEBROOT)
            raise
        run("install", "-o", "root", "-g", "root", "-m", "755", str(binary), str(BIN))

    run("useradd", "--system", "--no-create-home", "--shell", "/usr/sbin/nologin", SERVICE)
    BASE.mkdir(mode=0o750)
    run("chown", f"root:{SERVICE}", str(BASE))
    for filename in ("fullchain.pem", "privkey.pem"):
        run("install", "-o", "root", "-g", SERVICE, "-m", "640",
            f"/etc/letsencrypt/live/{args.domain}/{filename}", str(BASE / filename))
    password = secrets.token_urlsafe(36)
    write(BASE / "config.json", json.dumps(server_config(args.domain, args.port, password), indent=2) + "\n", 0o640)
    run("chown", f"root:{SERVICE}", str(BASE / "config.json"))
    run("runuser", "-u", SERVICE, "--", str(BIN), "check", "-c", str(BASE / "config.json"))
    write(UNIT, unit_text())
    write(HOOK, hook_text(args.domain), 0o750)
    run("sh", "-n", str(HOOK))
    run("ufw", "allow", f"{args.port}/tcp", "comment", "Self-hosted private proxy")
    # 不自动启用 UFW，也不更改 SSH 或云平台规则。
    run("systemctl", "daemon-reload")
    run("systemctl", "enable", "--now", SERVICE)
    try:
        for _ in range(20):
            try:
                with socket.create_connection(("127.0.0.1", args.port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("服务未开始监听；查看 journalctl -u selfhost-proxy")
        run("systemctl", "is-active", SERVICE)
    except Exception:
        # 验证失败时停止本次新增的服务，避免无限后台重试。
        run("systemctl", "disable", "--now", SERVICE)
        raise

    export_dir.mkdir(mode=0o700)
    os.chown(export_dir, user.pw_uid, user.pw_gid)
    write(export_dir / "clash.private.yaml", client_yaml(args.domain, args.port, password), 0o600)
    credentials = {"server": args.domain, "port": args.port, "password": password}
    write(export_dir / "credentials.private.json", json.dumps(credentials, indent=2) + "\n", 0o600)
    for path in export_dir.iterdir():
        os.chown(path, user.pw_uid, user.pw_gid)
    print(f"服务已监听；私人配置：{export_dir}/clash.private.yaml")
    print("请从另一台设备执行验证；本机监听成功不代表云防火墙已放行。")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--domain", required=True, type=domain_value)
    result.add_argument("--port", type=port_value, default=8443)
    result.add_argument("--client-user", default="ubuntu", help="接收私人配置的已有普通用户")
    result.add_argument("--email", help="申请证书的联系邮箱；实际部署必须提供")
    result.add_argument("--apply", action="store_true", help="实际修改服务器；省略时只输出计划")
    return result


def main():
    command = parser()
    args = command.parse_args()
    if not args.apply:
        print(f"计划：Trojan + TLS，{args.domain}:{args.port}，sing-box {VERSION}")
        print("新增独立 Nginx HTTP 虚拟主机、证书、专用服务用户、systemd 服务和 UFW 端口规则。")
        print(f"向 {args.client_user} 导出私人配置；已有同名路径时拒绝覆盖。")
        print("这次没有修改文件、启动服务或申请证书；添加 --apply 才会执行。")
        return
    if not args.email or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", args.email):
        command.error("实际部署需要 --email 有效邮箱")
    try:
        apply(args)
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.CalledProcessError) as error:
        print(f"部署停止：{error}\n已完成的操作可能保留，请按 README 的失败恢复说明检查；不要盲目重复运行。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
