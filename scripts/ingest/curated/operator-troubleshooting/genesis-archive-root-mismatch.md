---
title: "Node won't sync: 'incompatible: genesis archive root … Entering standby mode'"
---

# Node enters standby mode: "Rollup at 0x… is incompatible: genesis archive root (expected …, got …)"

If your Aztec node refuses to sync and the logs say the rollup is **incompatible**
because of a **genesis archive root** mismatch, and the node has dropped into
**standby mode**, the cause is almost always a **version mismatch between your
node software and the network you are connecting to** — not a wrong RPC URL and
not a wrong `--network` flag. The most common form is running a node image built
for one network (for example a mainnet version) against a different network (for
example testnet): live networks may run different Aztec versions, and each
version produces a different genesis archive root, so a binary built for one
network's version cannot sync another network running a different version. The
fix is to run the node version that matches the network, which you can confirm
against `networks.md`.
The rest of this page explains how to confirm and fix it.

## Symptom

Your node will not sync. Shortly after start-up the logs show a line like:

```
Rollup at 0x… is incompatible: genesis archive root (expected 0x…, got 0x…). Entering standby mode.
```

The node then sits in **standby mode** and never advances its synced block
number. You may also see it described as a "genesis archive root mismatch" or a
"genesis hash mismatch".

## Most common cause: your node version does not match the network's version

This error is almost always a **version skew between your node software and the
network you are connecting to** — not a wrong RPC URL and not a wrong network
flag.

Every Aztec rollup contract on L1 commits a **genesis archive root** at the
moment it is deployed. This root is derived from the protocol constants and
verification keys that are **compiled into a specific Aztec release**. On
start-up your node independently recomputes the genesis archive root it expects
from its own compiled-in parameters and compares it against the root stored in
the L1 rollup contract. If the two roots differ, the node refuses to validate
or follow that chain and drops into **standby mode** to avoid acting on state it
cannot agree on.

Because the genesis archive root is a function of the **protocol version**, the
roots only match when the version your node runs is the same version the
network's rollup was deployed with. Running an image built for one network's
version against a different network produces exactly this mismatch.

The classic case: a node image pinned to one network's version is started with
the other network's `--network` flag — for example a mainnet-version image
started with `--network testnet`, or vice-versa. When the two networks run
different Aztec versions (check `networks.md` for the current versions), their
rollup contracts have different genesis archive roots, so a binary built for one
network's version cannot sync the other.

## How to confirm it is a version mismatch

1. **Find the version your node is running.** Check the image tag in your
   `docker-compose.yml` (for example `image: "aztecprotocol/aztec:<tag>"`) or
   the version printed in the node's start-up logs.

2. **Find the version the target network expects.** Look up the live version
   for the network you are connecting to. The authoritative, always-current
   source is the **Network technical information** table on the networks page
   (`networks.md`, rendered at https://docs.aztec.network/networks), which lists
   the current version for each live network (mainnet and testnet). **Do not
   rely on a remembered version number — networks are upgraded, so always read
   the current value from `networks.md`.**

3. **Compare.** If your image tag is not the version `networks.md` lists for the
   network in your `--network` flag, that mismatch is the cause. For example, if
   `networks.md` shows testnet on one version and your image is pinned to the
   (different) mainnet version, a node started with `--network testnet` will hit
   this error.

4. **Cross-check tell:** if the *exact same configuration* syncs fine against
   one network but fails with the genesis mismatch against the other, that is a
   strong signal of version skew — your pinned image matches the version of the
   network that works and not the one that fails. It is **not** evidence that
   your RPC endpoints or `--network` flag are wrong.

## How to fix it

1. **Set your node image to the version the target network runs.** Read the
   current version for your network from `networks.md` and pin your image to it:

   ```yaml
   # docker-compose.yml — use the version networks.md lists for YOUR network
   image: "aztecprotocol/aztec:<version-from-networks.md>"
   ```

   If you install via `aztec-up`, install that same version instead.

2. **Pull the corrected image and restart:**

   ```bash
   docker compose pull
   docker compose down
   docker compose up -d
   ```

3. **Watch the logs.** Once the node's version matches the network, the genesis
   archive root will match, the node will leave standby mode, and the synced
   block number will start advancing.

You should not need to wipe your data directory just to fix a version mismatch
— correcting the version and restarting is enough. Only clear node data if the
logs indicate corrupted or stale local state after the version already matches.

## Why the node goes to standby instead of erroring out

Standby mode is the node deliberately refusing to attest, propose, or follow a
chain whose genesis state it cannot reproduce. It is a safety stop, not a crash.
The node will leave standby on its own once the genesis archive root it computes
matches the root in the L1 rollup contract — which happens as soon as it is
running the version that matches the network.

## Less common causes (check only after version is confirmed correct)

If your node version already matches the version `networks.md` lists for your
network and you still see the mismatch, then check:

- **Wrong rollup contract / registry address.** If you have overridden the L1
  contract addresses in your configuration, a stale or wrong rollup address
  points the node at a contract with a different genesis archive root. Remove
  custom overrides and let the node use the canonical addresses for the network,
  or set them to the current canonical addresses from `networks.md`.

- **Wrong L1 network.** The node must be pointed at the L1 chain that hosts your
  target network's rollup. If your `ETHEREUM_HOSTS` / L1 RPC points at a
  different L1 chain than the one the network is deployed on, it will read a
  different (or non-existent) rollup contract.

In practice these are far less common than a version skew — start with the
version check above.

## See also

- The networks page (`networks.md`) — the canonical, current version and L1
  contract addresses for each live network.
- The operator FAQ and "running a node" guides for general sync and snapshot
  guidance.
