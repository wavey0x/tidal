from copy import deepcopy
from eth_abi import encode
from eth_utils import keccak
from hexbytes import HexBytes
import pytest
from sqlalchemy import select

from tidal.constants import YEARN_AUCTION_REQUIRED_GOVERNANCE_ADDRESS as HANDLER
from tidal.legacy_evidence import legacy_sweep_evidence
from tidal.operation_reconciler import DecodedReceipt, DecodedSettlement, DecodedSweep
from tidal.persistence import models
from tests.unit.test_legacy_transaction_reconcile import seed, convert
from tests.unit.test_managed_execution import runtime, AUCTION, TOKEN, SOURCE

KICKER = '0x846475a1b97ac57861813206749c1b0f592383ef'
AMOUNT = 123456789012345678901234567890

def evidence():
    def log(address, signature, indexed, amount=None):
        return {'address': address, 'topics': [keccak(text=signature), *[bytes.fromhex(item[2:]).rjust(32, b'\0') for item in indexed]],
                'data': b'' if amount is None else amount.to_bytes(32)}
    transaction = {'legacy': 1, 'chain_id': 1, 'operation': 'sweep-and-settle', 'to_address': KICKER,
                   'data': HexBytes(keccak(text='sweepAndSettle(address,address)')[:4] + encode(['address','address'], [AUCTION,TOKEN]))}
    receipt = {'logs': [
        log(TOKEN, 'Transfer(address,address,uint256)', [AUCTION,HANDLER], AMOUNT),
        log(TOKEN, 'Transfer(address,address,uint256)', [HANDLER,SOURCE], AMOUNT),
        log(AUCTION, 'AuctionSettled(address)', [TOKEN]),
        log(KICKER, 'SweepAndSettled(address,address)', [AUCTION,TOKEN]),
    ]}
    return transaction, receipt

def test_retired_sweep_uses_exact_transfer_amount_and_retains_receipt():
    transaction, receipt = evidence()
    before = deepcopy(receipt)
    assert legacy_sweep_evidence(transaction, receipt) == (DecodedSweep(AUCTION,TOKEN,AMOUNT),)
    assert receipt == before

@pytest.mark.parametrize('change', ['native','chain','target','selector','padding','pair','missing_close','missing_wrapper','wrong_emitter','wrong_token','unequal_transfers','missing_transfer','duplicate_transfer','order','removed'])
def test_legacy_sweep_requires_complete_unambiguous_bound_evidence(change):
    transaction, receipt = evidence()
    logs = receipt['logs']
    if change == 'native': transaction['legacy'] = 0
    elif change == 'chain': transaction['chain_id'] = 2
    elif change == 'target': transaction['to_address'] = SOURCE
    elif change == 'selector': transaction['data'] = b'\0' * 68
    elif change == 'padding': transaction['data'] = bytes(transaction['data'][:4]) + b'\xff' * 64
    elif change == 'pair': logs[3]['topics'][2] = bytes.fromhex(SOURCE[2:]).rjust(32,b'\0')
    elif change == 'missing_close': logs.pop(2)
    elif change == 'missing_wrapper': logs.pop(3)
    elif change == 'wrong_emitter': logs[3]['address'] = SOURCE
    elif change == 'wrong_token': logs[0]['address'] = SOURCE
    elif change == 'unequal_transfers': logs[1]['data'] = (AMOUNT-1).to_bytes(32)
    elif change == 'missing_transfer': logs.pop(0)
    elif change == 'duplicate_transfer': logs.insert(0,deepcopy(logs[0]))
    elif change == 'order': logs[0],logs[1] = logs[1],logs[0]
    elif change == 'removed': logs[3]['removed'] = True
    assert legacy_sweep_evidence(transaction, receipt) == ()

@pytest.mark.asyncio
async def test_finalized_legacy_sweep_updates_only_its_linked_operation_without_sending(runtime):
    transaction_id, action, tx = seed(runtime, 'sweep-and-settle')
    ids = convert(runtime, transaction_id, action, tx)
    retained, receipt = evidence()
    runtime.session.execute(models.transactions.update().where(models.transactions.c.id==transaction_id).values(
        to_address=KICKER, data='0x'+bytes(retained['data']).hex()))
    runtime.session.commit()
    runtime.signer.last_transaction.update(to=KICKER, data=retained['data'])
    original_receipt = runtime.rpc.get_transaction_receipt
    async def actual_receipt(tx_hash, *, timeout_seconds):
        result = await original_receipt(tx_hash, timeout_seconds=timeout_seconds)
        return {**result, 'to':KICKER, 'logs':receipt['logs']}
    runtime.rpc.get_transaction_receipt = actual_receipt
    runtime.operation_reconciler.decode_receipt_fn = lambda *_: DecodedReceipt(settlements=(DecodedSettlement(AUCTION,TOKEN),))
    await runtime.executor.reconciler.reconcile(transaction_ids=[transaction_id])
    assert runtime.executor.repository.get(transaction_id)['status'] == 'CONFIRMED'
    row = runtime.session.execute(select(models.kick_txs).where(models.kick_txs.c.id.in_(ids))).mappings().one()
    assert row['sell_amount'] == str(AMOUNT)
    assert runtime.signer.calls == runtime.rpc.sends == 0
