import asyncio
import base64
import binascii
import inspect
import json
import mimetypes
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import flet as ft

from .drive import AuthenticationError, DriveSession
from .notes import (
    TEMPLATES, attachment_filename, attachment_link, attachment_links, folded,
    matches_query, parse_query, read_note, task_lines, template_note, without_attachment_links,
)
from .previews import (
    PDFPage, SHEET_COLUMNS, SHEET_ROWS, preview_kind, raster_pdf_page, sheet_page, spreadsheet_column, text_pdf_page,
)
from .store import ATTACHMENT_MAX_BYTES, Store

PINE = "#173C33"
LIME = "#D9ED9D"
PAPER = "#FFFEFA"
PANEL = "#F4F6F0"
LINE = "#E2E8DE"
INK = "#21382C"
MUTED = "#5F7467"
SIDEBAR_TEXT = "#D6E4D8"
MOBILE = (ft.PagePlatform.ANDROID, ft.PagePlatform.IOS)
VIEW_LABELS = {
    "all": "모든 노트", "pinned": "즐겨찾기", "tasks": "할 일",
    "unfiled": "미분류", "trash": "휴지통",
}


@dataclass(frozen=True)
class InlineAttachment:
    note_id: str
    name: str
    digest: str
    reference: str
    kind: str
    content: bytes


def title_of(note):
    return note["title"].strip() or "제목 없는 노트"


def day(timestamp):
    return datetime.fromtimestamp(timestamp / 1000).strftime("%Y.%m.%d")


def heading(value, size=20):
    return ft.Text(value, size=size, weight=ft.FontWeight.BOLD, color=INK)


def action(name, callback, icon=None, *, primary=False):
    if primary:
        return ft.Button(name, icon=icon, bgcolor=PINE, color=PAPER, on_click=callback)
    return ft.TextButton(name, icon=icon, on_click=callback)


def icon_button(icon, label, callback, color=MUTED):
    return ft.IconButton(icon, tooltip=label, icon_color=color, on_click=callback)


def note_button(label, icon, handler, *, active=False):
    return ft.Container(
        ft.Row([ft.Icon(icon, color=LIME if active else "#AEC7B6", size=17),
                ft.Text(label, color=SIDEBAR_TEXT, expand=True, max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS, weight=ft.FontWeight.W_500)], spacing=10),
        padding=ft.Padding.symmetric(horizontal=10, vertical=9),
        bgcolor="#2C5345" if active else None,
        border_radius=8, on_click=handler, ink=True,
    )


