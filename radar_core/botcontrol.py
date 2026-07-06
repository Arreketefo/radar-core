"""Control por Telegram para los radares (spec: radar-ops §7).

Un bot POR app (dos consumidores de getUpdates sobre un token = 409). Los
comandos comunes viven aquí; cada app registra sus comandos de dominio y su
whitelist de overrides. Los overrides se persisten en config_overrides y las
apps los fusionan sobre su Settings EN CADA TICK (config en caliente).

Seguridad: solo chat_ids autorizados; los comandos jamás ejecutan shell ni
tocan ficheros — mutan la whitelist validada y las tablas de dominio.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from radar_core.store import BaseStore

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_offset"
PAUSED_KEY = "paused"

DomainHandler = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class Override:
    """Entrada de la whitelist: clave ajustable + parseo/validación."""

    key: str
    parse: Callable[[str], object]   # lanza ValueError si es inválido
    describe: str                    # ayuda mostrada en /set


def float_range(lo: float, hi: float) -> Callable[[str], object]:
    def parse(raw: str) -> object:
        value = float(raw)
        if not lo <= value <= hi:
            raise ValueError(f"fuera de rango [{lo}, {hi}]")
        return value
    return parse


def int_range(lo: int, hi: int) -> Callable[[str], object]:
    def parse(raw: str) -> object:
        value = int(raw)
        if not lo <= value <= hi:
            raise ValueError(f"fuera de rango [{lo}, {hi}]")
        return value
    return parse


def boolean(raw: str) -> object:
    lowered = raw.strip().lower()
    if lowered in ("true", "on", "si", "sí", "1"):
        return True
    if lowered in ("false", "off", "no", "0"):
        return False
    raise ValueError("usa on/off")


def text(raw: str) -> object:
    return raw.strip()


def apply_overrides(settings: Any, raw: dict[str, str], specs: dict[str, Override]) -> Any:
    """Settings efectivo = base (.env) ⊕ overrides válidos. Los inválidos
    (p. ej. tras un cambio de código) se ignoran con aviso, nunca rompen."""
    updates: dict[str, object] = {}
    for key, value in raw.items():
        spec = specs.get(key)
        if spec is None:
            continue
        try:
            updates[key] = spec.parse(value)
        except (ValueError, TypeError) as exc:
            log.warning("override %s=%r invalido (%s); ignorado", key, value, exc)
    return settings.model_copy(update=updates) if updates else settings


async def is_paused(store: BaseStore) -> bool:
    return await store.get_meta(PAUSED_KEY) == "1"


class BotCommander:
    def __init__(
        self,
        bot_token: str,
        allowed_chat_ids: set[str],
        store: BaseStore,
        app_name: str,
        overrides: dict[str, Override],
        domain_commands: dict[str, DomainHandler] | None = None,
        status_provider: Callable[[], Awaitable[str]] | None = None,
    ) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._allowed = allowed_chat_ids
        self._store = store
        self._app = app_name
        self._overrides = overrides
        self._domain = domain_commands or {}
        self._status_provider = status_provider
        self._client = httpx.AsyncClient(timeout=70.0)

    async def run(self) -> None:
        """Bucle de long-polling; pensado como task de fondo del daemon."""
        log.info("%s: comandos de Telegram activos", self._app)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("bot: error de polling (%s); reintento en 15s", exc)
                await asyncio.sleep(15)

    async def _poll_once(self) -> None:
        offset = int(await self._store.get_meta(OFFSET_KEY) or 0)
        resp = await self._client.get(
            f"{self._base}/getUpdates",
            params={"offset": offset + 1, "timeout": 50},
        )
        resp.raise_for_status()
        for update in resp.json().get("result", []):
            await self._store.set_meta(OFFSET_KEY, str(update["update_id"]))
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text_in = str(message.get("text") or "").strip()
            if not text_in:
                continue
            if chat_id not in self._allowed:
                log.warning("bot: comando de chat NO autorizado %s ignorado", chat_id)
                continue
            reply = await self._handle(text_in)
            if reply:
                await self._send(chat_id, reply)

    async def _send(self, chat_id: str, reply_text: str) -> None:
        resp = await self._client.post(
            f"{self._base}/sendMessage",
            json={"chat_id": chat_id, "text": reply_text[:4000]},
        )
        resp.raise_for_status()

    async def _handle(self, text_in: str) -> str:
        parts = text_in.split(maxsplit=1)
        command = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if command == "ayuda":
            return self._help()
        if command == "status":
            base = f"{self._app}: " + ("EN PAUSA" if await is_paused(self._store) else "activo")
            if self._status_provider is not None:
                base += "\n" + await self._status_provider()
            return base
        if command == "config":
            raw = await self._store.get_overrides()
            lines = [f"{self._app} — overrides activos:"]
            lines += [f"  {k} = {v}" for k, v in sorted(raw.items())]
            if not raw:
                lines.append("  (ninguno)")
            return "\n".join(lines)
        if command == "set":
            return await self._set(args)
        if command == "reset":
            key = args.strip()
            if not key:
                return "uso: /reset <clave>"
            await self._store.delete_override(key)
            return f"{key}: override eliminado (vuelve al .env)"
        if command == "pausa" and not args:
            await self._store.set_meta(PAUSED_KEY, "1")
            return f"{self._app} en pausa (el daemon sigue vivo; /reanuda para volver)"
        if command == "reanuda" and not args:
            await self._store.set_meta(PAUSED_KEY, "0")
            return f"{self._app} reanudado"
        handler = self._domain.get(command)
        if handler is not None:
            try:
                return await handler(args)
            except (ValueError, TypeError) as exc:
                return f"error: {exc}"
        return f"comando desconocido: /{command} — prueba /ayuda"

    async def _set(self, args: str) -> str:
        if not args:
            keys = "\n".join(
                f"  {o.key} — {o.describe}" for o in self._overrides.values()
            )
            return f"claves ajustables:\n{keys}\nuso: /set <clave> <valor>"
        try:
            key, value = args.split(maxsplit=1)
        except ValueError:
            return "uso: /set <clave> <valor>"
        spec = self._overrides.get(key)
        if spec is None:
            return f"clave no ajustable: {key} (/set para ver la lista)"
        try:
            parsed = spec.parse(value)
        except (ValueError, TypeError) as exc:
            return f"valor invalido para {key}: {exc}"
        previous = (await self._store.get_overrides()).get(key, "(.env)")
        await self._store.set_override(key, str(value))
        return f"{key}: {previous} → {parsed} ✓ (aplica en el proximo tick)"

    def _help(self) -> str:
        common = (
            "/status /config /set <k> <v> /reset <k> /pausa /reanuda /ayuda"
        )
        domain = " ".join(f"/{name}" for name in sorted(self._domain)) or "(ninguno)"
        return f"{self._app} — comandos:\ncomunes: {common}\ndominio: {domain}"

    async def aclose(self) -> None:
        await self._client.aclose()
