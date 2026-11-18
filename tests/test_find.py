import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import flet as ft

from yeobaek.app import Workspace


class NoteFindTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.page = SimpleNamespace(
            platform=ft.PagePlatform.MACOS, width=1120,
            update=Mock(), show_dialog=Mock(), pop_dialog=Mock(),
        )
        self.workspace = Workspace(self.page, Path(self.directory.name))
        operation = self.workspace.store.put("note", {
            "title": "Finder", "body": "한글🙂 Alpha alpha ALPHA",
        })
        self.workspace.refresh_data()
        self.workspace.selected_id = operation["entity"]["id"]
        self.workspace.change_editor()
        self.workspace.body.focus = AsyncMock()
        self.workspace.find_query.focus = AsyncMock()
        self.workspace.find_query.value = "alpha"
        self.workspace.find_bar.visible = True

    async def test_find_selects_utf16_ranges_and_wraps_both_directions(self):
        before = self.workspace.store.operations()
        await self.workspace.find_text()
        selection = self.workspace.body.selection
        self.assertEqual((selection.base_offset, selection.extent_offset), (5, 10))
        self.assertEqual(self.workspace.find_status.value, "1 / 3")
        self.workspace.body.focus.assert_awaited_once()
        await self.workspace.find_text(True)
        self.assertEqual(self.workspace.find_status.value, "3 / 3")
        await self.workspace.find_text()
        self.assertEqual(self.workspace.find_status.value, "1 / 3")
        self.assertEqual(self.workspace.body.value, "한글🙂 Alpha alpha ALPHA")
        self.assertEqual(self.workspace.store.operations(), before)

    async def test_query_is_literal_and_missing_results_keep_editor_unchanged(self):
        self.workspace.body.value = "a.*b a🙂b"
        self.workspace.find_query.value = ".*"
        await self.workspace.find_text()
        self.assertEqual(self.workspace.body_selection(), (1, 3))
        selection = self.workspace.body.selection
        self.workspace.find_query.value = "missing"
        await self.workspace.find_text()
        self.assertEqual(self.workspace.find_status.value, "0곳")
        self.assertEqual(self.workspace.body.selection, selection)
        self.assertEqual(self.workspace.body.value, "a.*b a🙂b")

    async def test_selection_is_set_only_after_native_focus_completes(self):
        async def focus():
            self.assertIsNone(self.workspace.body.selection)

        self.workspace.body.focus.side_effect = focus
        await self.workspace.find_text()
        self.assertEqual(self.workspace.body_selection(), (4, 9))

    async def test_note_switch_during_focus_cannot_select_a_stale_range(self):
        note = self.workspace.store.put("note", {"title": "Second", "body": "Keep second note"})
        self.workspace.refresh_data()

        async def switch_note():
            await self.workspace.select_note(note["entity"]["id"])

        self.workspace.body.focus.side_effect = switch_note
        await self.workspace.find_text()
        self.assertEqual(self.workspace.selected_id, note["entity"]["id"])
        self.assertEqual(self.workspace.body.value, "Keep second note")
        self.assertIsNone(self.workspace.body.selection)

    async def test_opening_from_preview_reveals_editor_and_close_restores_focus(self):
        self.workspace.previewing = True
        self.workspace.change_editor(load=False)
        await self.workspace.show_find()
        self.assertFalse(self.workspace.previewing)
        self.assertTrue(self.workspace.body.visible)
        self.assertTrue(self.workspace.find_bar.visible)
        self.assertEqual(self.workspace.stage, "editor")
        self.workspace.find_query.focus.assert_awaited_once()
        await self.workspace.close_find()
        self.assertFalse(self.workspace.find_bar.visible)
        self.workspace.body.focus.assert_awaited_once()

    async def test_query_typing_updates_count_without_stealing_focus(self):
        await self.workspace.update_find_count()
        self.assertEqual(self.workspace.find_status.value, "3곳")
        self.workspace.body.focus.assert_not_awaited()
        self.workspace.find_query.value = ""
        await self.workspace.update_find_count()
        self.assertEqual(self.workspace.find_status.value, "")

    async def test_keyboard_shortcuts_open_find_and_navigate(self):
        event = lambda key, **modifiers: SimpleNamespace(
            key=key, ctrl=modifiers.get("ctrl", False),
            meta=modifiers.get("meta", False), shift=modifiers.get("shift", False))
        await self.workspace.keyboard(event("f", meta=True))
        self.assertTrue(self.workspace.find_bar.visible)
        await self.workspace.keyboard(event("F3"))
        self.assertEqual(self.workspace.find_status.value, "1 / 3")
        await self.workspace.keyboard(event("F3", shift=True))
        self.assertEqual(self.workspace.find_status.value, "3 / 3")
        await self.workspace.keyboard(event("g", meta=True))
        self.assertEqual(self.workspace.find_status.value, "1 / 3")
        await self.workspace.keyboard(event("g", meta=True, shift=True))
        self.assertEqual(self.workspace.find_status.value, "3 / 3")
        await self.workspace.keyboard(event("Escape"))
        self.assertFalse(self.workspace.find_bar.visible)


if __name__ == "__main__":
    unittest.main()
