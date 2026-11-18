import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import flet as ft

from yeobaek.app import Workspace


class EditorCloseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.page = SimpleNamespace(
            platform=ft.PagePlatform.MACOS,
            width=1120,
            window=SimpleNamespace(destroy=AsyncMock()),
            update=Mock(),
            show_dialog=Mock(),
        )
        self.workspace = Workspace(self.page, Path(self.directory.name))
        operation = self.workspace.store.put("note", {"title": "Original", "body": "Before"})
        self.note_id = operation["entity"]["id"]
        self.workspace.refresh_data()
        self.workspace.selected_id = self.note_id
        self.workspace.change_editor()

    async def start_edit(self):
        self.workspace.title.value = "Last title"
        self.workspace.body.value = "Last keystrokes before closing"
        await self.workspace.changed()

    async def test_close_flushes_pending_autosave_before_destroy(self):
        await self.start_edit()
        pending_save = self.workspace.save_task

        async def verify_before_destroy():
            note = self.workspace.store.notes()[0]
            self.assertEqual(note["title"], "Last title")
            self.assertEqual(note["body"], "Last keystrokes before closing")

        self.page.window.destroy.side_effect = verify_before_destroy
        await self.workspace.window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        self.page.window.destroy.assert_awaited_once()
        self.assertIsNone(self.workspace.draft)
        self.assertIsNone(self.workspace.save_task)
        await asyncio.gather(pending_save, return_exceptions=True)
        self.assertTrue(pending_save.cancelled())
        self.assertEqual(len(self.workspace.store.history(self.note_id)), 2)

    async def test_failed_close_preserves_draft_and_allows_retry(self):
        await self.start_edit()
        pending_save = self.workspace.save_task
        with patch.object(self.workspace.store, "put", side_effect=sqlite3.OperationalError("disk full")):
            await self.workspace.window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        self.page.window.destroy.assert_not_awaited()
        self.assertIsNotNone(self.workspace.draft)
        self.assertFalse(self.workspace.closing)
        self.assertEqual(self.workspace.store.notes()[0]["body"], "Before")
        await self.workspace.window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        self.page.window.destroy.assert_awaited_once()
        self.assertEqual(self.workspace.store.notes()[0]["body"], "Last keystrokes before closing")
        await asyncio.gather(pending_save, return_exceptions=True)

    async def test_repeated_close_events_destroy_only_once(self):
        closing = asyncio.Event()
        release = asyncio.Event()

        async def destroy():
            closing.set()
            await release.wait()

        self.page.window.destroy.side_effect = destroy
        first = asyncio.create_task(self.workspace.window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE)))
        await closing.wait()
        await self.workspace.window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE))
        release.set()
        await first
        self.page.window.destroy.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
