"""Settings conventions shared by every radar."""

from __future__ import annotations

from datetime import date

from pydantic_settings import SettingsConfigDict


def radar_settings_config(env_prefix: str) -> SettingsConfigDict:
    """Standard model_config for a radar's Settings class.

    env_ignore_empty: a blank value in .env (PREFIX_X=) means "use the
    default" instead of crashing numeric fields at startup.
    """
    return SettingsConfigDict(
        env_prefix=env_prefix, env_file=".env", extra="ignore", env_ignore_empty=True
    )


def csv_set(raw: str) -> set[str]:
    """'es, pt,' -> {'ES', 'PT'} — comma-separated config values."""
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def csv_dates(raw: str) -> set[date]:
    """'2026-06-24, 2027-04-15' -> {date(...), ...}"""
    return {date.fromisoformat(item.strip()) for item in raw.split(",") if item.strip()}
