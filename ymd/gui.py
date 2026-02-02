import json
import os
import sys
import queue
import subprocess
import threading
import tkinter as tk
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse
import re
import itertools
from typing import Any, Iterable, Optional, Tuple, Union, cast

from yandex_music import Playlist, Track

from ymd import core

TRACK_RE = re.compile(r"track/(\d+)")
ALBUM_RE = re.compile(r"album/(\d+)$")
ARTIST_RE = re.compile(r"artist/(\d+)$")
PLAYLIST_RE = re.compile(r"([\w\-._@]+)/playlists/(\d+)$")
PLAYLIST_LIKED_RE = re.compile(r"/playlists/((?:lk|ik)\.[\w-]+)$")

FETCH_PAGE_SIZE = 10

CONFIG_DIR_NAME = "yandex-music-downloader"
CONFIG_FILE_NAME = "gui.json"

QUALITY_OPTIONS = [
    ("Лучшее (FLAC)", 2),
    ("Оптимальное (AAC 192kbps)", 1),
    ("Низкое (AAC 64kbps)", 0),
]


def _setup_entry(entry: ttk.Entry) -> None:
    try:
        cast(Any, entry).configure(undo=True, autoseparators=True, maxundo=-1)
    except tk.TclError:
        pass

    def _show_menu(event: tk.Event) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _paste_event(event: Optional[tk.Event]) -> str:
        try:
            text = entry.clipboard_get()
        except tk.TclError:
            try:
                text = entry.selection_get(selection="PRIMARY")
            except tk.TclError:
                return "break"
        try:
            if entry.selection_present():
                entry.delete("sel.first", "sel.last")
        except tk.TclError:
            pass
        entry.insert(tk.INSERT, text)
        return "break"

    def _copy_event(event: Optional[tk.Event]) -> str:
        try:
            text = entry.selection_get()
        except tk.TclError:
            text = ""
        if text:
            entry.clipboard_clear()
            entry.clipboard_append(text)
        return "break"

    def _cut_event(event: Optional[tk.Event]) -> str:
        try:
            text = entry.selection_get()
        except tk.TclError:
            text = ""
        if text:
            entry.clipboard_clear()
            entry.clipboard_append(text)
            try:
                entry.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
        return "break"

    def _select_all_event(event: Optional[tk.Event]) -> str:
        entry.selection_range(0, tk.END)
        entry.icursor(tk.END)
        return "break"

    def _undo_event(event: Optional[tk.Event]) -> str:
        entry.event_generate("<<Undo>>")
        return "break"

    def _redo_event(event: Optional[tk.Event]) -> str:
        entry.event_generate("<<Redo>>")
        return "break"

    def _control_key_handler(event: tk.Event) -> Optional[str]:
        keysym = (event.keysym or "").lower()
        char = event.char or ""
        action_map = {
            "v": _paste_event,
            "c": _copy_event,
            "x": _cut_event,
            "a": _select_all_event,
            "z": _undo_event,
            "y": _redo_event,
            "м": _paste_event,
            "с": _copy_event,
            "ч": _cut_event,
            "ф": _select_all_event,
            "я": _undo_event,
            "н": _redo_event,
            "cyrillic_em": _paste_event,
            "cyrillic_es": _copy_event,
            "cyrillic_che": _cut_event,
            "cyrillic_ef": _select_all_event,
            "cyrillic_ya": _undo_event,
            "cyrillic_en": _redo_event,
        }
        control_chars = {
            "\x16": _paste_event,  # Ctrl+V
            "\x03": _copy_event,   # Ctrl+C
            "\x18": _cut_event,    # Ctrl+X
            "\x01": _select_all_event,  # Ctrl+A
            "\x1a": _undo_event,  # Ctrl+Z
            "\x19": _redo_event,  # Ctrl+Y
        }
        handler = action_map.get(keysym)
        if handler is None and char:
            handler = action_map.get(char.lower()) or control_chars.get(char)
        if handler is not None:
            return handler(event)
        return None

    menu = tk.Menu(entry, tearoff=0)
    menu.add_command(label="Вырезать", command=lambda: _cut_event(None))
    menu.add_command(label="Копировать", command=lambda: _copy_event(None))
    menu.add_command(label="Вставить", command=lambda: _paste_event(None))
    menu.add_command(label="Выделить всё", command=lambda: _select_all_event(None))
    menu.add_command(label="Отменить", command=lambda: _undo_event(None))
    menu.add_command(label="Повторить", command=lambda: _redo_event(None))

    entry.bind("<Button-3>", _show_menu)
    entry.bind("<Control-v>", _paste_event)
    entry.bind("<Control-V>", _paste_event)
    entry.bind("<Shift-Insert>", _paste_event)
    entry.bind("<Control-c>", _copy_event)
    entry.bind("<Control-C>", _copy_event)
    entry.bind("<Control-x>", _cut_event)
    entry.bind("<Control-X>", _cut_event)
    entry.bind("<Control-a>", _select_all_event)
    entry.bind("<Control-A>", _select_all_event)
    entry.bind("<Control-z>", _undo_event)
    entry.bind("<Control-Z>", _undo_event)
    entry.bind("<Control-y>", _redo_event)
    entry.bind("<Control-Y>", _redo_event)
    entry.bind("<Control-Insert>", _copy_event)
    entry.bind("<Shift-Insert>", _paste_event)
    entry.bind("<Shift-Delete>", _cut_event)
    entry.bind("<Control-KeyPress>", _control_key_handler)
    entry.bind("<Control-Shift-KeyPress>", _control_key_handler)


