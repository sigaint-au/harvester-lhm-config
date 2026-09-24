## Goal

Give the Harvester cluster external networks on all trunked VLANs via `eno2`, and automate the full lifecycle (PXE install, cluster networking, images, SSH keys, VMs) from the `harvester-config-lhm` repo using Kubernetes manifests only — no Terraform.

## Success Criteria

- Each node’s `eno2` carries tagged traffic for VLANs 10, 11, 12, 13, 14, 20, 21.
- One Harvester VM network per VLAN exists, reports active connectivity, and a test VM on each gets a router DHCP lease.
- `pxe/` configs and `boot.ipxe` are generated from a single `nodes.yaml`; committed per-node files match generator output.
- Images, SSH keys, cloud-init, and VMs are plain YAML applied with `kubectl`; re-apply is idempotent with no state files.
- Install token and kubeconfig are gitignored; rotation is documented.
- `backup-target` points at the QNAP NFS share and reports healthy; a test VM backup completes.
- The Quadro P400 is claimed for passthrough and attachable to a pinned VM.

## Context And Current Facts

- Nodes: `ab56` (.11), `527f` (.12), `49f4` (.13) on VLAN 14, gateway `.1`, VIP `.5`; `eno1` = management; disk `/dev/sda`; Harvester v1.9.0; SSH via `github:mhahl` (see `pxe/README.md`, `pxe/config-*.yaml`).
- Switch `sfp-sfpplus20/22/24` are `admit-only-vlan-tagged` trunks in `TRUNKS`, carrying tagged `1,10-14,20,21` (switch `config.rsc` lines 159-191).
- Router runs DHCP, DNS, gateway `.1` per VLAN with pools `.100-.200` (`.100-.199` on SERVER); `10.120.14.6-9` reserved as `HARVESTER_APIS`; LB VIPs at `.200-255`.
- Trunk ports run 9000 MTU; Harvester VM networks inherit MTU from the cluster-network uplink.
- [Execution result] Mgmt (`eno1`) rolled to MTU 9000 on all nodes 2026-09-24 (`bond-mgmt`+`bridge-mgmt` via host NetworkManager, one node at a time, no flaps), `mgmt` annotated `uplink-mtu=9000`, and `nodes.yaml`/`render.py` carry `mgmt_mtu` for reinstalls. Jumbo mesh verified node↔node, VIP, gateway. Mid-roll 9K→1.5K node pings fail as expected — converge all nodes before judging.
- `harvester-config-lhm` is not a git repo yet; no `docs/` or plan convention exists.
- Every Harvester object needed (cluster network, uplink config, VM networks, images, keys, VMs) is a Kubernetes CR applied through the cluster API, so `kubectl apply` covers all of it with no extra tooling.
- NFS backup target: QNAP `qnap.sigaint.au:/Backup`, export limited to `10.120.14.0/24` (all nodes are inside it).
- GPU: NVIDIA Quadro P400 (`10DE:1CB3`), intended resource `nvidia.com/GP107GL_QUADRO_P400`. Owner-supplied KubeVirt snippet is the controller's end state, not a manifest to apply by hand.

## Constraints And Non-goals

- No Terraform: user decision. No state files, no provider binaries; `kubectl` + `kustomize` only.
- No changes to RouterOS configs: DHCP, firewall, and trunk ports already provide what is needed.
- `eno1`/`mgmt` cluster network stays untouched; management IPs remain static.
- SERVER-VLAN exclusions (`.5`, `.6-9`, `.11-13`, `.200-255`) must not be handed to VMs.
- Non-goal: IPv6 VM networks (start v4, add v6 after DHCPv4 proven), storage network, Rancher provisioning.

## Key Decisions

