# harvester-lhm-config

Declarative config for the Harvester cluster at `lhm.prod.sigaint.au`
(VIP `10.120.14.5`). Everything is rendered or applied from here —
no click-ops needed to rebuild.

## What lives here

| Path | Purpose |
| --- | --- |
| `nodes.yaml` | Single source of truth: nodes, keys, images, backup, GPUs |
| `pxe/render.py` | Generates `pxe/config-*.yaml` + `pxe/boot.ipxe` |
| `bootstrap/` | `external` cluster network (eno2) + 7 VLAN networks |
| `workloads/` | Images, SSH keys, addons, backup target, GPU claims, VMs, backup schedules |

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

### 3. Post-install (one time, via UI/SSH)

1. SSH as `rancher`; rotate the install token; enable etcd snapshots.
2. Longhorn: disks auto-provision per `harvester-node-disk-manager`
   ConfigMap (`sdc–sdg`, `sdb–sdf` on 49f4). Evict + delete any
   sda-backed default disk before production data lands.

### 4. Cluster networking

Order matters — the webhook requires the cluster network first:

```sh
kubectl apply -f bootstrap/clusternetwork.yaml
kubectl apply -k bootstrap/
```

Verify every network reports DHCP-discovered routes:

```sh
kubectl get net-attach-def -n default -o custom-columns=NAME:.metadata.name,ROUTE:.metadata.annotations.network\\.harvesterhci\\.io/route,READY:.metadata.labels.network\\.harvesterhci\\.io/ready
```

Expect all seven `connectivity:true`, `ready=true`.

### 5. Workloads

```sh
kubectl apply -k workloads/
```

This creates the images (download takes minutes), SSH KeyPairs, enables
`pcidevices-controller`, sets the NFS backup target, claims the P400 GPUs,
registers the daily backup schedule (suspended), but **no VMs** — create
those deliberately, e.g.:

```sh
kubectl apply -f workloads/vms/user-test-01.yaml
```

### 6. Verify a VM end to end

```sh
# lease + agent IP
kubectl get vmi <name> -n default
# guest boot log (hostname, DHCP IP, login prompt)
POD=$(kubectl get pods -n default -o name | grep virt-launcher-<name> | head -n 1)
kubectl exec -n default $POD -c compute -- cat /var/run/kubevirt-private/*/virt-serial0-log
# SSH from a host on the same VLAN
ssh mhahl@<vm-ip>
```

VM recipe rules (all encoded in `workloads/vms/`): `bridge: {}` on VLAN
NICs, root-disk template uses the image's own `lh-*` storage class
(`kubectl get vmimage -n harvester-public`), cloud-init `networkData`
matched by `driver: virtio_net`, `secureBoot: false` for UEFI images.

### 7. Backups

Target `nfs://10.120.14.100:/Backup` must be reachable from SERVER VLAN
(the `qnap.sigaint.au` name resolves to USER VLAN — don't use it).
Check health:

```sh
kubectl get setting backup-target -o jsonpath='{.value}{"\n"}'
```

Enable a schedule once its VM exists:

```sh
kubectl patch schedulevmbackup <name> -n default --type=merge -p '{"spec":{"suspend":false}}'
```

## Rollback

- PXE: re-render from the previous `nodes.yaml`.
- Networks: stop attached VMs first (Harvester blocks network changes
  with running VMs), then `kubectl delete -k bootstrap/`.
- Settings/addons/claims: `kubectl delete -k workloads/` (re-apply to restore).
