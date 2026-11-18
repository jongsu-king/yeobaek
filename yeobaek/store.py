"""Local SQLite workspace and immutable revisions for Drive synchronization."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_ATTACHMENT_DIGEST = re.compile(r"[0-9a-f]{64}")
_ATTACHMENT_LINK = re.compile(r"\]\(<?yeobaek-attachment:([0-9a-f]{64})>?\)")
_KINDS = frozenset(("note", "notebook"))
_MAX_DATE = 253_402_214_399_999
_MAX_CLOCK = (1 << 53) - 1
ATTACHMENT_MAX_BYTES = 64 * 1024 * 1024

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('note', 'notebook')),
    entity_id TEXT NOT NULL,
    clock INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS operation_entity ON operations (entity_id, clock DESC, id DESC);
CREATE INDEX IF NOT EXISTS operation_kind ON operations (kind, clock DESC, id DESC);
CREATE INDEX IF NOT EXISTS operation_clock ON operations (clock, id);
CREATE TABLE IF NOT EXISTS parents (
    operation_id TEXT NOT NULL REFERENCES operations(id),
    parent_id TEXT NOT NULL,
    PRIMARY KEY (operation_id, parent_id)
);
CREATE INDEX IF NOT EXISTS parent_lookup ON parents (parent_id);
CREATE TABLE IF NOT EXISTS uploads (
    account_key TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(id),
    PRIMARY KEY (account_key, operation_id)
);
CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS staged_uploads (
    account_key TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    file_id TEXT NOT NULL,
    PRIMARY KEY (account_key, batch_key)
);
CREATE TABLE IF NOT EXISTS drive_receipts (
    account_key TEXT NOT NULL,
    workspace_key TEXT NOT NULL,
    file_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY (account_key, workspace_key, file_id)
);
CREATE TABLE IF NOT EXISTS attachments (
    digest TEXT PRIMARY KEY,
    md5 TEXT NOT NULL,
    content BLOB NOT NULL
);
COMMIT;
"""
_HEAD_FILTER = "NOT EXISTS (SELECT 1 FROM parents WHERE parents.parent_id = operations.id)"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _is_id(value):
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _is_int_between(value, maximum):
    return type(value) is int and 0 <= value <= maximum


def _checked_text(value, message):
    _require(isinstance(value, str), message)
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError(message) from error
    return value


def _checked_ids(values):
    _require(isinstance(values, (list, tuple)) and all(_is_id(value) for value in values),
             "수정 기록 ID 목록이 잘못되었습니다.")
    _require(len(set(values)) == len(values), "수정 기록 ID가 중복되었습니다.")
    return sorted(values)


def _checked_digest(digest):
    _require(isinstance(digest, str) and _ATTACHMENT_DIGEST.fullmatch(digest) is not None,
             "첨부 파일 ID가 올바르지 않습니다.")
    return digest


def _attachment_record(raw, expected_digest=None):
    _require(isinstance(raw, bytes), "첨부 파일을 읽을 수 없습니다.")
    _require(len(raw) <= ATTACHMENT_MAX_BYTES, "한 파일은 64 MiB까지 첨부할 수 있습니다.")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None:
        _require(_checked_digest(expected_digest) == digest, "첨부 파일의 내용이 저장된 ID와 일치하지 않습니다.")
    return digest, hashlib.md5(raw, usedforsecurity=False).hexdigest(), raw


def _checked_kind(kind):
    _require(isinstance(kind, str) and kind in _KINDS, "노트 또는 노트북 종류가 잘못되었습니다.")


