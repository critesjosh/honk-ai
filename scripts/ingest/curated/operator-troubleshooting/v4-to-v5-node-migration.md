---
title: "Migrating an Aztec node from v4 to v5 (mainnet v4.3.1 → testnet/v5 line)"
---

# Migrating an Aztec node across a major version (v4 → v5)

This entry covers what is **documented** about moving an Aztec node across a major
protocol version — specifically the v4 line (mainnet, v4.3.1) and the v5 line
(testnet, v5.0.0-rc.2). It captures the node-level migration mechanics and, just
as importantly, **marks the operational questions the migration guidance does NOT
answer** so those are not guessed at. The authoritative, living source is the
Aztec forum's **v5 node migration guide** and the related upgrade proposal; defer
version-pinned commands and any timing/slashing specifics to them and to the
official release notes.

## Current testnet version: v5.0.0-rc.2

As of **2026-06-30 12:00 UTC**, testnet runs **v5.0.0-rc.2**, which **supersedes
v5.0.0-rc.1** (install with `aztec-up install 5.0.0-rc.2`; Docker image
`aztecprotocol/aztec:5.0.0-rc.2`). rc.2 rolls up fixes and hardening found while
running rc.1 on testnet — including that a node now **auto-shuts-down on an
incompatible canonical rollup upgrade**. These v5 docs are the rc.2 snapshot;
still confirm any version-pinned command against the current v5 release notes, as
further v5 RCs may follow. Full v5 breaking changes will be published with the
first stable v5 release.

## The one hard rule: v4 and v5 nodes cannot share a network

A major protocol version bump means **v4 and v5 nodes cannot participate in the
same network** — the protocol major version has changed, so all nodes on a network
upgrade together. You do not run a v4 node and a v5 node against the same rollup at
the same time.

## Deploy-ahead + automatic standby (node activation only)

You **can** deploy a v5 node before the network's v5 upgrade is canonical:

- If you start a v5 node against a rollup that is still on v4, the node **detects
  the mismatch and enters standby mode automatically**, consuming minimal
  resources. This is expected — **no action is needed**.
- Once the v5 rollup becomes **canonical on L1**, the standby node **exits standby
  and starts syncing without manual intervention**.

So the documented node-activation path is: bring up the v5 node ahead of time, let
it wait in standby, and let it start itself when the upgrade lands — you do not
have to manually time a node restart to the exact canonical-switch moment for the
node to pick up the new rollup. **This is a node-activation convenience, NOT a
downtime or anti-slashing guarantee for a sequencer carrying active delegations.**
Whether a delegated sequencer misses attestations (and any slashing exposure)
during the transition is a separate question the migration guidance does not
answer — see "What the migration guidance does NOT specify" below.

## The mismatch errors you may see (and what they mean)

- **Consensus / genesis mismatch (node enters standby):** a node built for one
  version pointed at a network on another logs a *genesis archive root* mismatch
  ("Rollup at 0x… is incompatible: genesis archive root (expected 0x…, got 0x…).
  Entering standby mode."). This is the same standby safety-stop described in the
  genesis-archive-root-mismatch entry — it means the node's compiled-in version
  does not match the L1 rollup, which is exactly the expected state for a v5 node
  waiting on a v4 network. Fix by matching the node version to the network (or, for
  a deploy-ahead v5 node, simply wait for the upgrade).
- **HA database schema mismatch (high-availability deployments):** an out-of-date
  HA database logs "Database schema version 1 is outdated (expected 2). Please run
  migrations: aztec migrate-ha-db up --database-url <url>". Run the stated
  migration command against your HA database URL. (Quote the exact command from the
  official guide; do not improvise flags.)

## What the migration guidance does NOT specify — do not invent it

The node-migration guidance covers the node-level mechanics above. It does **not**
provide, and you must not fabricate:

- **Coordinated downtime windows** or a required manual cutover procedure.
- **Staking-provider / validator transition procedures** for nodes that carry
  active delegations.
- **Slashing risk during the transition**, or whether a provider in the committee
  during the canonical switch can miss attestations.
- **Epoch-relative timing** ("restart within N minutes of the switch").

If a user asks about slashing exposure, downtime for a delegated sequencer, or
exact timing relative to epochs during the upgrade, the honest and correct answer
is that the migration guide does **not** specify it: point them to the official
Aztec forum v5 node-migration guide and the related upgrade proposal, and to the
Aztec team, rather than estimating a slashing rule or downtime window. Do not state
a slashing threshold, an epoch length, or a "recommended" timing that is not in a
retrieved chunk.