def _config_path() -> Path:
    if os.name == "nt":
        base = os.getenv("APPDATA") or os.path.expanduser("~")
        return Path(base) / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    return Path.home() / ".config" / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _load_saved_token() -> str:
    path = _config_path()
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            token = data.get("token")
            if isinstance(token, str):
                return token
    except (OSError, json.JSONDecodeError):
        return ""
    return ""


def _save_token(token: str) -> None:
    path = _config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if token:
            path.write_text(json.dumps({"token": token}), encoding="utf-8")
        elif path.exists():
            path.unlink()
    except OSError:
        pass


def _parse_url(url: str) -> Tuple[str, str]:
    parsed_url = urlparse(url)
    path = parsed_url.path
    if match := ARTIST_RE.search(path):
        return ("artist", match.group(1))
    if match := ALBUM_RE.search(path):
        return ("album", match.group(1))
    if match := TRACK_RE.search(path):
        return ("track", match.group(1))
    if match := PLAYLIST_LIKED_RE.search(path):
        return ("playlist", match.group(1))
    if match := PLAYLIST_RE.search(path):
        return ("playlist", match.group(1) + "/" + match.group(2))
    raise ValueError("Ссылка не распознана: вставьте ссылку на артиста/альбом/трек/плейлист")


def _preview_fallback(target_type: str, target_id: str, token_missing: bool) -> str:
    label_map = {
        "artist": "Артист",
        "album": "Альбом",
        "track": "Трек",
        "playlist": "Плейлист",
    }
    label = label_map.get(target_type, "Ссылка")
    if target_type == "playlist" and target_id.startswith(("lk.", "ik.")):
        label = "Мне нравится"
    suffix = " Для названия нужен токен." if token_missing else ""
    return f"{label} (ID: {target_id}).{suffix}"


