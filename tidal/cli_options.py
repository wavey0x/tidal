"""Reusable Typer option definitions."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Optional runtime config file path.",
    ),
]

JsonOption = Annotated[
    bool,
    typer.Option(
        "--json",
        help="Emit machine-readable JSON output.",
    ),
]

NoConfirmationOption = Annotated[
    bool,
    typer.Option(
        "--no-confirmation",
        help="Skip interactive confirmation before sending.",
    ),
]

HeadlessOption = Annotated[
    bool,
    typer.Option(
        "--headless",
        help="Run unattended with compact line logs for service automation.",
    ),
]

AutoSettleOption = Annotated[
    bool,
    typer.Option(
        "--auto-settle",
        help="Enable scan-side auction settlement for this run.",
    ),
]

AutoEnableTokensOption = Annotated[
    bool,
    typer.Option(
        "--auto-enable-tokens",
        help="Enable scan-side auction token enablement for this run.",
    ),
]

VerboseOption = Annotated[
    bool,
    typer.Option(
        "--verbose",
        help="Show extra diagnostic detail.",
    ),
]

KeystoreOption = Annotated[
    Path | None,
    typer.Option(
        "--keystore",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Use the keystore at the given path.",
    ),
]

PasswordFileOption = Annotated[
    Path | None,
    typer.Option(
        "--password-file",
        exists=True,
        file_okay=True,
        dir_okay=False,
        help="Path to a file containing the keystore password.",
    ),
]

SourceTypeOption = Annotated[
    str | None,
    typer.Option(
        "--source-type",
        help="Filter by source type: strategy or fee-burner.",
    ),
]

SourceAddressOption = Annotated[
    str | None,
    typer.Option(
        "--source",
        help="Filter to a specific source address.",
    ),
]

AuctionAddressOption = Annotated[
    str | None,
    typer.Option(
        "--auction",
        help="Filter to a specific auction address.",
    ),
]

TokenAddressOption = Annotated[
    str | None,
    typer.Option(
        "--token",
        help="Filter to a specific sell token address.",
    ),
]

LimitOption = Annotated[
    int | None,
    typer.Option(
        "--limit",
        min=1,
        help="Limit the number of selected candidates.",
    ),
]

MinUsdValueOption = Annotated[
    float | None,
    typer.Option(
        "--min-usd-value",
        min=0,
        help="Override the minimum cached USD value for kick candidate selection.",
    ),
]

ApiBaseUrlOption = Annotated[
    str | None,
    typer.Option(
        "--api-base-url",
        envvar="TIDAL_API_BASE_URL",
        help="Base URL for the Tidal control-plane API.",
    ),
]

ApiKeyOption = Annotated[
    str | None,
    typer.Option(
        "--api-key",
        envvar="TIDAL_API_KEY",
        help="API key for the Tidal control-plane API.",
    ),
]
