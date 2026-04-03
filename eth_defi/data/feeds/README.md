# Feed sources

YAML feeder files for collecting posts from vault protocols, curators, and stablecoins.

## Structure

```
eth_defi/data/feeds/
  protocols/    — vault protocols (Morpho, Euler, Lagoon, etc.)
  curators/     — vault curators (Gauntlet, Steakhouse, RE7 Labs, etc.)
  stablecoins/  — stablecoin issuers (USDC, USDT, DAI, etc.)
```

## Schema

Each YAML file follows the unified feeder schema described in
[`eth_defi/feed/README-feed.md`](../../feed/README-feed.md):

```yaml
feeder-id: {slug}
name: {human-readable name}
role: {curator | protocol | stablecoin}
website: {optional company website URL}
twitter: {optional Twitter/X username}
linkedin: {optional LinkedIn company id}
rss: {optional RSS or Atom feed URL}
```

At least one of `twitter`, `linkedin`, or `rss` must be present.

## Progress

See [`tracking.md`](./tracking.md) for the full list of entities and their
creation status.
