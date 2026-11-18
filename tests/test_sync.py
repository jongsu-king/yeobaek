import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from yeobaek import drive
from yeobaek.notes import attachment_link
from yeobaek.store import Store


class DriveWire:
    """An immutable-file Drive fixture at the HTTP boundary of real sync code."""

    def __init__(self):
        self.files = {}
        self.next_id = 0
        self.drop_next_upload_response = False
        self.upload_attempts = []
        self.media_reads = []

    def replace_content(self, file_id, content):
        file = self.files[file_id]
        file["content"] = content
        file["version"] = str(int(file["version"]) + 1)
        file["md5Checksum"] = hashlib.md5(content, usedforsecurity=False).hexdigest()
        file["sha256Checksum"] = hashlib.sha256(content).hexdigest()

    def request(self, url, *, method, data, headers, accepted, response_limit=None):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if parsed.path.endswith("/about"):
            return 200, {}, drive._json_bytes({"user": {"permissionId": "test-account"}})
        if parsed.path.endswith("/generateIds"):
            self.next_id += 1
            return 200, {}, drive._json_bytes({"ids": [f"file-{self.next_id}"]})
        if method == "POST" and query.get("uploadType") == ["multipart"]:
            mime = f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + data
            parts = list(BytesParser(policy=policy.default).parsebytes(mime).iter_parts())
            metadata = json.loads(parts[0].get_payload(decode=True))
            content = parts[1].get_payload(decode=True)
            file_id = metadata["id"]
            self.upload_attempts.append(file_id)
            if file_id in self.files:
                return 409, {}, b"{}"
            self.files[file_id] = {**metadata, "version": "0"}
            self.replace_content(file_id, content)
            if self.drop_next_upload_response:
                self.drop_next_upload_response = False
                raise drive.DriveError("response lost after commit", retryable=True)
            return 200, {}, drive._json_bytes({"id": file_id})
        if parsed.path.endswith("/files"):
            condition = query["q"][0]
            files = [
                {key: value for key, value in file.items() if key != "content"}
                for file in self.files.values()
                if any(f"key='{key}' and value='{value}'" in condition
                       for key, value in file["appProperties"].items())
            ]
            return 200, {}, drive._json_bytes({"files": list(reversed(files))})
        file_id = parsed.path.rsplit("/", 1)[-1]
        file = self.files[file_id]
        if query.get("alt") == ["media"]:
            self.media_reads.append(file_id)
            content = file["content"]
            if response_limit is not None and len(content) > response_limit:
                raise drive.DriveError("response too large")
            return 200, {}, content
        return 200, {}, drive._json_bytes({key: value for key, value in file.items() if key != "content"})


class SynchronizationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.wire = DriveWire()
        transport = mock.patch.object(drive, "_request_once", side_effect=self.wire.request)
        transport.start()
        self.addCleanup(transport.stop)
        sleep = mock.patch.object(drive.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def device(self, name):
        store = Store(Path(self.folder.name) / (name + ".sqlite3"))
        session = drive.DriveSession(store)
        session.connect_token("test-access-token")
        return store, session

    def test_two_devices_keep_offline_edits_and_converge_after_merge(self):
        left, left_session = self.device("left")
        right, right_session = self.device("right")
        content = b"spreadsheet bytes retained during concurrent edits"
        digest = left.save_attachment(content)
        original = left.put("note", {"body": attachment_link("data.xlsx", digest)})
        note_id = original["entity"]["id"]
        left_session.sync()
        right_session.sync()
        self.assertEqual(right.attachment(digest), content)
        left.put("note", {"id": note_id, "title": "Offline left"}, parents=[original["id"]])
        right.put("note", {"id": note_id, "title": "Offline right"}, parents=[original["id"]])
        left_session.sync()
        right_session.sync()
        left_session.sync()
        self.assertEqual(left.operations(), right.operations())
        self.assertEqual(len(left.conflicts(note_id)), 2)
        self.assertEqual({head["entity"]["title"] for head in left.heads(note_id)}, {"Offline left", "Offline right"})
        left.put("note", {"id": note_id, "title": "Resolved"}, parents=[op["id"] for op in left.heads(note_id)])
        left_session.sync()
        right_session.sync()
        self.assertEqual(left.operations(), right.operations())
        self.assertEqual(left.notes()[0]["title"], "Resolved")
        self.assertEqual(right.conflicts(note_id), [])
        self.assertEqual(right.attachment(digest), content)

    def test_lost_commit_response_retries_reserved_id_without_duplicate_files(self):
        source, source_session = self.device("source")
        source.put("note", {"title": "Saved before response disappeared"})
        self.wire.drop_next_upload_response = True
        source_session.sync()
        self.assertEqual(len(self.wire.files), 1)
        self.assertEqual(len(self.wire.upload_attempts), 2)
        self.assertEqual(self.wire.upload_attempts[0], self.wire.upload_attempts[1])
        self.assertEqual(source.pending("test-account"), [])
        replica, replica_session = self.device("replica")
        replica_session.sync()
        self.assertEqual(replica.operations(), source.operations())
        reads = len(self.wire.media_reads)
        reopened, reopened_session = self.device("replica")
        reopened_session.sync()
        self.assertEqual(len(self.wire.media_reads), reads)
        self.assertEqual(reopened.operations(), source.operations())

    def test_corrupt_remote_attachment_is_repaired_from_an_intact_device(self):
        source, source_session = self.device("source")
        content = b"original attachment bytes"
        digest = source.save_attachment(content)
        source.put("note", {"body": attachment_link("data.bin", digest)})
        source_session.sync()
        attachment_id = next(file_id for file_id, file in self.wire.files.items()
                             if drive.ATTACHMENT_DIGEST_PROPERTY in file["appProperties"])
        self.wire.replace_content(attachment_id, b"damaged remote bytes")
        replica, replica_session = self.device("replica")
        first = replica_session.sync()
        self.assertEqual(first.unreadable, 1)
        self.assertEqual(len(replica.notes()), 1)
        self.assertIsNone(replica.attachment(digest))
        source_session.sync()
        repaired = replica_session.sync()
        self.assertEqual(repaired.unreadable, 0)
        self.assertEqual(replica.attachment(digest), content)

    def test_bad_remote_batch_does_not_block_valid_local_note_upload(self):
        source, source_session = self.device("source")
        source.put("note", {"title": "remote note"})
        source_session.sync()
        file_id = next(iter(self.wire.files))
        self.wire.replace_content(file_id, b'{"operations": "damaged"}')
        replica, replica_session = self.device("replica")
        replica.put("note", {"title": "valid local note"})
        result = replica_session.sync()
        self.assertEqual(result.unreadable, 1)
        self.assertEqual(result.uploaded, 1)
        self.assertEqual(replica.pending("test-account"), [])
        third, third_session = self.device("third")
        received = third_session.sync()
        self.assertEqual(received.unreadable, 1)
        self.assertEqual([note["title"] for note in third.notes()], ["valid local note"])

    def test_detected_local_corruption_is_repaired_from_remote_bytes(self):
        source, source_session = self.device("source")
        content = b"intact remote copy for local repair"
        digest = source.save_attachment(content)
        source.put("note", {"body": attachment_link("data.bin", digest)})
        source_session.sync()
        replica, replica_session = self.device("replica")
        replica_session.sync()
        with closing(sqlite3.connect(replica.path)) as db:
            with db:
                db.execute("UPDATE attachments SET content = ? WHERE digest = ?", (b"damaged local bytes", digest))
        self.assertIsNone(replica.attachment(digest))
        self.assertNotIn(digest, replica.attachment_checksums())
        self.assertEqual(replica_session.sync().unreadable, 0)
        self.assertEqual(replica.attachment(digest), content)
        self.assertEqual(len(self.wire.files), 2)

    def test_damaged_download_bytes_are_rejected_before_local_storage(self):
        source, source_session = self.device("source")
        content = b"unmodified remote attachment"
        digest = source.save_attachment(content)
        source.put("note", {"body": attachment_link("data.bin", digest)})
        source_session.sync()
        attachment_id = next(file_id for file_id, file in self.wire.files.items()
                             if drive.ATTACHMENT_DIGEST_PROPERTY in file["appProperties"])
        replica, replica_session = self.device("replica")

        def damaged_response(url, **options):
            response = self.wire.request(url, **options)
            parsed = urlsplit(url)
            if parsed.path.endswith("/" + attachment_id) and parse_qs(parsed.query).get("alt") == ["media"]:
                return response[0], response[1], b"corrupted during transfer"
            return response

        with mock.patch.object(drive, "_request_once", side_effect=damaged_response):
            result = replica_session.sync()
        self.assertEqual(result.unreadable, 1)
        self.assertIsNone(replica.attachment(digest))
        self.assertEqual(replica_session.sync().unreadable, 0)
        self.assertEqual(replica.attachment(digest), content)

    def test_modified_remote_file_is_rechecked_without_losing_received_notes(self):
        source, source_session = self.device("source")
        source.put("note", {"title": "Previously received note"})
        source_session.sync()
        file_id = next(iter(self.wire.files))
        original = self.wire.files[file_id]["content"]
        replica, replica_session = self.device("replica")
        replica_session.sync()
        self.wire.replace_content(file_id, b"damaged after the first download")
        self.assertEqual(replica_session.sync().unreadable, 1)
        self.assertEqual(replica.operations(), source.operations())
        self.wire.replace_content(file_id, original)
        self.assertEqual(replica_session.sync().unreadable, 0)
        self.assertEqual(replica.operations(), source.operations())


if __name__ == "__main__":
    unittest.main()