- New cluster network `external` on `eno2`, not reuse of `mgmt`: isolates VM traffic and matches the custom-cluster-network model.
- [Draft, settled] Trunk NIC is `eno2` on all three nodes (confirmed by owner). All three switch trunk ports (`sfp-sfpplus20/22/24`) carry the identical tagged VLAN set, so no per-port mapping is needed in the manifests; one uplink config covers every node.
- One uplink config covering all three nodes, single NIC `eno2`, MTU `9000`: matches 9K trunks; single NIC keeps default `active-backup`.
- Per-VLAN networks use DHCP route mode (router DHCP): no IPAM to run; `route_connectivity` proves each VLAN end to end.
- Optional Trunk-mode network for in-guest tagging (firewall/router VMs); deferred unless needed.
- VM network names mirror the router `/interface vlan` names (`vlan10-mgmt` … `vlan21-vmnet`): one naming scheme across RouterOS and Harvester, so firewall logs, DHCP leases, and VM attachments all read the same.
- Manifests over Terraform: mirrors the network repo’s `.rsc`-as-source-of-truth style — every object is a reviewable file re-applied with one command, no state to back up or lock.
- Kustomize bases per layer (`bootstrap/`, `workloads/`) with no overlays initially: keeps `kubectl apply -k` as the single entry point while leaving room for per-env variants.
- Exact CRD field names pinned with `kubectl explain` against the live cluster before manifests are written: avoids drifting from the v1.9 API.
- `backup-target` is a `Setting` manifest (`type: nfs`, endpoint `nfs://qnap.sigaint.au:/Backup`, `refreshIntervalInSeconds` set, e.g. 60, to avoid the constant-refresh CPU bug).
- GPU passthrough goes through the `pcidevices-controller` addon (enabled at install via `install.addons.harvester_pcidevices_controller`), not a hand-edited KubeVirt CR: one `PCIDeviceClaim` per P400 per node, then attach by resource name in the VM spec. The controller reconciles `permittedHostDevices` itself.
- GPU VMs are pinned to their node (Node Scheduling): the addon reuses one resource descriptor per device type, so a multi-P400 cluster can otherwise schedule the VM on the wrong node.
- [Draft, settled] No `github:` shortcut anywhere. Install configs (`nodes.yaml`, rendered PXE) and workload `KeyPair` manifests pin explicit public key material. Owner's keys on file: two hardware-backed keys (`sk-ecdsa`, `sk-ed25519`, both also published at `github.com/mhahl.keys`) plus one plain `ssh-ed25519` on GitHub only. Single operator, no other key holders. [Draft, settled] Both hardware keys pinned (`sk-ecdsa-sha2-nistp256` + `sk-ssh-ed25519`); plain GitHub-only key excluded.

## Recommended Approach

Three layers in `harvester-config-lhm`, applied bottom-up: render PXE files from `nodes.yaml`; `kubectl apply -k bootstrap/` creates the `external` cluster network, uplink config, and seven VLAN networks; `kubectl apply -k workloads/` manages images, SSH keys, cloud-init secrets, and VMs. Secrets stay in gitignored files mirroring the network repo’s `secrets.rsc` pattern.

## Work Plan

### Phase 0 — Repo hygiene

- `git init`, `.gitignore` (`kubeconfig`, token files, editor backups), new layout: `nodes.yaml`, `pxe/render.py`, `bootstrap/`, `workloads/`.
- Validation: `git status` clean of secrets; layout matches plan.

### Phase 1 — Single-source PXE

- Add `nodes.yaml` (hostname, MACs, IPs, `eno2`, ISO URL + checksum, DNS/NTP) and `render.py` generating today’s three configs byte-identical plus `boot.ipxe` with `${mac}` auto-chain and menu fallback.
- Generate the install token at render time into a gitignored file; document rotation post-install.
- Validation: render output `diff` against current `pxe/` files is empty; test-boot one node to iPXE menu.

### Phase 2 — Cluster networking (manifests)

- Run `kubectl explain` on `clusternetwork`, `vlanconfig`, and `network` against the live cluster; write `bootstrap/` manifests from the returned schema: one cluster network `external`, one uplink config (`nics: [eno2]`, `mtu: 9000`, selector covering all nodes), seven DHCP-routed VM networks, one per VLAN:

| Network | VLAN | Subnet |
|---|---|---|
| `vlan10-mgmt` | 10 | 10.120.10.0/24 |
| `vlan11-secure` | 11 | 10.120.11.0/24 |
| `vlan12-security` | 12 | 10.120.12.0/24 |
| `vlan13-user` | 13 | 10.120.13.0/24 |
| `vlan14-server` | 14 | 10.120.14.0/24 |
| `vlan20-dmz` | 20 | 10.120.20.0/24 |
| `vlan21-vmnet` | 21 | 10.120.21.0/24 |

