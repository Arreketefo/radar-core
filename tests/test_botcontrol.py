import json

import httpx
import pytest
from pydantic_settings import BaseSettings

from radar_core.botcontrol import (
    BotCommander,
    Override,
    apply_overrides,
    boolean,
    float_range,
    is_paused,
)
from radar_core.store import BaseStore, open_db


class Cfg(BaseSettings):
    budget_cap: float = 600.0
    discovery: bool = True


SPECS = {
    "budget_cap": Override("budget_cap", float_range(1, 10000), "presupuesto EUR"),
    "discovery": Override("discovery", boolean, "on/off"),
}


@pytest.fixture
async def store(tmp_path):
    async with open_db(str(tmp_path / "t.db")) as conn:
        await conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE config_overrides (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            """
        )
        yield BaseStore(conn)


def commander(store: BaseStore, handler=None) -> BotCommander:
    return BotCommander(
        "tok", {"42"}, store, "testapp", SPECS,
        domain_commands={"eco": handler} if handler else None,
    )


def test_apply_overrides_validates_and_ignores_bad():
    cfg = Cfg()
    out = apply_overrides(cfg, {"budget_cap": "250", "discovery": "off"}, SPECS)
    assert out.budget_cap == 250.0 and out.discovery is False
    # inválidos o no whitelisteados: ignorados sin romper
    out = apply_overrides(cfg, {"budget_cap": "999999", "hack": "1"}, SPECS)
    assert out.budget_cap == 600.0


async def test_set_reset_config_flow(store: BaseStore):
    bot = commander(store)
    reply = await bot._handle("/set budget_cap 250")
    assert "250" in reply and "✓" in reply
    assert (await store.get_overrides()) == {"budget_cap": "250"}

    reply = await bot._handle("/config")
    assert "budget_cap = 250" in reply

    assert "fuera de rango" in await bot._handle("/set budget_cap 999999")
    assert "no ajustable" in await bot._handle("/set db_path /tmp/evil")

    await bot._handle("/reset budget_cap")
    assert await store.get_overrides() == {}


async def test_pause_resume(store: BaseStore):
    bot = commander(store)
    assert not await is_paused(store)
    await bot._handle("/pausa")
    assert await is_paused(store)
    reply = await bot._handle("/status")
    assert "EN PAUSA" in reply
    await bot._handle("/reanuda")
    assert not await is_paused(store)


async def test_domain_command_and_unknown(store: BaseStore):
    async def eco(args: str) -> str:
        return f"eco:{args}"

    bot = commander(store, handler=eco)
    assert await bot._handle("/eco hola") == "eco:hola"
    assert "desconocido" in await bot._handle("/nada")


async def test_version_reports_build_info(store: BaseStore, monkeypatch):
    bot = BotCommander("tok", {"42"}, store, "testapp", SPECS, version="abc123 2026-07-06")
    assert await bot._handle("/version") == "testapp: abc123 2026-07-06"
    # sin version explícita cae al env BUILD_INFO o "dev"
    monkeypatch.delenv("BUILD_INFO", raising=False)
    assert "dev" in await commander(store)._handle("/version")


def digest_bot(store: BaseStore, sent: list[dict], **kwargs) -> BotCommander:
    async def summary() -> str:
        return "24h: 3 observaciones, 1 alerta"

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    kwargs.setdefault("digest_hour", 0)
    bot = BotCommander("tok", {"42"}, store, "testapp", SPECS,
                       digest_provider=summary, **kwargs)
    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return bot


async def test_digest_toggle_and_daily_send(store: BaseStore):
    sent: list[dict] = []
    bot = digest_bot(store, sent)

    # apagado por defecto: no envía
    await bot._digest_once()
    assert sent == []

    await store.set_meta("digest_enabled", "1")
    await bot._digest_once()
    assert len(sent) == 1 and "24h: 3 observaciones" in sent[0]["text"]
    # mismo día: no repite
    await bot._digest_once()
    assert len(sent) == 1

    assert "desactivado" in await bot._handle("/digest off")
    # una app sin provider lo dice claro
    assert "no ofrece" in await commander(store)._handle("/digest on")


async def test_digest_on_after_hour_defers_to_tomorrow(store: BaseStore):
    sent: list[dict] = []
    bot = digest_bot(store, sent)  # digest_hour=0: la hora de hoy YA pasó siempre
    assert "activado" in await bot._handle("/digest on")
    await bot._digest_once()
    assert sent == []  # marcado como enviado hoy → primer digest mañana


async def test_digest_default_on_and_hour_clamped(store: BaseStore):
    sent: list[dict] = []
    # digest_default=True: activo sin /digest on (spec de sospechosos de ojeador)
    bot = digest_bot(store, sent, digest_default=True)
    await bot._digest_once()
    assert len(sent) == 1
    # /digest off lo apaga aunque el default sea on
    await bot._handle("/digest off")
    assert not await bot._digest_enabled()
    # hora fuera de rango se recorta a 23 en vez de matar el digest en silencio
    assert digest_bot(store, [], digest_hour=24)._digest_hour == 23


async def test_digest_partial_send_failure_no_duplicates(store: BaseStore):
    async def summary() -> str:
        return "resumen"

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        chat = str(json.loads(request.content)["chat_id"])
        calls.append(chat)
        if chat == "13":
            return httpx.Response(429, json={"ok": False})
        return httpx.Response(200, json={"ok": True})

    bot = BotCommander("tok", {"42", "13"}, store, "testapp", SPECS,
                       digest_provider=summary, digest_hour=0)
    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await store.set_meta("digest_enabled", "1")

    await bot._digest_once()  # el fallo del chat 13 no debe romper ni repetir
    assert sorted(calls) == ["13", "42"]
    await bot._digest_once()  # mismo día: nadie recibe duplicado
    assert sorted(calls) == ["13", "42"]


async def test_poll_auth_and_offset(store: BaseStore):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            assert request.url.params["offset"] == "1"
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 7, "message": {"chat": {"id": 42},
                                             "text": "/set budget_cap 300"}},
                {"update_id": 8, "message": {"chat": {"id": 999},
                                             "text": "/set budget_cap 1"}},
            ]})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    bot = commander(store)
    bot._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await bot._poll_once()

    # el chat autorizado aplicó; el intruso fue ignorado
    assert (await store.get_overrides()) == {"budget_cap": "300"}
    assert len(sent) == 1 and sent[0]["chat_id"] == "42"
    # offset persistido: no se reprocesa tras reinicio
    assert await store.get_meta("telegram_offset") == "8"
