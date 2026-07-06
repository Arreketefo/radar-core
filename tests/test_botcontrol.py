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
