import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import flet as ft

from yeobaek.app import Workspace
from yeobaek.drive import DriveAccount


class DriveDisconnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        page = SimpleNamespace(platform=ft.PagePlatform.MACOS, width=1120,
                               update=Mock(), show_dialog=Mock(), pop_dialog=Mock())
        self.workspace = Workspace(page, Path(self.directory.name))
        self.workspace.drive.account = DriveAccount("fixture", "Test account", "")
        self.workspace.drive._token = "fixture-token"
        self.workspace.drive._expires_at = time.monotonic() + 60

    async def test_background_sync_cannot_restart_during_disconnect(self):
        entered = threading.Event()
        release = threading.Event()
        original_disconnect = self.workspace.drive.disconnect
        original_sleep = asyncio.sleep

        def paused_disconnect():
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("Test did not release disconnect")
            original_disconnect()

        async def fast_sleep(_):
            await original_sleep(0)

        self.workspace.sync_now = AsyncMock()
        loop = None
        with patch.object(self.workspace.drive, "disconnect", side_effect=paused_disconnect):
            disconnect = asyncio.create_task(self.workspace.disconnect_drive())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                with patch("yeobaek.app.asyncio.sleep", new=fast_sleep):
                    self.workspace.sync_requested.set()
                    loop = asyncio.create_task(self.workspace.sync_loop())
                    for _ in range(8):
                        await original_sleep(0)
                    self.assertFalse(self.workspace.sync_requested.is_set())
                    self.workspace.sync_now.assert_not_awaited()
            finally:
                release.set()
                await disconnect
                if loop is not None:
                    loop.cancel()
                    await asyncio.gather(loop, return_exceptions=True)
        self.assertFalse(self.workspace.drive.connected)
        self.assertFalse(self.workspace.drive_busy)

    async def test_sdk_signout_failure_keeps_backend_disconnected(self):
        self.workspace.mobile_auth = SimpleNamespace(sign_out=AsyncMock(side_effect=RuntimeError("SDK unavailable")))
        with self.assertRaisesRegex(RuntimeError, "SDK unavailable"):
            await self.workspace.disconnect_drive()
        self.assertFalse(self.workspace.drive.connected)
        self.assertIsNone(self.workspace.drive.account)
        self.assertFalse(self.workspace.drive_busy)
        self.assertEqual(self.workspace.drive_status, "동기화 연결")


if __name__ == "__main__":
    unittest.main()
