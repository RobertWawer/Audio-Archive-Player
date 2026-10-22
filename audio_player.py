from __future__ import annotations

import json
import ctypes
import argparse
from ctypes import wintypes
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from rich.style import Style
from rich.text import Text

from mutagen import File as MutagenFile
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Label, ListItem, ListView, Static

# py7zr imports native Cryptodome modules. Windows Smart App Control may
# block those modules; that must not prevent the whole player from starting.
PY7ZR_IMPORT_ERROR = None
try:
    import py7zr
except Exception as exc:
    py7zr = None
    PY7ZR_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
try:
    import rarfile
except ImportError:
    rarfile = None

AUDIO = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".wv", ".ape", ".aiff", ".aif"}
ARCHIVES = {".zip", ".7z", ".rar"}
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_CONFIG = {"theme": "textual-dark", "volume": 100, "last_directory": "", "playback": {}}
# Themes included by Textual. Press T to cycle through them.
THEME_NAMES = (
    "textual-dark", "textual-light", "nord", "gruvbox", "tokyo-night",
    "catppuccin-mocha", "catppuccin-latte", "dracula", "monokai", "flexoki",
)


def load_config():
    config = dict(DEFAULT_CONFIG)
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            config.update(loaded)
    except (OSError, json.JSONDecodeError):
        pass
    if config.get("theme") not in THEME_NAMES:
        config["theme"] = DEFAULT_CONFIG["theme"]
    try:
        config["volume"] = max(0, min(100, int(config.get("volume", 100))))
    except (TypeError, ValueError):
        config["volume"] = 100
    if not isinstance(config.get("last_directory"), str):
        config["last_directory"] = ""
    if not isinstance(config.get("playback"), dict):
        config["playback"] = {}
    return config


def save_config(config):
    """Write settings atomically so an interrupted save does not corrupt config.json."""
    try:
        tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
            f.write("\n")
        tmp_path.replace(CONFIG_PATH)
    except OSError:
        # The player should remain usable if its folder is read-only.
        pass


def natural(s):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r"(\d+)", s)]


def fmt(sec):
    if sec is None:
        return "--:--"
    try:
        sec = max(0, int(sec))
    except (TypeError, ValueError):
        return "--:--"
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def tags(path):
    try:
        f = MutagenFile(path, easy=True)
        t = f.tags if f and f.tags else {}
        def g(*names):
            for n in names:
                v = t.get(n)
                if v is not None:
                    return str(v[0] if isinstance(v, list) else v)
            return ""
        return {"artist": g("artist"), "albumartist": g("albumartist"),
                "album": g("album"), "title": g("title"),
                "date": g("date", "originaldate", "year"), "genre": g("genre"),
                "track": g("tracknumber"), "disc": g("discnumber")}
    except Exception:
        return {}


class Entry:
    def __init__(self, name, archive):
        self.name, self.archive = name, archive
    @property
    def display(self):
        return Path(self.name).name


def archive_list(path):
    ext = path.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    elif ext == ".7z":
        if not py7zr:
            detail = f"\nSzczegóły: {PY7ZR_IMPORT_ERROR}" if PY7ZR_IMPORT_ERROR else ""
            raise RuntimeError(
                "Obsługa archiwów 7Z jest niedostępna, ponieważ biblioteka "
                "py7zr nie może się załadować. ZIP i RAR nadal powinny działać."
                + detail
            )
        with py7zr.SevenZipFile(path, "r") as z:
            names = z.getnames()
    elif ext == ".rar":
        if not rarfile:
            raise RuntimeError("Brak rarfile. Zainstaluj rarfile.")
        with rarfile.RarFile(path) as r:
            names = r.namelist()
    else:
        names = []
    out = [Entry(n, path) for n in names if not n.endswith("/") and Path(n).suffix.lower() in AUDIO]
    return sorted(out, key=lambda x: natural(x.display))


def extract(e, target):
    target.mkdir(parents=True, exist_ok=True)
    out = target / Path(e.name).name
    # Avoid reusing an old, partially extracted copy.
    try:
        out.unlink()
    except FileNotFoundError:
        pass
    ext = e.archive.suffix.lower()
    if ext == ".zip":
        with zipfile.ZipFile(e.archive) as z, z.open(e.name) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    elif ext == ".7z":
        if not py7zr:
            detail = f"\nSzczegóły: {PY7ZR_IMPORT_ERROR}" if PY7ZR_IMPORT_ERROR else ""
            raise RuntimeError(
                "Obsługa archiwów 7Z jest niedostępna, ponieważ biblioteka "
                "py7zr nie może się załadować. ZIP i RAR nadal powinny działać."
                + detail
            )
        with py7zr.SevenZipFile(e.archive, "r") as z:
            z.extract(path=target, targets=[e.name])
        src = target / e.name
        if src != out:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, out)
    elif ext == ".rar":
        with rarfile.RarFile(e.archive) as r:
            r.extract(e.name, path=target)
        src = target / e.name
        if src != out:
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, out)
    return out