def _build_preview_text(client: core.Client, target_type: str, target_id: str) -> str:
    if target_type == "artist":
        artists = client.artists(target_id)
        if artists:
            artist = artists[0]
            name = artist.name or f"ID {target_id}"
            total = None
            if counts := artist.counts:
                total = counts.tracks
            if total:
                return f"Артист: {name}. Будет загружено треков: {total}."
            return f"Артист: {name}. Будут загружены все доступные треки."
        return f"Артист (ID: {target_id})."

    if target_type == "album":
        album = client.albums_with_tracks(target_id)
        if album:
            title = core.full_title(album) or f"ID {target_id}"
            if album.track_count:
                return f"Альбом: {title}. Треков: {album.track_count}."
            return f"Альбом: {title}. Будут загружены все доступные треки."
        return f"Альбом (ID: {target_id})."

    if target_type == "track":
        track_list = client.tracks(target_id)
        track = track_list[0] if track_list else None
        if track:
            title = core.full_title(track) or f"ID {target_id}"
            artist = track.artists[0].name if track.artists else ""
            if artist:
                return f"Трек: {artist} — {title}."
            return f"Трек: {title}."
        return f"Трек (ID: {target_id})."

    if target_type == "playlist":
        if target_id.startswith(("lk.", "ik.")):
            liked_tracks = client.users_likes_tracks()
            count = len(liked_tracks.tracks) if liked_tracks else 0
            return f"Мне нравится. Треков: {count}."
        if "/" in target_id:
            user, kind = target_id.split("/")
            playlist_obj = client.users_playlists(kind, user)
            title_fallback = f"{user}/{kind}"
        else:
            kind = target_id
            playlist_obj = client.users_playlists(kind)
            title_fallback = kind
        if not playlist_obj or isinstance(playlist_obj, list):
            return f"Плейлист: {title_fallback}."
        playlist = cast(Playlist, playlist_obj)
        title = playlist.title or title_fallback
        if playlist.track_count:
            return f"Плейлист: {title}. Треков: {playlist.track_count}."
        return f"Плейлист: {title}. Будут загружены все доступные треки."

    return f"Ссылка (ID: {target_id})."


def _preview_worker(
    token: str,
    url: str,
    version: int,
    queue_out: "queue.Queue[tuple]",
) -> None:
    try:
        if not url:
            queue_out.put(("preview", version, ""))
            return

        try:
            target_type, target_id = _parse_url(url)
        except ValueError as exc:
            queue_out.put(("preview", version, str(exc)))
            return

        if not token:
            queue_out.put(("preview", version, _preview_fallback(target_type, target_id, True)))
            return

        client = core.init_client(token=token, timeout=10, max_try_count=3, retry_delay=2)
        text = _build_preview_text(client, target_type, target_id)
        queue_out.put(("preview", version, text))
    except Exception as exc:
        queue_out.put(("preview", version, f"Не удалось получить данные: {exc}"))


def _album_tracks_gen(
    client: core.Client, album_ids: Iterable[Union[int, str]]
) -> Iterable[Track]:
    for album_id in album_ids:
        full_album = client.albums_with_tracks(album_id)
        if full_album and full_album.volumes:
            yield from itertools.chain.from_iterable(full_album.volumes)


def _resolve_tracks(
    client: core.Client, url: str
) -> Tuple[Iterable[Track], Optional[int]]:
    target_type, target_id = _parse_url(url)

    if target_type == "artist":
        album_ids: list[int] = []
        total = 0
        has_next = True
        page = 0
        while has_next:
            albums_info = client.artists_direct_albums(target_id, page)
            if not albums_info:
                break
            for album in albums_info.albums:
                if album.id and album.available:
                    album_ids.append(album.id)
                    if album.track_count:
                        total += album.track_count
            if pager := albums_info.pager:
                page = pager.page + 1
                has_next = pager.per_page * page < pager.total
            else:
                break

        tracks = _album_tracks_gen(client, album_ids)
        total = total or None
        return (tracks, total)

    if target_type == "album":
        tracks = _album_tracks_gen(client, (target_id,))
        total = None
        album = client.albums_with_tracks(target_id)
        if album:
            total = album.track_count
        return (tracks, total)

    if target_type == "track":
        tracks = client.tracks(target_id)
        return (tracks, 1)

    if target_type == "playlist":
        if target_id.startswith(("lk.", "ik.")):
            liked_tracks = client.users_likes_tracks()
            track_ids = liked_tracks.tracks_ids if liked_tracks else []
            total = len(track_ids)

            def liked_tracks_gen() -> Iterable[Track]:
                for i in range(0, len(track_ids), FETCH_PAGE_SIZE):
                    yield from client.tracks(track_ids[i : i + FETCH_PAGE_SIZE])

            return (liked_tracks_gen(), total)
        if "/" in target_id:
            user, kind = target_id.split("/")
            playlist_obj = client.users_playlists(kind, user)
        else:
            kind = target_id
            playlist_obj = client.users_playlists(kind)
        if not playlist_obj or isinstance(playlist_obj, list):
            raise ValueError("Плейлист не найден")
        playlist = cast(Playlist, playlist_obj)
        total = playlist.track_count

        def playlist_tracks_gen() -> Iterable[Track]:
            tracks = playlist.fetch_tracks()
            for i in range(0, len(tracks), FETCH_PAGE_SIZE):
                yield from client.tracks(
                    [track.id for track in tracks[i : i + FETCH_PAGE_SIZE]]
                )

        return (playlist_tracks_gen(), total)

    raise ValueError("Неизвестный тип ссылки")