class Workspace:
    def __init__(self, page, data_dir):
        self.page = page
        self.data_dir = data_dir
        self.store = Store(data_dir / "notes.sqlite3")
        self.drive = DriveSession(self.store)
        self.file_picker = ft.FilePicker()
        self.url_launcher = ft.UrlLauncher()
        self.share = ft.Share() if page.platform != ft.PagePlatform.LINUX else None
        self.mobile_auth = None
        self.mobile_pdf = None
        self.inline_attachment = None
        self.inline_revision = 0
        self.drive_busy = False
        self.drive_status = "동기화 연결"
        self.sync_requested = asyncio.Event()
        self.selected_id = None
        self.editor_parents = []
        self.editor_values = ("", "")
        self.draft = None
        self.save_task = None
        self.view = "all"
        self.stage = "list"
        self.previewing = False
        self.sort_key = self.store.setting("note-sort") or "updated"
        self.notes = []
        self.books = {}
        self.list_title = heading("모든 노트", 23)
        self.list_count = ft.Text(color=MUTED, size=11)
        self.sort = ft.PopupMenuButton(icon=ft.Icons.SORT, tooltip="정렬 변경")
        self.search = ft.TextField(
            hint_text="노트 검색", prefix_icon=ft.Icons.SEARCH, dense=True,
            border_color=LINE, bgcolor=PAPER, border_radius=9,
            on_change=self.refresh_list, text_size=13,
            suffix=icon_button(ft.Icons.HELP_OUTLINE, "검색 방법", self.handler(self.search_help)),
        )
        self.cards = ft.ListView(expand=True, spacing=5, padding=ft.Padding.only(bottom=12))
        self.nav = ft.Column(spacing=3, expand=True, scroll=ft.ScrollMode.AUTO)
        self.drive_text = ft.Text(self.drive_status, size=11, color="#BED2C2", max_lines=2)
        self.title = ft.TextField(
            hint_text="제목 없는 노트", border=ft.InputBorder.NONE, text_size=34,
            color=INK, text_style=ft.TextStyle(weight=ft.FontWeight.BOLD),
            content_padding=0, on_change=self.changed, on_blur=self.save,
        )
        self.body = ft.TextField(
            hint_text="여기에 내용을 적어보세요.", border=ft.InputBorder.NONE,
            multiline=True, min_lines=12, text_size=16, color=INK,
            content_padding=0, on_change=self.changed, on_blur=self.save,
            smart_dashes_type=False, smart_quotes_type=False,
        )
        self.preview = ft.Column(spacing=10, visible=False)
        self.editor_header = ft.Row(spacing=4)
        self.toolbar = ft.Row(spacing=1, scroll=ft.ScrollMode.AUTO)
        self.tags = ft.Row(wrap=True, spacing=7, run_spacing=5)
        self.attachments = ft.Row(wrap=True, spacing=7, run_spacing=6, visible=False)
        self.inline_preview = ft.Container(
            visible=False, padding=12, bgcolor=PANEL, border_radius=9,
            border=ft.Border.all(1, LINE),
        )
        self.note_date = ft.Text(color=MUTED, size=11)
        self.editor_state = ft.Text(color=MUTED, size=11, expand=True)
        self.characters = ft.Text(color=MUTED, size=11)
        self.empty = self.empty_editor()
        self.editor = self.editor_panel()
        self.editor_host = ft.Container(expand=True, bgcolor=PAPER)
        self.sidebar = self.sidebar_panel()
        self.list_panel = self.notes_panel()
        self.row = ft.Row([self.sidebar, self.list_panel, self.editor_host],
                          expand=True, spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH)

    @property
    def narrow(self):
        return (self.page.width or 1100) < 850 or self.page.platform in MOBILE

    @property
    def note(self):
        return next((note for note in self.notes if note["id"] == self.selected_id), None)

    def handler(self, callback, *args):
        async def call(_):
            try:
                result = callback(*args)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self.message(str(exc) or "작업을 완료하지 못했어요.")
        return call

    def message(self, message):
        self.page.show_dialog(ft.SnackBar(ft.Text(message, color=PAPER), bgcolor=PINE))

    def sidebar_panel(self):
        emblem = ft.Container(
            ft.Icon(ft.Icons.AUTO_STORIES_OUTLINED, size=23, color=PINE),
            bgcolor=LIME, border_radius=11, padding=9,
        )
        brand = ft.Row([emblem, ft.Text("여백", color=PAPER, size=25, weight=ft.FontWeight.BOLD)], spacing=11)
        new = ft.Button("새 노트", icon=ft.Icons.ADD, bgcolor=LIME, color=PINE, height=44,
                        on_click=self.handler(self.new_note))
        templates = note_button("템플릿으로 시작", ft.Icons.DASHBOARD_OUTLINED, self.handler(self.templates_dialog))
        cloud = ft.Container(
            ft.Row([ft.Icon(ft.Icons.CLOUD_OUTLINED, color=LIME, size=19),
                    ft.Column([ft.Text("Google Drive", color=PAPER, weight=ft.FontWeight.W_600, size=13),
                               self.drive_text], spacing=1, expand=True),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=SIDEBAR_TEXT, size=16)], spacing=8),
            bgcolor="#24493E", border_radius=9, padding=10, ink=True,
            on_click=self.handler(self.drive_dialog),
        )
        settings = note_button("설정 및 백업", ft.Icons.SETTINGS_OUTLINED, self.handler(self.settings_dialog))
        return ft.Container(
            ft.Column([brand, ft.Container(height=6), new, templates, self.nav, cloud, settings],
                      spacing=11, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            width=224, bgcolor=PINE, padding=ft.Padding(15, 24, 15, 14),
        )

    def notes_panel(self):
        self.list_menu = icon_button(ft.Icons.MENU, "메뉴", self.handler(self.show_navigation))
        self.list_new = icon_button(ft.Icons.ADD, "새 노트", self.handler(self.new_note))
        self.book_manage = icon_button(ft.Icons.EDIT_OUTLINED, "노트북 이름 변경", self.handler(self.rename_notebook))
        title_row = ft.Row([self.list_menu, ft.Container(self.list_title, expand=True), self.book_manage, self.list_new], spacing=4)
        options = ft.Row([self.list_count, self.sort], alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
        return ft.Container(
            ft.Column([title_row, self.search, options, self.cards],
                      expand=True, spacing=9, horizontal_alignment=ft.CrossAxisAlignment.STRETCH),
            width=303, bgcolor=PANEL, border=ft.Border.only(right=ft.BorderSide(1, LINE)),
            padding=ft.Padding(14, 19, 14, 0),
        )

    def editor_panel(self):
        paper = ft.Column([self.note_date, self.title, self.tags, self.attachments, self.inline_preview,
                           ft.Container(height=8), self.body, self.preview],
                          spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        self.paper = ft.Container(paper, padding=ft.Padding(45, 39, 45, 18))
        scroll = ft.Column([self.paper], expand=True, scroll=ft.ScrollMode.AUTO,
                           horizontal_alignment=ft.CrossAxisAlignment.STRETCH, spacing=0)
        footer = ft.Container(ft.Row([self.editor_state, self.characters], spacing=8), padding=ft.Padding.symmetric(horizontal=18, vertical=8))
        return ft.Column([
            ft.Container(self.editor_header, height=54, padding=ft.Padding.symmetric(horizontal=12)),
            ft.Divider(height=1, color=LINE),
            ft.Container(self.toolbar, height=45, padding=ft.Padding.symmetric(horizontal=12)),
            ft.Divider(height=1, color=LINE), scroll, ft.Divider(height=1, color=LINE), footer,
        ], spacing=0, expand=True, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    def empty_editor(self):
        return ft.Container(
            ft.Column([
                ft.Container(ft.Icon(ft.Icons.EDIT_NOTE, color=PINE, size=38),
                             padding=13, bgcolor="#E9F0DF", border_radius=16),
                heading("첫 노트를 써 보세요", 23),
                ft.Text("메모를 노트북과 태그로 정리할 수 있어요.", size=13, color=MUTED, text_align=ft.TextAlign.CENTER),
                action("새 노트", self.handler(self.new_note), ft.Icons.ADD, primary=True),
            ], tight=True, spacing=13, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            alignment=ft.Alignment.CENTER, padding=24, expand=True,
        )

    def refresh_data(self):
        self.notes = self.store.notes()
        self.books = {book["id"]: book for book in self.store.notebooks() if not book["deleted_at"]}

    def write_revision(self, kind, patch, parents=None):
        revision = self.store.put(kind, patch, parents=parents)
        self.sync_requested.set()
        return revision

    def update_layout(self, _=None):
        self.sidebar.visible = not self.narrow
        self.list_panel.visible = not self.narrow or self.stage == "list"
        self.list_panel.expand = self.narrow
        self.list_panel.width = None if self.narrow else 303
        self.editor_host.visible = not self.narrow or self.stage == "editor"
        self.list_menu.visible = self.narrow
        self.list_new.visible = self.narrow
        self.paper.padding = ft.Padding(20, 25, 20, 12) if self.narrow else ft.Padding(45, 39, 45, 18)
        if self.editor_header.controls:
            self.editor_header.controls[0].visible = self.narrow
        self.page.update()

    def nav_rows(self, mobile=False):
        select = self.handler
        active_count = sum(not note["deleted_at"] for note in self.notes)
        icons = {
            "all": ft.Icons.NOTES, "pinned": ft.Icons.STAR_BORDER, "tasks": ft.Icons.CHECKLIST,
            "unfiled": ft.Icons.INBOX_OUTLINED, "trash": ft.Icons.DELETE_OUTLINE,
        }
        rows = [note_button(label + (f"  {active_count}" if name == "all" else ""), icons[name],
                            select(self.set_view, name, mobile), active=self.view == name)
                for name, label in VIEW_LABELS.items()]
        rows += [ft.Divider(color="#3F6155"), ft.Row([
            ft.Text("노트북", color="#BED2C2", size=11, weight=ft.FontWeight.BOLD, expand=True),
            icon_button(ft.Icons.ADD, "노트북 추가", select(self.add_notebook, mobile), SIDEBAR_TEXT),
        ])]
        for book_id, book in sorted(self.books.items(), key=lambda entry: folded(entry[1]["name"])):
            rows.append(note_button(book["name"], ft.Icons.FOLDER_OUTLINED, select(self.set_view, "book:" + book_id, mobile),
                                    active=self.view == "book:" + book_id))
        tags = sorted({tag for note in self.notes if not note["deleted_at"] for tag in note["tags"]})
        if tags:
            rows += [ft.Divider(color="#3F6155"), ft.Text("태그", color="#BED2C2", size=11, weight=ft.FontWeight.BOLD)]
            rows.extend(note_button(tag, ft.Icons.TAG, select(self.set_view, "tag:" + tag, mobile),
                                    active=self.view == "tag:" + tag) for tag in tags)
        return rows

    def show_navigation(self):
        rows = self.nav_rows(mobile=True)
        rows.extend([
            ft.Divider(color="#3F6155"),
            note_button("템플릿으로 시작", ft.Icons.DASHBOARD_OUTLINED, self.handler(self.templates_dialog, True)),
            note_button("Google Drive", ft.Icons.CLOUD_OUTLINED, self.handler(self.drive_dialog, True)),
            note_button("설정 및 백업", ft.Icons.SETTINGS_OUTLINED, self.handler(self.settings_dialog, True)),
        ])
        self.page.show_dialog(ft.AlertDialog(
            title=ft.Text("여백", color=PAPER, weight=ft.FontWeight.BOLD), bgcolor=PINE,
            content=ft.Container(ft.Column(rows, scroll=ft.ScrollMode.AUTO, spacing=3), width=265, height=390),
            actions=[ft.TextButton("닫기", style=ft.ButtonStyle(color=LIME), on_click=self.handler(self.page.pop_dialog))],
        ))

    def view_name(self):
        if self.view.startswith("book:"):
            return self.books.get(self.view[5:], {}).get("name", "노트북")
        if self.view.startswith("tag:"):
            return "# " + self.view[4:]
        return VIEW_LABELS[self.view]

    def book_name(self, book_id):
        return self.books.get(book_id, {}).get("name", "미분류")

    def matches(self):
        query = parse_query(self.search.value or "")
        selected = []
        for note in self.notes:
            if bool(note["deleted_at"]) != (self.view == "trash"):
                continue
            if self.view == "pinned" and not note["pinned"]:
                continue
            if self.view == "tasks" and not any(task[2] == " " for _, task in task_lines(note["body"])):
                continue
            if self.view == "unfiled" and note["notebook_id"] in self.books:
                continue
            if self.view.startswith("book:") and note["notebook_id"] != self.view[5:]:
                continue
            if self.view.startswith("tag:") and self.view[4:] not in note["tags"]:
                continue
            if not query or matches_query(note, self.book_name(note["notebook_id"]), query):
                selected.append(note)
        if self.sort_key == "title":
            return sorted(selected, key=lambda note: folded(title_of(note)))
        date_key = "created_at" if self.sort_key == "created" else "updated_at"
        return sorted(selected, key=lambda note: note[date_key], reverse=True)

    def build_list(self):
        self.list_title.value = self.view_name()
        notes = self.matches()
        self.list_count.value = f"노트 {len(notes)}개"
        self.book_manage.visible = self.view.startswith("book:") and self.view[5:] in self.books
        sort_choices = {"updated": "최근 수정순", "created": "최근 작성순", "title": "제목순"}
        self.sort.tooltip = sort_choices.get(self.sort_key, sort_choices["updated"])
        self.sort.items = [
            ft.PopupMenuItem(content=label, checked=key == self.sort_key,
                             on_click=self.handler(self.change_sort, key))
            for key, label in sort_choices.items()
        ]
        cards = []
        for note in notes:
            selected = self.selected_id == note["id"]
            header = [ft.Text(title_of(note), weight=ft.FontWeight.W_700, color=INK,
                              size=14, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, expand=True)]
            if note["pinned"]:
                header.append(ft.Icon(ft.Icons.STAR, size=13, color="#6B8B50"))
            snippet = " ".join(without_attachment_links(note["body"]).split())
            if not snippet:
                count = len(attachment_links(note["body"]))
                snippet = f"첨부 파일 {count}개" if count else "본문 없음"
            meta = day(note["updated_at"])[5:] + "  ·  " + self.book_name(note["notebook_id"])
            card = ft.Container(
                ft.Column([ft.Row(header), ft.Text(snippet[:250], size=12, color=MUTED, max_lines=2,
                                                   overflow=ft.TextOverflow.ELLIPSIS),
                           ft.Text(meta, size=10, color=MUTED)], spacing=5),
                padding=12, bgcolor="#E8F0E1" if selected else PAPER,
                border=ft.Border.all(1, "#D2E2C9" if selected else LINE), border_radius=9,
                on_click=self.handler(self.select_note, note["id"]), ink=True,
            )
            cards.append(card)
        if not cards:
            cards.append(ft.Container(ft.Text("검색 결과가 없어요." if self.search.value else "이곳에 노트가 없어요.",
                                              size=13, color=MUTED, text_align=ft.TextAlign.CENTER), padding=20))
        self.cards.controls = cards
        self.nav.controls = self.nav_rows()

    async def refresh_list(self, _=None):
        self.build_list()
        self.page.update()

    async def change_sort(self, key):
        self.sort_key = key
        self.store.set_setting("note-sort", key)
        await self.refresh_list()

    def search_help(self):
        tips = [
            "제목, 본문, 태그, 노트북 이름을 함께 검색합니다.",
            'title:"회의 기록"  tag:업무  book:프로젝트',
            "is:pinned는 즐겨찾기, is:task는 미완료 할 일, is:file은 첨부가 있는 노트입니다.",
            "앞에 -를 붙이면 제외합니다. 예시  -tag:보관",
        ]
        self.page.show_dialog(ft.AlertDialog(
            title=heading("노트 검색"),
            content=ft.Column([ft.Text(tip, selectable=True) for tip in tips], tight=True),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    def change_editor(self, *, load=True):
        note = self.note
        self.editor_host.content = self.editor if note else self.empty
        if note is None:
            self.close_inline_preview()
            return
        if load:
            heads = self.store.heads(note["id"])
            self.editor_parents = [heads[0]["id"]] if heads else []
            self.editor_values = (note["title"], note["body"])
            self.title.value, self.body.value = self.editor_values
            self.title.selection = None
            self.body.selection = None
        deleted = bool(note["deleted_at"])
        self.title.read_only = deleted
        self.body.read_only = deleted
        self.note_date.value = day(note["created_at"])
        self.editor_header.controls = [
            icon_button(ft.Icons.ARROW_BACK, "노트 목록", self.handler(self.back_to_list)),
            ft.Icon(ft.Icons.FOLDER_OUTLINED, color=MUTED, size=18),
            ft.Container(action(self.book_name(note["notebook_id"]), self.handler(self.move_dialog)),
                         expand=True, alignment=ft.Alignment.CENTER_LEFT),
            icon_button(ft.Icons.STAR if note["pinned"] else ft.Icons.STAR_BORDER,
                        "즐겨찾기", self.handler(self.patch, {"pinned": not note["pinned"]})),
            icon_button(ft.Icons.MORE_HORIZ, "노트 메뉴", self.handler(self.note_menu)),
        ]
        self.editor_header.controls[0].visible = self.narrow
        self.editor_header.controls[2].disabled = deleted
        self.editor_header.controls[3].disabled = deleted
        self.toolbar.controls = [
            action("미리보기" if not self.previewing else "편집", self.handler(self.toggle_preview),
                   ft.Icons.VISIBILITY_OUTLINED if not self.previewing else ft.Icons.EDIT_OUTLINED),
            icon_button(ft.Icons.ATTACH_FILE, "파일 첨부", self.handler(self.attach_files)),
            icon_button(ft.Icons.FUNCTIONS, "LaTeX 수식 삽입", self.handler(self.formula_dialog)),
            icon_button(ft.Icons.FORMAT_BOLD, "굵게", self.handler(self.format_body, "**", "**")),
            icon_button(ft.Icons.FORMAT_ITALIC, "기울임", self.handler(self.format_body, "*", "*")),
            icon_button(ft.Icons.TITLE, "제목", self.handler(self.format_body, "## ")),
            icon_button(ft.Icons.FORMAT_LIST_BULLETED, "목록", self.handler(self.format_body, "- ")),
            icon_button(ft.Icons.CHECK_BOX_OUTLINED, "체크리스트", self.handler(self.format_body, "- [ ] ")),
            icon_button(ft.Icons.FORMAT_QUOTE, "인용", self.handler(self.format_body, "> ")),
            icon_button(ft.Icons.CODE, "인라인 코드", self.handler(self.format_body, "`", "`")),
        ]
        self.toolbar.controls[1].disabled = deleted
        for control in self.toolbar.controls[2:]:
            control.disabled = deleted or self.previewing
        self.tags.controls = [
            ft.TextButton("# " + tag, on_click=self.handler(self.tag_dialog, tag), disabled=deleted)
            for tag in note["tags"]
        ]
        if not deleted:
            self.tags.controls.append(action("태그 추가", self.handler(self.tag_dialog), ft.Icons.ADD))
        self.body.visible = not self.previewing
        self.preview.visible = self.previewing
        self.render_attachments()
        if self.previewing:
            self.render_preview()
        conflicts = self.store.conflicts(note["id"])
        self.editor_state.value = "수정 충돌 · 노트 메뉴에서 버전 보기" if conflicts else (
            "휴지통" if deleted else "이 기기에 저장됨")
        self.characters.value = f"{len(self.body.value)}자"

    async def select_note(self, note_id):
        if not await self.save():
            return
        self.selected_id = note_id
        self.previewing = False
        self.stage = "editor"
        self.change_editor()
        self.build_list()
        self.update_layout()

    async def set_view(self, view, close_dialog=False):
        if not await self.save():
            return
        if close_dialog:
            self.page.pop_dialog()
        self.view = view
        self.search.value = ""
        self.stage = "list"
        matches = self.matches()
        self.selected_id = matches[0]["id"] if matches else None
        self.previewing = False
        self.change_editor()
        self.build_list()
        self.update_layout()

    async def back_to_list(self):
        if await self.save():
            self.stage = "list"
            self.update_layout()

    async def new_note(self, initial=None):
        if not await self.save():
            return
        patch = dict(initial or {})
        if self.view.startswith("book:"):
            patch["notebook_id"] = self.view[5:]
        if self.view.startswith("tag:"):
            patch["tags"] = [self.view[4:]]
        if self.view == "pinned":
            patch["pinned"] = True
        if self.view in ("trash", "tasks"):
            self.view = "all"
        op = self.write_revision("note", patch)
        self.search.value = ""
        self.refresh_data()
        await self.select_note(op["entity"]["id"])
        await self.title.focus()

    def templates_dialog(self, close_dialog=False):
        if close_dialog:
            self.page.pop_dialog()

        async def choose(key):
            self.page.pop_dialog()
            await self.new_note(template_note(key))

        choices = [
            action(title, self.handler(choose, key), ft.Icons.ARTICLE_OUTLINED)
            for key, (title, _) in TEMPLATES.items()
        ]
        self.page.show_dialog(ft.AlertDialog(
            title=heading("노트 템플릿"),
            content=ft.Column(choices, tight=True),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    async def changed(self, _=None):
        if not self.note or self.note["deleted_at"]:
            return
        values = (self.title.value, self.body.value)
        if values == self.editor_values and not self.draft:
            return
        if self.save_task:
            self.save_task.cancel()
        self.draft = (self.selected_id, self.editor_parents[:], values)
        self.editor_state.value = "저장 중…"
        self.characters.value = f"{len(values[1])}자"
        self.page.update(self.editor_state, self.characters)
        self.save_task = asyncio.create_task(self.delayed_save())

    async def delayed_save(self):
        await asyncio.sleep(.55)
        self.save_task = None
        await self.save()

    async def save(self, _=None):
        if self.save_task:
            self.save_task.cancel()
            self.save_task = None
        if not self.draft:
            return True
        note_id, parents, values = self.draft
        try:
            op = self.write_revision("note", {"id": note_id, "title": values[0], "body": values[1]}, parents)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self.editor_state.value = "저장 실패 · 다시 편집하거나 저장하세요"
            self.page.update()
            self.message(str(exc))
            return False
        self.draft = None
        self.editor_values = values
        self.editor_parents = [op["id"]]
        self.refresh_data()
        self.build_list()
        self.render_attachments()
        self.editor_state.value = "수정 충돌 · 노트 메뉴에서 버전 보기" if self.store.conflicts(note_id) else "이 기기에 저장됨"
        self.page.update()
        return True

    async def patch(self, patch):
        if not self.note or not await self.save():
            return
        op = self.write_revision("note", {"id": self.selected_id, **patch}, self.editor_parents)
        self.editor_parents = [op["id"]]
        self.refresh_data()
        self.change_editor()
        self.build_list()
        self.update_layout()

    def text_dialog(self, title, label, confirm, callback, initial=""):
        field = ft.TextField(label=label, value=initial, autofocus=True, max_length=120)
        async def submit(_):
            value = field.value.strip()
            if value:
                try:
                    result = callback(value)
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    self.message(str(exc))
        field.on_submit = submit
        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=heading(title), content=field,
            actions=[action("취소", self.handler(self.page.pop_dialog)), action(confirm, submit, primary=True)],
        ))

    def add_notebook(self, close_dialog=False):
        if close_dialog:
            self.page.pop_dialog()
        async def create(name):
            op = self.write_revision("notebook", {"name": name})
            self.page.pop_dialog()
            self.refresh_data()
            await self.set_view("book:" + op["entity"]["id"])
        self.text_dialog("새 노트북", "노트북 이름", "만들기", create)

    def rename_notebook(self):
        book_id = self.view.removeprefix("book:")
        book = self.books.get(book_id)
        if book is None:
            return
        parents = [revision["id"] for revision in self.store.heads(book_id)[:1]]

        async def rename(name):
            if name != book["name"]:
                self.write_revision("notebook", {"id": book_id, "name": name}, parents)
                self.refresh_data()
                self.change_editor(load=False)
                self.build_list()
            self.page.pop_dialog()
            self.page.update()

        self.text_dialog("노트북 이름 변경", "새 이름", "저장", rename, book["name"])

    def move_dialog(self):
        note = self.note
        if not note:
            return
        choices = [ft.DropdownOption("", "미분류"),
                   *(ft.DropdownOption(key, book["name"]) for key, book in self.books.items())]
        picker = ft.Dropdown(value=note["notebook_id"] if note["notebook_id"] in self.books else "", options=choices)
        async def move():
            await self.patch({"notebook_id": picker.value or None})
            self.page.pop_dialog()
        self.page.show_dialog(ft.AlertDialog(
            title=heading("노트북으로 이동"), content=picker,
            actions=[action("취소", self.handler(self.page.pop_dialog)), action("이동", self.handler(move), primary=True)],
        ))

    def tag_dialog(self, tag=None):
        note = self.note
        if not note:
            return
        if tag:
            async def remove():
                self.page.pop_dialog()
                await self.patch({"tags": [value for value in note["tags"] if value != tag]})
            self.page.show_dialog(ft.AlertDialog(
                title=heading("# " + tag),
                actions=[action("닫기", self.handler(self.page.pop_dialog)), action("이 노트에서 삭제", self.handler(remove))],
            ))
            return
        async def add(value):
            name = value.lstrip("# ").strip()
            self.page.pop_dialog()
            if name and name not in note["tags"]:
                await self.patch({"tags": [*note["tags"], name]})
        self.text_dialog("태그 추가", "태그 이름", "추가", add)

    async def toggle_preview(self):
        if not await self.save():
            return
        self.previewing = not self.previewing
        self.change_editor(load=False)
        self.page.update()

    def render_attachments(self):
        links = dict.fromkeys(attachment_links(self.body.value))
        active = self.inline_attachment
        if active and (active.note_id != self.selected_id or (active.name, active.digest, active.reference) not in links):
            self.close_inline_preview()
        self.attachments.controls = [
            ft.TextButton(
                ft.Text(name, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS),
                icon=ft.Icons.INSERT_DRIVE_FILE_OUTLINED, tooltip=name, max_width=270,
                on_click=self.handler(self.open_inline_preview, name, digest, reference),
            )
            for name, digest, reference in links
        ]
        self.attachments.visible = bool(links)

    def close_inline_preview(self):
        self.inline_revision += 1
        self.inline_attachment = None
        self.inline_preview.visible = False
        self.inline_preview.content = None

    async def open_inline_preview(self, name, digest, reference):
        note_id = self.selected_id
        was_open = self.inline_attachment and self.inline_attachment.reference == reference
        self.close_inline_preview()
        self.page.update()
        if was_open:
            return
        request = self.inline_revision
        content = await asyncio.to_thread(self.store.attachment, digest)
        if self.selected_id != note_id or self.inline_revision != request:
            return
        kind = preview_kind(name, content) if content is not None else None
        if kind is None:
            await self.attachment_dialog(name, digest, reference)
            return
        attachment = InlineAttachment(note_id, name, digest, reference, kind, content)
        self.inline_attachment = attachment
        await self.load_inline_preview(attachment)

    def inline_header(self, attachment):
        def close():
            self.close_inline_preview()
            self.page.update()

        return ft.Row([
            ft.Icon(ft.Icons.INSERT_DRIVE_FILE_OUTLINED, size=18, color=MUTED),
            ft.Text(attachment.name, expand=True, max_lines=1, overflow=ft.TextOverflow.ELLIPSIS, weight=ft.FontWeight.BOLD),
            icon_button(ft.Icons.MORE_HORIZ, "첨부 파일 메뉴", self.handler(
                self.attachment_dialog, attachment.name, attachment.digest, attachment.reference)),
            icon_button(ft.Icons.CLOSE, "첨부 미리보기 닫기", self.handler(close)),
        ], spacing=4)

    def set_inline_preview(self, attachment, controls):
        self.inline_preview.content = ft.Column(
            [self.inline_header(attachment), *controls],
            spacing=8, horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self.inline_preview.visible = True
        self.page.update()

    async def load_inline_preview(self, attachment, *, page=0, sheet=0, row=0, column=0):
        if self.inline_attachment is not attachment:
            return
        self.inline_revision += 1
        request = self.inline_revision
        self.set_inline_preview(attachment, [ft.ProgressBar()])
        try:
            if attachment.kind == "pdf":
                rendered = await self.render_pdf_attachment(attachment.content, page)
                content = self.pdf_page_controls(attachment, rendered, page)
            elif attachment.kind == "sheet":
                rendered = await asyncio.to_thread(sheet_page, attachment.name, attachment.content, sheet, row, column)
                content = self.sheet_controls(attachment, rendered, sheet, row, column)
            elif attachment.kind == "image":
                content = [ft.Image(
                    src=attachment.content, height=420 if self.narrow else 580,
                    fit=ft.BoxFit.CONTAIN, cache_width=1200,
                    error_content=ft.Text("이미지 내용을 표시할 수 없어요.", color=MUTED),
                )]
            else:
                sample = attachment.content[:64 * 1024]
                encoding = "utf-16" if sample.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
                value = sample.decode(encoding, errors="replace")
                content = [ft.Text(value[:8_000] or "비어 있는 파일", selectable=True)]
                if len(sample) < len(attachment.content) or len(value) > 8_000:
                    content.append(ft.Text("파일 앞부분을 표시합니다.", size=12, color=MUTED))
        except ValueError as exc:
            content = [ft.Text(str(exc), color=MUTED, selectable=True)]
        except Exception:
            content = [ft.Text("이 파일을 미리 볼 수 없어요. 첨부 파일 메뉴에서 원본을 열거나 저장해 주세요.", color=MUTED)]
        if self.inline_revision == request and self.inline_attachment is attachment:
            self.set_inline_preview(attachment, content)

    async def render_pdf_attachment(self, content, index):
        if self.page.platform in MOBILE:
            try:
                from yeobaek_mobile_auth import MobilePdfPreview

                if self.mobile_pdf is None:
                    self.mobile_pdf = MobilePdfPreview()
                png, count = await self.mobile_pdf.render_page(content, index, width=850)
                return PDFPage(count, png=png)
            except Exception:
                pass
        try:
            raster = await asyncio.to_thread(raster_pdf_page, content, index)
        except Exception:
            raster = None
        return raster or await asyncio.to_thread(text_pdf_page, content, index)

    def pdf_page_controls(self, attachment, result, page):
        if not result.page_count:
            return [ft.Text("페이지가 없는 PDF입니다.", color=MUTED)]
        page = min(page, result.page_count - 1)
        previous = icon_button(ft.Icons.CHEVRON_LEFT, "이전 페이지", self.handler(
            lambda: self.load_inline_preview(attachment, page=page - 1)))
        next_page = icon_button(ft.Icons.CHEVRON_RIGHT, "다음 페이지", self.handler(
            lambda: self.load_inline_preview(attachment, page=page + 1)))
        previous.disabled = page == 0
        next_page.disabled = page + 1 >= result.page_count
        navigation = ft.Row([previous, ft.Text(f"{page + 1} / {result.page_count}쪽"), next_page], spacing=4)
        if result.png:
            body = ft.Image(src=result.png, height=450 if self.narrow else 650,
                            fit=ft.BoxFit.CONTAIN, semantics_label=f"PDF {page + 1}쪽")
        else:
            body = ft.Text(result.text or "이 페이지에 표시할 수 있는 텍스트가 없어요.", selectable=True)
            return [navigation, ft.Text("PDF 텍스트 미리보기", color=MUTED, size=12), body]
        return [navigation, body]

    def sheet_controls(self, attachment, result, sheet, row, column):
        def go(row_offset, column_offset):
            return self.load_inline_preview(attachment, sheet=sheet, row=row_offset, column=column_offset)

        controls = []
        if result.names:
            async def choose(event):
                await self.load_inline_preview(attachment, sheet=int(event.control.value))

            controls.append(ft.Dropdown(
                label="시트", value=str(sheet),
                options=[ft.DropdownOption(str(index), name) for index, name in enumerate(result.names)],
                on_select=choose,
            ))
        previous_rows = icon_button(ft.Icons.KEYBOARD_ARROW_UP, "이전 행", self.handler(go, row - SHEET_ROWS, column))
        next_rows = icon_button(ft.Icons.KEYBOARD_ARROW_DOWN, "다음 행", self.handler(go, row + SHEET_ROWS, column))
        previous_cols = icon_button(ft.Icons.CHEVRON_LEFT, "이전 열", self.handler(go, row, column - SHEET_COLUMNS))
        next_cols = icon_button(ft.Icons.CHEVRON_RIGHT, "다음 열", self.handler(go, row, column + SHEET_COLUMNS))
        previous_rows.disabled = row == 0
        next_rows.disabled = not result.more_rows
        previous_cols.disabled = column == 0
        next_cols.disabled = not result.more_columns
        controls.append(ft.Row([
            previous_rows, ft.Text(f"행 {row + 1}–{row + max(1, len(result.rows))}", size=12), next_rows,
            previous_cols, ft.Text(f"열 {spreadsheet_column(column + 1)}–{spreadsheet_column(column + result.width)}", size=12), next_cols,
        ], wrap=True, spacing=3, run_spacing=2))
        if not result.rows:
            return [*controls, ft.Text("이 범위에 셀 값이 없어요.", color=MUTED)]
        table = ft.DataTable(
            column_spacing=14, horizontal_margin=8,
            columns=[ft.DataColumn(ft.Text("#")), *[
                ft.DataColumn(ft.Text(spreadsheet_column(column + index + 1))) for index in range(result.width)
            ]],
            rows=[
                ft.DataRow(cells=[ft.DataCell(ft.Text(str(row + offset + 1), color=MUTED)), *[
                    ft.DataCell(ft.Text(value, width=105, max_lines=2, overflow=ft.TextOverflow.ELLIPSIS, selectable=True))
                    for value in (values + ("",) * result.width)[:result.width]
                ]])
                for offset, values in enumerate(result.rows)
            ],
        )
        return [*controls, ft.Row([table], scroll=ft.ScrollMode.AUTO)]

    async def attach_files(self):
        note = self.note
        if note is None or note["deleted_at"] or not await self.save():
            return
        files = await self.file_picker.pick_files(
            file_type=ft.FilePickerFileType.ANY,
            allow_multiple=True,
            with_data=self.page.platform in MOBILE,
        )
        if not files:
            return
        links = []
        errors = []
        for file in files:
            try:
                raw = await self.file_content(file, limit=ATTACHMENT_MAX_BYTES)
                digest = await asyncio.to_thread(self.store.save_attachment, raw)
                links.append(attachment_link(file.name, digest))
            except (OSError, ValueError, sqlite3.Error) as exc:
                errors.append(f"{file.name} · {exc}")
        if self.selected_id != note["id"] or not self.note or self.note["deleted_at"]:
            self.message("노트가 바뀌었어요. 첨부할 노트를 다시 열어 주세요.")
            return
        existing = {reference for _, _, reference in attachment_links(self.body.value)}
        added = list(dict.fromkeys(link for link in links if link not in existing))
        if added:
            self.body.value = "\n\n".join(filter(None, (self.body.value.rstrip(), "\n".join(added))))
            await self.changed()
            if not await self.save():
                return
            if self.previewing:
                self.render_preview()
            self.page.update()
        if errors:
            self.message(f"{len(added)}개 첨부 · {len(errors)}개 실패 · {errors[0]}")
        elif added:
            self.message(f"파일 {len(added)}개를 첨부했어요.")
        else:
            self.message("이미 첨부한 파일입니다.")
        for name, digest, reference in attachment_links("\n".join(added)):
            if preview_kind(name, b""):
                await self.open_inline_preview(name, digest, reference)
                break

    async def attachment_dialog(self, name, digest, reference):
        note_id = self.selected_id
        content = await asyncio.to_thread(self.store.attachment, digest)
        if self.selected_id != note_id or self.note is None:
            return
        filename = attachment_filename(name)
        buttons = []

        async def save_file():
            self.page.pop_dialog()
            await self.export_file(filename, content)

        async def share_file():
            self.page.pop_dialog()
            mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            await self.share.share_files([ft.ShareFile.from_bytes(content, name=filename, mime_type=mime_type)])

        async def remove():
            self.page.pop_dialog()
            if self.selected_id != note_id or self.note is None or self.note["deleted_at"]:
                return
            self.body.value = self.body.value.replace(reference, "")
            await self.changed()
            await self.save()
            if self.previewing:
                self.render_preview()
            self.page.update()

        if content is None:
            details = "이 기기에 파일이 아직 없습니다. Google Drive 동기화 후 다시 열어 주세요."
            if self.drive.connected:
                buttons.append(action("지금 동기화", self.handler(self.start_drive, False), ft.Icons.SYNC))
        else:
            details = f"{len(content):,}바이트"
            if self.share:
                buttons.append(action("다른 앱에서 열기", self.handler(share_file), ft.Icons.OPEN_IN_NEW))
            if content or self.page.platform not in MOBILE:
                buttons.append(action("파일 저장", self.handler(save_file), ft.Icons.DOWNLOAD_OUTLINED))
        if not self.note["deleted_at"]:
            buttons.append(action("노트에서 첨부 제거", self.handler(remove), ft.Icons.LINK_OFF))
        self.page.show_dialog(ft.AlertDialog(
            title=heading(name), content=ft.Column([ft.Text(details), *buttons], tight=True),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    def formula_dialog(self):
        note_id = self.selected_id
        original = self.body.value
        start, end = self.body_selection()
        selected = original[start:end]
        formula = ft.TextField(
            label="LaTeX 수식", hint_text=r"\frac{1}{2}mv^2", value=selected,
            multiline=True, min_lines=2, max_lines=7, autofocus=True,
            smart_dashes_type=False, smart_quotes_type=False,
        )

        async def insert(display):
            expression = formula.value.strip()
            if not expression or self.selected_id != note_id or not self.note or self.note["deleted_at"]:
                return
            for left, right in (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$")):
                if expression.startswith(left) and expression.endswith(right) and len(expression) > len(left) + len(right):
                    expression = expression[len(left):-len(right)].strip()
                    break
            if not expression:
                return
            if display:
                text = "\n\n$$\n" + expression + "\n$$\n\n"
            else:
                text = "$" + " ".join(expression.splitlines()) + "$ "
            body = self.body.value
            a, b = (start, end) if body == original else (len(body), len(body))
            self.body.value = body[:a] + text + body[b:]
            cursor = len(self.body.value[:a + len(text)].encode("utf-16-le")) // 2
            self.body.selection = ft.TextSelection(base_offset=cursor, extent_offset=cursor)
            self.page.pop_dialog()
            await self.changed()
            await self.save()
            await self.body.focus()

        self.page.show_dialog(ft.AlertDialog(
            title=heading("LaTeX 수식"),
            content=ft.Column([formula, ft.Text("미리보기에서 수식을 확인할 수 있어요.", color=MUTED)], tight=True),
            actions=[
                action("취소", self.handler(self.page.pop_dialog)),
                action("문장 안에 넣기", self.handler(insert, False)),
                action("별도 줄에 넣기", self.handler(insert, True), primary=True),
            ],
        ))

    def render_preview(self):
        controls = []
        lines = []
        tasks = dict(task_lines(self.body.value))

        def flush_markdown():
            if lines:
                # Keep images inert when displaying imported or synchronized notes.
                text = without_attachment_links("\n".join(lines)).replace("![", "&#33;[")
                if text.strip():
                    controls.append(ft.Markdown(
                        text, extension_set=ft.MarkdownExtensionSet.GITHUB_FLAVORED,
                        selectable=True, on_tap_link=self.open_markdown_link, latex_scale_factor=1.1,
                    ))
                lines.clear()

        for index, line in enumerate(self.body.value.splitlines()):
            task = tasks.get(index)
            if task:
                flush_markdown()
                async def toggle(event, line_index=index):
                    await self.toggle_task(line_index, event.control.value)
                controls.append(ft.Checkbox(label=task[4], value=task[2] != " ", on_change=toggle,
                                            disabled=bool(self.note["deleted_at"])))
            else:
                lines.append(line)
        flush_markdown()
        self.preview.controls = controls or ([] if self.attachments.visible else [ft.Text("본문 없음", color=MUTED)])

    async def toggle_task(self, index, checked):
        task = dict(task_lines(self.body.value)).get(index)
        if task is None:
            return
        lines = self.body.value.splitlines(keepends=True)
        begin, end = task.span(2)
        lines[index] = lines[index][:begin] + ("x" if checked else " ") + lines[index][end:]
        self.body.value = "".join(lines)
        await self.changed()
        await self.save()
        self.render_preview()
        self.page.update()

    async def open_markdown_link(self, event):
        url = event.data
        if isinstance(url, str) and urlsplit(url).scheme in {"https", "http"}:
            await self.url_launcher.launch_url(url)

    def body_selection(self):
        selection = self.body.selection
        value = self.body.value
        if selection and selection.base_offset >= 0:
            # Flutter's offsets use UTF-16 code units.
            encoded = value.encode("utf-16-le")
            a, b = sorted((selection.base_offset, selection.extent_offset))
            start = len(encoded[:a * 2].decode("utf-16-le", errors="ignore"))
            end = len(encoded[:b * 2].decode("utf-16-le", errors="ignore"))
        else:
            start = end = len(value)
        return start, end

    async def format_body(self, prefix, suffix=""):
        value = self.body.value
        start, end = self.body_selection()
        if not suffix:
            start = value.rfind("\n", 0, start) + 1
            end = start
        self.body.value = value[:start] + prefix + value[start:end] + suffix + value[end:]
        cursor = len(self.body.value[:end + len(prefix)].encode("utf-16-le")) // 2
        self.body.selection = ft.TextSelection(base_offset=cursor, extent_offset=cursor)
        await self.changed()
        self.page.update()
        await self.body.focus()

    def note_menu(self):
        note = self.note
        if not note:
            return
        async def trash_or_restore():
            self.page.pop_dialog()
            await self.patch({"deleted_at": None if note["deleted_at"] else time.time_ns() // 1_000_000})
            await self.set_view(self.view)
        async def export_note():
            if not await self.save():
                return
            current = self.note
            name = re.sub(r'[\\/:*?"<>|]', "_", title_of(current))
            contents = ("# " + title_of(current) + "\n\n" + current["body"]).encode()
            self.page.pop_dialog()
            await self.export_file(name + ".md", contents)

        async def duplicate():
            if not await self.save():
                return
            current = self.note
            copy = {key: current[key] for key in ("body", "notebook_id", "tags")}
            copy["title"] = title_of(current) + " (복사)"
            op = self.write_revision("note", copy)
            self.page.pop_dialog()
            self.refresh_data()
            await self.set_view("all")
            await self.select_note(op["entity"]["id"])

        choices = [
            action("수정 기록", self.handler(self.history_dialog, note["id"]), ft.Icons.HISTORY),
            action("노트 복제", self.handler(duplicate), ft.Icons.CONTENT_COPY),
            action("Markdown으로 내보내기", self.handler(export_note), ft.Icons.DOWNLOAD_OUTLINED),
            action("휴지통에서 복원" if note["deleted_at"] else "휴지통으로 이동",
                   self.handler(trash_or_restore), ft.Icons.RESTORE if note["deleted_at"] else ft.Icons.DELETE_OUTLINE),
        ]
        if self.store.conflicts(note["id"]):
            choices.insert(0, action("충돌한 버전 보기", self.handler(self.conflict_dialog, note["id"]), ft.Icons.COMPARE_ARROWS))
        self.page.show_dialog(ft.AlertDialog(
            title=heading("노트 메뉴"), content=ft.Column(choices, tight=True),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    async def history_dialog(self, note_id):
        if not await self.save():
            return
        self.page.pop_dialog()
        revisions = self.store.history(note_id)
        heads = self.store.heads(note_id)
        current_id = heads[0]["id"] if heads else None
        rows = []

        async def restore(version):
            patch = {key: version[key] for key in ("title", "body", "tags", "pinned", "notebook_id")}
            if self.selected_id != note_id:
                return
            self.page.pop_dialog()
            await self.patch(patch)
            self.message("이전 내용을 새 수정 기록으로 복원했어요.")

        for revision in revisions:
            version = revision["entity"]
            current = revision["id"] == current_id
            stamp = datetime.fromtimestamp(version["updated_at"] / 1000).strftime("%Y.%m.%d %H:%M:%S")
            rows.extend([
                heading(title_of(version), 15),
                ft.Text(stamp + (" · 현재" if current else ""), size=11, color=MUTED),
                ft.Text(version["body"][:600] or "본문 없음", selectable=True, max_lines=5,
                        overflow=ft.TextOverflow.ELLIPSIS),
            ])
            if not current:
                rows.append(action("이 버전 복원", self.handler(restore, version), ft.Icons.RESTORE))
            rows.append(ft.Divider(color=LINE))
        self.page.show_dialog(ft.AlertDialog(
            title=heading("최근 수정 기록"), scrollable=True,
            content=ft.Container(ft.Column(rows, tight=True), width=430),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    async def conflict_dialog(self, note_id):
        if not await self.save():
            return
        self.page.pop_dialog()
        heads = self.store.conflicts(note_id)
        if not heads:
            return
        observed = [op["id"] for op in heads]
        async def choose(note, duplicate=False):
            if duplicate:
                copy = {key: value for key, value in note.items()
                        if key in {"title", "body", "notebook_id", "tags", "pinned"}}
                op = self.write_revision("note", copy)
            else:
                op = self.write_revision("note", note, observed)
            self.page.pop_dialog()
            self.refresh_data()
            await self.set_view("trash" if op["entity"]["deleted_at"] else "all")
            await self.select_note(op["entity"]["id"])
        rows = []
        for version in heads:
            note = version["entity"]
            rows.extend([
                heading(title_of(note), 15),
                ft.Text("휴지통으로 이동된 버전" if note["deleted_at"] else note["body"][:1800] or "본문 없음",
                        selectable=True, color=MUTED),
                ft.Row([action("이 버전 사용", self.handler(choose, note), primary=True),
                        action("복사", self.handler(choose, note, True))], wrap=True),
                ft.Divider(color=LINE),
            ])
        self.page.show_dialog(ft.AlertDialog(
            title=heading("충돌한 버전"), scrollable=True, content=ft.Container(ft.Column(rows, tight=True), width=430),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    async def export_file(self, name, content):
        if self.page.platform in MOBILE:
            await self.file_picker.save_file(file_name=name, src_bytes=content)
        else:
            path = await self.file_picker.save_file(file_name=name)
            if path:
                await asyncio.to_thread(Path(path).write_bytes, content)

    async def file_content(self, file, *, limit=None):
        def too_large():
            return ValueError(f"파일 하나는 {limit // (1024 * 1024)} MiB까지 첨부할 수 있어요.")

        if limit is not None and file.size is not None and file.size > limit:
            raise too_large()
        if file.bytes is not None:
            content = file.bytes
        elif file.path:
            def read_local():
                with Path(file.path).open("rb") as stream:
                    return stream.read() if limit is None else stream.read(limit + 1)
            content = await asyncio.to_thread(read_local)
        else:
            raise ValueError(f"{file.name}의 내용을 읽을 수 없어요.")
        if limit is not None and len(content) > limit:
            raise too_large()
        return content

    async def import_text_notes(self):
        if not await self.save():
            return
        self.page.pop_dialog()
        files = await self.file_picker.pick_files(
            file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["md", "markdown", "txt"],
            allow_multiple=True, with_data=self.page.platform in MOBILE,
        )
        if not files:
            return
        patches = []
        for file in files:
            patch = read_note(file.name, await self.file_content(file))
            if self.view.startswith("book:"):
                patch["notebook_id"] = self.view[5:]
            if self.view.startswith("tag:"):
                patch["tags"] = [self.view[4:]]
            patches.append(patch)
        revisions = await asyncio.to_thread(self.store.create_notes, patches)
        self.sync_requested.set()
        self.refresh_data()
        if self.view in ("pinned", "trash", "tasks"):
            await self.set_view("all")
        self.search.value = ""
        await self.select_note(revisions[0]["entity"]["id"])
        self.message(f"노트 {len(revisions)}개를 가져왔어요.")

    def settings_dialog(self, close_dialog=False):
        if close_dialog:
            self.page.pop_dialog()
        async def export_backup():
            if not await self.save():
                return
            operations = self.store.operations()
            if not operations:
                self.message("백업할 노트가 없어요.")
                return
            self.page.pop_dialog()
            def export_bytes():
                files = {}
                missing = 0
                for digest in self.store.referenced_attachments():
                    content = self.store.attachment(digest)
                    if content is None:
                        missing += 1
                    else:
                        files[digest] = base64.b64encode(content).decode("ascii")
                backup = {"format": "yeobaek-backup-v1", "operations": operations, "attachments": files}
                return json.dumps(backup, ensure_ascii=False, separators=(",", ":")).encode(), missing
            payload, missing = await asyncio.to_thread(export_bytes)
            await self.export_file("yeobaek-backup.json", payload)
            if missing:
                self.message(f"이 기기에 없는 첨부 파일 {missing}개는 동기화 후 백업해 주세요.")
        async def import_backup():
            if not await self.save():
                return
            self.page.pop_dialog()
            files = await self.file_picker.pick_files(
                file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["json"],
                with_data=True,
            )
            if not files:
                return
            raw = await self.file_content(files[0])
            backup = json.loads(raw)
            if not isinstance(backup, dict) or backup.get("format") != "yeobaek-backup-v1":
                raise ValueError("여백 백업 파일을 선택해 주세요.")
            files = backup.get("attachments", {})
            if not isinstance(files, dict):
                raise ValueError("백업의 첨부 목록이 잘못되었습니다.")
            try:
                attachments = {digest: base64.b64decode(data, validate=True) for digest, data in files.items()}
            except (ValueError, TypeError, binascii.Error) as exc:
                raise ValueError("백업에서 첨부 파일을 읽을 수 없어요.") from exc
            await asyncio.to_thread(self.store.ingest, backup["operations"], attachments=attachments)
            self.sync_requested.set()
            self.refresh_data()
            await self.set_view("all")
            self.message("백업을 가져왔어요.")
        choices = [
            action("Markdown·텍스트 노트 가져오기", self.handler(self.import_text_notes), ft.Icons.NOTE_ADD_OUTLINED),
            action("전체 노트 백업 저장", self.handler(export_backup), ft.Icons.DOWNLOAD_OUTLINED),
            action("백업 파일 가져오기", self.handler(import_backup), ft.Icons.UPLOAD_FILE),
        ]
        self.page.show_dialog(ft.AlertDialog(
            title=heading("설정 및 백업"), content=ft.Column(choices, tight=True),
            actions=[action("닫기", self.handler(self.page.pop_dialog))],
        ))

    def drive_dialog(self, close_dialog=False):
        if close_dialog:
            self.page.pop_dialog()
        account = self.drive.account
        status = ft.Text(self.drive_status, color=MUTED, selectable=True)
        details = [
            status,
            ft.Text("연결하면 이 기기의 노트가 선택한 Google 계정의 여백 데이터에 저장됩니다.",
                    color=MUTED, size=13),
        ]
        if account:
            details.append(ft.Text(account.email or account.name or "Google 계정", color=INK, selectable=True))
        if self.page.platform in MOBILE:
            details.append(ft.Text("다른 기기에서도 같은 Google 계정으로 연결하세요.", color=MUTED, size=13))
        else:
            details.append(ft.Text("Google Cloud에서 다운로드한 데스크톱 OAuth 클라이언트 JSON이 필요합니다.", color=MUTED, size=13))
        if self.drive_busy:
            choices = []
        elif self.drive.connected:
            choices = [
                action("연결 해제", self.handler(self.disconnect_drive)),
                action("지금 동기화", self.handler(self.start_drive, False), primary=True),
            ]
        else:
            choices = [action("Drive 연결", self.handler(self.start_drive, True), primary=True)]
            if self.page.platform not in MOBILE and self.store.setting("drive-client-path"):
                choices.insert(0, action("다른 OAuth 파일 선택", self.handler(self.start_drive, True, True)))
        self.page.show_dialog(ft.AlertDialog(
            title=heading("Google Drive"), content=ft.Container(ft.Column(details, tight=True), width=365),
            actions=[action("닫기", self.handler(self.page.pop_dialog)), *choices],
        ))

    def set_drive_status(self, text):
        self.drive_status = text
        self.drive_text.value = text
        self.page.update()

    async def connect_mobile(self):
        from yeobaek_mobile_auth import GoogleDriveAuth

        if self.mobile_auth is None:
            config_path = Path(__file__).resolve().parent.parent / "assets" / "mobile_oauth.json"
            config = json.loads(config_path.read_text()) if config_path.is_file() else {}
            client_id = config.get("ios_client_id") if self.page.platform == ft.PagePlatform.IOS else None
            server_client_id = config.get("server_client_id") if self.page.platform == ft.PagePlatform.ANDROID else None
            required_id = client_id if self.page.platform == ft.PagePlatform.IOS else server_client_id
            if not isinstance(required_id, str) or not required_id.endswith(".apps.googleusercontent.com"):
                raise AuthenticationError("이 모바일 빌드에 플랫폼용 Google OAuth 클라이언트 설정이 필요합니다.")
            self.mobile_auth = GoogleDriveAuth(client_id=client_id, server_client_id=server_client_id)
        token = await self.mobile_auth.token(interactive=True)
        if not token:
            raise AuthenticationError("Google Drive 권한을 허용해 주세요.")
        return await asyncio.to_thread(self.drive.connect_token, token)

    async def start_drive(self, connect, choose_file=False):
        self.page.pop_dialog()
        if self.drive_busy or not await self.save():
            return
        self.drive_busy = True
        try:
            if connect:
                self.set_drive_status("Google 계정 연결 중…")
                if self.page.platform in MOBILE:
                    await self.connect_mobile()
                else:
                    path = None if choose_file else os.getenv("YEOBAEK_GOOGLE_CLIENT_JSON")
                    if not path and not choose_file:
                        saved_path = self.store.setting("drive-client-path")
                        if saved_path and Path(saved_path).is_file():
                            path = saved_path
                    if not path:
                        files = await self.file_picker.pick_files(
                            file_type=ft.FilePickerFileType.CUSTOM, allowed_extensions=["json"],
                        )
                        if not files:
                            self.set_drive_status("동기화 연결")
                            return
                        path = files[0].path
                    await asyncio.to_thread(self.drive.connect, path)
                    self.store.set_setting("drive-client-path", str(Path(path).expanduser().resolve()))
            await self.sync_now()
        except ImportError:
            self.set_drive_status("Google 로그인 모듈 필요")
            self.message("모바일용 Google 로그인 모듈과 OAuth 설정이 포함된 빌드가 필요해요.")
        except Exception as exc:
            self.set_drive_status("연결 확인 필요")
            self.message(str(exc) or "Google Drive 연결을 확인해 주세요.")
        finally:
            self.drive_busy = False

    async def disconnect_drive(self):
        self.page.pop_dialog()
        if self.drive_busy:
            return
        await asyncio.to_thread(self.drive.disconnect)
        if self.mobile_auth:
            await self.mobile_auth.sign_out()
        self.set_drive_status("동기화 연결")

    async def sync_now(self):
        self.set_drive_status("동기화 중…")
        try:
            result = await asyncio.to_thread(self.drive.sync)
        except AuthenticationError:
            if self.mobile_auth is None:
                raise
            await self.mobile_auth.clear_token()
            token = await self.mobile_auth.token(interactive=False)
            if not token:
                raise
            await asyncio.to_thread(self.drive.connect_token, token)
            result = await asyncio.to_thread(self.drive.sync)
        await self.save()
        self.refresh_data()
        if self.note is None:
            matches = self.matches()
            self.selected_id = matches[0]["id"] if matches else None
        if not self.draft:
            old_head = self.editor_parents[:]
            heads = self.store.heads(self.selected_id) if self.selected_id else []
            new_head = [heads[0]["id"]] if heads else []
            self.change_editor(load=old_head != new_head)
        self.build_list()
        if result.unreadable:
            self.set_drive_status(f"일부 동기화 · 읽지 못한 기록 {result.unreadable}개")
        else:
            self.set_drive_status("동기화 완료 · " + datetime.now().strftime("%H:%M"))

    async def sync_loop(self):
        retry_at = 0
        while True:
            try:
                await asyncio.wait_for(self.sync_requested.wait(), timeout=30)
                await asyncio.sleep(3)
            except TimeoutError:
                pass
            self.sync_requested.clear()
            can_sync = self.drive.connected or bool(self.mobile_auth and self.drive.account)
            if not can_sync or self.drive_busy or time.monotonic() < retry_at:
                continue
            self.drive_busy = True
            try:
                await self.sync_now()
            except AuthenticationError:
                self.set_drive_status("다시 연결 필요")
                retry_at = time.monotonic() + 30
            except Exception:
                self.set_drive_status("동기화 대기 · 연결 확인")
                retry_at = time.monotonic() + 30
            finally:
                self.drive_busy = False

    async def keyboard(self, event):
        if event.ctrl or event.meta:
            key = event.key.lower()
            if key == "s":
                await self.save()
            if key == "n":
                await self.new_note()
            if key == "k":
                self.stage = "list"
                self.update_layout()
                await self.search.focus()

    def mount(self):
        self.page.add(ft.SafeArea(self.row, expand=True))
        self.page.on_resize = self.update_layout
        self.page.on_keyboard_event = self.keyboard
        self.page.on_app_lifecycle_state_change = self.save
        self.refresh_data()
        first = self.matches()
        self.selected_id = first[0]["id"] if first else None
        self.change_editor()
        self.build_list()
        self.update_layout()
        self.page.run_task(self.sync_loop)


async def main(page):
    page.title = "여백 · 내 노트"
    page.bgcolor = PAPER
    page.padding = 0
    page.spacing = 0
    page.theme_mode = ft.ThemeMode.LIGHT
    page.theme = ft.Theme(color_scheme_seed=PINE)
    if page.platform not in MOBILE:
        page.window.width = 1120
        page.window.height = 760
        page.window.min_width = 420
        page.window.min_height = 540
    try:
        directory = os.getenv("YEOBAEK_DATA_DIR") or await ft.StoragePaths().get_application_support_directory()
        data_dir = Path(directory).expanduser() / "Yeobaek"
        data_dir.mkdir(parents=True, exist_ok=True)
        Workspace(page, data_dir).mount()
    except Exception as exc:
        page.add(ft.Container(ft.Column([heading("여백을 시작할 수 없어요"), ft.Text(str(exc), selectable=True)]), padding=26))
        raise