- Apply with `kubectl apply --dry-run=server -k bootstrap/` first (expect webhook errors: the ClusterNetwork must exist before dependents), then `kubectl apply -f bootstrap/clusternetwork.yaml` followed by `kubectl apply -k bootstrap/` for real.
- Validation: dry-run clean; after apply, every network shows active `route_connectivity`; a temp VM per VLAN gets a `.100+` lease and pings its `.1` gateway; `ping -s 8800` confirms MTU.
- [Execution result] All 7 VLANs proven end to end 2026-09-24: controller `connectivity:true` + correct DHCP CIDR/gateway on every NAD, plus live `udhcpc` leases from the router through the `external-br` bridge on each VLAN (`.10.103`, `.11.198`, `.12.110`, `.13.103`, `.14.198`, `.20.199`, `.21.199`, all /24, 86400s). Pod-level proof used netshoot pods with `k8s.v1.cni.cncf.io/networks` annotations (`kubectl run --overrides` does NOT carry annotations — use a manifest). `ping -s 8800` MTU check still open. Per-VLAN VM-lease proof still open (guest-side work, not network).

### Phase 3 — Day-2 workloads (manifests)

- `workloads/` manifests: two `VirtualMachineImage` downloads into `harvester-public` ([Draft, settled] openSUSE Tumbleweed Minimal Cloud Snapshot20260922 + Fedora 44 Cloud Base UEFI UKI, URLs in `nodes.yaml`/workloads kustomization — no Ubuntu, no Leap; short names `tumbleweed-minimal` and `fedora-44-uki`), `KeyPair` objects with the two pinned hardware keys (no `github:` bootstrap), cloud-init `Secret`s, one reference `VirtualMachine` per common shape with lease-wait and keys attached.
- Validation: `kubectl apply --dry-run=server -k workloads/` clean; reference VM boots, lease appears, SSH works with managed key only; re-apply is a no-op.
- [Execution result] `ref-tumbleweed-01` (2CPU/4Gi/20Gi, `vlan14-server`) booted first try on the corrected recipe, DHCP `10.120.14.196`, agent READY. Recipe rules now in runbook: image-owned `storageClassName` on disk templates (missing class = empty unbootable disk — root-caused via OVMF "no bootable device" + zero-byte serial), `bridge:{}` NIC binding, driver-match `networkData`, `secureBoot:false` for UKI.
- [Execution result] Owner SSH into `user-test-01` (`vlan13-user`, DHCP `10.120.13.105`) succeeded with the pinned `sk-ssh-ed25519` hardware key — full loop proven: manifest → boot → DHCP → cloud-init user/keys → SSH. SERVER-VLAN `.196` stays unreachable from the VPN path (zone policy); USER-VLAN access works.

### Phase 5 — Post-install services (manifests)

- 5a NFS backup target. `workloads/` (or `bootstrap/`) `Setting` manifest `backup-target` with endpoint `nfs://qnap.sigaint.au:/Backup`. Prerequisites verified first: QNAP serves NFSv4 on `/Backup`, `qnap.sigaint.au` resolves from the nodes via `10.120.14.1`, export covers the node IPs.
- Validation: setting reports healthy (no high-CPU refresh loop); a test VM backup to the share completes and restores.
- 5b GPU passthrough. `nodes.yaml` sets `install.addons.harvester_pcidevices_controller.enabled: true`; after install, `PCIDeviceClaim` manifest(s) for each P400 (fields pinned via `kubectl explain pcideviceclaim`), bound to `vfio-pci`; reference GPU VM attaches `nvidia.com/GP107GL_QUADRO_P400` and is node-pinned. Never claim `eno1`/`eno2` or other host-owned NICs.
- Validation: claims show `passthroughEnabled`; `lspci` inside the guest shows the P400; guest NVIDIA driver loads.

### Phase 6 — Runbook

- Rewrite `pxe/README.md`: render flow, apply order (PXE, bootstrap, workloads, post-install services), token rotation, Longhorn disk step carried over.
- Validation: fresh-eyes read-through; commands copy-paste cleanly.

## Validation Plan

