#!/usr/bin/env python3
"""直接验证 Trojan TLS 服务，不启动本地代理，不修改系统设置。"""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import struct
import sys


def target_address(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        domain = host.encode("idna")
        if len(domain) > 255:
            raise ValueError("目标域名过长")
        address = b"\x03" + bytes([len(domain)]) + domain
    else:
        address = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
    return b"\x01" + address + struct.pack("!H", port) + b"\r\n"


def request(credentials, password, host, connect_ip=None, timeout=15):
    context = ssl.create_default_context()
    endpoint = connect_ip or credentials["server"]
    with socket.create_connection((endpoint, credentials["port"]), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=credentials["server"]) as conn:
            conn.settimeout(timeout)
            authentication = hashlib.sha224(password.encode()).hexdigest().encode() + b"\r\n"
            http = f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
            conn.sendall(authentication + target_address(host, 80) + http)
            chunks = []
            try:
                while True:
                    data = conn.recv(8192)
                    if not data:
                        break
                    chunks.append(data)
                    if sum(map(len, chunks)) > 65536:
                        raise ValueError("测试响应超过预期大小")
            except (ssl.SSLError, ConnectionResetError):
                if chunks:
                    raise
            return b"".join(chunks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--expected-ip", type=ipaddress.ip_address, required=True)
    parser.add_argument("--connect-ip", type=ipaddress.ip_address,
                        help="按 IP 连接以绕过本机 DNS，证书仍按域名严格校验")
    args = parser.parse_args()
    try:
        credentials = json.loads(args.credentials.read_text(encoding="utf-8"))
        endpoint = str(args.connect_ip) if args.connect_ip else None
        response = request(credentials, credentials["password"], "api.ipify.org", endpoint)
        if b"200 OK" not in response.split(b"\r\n", 1)[0]:
            raise ValueError("出口查询未返回 HTTP 200")
        # 此 HTTP 测试用于观察出口；TLS 验证的是电脑到代理服务器这一段。
        body = response.split(b"\r\n\r\n", 1)[1]
        expected = str(args.expected_ip).encode()
        if expected not in body.split():
            raise ValueError("返回的出口 IP 与预期不符；未输出完整响应")
        print(f"PASS：TLS 证书有效，代理转发成功，出口 IP 为 {args.expected_ip}")
        wrong = request(credentials, "invalid-test-credential", "api.ipify.org", endpoint)
        if wrong:
            raise ValueError("错误密码测试返回了数据，需要检查认证或 fallback 配置")
        print("PASS：错误密码连接被关闭")
        private = request(credentials, credentials["password"], "127.0.0.1", endpoint)
        if private:
            raise ValueError("回环目标测试返回了数据，需要检查路由限制")
        print("PASS：回环目标测试未返回数据（不能据此证明所有内网域名均被阻止）")
        print("未启动本地监听服务，也未更改系统代理。")
    except (OSError, ValueError, KeyError, IndexError) as error:
        print(f"FAIL：{error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
