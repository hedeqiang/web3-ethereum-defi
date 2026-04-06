"""Inspect and reset the Webshare proxy failure state.

Displays the current blocked proxies and optionally resets them all.

Usage:

.. code-block:: shell

    poetry run python scripts/erc-4626/reset-proxy-state.py
"""

from tabulate import tabulate

from eth_defi.event_reader.webshare import DEFAULT_PROXY_STATE_PATH, ProxyStateManager


def main() -> None:
    """Show blocked proxies and offer to reset."""

    state = ProxyStateManager(state_path=DEFAULT_PROXY_STATE_PATH)
    state.load()

    entries = state._failed_proxies
    if not entries:
        print(f"No blocked proxies in {DEFAULT_PROXY_STATE_PATH}")
        return

    rows = [
        [
            proxy_id,
            entry.failed_at.isoformat(sep=" ", timespec="seconds"),
            entry.reason[:80],
            entry.failure_count,
        ]
        for proxy_id, entry in sorted(entries.items(), key=lambda x: x[1].failed_at, reverse=True)
    ]

    print(tabulate(rows, headers=["Proxy ID", "Failed at", "Reason", "Count"], tablefmt="fancy_grid"))
    print(f"\nTotal blocked: {len(entries)}")
    print(f"State file: {DEFAULT_PROXY_STATE_PATH}")

    answer = input("\nReset all blocked proxies? [y/N] ").strip().lower()
    if answer == "y":
        DEFAULT_PROXY_STATE_PATH.unlink()
        print("Proxy state reset.")
    else:
        print("No changes made.")


if __name__ == "__main__":
    main()