class _WindowsNamedPipe:
    """Windows named-pipe client using overlapped I/O.

    A synchronous ReadFile and WriteFile on the same handle can serialize and
    deadlock when one thread is blocked reading mpv events. Overlapped I/O lets
    the reader and command writer operate concurrently on the duplex pipe.
    """
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    FILE_FLAG_OVERLAPPED = 0x40000000
    BUFFER_SIZE = 4096
    ERROR_IO_PENDING = 997
    ERROR_OPERATION_ABORTED = 995
    ERROR_BROKEN_PIPE = 109
    ERROR_NO_DATA = 232
    ERROR_PIPE_NOT_CONNECTED = 233
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    class OVERLAPPED(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    def __init__(self, name):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel32
        k.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateEventW.restype = wintypes.HANDLE
        k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(self.OVERLAPPED)]
        k.ReadFile.restype = wintypes.BOOL
        k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(self.OVERLAPPED)]
        k.WriteFile.restype = wintypes.BOOL
        k.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.POINTER(self.OVERLAPPED),
                                          ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
        k.GetOverlappedResult.restype = wintypes.BOOL
        k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(self.OVERLAPPED)]
        k.CancelIoEx.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CloseHandle.restype = wintypes.BOOL

        self.handle = k.CreateFileW(
            name, self.GENERIC_READ | self.GENERIC_WRITE, 0, None,
            self.OPEN_EXISTING, self.FILE_ATTRIBUTE_NORMAL | self.FILE_FLAG_OVERLAPPED, None,
        )
        if self.handle == wintypes.HANDLE(-1).value:
            self.handle = None
            raise ctypes.WinError(ctypes.get_last_error())
        self._read_buffer = bytearray()

    def _transfer(self, *, write_data=None, read_size=0, timeout_ms=None):
        if self.handle is None:
            raise OSError("Potok IPC jest zamknięty.")
        k = self.kernel32
        event = k.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        ov = self.OVERLAPPED()
        ov.hEvent = event
        if write_data is not None:
            buffer = ctypes.create_string_buffer(write_data, len(write_data))
            count = len(write_data)
            ok = k.WriteFile(self.handle, buffer, count, None, ctypes.byref(ov))
        else:
            buffer = ctypes.create_string_buffer(read_size)
            count = read_size
            ok = k.ReadFile(self.handle, buffer, count, None, ctypes.byref(ov))
        try:
            if not ok:
                err = ctypes.get_last_error()
                if err != self.ERROR_IO_PENDING:
                    if write_data is None and err in (
                        self.ERROR_BROKEN_PIPE, self.ERROR_NO_DATA,
                        self.ERROR_PIPE_NOT_CONNECTED, self.ERROR_OPERATION_ABORTED,
                    ):
                        return b""
                    raise ctypes.WinError(err)
            wait_ms = self.INFINITE if timeout_ms is None else timeout_ms
            waited = k.WaitForSingleObject(event, wait_ms)
            if waited == self.WAIT_TIMEOUT:
                k.CancelIoEx(self.handle, ctypes.byref(ov))
                # Wait until the overlapped request has actually completed
                # before releasing its buffer and OVERLAPPED structure.
                k.WaitForSingleObject(event, self.INFINITE)
                if write_data is None:
                    return None
                raise TimeoutError("Przekroczono czas zapisu polecenia IPC mpv.")
            if waited != self.WAIT_OBJECT_0:
                raise ctypes.WinError(ctypes.get_last_error())
            transferred = wintypes.DWORD()
            if not k.GetOverlappedResult(self.handle, ctypes.byref(ov), ctypes.byref(transferred), False):
                err = ctypes.get_last_error()
                if err in (self.ERROR_BROKEN_PIPE, self.ERROR_NO_DATA,
                           self.ERROR_PIPE_NOT_CONNECTED, self.ERROR_OPERATION_ABORTED):
                    return b"" if write_data is None else 0
                raise ctypes.WinError(err)
            if write_data is not None:
                return transferred.value
            return buffer.raw[:transferred.value]
        finally:
            k.CloseHandle(event)

    def write(self, data):
        written = self._transfer(write_data=data, timeout_ms=2000)
        if written != len(data):
            raise OSError(f"Niepełny zapis IPC: {written}/{len(data)} bajtów")
        return written

    def flush(self):
        return None

    def readline(self):
        while True:
            idx = self._read_buffer.find(b"\n")
            if idx >= 0:
                end = idx + 1
                result = bytes(self._read_buffer[:end])
                del self._read_buffer[:end]
                return result
            if self.handle is None:
                return b""
            chunk = self._transfer(read_size=self.BUFFER_SIZE, timeout_ms=250)
            if chunk is None:
                continue
            if not chunk:
                return b""
            self._read_buffer.extend(chunk)

    def close(self):
        if self.handle is not None:
            handle = self.handle
            self.handle = None
            try:
                self.kernel32.CancelIoEx(handle, None)
            except Exception:
                pass
            self.kernel32.CloseHandle(handle)


