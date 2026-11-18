import hashlib
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from yeobaek.notes import attachment_link
from yeobaek.store import Store


def identifier(number):
    return str(uuid.UUID(int=number, version=4))


def note_revision(number, *, clock, parents=(), body="", entity_id=None):
    return {
        "id": identifier(number), "type": "note", "clock": clock,
        "parents": list(parents),
        "entity": {
            "id": entity_id or identifier(1000), "created_at": 100,
            "updated_at": clock, "deleted_at": None, "title": "Shared note",
            "body": body, "notebook_id": None, "tags": [], "pinned": False,
        },
    }


class StoreIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def store(self, name):
        return Store(Path(self.folder.name) / (name + ".sqlite3"))

    def test_out_of_order_and_duplicate_delivery_converges(self):
        original = note_revision(1, clock=100, body="original")
        left = note_revision(2, clock=101, parents=[original["id"]], body="left edit")
        right = note_revision(3, clock=101, parents=[original["id"]], body="right edit")
        merged = note_revision(4, clock=102, parents=[left["id"], right["id"]], body="both edits")
        deliveries = [
            [original, left, right, merged],
            [merged, right, original, left, merged],
            [right, left, merged, original, original],
        ]
        stores = [self.store(str(index)) for index in range(len(deliveries))]
        for store, delivery in zip(stores, deliveries):
            for revision in delivery:
                store.ingest([revision])
            self.assertEqual(store.notes(), [merged["entity"]])
            self.assertEqual(store.heads(original["entity"]["id"]), [merged])
            self.assertEqual(len(store.operations()), 4)
        self.assertEqual(stores[0].operations(), stores[1].operations())
        self.assertEqual(stores[1].operations(), stores[2].operations())

    def test_concurrent_edits_and_backup_restore_keep_both_branches(self):
        store = self.store("source")
        content = b"attachment retained by older revisions"
        digest = store.save_attachment(content)
        original = store.put("note", {"body": attachment_link("data.bin", digest)})
        note_id = original["entity"]["id"]

        def edit(title):
            return store.put("note", {"id": note_id, "title": title}, parents=[original["id"]])

        with ThreadPoolExecutor(max_workers=2) as executor:
            branches = list(executor.map(edit, ["Device A", "Device B"]))
        self.assertEqual({head["entity"]["title"] for head in store.conflicts(note_id)}, {"Device A", "Device B"})
        restored = self.store("restored")
        restored.ingest(list(reversed(store.operations())), attachments={digest: store.attachment(digest)})
        self.assertEqual(restored.operations(), store.operations())
        self.assertEqual(restored.attachment(digest), content)
        self.assertEqual(len(restored.conflicts(note_id)), 2)
        merged = store.put("note", {"id": note_id, "body": "Resolved together"}, parents=[op["id"] for op in branches])
        restored.ingest([merged, merged])
        self.assertEqual(restored.notes(), store.notes())
        self.assertEqual(restored.conflicts(note_id), [])
        self.assertEqual(restored.referenced_attachments(), {digest})

    def test_conflicting_backup_record_rolls_back_new_attachments_and_notes(self):
        store = self.store("existing")
        original = note_revision(1, clock=100, body="keep original")
        store.ingest([original])
        changed = {**original, "entity": {**original["entity"], "body": "replacement"}}
        fresh = note_revision(2, clock=101, entity_id=identifier(1001), body="new")
        content = b"new file in rejected backup"
        digest = hashlib.sha256(content).hexdigest()
        with self.assertRaises(ValueError):
            store.ingest([fresh, changed], attachments={digest: content})
        self.assertEqual(store.operations(), [original])
        self.assertIsNone(store.attachment(digest))

    def test_bad_attachment_digest_leaves_workspace_unchanged(self):
        store = self.store("existing")
        original = note_revision(1, clock=100, body="keep original")
        store.ingest([original])
        content = b"expected original attachment"
        digest = hashlib.sha256(content).hexdigest()
        incoming = note_revision(2, clock=101, entity_id=identifier(1001), body="new")
        with self.assertRaises(ValueError):
            store.ingest([incoming], attachments={digest: b"damaged bytes"})
        self.assertEqual(store.operations(), [original])
        self.assertEqual(store.attachment_hashes(), [])

    def test_delayed_invalid_parent_rolls_back_the_whole_delivery(self):
        store = self.store("delayed-parent")
        parent = note_revision(1, clock=100, entity_id=identifier(1001))
        child = note_revision(2, clock=101, parents=[parent["id"]])
        store.ingest([child])
        unrelated = note_revision(3, clock=99, entity_id=identifier(1002))
        with self.assertRaises(ValueError):
            store.ingest([unrelated, parent])
        self.assertEqual(store.operations(), [child])

    def corrupt_attachment(self, store, digest):
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE attachments SET content = ? WHERE digest = ?", (b"damaged stored bytes", digest))

    def test_corrupt_attachment_is_preserved_but_excluded_from_available_files(self):
        store = self.store("corrupt")
        digest = store.save_attachment(b"original stored bytes")
        self.corrupt_attachment(store, digest)
        self.assertIsNone(store.attachment(digest))
        self.assertNotIn(digest, store.attachment_checksums())
        with sqlite3.connect(store.path) as db:
            content = db.execute("SELECT content FROM attachments WHERE digest = ?", (digest,)).fetchone()[0]
        self.assertEqual(content, b"damaged stored bytes")
        with mock.patch("yeobaek.store.hashlib.sha256", side_effect=AssertionError("already quarantined")):
            self.assertIsNone(store.attachment(digest))

    def test_validated_save_and_backup_import_repair_damaged_existing_rows(self):
        for import_backup in (False, True):
            for already_detected in (False, True):
                with self.subTest(import_backup=import_backup, already_detected=already_detected):
                    store = self.store(f"repair-{import_backup}-{already_detected}")
                    content = b"valid attachment restored from another copy"
                    digest = store.save_attachment(content)
                    self.corrupt_attachment(store, digest)
                    if already_detected:
                        self.assertIsNone(store.attachment(digest))
                    if import_backup:
                        store.ingest([], attachments={digest: content})
                    else:
                        store.save_attachment(content, expected_digest=digest)
                    self.assertEqual(store.attachment(digest), content)
                    self.assertIn(digest, store.attachment_checksums())

    def test_failed_backup_import_rolls_back_attachment_repair(self):
        store = self.store("failed-repair")
        original = store.put("note", {"title": "Keep original"})
        content = b"original attachment"
        digest = store.save_attachment(content)
        self.corrupt_attachment(store, digest)
        self.assertIsNone(store.attachment(digest))
        conflicting = {**original, "entity": {**original["entity"], "title": "Invalid replacement"}}
        with self.assertRaises(ValueError):
            store.ingest([conflicting], attachments={digest: content})
        self.assertIsNone(store.attachment(digest))
        self.assertNotIn(digest, store.attachment_checksums())
        self.assertEqual(store.operations(), [original])
        with sqlite3.connect(store.path) as db:
            value = db.execute("SELECT content FROM attachments WHERE digest = ?", (digest,)).fetchone()[0]
        self.assertEqual(value, b"damaged stored bytes")

    def test_concurrent_repair_is_not_marked_as_damaged_by_an_older_read(self):
        store = self.store("repair-race")
        content = b"concurrently repaired attachment"
        digest = store.save_attachment(content)
        self.corrupt_attachment(store, digest)
        original_connection = store._connection
        repaired = False

        @contextmanager
        def repair_before_quarantine(*, write=False):
            nonlocal repaired
            if write and not repaired:
                repaired = True
                store.save_attachment(content, expected_digest=digest)
            with original_connection(write=write) as db:
                yield db

        with mock.patch.object(store, "_connection", new=repair_before_quarantine):
            self.assertIsNone(store.attachment(digest))
        self.assertTrue(repaired)
        self.assertEqual(store.attachment(digest), content)
        self.assertIn(digest, store.attachment_checksums())

    def test_existing_database_gains_damage_tracking_without_changing_notes(self):
        store = self.store("old-database")
        content = b"existing attachment"
        digest = store.save_attachment(content)
        original = store.put("note", {"body": attachment_link("file.bin", digest)})
        with sqlite3.connect(store.path) as db:
            db.execute("DROP TABLE damaged_attachments")
        reopened = Store(store.path)
        self.assertEqual(reopened.operations(), [original])
        self.assertEqual(reopened.attachment(digest), content)


if __name__ == "__main__":
    unittest.main()
