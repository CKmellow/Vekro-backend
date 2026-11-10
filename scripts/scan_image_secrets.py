#!/usr/bin/env python3
"""Fail if a container image carries a secret (#299). Both images are public on GHCR.

    python3 scripts/scan_image_secrets.py IMAGE --path /app [--path ...]
    python3 scripts/scan_image_secrets.py --self-test

The image's filesystem is read with `docker create` + `docker export` (nothing runs). Under each
--path (the files this project adds to the image) it refuses:

- files that hold secrets or test data: .env files (but .env.example), key files (.pem, .key,
  .p12, .pfx, id_rsa, id_ed25519), database dumps and SQLite files, test folders (tests/, e2e/)
  and .git/;
- text that looks like a secret: PEM private keys, 0x-prefixed 32-byte hex (an Ethereum private
  key), eConfirm keys (ek_...), GitHub tokens, and any of our secret settings assigned a value
  (ANCHOR_PRIVATE_KEY=..., DARAJA_CONSUMER_SECRET: "...", PESAPAL_*CONSUMER_SECRET=..., ...).

Across the whole image it refuses PEM private keys outside the usual system locations. Findings
name the file and the rule, never the matched value. Exit 0 clean, 1 findings, 2 usage or docker
error. Standard library only; the web app's repository runs the same file.
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

MAX_BYTES = 25 * 1024 * 1024

SECRET_SETTINGS = (
    r"ANCHOR_PRIVATE_KEY|ANCHOR_ADMIN_REF_SECRET|SECRET_KEY|PAYMENT_CALLBACK_SECRET"
    r"|DARAJA_CONSUMER_KEY|DARAJA_CONSUMER_SECRET|DARAJA_PASSKEY"
    r"|PESAPAL_\w*CONSUMER_KEY|PESAPAL_\w*CONSUMER_SECRET"
    r"|AFRICASTALKING_API_KEY|ECONFIRM_API_KEY|ECONFIRM_API_SECRET|ECONFIRM_CODE_ENCRYPTION_KEY"
    r"|LOOP_\w*SECRET|USSD_CALLBACK_TOKEN|POSTGRES_PASSWORD|CLOUDFLARE_TUNNEL_TOKEN"
    r"|CF_ACCESS_CLIENT_SECRET|DEPLOY_SSH_KEY|VEKRO_ADMIN_PASSWORD"
)

CONTENT_RULES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "pem-private-key",
        re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"),
    ),
    ("hex-private-key", re.compile(rb"(?<![0-9A-Za-z])0x[0-9a-fA-F]{64}(?![0-9A-Za-z])")),
    ("econfirm-key", re.compile(rb"(?<![0-9A-Za-z_])ek_[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{40,})")),
    (
        "secret-setting-with-value",
        # NAME=value, NAME: value, "NAME": "value"; placeholders (<...>, ${...}, $NAME) don't count.
        re.compile(
            rb"\b(?:" + SECRET_SETTINGS.encode() + rb")[\"']?\s*[:=]\s*[\"']?"
            rb"(?![<$]|\{\{)[A-Za-z0-9+/_.~-]{12,}"
        ),
    ),
)

FILE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("env-file", re.compile(r"(?:^|/)\.env(?:\.(?!example$)[^/]*)?$")),
    ("key-file", re.compile(r"(?:\.pem|\.key|\.p12|\.pfx|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519))$")),
    ("database-file", re.compile(r"(?:\.dump|\.sqlite3?|\.db)$")),
    ("test-folder", re.compile(r"(?:^|/)(?:tests?|e2e|__tests__)/")),
    ("git-folder", re.compile(r"(?:^|/)\.git/")),
)

# Distribution CA bundles and the like: never ours, and they hold no private keys anyway.
SYSTEM_PREFIXES = ("usr/", "etc/ssl/", "etc/ca-certificates/", "lib/", "var/lib/")


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str

    def __str__(self) -> str:
        return f"{self.path}: {self.rule}"


def _under(path: str, roots: Iterable[str]) -> bool:
    return any(path == root or path.startswith(root + "/") for root in roots)


def scan_members(members: Iterator[tuple[str, bytes | None]], roots: list[str]) -> list[Finding]:
    """members: (path without a leading slash, content or None for a non-file)."""
    roots = [root.strip("/") for root in roots]
    findings: list[Finding] = []
    for path, data in members:
        ours = _under(path, roots)
        if ours:
            for rule, pattern in FILE_RULES:
                if pattern.search(path):
                    findings.append(Finding("/" + path, rule))
        if data is None:
            continue
        rules = CONTENT_RULES if ours else CONTENT_RULES[:1]
        if not ours and path.startswith(SYSTEM_PREFIXES):
            continue
        for rule, pattern in rules:
            if pattern.search(data):
                findings.append(Finding("/" + path, rule))
    return findings


def _tar_members(stream: io.BufferedIOBase) -> Iterator[tuple[str, bytes | None]]:
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            path = member.name.lstrip("./")
            if not member.isfile() or member.size > MAX_BYTES:
                yield path, None
                continue
            handle = archive.extractfile(member)
            yield path, handle.read() if handle else None


def scan_image(image: str, roots: list[str]) -> list[Finding]:
    container = subprocess.run(
        ["docker", "create", image], check=True, capture_output=True, text=True
    ).stdout.strip()
    try:
        export = subprocess.Popen(["docker", "export", container], stdout=subprocess.PIPE)
        assert export.stdout is not None
        findings = scan_members(_tar_members(export.stdout), roots)
        if export.wait() != 0:
            raise RuntimeError("docker export failed")
        return findings
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


def self_test() -> None:
    """Every rule must fire on a planted secret, and a clean tree must pass."""
    hex_key = "0x" + "ab" * 32
    planted = {
        "app/.env": b"X=1",
        "app/deploy/.env.production": b"X=1",
        "app/certs/server.pem": b"x",
        "app/home/id_ed25519": b"x",
        "app/data/vekro.dump": b"x",
        "app/data/local.sqlite3": b"x",
        "app/tests/test_x.py": b"x",
        "app/e2e/login.spec.ts": b"x",
        "app/.git/config": b"x",
        "app/key.txt": b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n",
        "app/chain.py": b'KEY = "' + hex_key.encode() + b'"',
        "app/econfirm.py": b'api_key = "ek_live_' + b"A1b2C3d4E5f6G7h8" + b'"',
        "app/gh.txt": b"token ghp_" + b"a" * 36,
        "app/settings.env.txt": b"DARAJA_CONSUMER_SECRET=Zx81kQ0pLm2Vn7Rt",
        "app/bundle.js": b'{"PESAPAL_SANDBOX_CONSUMER_SECRET":"qY7wE2rT9uI1oP3a"}',
        "app/compose.yaml": b"POSTGRES_PASSWORD: 8f3e2a1b9c7d6e5f",
        "etc/stray/backup.txt": b"-----BEGIN RSA PRIVATE KEY-----\n",
    }
    expected = {
        "/app/.env": "env-file",
        "/app/deploy/.env.production": "env-file",
        "/app/certs/server.pem": "key-file",
        "/app/home/id_ed25519": "key-file",
        "/app/data/vekro.dump": "database-file",
        "/app/data/local.sqlite3": "database-file",
        "/app/tests/test_x.py": "test-folder",
        "/app/e2e/login.spec.ts": "test-folder",
        "/app/.git/config": "git-folder",
        "/app/key.txt": "pem-private-key",
        "/app/chain.py": "hex-private-key",
        "/app/econfirm.py": "econfirm-key",
        "/app/gh.txt": "github-token",
        "/app/settings.env.txt": "secret-setting-with-value",
        "/app/bundle.js": "secret-setting-with-value",
        "/app/compose.yaml": "secret-setting-with-value",
        "/etc/stray/backup.txt": "pem-private-key",
    }
    found = {(f.path, f.rule) for f in scan_members(iter(planted.items()), ["/app"])}
    missing = {item for item in expected.items() if item not in found}
    assert not missing, f"rules that did not fire: {sorted(missing)}"

    clean = {
        "app/.env.example": b"DARAJA_CONSUMER_SECRET=\nSECRET_KEY=<openssl rand -hex 32>",
        "app/settings.py": b'secret_key: str = ""\nmsg = "Set SECRET_KEY to a long random value"',
        "app/docs.md": b"ANCHOR_PRIVATE_KEY=${ANCHOR_PRIVATE_KEY}\nANCHOR_PRIVATE_KEY=$KEY",
        "app/root.txt": b"root 0x" + b"ab" * 31 + b" short, tx 0x" + b"cd" * 32 + b"ef",
        "app/registry.js": b'registry:"0xD8094faf2B44711dD372A59C288130FA8113E4DB"',
        "usr/lib/python3/test_keys.py": b"-----BEGIN RSA PRIVATE KEY-----",
        "usr/local/lib/x/tests/a.py": b"x",
    }
    stray = scan_members(iter(clean.items()), ["/app"])
    assert not stray, f"false positives: {[str(f) for f in stray]}"
    print(f"self-test passed: {len(expected)} planted secrets caught, {len(clean)} clean files ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("image", nargs="?")
    parser.add_argument("--path", action="append", default=[], help="a folder this project adds")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return 0
    if not args.image or not args.path:
        parser.error("give an image and at least one --path")
    try:
        findings = scan_image(args.image, args.path)
    except (subprocess.CalledProcessError, RuntimeError, tarfile.TarError) as exc:
        print(f"scan failed: {exc}", file=sys.stderr)
        return 2
    if findings:
        print(f"{args.image}: {len(findings)} finding(s); the image must not be published:")
        for finding in sorted(findings, key=str):
            print(f"  {finding}")
        return 1
    print(f"{args.image}: no secrets found under {', '.join(args.path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
