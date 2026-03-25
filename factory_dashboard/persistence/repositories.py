"""Repository helpers for upserting scan entities."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from factory_dashboard.persistence import models
from factory_dashboard.types import BalanceResult, ScanItemError, TokenLogoState, TokenMetadata


class StrategyRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_many(self, rows: Iterable[dict[str, object]]) -> None:
        for row in rows:
            stmt = insert(models.strategies).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[models.strategies.c.address],
                set_={
                    "chain_id": row["chain_id"],
                    "vault_address": row["vault_address"],
                    "name": row["name"] if row.get("name") is not None else models.strategies.c.name,
                    "adapter": row.get("adapter", "yearn_curve_strategy"),
                    "active": row.get("active", 1),
                    "last_seen_at": row["last_seen_at"],
                },
            )
            self.session.execute(stmt)

    def addresses_missing_name(self, addresses: list[str]) -> list[str]:
        if not addresses:
            return []
        stmt = select(models.strategies.c.address).where(
            models.strategies.c.address.in_(addresses),
            models.strategies.c.name.is_(None),
        )
        return [row[0] for row in self.session.execute(stmt).all()]

    def set_name(self, address: str, name: str) -> None:
        self.session.execute(
            update(models.strategies)
            .where(models.strategies.c.address == address)
            .values(name=name)
        )

    def set_auction_mappings(
        self,
        strategy_to_auction: dict[str, str | None],
        *,
        updated_at: str,
        strategy_to_want: dict[str, str | None] | None = None,
        strategy_to_auction_version: dict[str, str | None] | None = None,
    ) -> None:
        for strategy_address, auction_address in strategy_to_auction.items():
            values: dict[str, object] = {
                "auction_address": auction_address,
                "auction_updated_at": updated_at,
                "auction_error_message": None,
            }
            if strategy_to_want is not None:
                values["want_address"] = strategy_to_want.get(strategy_address)
            if strategy_to_auction_version is not None:
                values["auction_version"] = strategy_to_auction_version.get(strategy_address)
            self.session.execute(
                update(models.strategies)
                .where(models.strategies.c.address == strategy_address)
                .values(**values)
            )

    def mark_auction_refresh_failed(self, addresses: list[str], *, updated_at: str, error_message: str) -> None:
        if not addresses:
            return
        self.session.execute(
            update(models.strategies)
            .where(models.strategies.c.address.in_(addresses))
            .values(
                auction_updated_at=updated_at,
                auction_error_message=error_message,
            )
        )

    def auction_mapping_for_addresses(self, addresses: list[str]) -> dict[str, str | None]:
        if not addresses:
            return {}
        stmt = select(
            models.strategies.c.address,
            models.strategies.c.auction_address,
        ).where(models.strategies.c.address.in_(addresses))
        return {
            row.address: row.auction_address
            for row in self.session.execute(stmt)
        }


class FeeBurnerRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_many(self, rows: Iterable[dict[str, object]]) -> None:
        for row in rows:
            stmt = insert(models.fee_burners).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[models.fee_burners.c.address],
                set_={
                    "chain_id": row["chain_id"],
                    "name": row["name"] if row.get("name") is not None else models.fee_burners.c.name,
                    "active": row.get("active", 1),
                    "want_address": row.get("want_address", models.fee_burners.c.want_address),
                    "last_seen_at": row["last_seen_at"],
                },
            )
            self.session.execute(stmt)

    def set_auction_mappings(
        self,
        fee_burner_to_auction: dict[str, str | None],
        *,
        updated_at: str,
        fee_burner_to_want: dict[str, str | None] | None = None,
        fee_burner_to_auction_version: dict[str, str | None] | None = None,
    ) -> None:
        for fee_burner_address, auction_address in fee_burner_to_auction.items():
            values: dict[str, object] = {
                "auction_address": auction_address,
                "auction_updated_at": updated_at,
                "auction_error_message": None,
            }
            if fee_burner_to_want is not None:
                values["want_address"] = fee_burner_to_want.get(fee_burner_address)
            if fee_burner_to_auction_version is not None:
                values["auction_version"] = fee_burner_to_auction_version.get(fee_burner_address)
            self.session.execute(
                update(models.fee_burners)
                .where(models.fee_burners.c.address == fee_burner_address)
                .values(**values)
            )

    def mark_auction_refresh_failed(self, address_to_error: dict[str, str], *, updated_at: str) -> None:
        for address, error_message in address_to_error.items():
            self.session.execute(
                update(models.fee_burners)
                .where(models.fee_burners.c.address == address)
                .values(
                    auction_address=None,
                    auction_updated_at=updated_at,
                    auction_error_message=error_message,
                )
            )
    


class VaultRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert_many(self, rows: Iterable[dict[str, object]]) -> None:
        for row in rows:
            stmt = insert(models.vaults).values(**row)
            stmt = stmt.on_conflict_do_update(
                index_elements=[models.vaults.c.address],
                set_={
                    "chain_id": row["chain_id"],
                    "name": row["name"] if row.get("name") is not None else models.vaults.c.name,
                    "symbol": row["symbol"] if row.get("symbol") is not None else models.vaults.c.symbol,
                    "active": row.get("active", 1),
                    "last_seen_at": row["last_seen_at"],
                },
            )
            self.session.execute(stmt)

    def addresses_missing_name(self, addresses: list[str]) -> list[str]:
        if not addresses:
            return []
        stmt = select(models.vaults.c.address).where(
            models.vaults.c.address.in_(addresses),
            models.vaults.c.name.is_(None),
        )
        return [row[0] for row in self.session.execute(stmt).all()]

    def set_name(self, address: str, name: str) -> None:
        self.session.execute(
            update(models.vaults)
            .where(models.vaults.c.address == address)
            .values(name=name)
        )

    def addresses_missing_symbol(self, addresses: list[str]) -> list[str]:
        if not addresses:
            return []
        stmt = select(models.vaults.c.address).where(
            models.vaults.c.address.in_(addresses),
            models.vaults.c.symbol.is_(None),
        )
        return [row[0] for row in self.session.execute(stmt).all()]

    def set_symbol(self, address: str, symbol: str) -> None:
        self.session.execute(
            update(models.vaults)
            .where(models.vaults.c.address == address)
            .values(symbol=symbol)
        )

    def set_deposit_limit(self, address: str, deposit_limit: str) -> None:
        self.session.execute(
            update(models.vaults)
            .where(models.vaults.c.address == address)
            .values(deposit_limit=deposit_limit)
        )

    def delete_addresses_if_orphaned(self, addresses: list[str]) -> None:
        if not addresses:
            return
        for address in addresses:
            has_strategy = self.session.execute(
                select(models.strategies.c.address)
                .where(models.strategies.c.vault_address == address)
                .limit(1)
            ).first()
            if has_strategy is None:
                self.session.execute(delete(models.vaults).where(models.vaults.c.address == address))

    def delete_strategy_address_rows_without_children(self) -> None:
        strategy_addresses = [
            row[0]
            for row in self.session.execute(select(models.strategies.c.address.distinct())).all()
        ]
        self.delete_addresses_if_orphaned(strategy_addresses)


class TokenRepository:
    def __init__(self, session: Session):
        self.session = session

    def get(self, address: str) -> TokenMetadata | None:
        stmt = select(models.tokens).where(models.tokens.c.address == address)
        row = self.session.execute(stmt).mappings().first()
        if row is None:
            return None
        return TokenMetadata(
            address=row["address"],
            chain_id=row["chain_id"],
            name=row["name"],
            symbol=row["symbol"],
            decimals=row["decimals"],
            is_core_reward=bool(row["is_core_reward"]),
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )

    def upsert(self, token: TokenMetadata) -> None:
        row = asdict(token)
        row["is_core_reward"] = 1 if token.is_core_reward else 0
        stmt = insert(models.tokens).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=[models.tokens.c.address],
            set_={
                "chain_id": row["chain_id"],
                "name": row["name"],
                "symbol": row["symbol"],
                "decimals": row["decimals"],
                "is_core_reward": row["is_core_reward"],
                "last_seen_at": row["last_seen_at"],
            },
        )
        self.session.execute(stmt)

    def set_latest_price(
        self,
        *,
        address: str,
        price_usd: str | None,
        source: str,
        status: str,
        fetched_at: str,
        run_id: str,
        error_message: str | None,
    ) -> None:
        self.session.execute(
            update(models.tokens)
            .where(models.tokens.c.address == address)
            .values(
                price_usd=price_usd,
                price_source=source,
                price_status=status,
                price_fetched_at=fetched_at,
                price_run_id=run_id,
                price_error_message=error_message,
            )
        )

    def get_logo_state(self, address: str) -> TokenLogoState | None:
        stmt = (
            select(
                models.tokens.c.address,
                models.tokens.c.logo_url,
                models.tokens.c.logo_status,
                models.tokens.c.logo_validated_at,
            )
            .where(models.tokens.c.address == address)
        )
        row = self.session.execute(stmt).mappings().first()
        if row is None:
            return None
        return TokenLogoState(
            address=row["address"],
            logo_url=row["logo_url"],
            logo_status=row["logo_status"],
            logo_validated_at=row["logo_validated_at"],
        )

    def set_logo_url(self, *, address: str, logo_url: str | None) -> None:
        self.session.execute(
            update(models.tokens)
            .where(models.tokens.c.address == address)
            .values(logo_url=logo_url)
        )


class StrategyTokenRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, strategy_address: str, token_address: str, source: str, now_iso: str) -> None:
        stmt = insert(models.strategy_tokens).values(
            strategy_address=strategy_address,
            token_address=token_address,
            source=source,
            active=1,
            first_seen_at=now_iso,
            last_seen_at=now_iso,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[models.strategy_tokens.c.strategy_address, models.strategy_tokens.c.token_address],
            set_={
                "source": source,
                "active": 1,
                "last_seen_at": now_iso,
            },
        )
        self.session.execute(stmt)


class FeeBurnerTokenRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, fee_burner_address: str, token_address: str, source: str, now_iso: str) -> None:
        stmt = insert(models.fee_burner_tokens).values(
            fee_burner_address=fee_burner_address,
            token_address=token_address,
            source=source,
            active=1,
            first_seen_at=now_iso,
            last_seen_at=now_iso,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[models.fee_burner_tokens.c.fee_burner_address, models.fee_burner_tokens.c.token_address],
            set_={
                "source": source,
                "active": 1,
                "last_seen_at": now_iso,
            },
        )
        self.session.execute(stmt)


class BalanceRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, result: BalanceResult) -> None:
        stmt = insert(models.strategy_token_balances_latest).values(
            strategy_address=result.source_address,
            token_address=result.token_address,
            raw_balance=str(result.raw_balance),
            normalized_balance=result.normalized_balance,
            block_number=result.block_number,
            scanned_at=result.scanned_at.isoformat(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                models.strategy_token_balances_latest.c.strategy_address,
                models.strategy_token_balances_latest.c.token_address,
            ],
            set_={
                "raw_balance": str(result.raw_balance),
                "normalized_balance": result.normalized_balance,
                "block_number": result.block_number,
                "scanned_at": result.scanned_at.isoformat(),
            },
        )
        self.session.execute(stmt)


class FeeBurnerTokenBalanceRepository:
    def __init__(self, session: Session):
        self.session = session

    def upsert(self, result: BalanceResult) -> None:
        stmt = insert(models.fee_burner_token_balances_latest).values(
            fee_burner_address=result.source_address,
            token_address=result.token_address,
            raw_balance=str(result.raw_balance),
            normalized_balance=result.normalized_balance,
            block_number=result.block_number,
            scanned_at=result.scanned_at.isoformat(),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                models.fee_burner_token_balances_latest.c.fee_burner_address,
                models.fee_burner_token_balances_latest.c.token_address,
            ],
            set_={
                "raw_balance": str(result.raw_balance),
                "normalized_balance": result.normalized_balance,
                "block_number": result.block_number,
                "scanned_at": result.scanned_at.isoformat(),
            },
        )
        self.session.execute(stmt)


class ScanRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, row: dict[str, object]) -> None:
        self.session.execute(insert(models.scan_runs).values(**row))

    def finalize(
        self,
        run_id: str,
        *,
        finished_at: str,
        status: str,
        vaults_seen: int,
        strategies_seen: int,
        pairs_seen: int,
        pairs_succeeded: int,
        pairs_failed: int,
        error_summary: str | None,
    ) -> None:
        stmt = (
            models.scan_runs.update()
            .where(models.scan_runs.c.run_id == run_id)
            .values(
                finished_at=finished_at,
                status=status,
                vaults_seen=vaults_seen,
                strategies_seen=strategies_seen,
                pairs_seen=pairs_seen,
                pairs_succeeded=pairs_succeeded,
                pairs_failed=pairs_failed,
                error_summary=error_summary,
            )
        )
        self.session.execute(stmt)

    def latest_run_ids(self, limit: int) -> list[str]:
        stmt = (
            select(models.scan_runs.c.run_id)
            .order_by(models.scan_runs.c.started_at.desc())
            .limit(limit)
        )
        return [row[0] for row in self.session.execute(stmt).all()]


class ScanItemErrorRepository:
    def __init__(self, session: Session):
        self.session = session

    def add_many(self, run_id: str, errors: Iterable[ScanItemError], created_at: str) -> None:
        for error in errors:
            self.session.execute(
                insert(models.scan_item_errors).values(
                    run_id=run_id,
                    source_type=error.source_type,
                    source_address=error.source_address,
                    strategy_address=error.source_address if error.source_type == "strategy" else None,
                    token_address=error.token_address,
                    stage=error.stage,
                    error_code=error.error_code,
                    error_message=error.error_message,
                    created_at=created_at,
                )
            )

    def has_error_for_run(
        self,
        run_id: str,
        *,
        source_address: str | None,
        token_address: str | None,
        stage: str,
        error_code: str,
    ) -> bool:
        stmt = select(models.scan_item_errors.c.id).where(
            and_(
                models.scan_item_errors.c.run_id == run_id,
                models.scan_item_errors.c.source_address == source_address,
                models.scan_item_errors.c.token_address == token_address,
                models.scan_item_errors.c.stage == stage,
                models.scan_item_errors.c.error_code == error_code,
            )
        )
        return self.session.execute(stmt).first() is not None


class TxnRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, row: dict[str, object]) -> None:
        self.session.execute(insert(models.txn_runs).values(**row))
        self.session.commit()

    def finalize(
        self,
        run_id: str,
        *,
        finished_at: str,
        status: str,
        candidates_found: int,
        kicks_attempted: int,
        kicks_succeeded: int,
        kicks_failed: int,
        error_summary: str | None,
    ) -> None:
        self.session.execute(
            models.txn_runs.update()
            .where(models.txn_runs.c.run_id == run_id)
            .values(
                finished_at=finished_at,
                status=status,
                candidates_found=candidates_found,
                kicks_attempted=kicks_attempted,
                kicks_succeeded=kicks_succeeded,
                kicks_failed=kicks_failed,
                error_summary=error_summary,
            )
        )
        self.session.commit()


class KickTxRepository:
    def __init__(self, session: Session):
        self.session = session

    def insert(self, row: dict[str, object]) -> int:
        result = self.session.execute(insert(models.kick_txs).values(**row))
        self.session.commit()
        return result.lastrowid  # type: ignore[return-value]

    def update_status(
        self,
        kick_tx_id: int,
        *,
        status: str,
        tx_hash: str | None = None,
        gas_used: int | None = None,
        gas_price_gwei: str | None = None,
        block_number: int | None = None,
        error_message: str | None = None,
    ) -> None:
        values: dict[str, object] = {"status": status}
        if tx_hash is not None:
            values["tx_hash"] = tx_hash
        if gas_used is not None:
            values["gas_used"] = gas_used
        if gas_price_gwei is not None:
            values["gas_price_gwei"] = gas_price_gwei
        if block_number is not None:
            values["block_number"] = block_number
        if error_message is not None:
            values["error_message"] = error_message
        self.session.execute(
            models.kick_txs.update()
            .where(models.kick_txs.c.id == kick_tx_id)
            .values(**values)
        )
        self.session.commit()

    def last_kick_for_pair(self, source_address: str, token_address: str) -> dict[str, object] | None:
        stmt = (
            select(models.kick_txs)
            .where(
                or_(
                    models.kick_txs.c.source_address == source_address,
                    and_(
                        models.kick_txs.c.source_address.is_(None),
                        models.kick_txs.c.strategy_address == source_address,
                    ),
                ),
                models.kick_txs.c.token_address == token_address,
                models.kick_txs.c.status.in_(("CONFIRMED", "SUBMITTED")),
            )
            .order_by(models.kick_txs.c.created_at.desc())
            .limit(1)
        )
        row = self.session.execute(stmt).mappings().first()
        if row is None:
            return None
        return dict(row)
