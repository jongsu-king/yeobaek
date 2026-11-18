import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import flet as ft

from yeobaek.app import InlineAttachment, Workspace
from yeobaek.notes import attachment_link
from yeobaek.previews import PDFPage


class AttachmentInterfaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.page = SimpleNamespace(
            platform=ft.PagePlatform.MACOS, width=1120,
            update=Mock(), show_dialog=Mock(), pop_dialog=Mock(),
        )
        self.workspace = Workspace(self.page, Path(self.directory.name))
        self.content = b"original attachment bytes\x00\xff"
        self.digest = self.workspace.store.save_attachment(self.content)
        revision = self.workspace.store.put("note", {
            "title": "Export fixture", "body": attachment_link("data.bin", self.digest),
        })
        self.workspace.refresh_data()
        self.workspace.selected_id = revision["entity"]["id"]
        self.workspace.change_editor()

    def export_button(self):
        self.workspace.note_menu()
        dialog = self.page.show_dialog.call_args.args[0]
        return next(button for button in dialog.content.controls
                    if button.content == "첨부와 함께 ZIP 내보내기")

    async def test_menu_exports_original_files_and_portable_markdown(self):
        self.workspace.export_file = AsyncMock()
        await self.export_button().on_click(None)
        filename, payload = self.workspace.export_file.await_args.args
        self.assertEqual(filename, "Export fixture.zip")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            attachment = next(name for name in archive.namelist() if name.startswith("attachments/"))
            self.assertEqual(archive.read(attachment), self.content)
            markdown = archive.read("note.md").decode()
            self.assertIn("attachments/", markdown)
            self.assertNotIn("yeobaek-attachment:", markdown)

    async def test_missing_attachment_stops_before_save_dialog(self):
        revision = self.workspace.store.put("note", {
            "title": "Missing file", "body": attachment_link("missing.pdf", "a" * 64),
        })
        self.workspace.refresh_data()
        self.workspace.selected_id = revision["entity"]["id"]
        self.workspace.change_editor()
        self.workspace.export_file = AsyncMock()
        await self.export_button().on_click(None)
        self.workspace.export_file.assert_not_awaited()
        self.assertIn("missing.pdf", self.page.show_dialog.call_args.args[0].content.value)

    async def test_desktop_save_failure_preserves_existing_backup(self):
        destination = Path(self.directory.name) / "backup.json"
        destination.write_bytes(b"previous backup")
        self.workspace.file_picker.save_file = AsyncMock(return_value=str(destination))
        with patch("yeobaek.exports.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await self.workspace.export_file("backup.json", b"new backup")
        self.assertEqual(destination.read_bytes(), b"previous backup")
        self.assertEqual(list(destination.parent.glob(".yeobaek-export-*")), [])

    async def test_narrow_pdf_has_separate_navigation_and_zoom_controls(self):
        self.page.platform = ft.PagePlatform.ANDROID
        attachment = InlineAttachment(
            self.workspace.selected_id, "fixture.pdf", self.digest,
            attachment_link("fixture.pdf", self.digest), "pdf", b"%PDF-test",
        )
        controls = self.workspace.pdf_page_controls(attachment, PDFPage(2, png=b"PNG-test"), 0)
        navigation, tools, viewer = controls
        self.assertEqual(len(navigation.controls), 3)
        self.assertIsInstance(viewer, ft.InteractiveViewer)
        self.assertEqual(len(tools.controls), 3)
        with patch.object(viewer, "_invoke_method", new=AsyncMock()) as invoke:
            await tools.controls[1].on_click(None)
            invoke.assert_awaited_once_with("zoom", arguments={"factor": 1.25})
        self.assertTrue(navigation.controls[0].disabled)
        self.assertFalse(navigation.controls[2].disabled)


if __name__ == "__main__":
    unittest.main()
