"""Dashboard read model assembly reused by the API."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.orm import Session

from tidal.normalizers import normalize_address

STRATEGY_DETAIL_ROWS_SQL = """
SELECT
    'strategy' AS source_type,
    stbl.strategy_address AS source_address,
    s.name AS source_name,
    'vault' AS context_type,
    s.vault_address AS context_address,
    v.name AS context_name,
    v.symbol AS context_symbol,
    stbl.strategy_address AS strategy_address,
    s.name AS strategy_name,
    s.vault_address AS vault_address,
    v.name AS vault_name,
    v.symbol AS vault_symbol,
    {auction_column} AS auction_address,
    {auction_version_column} AS auction_version,
    {strategy_want_column} AS want_address,
    {strategy_want_symbol_column} AS want_symbol,
    {deposit_limit_column} AS deposit_limit,
    s.active,
    stbl.scanned_at,
    stbl.token_address,
    t.symbol AS token_symbol,
    t.name AS token_name,
    t.price_usd AS token_price_usd,
    {logo_column} AS token_logo_url,
    stbl.normalized_balance,
    {auction_enabled_scan_status_column} AS auction_enabled_scan_status,
    {auction_enabled_scan_scanned_at_column} AS auction_enabled_scan_scanned_at,
    {auction_enabled_scan_error_column} AS auction_enabled_scan_error,
    {auction_token_enabled_column} AS auction_token_enabled
FROM strategy_token_balances_latest stbl
JOIN strategies s ON s.address = stbl.strategy_address
JOIN vaults v ON v.address = s.vault_address
JOIN tokens t ON t.address = stbl.token_address
{strategy_want_join}
{auction_enabled_scan_join}
{auction_enabled_token_join}
ORDER BY s.vault_address, stbl.strategy_address, t.symbol
"""

FEE_BURNER_DETAIL_ROWS_SQL = """
SELECT
    'fee_burner' AS source_type,
    fbtbl.fee_burner_address AS source_address,
    fb.name AS source_name,
    NULL AS context_type,
    NULL AS context_address,
    NULL AS context_name,
    NULL AS context_symbol,
    NULL AS strategy_address,
    NULL AS strategy_name,
    NULL AS vault_address,
    NULL AS vault_name,
    NULL AS vault_symbol,
    {fee_burner_auction_column} AS auction_address,
    {fee_burner_auction_version_column} AS auction_version,
    {fee_burner_want_column} AS want_address,
    {fee_burner_want_symbol_column} AS want_symbol,
    NULL AS deposit_limit,
    1 AS active,
    fbtbl.scanned_at,
    fbtbl.token_address,
    t.symbol AS token_symbol,
    t.name AS token_name,
    t.price_usd AS token_price_usd,
    {logo_column} AS token_logo_url,
    fbtbl.normalized_balance,
    {auction_enabled_scan_status_column} AS auction_enabled_scan_status,
    {auction_enabled_scan_scanned_at_column} AS auction_enabled_scan_scanned_at,
    {auction_enabled_scan_error_column} AS auction_enabled_scan_error,
    {auction_token_enabled_column} AS auction_token_enabled
FROM fee_burner_token_balances_latest fbtbl
JOIN fee_burners fb ON fb.address = fbtbl.fee_burner_address
JOIN tokens t ON t.address = fbtbl.token_address
{fee_burner_want_join}
{auction_enabled_scan_join}
{auction_enabled_token_join}
ORDER BY fbtbl.fee_burner_address, t.symbol
"""

KICKS_SQL_TEMPLATE = """
SELECT
    {operation_type_expr} AS operation_type,
    {source_type_expr} AS source_type,
    {source_address_expr} AS source_address,
    k.strategy_address,
    k.chain_id,
    k.auction_address,
    k.tx_hash,
    k.status,
    k.token_address,
    {kick_token_symbol_column} AS token_symbol,
    {kick_auctionscan_round_id_column} AS auctionscan_round_id,
    k.usd_value,
    k.created_at