def _checked_entity(kind, entity):
    _checked_kind(kind)
    _require(isinstance(entity, dict) and _is_id(entity.get("id")), "노트 또는 노트북 ID가 잘못되었습니다.")
    for field in ("created_at", "updated_at"):
        _require(_is_int_between(entity.get(field), _MAX_DATE), "작성 또는 수정 날짜가 잘못되었습니다.")
    _require("deleted_at" in entity, "삭제 날짜가 없습니다.")
    deleted_at = entity["deleted_at"]
    _require(deleted_at is None or _is_int_between(deleted_at, _MAX_DATE), "삭제 날짜가 잘못되었습니다.")
    common = {field: entity[field] for field in ("id", "created_at", "updated_at", "deleted_at")}
    if kind == "notebook":
        return {**common, "name": _checked_text(entity.get("name"), "노트북 이름이 잘못되었습니다.")}

    notebook_id = entity.get("notebook_id")
    _require("notebook_id" in entity and (notebook_id is None or _is_id(notebook_id)),
             "노트북 ID가 잘못되었습니다.")
    _require(isinstance(entity.get("tags"), list), "태그가 잘못되었습니다.")
    _require(type(entity.get("pinned")) is bool, "노트 고정 상태가 잘못되었습니다.")
    return {
        **common,
        "title": _checked_text(entity.get("title"), "노트 제목이 잘못되었습니다."),
        "body": _checked_text(entity.get("body"), "노트 본문이 잘못되었습니다."),
        "notebook_id": notebook_id,
        "tags": [_checked_text(tag, "태그가 잘못되었습니다.") for tag in entity["tags"]],
        "pinned": entity["pinned"],
    }


def _op_key(op):
    return op["clock"], op["id"]


def _check_parent(child, parent):
    _require(
        parent["type"] == child["type"]
        and parent["entity"]["id"] == child["entity"]["id"]
        and parent["clock"] < child["clock"],
        "노트의 수정 이력이 일치하지 않습니다.",
    )


def validate_ops(operations):
    """Validate and canonicalize records without requiring all parents to be present."""
    _require(isinstance(operations, (list, tuple)), "동기화 기록 목록이 잘못되었습니다.")
    checked = {}
    kinds = {}
    for raw in operations:
        _require(isinstance(raw, dict) and _is_id(raw.get("id")), "동기화 기록 ID가 잘못되었습니다.")
        clock = raw.get("clock")
        _require(_is_int_between(clock, _MAX_CLOCK), "동기화 기록 순서가 잘못되었습니다.")
        parents = _checked_ids(raw.get("parents"))
        op_id = raw["id"]
        _require(op_id not in parents, "자기 자신을 이전 수정 기록으로 지정할 수 없습니다.")
        kind = raw.get("type")
        entity = _checked_entity(kind, raw.get("entity"))
        entity_id = entity["id"]
        _require(kinds.setdefault(entity_id, kind) == kind, "이 ID에 다른 종류의 항목이 있습니다.")
        op = {"id": op_id, "type": kind, "entity": entity, "clock": clock, "parents": parents}
        _require(checked.setdefault(op_id, op) == op, "같은 ID의 동기화 기록에 다른 내용이 있습니다.")
    for op in checked.values():
        for parent_id in op["parents"]:
            if parent_id in checked:
                _check_parent(op, checked[parent_id])
    return sorted(checked.values(), key=_op_key)


def _default_entity(kind, entity_id, now):
    common = {"id": entity_id, "created_at": now, "updated_at": now, "deleted_at": None}
    if kind == "notebook":
        return {**common, "name": ""}
    return {**common, "title": "", "body": "", "notebook_id": None, "tags": [], "pinned": False}


def _get_op(db, op_id):
    row = db.execute("SELECT payload FROM operations WHERE id = ?", (op_id,)).fetchone()
    return json.loads(row[0]) if row is not None else None


def _insert_op(db, op):
    payload = json.dumps(op, ensure_ascii=False, separators=(",", ":"))
    db.execute(
        "INSERT INTO operations (id, kind, entity_id, clock, payload) VALUES (?, ?, ?, ?, ?)",
        (op["id"], op["type"], op["entity"]["id"], op["clock"], payload),
    )
    db.executemany("INSERT INTO parents (operation_id, parent_id) VALUES (?, ?)",
                   ((op["id"], parent_id) for parent_id in op["parents"]))


