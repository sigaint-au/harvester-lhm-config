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

1. `python3 pxe/render.py` (creates `.token` on first run; back it up
   elsewhere, it is gitignored).
2. Serve `pxe/` over HTTP alongside the Harvester `vmlinuz`, `initrd`,
   `rootfs.squashfs` at `http://10.120.14.100/harvester/`.
3. Boot ab56 (iPXE auto-selects by MAC; menu fallback). Wait for the UI at
   `https://harvester-primary.lhm.prod.sigaint.au`, then boot the joins.
4. SSH as `rancher`; rotate the install token; set up etcd snapshots.
5. Storage: `kubectl apply -f workloads/storage/disk-manager.yaml` pins
   Longhorn to `/dev/sdb` (evict `/dev/sda`); Rook OSDs take `sdc+`
   via `workloads/storage/` step 5 in the root README.
6. `kubectl apply -f bootstrap/clusternetwork.yaml`, then
   `kubectl apply -k bootstrap/` (order matters: the webhook requires the
   cluster network to exist first).
7. Rook operator + `kubectl apply -k workloads/storage/` (root README
   step 5); wait for `HEALTH_OK` before creating images.
8. `kubectl apply -k workloads/` (backup-target needs the SERVER→QNAP
   firewall rule first, else the webhook rejects it — by design).
   Create images with StorageClass `rook-ceph-block`, then refresh the
   per-image classes in `workloads/vms/` + `workloads/templates/` before
   applying any VM.

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