class Mpv:
    PIPE_PREFIX = r"\\.\pipe\audio_archive_player"

    def __init__(self, event_callback):
        self.pipe_name = f"{self.PIPE_PREFIX}_{os.getpid()}_{id(self):x}"
        self.proc = None
        self.event_callback = event_callback
        self.reader_thread = None
        self.pipe = None
        self.pipe_ready = threading.Event()
        self.reader_stop = threading.Event()
        self.write_lock = threading.Lock()
        self.reader_error = None
        self.log_path = Path(__file__).with_name("mpv_startup.log")
        self.log_file = None

    def exe(self):
        # Prefer mpv.exe on Windows. Some installations expose mpv.com first
        # on PATH; that console launcher can be unsuitable for a hidden child.
        local = Path(__file__).with_name("mpv.exe")
        if local.exists():
            return str(local)

        p = shutil.which("mpv.exe")
        if p:
            return p

        p = shutil.which("mpv")
        if p and Path(p).suffix.lower() != ".com":
            return p
        if p:
            sibling = Path(p).with_name("mpv.exe")
            if sibling.exists():
                return str(sibling)
        raise RuntimeError(
            "Nie znaleziono mpv.exe. Sprawdź instalację mpv."
        )

    def start(self, initial_volume=100):
        self.stop()
        self.reader_stop.clear()
        self.pipe_ready.clear()

        self.reader_error = None
        try:
            if self.log_file:
                self.log_file.close()
        except Exception:
            pass
        self.log_file = open(self.log_path, "ab", buffering=0)
        self.proc = subprocess.Popen(
            [
                self.exe(),
                "--no-video",
                "--idle=yes",
                "--force-window=no",
                "--really-quiet",
                f"--volume={max(0, min(100, int(initial_volume)))}",
                f"--input-ipc-server={self.pipe_name}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        self.reader_thread = threading.Thread(
            target=self._reader,
            daemon=True,
            name="mpv-ipc",
        )
        self.reader_thread.start()

        if not self.pipe_ready.wait(timeout=6) or self.pipe is None:
            details = self.reader_error or "mpv nie otworzył potoku IPC."
            if self.proc is not None and self.proc.poll() is not None:
                details += f" Proces mpv zakończył się kodem {self.proc.returncode}."
            self.stop()
            raise RuntimeError(
                "Nie udało się połączyć z IPC mpv. " + details +
                f" Szczegóły: {self.log_path}"
            )

        # mpv reports these properties whenever they change.
        self.command("observe_property", 1, "time-pos")
        self.command("observe_property", 2, "duration")
        self.command("observe_property", 3, "pause")
        self.command("observe_property", 4, "volume")

        # Also request the current values. This is important when a property
        # does not generate a change event immediately after a new load.
        self.request_state()

    def _reader(self):
        deadline = time.monotonic() + 5

        while not self.reader_stop.is_set():
            try:
                pipe = _WindowsNamedPipe(self.pipe_name)
                self.pipe = pipe
                self.pipe_ready.set()

                while not self.reader_stop.is_set():
                    line = pipe.readline()
                    if not line:
                        break

                    try:
                        obj = json.loads(
                            line.decode("utf-8", errors="replace")
                        )
                    except json.JSONDecodeError:
                        continue

                    try:
                        self.event_callback(obj)
                    except Exception:
                        pass

                try:
                    pipe.close()
                except Exception:
                    pass

                self.pipe = None

            except OSError as e:
                self.reader_error = f"{type(e).__name__}: {e}"
                if time.monotonic() >= deadline:
                    self.pipe_ready.set()
                    return
                time.sleep(0.05)

            if not self.reader_stop.is_set():
                time.sleep(0.05)

    def command(self, *args):
        raw = (
            json.dumps(
                {"command": list(args)},
                ensure_ascii=False,
            ) + "\n"
        ).encode("utf-8")

        deadline = time.monotonic() + 2

        while time.monotonic() < deadline:
            if self.reader_stop.is_set():
                raise OSError("IPC mpv jest zamknięte.")

            pipe = self.pipe
            if pipe is None:
                time.sleep(0.01)
                continue

            try:
                with self.write_lock:
                    pipe.write(raw)
                    try:
                        pipe.flush()
                    except Exception:
                        pass
                return
            except OSError:
                time.sleep(0.02)

        raise OSError("Nie udało się wysłać polecenia do IPC mpv.")

    def request_state(self):
        for prop in ("time-pos", "duration", "pause", "volume"):
            try:
                self.command("get_property", prop)
            except OSError:
                pass

    def load(self, path, start_position=0.0, paused=False):
        options = {}
        if start_position and start_position > 0:
            options["start"] = str(float(start_position))
        if paused:
            options["pause"] = "yes"
        command = ["loadfile", str(Path(path).resolve()), "replace"]
        if options:
            command.extend([-1, options])
        self.command(*command)

    def stop_playback(self):
        self.command("stop")

    def pause(self):
        self.command("cycle", "pause")

    def seek(self, seconds):
        self.command(
            "seek",
            seconds,
            "relative",
            "exact",
        )
        # Get the exact new position instead of waiting for an event.
        self.command("get_property", "time-pos")

    def volume(self, value):
        self.command(
            "set_property",
            "volume",
            max(0, min(100, value)),
        )

    def stop(self):
        self.reader_stop.set()

        if self.proc and self.proc.poll() is None:
            try:
                pipe = self.pipe
                if pipe is not None:
                    raw = (
                        json.dumps({"command": ["quit"]}) + "\n"
                    ).encode("utf-8")
                    with self.write_lock:
                        pipe.write(raw)
                        try:
                            pipe.flush()
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=2)
                except Exception:
                    pass

        try:
            if self.pipe is not None:
                self.pipe.close()
        except Exception:
            pass

        self.pipe = None
        self.proc = None
        self.pipe_ready.clear()
        try:
            if self.log_file:
                self.log_file.close()
        except Exception:
            pass
        self.log_file = None


