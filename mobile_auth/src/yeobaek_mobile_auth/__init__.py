from typing import ClassVar

import flet as ft


@ft.control("YeobaekGoogleDriveAuth")
class GoogleDriveAuth(ft.Service):
    """Request Drive app-data access through the system Google Sign-In SDK."""

    client_id: str | None = None
    server_client_id: str | None = None
    _interactive_timeout: ClassVar[int] = 180

    async def token(self, *, interactive: bool = False) -> str | None:
        """Return a Drive access token, or None when sign-in is needed or canceled."""
        return await self._invoke_method(
            "token",
            {"interactive": interactive},
            timeout=self._interactive_timeout if interactive else 30,
        )

    async def clear_token(self) -> None:
        """Discard the SDK's last access token after an HTTP 401."""
        await self._invoke_method("clear_token", timeout=30)

    async def sign_out(self) -> None:
        """Clear the local Google Sign-In session."""
        await self._invoke_method("sign_out", timeout=30)


@ft.control("YeobaekMobilePdfPreview")
class MobilePdfPreview(ft.Service):
    """Render one local PDF page through the Android or iOS renderer."""

    async def render_page(self, data: bytes, index: int, width: int = 900) -> tuple[bytes, int]:
        """Return PNG bytes and total page count for a zero-based page index."""
        result = await self._invoke_method(
            "page", {"data": data, "index": index, "width": width}, timeout=30,
        )
        if (not isinstance(result, (list, tuple)) or len(result) != 2
                or not isinstance(result[0], bytes) or type(result[1]) is not int):
            raise ValueError("PDF 페이지를 읽을 수 없습니다.")
        return result[0], result[1]


__all__ = ["GoogleDriveAuth", "MobilePdfPreview"]
