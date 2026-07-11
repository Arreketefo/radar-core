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
import os
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from radar_core.store import BaseStore, utcnow_str

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_offset"
PAUSED_KEY = "paused"
DIGEST_KEY = "digest_enabled"
DIGEST_SENT_KEY = "digest_last_sent"

# El digest se reevalúa con esta cadencia: llega como muy tarde 15 min después
# de la hora configurada, sin cron adicional en la VM.
_DIGEST_POLL_S = 900

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


def id_and_value(args: str, name: str) -> tuple[int, float]:
    """Parseo '<id> <valor>' de comandos de dominio (/tope 3 350)."""
    try:
        raw_id, raw_value = args.split()
        return int(raw_id), float(raw_value)
    except ValueError as exc:
        raise ValueError(f"uso: /{name} <id> <valor>") from exc


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


async def skip_tick_if_paused(store: BaseStore, last_tick_key: str) -> bool:
    """Guard de tick para /pausa: pausado != muerto, así que refresca la
    marca de frescura que lee el healthcheck. True = omitir el tick."""
    if not await is_paused(store):
        return False
    await store.set_meta(last_tick_key, utcnow_str())
    log.info("en pausa (/reanuda en Telegram): tick omitido")
    return True


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
        digest_provider: Callable[[], Awaitable[str]] | None = None,
        digest_hour: int = 9,
        digest_default: bool = False,
        version: str | None = None,
        help_text: str | None = None,
    ) -> None:
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._allowed = allowed_chat_ids
        self._store = store
        self._app = app_name
        self._overrides = overrides
        self._domain = domain_commands or {}
        self._status_provider = status_provider
        self._digest_provider = digest_provider
        # Un valor fuera de 0-23 haría `now.hour < hora` siempre cierto y el
        # digest moriría en silencio: se recorta, no se confía en el .env.
        self._digest_hour = min(max(digest_hour, 0), 23)
        self._digest_default = digest_default
        self._version = version or os.environ.get("BUILD_INFO", "dev")
        # Ayuda curada opcional: si la app la aporta, /ayuda la usa tal cual en
        # vez del listado genérico. Backward-compatible (None = comportamiento
        # anterior), así que las apps que no la pasan no cambian.
        self._help_text = help_text
        self._client = httpx.AsyncClient(timeout=70.0)

    def background_tasks(self) -> tuple[Callable[[], Coroutine[None, None, None]], ...]:
        """Tasks de fondo que el daemon debe lanzar junto al scheduler. La
        lista vive aquí: las apps no tienen que conocer los bucles internos."""
        return (self.run, self.run_digest)

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

    async def run_digest(self) -> None:
        """Bucle del digest diario; task de fondo hermano de run(). Sin
        provider registrado no hace nada (la app no ofrece digest)."""
        if self._digest_provider is None:
            return
        while True:
            try:
                await self._digest_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("bot: error del digest (%s); reintento en %ds",
                            exc, _DIGEST_POLL_S)
            await asyncio.sleep(_DIGEST_POLL_S)

    async def _digest_enabled(self) -> bool:
        value = await self._store.get_meta(DIGEST_KEY)
        if value is None:
            return self._digest_default
        return value == "1"

    async def _digest_once(self) -> None:
        if self._digest_provider is None:
            return
        if not await self._digest_enabled():
            return
        now = datetime.now(UTC)
        if now.hour < self._digest_hour:
            return
        today = now.strftime("%Y-%m-%d")
        if await self._store.get_meta(DIGEST_SENT_KEY) == today:
            return
        summary = await self._digest_provider()
        # At-most-once: el día queda marcado ANTES de enviar y cada chat va en
        # su propio try — un 429/timeout parcial no puede provocar una tormenta
        # de duplicados en los chats ya servidos (reintento cada 15 min).
        await self._store.set_meta(DIGEST_SENT_KEY, today)
        for chat_id in self._allowed:
            try:
                await self._send(chat_id, f"{self._app} — digest {today}\n{summary}")
            except Exception as exc:
                log.warning("digest: envío a chat %s falló (%s)", chat_id, exc)

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
        if command == "version":
            return f"{self._app}: {self._version}"
        if command == "digest":
            return await self._digest_command(args)
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

    async def _digest_command(self, args: str) -> str:
        if self._digest_provider is None:
            return f"{self._app} no ofrece digest diario"
        choice = args.strip().lower()
        if choice == "on":
            await self._store.set_meta(DIGEST_KEY, "1")
            now = datetime.now(UTC)
            if now.hour >= self._digest_hour:
                # La hora de hoy ya pasó: sin esta marca el primer digest
                # saldría "ya mismo" en vez de mañana a la hora prometida.
                await self._store.set_meta(
                    DIGEST_SENT_KEY, now.strftime("%Y-%m-%d")
                )
            return (f"digest diario activado (~{self._digest_hour:02d}:00 UTC, "
                    "primer envío mañana si la hora de hoy ya pasó); "
                    "/digest off para quitarlo")
        if choice == "off":
            await self._store.set_meta(DIGEST_KEY, "0")
            return "digest diario desactivado"
        state = "on" if await self._digest_enabled() else "off"
        return (f"digest diario: {state} (~{self._digest_hour:02d}:00 UTC) — "
                "uso: /digest on|off")

    def _help(self) -> str:
        if self._help_text:
            return self._help_text
        common = (
            "/status /config /set <k> <v> /reset <k> /pausa /reanuda "
            "/digest /version /ayuda"
        )
        domain = " ".join(f"/{name}" for name in sorted(self._domain)) or "(ninguno)"
        return f"{self._app} — comandos:\ncomunes: {common}\ndominio: {domain}"

    async def aclose(self) -> None:
        await self._client.aclose()