class Player(App):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #browser { width: 65%; border: round $accent; }
    #info { width: 35%; border: round $accent; padding: 1 2; }
    #total { height: 1; padding: 0 1; }
    .audio-row { width: 100%; height: 1; }
    .audio-name { width: 1fr; }
    .audio-duration { width: 8; text-align: right; color: $text-muted; }
    #now { height: 9; border: round $accent; padding: 1 2; }
    #bar { height: 1; margin: 0 2; content-align: center middle; }
    #time { height: 1; content-align: center middle; }
    ListItem.playing { background: $accent; color: $text; text-style: bold; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"), ("space", "pause_playback", "Play/Pause"),
        ("s", "stop_playback", "Stop"), ("left", "seek_back", "-10s"),
        ("right", "seek_forward", "+10s"), ("-", "volume_down", "Volume -"),
        ("+", "volume_up", "Volume +"), ("=", "volume_up", "Volume +"),
        ("t", "cycle_theme", "Theme"),
    ]

    def __init__(self, start_dir=None):
        super().__init__()
        self.config = load_config()
        # An explicit command-line path wins; otherwise reopen the last browsed directory.
        saved_dir = self.config.get("last_directory", "")
        if start_dir:
            self.cwd = Path(start_dir).expanduser().resolve()
        elif saved_dir and Path(saved_dir).is_dir():
            self.cwd = Path(saved_dir).resolve()
        else:
            self.cwd = Path.cwd()
        try:
            self.theme = self.config["theme"]
        except Exception:
            self.theme = DEFAULT_CONFIG["theme"]
        self.mode = "fs"
        self.items = []
        self.archive = None
        self.index = -1
        self.current = None
        self.pos = 0.0
        self.length = 0.0
        self.paused = False
        self.vol = self.config["volume"]
        self.mpv = Mpv(self.on_mpv_event)
        self._duration_scan_id = 0
        self.restore_enabled = start_dir is None
        self.saved_playback = dict(self.config.get("playback") or {}) if self.restore_enabled else {}
        self.current_entry = None
        self._last_playback_save = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="browser"):
                yield Label("", id="title")
                yield Label("Łączny czas audio: —", id="total")
                yield ListView(id="list")
            with Vertical(id="info"):
                yield Static("Wybierz plik.", id="details")
        with Vertical(id="now"):
            yield Static("■ Nie odtwarzam", id="nowtext")
            yield Static("░" * 40 + "   0%", id="bar")
            yield Label(f"00:00 / 00:00    Volume: {self.vol}%", id="time")
        yield Footer()

    def on_mount(self):
        try:
            self.mpv.start(initial_volume=self.vol)
        except Exception as e:
            self.query_one("#details", Static).update(str(e))
        if not self.cwd.exists() or not self.cwd.is_dir():
            self.query_one("#details", Static).update(
                f"Podana ścieżka nie jest katalogiem lub nie istnieje:\n{self.cwd}"
            )
            self.cwd = Path.cwd()
        # Restoring a saved filesystem selection calls refresh_fs() itself.
        # Do not build the same ListView twice during startup: Textual may still
        # be processing removals from clear(), which can briefly leave duplicate
        # IDs (for example, fs-audio-1) in the child list.
        if self.restore_enabled and self.saved_playback:
            self.restore_playback_state()
            if not self.query_one("#list", ListView).children:
                self.refresh_fs()
        else:
            self.refresh_fs()
        self.set_interval(.20, self.tick)

    def on_unmount(self):
        self.save_settings()
        self.mpv.stop()
        if hasattr(self, "_temp"):
            shutil.rmtree(self._temp, ignore_errors=True)

    def save_settings(self):
        self.config.update({
            "theme": getattr(self, "theme", DEFAULT_CONFIG["theme"]),
            "volume": max(0, min(100, int(getattr(self, "vol", 100)))),
            "last_directory": str(getattr(self, "cwd", Path.cwd())),
        })
        if hasattr(self, "current") and self.current:
            paused = bool(self.paused)
            # On exit, resume only when the user had explicitly paused playback.
            self.config["playback"] = {
                "mode": self.mode,
                "directory": str(self.cwd),
                "archive": str(self.archive) if self.mode == "archive" and self.archive else "",
                "file": self.current_entry.name if self.mode == "archive" and self.current_entry else str(self.current),
                "position": max(0.0, float(self.pos)) if paused else 0.0,
                "paused": paused,
            }
        save_config(self.config)

    def save_stopped_selection(self, *, mode=None, file_name=None, archive=None):
        """Persist the last selected track without requesting automatic playback."""
        mode = mode or self.mode
        if file_name is None:
            if mode == "archive" and self.current_entry:
                file_name = self.current_entry.name
            elif self.current:
                file_name = str(self.current)
        # Save theme/volume/directory first, then override any active-playback
        # snapshot with the explicit stopped-selection state.
        self.save_settings()
        self.config["playback"] = {
            "mode": mode,
            "directory": str(self.cwd),
            "archive": str(archive or self.archive or "") if mode == "archive" else "",
            "file": file_name or "",
            "position": 0.0,
            "paused": False,
        }
        save_config(self.config)

    def restore_playback_state(self):
        state = self.saved_playback
        if not state:
            return
        try:
            mode = state.get("mode", "fs")
            directory = Path(state.get("directory", "")).expanduser()
            paused = bool(state.get("paused", False))
            position = max(0.0, float(state.get("position", 0.0)))
            file_name = state.get("file", "")
            if mode == "archive":
                archive_path = Path(state.get("archive", "")).expanduser()
                if not archive_path.is_file() or archive_path.suffix.lower() not in ARCHIVES:
                    return
                self.cwd = directory if directory.is_dir() else archive_path.parent
                self.refresh_fs()
                self.open_archive(archive_path)
                index = next((i for i, e in enumerate(self.items) if e.name == file_name), -1)
                if index < 0:
                    return
                if paused:
                    self.play_archive(index, resume_position=position, resume_paused=True)
                else:
                    self.mark_playing(index + 1)
                    self.query_one("#list", ListView).index = index + 1
            else:
                if not directory.is_dir():
                    return
                self.cwd = directory
                self.refresh_fs()
                target = Path(file_name).expanduser() if file_name else None
                index = next((i for i, item in enumerate(self.items) if target and item == target), -1)
                if index < 0:
                    return
                if paused:
                    self.play_fs(index, resume_position=position, resume_paused=True)
                else:
                    row = self.fs_audio_row(index)
                    self.mark_playing(row)
                    self.query_one("#list", ListView).index = row
        except (OSError, ValueError, TypeError, RuntimeError):
            # A removed/moved file or malformed state must not prevent startup.
            return

    def folder_label(self, name):
        """Return a monochrome folder glyph colored with the active theme accent."""
        theme = self.current_theme
        accent = getattr(theme, "accent", "#00aaee")
        rich_text = Text("▰ ", style=Style(color=accent, bold=True))
        rich_text.append(name)
        return rich_text

    def refresh_theme_icons(self):
        """Recolor folder glyphs after a theme switch without rebuilding the list."""
        lv = self.query_one("#list", ListView)
        if self.mode == "archive":
            if lv.children:
                label = lv.children[0].query_one(Label)
                label.update(self.folder_label(".."))
            return
        for p, item in zip(self.items, lv.children):
            if p.is_dir():
                name = ".." if p == self.cwd.parent and p != self.cwd else p.name
                item.query_one(Label).update(self.folder_label(name))

    def action_cycle_theme(self):
        current = getattr(self, "theme", DEFAULT_CONFIG["theme"])
        try:
            index = THEME_NAMES.index(current)
        except ValueError:
            index = 0
        # Try each built-in theme in order, skipping themes unavailable in this
        # installed Textual version.
        for offset in range(1, len(THEME_NAMES) + 1):
            candidate = THEME_NAMES[(index + offset) % len(THEME_NAMES)]
            try:
                self.theme = candidate
                self.refresh_theme_icons()
                self.save_settings()
                self.notify(f"Motyw: {candidate}", timeout=2)
                return
            except Exception:
                continue
        self.notify("Nie udało się zmienić motywu.", severity="warning")

    def clear_playing_highlight(self):
        lv = self.query_one("#list", ListView)
        for item in lv.children:
            item.remove_class("playing")

    def mark_playing(self, row_index):
        lv = self.query_one("#list", ListView)
        self.clear_playing_highlight()
        if 0 <= row_index < len(lv.children):
            lv.children[row_index].add_class("playing")

    def fs_items(self):
        parent = [self.cwd.parent] if self.cwd.parent != self.cwd else []
        try:
            p = sorted(self.cwd.iterdir(), key=lambda x: (not x.is_dir(), natural(x.name)))
        except PermissionError:
            p = []
        return parent + [x for x in p if x.is_dir() or x.suffix.lower() in ARCHIVES | AUDIO]

    def _set_list_cursor(self, index):
        """Apply a cursor position after Textual has finished rebuilding the list."""
        lv = self.query_one("#list", ListView)
        if index is None or not 0 <= index < len(lv.children):
            return
        # The highlight is only immediately visible when the ListView owns focus.
        lv.focus()
        lv.index = index

    def refresh_fs(self, select_path=None):
        """Rebuild the filesystem list and restore the relevant cursor position."""
        self.mode = "fs"
        self.archive = None
        self.items = self.fs_items()
        self.index = -1
        lv = self.query_one("#list", ListView)
        lv.clear()
        for i, p in enumerate(self.items):
            if p.is_dir():
                name = ".." if p == self.cwd.parent and p != self.cwd else p.name
                label = Label(self.folder_label(name))
                lv.append(ListItem(label))
            elif p.suffix.lower() in AUDIO:
                row = Horizontal(
                    Label("🎵 " + p.name, classes="audio-name"),
                    Label("…", classes="audio-duration"),
                    classes="audio-row",
                )
                lv.append(ListItem(row, id=f"fs-audio-{i}"))
            else:
                text = ("📦 " if p.suffix.lower() in ARCHIVES else "🎵 ") + p.name
                lv.append(ListItem(Label(text)))
        self.query_one("#title", Label).update("Browser: " + str(self.cwd))

        selected_index = None
        # If the currently playing filesystem track is in this view, keep it
        # selected even after browsing other directories or archives.
        if self.current and self.current_entry is None:
            selected_index = next(
                (i for i, entry in enumerate(self.items) if entry == self.current),
                None,
            )
        # Otherwise honor the navigation target (e.g. the folder/archive we left).
        if selected_index is None and select_path is not None:
            target = Path(select_path)
            selected_index = next(
                (i for i, entry in enumerate(self.items) if entry == target),
                None,
            )
        if selected_index is not None:
            self.call_after_refresh(self._set_list_cursor, selected_index)
        self.scan_directory_duration()

    def open_archive(self, p):
        self.mode = "archive"
        self.archive = p
        self.items = archive_list(p)
        self.index = -1
        lv = self.query_one("#list", ListView)
        lv.clear()
        # Same navigation convention as directories: .. exits the archive.
        lv.append(ListItem(Label(self.folder_label("..")), id="archive_parent"))
        for i, e in enumerate(self.items):
            row = Horizontal(
                Label("🎵 " + e.display, classes="audio-name"),
                Label("…", classes="audio-duration"),
                classes="audio-row",
            )
            lv.append(ListItem(row, id=f"archive-{i}"))
        self.query_one("#title", Label).update(f"📦 {p.name} ({len(self.items)} audio)")

        # When revisiting the archive that is currently playing, select its track.
        selected_index = None
        if self.current_entry is not None and Path(self.current_entry.archive) == p:
            selected_index = next(
                (i + 1 for i, entry in enumerate(self.items)
                 if entry.name == self.current_entry.name),
                None,
            )
        if selected_index is not None:
            self.call_after_refresh(self._set_list_cursor, selected_index)
            self.call_after_refresh(self.mark_playing, selected_index)
        self.scan_archive_duration()

    @staticmethod
    def _file_duration(path):
        """Read duration metadata without decoding the audio stream."""
        try:
            media = MutagenFile(path)
            length = getattr(getattr(media, "info", None), "length", None) if media else None
            return float(length) if length is not None and float(length) >= 0 else None
        except Exception:
            return None

    def _start_duration_scan(self, mode, entries, archive=None):
        """Read per-file durations and their total off the UI thread."""
        self._duration_scan_id += 1
        scan_id = self._duration_scan_id
        self.query_one("#total", Label).update("Łączny czas audio: obliczanie…")

        def worker():
            total = 0.0
            found = 0
            errors = 0
            durations = {}
            if mode == "fs":
                for path in entries:
                    duration = self._file_duration(path)
                    durations[str(path)] = duration
                    if duration is None:
                        errors += 1
                    else:
                        total += duration
                        found += 1
            else:
                temp = Path(tempfile.mkdtemp(prefix="aap_duration_"))
                try:
                    for i, entry in enumerate(entries):
                        try:
                            extracted = extract(entry, temp)
                            duration = self._file_duration(extracted)
                            durations[i] = duration
                            if duration is None:
                                errors += 1
                            else:
                                total += duration
                                found += 1
                            try:
                                extracted.unlink()
                            except OSError:
                                pass
                        except Exception:
                            durations[i] = None
                            errors += 1
                finally:
                    shutil.rmtree(temp, ignore_errors=True)
            try:
                self.call_from_thread(
                    self._duration_scan_done, scan_id, mode, total, found,
                    errors, len(entries), durations
                )
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True, name="audio-duration-scan").start()

    def _duration_scan_done(self, scan_id, mode, total, found, errors, count, durations):
        if scan_id != self._duration_scan_id:
            return
        summary = f"Łączny czas audio: {fmt(total)} ({found}/{count} plików)"
        if errors:
            summary += f"; brak danych: {errors}"
        self.query_one("#total", Label).update(summary)

        lv = self.query_one("#list", ListView)
        for item in lv.children:
            try:
                if mode == "fs" and item.id and item.id.startswith("fs-audio-"):
                    index = int(item.id.rsplit("-", 1)[1])
                    duration = durations.get(str(self.items[index]))
                elif mode == "archive" and item.id and item.id.startswith("archive-"):
                    index = int(item.id.rsplit("-", 1)[1])
                    duration = durations.get(index)
                else:
                    continue
                item.query_one(".audio-duration", Label).update(fmt(duration))
            except (ValueError, IndexError, Exception):
                # A row may disappear while the user navigates to another view.
                continue

    def scan_directory_duration(self):
        entries = [p for p in self.items if p.is_file() and p.suffix.lower() in AUDIO]
        self._start_duration_scan("fs", entries)

    def scan_archive_duration(self):
        self._start_duration_scan("archive", list(self.items), self.archive)

    def on_list_view_selected(self, event):
        item = event.item
        if item.id == "archive_parent":
            # Return to the directory and keep the archive we just left selected.
            archive_path = self.archive
            self.refresh_fs(select_path=archive_path)
            return

        i = event.list_view.index
        if i is None:
            return

        if self.mode == "archive":
            # Row 0 is .., so audio index is row - 1.
            if i > 0:
                self.play_archive(i - 1)
            return

        if i >= len(self.items):
            return
        p = self.items[i]
        if p == self.cwd.parent and p != self.cwd:
            # Going up: highlight the directory we just exited.
            exited_directory = self.cwd
            self.cwd = p
            self.refresh_fs(select_path=exited_directory)
        elif p.is_dir():
            # Going down: remember the child directory so it is selected on return.
            self.cwd = p
            self.refresh_fs()
        elif p.suffix.lower() in ARCHIVES:
            try:
                self.open_archive(p)
            except Exception as e:
                self.query_one("#details", Static).update("Błąd:\n" + str(e))
        else:
            self.play_fs(i)

    def play_fs(self, i, resume_position=0.0, resume_paused=False):
        p = self.items[i]
        self.index = i
        self.current = p
        self.current_entry = None
        self.pos = max(0.0, float(resume_position)) if resume_paused else 0.0
        self.length = 0.0
        self.paused = bool(resume_paused)
        self.show(tags(p), p.name)
        self.mpv.load(p, start_position=self.pos, paused=resume_paused)
        self.mark_playing(self.fs_audio_row(i))
        if resume_paused:
            self.notify(f"Wznowiono w pauzie: {p.name} ({fmt(self.pos)})", timeout=3)

    def fs_audio_row(self, item_index):
        # ListView has exactly one row for each item in self.items.
        return item_index

    def _extract_archive_worker(self, entry, target, index, resume_position=0.0, resume_paused=False):
        """Extract off the UI thread so large archives cannot freeze Textual."""
        try:
            path = extract(entry, target)
            self.call_from_thread(self._archive_extract_done, path, entry, index, resume_position, resume_paused)
        except Exception as e:
            self.call_from_thread(self._archive_extract_error, str(e), entry)

    def _archive_extract_done(self, path, entry, index, resume_position=0.0, resume_paused=False):
        self._archive_worker = None
        self.index = index
        self.current = path
        self.current_entry = entry
        self.pos = max(0.0, float(resume_position)) if resume_paused else 0.0
        self.length = 0.0
        self.paused = bool(resume_paused)
        self.show(tags(path), entry.display)
        self.mpv.load(path, start_position=self.pos, paused=resume_paused)
        self.mark_playing(index + 1)
        if resume_paused:
            self.notify(f"Wznowiono w pauzie: {entry.display} ({fmt(self.pos)})", timeout=3)

    def _archive_extract_error(self, message, entry):
        self._archive_worker = None
        self.query_one("#details", Static).update(
            f"Błąd odczytu archiwum ({entry.display}):\n{message}"
        )

    def play_archive(self, i, resume_position=0.0, resume_paused=False):
        # Avoid concurrent writes into the same temporary directory.
        worker = getattr(self, "_archive_worker", None)
        if worker is not None and worker.is_alive():
            self.query_one("#details", Static).update(
                "Przygotowywanie poprzedniego pliku z archiwum..."
            )
            return

        entry = self.items[i]
        target = self.temp_dir()
        self._archive_worker = threading.Thread(
            target=self._extract_archive_worker,
            args=(entry, target, i, resume_position, resume_paused),
            daemon=True,
            name="archive-extractor",
        )
        self._archive_worker.start()
        self.query_one("#details", Static).update(
            "Przygotowywanie pliku z archiwum:\n" + entry.display
        )

    def temp_dir(self):
        # Reuse mpv's private temporary directory and clean the previous extracted file.
        if not hasattr(self, "_temp"):
            self._temp = Path(tempfile.mkdtemp(prefix="audio_archive_player_"))
        return self._temp

    def show(self, t, name):
        self.query_one("#details", Static).update(
            f"{name}\n\nArtist: {t.get('artist','')}\nAlbum Artist: {t.get('albumartist','')}\n"
            f"Album: {t.get('album','')}\nTitle: {t.get('title','')}\nYear: {t.get('date','')}\n"
            f"Genre: {t.get('genre','')}\nTrack: {t.get('track','')}\nDisc: {t.get('disc','')}"
        )
        artist = t.get("artist")
        title = t.get("title") or name
        self.query_one("#nowtext", Static).update("▶ " + (artist + " — " if artist else "") + title)

    def on_mpv_event(self, obj):
        event = obj.get("event")
        name = obj.get("name")
        if event == "property-change":
            data = obj.get("data")
            if name == "time-pos":
                self.pos = float(data or 0)
            elif name == "duration":
                self.length = float(data or 0)
            elif name == "pause":
                self.paused = bool(data)
            elif name == "volume" and data is not None:
                self.vol = int(float(data))
        elif event == "end-file" and obj.get("reason") == "eof":
            try:
                self.call_from_thread(self.next_track)
            except Exception:
                pass

    def tick(self):
        # Ask mpv for the authoritative values periodically.
        # The UI never estimates playback time with a Python clock.
        if self.current:
            now = time.monotonic()
            if now - getattr(self, "_last_state_request", 0.0) >= 1.0:
                self._last_state_request = now
                self.mpv.request_state()
            # Save paused playback position periodically so a crash/power loss
            # does not lose the most recent pause point.
            if self.paused and now - self._last_playback_save >= 1.0:
                self._last_playback_save = now
                self.save_settings()

        pos = max(0.0, self.pos)
        length = max(0.0, self.length)
        bar = self.query_one("#bar", Static)
        ratio = min(1.0, max(0.0, pos / length)) if length > 0 else 0.0
        # Derive the bar length from the actual widget width, so resizing the
        # terminal/window automatically changes the number of rendered cells.
        percent = int(ratio * 100)
        bar_width = max(1, bar.size.width - 7)
        filled = min(bar_width, int(ratio * bar_width))
        empty = bar_width - filled
        # Use theme colors and one row of solid block characters.
        theme = self.current_theme
        accent = getattr(theme, "accent", "#00aaee")
        muted = getattr(theme, "foreground", "#888888")
        progress = Text()
        progress.append("█" * filled, style=Style(color=accent, bold=True))
        progress.append("░" * empty, style=Style(color=muted, dim=True))
        progress.append(f"  {percent:3d}%", style=Style(color=muted, bold=True))
        bar.update(progress)
        self.query_one("#time", Label).update(
            f"{fmt(pos)} / {fmt(length)}    Volume: {self.vol}%" + ("  ⏸" if self.paused else "")
        )

    def next_track(self):
        i = self.index + 1
        if self.mode == "archive":
            if i < len(self.items):
                self.play_archive(i)
            else:
                self.action_stop()
        else:
            audio = [p for p in self.items if p.is_file() and p.suffix.lower() in AUDIO]
            current_pos = -1
            if self.current:
                for n, p in enumerate(audio):
                    if p == self.current:
                        current_pos = n
                        break
            if current_pos + 1 < len(audio):
                self.play_fs(self.items.index(audio[current_pos + 1]))
            else:
                self.action_stop()

    def action_pause_playback(self):
        if self.current:
            self.mpv.pause()

    def action_stop_playback(self):
        self.action_stop()

    def action_stop(self):
        # Keep the selected track and its containing view, but do not autoplay it
        # on the next launch.
        self.save_stopped_selection()
        try:
            self.mpv.stop_playback()
        except Exception:
            pass
        self.current = None
        self.current_entry = None
        self.index = -1
        self.pos = 0.0
        self.length = 0.0
        self.paused = False
        self.clear_playing_highlight()
        self.query_one("#nowtext", Static).update("■ Zatrzymano")
        theme = self.current_theme
        muted = getattr(theme, "foreground", "#888888")
        reset_width = max(1, self.query_one("#bar", Static).size.width - 7)
        reset_bar = Text("░" * reset_width + "    0%", style=Style(color=muted, dim=True))
        self.query_one("#bar", Static).update(reset_bar)
        self.query_one("#time", Label).update(
            f"00:00 / 00:00    Volume: {self.vol}%"
        )

    def action_seek_back(self):
        if self.current:
            self.mpv.seek(-10)

    def action_seek_forward(self):
        if self.current:
            self.mpv.seek(10)

    def action_volume_down(self):
        self.vol = max(0, self.vol - 5)
        try:
            self.mpv.volume(self.vol)
        except OSError:
            pass
        self.save_settings()

    def action_volume_up(self):
        self.vol = min(100, self.vol + 5)
        try:
            self.mpv.volume(self.vol)
        except OSError:
            pass
        self.save_settings()

    def action_quit(self):
        self.save_settings()
        self.mpv.stop()
        if hasattr(self, "_temp"):
            shutil.rmtree(self._temp, ignore_errors=True)
        self.exit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Odtwarzacz plików audio i muzyki z archiwów ZIP/7Z/RAR. Ustawienia zapisuje w config.json."
    )
    parser.add_argument(
        "directory", nargs="?", default=None,
        help="katalog startowy (ścieżka względna względem bieżącego katalogu lub bezwzględna)"
    )
    args = parser.parse_args()
    start_dir = Path(args.directory).expanduser().resolve() if args.directory else None
    if start_dir is not None and (not start_dir.exists() or not start_dir.is_dir()):
        parser.error(f"podana ścieżka nie jest istniejącym katalogiem: {start_dir}")
    Player(start_dir=start_dir).run()
