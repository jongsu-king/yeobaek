import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from yeobaek import drive
from yeobaek.notes import attachment_link
from yeobaek.store import Store


class AttachmentAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name) / "notes.sqlite3")
        self.content = b"attachment authentication regression"
        self.digest = hashlib.sha256(self.content).hexdigest()
        revision = self.store.put(
            "note", {"body": attachment_link("file.bin", self.digest)},
        )
        self.store.mark_uploaded([revision["id"]], "test-account")
        self.session = drive.DriveSession(self.store)
        self.session.account = drive.DriveAccount("test-account", "", "")
        self.session._token = "expired-test-token"
        self.session._expires_at = 0
        self.download_notes = mock.patch.object(self.session, "_download", return_value=(0, 0))
        self.download_notes.start()
        self.addCleanup(self.download_notes.stop)

    def assert_login_required(self):
        with self.assertRaises(drive.AuthenticationError):
            self.session.sync()
        self.assertIsNone(self.session.account)

    def test_expiry_while_listing_attachments_requests_token_refresh(self):
        self.assert_login_required()

    def test_expiry_while_downloading_attachment_requests_token_refresh(self):
        remote = [{"id": "remote-attachment", "appProperties": {
            drive.ATTACHMENT_WORKSPACE_PROPERTY: drive.WORKSPACE_VERSION,
            drive.ATTACHMENT_DIGEST_PROPERTY: self.digest,
        }}]
        with mock.patch.object(self.session, "_listed_files", return_value=remote):
            self.assert_login_required()
        self.assertIsNone(self.store.attachment(self.digest))

    def test_expiry_while_uploading_attachment_requests_token_refresh(self):
        self.store.save_attachment(self.content)
        with mock.patch.object(self.session, "_listed_files", return_value=[]):
            self.assert_login_required()
        self.assertEqual(self.store.attachment(self.digest), self.content)

    def test_temporary_listing_failure_keeps_retryable_local_attachment(self):
        self.store.save_attachment(self.content)
        error = drive.DriveError("offline", retryable=True)
        with mock.patch.object(self.session, "_listed_files", side_effect=error):
            result = self.session.sync()
        self.assertEqual(result, drive.SyncResult(0, 0, 1))
        self.assertIsNotNone(self.session.account)
        self.assertEqual(self.store.attachment(self.digest), self.content)


if __name__ == "__main__":
    unittest.main()
