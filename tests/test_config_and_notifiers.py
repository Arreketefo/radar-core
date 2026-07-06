import json
from datetime import date

import httpx
from pydantic_settings import BaseSettings

from radar_core.config import csv_dates, csv_set, radar_settings_config
from radar_core.notifiers import TelegramTransport, escape_markdown


def test_csv_helpers():
    assert csv_set("es, pt,") == {"ES", "PT"}
    assert csv_set("") == set()
    assert csv_dates("2026-06-24, 2027-04-15") == {date(2026, 6, 24), date(2027, 4, 15)}


def test_settings_config_blank_env_means_default(monkeypatch):
    class S(BaseSettings):
        model_config = radar_settings_config("TESTRADAR_")
        cap: float = 600.0

    monkeypatch.setenv("TESTRADAR_CAP", "")
    assert S(_env_file=None).cap == 600.0
    monkeypatch.setenv("TESTRADAR_CAP", "250")
    assert S(_env_file=None).cap == 250.0


def test_escape_markdown():
    assert escape_markdown("a_b*c[d`e") == r"a\_b\*c\[d\`e"


async def test_telegram_transport_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    t = TelegramTransport("tok", "42")
    t._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await t.send_text("hola *mundo*")
    assert captured["chat_id"] == "42"
    assert captured["parse_mode"] == "Markdown"
    assert captured["disable_web_page_preview"] is True
