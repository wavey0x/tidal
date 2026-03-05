"""Healthcheck helpers."""

from __future__ import annotations

from sqlalchemy import text


async def run_healthcheck(db_session, web3_client=None) -> dict[str, object]:
    db_session.execute(text("SELECT 1"))

    block_number = None
    if web3_client is not None:
        block_number = await web3_client.get_block_number()

    return {
        "db": "ok",
        "rpc": "ok" if web3_client is not None else "skipped",
        "block_number": block_number,
    }
