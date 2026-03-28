"""Interactive prompt helpers for auction migration scripts."""

from __future__ import annotations

from tidal.normalizers import normalize_address


def prompt_text(label: str, *, default: str | None = None, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default not in {None, ""} else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default is not None:
            return default
        if not required:
            return ""
        print("Value is required.")


def prompt_address(label: str, *, default: str | None = None) -> str:
    while True:
        value = prompt_text(label, default=default)
        try:
            return normalize_address(value)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid address: {exc}")


def prompt_optional_address(label: str, *, default: str | None = None) -> str | None:
    while True:
        value = prompt_text(label, default=default, required=False)
        if not value:
            return None
        try:
            return normalize_address(value)
        except Exception as exc:  # noqa: BLE001
            print(f"Invalid address: {exc}")


def prompt_uint(label: str, *, default: int | None = None) -> int:
    while True:
        raw = prompt_text(label, default=str(default) if default is not None else None)
        try:
            value = int(raw, 10)
        except ValueError:
            print("Enter a base-10 integer.")
            continue
        if value < 0:
            print("Value must be non-negative.")
            continue
        return value


def prompt_bool(label: str, *, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Enter y or n.")
