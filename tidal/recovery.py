"""Native recovery operations. Only explicit resume grants execution authority."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

from hexbytes import HexBytes
from sqlalchemy import func, select

from tidal.async_resources import close_client
from tidal.lifecycle import (
    LifecycleError, activation_binding, atomic_json, clear_activation,
    execution_lock, inspect_database, require_activation, result, write_activation,
)
from tidal.normalizers import normalize_address
from tidal.operation_reconciler import OperationReconciler
from tidal.persistence import models
from tidal.runtime import build_scanner_service, build_web3_client
from tidal.time import utcnow_iso
from tidal.transaction_evidence import rpc_int
from tidal.transactions import LedgerReconciler, TransactionRepository


def configured_signer(settings) -> str:
    try:
        address = json.loads(settings.resolved_txn_keystore_path.read_text())["address"]
        return normalize_address("0x" + str(address).removeprefix("0x"))
    except (OSError, TypeError, KeyError, ValueError, AttributeError) as exc:
        raise LifecycleError("WRONG_SIGNER", "Configured encrypted keystore identity cannot be inspected.") from exc


def check_configuration(settings) -> dict:
    """Validate recoverable credentials with the native loader, without DB/RPC."""
    from tidal.transaction_service.signer import TransactionSigner
    try:
        signer = TransactionSigner(str(settings.resolved_txn_keystore_path), settings.txn_keystore_passphrase)
    except Exception as exc:
        raise LifecycleError("WRONG_SIGNER", "Configured encrypted keystore cannot be unlocked with the selected secret file.") from exc
    declared = {profile: normalize_address(address) for profile, address in settings.managed_signers.items()}
    if set(declared) != {"scan", "kick"} or set(declared.values()) != {signer.address}:
        raise LifecycleError("WRONG_SIGNER", "Declared scan/kick identities differ from the recovered key.")
    if configured_signer(settings) != signer.address:
        raise LifecycleError("WRONG_SIGNER", "Keystore public identity differs from its encrypted key.")
    if set(settings.execution_profiles) != {"scan", "strategy", "fee_burner"}:
        raise LifecycleError("INCOMPLETE_POLICY", "Declare the scan, strategy and fee-burner execution policies.")
    return result("CONFIGURATION_VALID", data={
        "chain_id": settings.chain_id, "signers": declared,
        "database_path": str(settings.resolved_db_path), "home_path": str(settings.resolved_home_path),
        "config_path": str(settings.resolved_config_path), "secret_file": str(settings.resolved_env_path),
        "keystore_path": str(settings.resolved_txn_keystore_path),
        "execution_profiles": {name: policy.model_dump() for name, policy in settings.execution_profiles.items()},
    })


def observation_readiness(settings, session) -> dict:
    latest = session.execute(select(models.scan_runs).order_by(models.scan_runs.c.started_at.desc()).limit(1)).mappings().first()
    if latest is None:
        pristine = not any(session.execute(select(table).limit(1)).first() is not None
            for table in (models.vaults, models.strategies, models.fee_burners, models.kick_txs, models.transactions))
        return {"ready": pristine, "pristine": pristine, "latest_scan": None,
                "reason": None if pristine else "Current observations are missing; run refresh --recovery."}
    critical = session.execute(select(models.scan_item_errors.c.id).where(
        models.scan_item_errors.c.run_id == latest["run_id"],
        models.scan_item_errors.c.stage.not_in(("PRICE_READ", "AUCTIONSCAN_ENRICHMENT", "OPERATION_RECONCILIATION")),
    )).first()
    try:
        age = time.time() - datetime.fromisoformat(str(latest["started_at"]).replace("Z", "+00:00")).timestamp()
        fresh = 0 <= age <= settings.txn_data_freshness_limit_seconds
    except (ValueError, TypeError):
        fresh = False
    ready = latest["status"] == "SUCCESS" and critical is None and fresh
    return {"ready": ready, "pristine": False, "latest_scan": latest["run_id"],
            "reason": None if ready else "Current observations are incomplete or stale; run refresh --recovery."}


async def chain_readiness(settings, web3) -> dict:
    """A fresh current and finalized head, never a historical indexing claim."""
    try:
        if await web3.get_chain_id() != settings.chain_id:
            raise LifecycleError("WRONG_CHAIN", "RPC chain differs from configuration.")
        latest = await web3.get_block("latest")
        finalized = await web3.get_block("finalized")
    except LifecycleError:
        raise
    except Exception as exc:
        raise LifecycleError("WAITING_FOR_RPC", f"Current chain reads unavailable ({type(exc).__name__}); retry when RPC is ready.") from exc
    try:
        for block, age in ((latest, settings.chain_read_max_age_seconds),
                           (finalized, settings.finality_max_age_seconds)):
            if not -120 <= time.time() - rpc_int(block["timestamp"]) <= age:
                raise LifecycleError("STALE_CHAIN", "Current chain or finalized head is stale; retry when RPC is ready.")
            if len(HexBytes(block["hash"])) != 32:
                raise ValueError("invalid hash")
        if rpc_int(finalized["number"]) > rpc_int(latest["number"]):
            raise ValueError("invalid finality head")
    except (ValueError, TypeError, KeyError) as exc:
        raise LifecycleError("INVALID_RPC_EVIDENCE", "RPC returned incomplete current/finality evidence.") from exc
    return {"head_number": rpc_int(latest["number"]), "head_hash": "0x" + bytes(HexBytes(latest["hash"])).hex(),
            "finalized_number": rpc_int(finalized["number"]), "checked_at": utcnow_iso()}


def ledger_reconciler(settings, session, web3) -> LedgerReconciler:
    return LedgerReconciler(settings=settings, session=session, web3_client=web3,
        operation_reconciler=OperationReconciler(session=session, web3_client=web3,
            auction_kicker_address=settings.auction_kicker_address, chain_id=settings.chain_id, settings=settings))


def price_readiness(settings, session) -> dict:
    # This is a cached diagnostic. Action-specific amount quotes are always
    # obtained by the action owner; status/restore never call price providers.
    rows = session.execute(select(models.tokens.c.price_status, models.tokens.c.price_fetched_at)).all()
    fresh = 0
    for status, fetched in rows:
        try:
            at = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
            if at.tzinfo is None:
                at = at.replace(tzinfo=timezone.utc)
            if status == "SUCCESS" and 0 <= time.time() - at.timestamp() <= settings.txn_data_freshness_limit_seconds:
                fresh += 1
        except (ValueError, TypeError):
            pass
    return {"tokens_with_fresh_cached_prices": fresh, "tokens_total": len(rows),
            "required_for_basic_activation": False, "action_quotes_rechecked_before_send": True}


async def status(settings, session, *, web3=None) -> dict:
    info = inspect_database(settings.resolved_db_path)
    blockers, warnings = [], []
    activated = False
    activation = {}
    try:
        binding = activation_binding(info["database_identity"], settings.chain_id, settings.managed_signers)
        activation = require_activation(settings.resolved_home_path / "activation.json", binding)
        activated = True
    except LifecycleError as exc:
        warnings.append({"code": exc.code, "message": str(exc)})
    pending = TransactionRepository(session).unresolved()
    observations = observation_readiness(settings, session)
    if not observations["ready"]:
        warnings.append({"code": "INCOMPLETE_REFRESH", "message": observations["reason"]})
    try:
        signer = configured_signer(settings)
    except LifecycleError as exc:
        signer = None
        warnings.append({"code": exc.code, "message": str(exc)})
    session.commit()
    chain = None
    own_client = web3 is None
    nonces = {}
    try:
        web3 = web3 or build_web3_client(settings)
        chain = await chain_readiness(settings, web3)
        for address in set(settings.managed_signers.values()):
            address = normalize_address(address)
            latest = rpc_int(await web3.get_transaction_count(address, "latest"))
            mempool = rpc_int(await web3.get_transaction_count(address, "pending"))
            retained = session.execute(select(func.max(models.transactions.c.nonce)).where(
                models.transactions.c.chain_id == settings.chain_id, models.transactions.c.signer == address)).scalar()
            baseline = activation.get("nonce_baseline", {}).get(address)
            expected = None if baseline is None else max(int(baseline), retained + 1 if retained is not None else 0)
            nonces[address] = {"latest": latest, "pending": mempool, "expected": expected,
                               "ready": expected is not None and latest == mempool == expected}
    except LifecycleError as exc:
        blockers.append({"code": exc.code, "message": str(exc)})
    except Exception as exc:
        blockers.append({"code": "WAITING_FOR_RPC", "message": f"Current chain reads unavailable ({type(exc).__name__}); retry later."})
    finally:
        if own_client and web3 is not None:
            await close_client(web3)
    if pending:
        warnings.append({"code": "UNRESOLVED_ATTEMPTS", "message": f"{len(pending)} retained attempt(s) block their affected signers."})
    data = {**info, "api_can_serve": True, "chain_reads_ready": chain is not None,
            "runtime": {"python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
                        "executable": sys.executable, "package_path": str(Path(__file__).parent)},
            "observations": observations,
            "activated": activated, "chain": chain, "prices": price_readiness(settings, session),
            "pending_count": sum(row["status"] != "REVIEW_REQUIRED" for row in pending),
            "review_count": sum(row["status"] == "REVIEW_REQUIRED" for row in pending),
            "unresolved_transactions": pending,
            "managed_signers": {profile: {"address": address, "nonce": nonces.get(address.lower()),
                "may_send": activated and chain is not None and not blockers and observations["ready"]
                and signer == address.lower() and nonces.get(address.lower(), {}).get("ready", False)
                and not any(row["signer"] is None or row["signer"] == address.lower() for row in pending)}
                for profile, address in settings.managed_signers.items()}}
    return result(blockers[0]["code"] if blockers else "RUNNING" if activated else "HELD", data=data, blockers=blockers, warnings=warnings)


async def reconcile(settings, session, *, transaction_id=None, replacement_hash=None, note=None) -> dict:
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        inspect_database(settings.resolved_db_path)
        web3 = build_web3_client(settings)
        try:
            await chain_readiness(settings, web3)
            reconciler = ledger_reconciler(settings, session, web3)
            if replacement_hash is not None:
                if transaction_id is None or not note:
                    raise LifecycleError("REVIEW_NOTE_REQUIRED", "Replacement proof requires the original transaction ID and a review note.")
                await reconciler.resolve_with_replacement(transaction_id=transaction_id, replacement_hash=replacement_hash, note=note)
            else:
                await reconciler.reconcile(transaction_ids=[transaction_id] if transaction_id is not None else None)
            warnings = []
            if transaction_id is None:
                # Current retained open rounds only; a historical gap stays a
                # scoped operator review, never a restore-time indexing job.
                pairs = []
                for row in reversed(reconciler.operations.kick_repo.list_confirmed_kicks()):
                    if row.get("historical_baseline"):
                        continue
                    pair = (str(row["auction_address"]), str(row["token_address"]))
                    if pair not in pairs and reconciler.operations.kick_repo.latest_confirmed_unclosed_kick(*pair):
                        pairs.append(pair)
                if len(pairs) > 25:
                    warnings.append({"code": "KNOWN_HISTORY_LIMIT", "message": "Checked 25 known open pairs; select older pairs with db repair-auction-rounds for review."})
                errors = await reconciler.operations.discover_direct_settlements(pairs=pairs[:25])
                warnings.extend({"code": error.error_code.upper(), "message": error.error_message} for error in errors)
            pending = reconciler.repository.unresolved()
            blockers = [{"code": "UNRESOLVED_ATTEMPTS", "message": f"{len(pending)} retained attempt(s) still require evidence or review."}] if pending else []
            return result("UNRESOLVED_ATTEMPTS" if pending else "OK", data={"unresolved_transactions": pending}, blockers=blockers, warnings=warnings)
        finally:
            await close_client(web3)


def prepare_restore(settings, session, *, credential_file: Path) -> dict:
    """Called once for each restored DB, before reopening protected endpoints."""
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        protected = {settings.resolved_db_path.resolve(), (settings.resolved_home_path / "activation.json").resolve(),
                     (settings.resolved_home_path / "execution.lock").resolve()}
        if settings.resolved_txn_keystore_path:
            protected.add(settings.resolved_txn_keystore_path.resolve())
        if credential_file.resolve() in protected or credential_file.name.endswith(("-wal", "-shm")):
            raise LifecycleError("INVALID_CREDENTIAL_PATH", "Choose a separate protected credential output file.")
        if credential_file.exists():
            try:
                previous = json.loads(credential_file.read_text())
                if set(previous) != {"interface_version", "api_key"} or previous["interface_version"] != 1:
                    raise ValueError("not a credential output")
            except (OSError, ValueError, TypeError) as exc:
                raise LifecycleError("INVALID_CREDENTIAL_PATH", "Credential output would overwrite an unrelated file.") from exc
        clear_activation(settings.resolved_home_path / "activation.json")
        inspect_database(settings.resolved_db_path)
        key = secrets.token_urlsafe(32)
        now = utcnow_iso()
        # Write protected credentials first. An interruption cannot leave old
        # restored credentials valid after the DB transaction has committed.
        atomic_json(credential_file, {"interface_version": 1, "api_key": key})
        try:
            session.execute(models.app_metadata.update().values(
                notification_baseline_pending=1, recovery_refreshed_at=None,
                recovery_block_number=None, recovery_block_hash=None))
            session.execute(models.tokens.update().values(price_status="STALE", price_error_message="Restored cache requires a normal price refresh"))
            session.execute(models.api_keys.update().where(models.api_keys.c.revoked_at.is_(None)).values(revoked_at=now))
            session.execute(models.api_keys.insert().values(
                label="restore-" + secrets.token_hex(8), key_hash=hashlib.sha256(key.encode()).hexdigest(),
                key_prefix=key[:8], created_at=now))
            session.commit()
        except BaseException:
            session.rollback()
            raise
        return result("RESTORED_AND_HELD", data={"activated": False, "credential_file": str(credential_file),
            "notification_baseline_pending": True, "latest_prices": "stale"},
            warnings=[{"code": "NEW_API_CREDENTIAL", "message": "Use the protected credential file to install fresh API access."}])


async def refresh_recovery(settings, session) -> dict:
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        inspect_database(settings.resolved_db_path)
        session.execute(models.app_metadata.update().values(recovery_refreshed_at=None, recovery_block_number=None, recovery_block_hash=None))
        session.commit()
        scanner = build_scanner_service(settings, session, recovery=True)
        try:
            await chain_readiness(settings, scanner.web3_client)
            scan = await scanner.scan_once()
            errors = [dict(row) for row in session.execute(select(models.scan_item_errors).where(
                models.scan_item_errors.c.run_id == scan.run_id)).mappings()]
            chain = await chain_readiness(settings, scanner.web3_client)
            if scan.status != "SUCCESS" or errors:
                return result("WAITING_FOR_DEPENDENCY", data={"scan": asdict(scan), "read_errors": errors},
                    blockers=[{"code": "INCOMPLETE_REFRESH", "message": "Current-state reads were incomplete; retry recovery refresh after fixing the reported dependency."}])
            session.execute(models.app_metadata.update().values(
                recovery_refreshed_at=utcnow_iso(), recovery_block_number=chain["head_number"], recovery_block_hash=chain["head_hash"]))
            session.commit()
            return result("REFRESHED", data={"scan": asdict(scan), "chain": chain, "prices_refreshed": False,
                "signer_loaded": False, "delivery_enabled": False})
        finally:
            await scanner.close()


async def resume(settings, session) -> dict:
    with execution_lock(settings.resolved_home_path / "execution.lock"):
        clear_activation(settings.resolved_home_path / "activation.json")
        info = inspect_database(settings.resolved_db_path)
        binding = activation_binding(info["database_identity"], settings.chain_id, settings.managed_signers)
        if set(binding["signers"]) != {"scan", "kick"}:
            raise LifecycleError("WRONG_SIGNER", "Declare the scan and kick signing identities before resuming.")
        key_address = configured_signer(settings)
        if set(binding["signers"].values()) != {key_address}:
            raise LifecycleError("WRONG_SIGNER", "The configured keystore does not match the declared native signing identities.")
        web3 = build_web3_client(settings)
        try:
            chain = await chain_readiness(settings, web3)
            pending = await ledger_reconciler(settings, session, web3).reconcile()
            if any(row["signer"] is None or row["status"] == "REVIEW_REQUIRED" for row in pending):
                raise LifecycleError("UNRESOLVED_ATTEMPTS", "Resolve unknown or conflicting retained transaction evidence before resuming.")
            observations = observation_readiness(settings, session)
            if not observations["ready"]:
                raise LifecycleError("INCOMPLETE_REFRESH", observations["reason"])
            baselines, warnings = {}, []
            for address in set(binding["signers"].values()):
                latest = rpc_int(await web3.get_transaction_count(address, "latest"))
                mempool = rpc_int(await web3.get_transaction_count(address, "pending"))
                known = [row for row in pending if row["signer"] == address]
                known_nonces = {row["nonce"] for row in known}
                if mempool < latest or mempool - latest > len(known_nonces) or any(nonce not in known_nonces for nonce in range(latest, mempool)):
                    raise LifecycleError("UNEXPECTED_NONCE", "Unrecorded pending account activity requires review before activation.")
                retained = session.execute(select(func.max(models.transactions.c.nonce)).where(
                    models.transactions.c.chain_id == settings.chain_id, models.transactions.c.signer == address)).scalar()
                if retained is not None and retained >= mempool and not known:
                    raise LifecycleError("UNEXPECTED_NONCE", "RPC account nonce is behind retained finalized attempts.")
                baselines[address] = latest
                if known:
                    warnings.append({"code": "UNRESOLVED_ATTEMPTS", "message": f"{address} remains blocked by {len(known)} retained attempt(s); schedules may observe and reconcile."})
                elif retained is None or latest != retained + 1:
                    warnings.append({"code": "HISTORY_GAP", "message": f"Explicit resume uses current nonce {latest} for {address}; it does not reconstruct missing historical activity."})
            session.commit()
            # Recheck chain freshness after reconciliation and account reads.
            chain = await chain_readiness(settings, web3)
            write_activation(settings.resolved_home_path / "activation.json", {**binding, "nonce_baseline": baselines})
            return result("RUNNING", data={"activated": True, "chain": chain, "nonce_baseline": baselines,
                "timers_enabled": False}, warnings=warnings)
        finally:
            await close_client(web3)