FROM kick_txs k
LEFT JOIN tokens t ON t.address = k.token_address
WHERE k.tx_hash IS NOT NULL AND k.tx_hash != ''
ORDER BY {source_address_expr}, k.created_at DESC
"""


class DashboardReadService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def load(self) -> dict[str, object]:
        features = self._get_schema_features()
        detail_rows = self.session.execute(text(self._build_strategy_detail_rows_sql(features))).mappings().all()
        if features["fee_burner_rows"]:
            detail_rows.extend(self.session.execute(text(self._build_fee_burner_detail_rows_sql(features))).mappings().all())
        kick_rows = self.session.execute(text(self._build_kicks_sql(features))).mappings().all() if features["kick_txs"] else []

        kicks_by_source = self._group_kicks(kick_rows)
        rows = self._assemble_rows(detail_rows, kicks_by_source)
        token_rows = self._build_token_catalog(detail_rows)
        latest_scan_at = max((row["scanned_at"] for row in detail_rows if row["scanned_at"]), default=None)
        summary = self._build_summary(rows, token_rows, latest_scan_at)
        return {
            "latestScanAt": latest_scan_at,
            "summary": summary,
            "tokens": token_rows,
            "rows": rows,
        }

    def _group_kicks(self, kick_rows: list[dict[str, object]]) -> dict[tuple[str | None, object], list[dict[str, object]]]:
        kicks_by_source: dict[tuple[str | None, object], list[dict[str, object]]] = {}
        for row in kick_rows:
            if (row["operation_type"] or "kick") != "kick":
                continue
            source_address = row["source_address"] or row["strategy_address"]
            if not source_address:
                continue
            source_key = (row["source_type"], source_address)
            kicks = kicks_by_source.setdefault(source_key, [])
            if len(kicks) < 5:
                kicks.append(
                    {
                        "chainId": row["chain_id"],
                        "auctionAddress": self._optional_normalize_address(row["auction_address"]),
                        "auctionScanRoundId": row["auctionscan_round_id"],
                        "txHash": row["tx_hash"],
                        "status": row["status"],
                        "tokenSymbol": row["token_symbol"],
                        "usdValue": row["usd_value"],
                        "createdAt": row["created_at"],
                    }
                )
        return kicks_by_source

    def _assemble_rows(
        self,
        detail_rows: list[dict[str, object]],
        kicks_by_source: dict[tuple[str | None, object], list[dict[str, object]]],
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        grouped_rows: dict[tuple[object, object, object], dict[str, object]] = {}
        for detail_row in detail_rows:
            source_key = (detail_row["source_type"], detail_row["source_address"])
            row_key = (detail_row["source_type"], detail_row["source_address"], detail_row["context_address"])
            grouped_row = grouped_rows.get(row_key)
            if grouped_row is None:
                grouped_row = {
                    "sourceType": detail_row["source_type"],
                    "sourceAddress": self._optional_normalize_address(detail_row["source_address"]),
                    "sourceName": detail_row["source_name"],
                    "contextType": detail_row["context_type"],
                    "contextAddress": self._optional_normalize_address(detail_row["context_address"]),
                    "contextName": detail_row["context_name"],
                    "contextSymbol": detail_row["context_symbol"],
                    "strategyAddress": self._optional_normalize_address(detail_row["strategy_address"]),
                    "strategyName": detail_row["strategy_name"],
                    "vaultAddress": self._optional_normalize_address(detail_row["vault_address"]),
                    "vaultName": detail_row["vault_name"],
                    "vaultSymbol": detail_row["vault_symbol"],
                    "auctionAddress": self._optional_normalize_address(detail_row["auction_address"]),
                    "auctionVersion": detail_row["auction_version"],
                    "wantAddress": self._optional_normalize_address(detail_row["want_address"]),
                    "wantSymbol": detail_row["want_symbol"],
                    "depositLimit": detail_row["deposit_limit"],
                    "active": bool(detail_row["active"]) if detail_row["active"] is not None else None,
                    "scannedAt": detail_row["scanned_at"],
                    "balances": [],
                    "kicks": kicks_by_source.get(source_key, []),
                }
                grouped_rows[row_key] = grouped_row
                rows.append(grouped_row)
            elif detail_row["scanned_at"] and (grouped_row["scannedAt"] is None or detail_row["scanned_at"] > grouped_row["scannedAt"]):
                grouped_row["scannedAt"] = detail_row["scanned_at"]

            grouped_row["balances"].append(
                {
                    "tokenAddress": self._optional_normalize_address(detail_row["token_address"]),
                    "tokenSymbol": detail_row["token_symbol"],
                    "tokenName": detail_row["token_name"],
                    "normalizedBalance": detail_row["normalized_balance"],
                    "tokenPriceUsd": detail_row["token_price_usd"],
                    "tokenLogoUrl": detail_row["token_logo_url"],
                    "auctionSellTokenStatus": self._derive_auction_sell_token_status(detail_row),
                    "auctionSellTokenStatusScannedAt": detail_row["auction_enabled_scan_scanned_at"],
                    "auctionSellTokenStatusError": detail_row["auction_enabled_scan_error"],
                }
            )
        return rows

    def _derive_auction_sell_token_status(self, detail_row: dict[str, object]) -> str:
        if not detail_row["auction_address"]:
            return "no_auction"
        if detail_row["token_address"] and detail_row["want_address"] and str(detail_row["token_address"]).lower() == str(detail_row["want_address"]).lower():
            return "want"
        if detail_row["auction_enabled_scan_status"] != "SUCCESS":
            return "unknown"
        return "enabled" if detail_row["auction_token_enabled"] else "disabled"

    def _build_token_catalog(self, detail_rows: list[dict[str, object]]) -> list[dict[str, object]]:
        tokens_by_address: dict[object, dict[str, object]] = {}
        for row in detail_rows:
            token_address = row["token_address"]
            if token_address not in tokens_by_address:
                tokens_by_address[token_address] = {
                    "tokenAddress": self._optional_normalize_address(token_address),
                    "tokenSymbol": row["token_symbol"],
                    "tokenName": row["token_name"],
                    "tokenPriceUsd": row["token_price_usd"],
                    "logoUrl": row["token_logo_url"],
                    "latestScanAt": row["scanned_at"],
                    "strategyCount": 0,
                    "sourceCount": 0,
                    "_source_keys": set(),
                }
            token_row = tokens_by_address[token_address]
            token_row["tokenSymbol"] = token_row["tokenSymbol"] or row["token_symbol"]
            token_row["tokenName"] = token_row["tokenName"] or row["token_name"]
            token_row["tokenPriceUsd"] = token_row["tokenPriceUsd"] or row["token_price_usd"]
            token_row["logoUrl"] = token_row["logoUrl"] or row["token_logo_url"]
            if row["scanned_at"] and (token_row["latestScanAt"] is None or row["scanned_at"] > token_row["latestScanAt"]):
                token_row["latestScanAt"] = row["scanned_at"]

            source_key = (row["source_type"], row["source_address"])
            if source_key not in token_row["_source_keys"]:
                token_row["_source_keys"].add(source_key)
                token_row["sourceCount"] += 1
                if row["source_type"] == "strategy":
                    token_row["strategyCount"] += 1

        token_rows = list(tokens_by_address.values())
        for row in token_rows:
            row.pop("_source_keys", None)
        token_rows.sort(key=lambda row: (-int(row["strategyCount"]), str(row["tokenSymbol"] or "").upper(), str(row["tokenAddress"])))
        return token_rows

    def _build_summary(
        self,
        rows: list[dict[str, object]],
        token_rows: list[dict[str, object]],
        latest_scan_at: object,
    ) -> dict[str, object]:
        strategy_count = len({row["sourceAddress"] for row in rows if row["sourceType"] == "strategy"})
        fee_burner_count = len({row["sourceAddress"] for row in rows if row["sourceType"] == "fee_burner"})
        return {
            "rowCount": len(rows),
            "sourceCount": len(rows),
            "strategyCount": strategy_count,
            "feeBurnerCount": fee_burner_count,
            "tokenCount": len(token_rows),
            "latestScanAt": latest_scan_at,
        }

    def _get_schema_features(self) -> dict[str, bool]:
        return {
            "strategies.auction_address": self._has_column("strategies", "auction_address"),
            "strategies.auction_version": self._has_column("strategies", "auction_version"),
            "strategies.want_address": self._has_column("strategies", "want_address"),
            "tokens.logo_url": self._has_column("tokens", "logo_url"),
            "vaults.deposit_limit": self._has_column("vaults", "deposit_limit"),
            "auction_enabled_tokens_latest": self._has_table("auction_enabled_tokens_latest"),
            "auction_enabled_token_scans": self._has_table("auction_enabled_token_scans"),
            "kick_txs": self._has_table("kick_txs"),
            "kick_txs.operation_type": self._has_column("kick_txs", "operation_type"),
            "kick_txs.source_type": self._has_column("kick_txs", "source_type"),
            "kick_txs.source_address": self._has_column("kick_txs", "source_address"),
            "kick_txs.token_symbol": self._has_column("kick_txs", "token_symbol"),
            "kick_txs.auctionscan_round_id": self._has_column("kick_txs", "auctionscan_round_id"),
            "fee_burners": self._has_table("fee_burners"),
            "fee_burners.auction_address": self._has_column("fee_burners", "auction_address"),
            "fee_burners.auction_version": self._has_column("fee_burners", "auction_version"),
            "fee_burners.want_address": self._has_column("fee_burners", "want_address"),
            "fee_burner_rows": self._has_table("fee_burners") and self._has_table("fee_burner_token_balances_latest"),
        }

    def _build_strategy_detail_rows_sql(self, features: dict[str, bool]) -> str:
        auction_column = "s.auction_address" if features["strategies.auction_address"] else "NULL"
        auction_version_column = "s.auction_version" if features["strategies.auction_version"] else "NULL"
        logo_column = "t.logo_url" if features["tokens.logo_url"] else "NULL"
        deposit_limit_column = "v.deposit_limit" if features["vaults.deposit_limit"] else "NULL"
        if features["strategies.want_address"]:
            strategy_want_column = "s.want_address"
            strategy_want_symbol_column = "wt.symbol"
            strategy_want_join = "LEFT JOIN tokens wt ON wt.address = s.want_address"
        else:
            strategy_want_column = "NULL"
            strategy_want_symbol_column = "NULL"
            strategy_want_join = ""

        if features["auction_enabled_token_scans"] and features["strategies.auction_address"]:
            auction_enabled_scan_status_column = "aes.status"
            auction_enabled_scan_scanned_at_column = "aes.scanned_at"
            auction_enabled_scan_error_column = "aes.error_message"
            auction_enabled_scan_join = "LEFT JOIN auction_enabled_token_scans aes ON aes.auction_address = s.auction_address"
        else:
            auction_enabled_scan_status_column = "NULL"
            auction_enabled_scan_scanned_at_column = "NULL"
            auction_enabled_scan_error_column = "NULL"
            auction_enabled_scan_join = ""

        if features["auction_enabled_tokens_latest"] and features["strategies.auction_address"]:
            auction_token_enabled_column = "CASE WHEN aet.token_address IS NOT NULL THEN 1 ELSE 0 END"
            auction_enabled_token_join = (
                "LEFT JOIN auction_enabled_tokens_latest aet "
                "ON aet.auction_address = s.auction_address "
                "AND aet.token_address = stbl.token_address "
                "AND aet.active = 1"
            )
        else:
            auction_token_enabled_column = "NULL"
            auction_enabled_token_join = ""

        return STRATEGY_DETAIL_ROWS_SQL.format(
            auction_column=auction_column,
            auction_version_column=auction_version_column,
            logo_column=logo_column,
            deposit_limit_column=deposit_limit_column,
            strategy_want_column=strategy_want_column,
            strategy_want_symbol_column=strategy_want_symbol_column,
            strategy_want_join=strategy_want_join,
            auction_enabled_scan_status_column=auction_enabled_scan_status_column,
            auction_enabled_scan_scanned_at_column=auction_enabled_scan_scanned_at_column,
            auction_enabled_scan_error_column=auction_enabled_scan_error_column,
            auction_token_enabled_column=auction_token_enabled_column,
            auction_enabled_scan_join=auction_enabled_scan_join,
            auction_enabled_token_join=auction_enabled_token_join,
        )

    def _build_fee_burner_detail_rows_sql(self, features: dict[str, bool]) -> str:
        logo_column = "t.logo_url" if features["tokens.logo_url"] else "NULL"
        fee_burner_auction_column = "fb.auction_address" if features["fee_burners.auction_address"] else "NULL"
        fee_burner_auction_version_column = "fb.auction_version" if features["fee_burners.auction_version"] else "NULL"
        if features["fee_burners.want_address"]:
            fee_burner_want_column = "fb.want_address"
            fee_burner_want_symbol_column = "wt.symbol"
            fee_burner_want_join = "LEFT JOIN tokens wt ON wt.address = fb.want_address"
        else:
            fee_burner_want_column = "NULL"
            fee_burner_want_symbol_column = "NULL"
            fee_burner_want_join = ""

        if features["auction_enabled_token_scans"] and features["fee_burners.auction_address"]:
            auction_enabled_scan_status_column = "aes.status"
            auction_enabled_scan_scanned_at_column = "aes.scanned_at"
            auction_enabled_scan_error_column = "aes.error_message"
            auction_enabled_scan_join = "LEFT JOIN auction_enabled_token_scans aes ON aes.auction_address = fb.auction_address"
        else:
            auction_enabled_scan_status_column = "NULL"
            auction_enabled_scan_scanned_at_column = "NULL"
            auction_enabled_scan_error_column = "NULL"
            auction_enabled_scan_join = ""

        if features["auction_enabled_tokens_latest"] and features["fee_burners.auction_address"]:
            auction_token_enabled_column = "CASE WHEN aet.token_address IS NOT NULL THEN 1 ELSE 0 END"
            auction_enabled_token_join = (
                "LEFT JOIN auction_enabled_tokens_latest aet "
                "ON aet.auction_address = fb.auction_address "
                "AND aet.token_address = fbtbl.token_address "
                "AND aet.active = 1"
            )
        else:
            auction_token_enabled_column = "NULL"
            auction_enabled_token_join = ""

        return FEE_BURNER_DETAIL_ROWS_SQL.format(
            fee_burner_auction_column=fee_burner_auction_column,
            fee_burner_auction_version_column=fee_burner_auction_version_column,
            fee_burner_want_column=fee_burner_want_column,
            fee_burner_want_symbol_column=fee_burner_want_symbol_column,
            fee_burner_want_join=fee_burner_want_join,
            logo_column=logo_column,
            auction_enabled_scan_status_column=auction_enabled_scan_status_column,
            auction_enabled_scan_scanned_at_column=auction_enabled_scan_scanned_at_column,
            auction_enabled_scan_error_column=auction_enabled_scan_error_column,
            auction_token_enabled_column=auction_token_enabled_column,
            auction_enabled_scan_join=auction_enabled_scan_join,
            auction_enabled_token_join=auction_enabled_token_join,
        )

    def _build_kicks_sql(self, features: dict[str, bool]) -> str:
        operation_type_expr = "COALESCE(k.operation_type, 'kick')" if features["kick_txs.operation_type"] else "'kick'"
        source_type_expr = (
            "COALESCE(k.source_type, CASE WHEN k.strategy_address IS NOT NULL THEN 'strategy' END)"
            if features["kick_txs.source_type"]
            else "'strategy'"
        )
        source_address_expr = "COALESCE(k.source_address, k.strategy_address)" if features["kick_txs.source_address"] else "k.strategy_address"
        kick_token_symbol_column = "COALESCE(k.token_symbol, t.symbol)" if features["kick_txs.token_symbol"] else "t.symbol"
        kick_auctionscan_round_id_column = (
            "k.auctionscan_round_id" if features["kick_txs.auctionscan_round_id"] else "NULL"
        )
        return KICKS_SQL_TEMPLATE.format(
            operation_type_expr=operation_type_expr,
            source_type_expr=source_type_expr,
            source_address_expr=source_address_expr,
            kick_token_symbol_column=kick_token_symbol_column,
            kick_auctionscan_round_id_column=kick_auctionscan_round_id_column,
        )

    def _has_table(self, table_name: str) -> bool:
        row = self.session.connection().exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _has_column(self, table_name: str, column_name: str) -> bool:
        rows = self.session.connection().exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
        return any(row[1] == column_name for row in rows)

    @staticmethod
    def _parse_decimal(value: object) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _optional_normalize_address(address: object) -> str | None:
        if not address:
            return None
        return normalize_address(str(address))
