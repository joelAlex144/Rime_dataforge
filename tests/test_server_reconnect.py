"""A dead Rime socket must not take the session down with it.

traces/session_web-8170fb81.jsonl: after a four-hour pause Rime dropped the
/ws3 socket ("keepalive ping timeout; no close frame received"). The status
cell still said "open 14367 s", and every play produced a reader_error until
the process was restarted. Now the adapter reports whether its socket is
alive, ensure_provider() reconnects when it is not, and a unit whose
synthesis failed before any byte was sent is said again on the new socket.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

os.environ["TTS_PROVIDER"] = "fake"

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

import server as srv                                                # noqa: E402
from delivery_layer.events import EventLog                          # noqa: E402
from delivery_layer.tts.fake import FakeTTS                         # noqa: E402
from delivery_layer.tts.rime import RimeConfig, RimeTTS             # noqa: E402


class DeadProvider:
    """What a RimeTTS looks like after Rime dropped the socket."""
    name = "rime"
    connected = False
    descriptor = {"provider": "rime", "modelId": "coda"}
    sample_rate = 24000

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class TestRimeConnected(unittest.IsolatedAsyncioTestCase):
    async def test_no_socket_is_not_connected(self):
        t = RimeTTS(RimeConfig(api_key="k", speaker="s"), EventLog())
        self.assertFalse(t.connected)

    async def test_a_finished_reader_task_means_the_socket_is_gone(self):
        t = RimeTTS(RimeConfig(api_key="k", speaker="s"), EventLog())
        t._ws = object()                              # something that is not None

        async def reader():
            return None
        t._reader = asyncio.ensure_future(reader())
        await t._reader                               # let it finish
        self.assertFalse(t.connected)
        keepalive = asyncio.ensure_future(asyncio.sleep(10))
        t._reader = keepalive
        self.assertTrue(t.connected, "a live reader and a socket without a closed flag is connected")
        keepalive.cancel()
        try:
            await keepalive
        except asyncio.CancelledError:
            pass

    async def test_fake_is_always_connected(self):
        self.assertTrue(FakeTTS(EventLog()).connected)


class TestEnsureProviderReconnects(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = srv.ReaderSession(dev=False)
        self.dead = DeadProvider()
        self.s.provider = self.dead
        self.s.provider_connected_at = 0.0

    async def asyncTearDown(self):
        os.environ["TTS_PROVIDER"] = "fake"          # never let a reconnect leak into other tests
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def test_reconnect_does_not_touch_the_environment(self):
        orig = srv.make_provider
        srv.make_provider = lambda events, name=None: FakeTTS(events)
        try:
            await self.s.ensure_provider()
        finally:
            srv.make_provider = orig
        self.assertEqual(os.environ.get("TTS_PROVIDER"), "fake")

    async def test_status_says_the_socket_dropped_rather_than_open(self):
        cell = self.s.status()["rime_ws"]
        self.assertEqual(cell["state"], "down")
        self.assertIn("reconnect", cell["detail"])

    async def test_ensure_provider_replaces_a_dead_provider_of_the_same_kind(self):
        made = []
        orig = srv.make_provider

        def fake_make(events, name=None):
            made.append(name or "env")
            return FakeTTS(events)
        srv.make_provider = fake_make
        try:
            p = await self.s.ensure_provider()
        finally:
            srv.make_provider = orig
        self.assertIsNot(p, self.dead)
        self.assertTrue(self.dead.closed, "the dead provider is closed, not leaked")
        self.assertEqual(made, ["rime"], "reconnected as rime, not silently as fake")
        self.assertEqual(len(self.s.events.of_type("provider_reconnect")), 1)
        self.assertTrue(p.connected)

    async def test_a_live_provider_is_kept(self):
        live = FakeTTS(self.s.events)
        self.s.provider = live
        self.assertIs(await self.s.ensure_provider(), live)
        self.assertEqual(self.s.events.of_type("provider_reconnect"), [])


if __name__ == "__main__":
    unittest.main()
