from datetime import date

from radar_core.store import BaseStore, iso_date, open_db, utcnow_str


async def test_meta_roundtrip_and_prune(tmp_path):
    async with open_db(str(tmp_path / "t.db")) as conn:
        await conn.executescript(
            """
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE things (id INTEGER PRIMARY KEY, at TEXT NOT NULL);
            """
        )
        store = BaseStore(conn)

        assert await store.get_meta("k") is None
        await store.set_meta("k", "v1")
        await store.set_meta("k", "v2")
        assert await store.get_meta("k") == "v2"

        await conn.execute("INSERT INTO things (at) VALUES (?)", (utcnow_str(),))
        await conn.execute(
            "INSERT INTO things (at) VALUES (datetime('now', '-60 days'))"
        )
        await store.commit()
        assert await store.prune_older_than("things", "at", 45) == 1
        assert await store.prune_older_than("things", "at", 45) == 0


def test_iso_date():
    assert iso_date(date(2026, 8, 1)) == "2026-08-01"
    assert iso_date(None) is None