def _download_worker(
    token: str,
    url: str,
    quality: int,
    download_dir: str,
    workers: int,
    queue_out: "queue.Queue[tuple]",
) -> None:
    try:
        client = core.init_client(token=token, timeout=20, max_try_count=20, retry_delay=5)
        tracks, total = _resolve_tracks(client, url)
        queue_out.put(("total", total))

        base_dir = Path(download_dir)
        base_dir.mkdir(parents=True, exist_ok=True)

        track_counter = 0
        completed = 0

        def build_downloadable(track: Track) -> core.DownloadableTrack:
            save_path = base_dir / core.prepare_base_path(
                core.DEFAULT_PATH_PATTERN,
                track,
                unsafe_path=False,
            )
            save_path.parent.mkdir(parents=True, exist_ok=True)
            return core.to_downloadable_track(
                track, core.CoreTrackQuality(quality), save_path
            )

        def run_download(downloadable: core.DownloadableTrack) -> None:
            core.download_track(
                track_info=downloadable,
                lyrics_format=core.LyricsFormat.NONE,
                embed_cover=False,
                cover_resolution=core.DEFAULT_COVER_RESOLUTION,
                covers_cache=None,
                compatibility_level=1,
            )

        if workers <= 1:
            for track in tracks:
                track_counter += 1
                if total:
                    progress_status = f"[{track_counter}/{total}] "
                else:
                    progress_status = f"[{track_counter}] "

                if not track.available:
                    queue_out.put(("status", f"{progress_status}Трек {track.title} недоступен"))
                    completed += 1
                    queue_out.put(("progress", completed, total))
                    continue

                downloadable = build_downloadable(track)
                queue_out.put(("status", f"{progress_status}Скачивается {downloadable.path}"))
                run_download(downloadable)
                completed += 1
                queue_out.put(("progress", completed, total))
        else:
            pending = set()
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for track in tracks:
                    track_counter += 1
                    if total:
                        progress_status = f"[{track_counter}/{total}] "
                    else:
                        progress_status = f"[{track_counter}] "

                    if not track.available:
                        queue_out.put(("status", f"{progress_status}Трек {track.title} недоступен"))
                        completed += 1
                        queue_out.put(("progress", completed, total))
                        continue

                    downloadable = build_downloadable(track)
                    queue_out.put(("status", f"{progress_status}Скачивается {downloadable.path}"))
                    pending.add(executor.submit(run_download, downloadable))

                    if len(pending) >= workers * 2:
                        done, pending = wait(pending, return_when=FIRST_COMPLETED)
                        for future in done:
                            future.result()
                            completed += 1
                            queue_out.put(("progress", completed, total))

                if pending:
                    done, _ = wait(pending)
                    for future in done:
                        future.result()
                        completed += 1
                        queue_out.put(("progress", completed, total))

        queue_out.put(("done", None))
    except Exception as exc:
        queue_out.put(("error", f"{type(exc).__name__}: {exc}"))


class DownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Yandex Music Downloader")

        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.progress_total: Optional[int] = None
        self.progress_running_indeterminate = False
        self.preview_after_id: Optional[str] = None
        self.preview_version = 0
        self.token_save_after_id: Optional[str] = None

        self._configure_styles()

        outer = ttk.Frame(root, padding=(24, 20, 24, 20), style="TFrame")
        outer.grid(row=0, column=0, sticky="nsew")

        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)

        ttk.Label(outer, text="Версия GUI 1.0.0 - Yandex Music Downloader", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )

        ttk.Label(outer, text="Ссылка", style="TLabel").grid(
            row=1, column=0, sticky="w", pady=(16, 0)
        )
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(outer, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=(16, 0))
        _setup_entry(self.url_entry)

        self.preview_var = tk.StringVar(value="")
        self.preview_label = ttk.Label(
            outer,
            textvariable=self.preview_var,
            wraplength=560,
            justify="left",
            style="Muted.TLabel",
        )
        self.preview_label.grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Label(outer, text="Токен", style="TLabel").grid(
            row=3, column=0, sticky="w", pady=(16, 0)
        )
        saved_token = _load_saved_token()
        token_value = saved_token if saved_token else os.getenv("YANDEX_MUSIC_TOKEN", "")
        self.token_var = tk.StringVar(value=token_value)
        self.token_entry = ttk.Entry(outer, textvariable=self.token_var, show="*")
        self.token_entry.grid(row=3, column=1, sticky="ew", pady=(16, 0))
        _setup_entry(self.token_entry)

        token_actions = ttk.Frame(outer, style="TFrame")
        token_actions.grid(row=3, column=2, sticky="e", pady=(16, 0), padx=(8, 0))

        self.save_token_var = tk.BooleanVar(value=bool(saved_token))
        self.save_token_check = ttk.Checkbutton(
            token_actions, text="Сохранить", variable=self.save_token_var
        )
        self.save_token_check.grid(row=0, column=0, sticky="e")

        self.show_token_var = tk.BooleanVar(value=False)
        self.show_token_check = ttk.Checkbutton(
            token_actions, text="Показать", variable=self.show_token_var, command=self._toggle_token
        )
        self.show_token_check.grid(row=0, column=1, sticky="e", padx=(8, 0))

        ttk.Label(outer, text="Папка", style="TLabel").grid(
            row=4, column=0, sticky="w", pady=(16, 0)
        )
        self.dir_var = tk.StringVar(value=os.getcwd())
        self.dir_entry = ttk.Entry(outer, textvariable=self.dir_var)
        self.dir_entry.grid(row=4, column=1, sticky="ew", pady=(16, 0))
        _setup_entry(self.dir_entry)
        self.dir_button = ttk.Button(outer, text="Выбрать…", command=self._choose_dir)
        self.dir_button.grid(row=4, column=2, sticky="e", padx=(8, 0), pady=(16, 0))

        ttk.Label(outer, text="Качество", style="TLabel").grid(
            row=5, column=0, sticky="w", pady=(12, 0)
        )
        self.quality_var = tk.StringVar(value=QUALITY_OPTIONS[0][0])
        self.quality_combo = ttk.Combobox(
            outer,
            textvariable=self.quality_var,
            values=[label for label, _ in QUALITY_OPTIONS],
            state="readonly",
        )
        self.quality_combo.grid(row=5, column=1, sticky="w", pady=(12, 0))

        ttk.Label(outer, text="Потоки", style="TLabel").grid(
            row=6, column=0, sticky="w", pady=(12, 0)
        )
        self.workers_var = tk.IntVar(value=1)
        self.workers_spin = ttk.Spinbox(
            outer,
            from_=1,
            to=4,
            textvariable=self.workers_var,
            width=6,
        )
        self.workers_spin.grid(row=6, column=1, sticky="w", pady=(12, 0))

        actions = ttk.Frame(outer, style="TFrame")
        actions.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=0)

        self.download_button = ttk.Button(
            actions, text="Скачать", command=self.start_download, style="Primary.TButton"
        )
        self.download_button.grid(row=0, column=0, sticky="ew")
        self.open_folder_button = ttk.Button(
            actions, text="Открыть папку", command=self._open_folder
        )
        self.open_folder_button.grid(row=0, column=1, sticky="ew", padx=(10, 0))

        self.status_var = tk.StringVar(value="Готово")
        self.status_label = ttk.Label(outer, textvariable=self.status_var, style="TLabel")
        self.status_label.grid(row=8, column=0, columnspan=3, sticky="w", pady=(18, 0))

        self.progress = ttk.Progressbar(
            outer, mode="determinate", style="Accent.Horizontal.TProgressbar"
        )
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(8, 0))

        self.url_var.trace_add("write", lambda *_: self._schedule_preview())
        self.token_var.trace_add("write", lambda *_: self._schedule_preview())
        self.save_token_var.trace_add("write", lambda *_: self._handle_save_toggle())
        self.token_var.trace_add("write", lambda *_: self._handle_token_change())

        self.root.after(100, self._poll_queue)
        self.root.bind("<Configure>", self._on_resize)

    def _configure_styles(self) -> None:
        self.root.configure(background="#f6f7fb")
        try:
            self.root.minsize(680, 560)
        except tk.TclError:
            pass

        is_windows = os.name == "nt"
        base_font = ("Segoe UI", 10) if is_windows else ("DejaVu Sans", 10)
        header_font = ("Segoe UI Semibold", 16) if is_windows else ("DejaVu Sans", 16, "bold")
        section_font = ("Segoe UI Semibold", 11) if is_windows else ("DejaVu Sans", 11, "bold")

        self.root.option_add("*Font", base_font)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        bg = "#f6f7fb"
        card = "#ffffff"
        field = "#ffffff"
        text = "#111827"
        muted = "#6b7280"
        accent = "#2563eb"

        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)

        style.configure("TLabel", background=bg, foreground=text)
        style.configure("Muted.TLabel", background=bg, foreground=muted)
        style.configure("Card.TLabel", background=card, foreground=text)
        style.configure("CardMuted.TLabel", background=card, foreground=muted)
        style.configure("Header.TLabel", background=bg, foreground=text, font=header_font)
        style.configure("Section.TLabel", background=card, foreground=text, font=section_font)

        style.configure("TEntry", fieldbackground=field, foreground=text, insertcolor=text)
        style.configure("TCombobox", fieldbackground=field, foreground=text)
        style.configure("TSpinbox", fieldbackground=field, foreground=text)
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", field)],
            foreground=[("readonly", text)],
        )

        style.configure(
            "Primary.TButton",
            background=accent,
            foreground="#ffffff",
            padding=(10, 6),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#1d4ed8"), ("disabled", "#93c5fd")],
            foreground=[("disabled", "#e5e7eb")],
        )

        style.configure(
            "TButton",
            background="#e5e7eb",
            foreground=text,
            padding=(8, 6),
        )
        style.map(
            "TButton",
            background=[("active", "#d1d5db"), ("disabled", "#e5e7eb")],
            foreground=[("disabled", "#9ca3af")],
        )

        style.configure(
            "Accent.Horizontal.TProgressbar",
            background=accent,
            troughcolor="#e5e7eb",
            thickness=14,
        )

    def _on_resize(self, event: tk.Event) -> None:
        if event.widget is not self.root:
            return
        width = max(420, event.width - 120)
        try:
            self.preview_label.configure(wraplength=width)
            self.status_label.configure(wraplength=width)
        except tk.TclError:
            pass

    def _toggle_token(self) -> None:
        self.token_entry.config(show="" if self.show_token_var.get() else "*")

    def _schedule_token_save(self) -> None:
        if self.token_save_after_id is not None:
            self.root.after_cancel(self.token_save_after_id)
            self.token_save_after_id = None
        self.token_save_after_id = self.root.after(500, self._persist_token)

    def _persist_token(self) -> None:
        self.token_save_after_id = None
        token = self.token_var.get().strip()
        if self.save_token_var.get():
            _save_token(token)
        else:
            _save_token("")

    def _handle_save_toggle(self) -> None:
        if self.save_token_var.get():
            self._schedule_token_save()
        else:
            _save_token("")

    def _handle_token_change(self) -> None:
        if self.save_token_var.get():
            self._schedule_token_save()

    def _schedule_preview(self) -> None:
        if self.preview_after_id is not None:
            self.root.after_cancel(self.preview_after_id)
            self.preview_after_id = None
        self.preview_after_id = self.root.after(600, self._start_preview)

    def _start_preview(self) -> None:
        self.preview_after_id = None
        url = self.url_var.get().strip()
        token = self.token_var.get().strip()
        if not url:
            self.preview_var.set("")
            return
        self.preview_var.set("Проверяю ссылку...")
        self.preview_version += 1
        version = self.preview_version
        threading.Thread(
            target=_preview_worker,
            args=(token, url, version, self.queue),
            daemon=True,
        ).start()

    def _choose_dir(self) -> None:
        initial = self.dir_var.get().strip() or os.getcwd()
        directory = filedialog.askdirectory(initialdir=initial)
        if directory:
            self.dir_var.set(directory)

    def _open_folder(self) -> None:
        folder = Path(self.dir_var.get().strip() or os.getcwd())
        if not folder.exists():
            messagebox.showerror("Папка не найдена", "Указанная папка не существует.")
            return
        if not folder.is_dir():
            messagebox.showerror("Папка не найдена", "Указанный путь не является папкой.")
            return
        try:
            if os.name == "nt":
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(folder)], check=False)
            else:
                subprocess.run(["xdg-open", str(folder)], check=False)
        except Exception as exc:
            messagebox.showerror("Ошибка", f"Не удалось открыть папку: {exc}")

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.download_button.config(state=state)
        self.open_folder_button.config(state=state)
        self.save_token_check.config(state=state)
        self.url_entry.config(state=state)
        self.token_entry.config(state=state)
        self.dir_entry.config(state=state)
        self.dir_button.config(state=state)
        self.quality_combo.config(state="disabled" if running else "readonly")
        self.workers_spin.config(state=state)
        self.show_token_check.config(state=state)

    def start_download(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return

        token = self.token_var.get().strip()
        url = self.url_var.get().strip()
        download_dir = self.dir_var.get().strip()
        if not token:
            messagebox.showerror(
                "Нужен токен",
                "Введите токен или установите переменную окружения YANDEX_MUSIC_TOKEN.",
            )
            return
        if not url:
            messagebox.showerror("Нужна ссылка", "Введите ссылку на Яндекс.Музыку.")
            return
        if not download_dir:
            messagebox.showerror("Нужна папка", "Укажите папку для скачивания.")
            return
        download_path = Path(download_dir)
        if download_path.exists() and not download_path.is_dir():
            messagebox.showerror("Нужна папка", "Указанный путь не является папкой.")
            return

        try:
            workers = int(self.workers_var.get())
        except (TypeError, ValueError):
            messagebox.showerror("Неверное значение", "Количество потоков должно быть числом.")
            return
        if workers < 1 or workers > 4:
            messagebox.showerror("Неверное значение", "Количество потоков должно быть от 1 до 4.")
            return

        quality = dict(QUALITY_OPTIONS).get(self.quality_var.get(), 2)

        self.status_var.set("Подготовка...")
        self.progress["value"] = 0
        self.progress_total = None
        self.progress_running_indeterminate = False
        self.progress.config(mode="determinate")

        self._set_running(True)
        self.worker_thread = threading.Thread(
            target=_download_worker,
            args=(token, url, quality, download_dir, workers, self.queue),
            daemon=True,
        )
        self.worker_thread.start()
        self.root.after(100, self._poll_queue)

    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                self._handle_queue_item(item)
        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _handle_queue_item(self, item: tuple) -> None:
        event = item[0]
        if event == "total":
            self.progress_total = item[1]
            if self.progress_total:
                self.progress.config(mode="determinate", maximum=self.progress_total)
            else:
                self.progress.config(mode="indeterminate")
                self.progress.start(10)
                self.progress_running_indeterminate = True
        elif event == "status":
            self.status_var.set(item[1])
        elif event == "preview":
            version, text = item[1], item[2]
            if version == self.preview_version:
                self.preview_var.set(text)
        elif event == "progress":
            current, total = item[1], item[2]
            if total:
                self.progress["value"] = current
            elif not self.progress_running_indeterminate:
                self.progress.config(mode="indeterminate")
                self.progress.start(10)
                self.progress_running_indeterminate = True
        elif event == "error":
            if self.progress_running_indeterminate:
                self.progress.stop()
            self.status_var.set("Ошибка")
            self._set_running(False)
            messagebox.showerror("Ошибка", item[1])
        elif event == "done":
            if self.progress_running_indeterminate:
                self.progress.stop()
            if self.progress_total:
                self.progress["value"] = self.progress_total
            self.status_var.set("Готово")
            self._set_running(False)


def main() -> None:
    root = tk.Tk()
    app = DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
