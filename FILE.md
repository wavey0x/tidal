# Curve Quote Plan

## Goal

1. The CLI should allow an explicit bypass for the Curve-quote requirement.
2. The UI deploy flow should not be blocked just because Curve did not return a quote.

## Plan

1. Add a txn CLI override flag.
Add a paired flag like `--require-curve-quote/--allow-missing-curve-quote` to `tidal txn` and `tidal txn daemon`.
Default it to `None` so config still wins unless the operator overrides it for that run.

2. Thread the override through the txn service wiring.
Pass the optional override from `tidal/cli.py` into `tidal/runtime.py`, then into `AuctionKicker(require_curve_quote=...)`.
Do this as an explicit parameter instead of mutating settings in place.

3. Leave the core kicker rule in place.
Keep the current guard in `tidal/transaction_service/kicker.py` that fails when `require_curve_quote=True` and Curve is unavailable.
The behavior is already covered by unit tests, so the change is mainly about exposing a clean operator override in the CLI.

4. Remove the deploy-side hard failure on missing Curve.
In `/Users/wavey/yearn/wavey-api/services/tidal.py`, stop raising `409` from `build_strategy_deploy_tx()` when Curve did not return an amount but another provider produced a usable quote.
The deploy inference path already has `amountOutRaw` and computes `starting_price` from that, so no deeper fallback design should be required.

5. Preserve deploy visibility as a warning instead of an error.
Return the deploy payload even when Curve is unavailable, and include non-blocking metadata such as:
- `inference.curveQuoteAvailable`
- `inference.curveQuoteStatus`
- `warnings`

6. Decide how much config to keep in the external API.
If we still want an emergency strict mode, keep the existing `deploy_require_curve_quote` wiring in `/Users/wavey/yearn/wavey-api/config.py` and `/Users/wavey/yearn/wavey-api/app.py`, but default it to `False`.
If the policy is settled, remove the deploy Curve gate entirely.

7. Make the UI tolerant of warnings.
The deploy request path in `ui/src/App.jsx` should continue to treat a successful deploy payload as success even if it includes a warning about missing Curve.
Optionally surface that warning in the deploy confirmation modal, but do not block the deploy CTA.

8. Test the txn override.
Add CLI coverage for the new override flag on both `tidal txn` and `tidal txn daemon`.
Rely on the existing kicker unit tests for strict vs non-strict quote behavior.

9. Test the deploy flow end to end.
Add external API coverage for the case where Curve is unavailable but another provider succeeds.
Manually verify that the UI no longer shows `Curve quote unavailable for deploy inference (status: error)` as a blocking deploy failure.

## Recommended Policy

- Txn path: strict by default, operator override via CLI flag.
- Deploy/UI path: warning-only, not blocking.
