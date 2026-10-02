#!/usr/bin/env python3
"""Tag Longhorn disks into tiers from nodes.yaml.

Reads `storage.longhorn_tiers` (tag -> [/dev/...]) and `cluster.data_disk`.
Longhorn disks are keyed by BlockDevice name (extra disks) or
`default-disk-*` (the Harvester data disk), so devices are resolved via
the BlockDevice CRs, not by path. Tier StorageClasses (`longhorn-ssd`,
`longhorn-hdd`) select disks via `diskSelector`, so volumes land on the
right media. Idempotent; unknown disks are reported and skipped, never
guessed.

Usage:
  python3 workloads/storage/tag-longhorn-disks.py [--dry-run]
"""
import argparse
import json
import os
import subprocess
import sys

import yaml

ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
NODES_YAML = os.path.join(ROOT, "nodes.yaml")
NAMESPACE = "longhorn-system"


def kubectl(*args):
    p = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, check=False
    )
    if p.returncode != 0:
        print(f"kubectl {' '.join(args)} failed:\n{p.stderr}", file=sys.stderr)
        sys.exit(1)
    return p.stdout


def load_layout():
    with open(NODES_YAML) as f:
        data = yaml.safe_load(f)
    tiers = data["storage"]["longhorn_tiers"]  # tag -> [devices]
    device_tag = {dev: tag for tag, devs in tiers.items() for dev in devs}
    data_disk = data["cluster"]["data_disk"]
    return device_tag, data_disk, {n["hostname"] for n in data["nodes"]}


def plan_disks(lh_nodes, blockdevices, device_tag, data_disk, hostnames):
    """Return {node: {disk_key: tag}} patches needed. Pure logic, tested."""
    bd_dev = {}  # (node, bd-name) -> /dev/...
    for item in blockdevices.get("items", []):
        s = item.get("spec", {})
        bd_dev[(s.get("nodeName"), item["metadata"]["name"])] = s.get("devPath")
    plans = {}
    for item in lh_nodes.get("items", []):
        name = item["metadata"]["name"]
        if name not in hostnames:
            continue
        patch = {}
        for disk_key, disk in item.get("spec", {}).get("disks", {}).items():
            if disk_key.startswith("default-disk"):
                dev = data_disk
            else:
                dev = bd_dev.get((name, disk_key))
            want = device_tag.get(dev) if dev else None
            if want is None:
                print(f"{name}: disk {disk_key} (device {dev}) not in "
                      f"nodes.yaml tiers, skipping")
                continue
            if disk.get("tags", []) != [want]:
                patch[disk_key] = {"tags": [want]}
        if patch:
            plans[name] = patch
        else:
            print(f"{name}: tags already correct")
    return plans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    device_tag, data_disk, hostnames = load_layout()
    lh_nodes = json.loads(kubectl("get", "nodes.longhorn.io",
                                  "-n", NAMESPACE, "-o", "json"))
    blockdevices = json.loads(kubectl("get", "blockdevice",
                                      "-n", NAMESPACE, "-o", "json"))
    plans = plan_disks(lh_nodes, blockdevices, device_tag,
                       data_disk, hostnames)
    for name, patch in plans.items():
        summary = ", ".join(f"{d}->[{patch[d]['tags'][0]}]" for d in patch)
        if args.dry_run:
            print(f"{name}: would tag {summary}")
            continue
        kubectl("patch", "nodes.longhorn.io", name, "-n", NAMESPACE,
                "--type=merge",
                "-p", json.dumps({"spec": {"disks": patch}}))
        print(f"{name}: tagged {summary}")


if __name__ == "__main__":
    sys.exit(main())
