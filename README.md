# harvester-lhm-config

Declarative config for the Harvester cluster at `lhm.prod.sigaint.au`
(VIP `10.120.14.5`). Everything is rendered or applied from here;
manual UI/SSH is limited to the post-install safety checklist.

## What lives here

| Path | Purpose |
| --- | --- |
| `nodes.yaml` | Single source of truth: nodes, keys, images, backup, GPUs |
| `pxe/render.py` | Generates `pxe/config-*.yaml` + `pxe/boot.ipxe` |
| `bootstrap/` | `external` cluster network (eno2) + 7 VLAN networks |
| `workloads/` | Images, SSH keys, addons, backup target, GPU claims, VM templates (no VM instances — config only) |
| `workloads/storage/` | Longhorn disk pin (`sdb`+`sdc`+`sdd`), SSD/HDD tier classes, snapshot class, CSI settings |

## Prerequisites

- Python 3 with `pyyaml`, `kubectl`, `kustomize`
- This repo checked out; cluster kubeconfig in `~/.kube/config` (or set `KUBECONFIG`)
- HTTP server serving `pxe/` + Harvester `vmlinuz`/`initrd`/`rootfs.squashfs`
  at `http://10.120.14.100/harvester/`

## Step by step

### 1. Render the install configs

```sh
python3 pxe/render.py
```

On first run this creates `.token` (gitignored). Back it up elsewhere.
Re-running must produce no diff unless `nodes.yaml` changed:

```sh
git status --short pxe/
```

### 2. Install the nodes (PXE)

Boot `harvester-node-ab56` first (iPXE auto-selects by MAC; menu fallback).
Wait for the UI at `https://harvester-primary.lhm.prod.sigaint.au`,
then boot `harvester-node-527f` and `harvester-node-49f4`.

### 3. Post-install (one time)

Run from the repo root against the Harvester cluster:

```sh
export KUBECONFIG="$HOME/.kube/config" # or the Harvester kubeconfig path
kubectl get nodes
kubectl get storageclass
```

Expected: all three nodes `Ready`; `harvester-longhorn` is present.

Manual safety steps first (UI/SSH, not repo commands):

- SSH as `rancher`, rotate the install token.
- Enable etcd snapshots.
- In Harvester UI > Hosts, confirm the default disk is `sdb`-backed
  (the Harvester data disk); evict and delete any auto-provisioned
  `sdd+` disks before production data lands.

Pin Longhorn to the data disks (`/dev/sdb` + `/dev/sdc` + `/dev/sdd`)
before it can claim anything else:

```sh
kubectl apply -f workloads/storage/disk-manager.yaml
kubectl get configmap harvester-node-disk-manager -n harvester-system -o yaml
```

Expected: `autoprovision.yaml` lists `/dev/sdb`, `/dev/sdc` and `/dev/sdd`.

Then continue in order: networking (§4), storage (§5), workloads (§6),
VM verification (§7), backups (§8).

### 4. Cluster networking

Order matters — the webhook requires the cluster network first:

```sh
kubectl apply -f bootstrap/clusternetwork.yaml
kubectl apply -k bootstrap/
```

Verify every network reports DHCP-discovered routes:

```sh
kubectl get net-attach-def -n harvester-public -o custom-columns=NAME:.metadata.name,ROUTE:.metadata.annotations.network\\.harvesterhci\\.io\\/route,READY:.metadata.labels.network\\.harvesterhci\\.io\\/ready
```

Expect all seven `connectivity:true`, `ready=true`.

### 5. Storage (Longhorn, SSD + HDD tiers)

Tier classes select disks by tag (`ssd` -> `sdb`+`sdc`,
`hdd` -> `sdd`, from `nodes.yaml` `storage.longhorn_tiers`):

```sh
kubectl apply -k workloads/storage/
python3 workloads/storage/tag-longhorn-disks.py --dry-run
python3 workloads/storage/tag-longhorn-disks.py
kubectl get storageclass longhorn-ssd longhorn-hdd
kubectl get volumesnapshotclass longhorn-snapshot
kubectl get setting csi-driver-config csi-online-expand-validation
```

Expected: both tier classes exist; re-running the tag script reports
`tags already correct` on all three nodes.

### 6. Workloads

Create new VM images with StorageClass `longhorn-ssd` on the Storage
tab. Image-backed disks carry `lh-PENDING-<image>` placeholders
(Harvester regenerates the real `lh-*` class per image on every
redeploy); fill them from the live cluster before applying the templates:

```sh
python3 workloads/storage/refresh-image-classes.py --dry-run
python3 workloads/storage/refresh-image-classes.py
grep -R 'lh-PENDING' -n workloads/templates && echo STALE || echo OK
```

Expected: the script reports one substitution per image-backed disk;
the final grep finds nothing.

```sh
kubectl apply -k workloads/
```

This creates the images (download takes minutes), SSH KeyPairs, enables
`pcidevices-controller`, sets the NFS backup target, claims the P400 GPUs,
and applies the VM templates. This repo is config only: it holds no VM
instances. Create VMs from the templates in the Harvester UI
(Virtual Machines > Create from Template), never as manifests here.

### 7. Verify a VM end to end

```sh
VM="<vm-name>" # VM created from a template via the UI
kubectl get vmi "$VM" -n default
# guest boot log (hostname, DHCP IP, login prompt)
POD=$(kubectl get pods -n default -o name | grep "virt-launcher-$VM" | head -n 1)
kubectl exec -n default "$POD" -c compute -- cat /var/run/kubevirt-private/*/virt-serial0-log
VM_IP="<vm-ip>" # replace with the guest IP from the VMI/agent
ssh "mhahl@${VM_IP}" # run from a host on the same VLAN
```

VM recipe rules (all encoded in `workloads/templates/`): `bridge: {}` on VLAN
NICs, root-disk template uses the image's own `lh-*` storage class
(`kubectl get vmimage -n harvester-public`), cloud-init `networkData`
matched by `driver: virtio_net`, `secureBoot: false` for UEFI images
(Linux only — Windows 11 needs `secureBoot: true` + SMM + TPM, see
`pxe/README.md` and `workloads/templates/tpl-win11-iot-ltsc-2024-amd64-80g-ssd-r1.yaml`).

### 8. Backups

All volumes are Longhorn now, so every VM volume can be backed up —
no Rook exclusion to worry about. Backup schedules (`ScheduleVMBackup`,
one object per VM) are created alongside their VM, not stored here.

Target `nfs://10.120.14.100:/Backup` must be reachable from SERVER VLAN
(the `qnap.sigaint.au` name resolves to USER VLAN — don't use it).
Check health:

```sh
kubectl get setting backup-target -o jsonpath='{.value}{"\n"}'
```

## Rollback

- PXE: re-render from the previous `nodes.yaml`.
- Networks: stop attached VMs first (Harvester blocks network changes
  with running VMs), then `kubectl delete -k bootstrap/`.
- Settings/addons/claims: `kubectl delete -k workloads/` (re-apply to restore).