- Phase 1: generator `diff` empty; iPXE menu renders on test boot.
- Phase 2 (highest-risk): per-VLAN DHCP lease + gateway ping + jumbo ping; `route_connectivity` active on all seven networks. Any failure isolates to one layer: lease fail = trunk/tagging or DHCP; ping fail = firewall zone policy.
- Phase 3: VM reachable over SSH via managed key; `kubectl apply -k workloads/` re-run is a no-op (idempotence).
- Phase 5: `backup-target` healthy with `refreshIntervalInSeconds` set; test backup completes and restores; P400 claim `passthroughEnabled`, guest `lspci` + driver OK.
- Rollback per phase: re-render old PXE files; `kubectl delete -k bootstrap/` (stop attached VMs first — Harvester blocks network changes with running VMs).

## Risks / Rollback

- Wrong `eno2` name or cabling (see Open Questions): uplink config applies but connectivity stays error; fix mapping, re-apply. No host impact.
- MTU change with running VMs is rejected by the webhook: schedule MTU work before workloads land.
- Install token served over HTTP at PXE time: short-lived token file, rotate immediately after join, never commit.
- Single-NIC uplink means no NIC redundancy on `external`; a second trunk NIC per node can be added to the same uplink config later.
- Manifests apply imperatively per layer: keep the phase order (PXE, bootstrap, workloads, post-install) since VM networks require the cluster network to exist first.
- VM secondary NICs require an explicit `bridge: {}` binding (omit only for the masquerade default network); without it the VMI fails with "undefined binding method".
- Tumbleweed/wicked leaves unconfigured NICs DOWN: guests need explicit cloud-init `networkData` (match by `driver: virtio_net`, never by MAC — KubeVirt assigns a fresh random MAC per VMI).
- [Execution result] Backup target resolved: QNAP NFS is also exported on the SERVER VLAN at `10.120.14.100` (owner info; `qnap.sigaint.au` points at the USER-VLAN `.13.12`, which SERVER cannot reach — no firewall change needed). `Setting` applied, `configured=True`, Longhorn target `available=true`, and test backup `ref-tumbleweed-01-test1` completed `ready=true`/100%. No `.rsc` change required.
- QNAP must serve NFSv4 on `/Backup` (Longhorn requirement); NFSv3-only exports fail the target health check.
- Claiming a host-owned NIC (`eno1`/`eno2`) for passthrough can take down the node: double-check PCI addresses against the host NICs before claiming.

## Open Questions

- [Draft, settled] Per-port cable mapping (`sfp-sfpplus20/22/24` to specific nodes) is not needed: all three ports carry the identical tagged set, and the trunk NIC is `eno2` on every node. Phase 2 `route_connectivity` still proves each leg.
- [Draft, settled, CORRECTED by live inventory] P400s exist only on `harvester-node-49f4` and `harvester-node-527f` (both at `0000:03:00.0` VGA + `0000:03:00.1` audio, IOMMU group 23). `harvester-node-ab56` has only the Matrox BMC VGA — no P400, despite the earlier assumption. Phase 5b claims both functions per GPU node (audio shares the IOMMU group). Claim `metadata.name` must equal the `PCIDevice` object name (e.g. `harvester-node-49f4-000003000`), enforced by the webhook. Live `PCIDevice` already advertises `resourceName: nvidia.com/GP107GL_QUADRO_P400`. GPU VMs still pin to their node per the scheduling known issue.
- None other: defaults above (manifests + kustomize, DHCP route mode, deferred trunk-mode network) are reversible and recorded as decisions.

## Sources

- https://raw.githubusercontent.com/harvester/docs/HEAD/docs/networking/harvester-network.md
- https://raw.githubusercontent.com/harvester/docs/HEAD/docs/networking/clusternetwork.md
- https://raw.githubusercontent.com/harvester/docs/HEAD/docs/install/harvester-configuration.md
- https://raw.githubusercontent.com/harvester/terraform-provider-harvester/master/docs/resources/network.md
- https://raw.githubusercontent.com/harvester/terraform-provider-harvester/master/docs/resources/vlanconfig.md
- https://raw.githubusercontent.com/harvester/terraform-provider-harvester/master/docs/resources/image.md
- https://raw.githubusercontent.com/harvester/terraform-provider-harvester/master/docs/resources/ssh_key.md
- https://raw.githubusercontent.com/harvester/terraform-provider-harvester/master/docs/resources/virtualmachine.md
- https://raw.githubusercontent.com/harvester/docs/HEAD/docs/advanced/addons/pcidevices.md
- https://raw.githubusercontent.com/harvester/docs/HEAD/docs/upgrade/v1-4-1-to-v1-4-2.md
