"""Read models for kick history and AuctionScan lookups."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from tidal.normalizers import normalize_address

FAILED_KICK_LOG_STATUSES = ("REVERTED", "ERROR", "ESTIMATE_FAILED")

KICKS_DETAIL_SQL_TEMPLATE = """
SELECT
    k.id,
    k.run_id,
    {operation_type_expr} AS operation_type,
    {source_type_expr} AS source_type,
    {source_address_expr} AS source_address,
    {source_name_column} AS source_name,
    k.strategy_address,
    s.name AS strategy_name,
    k.token_address,
    {kick_token_symbol_column} AS token_symbol,
    k.auction_address,
    k.want_address,
    {kick_want_symbol_column} AS want_symbol,
    k.normalized_balance,
    k.sell_amount,
    k.starting_price,
    k.minimum_price,
    {kick_minimum_quote_column} AS minimum_quote,
    k.start_price_buffer_bps,
    k.min_price_buffer_bps,
    k.quote_amount,
    k.quote_response_json,
    {kick_step_decay_rate_bps_column} AS step_decay_rate_bps,
    {kick_settle_token_column} AS settle_token,
    {kick_stuck_abort_reason_column} AS stuck_abort_reason,
    k.price_usd,
    k.usd_value,
    k.status,
    k.tx_hash,
    k.gas_used,
    k.gas_price_gwei,
    k.block_number,
    {kick_auctionscan_round_id_column} AS auctionscan_round_id,
    {kick_auctionscan_last_checked_at_column} AS auctionscan_last_checked_at,
    {kick_auctionscan_matched_at_column} AS auctionscan_matched_at,
    k.error_message,
    k.created_at
