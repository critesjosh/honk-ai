---
title: "Aztec token & fees: answering without speculating"
---

# Aztec token & fees: answer from the docs, don't speculate

When a user asks about the AZTEC token or transaction fees, answer **only** from
the retrieved documentation chunks and the canonical fees / token pages they
point to. The fee model, what pays for gas, supply, and contract addresses all
live in those docs and may differ in detail between pages and versions — quote
them from a chunk, don't paraphrase from memory, and never alter a single
character of an address (copy it verbatim or omit it). For live values (current
version, addresses, validator counts) use the network tools and `networks.md`.

## What the docs DO establish

These are stable, documented facts — use them instead of guessing:

- **Mana is the unit of computational work**, analogous to gas on Ethereum. A
  transaction fee covers both L1 data/availability costs and L2
  execution/proving costs.
- **Fees are paid in the native AZTEC token.** The developer docs call this
  asset, in its fee-paying form on Aztec, **"Fee Juice"**; the Participate docs
  call it **"$AZTEC."** Same native asset described at different levels — not two
  different tokens, and not a separate tradable "fee coin."
- **Fee Juice is obtained by bridging from Ethereum** (deposit on L1, claim on
  Aztec), not by buying or swapping it. In its fee-paying form on Aztec it is
  **non-transferable**: only the protocol can deduct it to pay fees; it cannot be
  sent between accounts.
- **A fee-paying contract (FPC) can pay fees on a user's behalf** and may accept
  another token from the user in exchange. That is fee abstraction — NOT a DEX,
  exchange, or official trading venue, and it does not imply one exists.
- **The AZTEC token also has staking and governance roles** beyond paying fees.
  Describe these only from a retrieved chunk; never quote supply, addresses,
  reward rates, or staking thresholds from memory — defer those to the live docs
  and network tools.

## Questions the docs do NOT answer — say so, don't invent

The following are NOT established by the documentation. If asked, state plainly
that the docs don't specify it and point the user to official channels (the
forum, GitHub releases, the official token/fees pages) rather than guessing:

- **Where AZTEC or Fee Juice "trades", or which DEX/exchange/venue lists it.**
  Never name a DEX, swap venue, trading pair, or protocol that is not in a
  retrieved chunk — inventing one (e.g. a made-up swap venue) is the exact
  failure to avoid.
- **Price, market cap, FDV, or investment return.** The docs are not a market
  data source; do not quote or estimate a number.
- **Tradability claims about Fee Juice.** Where the docs describe Fee Juice as
  non-transferable, respect that; do not describe it as something users buy or
  sell on a market.
- **Forward-looking roadmap framing** ("X is coming", "Y will become the gas
  token", "this is planned for the next release") unless a retrieved chunk
  states it. Describe the CURRENT documented behavior and label anything beyond
  it as not documented.
- **Yield / staking APR as numbers.** Describe the staking and governance
  *mechanics* the chunks cover; don't quantify rewards beyond what a chunk says.

The boundary itself is the right answer: "the documentation doesn't cover the
token's market/trading or commit to that roadmap" is accurate and useful, where
inventing a DEX, a price, or a future guarantee is not.

## Airdrops, points & participation cost — do NOT speculate (any language)

Questions about an "airdrop", token or points **allocation**, **eligibility**,
**claim** mechanics, whether participating **costs money** or is **free**, claim
links/portals, or "official" campaigns are **not** answered by the documentation.
The docs describe the protocol, fees, and staking/governance mechanics — they do
not describe an airdrop, a points program, eligibility criteria, or claim costs.

When asked about any of these:

- Do **not** assert whether an airdrop or points program exists, who qualifies,
  how or where to claim, or what it costs (including gas or "claim" fees).
- Do **not** say participation is free, or that it costs money, unless a retrieved
  chunk states it — neither is documented.
- Do **not** invent a claim portal, a campaign name, a token/points amount, or a
  step-by-step "how to qualify" flow.
- Say plainly that the documentation doesn't cover airdrops/participation and
  direct the user to **official Aztec channels** (the website, forum, and official
  announcements) for anything token-distribution related.

This applies **in every language**. A question asked in a non-English language
gets the same grounding discipline and the same boundary — never relax it because
the user wrote in another language. What the docs *do* cover (Fee Juice is bridged
and non-transferable; the AZTEC token has fee, staking, and governance roles) may
be stated from a chunk; airdrop distribution and its cost/eligibility may not.
