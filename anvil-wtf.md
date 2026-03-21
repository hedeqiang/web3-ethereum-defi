# Anvil WTF

This note captures what we learned while debugging Anvil timeouts on a HyperEVM fork during a `trade-executor` simulation run.

## Symptom

Anvil starts successfully, forks HyperEVM successfully, and the deployment progresses well past initial bootstrap. The interesting failure happens later, when local JSON-RPC calls to Anvil start timing out.

From the observed logs:

- Anvil starts against a local upstream failover proxy.
- The fork is pinned to HyperEVM block `30356898`.
- Bootstrap requests such as `eth_getBalance`, `eth_getCode`, `eth_getTransactionCount` and `eth_getBlockByNumber(..., false)` succeed.
- The suspicious later request is `eth_getBlockByNumber(<fork_block>, true)`.

This is important because the method is **not** `eth_getBlockNumber(true)`. The `true` flag belongs to `eth_getBlockByNumber` and means "return full transaction objects, not only hashes".

## Working hypothesis

The timeout is not primarily an Anvil startup problem.

The best current explanation is:

1. Anvil starts correctly and the upstream proxy is working.
2. Later, Anvil needs to lazily hydrate more fork state from upstream.
3. One of those lazy reads is a full-block fetch via `eth_getBlockByNumber(..., true)`.
4. That call is much heavier than the earlier header-only request.
5. While Anvil is blocked on upstream fork retrieval, local requests to Anvil can queue behind it and look like a deadlock.

So the behaviour may be:

- not a crash,
- not a startup genesis failure,
- but Anvil getting stuck in a slow or wedged fork-data fetch path.

## Why `eth_getBlockByNumber(..., true)` matters

There are two very different request shapes:

- `eth_getBlockByNumber(block, false)` returns the block with transaction hashes only
- `eth_getBlockByNumber(block, true)` returns the block with full transaction objects

The `true` form can be dramatically larger and slower, especially on a busy block or when the upstream provider is rate-limited or slow to stream large JSON payloads.

In our own codebase, the one obvious explicit caller is [eth_defi/foundry/forge.py](./eth_defi/foundry/forge.py), where `_find_deploy_tx_hash()` scans recent blocks with:

```python
block = web3.eth.get_block(block_num, full_transactions=True)
```

However, this path is unlikely to explain the Hyperliquid Lagoon deployment hang we observed:

- [eth_defi/erc_4626/vault_protocol/lagoon/deployment.py](./eth_defi/erc_4626/vault_protocol/lagoon/deployment.py) forces `use_forge=False` for `TradingStrategyModuleV0`
- the non-Forge deployment path is used specifically because Forge cannot handle the required dynamic library linking

That makes it more likely that the `eth_getBlockByNumber(..., true)` request in the logs is coming from **Anvil itself**, not from our Python deployment code.

## Relevant local code

### Anvil launcher

Anvil is launched from [eth_defi/provider/anvil.py](./eth_defi/provider/anvil.py).

Important details:

- `launch_anvil()` supports forking, block pinning, verbose mode and the upstream proxy
- `_launch()` sets `RUST_BACKTRACE=1`
- by default, subprocess stdout/stderr used to be captured in pipes

We added `inherit_stdio=True` support because captured pipes can become their own failure mode in Docker:

- if Anvil is verbose enough,
- and nothing is draining its stdout/stderr pipes live,
- the pipe buffer can fill,
- which can stall the subprocess

This was a separate but very real observability and liveness problem.

### Timeout budgets in the stack

The timeout shape matters because a slow upstream call can easily look like an Anvil deadlock:

- the downstream local Web3 client was using roughly `(3s connect, 90s read)`
- the RPC failover proxy defaulted to `30s` timeout per upstream attempt with `3` retries

That means one slow Anvil upstream read can consume about 90 seconds before the caller gives up.

## What Anvil source code suggests

We looked at the public Foundry Anvil source documentation and issue tracker.

Useful source docs:

- `EthApi::block_by_number_full()`:
  https://foundry-rs.github.io/foundry/anvil/eth/api/struct.EthApi.html
- backend implementation:
  https://foundry-rs.github.io/foundry/src/anvil/eth/backend/mem/mod.rs.html

The important takeaway from the source is:

- `eth_getBlockByNumber(..., true)` goes through Anvil's full-block path
- in fork mode, if the block is not already fully materialised locally, Anvil may ask the fork client for the full block
- the backend comments explicitly indicate that fork-backed operations can block while data is retrieved remotely

There is also a local/full representation distinction in the backend:

- hash-only block handling is cheap
- full block handling reconstructs `BlockTransactions::Full(...)`

This matches the operational symptom: an inexpensive header read may succeed during startup, while a later full-block fetch stalls.

## Issue tracker references

These issues are not exact matches, but they show the same family of problems in Anvil fork mode:

- Issue `#7966`: first forked call hanging after `anvil_reset`
  https://github.com/foundry-rs/foundry/issues/7966
- Issue `#1920`: Anvil freezing on a forked network
  https://github.com/foundry-rs/foundry/issues/1920
- Issue `#5810`: extreme slowness on forked-block interactions
  https://github.com/foundry-rs/foundry/issues/5810
- Issue `#2686`: slow forwarding of pre-fork tracing requests upstream
  https://github.com/foundry-rs/foundry/issues/2686

The pattern across these reports is consistent:

- fork mode can be very sensitive to upstream node behaviour
- some requests trigger much heavier remote reads than expected
- when Anvil is waiting on those reads, the local node can look frozen

## What we changed to improve observability

We added live Anvil log passthrough support in [eth_defi/provider/anvil.py](./eth_defi/provider/anvil.py):