FROM kick_txs k
LEFT JOIN strategies s ON s.address = {source_address_expr}
{fee_burner_join}
LEFT JOIN tokens t ON t.address = k.token_address
{want_token_join}
{where_clause}
ORDER BY k.created_at DESC
LIMIT :limit OFFSET :offset
"""


class KickLogReadService:
    def __init__(self, session: Session, *, chain_id: int, auctionscan_base_url: str) -> None:
        self.session = session
        self.chain_id = chain_id
        self.auctionscan_base_url = auctionscan_base_url.rstrip("/")

    def list_kicks(
        self,
        *,
        limit: int,
        offset: int = 0,
        status: str | None = None,
        q: str | None = None,
        source_address: str | None = None,
        auction_address: str | None = None,
        run_id: str | None = None,
        kick_id: int | None = None,
    ) -> dict[str, object]:
        if not self._has_table("kick_txs"):
            return {"kicks": [], "total": 0, "limit": limit, "offset": offset, "hasMore": False}

        features = self._get_schema_features()
        operation_type_expr, source_type_expr, resolved_source_address_expr = self._build_kick_source_expressions(features)
        token_symbol_expr = "COALESCE(k.token_symbol, '')" if features["kick_txs.token_symbol"] else "''"
        want_symbol_expr = "COALESCE(k.want_symbol, '')" if features["kick_txs.want_symbol"] else "''"
        search_operation_type_expr = "COALESCE(k.operation_type, 'kick')" if features["kick_txs.operation_type"] else "'kick'"
        clauses: list[str] = []
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if status is not None:
            normalized_status = str(status).strip().upper()
            if normalized_status == "FAILED":
                clauses.append("k.status IN ('REVERTED', 'ERROR', 'ESTIMATE_FAILED')")
            else:
                clauses.append("k.status = :status")
                params["status"] = normalized_status
        if q is not None and str(q).strip():
            clauses.append(
                "("
                f"LOWER({token_symbol_expr}) LIKE :q OR "
                f"LOWER({want_symbol_expr}) LIKE :q OR "
                "LOWER(COALESCE(k.auction_address, '')) LIKE :q OR "
                "LOWER(COALESCE(k.tx_hash, '')) LIKE :q OR "
                f"LOWER(COALESCE({resolved_source_address_expr}, '')) LIKE :q OR "
                "LOWER(COALESCE(k.run_id, '')) LIKE :q OR "
                f"LOWER({search_operation_type_expr}) LIKE :q"
                ")"
            )
            params["q"] = f"%{str(q).strip().lower()}%"
        if source_address is not None:
            clauses.append(f"LOWER({resolved_source_address_expr}) = :source_address")
            params["source_address"] = normalize_address(source_address).lower()
        if auction_address is not None:
            clauses.append("LOWER(k.auction_address) = :auction_address")
            params["auction_address"] = normalize_address(auction_address).lower()
        if run_id is not None:
            clauses.append("k.run_id = :run_id")
            params["run_id"] = run_id
        if kick_id is not None:
            clauses.append("k.id = :kick_id")
            params["kick_id"] = int(kick_id)
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        count_sql = f"SELECT COUNT(*) AS total FROM kick_txs k {where_clause}"
        total_row = self.session.execute(text(count_sql), params).mappings().first()
        total = int(total_row["total"]) if total_row is not None else 0
        if total == 0:
            return {"kicks": [], "total": 0, "limit": limit, "offset": offset, "hasMore": False}

        detail_sql = self._build_kicks_detail_sql(features, where_clause=where_clause)
        kick_rows = self.session.execute(text(detail_sql), params).mappings().all()
        kicks = []
        for row in kick_rows:
            kicks.append(
                {
                    "id": row["id"],
                    "runId": row["run_id"],
                    "operationType": row["operation_type"] or "kick",
                    "sourceType": row["source_type"],
                    "sourceAddress": self._optional_normalize_address(row["source_address"]),
                    "sourceName": row["source_name"],
                    "strategyAddress": self._optional_normalize_address(row["strategy_address"]),
                    "strategyName": row["strategy_name"],
                    "tokenAddress": self._optional_normalize_address(row["token_address"]),
                    "tokenSymbol": row["token_symbol"],
                    "auctionAddress": self._optional_normalize_address(row["auction_address"]),
                    "wantAddress": self._optional_normalize_address(row["want_address"]),
                    "wantSymbol": row["want_symbol"],
                    "normalizedBalance": row["normalized_balance"],
                    "sellAmount": row["sell_amount"],
                    "startingPrice": row["starting_price"],
                    "minimumPrice": row["minimum_price"],
                    "minimumQuote": row["minimum_quote"],
                    "startPriceBufferBps": row["start_price_buffer_bps"],
                    "minPriceBufferBps": row["min_price_buffer_bps"],
                    "quoteAmount": row["quote_amount"],
                    "quoteResponseJson": row["quote_response_json"],
                    "stepDecayRateBps": row["step_decay_rate_bps"],
                    "settleToken": self._optional_normalize_address(row["settle_token"]),
                    "stuckAbortReason": row["stuck_abort_reason"],
                    "priceUsd": row["price_usd"],
                    "usdValue": row["usd_value"],
                    "status": row["status"],
                    "txHash": row["tx_hash"],
                    "gasUsed": row["gas_used"],
                    "gasPriceGwei": row["gas_price_gwei"],
                    "blockNumber": row["block_number"],
                    "chainId": self.chain_id,
                    "auctionScanRoundId": row["auctionscan_round_id"],
                    "auctionScanLastCheckedAt": row["auctionscan_last_checked_at"],
                    "auctionScanMatchedAt": row["auctionscan_matched_at"],
                    "auctionScanAuctionUrl": self._build_auctionscan_auction_url(row["auction_address"]),
                    "auctionScanRoundUrl": self._build_auctionscan_round_url(row["auction_address"], row["auctionscan_round_id"]),
                    "errorMessage": row["error_message"],
                    "createdAt": row["created_at"],
                }
            )
        return {
            "kicks": kicks,
            "total": total,
            "limit": limit,
            "offset": offset,
            "hasMore": offset + len(kicks) < total,
        }

    def load_kick_auctionscan_context(self, kick_id: int) -> dict[str, object]:
        features = self._get_schema_features()
        if not features["kick_txs"]:
            raise ValueError("Kick history is unavailable")

        round_id_column = "k.auctionscan_round_id" if features["kick_txs.auctionscan_round_id"] else "NULL"
        last_checked_at_column = "k.auctionscan_last_checked_at" if features["kick_txs.auctionscan_last_checked_at"] else "NULL"
        matched_at_column = "k.auctionscan_matched_at" if features["kick_txs.auctionscan_matched_at"] else "NULL"

        row = self.session.execute(
            text(
                f"""
                SELECT
                    k.id,
                    COALESCE(k.operation_type, 'kick') AS operation_type,
                    k.status,
                    k.tx_hash,
                    k.auction_address,
                    k.token_address,
                    {round_id_column} AS auctionscan_round_id,
                    {last_checked_at_column} AS auctionscan_last_checked_at,
                    {matched_at_column} AS auctionscan_matched_at
                FROM kick_txs k
                WHERE k.id = :kick_id
                """
            ),
            {"kick_id": kick_id},
        ).mappings().first()
        if row is None:
            raise ValueError("Kick not found")

        operation_type = row["operation_type"] or "kick"
        auction_address = self._optional_normalize_address(row["auction_address"])
        token_address = self._optional_normalize_address(row["token_address"])
        tx_hash = row["tx_hash"]
        eligible = (
            operation_type == "kick"
            and row["status"] == "CONFIRMED"
            and auction_address is not None
            and token_address is not None
            and bool(tx_hash)
        )
        return {
            "id": row["id"],
            "operation_type": operation_type,
            "status": row["status"],
            "tx_hash": tx_hash,
            "auction_address": auction_address,
            "token_address": token_address,
            "auctionscan_round_id": row["auctionscan_round_id"],
            "auctionscan_last_checked_at": row["auctionscan_last_checked_at"],
            "auctionscan_matched_at": row["auctionscan_matched_at"],
            "eligible": eligible,
        }

    def persist_auctionscan_match(self, kick_id: int, *, round_id: int, checked_at: str, matched_at: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE kick_txs
                SET auctionscan_round_id = :round_id,
                    auctionscan_last_checked_at = :checked_at,
                    auctionscan_matched_at = :matched_at
                WHERE id = :kick_id
                """
            ),
            {
                "round_id": round_id,
                "checked_at": checked_at,
                "matched_at": matched_at,
                "kick_id": kick_id,
            },
        )
        self.session.commit()

    def persist_auctionscan_check(self, kick_id: int, *, checked_at: str) -> None:
        self.session.execute(
            text(
                """
                UPDATE kick_txs
                SET auctionscan_last_checked_at = :checked_at
                WHERE id = :kick_id
                """
            ),
            {"checked_at": checked_at, "kick_id": kick_id},
        )
        self.session.commit()

    def build_auctionscan_response(self, kick: dict[str, object], *, resolved: bool, cached: bool) -> dict[str, object]:
        return {
            "kickId": kick["id"],
            "chainId": self.chain_id,
            "eligible": bool(kick["eligible"]),
            "resolved": bool(resolved),
            "cached": bool(cached),
            "auctionAddress": kick["auction_address"],
            "roundId": kick["auctionscan_round_id"],
            "auctionUrl": self._build_auctionscan_auction_url(kick["auction_address"]),
            "roundUrl": self._build_auctionscan_round_url(kick["auction_address"], kick["auctionscan_round_id"]),
            "lastCheckedAt": kick["auctionscan_last_checked_at"],
            "matchedAt": kick["auctionscan_matched_at"],
        }

    def _build_kicks_detail_sql(self, features: dict[str, bool], *, where_clause: str) -> str:
        operation_type_expr, source_type_expr, source_address_expr = self._build_kick_source_expressions(features)
        kick_token_symbol_column = "COALESCE(k.token_symbol, t.symbol)" if features["kick_txs.token_symbol"] else "t.symbol"
        kick_auctionscan_round_id_column = "k.auctionscan_round_id" if features["kick_txs.auctionscan_round_id"] else "NULL"
        kick_auctionscan_last_checked_at_column = (
            "k.auctionscan_last_checked_at" if features["kick_txs.auctionscan_last_checked_at"] else "NULL"
        )
        kick_auctionscan_matched_at_column = (
            "k.auctionscan_matched_at" if features["kick_txs.auctionscan_matched_at"] else "NULL"
        )
        kick_step_decay_rate_bps_column = "k.step_decay_rate_bps" if features["kick_txs.step_decay_rate_bps"] else "NULL"
        kick_settle_token_column = "k.settle_token" if features["kick_txs.settle_token"] else "NULL"
        kick_stuck_abort_reason_column = "k.stuck_abort_reason" if features["kick_txs.stuck_abort_reason"] else "NULL"
        kick_want_symbol_column = "COALESCE(k.want_symbol, wt.symbol)" if features["kick_txs.want_symbol"] else "wt.symbol"
        kick_minimum_quote_column = "k.minimum_quote" if features["kick_txs.minimum_quote"] else "NULL"

        if features["fee_burners"]:
            fee_burner_join = f"LEFT JOIN fee_burners fb ON fb.address = {source_address_expr}"
            source_name_column = f"CASE WHEN {source_type_expr} = 'fee_burner' THEN fb.name ELSE s.name END"
        else:
            fee_burner_join = ""
            source_name_column = "s.name"

        return KICKS_DETAIL_SQL_TEMPLATE.format(
            operation_type_expr=operation_type_expr,
            source_type_expr=source_type_expr,
            source_address_expr=source_address_expr,
            source_name_column=source_name_column,
            fee_burner_join=fee_burner_join,
            kick_token_symbol_column=kick_token_symbol_column,
            kick_want_symbol_column=kick_want_symbol_column,
            kick_step_decay_rate_bps_column=kick_step_decay_rate_bps_column,
            kick_settle_token_column=kick_settle_token_column,
            kick_stuck_abort_reason_column=kick_stuck_abort_reason_column,
            kick_minimum_quote_column=kick_minimum_quote_column,
            kick_auctionscan_round_id_column=kick_auctionscan_round_id_column,
            kick_auctionscan_last_checked_at_column=kick_auctionscan_last_checked_at_column,
            kick_auctionscan_matched_at_column=kick_auctionscan_matched_at_column,
            want_token_join="LEFT JOIN tokens wt ON wt.address = k.want_address",
            where_clause=where_clause,
        )

    def _build_kick_source_expressions(self, features: dict[str, bool]) -> tuple[str, str, str]:
        operation_type_expr = "COALESCE(k.operation_type, 'kick')" if features["kick_txs.operation_type"] else "'kick'"
        source_type_expr = (
            "COALESCE(k.source_type, CASE WHEN k.strategy_address IS NOT NULL THEN 'strategy' END)"
            if features["kick_txs.source_type"]
            else "'strategy'"
        )
        source_address_expr = "COALESCE(k.source_address, k.strategy_address)" if features["kick_txs.source_address"] else "k.strategy_address"
        return operation_type_expr, source_type_expr, source_address_expr

    def _get_schema_features(self) -> dict[str, bool]:
        return {
            "kick_txs": self._has_table("kick_txs"),
            "kick_txs.operation_type": self._has_column("kick_txs", "operation_type"),
            "kick_txs.source_type": self._has_column("kick_txs", "source_type"),
            "kick_txs.source_address": self._has_column("kick_txs", "source_address"),
            "kick_txs.token_symbol": self._has_column("kick_txs", "token_symbol"),
            "kick_txs.want_symbol": self._has_column("kick_txs", "want_symbol"),
            "kick_txs.minimum_quote": self._has_column("kick_txs", "minimum_quote"),
            "kick_txs.step_decay_rate_bps": self._has_column("kick_txs", "step_decay_rate_bps"),
            "kick_txs.settle_token": self._has_column("kick_txs", "settle_token"),
            "kick_txs.stuck_abort_reason": self._has_column("kick_txs", "stuck_abort_reason"),
            "kick_txs.auctionscan_round_id": self._has_column("kick_txs", "auctionscan_round_id"),
            "kick_txs.auctionscan_last_checked_at": self._has_column("kick_txs", "auctionscan_last_checked_at"),
            "kick_txs.auctionscan_matched_at": self._has_column("kick_txs", "auctionscan_matched_at"),
            "fee_burners": self._has_table("fee_burners"),
        }

    def _has_table(self, table_name: str) -> bool:
        row = self.session.connection().exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _has_column(self, table_name: str, column_name: str) -> bool:
        rows = self.session.connection().exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
        return any(row[1] == column_name for row in rows)

    def _build_auctionscan_auction_url(self, auction_address: object) -> str | None:
        if not auction_address:
            return None
        return f"{self.auctionscan_base_url}/auction/{self.chain_id}/{normalize_address(str(auction_address))}"

    def _build_auctionscan_round_url(self, auction_address: object, round_id: object) -> str | None:
        if not auction_address or round_id is None:
            return None
        return f"{self.auctionscan_base_url}/round/{self.chain_id}/{normalize_address(str(auction_address))}/{int(round_id)}"

    @staticmethod
    def _optional_normalize_address(address: object) -> str | None:
        if not address:
            return None
        return normalize_address(str(address))
