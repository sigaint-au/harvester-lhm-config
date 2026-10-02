# Harvester config — lhm.prod.sigaint.au

Cluster: `harvester-primary.lhm.prod.sigaint.au` (VIP `10.120.14.5`).
Nodes (`eno1` = management on VLAN 14 access ports, `eno2` = trunk to
`sfp-sfpplus20/22/24` carrying tagged `1,10-14,20,21`):

| Host (FQDN) | IP | MAC (eno1) | Install |
| --- | --- | --- | --- |
| harvester-node-ab56 | 10.120.14.11 | 44:A8:42:0A:5E:E3 | create (first) |
| harvester-node-527f | 10.120.14.12 | 90:B1:1C:3D:75:1C | join |
| harvester-node-49f4 | 10.120.14.13 | 90:B1:1C:3D:87:4B | join |

Install disk: `/dev/sda`, data disk: `/dev/sdb`. Mgmt MTU 9000 on `eno1`
(`bond-mgmt`/`bridge-mgmt` profiles + `mgmt` uplink-mtu annotation; roll
one node at a time and re-check jumbo mesh). DNS `139.99.149.92,
139.99.210.89, 139.99.210.170`. NTP Google. SSH: pinned operator keys
(see `nodes.yaml`, no `github:` shortcuts).

## Layout

- `nodes.yaml` — single source of truth (nodes, keys, images, backup, GPUs).
- `pxe/render.py` — generates `pxe/config-*.yaml` + `pxe/boot.ipxe`. Never
  hand-edit generated files.
- `.token` (gitignored) — install token. Precedence: `--token` flag >
  `$HARVESTER_TOKEN` > `.token` file > generate new.
- `bootstrap/` — `external` cluster network (eno2, MTU 9000) + one
  `vlan<ID>-<zone>` VM network per VLAN (names mirror the router).
- `workloads/` — images, KeyPairs, addon enablement, backup target,
  PCI claims, VM templates (no VM instances — config only).

## Fresh install

Render install configs:

```sh
python3 pxe/render.py
git status --short pxe/
```

Expected: three `config-*.yaml` files plus `boot.ipxe`; first run also
creates gitignored `.token` (back it up elsewhere).

Serve `pxe/` over HTTP alongside Harvester `vmlinuz`, `initrd`, and
`rootfs.squashfs` at `http://10.120.14.100/harvester/`, then boot ab56
(iPXE auto-selects by MAC; menu fallback). Wait for the UI at
`https://harvester-primary.lhm.prod.sigaint.au`, then boot the joins.

Manual safety steps:

- SSH as `rancher`, rotate the install token.
- Enable etcd snapshots.
- In Harvester UI > Hosts, confirm the default disk is `sdb`-backed
  (the Harvester data disk); evict/delete any auto-provisioned `sdd+`
  disks.

Pin Longhorn to the data disks before it can claim anything else:

```sh
export KUBECONFIG="$HOME/.kube/config" # or the Harvester kubeconfig path
kubectl get nodes
kubectl apply -f workloads/storage/disk-manager.yaml
kubectl get configmap harvester-node-disk-manager -n harvester-system -o yaml
```

Expected: `autoprovision.yaml` lists `/dev/sdb`, `/dev/sdc` and `/dev/sdd`.

Create networking in webhook order:

```sh
kubectl apply -f bootstrap/clusternetwork.yaml
kubectl apply -k bootstrap/
kubectl get net-attach-def -n harvester-public -o custom-columns=NAME:.metadata.name,ROUTE:.metadata.annotations.network\\.harvesterhci\\.io\\/route,READY:.metadata.labels.network\\.harvesterhci\\.io\\/ready
```

Apply the storage layer, then tag the disks into tiers (`ssd`/`hdd`
from `nodes.yaml`) so the tier classes select the right media:

```sh
kubectl apply -k workloads/storage/
python3 workloads/storage/tag-longhorn-disks.py --dry-run
python3 workloads/storage/tag-longhorn-disks.py
kubectl get storageclass longhorn-ssd longhorn-hdd
```

Expected: both tier classes exist before creating images.

Create images on `longhorn-ssd`, fill the `lh-PENDING-*` placeholders
from the live cluster, then apply:

```sh
python3 workloads/storage/refresh-image-classes.py --dry-run
python3 workloads/storage/refresh-image-classes.py
```

```sh
kubectl get vmimage -n harvester-public
kubectl get storageclass
grep -R '"storageClassName": "lh-' -n workloads/templates || true
kubectl apply -k workloads/
```

## VM recipe (learned the hard way)

