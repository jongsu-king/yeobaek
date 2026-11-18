import hashlib
import io
import os
import re
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote

from yeobaek.exports import atomic_write_file, export_note_zip
from yeobaek.notes import attachment_link


def read_archive(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class PortableNoteExportTests(unittest.TestCase):
    def test_plain_note_preserves_markdown_math_and_external_links(self):
        body = "## 기록\n\n문장 속 $E=mc^2$\n\n$$\n\\int_0^1 x^2 dx\n$$\n\n[문서](https://example.com/a)"
        reader = Mock()
        note = {"title": " 측정 기록 ", "body": body}
        result = read_archive(export_note_zip(note, reader))
        self.assertEqual(result, {"note.md": ("# 측정 기록\n\n" + body).encode()})
        self.assertEqual(note["body"], body)
        reader.assert_not_called()

    def test_binary_files_and_escaped_unicode_labels_remain_portable(self):
        contents = {hashlib.sha256(raw).hexdigest(): raw for raw in (b"%PDF-1.7\x00fixture", b"PK\x03\x04excel\x00")}
        names = ["한글 [측정](1)#.pdf", "보고서.xlsx"]
        references = [attachment_link(name, digest) for name, digest in zip(names, contents)]
        note = {"title": "첨부 있는 노트", "body": "\n\n".join(references)}
        result = read_archive(export_note_zip(note, contents.get))
        markdown = result["note.md"].decode()
        destinations = re.findall(r"\]\((attachments/[^\n]+)\)", markdown)
        self.assertEqual(len(destinations), 2)
        for reference, destination, raw in zip(references, destinations, contents.values()):
            prefix = reference[:reference.rfind("](") + 2]
            self.assertIn(prefix + destination + ")", markdown)
            self.assertEqual(result[unquote(destination)], raw)
        self.assertNotIn("yeobaek-attachment:", markdown)
        self.assertIn("%23", markdown)
        self.assertIn("%28", markdown)

    def test_duplicate_names_and_repeated_links_never_overwrite_original_bytes(self):
        first, second = b"first PDF", b"second PDF"
        contents = {hashlib.sha256(raw).hexdigest(): raw for raw in (first, second)}
        references = [attachment_link("report.pdf", digest) for digest in contents]
        reader = Mock(side_effect=contents.get)
        result = read_archive(export_note_zip({"title": "Files", "body": "\n".join([*references, references[0]])}, reader))
        files = [raw for name, raw in result.items() if name.startswith("attachments/")]
        self.assertCountEqual(files, [first, second])
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(len(re.findall(r"\]\(attachments/", result["note.md"].decode())), 3)

    def test_unsafe_and_long_names_produce_safe_extractable_members(self):
        payload = b"safe content"
        digest = hashlib.sha256(payload).hexdigest()
        names = ["../../CON.txt", r"C:\private\NUL.pdf", "시험" * 180 + ".xlsx"]
        result = read_archive(export_note_zip({"title": "Paths", "body": "\n".join(attachment_link(name, digest) for name in names)}, lambda _: payload))
        for name, raw in result.items():
            self.assertFalse(name.startswith("/"))
            self.assertNotIn("..", name.split("/"))
            self.assertNotIn("\\", name)
            self.assertLessEqual(len(Path(name).name.encode("utf-8")), 255)
            if name != "note.md":
                self.assertEqual(raw, payload)
        self.assertTrue(any(name.endswith(".xlsx") for name in result))

    def test_code_examples_and_escaped_links_are_preserved_without_attachment_reads(self):
        reference = attachment_link("example.pdf", "a" * 64)
        body = "```md\n" + reference + "\n```\n\n`" + reference + "`\n\n\\" + reference
        reader = Mock()
        result = read_archive(export_note_zip({"title": "Examples", "body": body}, reader))
        self.assertEqual(result["note.md"].decode(), "# Examples\n\n" + body)
        reader.assert_not_called()

    def test_nested_list_attachment_is_exported_while_indented_example_is_literal(self):
        raw = b"list attachment"
        digest = hashlib.sha256(raw).hexdigest()
        live = attachment_link("live.pdf", digest)
        example = attachment_link("example.pdf", "a" * 64)
        body = "- Files\n    - " + live + "\n\nOrdinary paragraph.\n\n    " + example
        reader = Mock(return_value=raw)
        result = read_archive(export_note_zip({"title": "Lists", "body": body}, reader))
        markdown = result["note.md"].decode()
        self.assertNotIn(live, markdown)
        self.assertIn("    - [live.pdf](attachments/", markdown)
        self.assertIn("    " + example, markdown)
        reader.assert_called_once_with(digest)

    def test_unmatched_backticks_in_separate_paragraphs_do_not_hide_attachment(self):
        raw = b"paragraph attachment"
        digest = hashlib.sha256(raw).hexdigest()
        body = "A lone ` mark.\n\n" + attachment_link("real.pdf", digest) + "\n\nAnother ` mark."
        reader = Mock(return_value=raw)
        result = read_archive(export_note_zip({"title": "Paragraphs", "body": body}, reader))
        self.assertIn("[real.pdf](attachments/", result["note.md"].decode())
        reader.assert_called_once_with(digest)

    def test_escaped_literal_backticks_do_not_hide_a_live_attachment(self):
        raw = b"live after escaped punctuation"
        digest = hashlib.sha256(raw).hexdigest()
        body = r"\` " + attachment_link("real.pdf", digest) + r" \`"
        reader = Mock(return_value=raw)
        result = read_archive(export_note_zip({"title": "Escaped", "body": body}, reader))
        markdown = result["note.md"].decode()
        self.assertIn(r"\` [real.pdf](attachments/", markdown)
        self.assertTrue(markdown.endswith(r" \`"))
        reader.assert_called_once_with(digest)

    def test_code_span_closing_backtick_treats_its_preceding_slash_literally(self):
        reference = attachment_link("example.pdf", "a" * 64)
        body = "`" + reference + r"\`"
        reader = Mock()
        result = read_archive(export_note_zip({"title": "Code", "body": body}, reader))
        self.assertEqual(result["note.md"].decode(), "# Code\n\n" + body)
        reader.assert_not_called()

    def test_list_and_quote_code_containers_preserve_literal_examples(self):
        example = attachment_link("example.pdf", "a" * 64)
        bodies = [
            "- ```md\n  " + example + "\n  ```",
            "- Item\n\n        " + example,
            "> ```md\n> " + example + "\n> ```",
            "> - ```md\n>   " + example + "\n>   ```",
            "- - ```md\n    " + example + "\n    ```",
            "- > ```md\n  > " + example + "\n  > ```",
            "-     " + example,
        ]
        for body in bodies:
            with self.subTest(body=body):
                reader = Mock()
                result = read_archive(export_note_zip({"title": "Examples", "body": body}, reader))
                self.assertEqual(result["note.md"].decode(), "# Examples\n\n" + body)
                reader.assert_not_called()

    def test_invalid_backtick_fence_info_leaves_following_attachment_live(self):
        raw = b"live after invalid fence"
        digest = hashlib.sha256(raw).hexdigest()
        body = "```lang`broken\n" + attachment_link("real.pdf", digest)
        reader = Mock(return_value=raw)
        result = read_archive(export_note_zip({"title": "Fence", "body": body}, reader))
        self.assertIn("[real.pdf](attachments/", result["note.md"].decode())
        reader.assert_called_once_with(digest)

    def test_live_quote_and_nested_list_links_still_export_after_code_blocks(self):
        raw = b"live quoted attachment"
        digest = hashlib.sha256(raw).hexdigest()
        reference = attachment_link("real.pdf", digest)
        example = attachment_link("example.pdf", "a" * 64)
        body = "> ```md\n> " + example + "\n> ```\n\n> " + reference + "\n\n- Item\n    - " + reference
        body += "\n\n- - " + reference + "\n\n- > " + reference
        reader = Mock(return_value=raw)
        result = read_archive(export_note_zip({"title": "Containers", "body": body}, reader))
        markdown = result["note.md"].decode()
        self.assertIn(example, markdown)
        self.assertEqual(markdown.count("[real.pdf](attachments/"), 4)
        reader.assert_called_once_with(digest)

    def test_missing_or_corrupt_attachments_abort_the_export(self):
        raw = b"valid content"
        digest = hashlib.sha256(raw).hexdigest()
        note = {"title": "File", "body": attachment_link("required.pdf", digest)}
        with self.assertRaisesRegex(ValueError, "required.pdf.*없습니다"):
            export_note_zip(note, lambda _: None)
        with self.assertRaisesRegex(ValueError, "일치하지"):
            export_note_zip(note, lambda _: b"corrupt content")
        with patch("yeobaek.exports.ATTACHMENT_MAX_BYTES", 2):
            with self.assertRaisesRegex(ValueError, "내보낼 수 없습니다"):
                export_note_zip(note, lambda _: raw)


class AtomicExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.file = self.folder / "backup.json"
        self.file.write_bytes(b"previous backup")

    def assert_original_and_no_temporary_files(self):
        self.assertEqual(self.file.read_bytes(), b"previous backup")
        self.assertEqual(list(self.folder.iterdir()), [self.file])

    def test_successful_binary_export_preserves_existing_permissions(self):
        self.file.chmod(0o640)
        mode = stat.S_IMODE(self.file.stat().st_mode)
        content = b"\x00new\xffbackup\x00"
        atomic_write_file(self.file, content)
        self.assertEqual(self.file.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), mode)
        self.assertEqual(list(self.folder.iterdir()), [self.file])

    def test_fsync_failure_preserves_existing_backup(self):
        with patch("yeobaek.exports.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                atomic_write_file(self.file, b"new backup")
        self.assert_original_and_no_temporary_files()

    def test_replace_failure_preserves_existing_backup(self):
        with patch("yeobaek.exports.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                atomic_write_file(self.file, b"new backup")
        self.assert_original_and_no_temporary_files()

    def test_write_failure_preserves_existing_backup(self):
        factory = tempfile.NamedTemporaryFile

        def failing_file(*args, **kwargs):
            stream = factory(*args, **kwargs)
            original_write = stream.write

            def partial_write(content):
                original_write(content[:4])
                raise OSError("write failed after partial output")

            stream.write = partial_write
            return stream

        with patch("yeobaek.exports.tempfile.NamedTemporaryFile", side_effect=failing_file):
            with self.assertRaisesRegex(OSError, "write failed"):
                atomic_write_file(self.file, b"new backup")
        self.assert_original_and_no_temporary_files()

    def test_new_file_binary_roundtrip_and_private_posix_mode(self):
        new = self.folder / "new.bin"
        atomic_write_file(new, b"new file")
        self.assertEqual(new.read_bytes(), b"new file")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(new.stat().st_mode), 0o600)

    def test_symlink_still_targets_original_file(self):
        link = self.folder / "linked.json"
        try:
            link.symlink_to(self.file.name)
        except OSError:
            self.skipTest("This environment does not allow symbolic link creation.")
        atomic_write_file(link, b"updated through link")
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.file.read_bytes(), b"updated through link")
        self.assertEqual(link.read_bytes(), b"updated through link")


if __name__ == "__main__":
    unittest.main()