def _read_heads(db, entity_id):
    return [
        json.loads(payload)
        for (payload,) in db.execute(
            f"SELECT payload FROM operations WHERE entity_id = ? AND {_HEAD_FILTER} ORDER BY clock DESC, id DESC",
            (entity_id,),
        )
    ]


def _record_uploads(db, account_key, op_ids):
    db.executemany(
        "INSERT INTO uploads (account_key, operation_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        ((account_key, op_id) for op_id in op_ids),
    )


def _check_account(account_key):
    _require(bool(_checked_text(account_key, "Drive 계정 ID가 잘못되었습니다.")), "Drive 계정 ID가 없습니다.")


def _enable_wal(db):
    deadline = time.monotonic() + 15
    while True:
        try:
            mode = db.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        except sqlite3.OperationalError as error:
            # SQLite can skip its busy handler while two processes switch journal mode.
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)
        else:
            if mode != "wal":
                raise sqlite3.OperationalError("SQLite WAL을 활성화할 수 없습니다.")
            return


class Store:
    """Use a fresh connection per call so the UI and Drive worker can share the store."""

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            _enable_wal(db)
            db.executescript(_SCHEMA)

    @contextmanager
    def _connection(self, *, write=False):
        with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                if write:
                    db.commit()
            except BaseException:
                if db.in_transaction:
                    db.rollback()
                raise

    def put(self, kind, patch, parents=None):
        """Store a revision based on the caller's observed parents when supplied."""
        _checked_kind(kind)
        _require(isinstance(patch, dict), "유효한 노트 또는 노트북이 필요합니다.")
        with self._connection(write=True) as db:
            return self._put(db, kind, patch, parents)

    def create_notes(self, patches):
        """Import new notes together so a failed import leaves no partial set."""
        with self._connection(write=True) as db:
            return [self._put(db, "note", {**patch, "id": None}, None) for patch in patches]

    def _put(self, db, kind, patch, parents):
        entity_id = patch.get("id")
        if entity_id is None:
            entity_id = str(uuid.uuid4())
        _require(_is_id(entity_id), "노트 또는 노트북 ID가 잘못되었습니다.")
        if parents is not None:
            parents = _checked_ids(parents)
        heads = _read_heads(db, entity_id)
        if heads:
            _require(heads[0]["type"] == kind, "이 ID에 다른 종류의 항목이 있습니다.")
        if parents is None:
            observed = heads
        else:
            observed = [_get_op(db, parent_id) for parent_id in parents]
            _require(all(parent is not None and parent["entity"]["id"] == entity_id for parent in observed),
                     "이 항목의 이전 수정 기록이 기기에 없습니다.")
            _require(observed or not heads, "수정할 이전 기록을 선택해 주세요.")
        now = time.time_ns() // 1_000_000
        previous_clock = db.execute("SELECT MAX(clock) FROM operations").fetchone()[0]
        clock = max(now, (previous_clock or 0) + 1)
        _require(clock <= _MAX_CLOCK, "수정 기록 순서가 허용 범위를 벗어났습니다.")
        base = max(observed, key=_op_key)["entity"] if observed else _default_entity(kind, entity_id, now)
        entity = _checked_entity(kind, {**base, **patch, "id": entity_id, "updated_at": now})
        op = {
            "id": str(uuid.uuid4()),
            "type": kind,
            "entity": entity,
            "clock": clock,
            "parents": sorted(parent["id"] for parent in observed),
        }
        _insert_op(db, op)
        return op

    def ingest(self, operations, account_key=None, *, drive_receipt=None, attachments=None):
        """Add operations, their attachments, and an optional Drive receipt atomically."""
        incoming = validate_ops(operations)
        attachment_records = ()
        if attachments is not None:
            _require(isinstance(attachments, dict), "첨부 파일 목록이 올바르지 않습니다.")
            attachment_records = [_attachment_record(raw, digest) for digest, raw in attachments.items()]
        if account_key is not None:
            _check_account(account_key)
        if drive_receipt is not None:
            _require(account_key is not None and isinstance(drive_receipt, tuple) and len(drive_receipt) == 4,
                     "Drive 파일 수신 기록이 잘못되었습니다.")
            _require(all(_checked_text(value, "Drive 파일 수신 기록이 잘못되었습니다.") for value in drive_receipt),
                     "Drive 파일 수신 기록이 비어 있습니다.")
        if not incoming and not attachment_records:
            return
        with self._connection(write=True) as db:
            if attachment_records:
                db.executemany("INSERT INTO attachments VALUES (?, ?, ?) ON CONFLICT DO NOTHING", attachment_records)
            for op in incoming:
                previous = _get_op(db, op["id"])
                if previous is not None:
                    _require(previous == op, "같은 ID의 동기화 기록에 다른 내용이 있습니다.")
                    continue
                kind = db.execute("SELECT kind FROM operations WHERE entity_id = ? LIMIT 1",
                                  (op["entity"]["id"],)).fetchone()
                _require(kind is None or kind[0] == op["type"], "이 ID에 다른 종류의 항목이 있습니다.")
                for parent_id in op["parents"]:
                    parent = _get_op(db, parent_id)
                    if parent is not None:
                        _check_parent(op, parent)
                for (child_payload,) in db.execute(
                    "SELECT operations.payload FROM parents JOIN operations ON operations.id = parents.operation_id "
                    "WHERE parents.parent_id = ?",
                    (op["id"],),
                ):
                    _check_parent(json.loads(child_payload), op)
                _insert_op(db, op)
            if account_key is not None:
                _record_uploads(db, account_key, (op["id"] for op in incoming))
            if drive_receipt is not None:
                workspace_key, file_id, revision, batch_key = drive_receipt
                db.execute(
                    "INSERT INTO drive_receipts (account_key, workspace_key, file_id, revision) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (account_key, workspace_key, file_id) DO UPDATE SET revision = excluded.revision",
                    (account_key, workspace_key, file_id, revision),
                )
                db.execute(
                    "DELETE FROM staged_uploads WHERE account_key = ? AND batch_key = ? AND file_id = ?",
                    (account_key, batch_key, file_id),
                )

    def drive_file_receipts(self, account_key, workspace_key):
        _check_account(account_key)
        with self._connection() as db:
            return dict(db.execute(
                "SELECT file_id, revision FROM drive_receipts WHERE account_key = ? AND workspace_key = ?",
                (account_key, workspace_key),
            ))

    def save_attachment(self, raw, *, expected_digest=None):
        record = _attachment_record(raw, expected_digest)
        with self._connection(write=True) as db:
            db.execute("INSERT INTO attachments VALUES (?, ?, ?) ON CONFLICT DO NOTHING", record)
        return record[0]

    def attachment(self, digest):
        with self._connection() as db:
            row = db.execute("SELECT content FROM attachments WHERE digest = ?", (_checked_digest(digest),)).fetchone()
            return row[0] if row is not None else None

    def attachment_hashes(self):
        with self._connection() as db:
            return [digest for (digest,) in db.execute("SELECT digest FROM attachments ORDER BY digest")]

    def attachment_checksums(self):
        with self._connection() as db:
            return dict(db.execute("SELECT digest, md5 FROM attachments"))

    def referenced_attachments(self):
        """Find attachments in every revision so restoring earlier notes restores their links."""
        digests = set()
        with self._connection() as db:
            for (payload,) in db.execute(
                "SELECT payload FROM operations WHERE kind = 'note' AND instr(payload, 'yeobaek-attachment:') > 0"
            ):
                digests.update(_ATTACHMENT_LINK.findall(json.loads(payload)["entity"]["body"]))
        return digests

    def operations(self):
        with self._connection() as db:
            return [json.loads(payload) for (payload,) in db.execute(
                "SELECT payload FROM operations ORDER BY clock, id")]

    def history(self, entity_id, limit=50):
        """Return the latest retained revisions, including conflicting branches."""
        with self._connection() as db:
            return [json.loads(payload) for (payload,) in db.execute(
                "SELECT payload FROM operations WHERE entity_id = ? ORDER BY clock DESC, id DESC LIMIT ?",
                (entity_id, limit),
            )]

    def setting(self, key):
        with self._connection() as db:
            row = db.execute("SELECT value FROM state WHERE key = ?", ("setting:" + key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key, value):
        with self._connection(write=True) as db:
            db.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("setting:" + key, value),
            )

    def pending(self, account_key):
        _check_account(account_key)
        with self._connection() as db:
            return [
                json.loads(payload)
                for (payload,) in db.execute(
                    "SELECT payload FROM operations WHERE NOT EXISTS ("
                    "SELECT 1 FROM uploads WHERE account_key = ? AND operation_id = operations.id"
                    ") ORDER BY clock, id",
                    (account_key,),
                )
            ]

    def mark_uploaded(self, ids, account_key):
        _check_account(account_key)
        ids = _checked_ids(ids)
        if not ids:
            return
        with self._connection(write=True) as db:
            try:
                _record_uploads(db, account_key, ids)
            except sqlite3.IntegrityError as error:
                raise ValueError("기기에 없는 수정 기록입니다.") from error

    def bind_drive_account(self, account_key):
        """Atomically bind the local workspace to one Drive account."""
        _check_account(account_key)
        with self._connection(write=True) as db:
            row = db.execute("SELECT value FROM state WHERE key = 'drive_account'").fetchone()
            if row is not None:
                return row[0] == account_key
            db.execute("INSERT INTO state (key, value) VALUES ('drive_account', ?)", (account_key,))
            return True

    def drive_upload_id(self, account_key, batch_key, candidate=None):
        """Read the Drive file ID reserved for this batch, or reserve a candidate."""
        _check_account(account_key)
        _require(bool(_checked_text(batch_key, "업로드 묶음 ID가 잘못되었습니다.")), "업로드 묶음 ID가 없습니다.")
        if candidate is not None:
            _require(bool(_checked_text(candidate, "Drive 파일 ID가 잘못되었습니다.")), "Drive 파일 ID가 없습니다.")
        with self._connection(write=candidate is not None) as db:
            row = db.execute("SELECT file_id FROM staged_uploads WHERE account_key = ? AND batch_key = ?",
                             (account_key, batch_key)).fetchone()
            if row is not None:
                return row[0]
            if candidate is not None:
                db.execute("INSERT INTO staged_uploads (account_key, batch_key, file_id) VALUES (?, ?, ?)",
                           (account_key, batch_key, candidate))
            return candidate

    def clear_drive_upload_id(self, account_key, batch_key, file_id):
        """Remove a completed reservation if it still matches the uploaded file."""
        _check_account(account_key)
        with self._connection(write=True) as db:
            result = db.execute(
                "DELETE FROM staged_uploads WHERE account_key = ? AND batch_key = ? AND file_id = ?",
                (account_key, batch_key, file_id),
            )
            return result.rowcount == 1

    def _entities(self, kind):
        winners = {}
        with self._connection() as db:
            for entity_id, payload in db.execute(
                f"SELECT entity_id, payload FROM operations WHERE kind = ? AND {_HEAD_FILTER} "
                "ORDER BY clock DESC, id DESC",
                (kind,),
            ):
                if entity_id not in winners:
                    winners[entity_id] = json.loads(payload)["entity"]
        return list(winners.values())

    def notes(self):
        return self._entities("note")

    def notebooks(self):
        return self._entities("notebook")

    def heads(self, entity_id):
        with self._connection() as db:
            return _read_heads(db, entity_id)

    def conflicts(self, entity_id):
        heads = self.heads(entity_id)
        return heads if len(heads) > 1 else []


NoteStore = Store