- VLAN NICs need `bridge: {}` binding, `virtio` model.
- Root-disk PVC templates MUST set `storageClassName` to the image's
  own class (`kubectl get vmimage -n harvester-public`; `lh-*` names are
  recreated on every redeploy, so refresh them from the live list);
  without it the disk is empty and the guest never boots.
- Tumbleweed/wicked leaves NICs down unless cloud-init `networkData`
  configures them; match by `driver: virtio_net` (MACs are random per VMI).
- UEFI images (Fedora UKI): `firmware.bootloader.efi.secureBoot: false`.
- Windows 11 IoT Enterprise LTSC 2024 from ISO (`workloads/templates/tpl-win11-iot-ltsc-2024-amd64-80g-ssd-r1.yaml`): the installer
  ISO MUST be a `cdrom` on `bus: sata` with `bootOrder: 1` — a virtio-bus
  ISO shows "press any key" then hangs at the Tianocore logo. Win11 also
  requires `efi.secureBoot: true` + `features.smm.enabled: true` +
  `devices.tpm: {}` (opposite of the Linux recipe), q35, ≥2 CPU / 4 GiB /
  64 GiB disk. Second SATA CD-ROM is a `containerDisk` with the SUSE VMDP
  image (`registry.suse.com/suse/vmdp/vmdp:2.5.5`) supplying the
  Viostor/NetKVM drivers at Setup's disk-selection step. After
  install: stop the VM, remove both CD-ROMs, set rootdisk `bootOrder: 1`.

## Reference

- `ssh mhahl@<vm-ip>` (hardware-key touch). Jumbo check from a VM:
  `ping -M do -s 8972 10.120.14.1`.
- GPU attach: claim exists → add `nvidia.com/GP107GL_QUADRO_P400` to the
  VM devices, pin the VM to its node. P400s on 527f/49f4 only (ab56: none).
  Template `tpl-tumbleweed-amd64-50g-ssd-p400` (4CPU/8G/50G, vlan14) is ready in
  `harvester-public` — still pin the node at creation.
- GPU claims stuck `In Progress` ("Cannot find PCIDevice that owns …" in
  the pcidevices-controller log): manifest-applied claims lack the
  `ownerReferences` entry the controller requires (UI-created claims get it
  automatically). Patch each claim to reference its same-named PCIDevice:
  `DUID=$(kubectl get pcidevice <claim> -o jsonpath='{.metadata.uid}')`
  then `kubectl patch pcideviceclaim -n <ns> <claim> --type=merge -p
  '{"metadata":{"ownerReferences":[{"apiVersion":"devices.harvesterhci.io/v1beta1","kind":"PCIDevice","name":"<claim>","uid":"'$DUID'","controller":true,"blockOwnerDeletion":true}]}}'`.
  A transient `vfio-pci/bind: device or resource busy` on the first retry is
  normal (both GPU functions race for the IOMMU group); it clears once both
  functions sit on vfio-pci.
- Namespaces (`server-lhm-prod`, `servers-lhm-dev`): images, keys and
  networks are shared by reference, nothing is copied —
  `harvester-public/<image>` in the disk template, `default/<key>` in
  `sshNames`, and `harvester-public/<network>` in the multus `networkName`
  (bare names resolve in the VM's own namespace, so templates qualify
  them). The VLAN networks live in `harvester-public` for this reason.
- Longhorn: replica auto-balance `least-effort`. VM backups cover all
  volumes (everything is Longhorn); create each VM's `ScheduleVMBackup`
  alongside its VM, not in this repo.
- QNAP backup at `nfs://10.120.14.100:/Backup` (SERVER-VLAN address; the
  `qnap.sigaint.au` name resolves to the USER VLAN, which nodes can't
  reach). Verified healthy; test backup `ref-tumbleweed-01-test1` done.
- Storage: Longhorn only, two tiers by disk tag (uniform on all three
  nodes). sda 250G boot SSD (OS/install); Harvester data disk `/dev/sdb`
  (450G SSD). SSD tier (`longhorn-ssd`, tag `ssd`): sdb + sdc (1T) — VM
  roots, images, system, backups. HDD tier (`longhorn-hdd`, tag `hdd`):
  sdd (3.5T) — bulk data. Both replica 3, pinned via
  `workloads/storage/disk-manager.yaml`, tags applied via
  `workloads/storage/tag-longhorn-disks.py` from `nodes.yaml`. VM images
  live on `longhorn-ssd`; the per-image classes in `workloads/templates/`
  must be refreshed from `kubectl get vmimage -n
  harvester-public` after every redeploy.
