# Tidal Kick Automation Plan

## Goal

Run unattended kick attempts from the server every 15 minutes.

Policy:

- Strategy sources require a usable Curve quote.
- Fee-burner sources may run without a Curve quote.
- Strategy sources use the scanner's persisted kick guard status; killed Curve gauges are skipped.
- Minimum sell-side liquidity is `$250`.

## Server Config

Set this in the server checkout's `config/server.yaml`:

```yaml
txn_usd_threshold: 250
txn_require_curve_quote: true
```

Remove any `TXN_USD_THRESHOLD` environment override, or set it to `250` too.

## Headless Kick Run

Use headless mode for timer-driven execution from the server checkout's virtualenv:

```bash
/home/wavey/tidal/venv/bin/tidal kick run --headless
```

`--headless`:

- runs unattended without a confirmation prompt
- keeps preparing and sending candidates until the current filtered ready set is cleared, skipped, or blocked
- emits plain line-oriented logs, not Rich panels or spinners
- keeps routine logs compact: start, skips, broadcasts, final summary
- treats normal no-op outcomes as success: no ready candidates, prepare skips, stale prepared tx skips
- keeps real failures nonzero: config errors, API errors, signing errors, broadcast errors

Example log shape:

```text
kick.run.start source_type=strategy require_curve=true
kick.candidate.skip token=CRV auction=0x... reason="curve quote unavailable"
kick.broadcast tx_hash=0x... receipt_status=CONFIRMED
kick.run.complete status=ok sent=4 skipped=3
```

## systemd Service

No wrapper script, `uv run`, or special systemd success codes are needed.
The two `ExecStart=` commands run sequentially, and systemd will not start a second instance of
the same oneshot service while the first is still active.

Create `/etc/systemd/system/tidal-kick.service`:

```ini
[Unit]
Description=Tidal kick automation cycle
Wants=network-online.target
After=network-online.target tidal-api.service

[Service]
Type=oneshot
User=wavey
Group=wavey
WorkingDirectory=/home/wavey/tidal
EnvironmentFile=/etc/tidal/kick.env
ExecStart=/home/wavey/tidal/venv/bin/tidal kick run --headless --source-type strategy --require-curve
ExecStart=/home/wavey/tidal/venv/bin/tidal kick run --headless --source-type fee-burner --no-require-curve
TimeoutStartSec=12min
```

Do not include `--allow-killed-gauge` in automation. That flag is only for a deliberate manual
override after reviewing the warning in the UI.

If the API is not local, remove `tidal-api.service` from `After=`.

Create `/etc/systemd/system/tidal-kick.timer`:

```ini
[Unit]
Description=Run Tidal kick automation every 15 minutes

[Timer]
OnBootSec=2min
OnCalendar=*-*-* *:00/15:00
AccuracySec=30s
Persistent=true
Unit=tidal-kick.service

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now tidal-kick.timer
```

If the virtualenv lives somewhere else, update both `ExecStart=` paths. Prefer the absolute
virtualenv binary path over relying on systemd `PATH`.

## Environment

Use `/etc/tidal/kick.env`:

```dotenv
TIDAL_HOME=/var/lib/tidal
TIDAL_API_BASE_URL=http://127.0.0.1:8787
TIDAL_API_KEY=<operator-api-key>
RPC_URL=<mainnet-rpc-url>
TXN_KEYSTORE_PATH=/var/lib/tidal/operator-keystore.json
TXN_KEYSTORE_PASSPHRASE=<keystore-passphrase>
```

## Rollout Checks

```bash
sudo systemctl start tidal-kick.service
journalctl -u tidal-kick.service -n 100 --no-pager
/home/wavey/tidal/venv/bin/tidal logs kicks --limit 20
```

Keep scanner automation running separately. Kick selection depends on fresh cached balances and
prices.
