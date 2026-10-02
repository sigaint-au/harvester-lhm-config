#!/usr/bin/env python3
"""Fill per-image Longhorn StorageClasses from the live cluster.

Harvester generates an `lh-<uuid>` StorageClass per VM image, with new
UUIDs on every (re)install. Manifests therefore keep image-backed disks
on placeholders (`lh-PENDING-<image>`, derived from the disk's
`harvesterhci.io/imageId` annotation); this script replaces each
placeholder with the image's live `.status.storageClassName`.

Only entries carrying an imageId annotation are touched. Blank disks
(e.g. `longhorn-ssd`) are left alone. Already-filled values are verified
against the live class and reported if they drift. Idempotent.

Usage:
  python3 workloads/storage/refresh-image-classes.py [--dry-run]
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
SCAN_DIRS = (
    os.path.join(ROOT, "workloads", "templates"),
)
TEMPLATES_ANNOTATION = "harvesterhci.io/volumeClaimTemplates"
IMAGE_ANNOTATION = "harvesterhci.io/imageId"
PLACEHOLDER_PREFIX = "lh-PENDING-"


def placeholder_for(image_id):
    return PLACEHOLDER_PREFIX + image_id.split("/")[-1]


def fetch_image_classes():
    p = subprocess.run(
        ["kubectl", "get", "vmimage", "-n", "harvester-public", "-o", "json"],
        capture_output=True, text=True, check=False,
    )
    if p.returncode != 0:
        sys.exit(f"kubectl get vmimage failed:\n{p.stderr}")
    classes = {}
    for item in json.loads(p.stdout).get("items", []):
        name = item["metadata"]["name"]
        sc = item.get("status", {}).get("storageClassName", "")
        if sc:
            classes[f"harvester-public/{name}"] = sc
    return classes


def claim_blocks(doc):
    """Yield volumeClaimTemplates JSON strings found anywhere in a doc.

    Template versions nest it under spec.vm.metadata.annotations.
    """
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key == TEMPLATES_ANNOTATION and isinstance(value, str):
                yield value
            else:
                yield from claim_blocks(value)
    elif isinstance(doc, list):
        for value in doc:
            yield from claim_blocks(value)


def plan_file(path, image_classes):
    """Return [(old, new)] substitutions needed in path."""
    with open(path) as f:
        text = f.read()
    plan = []
    for doc in yaml.safe_load_all(text):
        for templates in claim_blocks(doc):
            for claim in json.loads(templates):
                image_id = claim.get("metadata", {}).get("annotations", {}).get(
                    IMAGE_ANNOTATION
                )
                if not image_id:
                    continue
                live = image_classes.get(image_id)
                if not live:
                    sys.exit(f"{path}: image {image_id} has no live "
                             f"storage class (image missing or still downloading?)")
                current = claim["spec"]["storageClassName"]
                if current == live:
                    continue
                if current != placeholder_for(image_id):
                    sys.exit(f"{path}: {image_id} pins unexpected class "
                             f"{current!r} (expected placeholder or {live!r})")
                plan.append((current, live))
    return plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    image_classes = fetch_image_classes()
    changed = False
    for d in SCAN_DIRS:
        for name in sorted(os.listdir(d)):
            if not name.endswith((".yaml", ".yml")):
                continue
            path = os.path.join(d, name)
            plan = plan_file(path, image_classes)
            for old, new in plan:
                changed = True
                print(f"{path}: {old} -> {new}")
                if not args.dry_run:
                    with open(path) as f:
                        text = f.read()
                    assert text.count(f'"{old}"') >= 1, old
                    with open(path, "w") as f:
                        f.write(text.replace(f'"{old}"', f'"{new}"'))
    if not changed:
        print("all image classes already match the live cluster")


if __name__ == "__main__":
    sys.exit(main())
