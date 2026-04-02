"""Interactive auction token enablement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from eth_utils import to_checksum_address
from sqlalchemy import select
from web3 import Web3

from tidal.chain.contracts.abis import (
    AUCTION_ABI,
    AUCTION_FACTORY_ABI,
    AUCTION_KICKER_ABI,
    ERC20_ABI,
    FEE_BURNER_ABI,
    STRATEGY_ABI,
)
from tidal.config import MonitoredFeeBurner
from tidal.constants import CORE_REWARD_TOKENS, YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS, ZERO_ADDRESS
from tidal.normalizers import normalize_address, short_address, to_decimal_string
from tidal.persistence import models
from tidal.persistence.db import Database
from tidal.transaction_service.signer import TransactionSigner


@dataclass(slots=True)
class AuctionInspection:
    auction_address: str
    governance: str
    want: str
    receiver: str
    version: str | None
    in_configured_factory: bool
    governance_matches_required: bool
    enabled_tokens: tuple[str, ...]


@dataclass(slots=True)
class SourceResolution:
    source_type: str
    source_address: str
    source_name: str | None
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class TokenDiscovery:
    tokens_by_address: dict[str, set[str]]
    notes: list[str]


@dataclass(slots=True)
class TokenProbe:
    token_address: str
    origins: tuple[str, ...]
    symbol: str | None
    decimals: int | None
    raw_balance: int | None
    normalized_balance: str | None
    status: str
    reason: str
    detail: str | None = None

    @property
    def display_label(self) -> str:
        symbol = self.symbol or "UNKNOWN"
        return f"{symbol} ({to_checksum_address(self.token_address)})"


@dataclass(slots=True)
class EnableExecutionPlan:
    to_address: str
    data: str
    call_succeeded: bool
    gas_estimate: int | None
    error_message: str | None = None
    sender_authorized: bool | None = None
    authorization_target: str | None = None


def format_probe_reason(reason: str) -> str:
    return reason.replace("_", " ")


def parse_manual_token_input(raw: str) -> list[str]:
    output: list[str] = []
    for chunk in raw.replace("\n", ",").split(","):
        value = chunk.strip()
        if not value:
            continue
        output.append(normalize_address(value))
    return output


def resolve_source_type(
    *,
    receiver: str,
    auction_want: str,
    monitored_fee_burners: list[MonitoredFeeBurner],
    strategy_want: str | None,
    strategy_name: str | None = None,
) -> SourceResolution:
    receiver = normalize_address(receiver)
    auction_want = normalize_address(auction_want)

    for fee_burner in monitored_fee_burners:
        burner_address = normalize_address(fee_burner.address)
        if burner_address != receiver:
            continue

        warnings: list[str] = []
        expected_want = normalize_address(fee_burner.want_address)
        if expected_want != auction_want:
            warnings.append(
                f"configured fee burner want is {to_checksum_address(expected_want)}, "
                f"but auction want is {to_checksum_address(auction_want)}"
            )

        return SourceResolution(
            source_type="fee_burner",
            source_address=receiver,
            source_name=fee_burner.label or "Fee Burner",
            warnings=tuple(warnings),
        )

    if strategy_want and normalize_address(strategy_want) == auction_want:
        return SourceResolution(
            source_type="strategy",
            source_address=receiver,
            source_name=strategy_name,
        )

    raise RuntimeError(
        "receiver is neither a configured fee burner nor a strategy whose want matches the auction"
    )


class AuctionTokenEnabler:
    """Loads auction metadata, discovers candidate tokens, and prepares enable txs."""

    def __init__(self, w3: Web3, settings) -> None:  # noqa: ANN001
        self.w3 = w3
        self.settings = settings
        self.required_trade_handler = normalize_address(YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS)

    def inspect_auction(self, auction_address: str) -> AuctionInspection:
        auction_address = normalize_address(auction_address)
        auction = self.w3.eth.contract(
            address=to_checksum_address(auction_address),
            abi=AUCTION_ABI,
        )

        try:
            governance = normalize_address(auction.functions.governance().call())
            want = normalize_address(auction.functions.want().call())
            receiver = normalize_address(auction.functions.receiver().call())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"failed to load auction metadata for {auction_address}: {exc}") from exc

        if want == ZERO_ADDRESS:
            raise RuntimeError("auction want is zero address")
        if receiver == ZERO_ADDRESS:
            raise RuntimeError("auction receiver is zero address")

        version: str | None
        try:
            raw_version = auction.functions.version().call()
            version = str(raw_version).strip() or None
        except Exception:  # noqa: BLE001
            version = None

        enabled_tokens: tuple[str, ...]
        try:
            enabled_tokens = tuple(
                normalize_address(address)
                for address in auction.functions.getAllEnabledAuctions().call()
            )
        except Exception:  # noqa: BLE001
            enabled_tokens = ()

        in_configured_factory = False
        try:
            factory = self.w3.eth.contract(
                address=to_checksum_address(normalize_address(self.settings.auction_factory_address)),
                abi=AUCTION_FACTORY_ABI,
            )
            all_auctions = {
                normalize_address(address)
                for address in factory.functions.getAllAuctions().call()
            }
            in_configured_factory = auction_address in all_auctions
        except Exception:  # noqa: BLE001
            in_configured_factory = False

        return AuctionInspection(
            auction_address=auction_address,
            governance=governance,
            want=want,
            receiver=receiver,
            version=version,
            in_configured_factory=in_configured_factory,
            governance_matches_required=governance == self.required_trade_handler,
            enabled_tokens=enabled_tokens,
        )

    def resolve_source(self, inspection: AuctionInspection) -> SourceResolution:
        strategy_want = self._read_strategy_want(inspection.receiver)
        strategy_name = self._read_strategy_name(inspection.receiver)
        return resolve_source_type(
            receiver=inspection.receiver,
            auction_want=inspection.want,
            monitored_fee_burners=list(self.settings.monitored_fee_burners),
            strategy_want=strategy_want,
            strategy_name=strategy_name,
        )

    def discover_tokens(
        self,
        *,
        inspection: AuctionInspection,
        source: SourceResolution,
        manual_tokens: list[str] | None = None,
    ) -> TokenDiscovery:
        tokens_by_address: dict[str, set[str]] = {}
        notes: list[str] = []

        def add_token(token_address: str, origin: str) -> None:
            token_address = normalize_address(token_address)
            tokens_by_address.setdefault(token_address, set()).add(origin)

        if source.source_type == "fee_burner":
            burner = self.w3.eth.contract(
                address=to_checksum_address(source.source_address),
                abi=FEE_BURNER_ABI,
            )
            spender = to_checksum_address(inspection.governance)
            try:
                allowed = bool(burner.functions.isTokenSpender(spender).call())
            except Exception as exc:  # noqa: BLE001
                notes.append(f"failed to verify token spender permissions: {exc}")
                allowed = False

            if not allowed:
                notes.append(
                    f"{short_address(inspection.governance)} is not an allowed fee burner token spender"
                )
            else:
                try:
                    approvals = burner.functions.getApprovals(spender).call()
                    for token_address in approvals:
                        add_token(token_address, "trade_handler_approval")
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"failed to read fee burner approvals: {exc}")
        else:
            for token_address in CORE_REWARD_TOKENS:
                add_token(token_address, "core_reward_token")

            rewards = self._read_strategy_rewards_tokens(source.source_address)
            for token_address in rewards:
                add_token(token_address, "strategy_rewards")

        for token_address in self._load_historical_tokens(
            source_type=source.source_type,
            source_address=source.source_address,
        ):
            add_token(token_address, "local_db")

        for token_address in manual_tokens or []:
            add_token(token_address, "manual")

        return TokenDiscovery(tokens_by_address=tokens_by_address, notes=notes)

    def probe_tokens(
        self,
        *,
        inspection: AuctionInspection,
        source: SourceResolution,
        discovery: TokenDiscovery,
    ) -> list[TokenProbe]:
        probes: list[TokenProbe] = []
        enabled_tokens = set(inspection.enabled_tokens)

        for token_address in sorted(discovery.tokens_by_address):
            origins = tuple(sorted(discovery.tokens_by_address[token_address]))
            symbol = self._read_token_symbol(token_address)

            if token_address == inspection.want:
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=None,
                        raw_balance=None,
                        normalized_balance=None,
                        status="skip",
                        reason="token_is_want",
                    )
                )
                continue

            if token_address in enabled_tokens:
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=None,
                        raw_balance=None,
                        normalized_balance=None,
                        status="skip",
                        reason="already_enabled",
                    )
                )
                continue

            try:
                decimals = self._read_token_decimals(token_address)
            except Exception as exc:  # noqa: BLE001
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=None,
                        raw_balance=None,
                        normalized_balance=None,
                        status="skip",
                        reason="decimals_call_failed",
                        detail=str(exc),
                    )
                )
                continue

            if decimals > 18:
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=decimals,
                        raw_balance=None,
                        normalized_balance=None,
                        status="skip",
                        reason="unsupported_decimals",
                    )
                )
                continue

            try:
                raw_balance = self._read_token_balance(token_address, source.source_address)
            except Exception as exc:  # noqa: BLE001
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=decimals,
                        raw_balance=None,
                        normalized_balance=None,
                        status="skip",
                        reason="balance_call_failed",
                        detail=str(exc),
                    )
                )
                continue

            normalized_balance = to_decimal_string(raw_balance, decimals)
            if raw_balance == 0:
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=decimals,
                        raw_balance=raw_balance,
                        normalized_balance=normalized_balance,
                        status="skip",
                        reason="zero_balance",
                    )
                )
                continue

            try:
                self._auction_contract(inspection.auction_address).functions.enable(
                    to_checksum_address(token_address)
                ).call({"from": to_checksum_address(inspection.governance)})
            except Exception as exc:  # noqa: BLE001
                probes.append(
                    TokenProbe(
                        token_address=token_address,
                        origins=origins,
                        symbol=symbol,
                        decimals=decimals,
                        raw_balance=raw_balance,
                        normalized_balance=normalized_balance,
                        status="skip",
                        reason="enable_call_failed",
                        detail=str(exc),
                    )
                )
                continue

            probes.append(
                TokenProbe(
                    token_address=token_address,
                    origins=origins,
                    symbol=symbol,
                    decimals=decimals,
                    raw_balance=raw_balance,
                    normalized_balance=normalized_balance,
                    status="eligible",
                    reason="eligible",
                )
            )

        return probes

    def build_execution_plan(
        self,
        *,
        inspection: AuctionInspection,
        tokens: list[str],
        caller_address: str | None,
    ) -> EnableExecutionPlan:
        if not inspection.governance_matches_required:
            raise RuntimeError(
                "auction governance does not match the configured Yearn trade handler; "
                "enable-tokens only supports standard Yearn auctions via AuctionKicker"
            )
        kicker_address = self._require_auction_kicker_address()
        enable_fn = self._enable_tokens_function(
            kicker_address=kicker_address,
            inspection=inspection,
            tokens=tokens,
        )

        if not caller_address:
            return EnableExecutionPlan(
                to_address=kicker_address,
                data=enable_fn._encode_transaction_data(),
                call_succeeded=False,
                gas_estimate=None,
                error_message="no caller address provided for enableTokens() preview",
                sender_authorized=None,
                authorization_target=kicker_address,
            )

        caller_address = normalize_address(caller_address)
        sender_authorized = self.is_authorized_kicker(kicker_address, caller_address)

        try:
            enable_fn.call({"from": to_checksum_address(caller_address)})
            call_succeeded = True
            error_message = None
        except Exception as exc:  # noqa: BLE001
            call_succeeded = False
            error_message = str(exc)

        try:
            tx = enable_fn.build_transaction({"from": to_checksum_address(caller_address)})
            gas_estimate = int(self.w3.eth.estimate_gas(tx))
        except Exception as exc:  # noqa: BLE001
            gas_estimate = None
            if error_message is None:
                error_message = str(exc)

        return EnableExecutionPlan(
            to_address=kicker_address,
            data=enable_fn._encode_transaction_data(),
            call_succeeded=call_succeeded,
            gas_estimate=gas_estimate,
            error_message=error_message,
            sender_authorized=sender_authorized,
            authorization_target=kicker_address,
        )

    def is_authorized_kicker(self, kicker_address: str, caller_address: str) -> bool:
        kicker = self._auction_kicker_contract(kicker_address)
        checksum_caller = to_checksum_address(normalize_address(caller_address))
        owner = normalize_address(kicker.functions.owner().call())
        if normalize_address(owner) == normalize_address(caller_address):
            return True
        return bool(kicker.functions.keeper(checksum_caller).call())

    def send_enable_transaction(
        self,
        *,
        signer: TransactionSigner,
        inspection: AuctionInspection,
        tokens: list[str],
    ) -> tuple[str, int]:
        kicker_address = self._require_auction_kicker_address()
        if not inspection.governance_matches_required:
            raise RuntimeError(
                "auction governance does not match the configured Yearn trade handler; "
                "enable-tokens only supports standard Yearn auctions via AuctionKicker"
            )
        if not self.is_authorized_kicker(kicker_address, signer.address):
            raise RuntimeError(
                f"{to_checksum_address(signer.address)} is not an authorized keeper on "
                f"{to_checksum_address(kicker_address)}"
            )

        enable_fn = self._enable_tokens_function(
            kicker_address=kicker_address,
            inspection=inspection,
            tokens=tokens,
        )

        latest_block = self.w3.eth.get_block("latest")
        base_fee = int(latest_block.get("baseFeePerGas") or 0)
        try:
            priority_fee = int(self.w3.eth.max_priority_fee)
        except Exception:  # noqa: BLE001
            priority_fee = self.w3.to_wei(1, "gwei")

        if base_fee > 0:
            max_fee = int(base_fee * 2 + priority_fee)
        else:
            max_fee = int(self.w3.eth.gas_price)
            priority_fee = 0

        tx = enable_fn.build_transaction(
            {
                "from": signer.checksum_address,
                "chainId": int(self.w3.eth.chain_id),
                "nonce": int(self.w3.eth.get_transaction_count(signer.checksum_address, "pending")),
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee,
            }
        )
        gas_estimate = int(self.w3.eth.estimate_gas(tx))
        tx["gas"] = int(gas_estimate * 1.2)

        signed_tx = signer.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx).hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        return tx_hash, gas_estimate

    def _auction_contract(self, auction_address: str):
        return self.w3.eth.contract(
            address=to_checksum_address(normalize_address(auction_address)),
            abi=AUCTION_ABI,
        )

    def _auction_kicker_contract(self, kicker_address: str):
        return self.w3.eth.contract(
            address=to_checksum_address(normalize_address(kicker_address)),
            abi=AUCTION_KICKER_ABI,
        )

    def _enable_tokens_function(
        self,
        *,
        kicker_address: str,
        inspection: AuctionInspection,
        tokens: list[str],
    ):
        checksum_tokens = [to_checksum_address(normalize_address(token)) for token in tokens]
        if not checksum_tokens:
            raise RuntimeError("no eligible tokens to enable")
        contract = self._auction_kicker_contract(kicker_address)
        return contract.functions.enableTokens(
            to_checksum_address(inspection.auction_address),
            checksum_tokens,
        )

    def _require_auction_kicker_address(self) -> str:
        raw_value = str(getattr(self.settings, "auction_kicker_address", "") or "").strip()
        if not raw_value:
            raise RuntimeError("auction_kicker_address is not configured")
        kicker_address = normalize_address(raw_value)
        if kicker_address == ZERO_ADDRESS:
            raise RuntimeError("auction_kicker_address is not configured")
        return kicker_address

    def _read_strategy_want(self, strategy_address: str) -> str | None:
        contract = self.w3.eth.contract(
            address=to_checksum_address(strategy_address),
            abi=STRATEGY_ABI,
        )
        try:
            return normalize_address(contract.functions.want().call())
        except Exception:  # noqa: BLE001
            return None

    def _read_strategy_name(self, strategy_address: str) -> str | None:
        contract = self.w3.eth.contract(
            address=to_checksum_address(strategy_address),
            abi=STRATEGY_ABI,
        )
        try:
            value = contract.functions.name().call()
        except Exception:  # noqa: BLE001
            return None
        if value is None:
            return None
        return str(value).strip() or None

    def _read_strategy_rewards_tokens(self, strategy_address: str) -> list[str]:
        contract = self.w3.eth.contract(
            address=to_checksum_address(strategy_address),
            abi=STRATEGY_ABI,
        )
        output: list[str] = []
        max_index = int(getattr(self.settings, "multicall_rewards_index_max", 16))
        for index in range(max_index):
            try:
                token_address = normalize_address(
                    contract.get_function_by_signature("rewardsTokens(uint256)")(index).call()
                )
            except Exception:  # noqa: BLE001
                break
            if token_address == ZERO_ADDRESS:
                break
            if token_address not in output:
                output.append(token_address)
        return output

    def _read_token_symbol(self, token_address: str) -> str | None:
        contract = self.w3.eth.contract(
            address=to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        try:
            value = contract.functions.symbol().call()
        except Exception:  # noqa: BLE001
            return None

        if isinstance(value, bytes):
            return value.rstrip(b"\x00").decode(errors="ignore") or None
        if hasattr(value, "hex"):
            try:
                raw = bytes(value).rstrip(b"\x00")
                return raw.decode(errors="ignore") or None
            except Exception:  # noqa: BLE001
                return None
        if value is None:
            return None
        return str(value).strip() or None

    def _read_token_decimals(self, token_address: str) -> int:
        contract = self.w3.eth.contract(
            address=to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        return int(contract.functions.decimals().call())

    def _read_token_balance(self, token_address: str, holder_address: str) -> int:
        contract = self.w3.eth.contract(
            address=to_checksum_address(token_address),
            abi=ERC20_ABI,
        )
        return int(contract.functions.balanceOf(to_checksum_address(holder_address)).call())

    def _load_historical_tokens(self, *, source_type: str, source_address: str) -> set[str]:
        db_path = Path(self.settings.resolved_db_path)
        if not db_path.is_file():
            return set()

        database = Database(self.settings.database_url)
        with database.session() as session:
            try:
                if source_type == "strategy":
                    statements = (
                        select(models.strategy_tokens.c.token_address).where(
                            models.strategy_tokens.c.strategy_address == source_address
                        ),
                        select(models.strategy_token_balances_latest.c.token_address).where(
                            models.strategy_token_balances_latest.c.strategy_address == source_address
                        ),
                    )
                else:
                    statements = (
                        select(models.fee_burner_tokens.c.token_address).where(
                            models.fee_burner_tokens.c.fee_burner_address == source_address
                        ),
                        select(models.fee_burner_token_balances_latest.c.token_address).where(
                            models.fee_burner_token_balances_latest.c.fee_burner_address == source_address
                        ),
                    )

                tokens: set[str] = set()
                for statement in statements:
                    tokens.update(normalize_address(row[0]) for row in session.execute(statement).all())
                return tokens
            except Exception:  # noqa: BLE001
                return set()
