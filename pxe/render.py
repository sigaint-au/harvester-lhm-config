#!/usr/bin/env python3
"""Render pxe/ install configs and boot.ipxe from nodes.yaml.

Usage: pxe/render.py [--token TOKEN]

Token precedence: --token > $HARVESTER_TOKEN > .token file > generate+save.
Generated tokens are 48 hex chars, matching the existing token format.
"""
import argparse
import os
import re
import secrets
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODES_YAML = os.path.join(ROOT, "nodes.yaml")
PXE_DIR = os.path.join(ROOT, "pxe")
TOKEN_FILE = os.path.join(ROOT, ".token")


def load_token(cli_token):
    if cli_token:
        return cli_token
    if os.environ.get("HARVESTER_TOKEN"):
        return os.environ["HARVESTER_TOKEN"]
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    token = secrets.token_hex(24)
    with open(TOKEN_FILE, "w") as f:
        f.write(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    print(f"generated new token -> {TOKEN_FILE} (gitignored, rotate post-install)")
    return token


def config_text(node, cluster, keys, token):
    c = cluster
    fqdn = f"harvester-primary.{c['domain']}"
    url = '""' if node["role"] == "create" else f"https://{c['vip']}:443"
    lines = [
        "scheme_version: 1",
        f"server_url: {url}",
        f"token: {token}",
    ]
    if node["role"] == "create":
        lines += ["sans:", f"  - {fqdn}", f"  - {c['vip']}"]
    lines += [
        "os:",
        f"  hostname: {node['hostname']}",
        "  ssh_authorized_keys:",
    ]
    lines += [f"    - {k}" for k in keys]
    lines += ["  dns_nameservers:"]
    lines += [f"    - {d}" for d in c["dns_nameservers"]]
    lines += ["  ntp_servers:"]
    lines += [f"    - {n}" for n in c["ntp_servers"]]
    # Optional host backing (persistent state paths + kernel modules).
    # Install-time only (immutable OS); absent keys render nothing.
    paths = c.get("os_persistent_state_paths") or []
    if paths:
        lines += ["  persistent_state_paths:"]
        lines += [f"    - {p}" for p in paths]
    if c.get("os_modules"):
        lines += ["  modules:"]
        lines += [f"    - {m}" for m in c["os_modules"]]
    lines += [
        "install:",
        f"  mode: {node['role']}",
        "  management_interface:",
        "    interfaces:",
        f"      - name: {c['mgmt_interface']}",
        f"        hwAddr: \"{node['mac']}\"",
        f"    mtu: {c['mgmt_mtu']}",
        "    method: static",
        f"    ip: {node['ip']}",
        f"    subnet_mask: {c['subnet_mask']}",
        f"    gateway: {c['gateway']}",
        f"  device: {c['install_device']}",
        f"  data_disk: {c['data_disk']}",
    ]
    if node["role"] == "create":
        lines += [f"  vip: {c['vip']}", f"  vip_mode: {c['vip_mode']}"]
    lines += [f"  iso_url: {c['iso_url']}"]
    return "\n".join(lines) + "\n"


def boot_text(nodes, cluster):
    base = cluster["http_server"]
    m = re.search(r"v(\d+\.\d+\.\d+)", cluster["iso_url"])
    ver = m.group(1) if m else "UNKNOWN"
    kernel = (
        f"kernel ${{base}}/harvester-{ver}-vmlinuz-amd64 ip=dhcp net.ifnames=1 "
        f"rd.cos.disable rd.noverifyssl console=tty1 "
        f"root=live:${{base}}/harvester-{ver}-rootfs-amd64.squashfs "
        f"harvester.install.automatic=true harvester.install.config_url=${{base}}/"
    )
    initrd = f"initrd ${{base}}/harvester-{ver}-initrd-amd64"

    def short(n):
        return n["hostname"].split("harvester-node-")[-1]

    def cfg(n):
        if n["role"] == "create":
            return "config-create.yaml"
        return f"config-join-{n['hostname']}.yaml"

    out = [
        "#!ipxe",
        f"# Harvester v{ver} automatic install menu — {cluster['domain']}",
        f"# Served as {base}/boot.ipxe",
        "# Auto-selects by client MAC; falls back to the menu below.",
        "# Boot order: CREATE node first, wait for UI, then the JOIN nodes.",
        "",
        f"set base {base}",
        "",
        ":autoselect",
    ]
    for n in nodes:
        out.append(f"iseq ${{mac}} {n['mac'].lower()} && goto node-{short(n)} ||")
    out += [
        "goto start",
        "",
        ":start",
        "menu Harvester automatic install — select node",
        "item --gap -- Nodes (CREATE first, then JOIN):",
    ]
    create = [n for n in nodes if n["role"] == "create"]
    joins = [n for n in nodes if n["role"] != "create"]
    for n in create + joins:
        tag = "CREATE — install first" if n["role"] == "create" else "JOIN"
        out.append(
            f"item node-{short(n)} {n['hostname']} (.{n['ip'].split('.')[-1]} {tag})"
        )
    out += [
        "item --gap -- Other:",
        "item shell iPXE shell",
        "item reboot Reboot",
        "choose --timeout 30000 "
        + (f"--default node-{short(create[0])} " if create else "")
        + "target && goto ${target} || goto start",
        "",
    ]
    for n in create + joins:
        out += [
            f":node-{short(n)}",
            kernel + cfg(n),
            initrd,
            "boot",
            "",
        ]
    out += [":shell", "shell", "goto start", "", ":reboot", "reboot"]
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    args = ap.parse_args()

    with open(NODES_YAML) as f:
        data = yaml.safe_load(f)
    cluster, nodes = data["cluster"], data["nodes"]
    keys = data["ssh_authorized_keys"]
    token = load_token(args.token)

    for n in nodes:
        name = "config-create.yaml" if n["role"] == "create" else (
            f"config-join-{n['hostname']}.yaml"
        )
        path = os.path.join(PXE_DIR, name)
        with open(path, "w") as f:
            f.write(config_text(n, cluster, keys, token))
        print(f"wrote {path}")
    boot = os.path.join(PXE_DIR, "boot.ipxe")
    with open(boot, "w") as f:
        f.write(boot_text(nodes, cluster))
    print(f"wrote {boot}")


if __name__ == "__main__":
    sys.exit(main())
