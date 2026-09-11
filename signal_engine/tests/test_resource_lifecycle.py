"""L1/L2/L3: the process's long-lived handles are shared, and released on shutdown.

Each of these used to be created per call — a new httpx.AsyncClient per API request, a new
SQLite connection per trades.db write. Sharing them is the fix; closing them deliberately is
what keeps the sharing honest, since nothing else reclaims a module-level singleton.
"""

import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_engine import api_client, db, startup


@pytest.fixture(autouse=True)
def _clean_client():
    """Drop the reference rather than awaiting aclose() — this is a sync fixture, and an
    unclosed test client is collected with the loop."""
    api_client._client = None
    yield
    api_client._client = None


class TestSharedHttpClient:
    def test_the_same_client_is_reused_across_calls(self):
        assert api_client._get_client() is api_client._get_client()

    @pytest.mark.asyncio
    async def test_a_closed_client_is_replaced_not_reused(self):
        first = api_client._get_client()
        await api_client.close_client()
        assert api_client._get_client() is not first

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        api_client._get_client()
        await api_client.close_client()
        await api_client.close_client()  # must not raise


class TestShutdownReleasesEverything:
    @pytest.mark.asyncio
    async def test_all_three_handles_are_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(db, "_DB_PATH", str(tmp_path / "trades.db"))
        db.reset_connection()
        conn = db._get_connection()
        api_client._get_client()

        store = MagicMock()
        risk_engine = MagicMock()
        risk_engine._store = store

        await startup._close_resources(risk_engine)

        assert api_client._client is None
        store.close.assert_called_once()
        # Connections are thread-local now, so "closed" is the handle's own state rather
        # than a module global going None — see db._get_connection().
        assert db._all_connections == []
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_a_store_that_cannot_close_does_not_break_shutdown(self):
        store = MagicMock()
        store.close.side_effect = RuntimeError("already gone")
        risk_engine = MagicMock()
        risk_engine._store = store
        await startup._close_resources(risk_engine)  # must not raise

    @pytest.mark.asyncio
    async def test_a_risk_engine_with_no_store_is_fine(self):
        risk_engine = MagicMock()
        risk_engine._store = None
        await startup._close_resources(risk_engine)
