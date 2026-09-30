# Harvester config — lhm.prod.sigaint.au

Cluster: `harvester-primary.lhm.prod.sigaint.au` (VIP `10.120.14.5`).
Nodes (`eno1` = management on VLAN 14 access ports, `eno2` = trunk to
`sfp-sfpplus20/22/24` carrying tagged `1,10-14,20,21`):

| Host (FQDN) | IP | MAC (eno1) | Install |
| --- | --- | --- | --- |
| harvester-node-ab56 | 10.120.14.11 | 44:A8:42:0A:5E:E3 | create (first) |
| harvester-node-527f | 10.120.14.12 | 90:B1:1C:3D:75:1C | join |
| harvester-node-49f4 | 10.120.14.13 | 90:B1:1C:3D:87:4B | join |

Install disk + data disk: `/dev/sda`. Mgmt MTU 9000 on `eno1`
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
  PCI claims, reference VMs.

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
- In Harvester UI > Hosts, evict/delete `sda`-backed default disk.

Pin Longhorn before it can claim Ceph disks:

```sh
export KUBECONFIG="$HOME/.kube/config" # or the Harvester kubeconfig path
kubectl get nodes
kubectl apply -f workloads/storage/disk-manager.yaml
kubectl get configmap harvester-node-disk-manager -n harvester-system -o yaml
```

Create networking in webhook order:

```sh
kubectl apply -f bootstrap/clusternetwork.yaml
kubectl apply -k bootstrap/
kubectl get net-attach-def -n default -o custom-columns=NAME:.metadata.name,ROUTE:.metadata.annotations.network\\.harvesterhci\\.io/route,READY:.metadata.labels.network\\.harvesterhci\\.io/ready
```

Install the pinned Rook operator, then the storage layer:

```sh
ROOK=$(python3 -c "import yaml;print(yaml.safe_load(open('nodes.yaml'))['storage']['rook_version'])")
kubectl apply -f https://raw.githubusercontent.com/rook/rook/$ROOK/deploy/examples/crds.yaml
kubectl apply -f https://raw.githubusercontent.com/rook/rook/$ROOK/deploy/examples/common.yaml
kubectl apply -f https://raw.githubusercontent.com/rook/rook/$ROOK/deploy/examples/operator.yaml
kubectl -n rook-ceph wait --for=condition=Available deploy/rook-ceph-operator --timeout=10m
kubectl apply -k workloads/storage/
kubectl -n rook-ceph get cephcluster rook-ceph -o jsonpath='{.status.ceph.health}{"\n"}'
```

Expected: `HEALTH_OK` before creating images.

Create images on `rook-ceph-block`, refresh dead image classes, then apply:

```sh
kubectl get vmimage -n harvester-public
kubectl get storageclass
grep -R '"storageClassName": "lh-' -n workloads/vms workloads/templates || true
kubectl apply -k workloads/
```

## VM recipe (learned the hard way)

- VLAN NICs need `bridge: {}` binding, `virtio` model.
- Root-disk PVC templates MUST set `storageClassName` to the image's
  own class (`kubectl get vmimage -n harvester-public`; formerly `lh-*`,
  now derived from `rook-ceph-block`);
  without it the disk is empty and the guest never boots.
- Tumbleweed/wicked leaves NICs down unless cloud-init `networkData`
  configures them; match by `driver: virtio_net` (MACs are random per VMI).
- UEFI images (Fedora UKI): `firmware.bootloader.efi.secureBoot: false`.
- Windows 11 from ISO (`workloads/vms/win11-ref-01.yaml`): the installer
  ISO MUST be a `cdrom` on `bus: sata` with `bootOrder: 1` — a virtio-bus
  ISO shows "press any key" then hangs at the Tianocore logo. Win11 also
  requires `efi.secureBoot: true` + `features.smm.enabled: true` +
  `devices.tpm: {}` (opposite of the Linux recipe), q35, ≥2 CPU / 4 GiB /
  64 GiB disk. Second SATA CD-ROM with the `virtio-win` image supplies
  the Viostor/NetKVM drivers at Setup's disk-selection step. After
  install: stop the VM, remove both CD-ROMs, set rootdisk `bootOrder: 1`.

## Reference

- `ssh mhahl@<vm-ip>` (hardware-key touch). Jumbo check from a VM:
  `ping -M do -s 8972 10.120.14.1`.
- GPU attach: claim exists → add `nvidia.com/GP107GL_QUADRO_P400` to the
  VM devices, pin the VM to its node. P400s on 527f/49f4 only (ab56: none).
  Template `gpu-tumbleweed-p400` (4CPU/8G/50G, vlan14) is ready in
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
- Namespaces (`server-lhm-prod`, `servers-lhm-dev`): images and keys are
  shared by reference, nothing is copied — `harvester-public/<image>` in
  the disk template, `default/<key>` in `sshNames`, and
  `default/<network>` in the multus `networkName`. Proven with a booted
  VM in `servers-lhm-dev` (DHCP `.13.111`).
- Longhorn: replica auto-balance `least-effort`. Daily VM backups via
  `workloads/backups/` (`ScheduleVMBackup` is one object per VM, daily
  02:00, keep 7) — flip `suspend:false` once the target VM exists.
- QNAP backup at `nfs://10.120.14.100:/Backup` (SERVER-VLAN address; the
  `qnap.sigaint.au` name resolves to the USER VLAN, which nodes can't
  reach). Verified healthy; test backup `ref-tumbleweed-01-test1` done.
- Storage (redeploy layout): Longhorn keeps `/dev/sdb` only (pinned via
  `workloads/storage/disk-manager.yaml`); Rook OSDs take `sdc+`
  (`sdg` missing on 49f4 — the `^sd[c-g]$` device filter matches what
  exists). 5 × 931G disks per node. sda is OS-only. VM images live on
  `rook-ceph-block`; the per-image classes in `workloads/vms/` and
  `workloads/templates/` must be refreshed from `kubectl get vmimage -n
  harvester-public` after every redeploy. Harvester VM backups cover
  Longhorn volumes only — Rook VMs are excluded from `ScheduleVMBackup`.
