#!/usr/bin/env python3
"""Minimal read-only Usenet helpers: read AltMount's providers, STAT articles, parse NZBs.
No side effects on import. Credentials stay in memory and are never logged."""
import os, random, re, socket, ssl, subprocess

CFG = os.environ.get("ALTMOUNT_CONFIG", "/opt/altmount/config.yaml")


def providers():
    body = open(CFG).read().split("providers:", 1)[1]
    out, cur = [], None
    for line in body.splitlines():
        s = line.strip()
        if s.startswith("-"):
            if cur and cur.get("host"):
                out.append(cur)
            cur, s = {}, s.lstrip("- ").strip()
        if cur is None:
            continue
        m = re.match(r'([a-z_]+):\s*"?([^"]*)"?\s*$', s)
        if m:
            cur[m[1]] = m[2]
        if s.startswith("mount_path"):
            break
    if cur and cur.get("host"):
        out.append(cur)
    seen, uniq = set(), []
    for p in out:
        if p.get("host") and p["host"] not in seen and p.get("enabled", "true") != "false":
            seen.add(p["host"])
            uniq.append(p)
    return uniq


class NNTP:
    def __init__(self, p):
        self.host = p["host"]
        self.s = ssl.create_default_context().wrap_socket(
            socket.create_connection((p["host"], int(p.get("port", 563))), timeout=30), server_hostname=p["host"])
        self.f = self.s.makefile("rb")
        self.f.readline()
        self.cmd(f"AUTHINFO USER {p['username']}")
        self.cmd(f"AUTHINFO PASS {p['password']}")

    def cmd(self, c):
        self.s.sendall(c.encode() + b"\r\n")
        return self.f.readline().decode(errors="replace").strip()

    def has(self, mid):
        try:
            return self.cmd(f"STAT <{mid}>").startswith("223")
        except OSError:
            return False

    def close(self):
        try:
            self.cmd("QUIT")
            self.s.close()
        except OSError:
            pass


def read_nzb(path):
    raw = open(path, "rb").read()
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        raw = subprocess.run(["zstd", "-dc", path], capture_output=True).stdout
    elif raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    return raw


def sample(nzb, n=10):
    ids = re.findall(rb"<segment[^>]*>([^<]+)</segment>", read_nzb(nzb))
    ids = [i.decode() for i in ids]
    return random.sample(ids, min(n, len(ids))) if ids else []