- `verbose=True` for chatty Anvil output
- `inherit_stdio=True` so Anvil writes directly to the parent stdout/stderr instead of only to undrained pipes

This is particularly useful in Docker, where we want Anvil logs to appear in container logs in real time.

In the downstream `trade-executor` wiring, this is exposed as:

- `ANVIL_VERBOSE=true`
- `ANVIL_INHERIT_STDIO=true`
- `RPC_PROXY_VERBOSE=true`

That setup should let us see whether:

- Anvil is actively logging while the system is "hung"
- the RPC proxy sees the upstream request and never gets a response
- or Anvil stops making upstream requests and wedges internally

## What we changed to mitigate it

We added an explicit Anvil warm-up path in [eth_defi/provider/anvil.py](./eth_defi/provider/anvil.py).

The new `launch_anvil(..., warm_up_block=False)` option:

- waits until Anvil is responsive
- determines the effective fork block
- immediately calls `eth_getBlockByNumber(fork_block, true)` once against the **local** Anvil instance

Implementation notes:

- the helper lives in [eth_defi/provider/anvil.py](./eth_defi/provider/anvil.py)
- the warm-up request is made by `_warm_up_fork_block()`
- if warm-up fails, Anvil startup is treated as failed and the launcher retries or raises

This does **not** remove the expensive RPC read. Instead, it front-loads it:

- before: Anvil may hang later during a random deployment step
- after: Anvil pays the cost once during startup, before the caller starts using it

In `trade-executor`, this is exposed through:

- `ANVIL_WARM_UP_BLOCK=true`

So the recommended Docker debugging setup is now:

```shell
SIMULATE=true \
RPC_PROXY_VERBOSE=true \
ANVIL_VERBOSE=true \
ANVIL_INHERIT_STDIO=true \
ANVIL_WARM_UP_BLOCK=true \
deploy/deploy-hyper-ai.sh
```

## What we verified with a live test

We added a focused HyperEVM integration test in [tests/rpc/test_anvil_hyperliquid_warmup.py](./tests/rpc/test_anvil_hyperliquid_warmup.py).

The test:

- launches a live HyperEVM fork with `warm_up_block=True`
- lets Anvil perform the eager full-block fetch during startup
- immediately fetches the same full block again via `web3.eth.get_block(current_block, full_transactions=True)`

Observed result from a real run:

- HyperEVM tip was `30359549`
- Anvil auto-pinned the fork to block `30359545`
- warm-up completed during startup for block `30359545`
- the warmed block had `0` transactions
- the post-start full-block read completed in `0.013` seconds
- total test setup time was about `4.20` seconds

The RPC proxy statistics were especially interesting:

- upstream saw only `2` `eth_getBlockByNumber` requests in total
- one was the initial cheap header-style fork startup request
- one was the eager warm-up full-block request
- the later test call appears to have been served locally by Anvil, not forwarded upstream again

This is the strongest evidence so far that the mitigation works as intended:

- the expensive full-block hydration can be triggered once at startup
- the same later call then becomes cheap and local

## Most likely explanation today

The current best explanation is:

- Anvil lazily requests `eth_getBlockByNumber(fork_block, true)` from the upstream RPC
- the upstream response is heavy, slow, rate-limited, or otherwise problematic
- Anvil blocks waiting for that data
- other local JSON-RPC requests pile up behind it
- the caller sees timeouts and the node looks deadlocked

This is still partly an inference, but it is consistent with:

- the runtime logs,
- our own deployment code paths,
- Anvil's documented fork backend behaviour,
- and the existing upstream issue reports.

## Practical mitigation summary

The current mitigation strategy is:

1. Keep HyperEVM forks pinned away from the chain tip.
2. Use the local multi-upstream RPC proxy so Anvil has retries, timeouts and failover.
3. Send Anvil logs directly to container stdout/stderr.
4. Warm up the fork block once at startup with `ANVIL_WARM_UP_BLOCK=true`.

This is not a perfect fix, because Anvil still performs the heavy full-block fetch. But it changes the failure mode from:

- "deployment randomly wedges later"

to:

- "startup pays the hydration cost once, up front, in a visible and controlled place"

That is a much better operational shape.

## Next debugging steps

If this happens again, the most useful setup is:

```shell
SIMULATE=true \
RPC_PROXY_VERBOSE=true \
ANVIL_VERBOSE=true \
ANVIL_INHERIT_STDIO=true \
ANVIL_WARM_UP_BLOCK=true \
deploy/deploy-hyper-ai.sh
```

Things to watch for:

- a proxy log line showing `eth_getBlockByNumber(..., true)` without a matching response for a long time
- Anvil logging that stops exactly when the request is made
- whether the stall disappears when using a different upstream provider
- whether the stall disappears when avoiding the code path that needs full block bodies

## Open questions

- Does Anvil hold a lock across the remote `block_by_number_full()` fork fetch path?
- Is HyperEVM especially sensitive here because of its node/provider behaviour?
- Is the slow part the full block body fetch itself, or a follow-on state hydration triggered by that block?
- Can the proxy safely impose a lower timeout for `eth_getBlockByNumber(..., true)` without breaking legitimate calls?

## Short conclusion

The failure mode looks like a fork-mode Anvil runtime stall, not a plain upstream startup issue.

The strongest trigger we found is `eth_getBlockByNumber(..., true)`, because full-block retrieval is substantially more expensive than header-only reads and appears to line up with Anvil's lazy fork hydration path.

Our current mitigation is to make Anvil do that expensive read once, explicitly, during startup. The live HyperEVM warm-up test strongly suggests this works: after eager warm-up, the same full-block read was served locally in `0.013` seconds instead of becoming a surprise runtime stall.
