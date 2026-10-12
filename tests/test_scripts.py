"""验证危险输入被拒绝、预览不修改系统，以及协议报文格式。"""

import argparse
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = load("deploy")
verify = load("verify")


class InputTests(unittest.TestCase):
    def test_domain_rejects_config_injection(self):
        for value in ("example.com;\nlisten 22", "https://example.com", "a/b.example", "a..com", "127.0.0.1"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                deploy.domain_value(value)

    def test_domain_normalizes_case_and_trailing_dot(self):
        self.assertEqual(deploy.domain_value("Proxy.Example.COM."), "proxy.example.com")

    def test_invalid_ports_rejected(self):
        for port in ("443", "0", "65536", "8443;echo", "-1"):
            with self.subTest(port=port), self.assertRaises(argparse.ArgumentTypeError):
                deploy.port_value(port)

    def test_valid_port(self):
        self.assertEqual(deploy.port_value("8443"), 8443)

    def test_preview_does_not_need_root_or_linux(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "deploy.py"), "--domain", "proxy.example.com"],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("这次没有修改文件", result.stdout)

    def test_apply_requires_email_before_system_changes(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "deploy.py"), "--domain", "proxy.example.com", "--apply"],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--email", result.stderr)


class WireTests(unittest.TestCase):
    def test_domain_connect_request(self):
        self.assertEqual(verify.target_address("example.com", 443),
                         b"\x01\x03\x0bexample.com" + struct.pack("!H", 443) + b"\r\n")

    def test_ipv4_connect_request(self):
        self.assertEqual(verify.target_address("127.0.0.1", 80), b"\x01\x01\x7f\x00\x00\x01\x00\x50\r\n")

    def test_ipv6_connect_request(self):
        wire = verify.target_address("::1", 80)
        self.assertEqual(wire[:2], b"\x01\x04")
        self.assertEqual(len(wire), 22)

    def test_hostname_length_rejected(self):
        with self.assertRaises((ValueError, UnicodeError)):
            verify.target_address("a" * 256, 443)


if __name__ == "__main__":
    unittest.main()
