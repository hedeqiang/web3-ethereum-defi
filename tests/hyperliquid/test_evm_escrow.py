from types import SimpleNamespace

import pytest

from eth_defi.erc_4626.vault_protocol.lagoon.deployment import should_enable_hypercore_guard
from eth_defi.hyperliquid.evm_escrow import _assert_activation_guard_config


class _FakeCall:
    def __init__(self, value: bool):
        self.value = value

    def call(self) -> bool:
        return self.value


class _FakeFunctions:
    def __init__(self, *, approval_allowed: bool, receiver_allowed: bool):
        self.approval_allowed = approval_allowed
        self.receiver_allowed = receiver_allowed

    def isAllowedApprovalDestination(self, _address):
        return _FakeCall(self.approval_allowed)

    def isAllowedReceiver(self, _address):
        return _FakeCall(self.receiver_allowed)


def _make_vault(*, approval_allowed: bool, receiver_allowed: bool):
    module = SimpleNamespace(
        address="0xdA1262A20Ed853Fa3BbA16e079Bbe2d1e0728d2f",
        functions=_FakeFunctions(
            approval_allowed=approval_allowed,
            receiver_allowed=receiver_allowed,
        ),
    )
    return SimpleNamespace(
        safe_address="0x49Be988d2090aa221586e9A51cacBA3D3A1eA087",
        trading_strategy_module=module,
    )


def test_should_enable_hypercore_guard_for_any_asset_on_hyperevm():
    assert should_enable_hypercore_guard(
        chain_id=999,
        any_asset=True,
        hypercore_vaults=None,
    )


def test_should_not_enable_hypercore_guard_off_hyperevm_without_vaults():
    assert not should_enable_hypercore_guard(
        chain_id=1,
        any_asset=True,
        hypercore_vaults=None,
    )


def test_activation_guard_check_rejects_missing_core_deposit_wallet_approval():
    vault = _make_vault(approval_allowed=False, receiver_allowed=True)

    with pytest.raises(RuntimeError, match="does not allow approving CoreDepositWallet"):
        _assert_activation_guard_config(
            vault,
            "0x6B9E773128f453f5c2C60935Ee2DE2CBc5390A24",
        )


def test_activation_guard_check_accepts_whitelisted_hypercore_setup():
    vault = _make_vault(approval_allowed=True, receiver_allowed=True)

    _assert_activation_guard_config(
        vault,
        "0x6B9E773128f453f5c2C60935Ee2DE2CBc5390A24",
    )
