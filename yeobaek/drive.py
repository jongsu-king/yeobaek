"""Google Drive synchronization for the native app. Network calls are blocking."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import ssl
import sys
import time
import webbrowser
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import RLock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from .store import ATTACHMENT_MAX_BYTES

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
WORKSPACE_PROPERTY = "yeobaekWorkspace"
WORKSPACE_VERSION = "python-operations-v1"
BATCH_PROPERTY = "yeobaekBatch"
BATCH_CHECKSUM_PROPERTY = "yeobaekChecksum"
ATTACHMENT_WORKSPACE_PROPERTY = "yeobaekAttachmentWorkspace"
ATTACHMENT_DIGEST_PROPERTY = "yeobaekAttachmentDigest"
JSON_TYPE = "application/json; charset=UTF-8"
BINARY_TYPE = "application/octet-stream"
MAX_BATCH_OPERATIONS = 250
MULTIPART_LIMIT = 4 * 1024 * 1024
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}


class DriveError(Exception):
    def __init__(self, message: str, status: int | None = None, *,
                 retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after


class AuthenticationError(DriveError):
    pass


@dataclass(frozen=True)
class DriveAccount:
    key: str
    name: str
    email: str


@dataclass(frozen=True)
class SyncResult:
    downloaded: int
    uploaded: int
    unreadable: int = 0


@dataclass(frozen=True)
class DesktopClient:
    client_id: str
    client_secret: str = field(repr=False)

    @classmethod
    def read(cls, path: str | Path) -> DesktopClient:
        try:
            data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
            installed = data["installed"]
            client_id = installed["client_id"]
            client_secret = installed.get("client_secret", "")
            if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
                raise ValueError
            if not isinstance(client_secret, str):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise AuthenticationError("Google Cloud의 데스크톱 앱 OAuth 클라이언트 JSON을 선택하세요.") from None
        return cls(client_id, client_secret)


class _GoogleRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urlsplit(newurl)
        origin = urlsplit(req.full_url)
        if destination.scheme != "https":
            return None
        same_host = origin.netloc == destination.netloc
        download_host = destination.hostname or ""
        if not same_host and (req.get_method() != "GET" or not download_host.endswith(".googleusercontent.com")):
            return None
        request = super().redirect_request(req, fp, code, msg, headers, newurl)
        if request is not None and not same_host:
            request.remove_header("Authorization")
        return request


_HTTP = build_opener(HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())), _GoogleRedirect())


def _url(base: str, **params) -> str:
    return f"{base}?{urlencode(params)}"


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _read_json(body: bytes) -> dict:
    try:
        result = json.loads(body)
    except (ValueError, RecursionError):
        raise DriveError("Google 드라이브에서 받은 데이터를 읽을 수 없습니다.") from None
    if not isinstance(result, dict):
        raise DriveError("Google 드라이브 데이터의 형식이 올바르지 않습니다.")
    return result


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    try:
        if value.isdecimal():
            return int(value)
        return max(0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def _rate_limited(error: HTTPError) -> bool:
    try:
        entries = json.loads(error.read(8192))["error"]["errors"]
        return any(entry["reason"] in _RATE_LIMIT_REASONS for entry in entries)
    except (ValueError, TypeError, KeyError, RecursionError, OSError, HTTPException):
        return False


def _retry_delay(error: DriveError, attempt: int) -> float | None:
    if not error.retryable or error.retry_after > 8:
        return None
    return max(error.retry_after, 2 ** attempt + random.uniform(0, 0.25))


def _request_once(url: str, *, method: str, data: bytes | None, headers: dict | None,
                  accepted: tuple[int, ...], response_limit: int | None = None):
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        try:
            response = _HTTP.open(request, timeout=20)
        except HTTPError as error:
            response = error
        with response:
            status = response.status
            if not isinstance(response, HTTPError) or status in accepted:
                body = response.read() if response_limit is None else response.read(response_limit + 1)
                if response_limit is not None and len(body) > response_limit:
                    raise DriveError("Drive에 있는 첨부 파일이 64 MiB를 초과합니다.")
                return status, response.headers, body
            rate_limited = status == 429 or (status == 403 and _rate_limited(response))
            retry_after = _retry_after(response.headers.get("Retry-After"))
    except (URLError, OSError, HTTPException):
        raise DriveError("Google에 접속할 수 없습니다. 인터넷 연결을 확인한 뒤 다시 시도하세요.",
                         retryable=True) from None
    if status == 401:
        raise AuthenticationError("Google 연결이 만료되었습니다. 다시 연결하세요.", status)
    messages = {
        403: "Google 드라이브 접근이 거부되었습니다. Drive API 설정과 계정 권한을 확인하세요.",
        404: "Google 드라이브 동기화 파일을 찾을 수 없습니다. 다시 동기화하세요.",
    }
    message = ("Google 드라이브 요청이 많습니다. 잠시 후 다시 동기화하세요." if rate_limited
               else messages.get(status, f"Google 요청에 실패했습니다 ({status}). 다시 시도하세요."))
    raise DriveError(message, status, retryable=rate_limited or status in _RETRYABLE_STATUS,
                     retry_after=retry_after)


def _request(url: str, *, method: str = "GET", data: bytes | None = None,
             headers: dict | None = None, accepted: tuple[int, ...] = (), retry: bool = False,
             response_limit: int | None = None):
    attempts = 3 if method in {"GET", "HEAD"} or retry else 1
    options = {} if response_limit is None else {"response_limit": response_limit}
    for attempt in range(attempts):
        try:
            return _request_once(url, method=method, data=data, headers=headers, accepted=accepted, **options)
        except DriveError as error:
            delay = _retry_delay(error, attempt)
            if delay is None or attempt == attempts - 1:
                raise
            time.sleep(delay)


def _token_request(client: DesktopClient, **parameters) -> dict:
    form = {"client_id": client.client_id, **parameters}
    if client.client_secret:
        form["client_secret"] = client.client_secret
    _, _, body = _request(
        TOKEN_URL, method="POST", data=urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}, accepted=(400, 401),
    )
    token = _read_json(body)
    if not token.get("access_token"):
        raise AuthenticationError("Google 로그인 정보를 확인할 수 없습니다. 계정과 OAuth 설정을 확인한 뒤 다시 연결하세요.")
    scopes = token.get("scope", DRIVE_SCOPE)
    if not isinstance(scopes, str) or DRIVE_SCOPE not in scopes.split():
        raise AuthenticationError("Google 드라이브 앱 데이터 권한을 허용한 뒤 다시 연결하세요.")
    return token


def _desktop_token(client: DesktopClient) -> dict:
    if sys.platform in {"ios", "android"} or hasattr(sys, "getandroidapilevel") or "ANDROID_ARGUMENT" in os.environ:
        raise AuthenticationError("모바일에서는 해당 기기의 Google 로그인 연결이 필요합니다.")

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    received: dict[str, str] = {}

    class Callback(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(3)
            super().setup()

        def log_message(self, *args):
            pass

        def do_GET(self):
            target = urlsplit(self.path)
            if target.path != "/":
                self.reply(404, "여백 로그인 주소가 아닙니다.")
                return
            try:
                values = parse_qs(target.query, max_num_fields=20)
            except ValueError:
                self.reply(400, "로그인 응답의 형식이 올바르지 않습니다.")
                return
            states = values.get("state", [])
            if len(states) != 1 or not hmac.compare_digest(states[0].encode(), state.encode()):
                self.reply(400, "로그인 요청을 확인할 수 없습니다. 여백에서 다시 시도하세요.")
                return
            codes = values.get("code", [])
            if len(codes) == 1 and "error" not in values:
                received["code"] = codes[0]
                self.reply(200, "Google 로그인 응답을 여백에 전달했습니다. 이 창을 닫고 앱으로 돌아가세요.")
            else:
                received["error"] = "Google 드라이브 권한이 허용되지 않았습니다."
                self.reply(400, "Google 권한이 허용되지 않았습니다. 여백에서 다시 시도할 수 있습니다.")

        def reply(self, status, message):
            body = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

    try:
        server = HTTPServer(("127.0.0.1", 0), Callback)
    except OSError:
        raise AuthenticationError("Google 로그인 응답을 받을 로컬 포트를 열 수 없습니다.") from None
    with server:
        server.timeout = 0.5
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        consent_url = _url(
            AUTH_URL, client_id=client.client_id, redirect_uri=redirect_uri, response_type="code",
            scope=DRIVE_SCOPE, code_challenge=challenge, code_challenge_method="S256", state=state,
            access_type="offline", prompt="select_account consent",
        )
        if not webbrowser.open(consent_url, new=1):
            raise AuthenticationError("기본 브라우저를 열 수 없습니다. 시스템의 기본 브라우저 설정을 확인하세요.")
        deadline = time.monotonic() + 180
        while not received and time.monotonic() < deadline:
            server.handle_request()

    if "code" not in received:
        raise AuthenticationError(received.get("error", "Google 로그인 시간이 초과되었습니다. 다시 연결하세요."))
    return _token_request(
        client, code=received["code"], redirect_uri=redirect_uri,
        grant_type="authorization_code", code_verifier=verifier,
    )


def _operation_batches(operations: list[dict]) -> Iterator[list[dict]]:
    batch = []
    size = 0
    for operation in sorted(operations, key=lambda item: item["id"]):
        length = len(_json_bytes(operation)) + 1
        if batch and (size + length > MULTIPART_LIMIT or len(batch) == MAX_BATCH_OPERATIONS):
            yield batch
            batch = []
            size = 0
        batch.append(operation)
        size += length
    if batch:
        yield batch


def _batch_key(operations: list[dict]) -> str:
    ids = [op["id"] for op in operations]
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def _batch_operations(content: bytes, properties: dict) -> list[dict]:
    batch = _read_json(content)
    batch_key = properties.get(BATCH_PROPERTY)
    checksum = properties.get(BATCH_CHECKSUM_PROPERTY)
    operations = batch.get("operations")
    if (batch.get("version") != 1 or not batch_key or batch.get("id") != batch_key
            or not isinstance(operations, list) or not 1 <= len(operations) <= MAX_BATCH_OPERATIONS
            or (checksum is not None and checksum != hashlib.sha256(content).hexdigest())):
        raise ValueError("Google 드라이브 동기화 파일의 형식이 올바르지 않습니다.")
    if not all(isinstance(op, dict) and isinstance(op.get("id"), str) for op in operations):
        raise ValueError("Google 드라이브 수정 기록 ID가 없습니다.")
    if _batch_key(operations) != batch_key:
        raise ValueError("Google 드라이브 수정 기록 ID가 일치하지 않습니다.")
    return operations


def _file_revision(file: dict, properties: dict) -> tuple[str, str] | None:
    version = file.get("version")
    checksum = file.get("md5Checksum")
    if not (isinstance(version, str) and re.fullmatch(r"[0-9]+", version)
            and isinstance(checksum, str) and re.fullmatch(r"[0-9a-fA-F]{32}", checksum)):
        return None
    checksum = checksum.lower()
    # Drive version covers metadata changes, and its checksum identifies the matching file bytes.
    identity = json.dumps([version, checksum, properties], sort_keys=True).encode()
    return hashlib.sha256(identity).hexdigest(), checksum


class DriveSession:
    """Drive connection and sync. Run connect, sync, and disconnect on a worker."""

    def __init__(self, store):
        self._store = store
        self._lock = RLock()
        self._token: str | None = None
        self._refresh: Callable[[], tuple[str, int]] | None = None
        self._expires_at = 0.0
        self.account: DriveAccount | None = None

    @property
    def connected(self) -> bool:
        return bool(self.account and (self._refresh or (self._token and time.monotonic() < self._expires_at)))

    def connect(self, credentials_path: str | Path) -> DriveAccount:
        """Authorize with a downloaded Google OAuth Desktop app JSON."""
        with self._lock:
            client = DesktopClient.read(credentials_path)
            token = _desktop_token(client)
            refresh_token = token.get("refresh_token")

            def refresh_access():
                response = _token_request(client, grant_type="refresh_token", refresh_token=refresh_token)
                return response["access_token"], response.get("expires_in", 3600)

            return self.connect_token(token["access_token"], token.get("expires_in", 3600),
                                      refresh_access if refresh_token else None)

    def connect_token(self, access_token: str, expires_in: int = 3600,
                      refresh: Callable[[], tuple[str, int]] | None = None) -> DriveAccount:
        """Attach an OAuth token from a platform adapter with the Drive appdata scope."""
        with self._lock:
            account = self._get_account(access_token)
            if not self._store.bind_drive_account(account.key):
                raise AuthenticationError("이 기기에 이미 연결했던 Google 계정이 있습니다. 같은 계정으로 다시 연결하세요.")
            self._set_token(access_token, expires_in)
            self._refresh = refresh
            self.account = account
            return account

    def disconnect(self) -> None:
        """Release this session's tokens without changing the workspace's account binding."""
        with self._lock:
            self._token = self._refresh = None
            self._expires_at = 0.0
            self.account = None

    def sync(self) -> SyncResult:
        """Pull and acknowledge Drive revisions, then send pending local revisions."""
        with self._lock:
            if not self.account:
                raise AuthenticationError("Google 드라이브에 먼저 연결하세요.")
            account_key = self.account.key
            downloaded, unreadable = self._download(account_key)
            uploaded = 0
            for operations in _operation_batches(self._store.pending(account_key)):
                uploaded += self._upload(account_key, operations)
            unreadable += self._sync_attachments(account_key)
            return SyncResult(downloaded, uploaded, unreadable)

    @staticmethod
    def _get_account(token: str) -> DriveAccount:
        if not isinstance(token, str) or not token or not token.isascii() or any(char.isspace() for char in token):
            raise AuthenticationError("Google 액세스 토큰의 형식이 올바르지 않습니다.")
        _, _, body = _request(
            _url(f"{DRIVE_URL}/about", fields="user(permissionId,displayName,emailAddress)"),
            headers={"Authorization": f"Bearer {token}"},
        )
        user = _read_json(body).get("user")
        if not isinstance(user, dict) or not isinstance(user.get("permissionId"), str) or not user["permissionId"]:
            raise AuthenticationError("연결한 Google 계정을 확인할 수 없습니다.")
        return DriveAccount(user["permissionId"], user.get("displayName", ""), user.get("emailAddress", ""))

    def _set_token(self, token: str, expires_in: int) -> None:
        try:
            lifetime = max(1, int(expires_in))
        except (TypeError, ValueError, OverflowError):
            raise AuthenticationError("Google 연결 유효 시간을 확인할 수 없습니다.") from None
        self._token = token
        self._expires_at = time.monotonic() + max(1, lifetime - 30)

    def _access_token(self, *, refresh_now: bool = False) -> str:
        if not refresh_now and self._token and time.monotonic() < self._expires_at:
            return self._token
        if not self._refresh or not self.account:
            self.disconnect()
            raise AuthenticationError("Google 연결이 만료되었습니다. 다시 연결하세요.")
        try:
            token, expires = self._refresh()
            if self._get_account(token).key != self.account.key:
                raise AuthenticationError("Google 로그인 계정이 달라졌습니다. 같은 계정으로 다시 연결하세요.")
            self._set_token(token, expires)
        except AuthenticationError:
            self.disconnect()
            raise
        return token

    def _drive_request(self, url: str, *, method: str = "GET", data: bytes | None = None,
                       headers: dict | None = None, accepted: tuple[int, ...] = (), retry: bool = False,
                       response_limit: int | None = None):
        token = self._access_token()
        for auth_attempt in range(2):
            try:
                return _request(url, method=method, data=data, accepted=accepted, retry=retry,
                                response_limit=response_limit,
                                headers={**(headers or {}), "Authorization": f"Bearer {token}"})
            except AuthenticationError:
                if auth_attempt:
                    self.disconnect()
                    raise
                token = self._access_token(refresh_now=True)

    def _listed_files(self, property_name: str, fields: str) -> Iterator[dict]:
        page_tokens = set()
        restarted = False
        params = {
            "spaces": "appDataFolder",
            "q": f"appProperties has {{ key='{property_name}' and value='{WORKSPACE_VERSION}' }}",
            "fields": f"nextPageToken,files(id,{fields})",
            "pageSize": 1000,
        }
        while True:
            try:
                _, _, body = self._drive_request(_url(f"{DRIVE_URL}/files", **params))
            except DriveError as error:
                if error.status != 400 or "pageToken" not in params or restarted:
                    raise
                # Drive can reject a page token after the file list changes.
                params.pop("pageToken")
                page_tokens.clear()
                restarted = True
                continue
            listing = _read_json(body)
            for file in listing.get("files", []):
                if isinstance(file, dict) and isinstance(file.get("id"), str) and file["id"]:
                    yield file
            next_page = listing.get("nextPageToken")
            if not next_page:
                return
            if not isinstance(next_page, str) or next_page in page_tokens:
                raise DriveError("Google 드라이브 파일 목록을 끝까지 확인할 수 없습니다. 다시 동기화하세요.")
            page_tokens.add(next_page)
            params["pageToken"] = next_page

    def _download(self, account_key: str) -> tuple[int, int]:
        downloaded = 0
        unreadable = set()
        workspace_key = f"appDataFolder:{WORKSPACE_PROPERTY}={WORKSPACE_VERSION}"
        receipts = self._store.drive_file_receipts(account_key, workspace_key)
        for file in self._listed_files(WORKSPACE_PROPERTY, "version,md5Checksum,appProperties"):
            file_id = file["id"]
            properties = file.get("appProperties")
            if not isinstance(properties, dict):
                unreadable.add(file_id)
                continue
            revision = _file_revision(file, properties)
            if revision is not None and receipts.get(file_id) == revision[0]:
                unreadable.discard(file_id)
                continue
            try:
                _, _, content = self._drive_request(self._file_url(file_id, alt="media"))
            except DriveError as error:
                if error.status != 404:
                    raise
                unreadable.add(file_id)
                continue
            if revision is not None and hashlib.md5(content, usedforsecurity=False).hexdigest() != revision[1]:
                unreadable.add(file_id)
                continue
            try:
                operations = _batch_operations(content, properties)
                batch_key = properties[BATCH_PROPERTY]
                receipt = (workspace_key, file_id, revision[0], batch_key) if revision is not None else None
                self._store.ingest(operations, account_key, drive_receipt=receipt)
            except (DriveError, ValueError):
                unreadable.add(file_id)
                continue
            if revision is not None:
                receipts[file_id] = revision[0]
            else:
                self._store.clear_drive_upload_id(account_key, batch_key, file_id)
            unreadable.discard(file_id)
            downloaded += len(operations)
        return downloaded, len(unreadable)

    @staticmethod
    def _file_url(file_id: str, **params) -> str:
        return _url(f"{DRIVE_URL}/files/{quote(file_id, safe='')}", **params)

    def _reserved_upload(self, account_key: str, batch_key: str) -> str:
        existing = self._store.drive_upload_id(account_key, batch_key)
        if existing:
            return existing
        _, _, body = self._drive_request(_url(f"{DRIVE_URL}/files/generateIds", count=1, space="appDataFolder", type="files"))
        generated = _read_json(body).get("ids", [])
        if not generated:
            raise DriveError("Google 드라이브에서 파일 ID를 발급받지 못했습니다.")
        return self._store.drive_upload_id(account_key, batch_key, generated[0])

    def _upload(self, account_key: str, operations: list[dict]) -> int:
        ids = [op["id"] for op in operations]
        key = _batch_key(operations)
        file_id = self._reserved_upload(account_key, key)
        payload = _json_bytes({"version": 1, "id": key, "operations": operations})
        metadata = _json_bytes({
            "id": file_id,
            "name": f"Yeobaek {key[:12]}.json",
            "mimeType": "application/json",
            "parents": ["appDataFolder"],
            "appProperties": {
                WORKSPACE_PROPERTY: WORKSPACE_VERSION,
                BATCH_PROPERTY: key,
                BATCH_CHECKSUM_PROPERTY: hashlib.sha256(payload).hexdigest(),
            },
        })
        if len(payload) <= MULTIPART_LIMIT:
            status = self._upload_multipart(metadata, payload)
        else:
            status = self._upload_large(metadata, payload)
        if status == 409:
            if not self._uploaded_operations_match(file_id, key, operations):
                self._store.clear_drive_upload_id(account_key, key, file_id)
                raise DriveError("Drive에 있는 기록이 이 기기의 업로드와 다릅니다. 다시 동기화하세요.")
        self._store.mark_uploaded(ids, account_key)
        self._store.clear_drive_upload_id(account_key, key, file_id)
        return len(ids)

    def _uploaded_operations_match(self, file_id: str, key: str, operations: list[dict]) -> bool:
        _, _, body = self._drive_request(self._file_url(file_id, fields="appProperties"))
        properties = _read_json(body).get("appProperties")
        if (not isinstance(properties, dict) or properties.get(WORKSPACE_PROPERTY) != WORKSPACE_VERSION
                or properties.get(BATCH_PROPERTY) != key):
            return False
        _, _, content = self._drive_request(self._file_url(file_id, alt="media"))
        try:
            return _batch_operations(content, properties) == operations
        except (DriveError, ValueError):
            return False

    @staticmethod
    def _attachment_properties(digest: str) -> dict[str, str]:
        return {ATTACHMENT_WORKSPACE_PROPERTY: WORKSPACE_VERSION, ATTACHMENT_DIGEST_PROPERTY: digest}

    @staticmethod
    def _attachment_matches(file: dict, digest: str, md5: str) -> bool:
        sha256 = file.get("sha256Checksum")
        return sha256 == digest if sha256 else file.get("md5Checksum") == md5

    def _sync_attachments(self, account_key: str) -> int:
        referenced = sorted(self._store.referenced_attachments())
        if not referenced:
            return 0
        local = self._store.attachment_checksums()
        remote = {}
        try:
            for file in self._listed_files(ATTACHMENT_WORKSPACE_PROPERTY, "sha256Checksum,md5Checksum,appProperties"):
                properties = file.get("appProperties")
                if not isinstance(properties, dict) or properties.get(ATTACHMENT_WORKSPACE_PROPERTY) != WORKSPACE_VERSION:
                    continue
                digest = properties.get(ATTACHMENT_DIGEST_PROPERTY)
                if isinstance(digest, str):
                    remote.setdefault(digest, []).append(file)
        except DriveError:
            return len(referenced)

        unavailable = 0
        for index, digest in enumerate(referenced):
            files = remote.get(digest, [])
            try:
                if digest in local:
                    if not any(self._attachment_matches(file, digest, local[digest]) for file in files):
                        self._upload_attachment(account_key, digest, local[digest])
                elif not self._download_attachment(digest, files):
                    unavailable += 1
            except DriveError as error:
                if error.retryable or isinstance(error, AuthenticationError):
                    return unavailable + len(referenced) - index
                unavailable += 1
        return unavailable

    def _download_attachment(self, digest: str, files: list[dict]) -> bool:
        for file in files:
            checksum = file.get("sha256Checksum")
            if checksum and checksum != digest:
                continue
            try:
                _, _, raw = self._drive_request(self._file_url(file["id"], alt="media"),
                                                response_limit=ATTACHMENT_MAX_BYTES)
                self._store.save_attachment(raw, expected_digest=digest)
                return True
            except ValueError:
                continue
            except DriveError as error:
                if error.retryable or isinstance(error, AuthenticationError):
                    raise
        return False

    def _upload_attachment(self, account_key: str, digest: str, md5: str) -> None:
        payload = self._store.attachment(digest)
        if payload is None or hashlib.sha256(payload).hexdigest() != digest:
            raise DriveError("기기의 첨부 파일을 확인할 수 없습니다.")
        key = f"attachment:{WORKSPACE_VERSION}:{digest}"
        file_id = self._reserved_upload(account_key, key)
        properties = self._attachment_properties(digest)
        metadata = _json_bytes({
            "id": file_id,
            "name": f"Yeobaek Attachment {digest}",
            "mimeType": BINARY_TYPE,
            "parents": ["appDataFolder"],
            "appProperties": properties,
        })
        upload = self._upload_multipart if len(payload) <= MULTIPART_LIMIT else self._upload_large
        if upload(metadata, payload, BINARY_TYPE) == 409:
            _, _, body = self._drive_request(self._file_url(file_id, fields="sha256Checksum,md5Checksum,appProperties"))
            file = _read_json(body)
            if file.get("appProperties") != properties or not self._attachment_matches(file, digest, md5):
                self._store.clear_drive_upload_id(account_key, key, file_id)
                raise DriveError("Drive에 있는 첨부 파일이 이 기기의 파일과 다릅니다.")
        self._store.clear_drive_upload_id(account_key, key, file_id)

    def _upload_multipart(self, metadata: bytes, payload: bytes, content_type: str = JSON_TYPE) -> int:
        boundary = f"yeobaek-{secrets.token_hex(16)}"
        metadata_header = f"--{boundary}\r\nContent-Type: {JSON_TYPE}\r\n\r\n".encode()
        media_header = f"\r\n--{boundary}\r\nContent-Type: {content_type}\r\n\r\n".encode()
        data = metadata_header + metadata + media_header + payload + f"\r\n--{boundary}--\r\n".encode()
        status, _, _ = self._drive_request(
            _url(UPLOAD_URL, uploadType="multipart", fields="id"), method="POST", data=data,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, accepted=(409,), retry=True,
        )
        return status

    def _upload_large(self, metadata: bytes, payload: bytes, content_type: str = JSON_TYPE) -> int:
        status, headers, _ = self._drive_request(
            _url(UPLOAD_URL, uploadType="resumable", fields="id"), method="POST", data=metadata,
            headers={"Content-Type": JSON_TYPE, "X-Upload-Content-Type": content_type,
                     "X-Upload-Content-Length": str(len(payload))}, accepted=(409,), retry=True,
        )
        if status == 409:
            return status
        location = headers.get("Location", "")
        if not location.startswith(f"{UPLOAD_URL}?"):
            raise DriveError("Google 드라이브의 대용량 업로드 주소를 확인할 수 없습니다.")
        size = len(payload)
        start = 0
        for attempt in range(3):
            headers = {"Content-Type": content_type}
            if attempt:
                headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
            try:
                status, headers, _ = self._drive_request(location, method="PUT", data=payload[start:],
                                                          headers=headers, accepted=(308, 409))
            except DriveError as error:
                delay = _retry_delay(error, attempt)
                if delay is None:
                    raise
                time.sleep(delay)
                status, headers, _ = self._drive_request(
                    location, method="PUT", data=b"", headers={"Content-Range": f"bytes */{size}"},
                    accepted=(308, 409), retry=True,
                )
            if status != 308:
                return status
            received = headers.get("Range")
            if not received:
                start = 0
                continue
            match = re.fullmatch(r"bytes=0-(\d+)", received)
            if match is None or (start := int(match[1]) + 1) >= size:
                raise DriveError("Drive에서 받은 업로드 진행 위치가 올바르지 않습니다.")
        raise DriveError("Drive 업로드를 아직 완료하지 못했습니다. 다시 동기화하세요.")
