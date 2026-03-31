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

BroadcastOption = Annotated[
    bool,
    typer.Option(
        "--broadcast",
        help="Broadcast the transaction instead of running a preview.",
    ),
]

BypassConfirmationOption = Annotated[
    bool,
    typer.Option(
        "--bypass-confirmation",
        help="Skip interactive confirmation before broadcasting.",
    ),
]

VerboseOption = Annotated[
    bool,
    typer.Option(
        "--verbose",
        help="Show extra diagnostic detail.",
    ),
]

IntervalOption = Annotated[
    int | None,
    typer.Option(
        "--interval-seconds",
        min=1,
        help="Seconds between daemon cycles.",
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

AccountOption = Annotated[
    str | None,
    typer.Option(
        "--account",
        help="Use a keystore from ~/.foundry/keystores by filename.",
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

SenderOption = Annotated[
    str | None,
    typer.Option(
        "--sender",
        help="Execution address for preview and broadcast.",
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

LimitOption = Annotated[
    int | None,
    typer.Option(
        "--limit",
        min=1,
        help="Limit the number of selected candidates.",
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
