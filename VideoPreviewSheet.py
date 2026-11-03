#!/usr/bin/env python3
"""
Video Preview Sheet
-------------------
Drag & drop contact-sheet generator.  FFmpeg extracts the frames, Pillow
assembles the sheet (grid, borders, burned-in timestamps with optional stroke).

Requirements:  pip install -r requirements.txt   +   ffmpeg & ffprobe on PATH
Run:           python preview_sheet.py
"""
import io
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from PySide6.QtCore import QSettings, QSize, Qt, QPointF, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QIcon, QKeySequence, QPainter,
    QPalette, QPen, QPixmap, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WINGET_CMD = "winget install Gyan.FFmpeg"
APP_ID = "VideoPreviewSheet.App"


def resource_path(name: str) -> str:
    """Path to a bundled file, both when run as a script and as a PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)
POSITIONS = ["Top left", "Top center", "Top right",
             "Middle left", "Center", "Middle right",
             "Bottom left", "Bottom center", "Bottom right"]
DEFAULT_FONT_LABEL = "Arial Bold Italic"

VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
    ".mpeg", ".ts", ".m2ts", ".mts", ".3gp", ".ogv", ".vob", ".mxf",
}


# ───────────────────────────── Engine ──────────────────────────────────────

@dataclass
class SheetSettings:
    cols: int
    rows: int
    width: int
    border: int
    border_color: str
    stamp: bool
    text_color: str
    font_size: int
    stroke: bool
    stroke_color: str
    stroke_width: int
    out_dir: Optional[str]
    rename: Optional[str]
    font_path: Optional[str] = None      # None -> Arial Bold Italic
    position: str = "Bottom right"
    bold: bool = True
    italic: bool = True


class Cancelled(Exception):
    pass


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL,
                          creationflags=NO_WINDOW)


def probe_duration(path: str) -> float:
    for entry in ("format=duration", "stream=duration"):
        cmd = [FFPROBE, "-v", "error"]
        if entry.startswith("stream"):
            cmd += ["-select_streams", "v:0"]
        cmd += ["-show_entries", entry, "-of", "default=noprint_wrappers=1:nokey=1", path]
        try:
            d = float(_run(cmd).stdout.decode().strip().splitlines()[0])
            if d > 0:
                return d
        except (ValueError, IndexError):
            pass
    return 0.0


def grab_frame(path: str, t: float, tile_w: int) -> Optional[Image.Image]:
    for ts in (t, max(0.0, t - 2.0), 0.0):
        r = _run([
            FFMPEG, "-v", "error", "-ss", f"{ts:.3f}", "-i", path,
            "-frames:v", "1", "-an", "-sn",
            "-vf", f"scale={tile_w}:-2:flags=lanczos",
            "-f", "image2pipe", "-vcodec", "png", "-",
        ])
        if r.returncode == 0 and r.stdout:
            try:
                return Image.open(io.BytesIO(r.stdout)).convert("RGB")
            except Exception:
                pass
    return None


ARIAL_FILES = {
    # (bold, italic): Windows, macOS, Linux (msttcorefonts / Liberation / DejaVu) names
    (True, True): ("arialbi.ttf", "Arial Bold Italic.ttf",
                   "/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf",
                   "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold_Italic.ttf",
                   "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",
                   "LiberationSans-BoldItalic.ttf", "DejaVuSans-BoldOblique.ttf"),
    (True, False): ("arialbd.ttf", "Arial Bold.ttf",
                    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                    "LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf"),
    (False, True): ("ariali.ttf", "Arial Italic.ttf",
                    "/System/Library/Fonts/Supplemental/Arial Italic.ttf",
                    "/usr/share/fonts/truetype/msttcorefonts/Arial_Italic.ttf",
                    "/usr/share/fonts/truetype/liberation/LiberationSans-Italic.ttf",
                    "LiberationSans-Italic.ttf", "DejaVuSans-Oblique.ttf"),
    (False, False): ("arial.ttf", "Arial.ttf",
                     "/System/Library/Fonts/Supplemental/Arial.ttf",
                     "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
                     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                     "LiberationSans-Regular.ttf", "DejaVuSans.ttf"),
}


def load_font(size: int, path: Optional[str] = None, bold: bool = True, italic: bool = True):
    """Custom font if given (and valid), else Arial in the requested style, else a fallback."""
    candidates = [path] if path else []
    candidates += ARIAL_FILES[(bold, italic)]
    for other in ((bold, False), (False, italic), (False, False)):
        candidates += ARIAL_FILES[other]
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def fmt_time(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def build_sheet(video: str, s: SheetSettings, progress, cancel: threading.Event) -> Image.Image:
    duration = probe_duration(video)
    if duration <= 0:
        raise RuntimeError("Could not read video duration")

    n = s.cols * s.rows
    b = s.border
    tile_w = max(16, (s.width - b * (s.cols + 1)) // s.cols)
    times = [duration * (i + 0.5) / n for i in range(n)]

    frames = [None] * n
    done = 0
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
        futs = {ex.submit(grab_frame, video, t, tile_w): i for i, t in enumerate(times)}
        for f in as_completed(futs):
            if cancel.is_set():
                for x in futs:
                    x.cancel()
                raise Cancelled()
            frames[futs[f]] = f.result()
            done += 1
            progress(done / n)

    first = next((f for f in frames if f is not None), None)
    if first is None:
        raise RuntimeError("FFmpeg could not extract any frames")
    tile_h = first.height

    font = load_font(s.font_size, s.font_path, s.bold, s.italic) if s.stamp else None
    pad = 8 + (s.stroke_width if s.stroke else 0)
    rem = s.width - (tile_w * s.cols + b * (s.cols + 1))
    x0 = b + rem // 2
    sheet = Image.new("RGB", (s.width, tile_h * s.rows + b * (s.rows + 1)), s.border_color)

    for i in range(n):
        r, c = divmod(i, s.cols)
        tile = frames[i] or Image.new("RGB", (tile_w, tile_h), "black")
        if tile.size != (tile_w, tile_h):
            tile = tile.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        if s.stamp:
            draw = ImageDraw.Draw(tile)
            sw = s.stroke_width if s.stroke else 0
            text = fmt_time(times[i])
            bb = draw.textbbox((0, 0), text, font=font, stroke_width=sw)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            pos = s.position.lower()
            if "left" in pos:
                x = pad - bb[0]
            elif "right" in pos:
                x = tile_w - tw - pad - bb[0]
            else:
                x = (tile_w - tw) // 2 - bb[0]
            if "top" in pos:
                y = pad - bb[1]
            elif "bottom" in pos:
                y = tile_h - th - pad - bb[1]
            else:
                y = (tile_h - th) // 2 - bb[1]
            draw.text((x, y), text, font=font, fill=s.text_color,
                      stroke_width=sw, stroke_fill=s.stroke_color)
        sheet.paste(tile, (x0 + c * (tile_w + b), b + r * (tile_h + b)))
    return sheet


def output_path(video: str, idx: int, total: int, s: SheetSettings) -> Path:
    p = Path(video)
    folder = Path(s.out_dir) if s.out_dir else p.parent
    base = p.stem
    if s.rename:
        name = re.sub(r'[<>:"/\\|?*]', "_", s.rename).strip()
        if name.lower().endswith(".png"):
            name = name[:-4]
        if name:
            base = f"{name}_{idx + 1}" if total > 1 else name
    return folder / f"{base}.png"


def make_chime_wav() -> str:
    """Generate a short three-note chime once and cache it in the temp folder."""
    path = os.path.join(tempfile.gettempdir(), "vps_done_chime.wav")
    if os.path.exists(path):
        return path
    rate = 44100
    frames = bytearray()
    for freq, dur in ((659.25, 0.15), (880.00, 0.15), (1318.51, 0.55)):
        for i in range(int(rate * dur)):
            t = i / rate
            env = min(1.0, i / (rate * 0.004)) * math.exp(-4.5 * t / dur)
            v = math.sin(2 * math.pi * freq * t) + 0.25 * math.sin(4 * math.pi * freq * t)
            frames += struct.pack("<h", int(v / 1.25 * env * 0.55 * 32767))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def reveal_in_folder(path: str):
    """Open the file's folder, selecting the file where the OS supports it."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
            return
    except Exception:
        pass
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


class Worker(QThread):
    item_status = Signal(str, str)
    item_done = Signal(str, bool, str)
    overall = Signal(int)
    all_done = Signal(bool)  # True if cancelled

    def __init__(self, jobs, settings):
        super().__init__()
        self.jobs = jobs
        self.settings = settings
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def run(self):
        total = len(self.jobs)
        for idx, path in enumerate(self.jobs):
            if self._cancel.is_set():
                break
            self.item_status.emit(path, "Processing…")

            def cb(frac, idx=idx):
                self.overall.emit(int((idx + frac) / total * 100))

            try:
                sheet = build_sheet(path, self.settings, cb, self._cancel)
                out = output_path(path, idx, total, self.settings)
                out.parent.mkdir(parents=True, exist_ok=True)
                sheet.save(out, "PNG")
                self.item_done.emit(path, True, str(out))
            except Cancelled:
                self.item_status.emit(path, "Cancelled")
                break
            except Exception as e:  # noqa: BLE001
                self.item_done.emit(path, False, str(e))
        cancelled = self._cancel.is_set()
        if not cancelled:
            self.overall.emit(100)
        self.all_done.emit(cancelled)


# ───────────────────────────── Theme ───────────────────────────────────────

DARK = dict(canvas="#010102", s1="#0f1011", s2="#141516", s3="#191a1b",
            hairline="#23252a", strong="#34343a", ink="#f7f8f8", muted="#d0d6e0",
            subtle="#8a8f98", tertiary="#62666d", primary="#5e6ad2",
            hover="#828fff", pressed="#5e69d1", box="#7a7f88")
LIGHT = dict(canvas="#f7f8f8", s1="#ffffff", s2="#f1f2f3", s3="#e6e8ea",
             hairline="#e1e3e6", strong="#c9ccd1", ink="#1c1d1f", muted="#3c4149",
             subtle="#6b7078", tertiary="#9a9fa6", primary="#5e6ad2",
             hover="#4f5bc0", pressed="#4650b0", box="#8a8f98")

QSS = Template("""
QWidget#root { background: $canvas; }
QFrame#card { background: $s1; border: 1px solid $hairline; border-radius: 12px; }
QLabel#appTitle { font-size: 22px; font-weight: 600; }
QLabel#appSub, QLabel#muted, QLabel#dzSub { color: $subtle; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; }
QLabel#dzTitle { font-size: 16px; font-weight: 600; }
QFrame#dropzone { background: $s1; border: 2px dashed $strong; border-radius: 16px; }
QFrame#dropzone[drag="true"], QFrame#dropzone:hover { border-color: $primary; background: $s2; }
QPushButton { background: $s1; color: $ink; border: 1px solid $hairline;
              border-radius: 8px; padding: 7px 14px; font-weight: 500; }
QPushButton:hover { background: $s2; border-color: $strong; }
QPushButton:pressed { background: $s3; }
QPushButton:disabled { color: $tertiary; }
QPushButton#primary { background: $primary; color: #ffffff; border: 1px solid $primary; padding: 11px 16px; }
QPushButton#primary:hover { background: $hover; border-color: $hover; }
QPushButton#primary:pressed { background: $pressed; border-color: $pressed; }
QPushButton#primary:disabled { background: $s3; color: $tertiary; border-color: $hairline; }
QCheckBox { spacing: 10px; font-weight: 500; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 5px;
                       border: 2px solid $box; background: $s2; }
QCheckBox::indicator:hover { border-color: $primary; }
QCheckBox::indicator:checked { background: $primary; border: 2px solid $primary; image: url("$check"); }
QCheckBox::indicator:checked:hover { background: $hover; border-color: $hover; }
QCheckBox::indicator:disabled { border-color: $hairline; background: $s1; }
QCheckBox::indicator:checked:disabled { background: $tertiary; border-color: $tertiary; }
QCheckBox:disabled { color: $tertiary; }
QComboBox { background: $s2; color: $ink; border: 1px solid $hairline; border-radius: 8px;
            padding: 6px 10px; min-width: 100px; }
QComboBox:hover { border-color: $strong; }
QComboBox:focus { border: 1px solid $primary; }
QComboBox:disabled { color: $tertiary; }
QComboBox::drop-down { border: none; width: 24px; }
QComboBox::down-arrow { image: url("$arrow"); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: $s3; color: $ink; border: 1px solid $strong;
                              selection-background-color: $primary; selection-color: #ffffff; outline: 0; }
QLineEdit { background: $s2; color: $ink; border: 1px solid $hairline; border-radius: 8px;
            padding: 6px 10px; selection-background-color: $primary; }
QLineEdit:focus { border: 1px solid $primary; }
QLineEdit:disabled { color: $tertiary; }
QListWidget { background: transparent; border: none; outline: 0; }
QListWidget::item { background: $s2; border: 1px solid $hairline; border-radius: 8px;
                    padding: 6px 10px; margin: 3px 0; color: $ink; }
QListWidget::item:selected { background: $s3; border: 1px solid $primary; color: $ink; }
QProgressBar { background: $s2; border: none; border-radius: 4px; max-height: 8px; color: transparent; }
QProgressBar::chunk { background: $primary; border-radius: 4px; }
QPlainTextEdit { background: $s2; border: 1px solid $hairline; border-radius: 8px; color: $muted;
                 padding: 6px; font-family: Consolas, Menlo, monospace; font-size: 12px; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: $strong; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: $s3; color: $ink; border: 1px solid $strong; padding: 4px; }
""")


def make_icons(t) -> dict:
    """Paint the checkbox tick and combo arrow to PNGs (QSS can only reference files)."""
    folder = tempfile.gettempdir()
    tag = t["canvas"].lstrip("#")

    def paint(name, draw):
        pm = QPixmap(18, 18)
        pm.fill(Qt.GlobalColor.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.RenderHint.Antialiasing)
        draw(qp)
        qp.end()
        path = os.path.join(folder, f"vps_{name}_{tag}.png")
        pm.save(path, "PNG")
        return path.replace("\\", "/")

    def tick(qp):
        pen = QPen(QColor("#ffffff"), 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        qp.setPen(pen)
        qp.drawPolyline([QPointF(3.5, 9.5), QPointF(7.5, 13.2), QPointF(14.5, 5.0)])

    def chevron(qp):
        pen = QPen(QColor(t["muted"]), 2.0)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        qp.setPen(pen)
        qp.drawPolyline([QPointF(4.0, 6.5), QPointF(9.0, 11.5), QPointF(14.0, 6.5)])

    return {"check": paint("check", tick), "arrow": paint("arrow", chevron)}


def make_palette(t) -> QPalette:
    p = QPalette()
    R = QPalette.ColorRole
    for role, key in ((R.Window, "canvas"), (R.WindowText, "ink"), (R.Base, "s2"),
                      (R.AlternateBase, "s1"), (R.Text, "ink"), (R.Button, "s1"),
                      (R.ButtonText, "ink"), (R.ToolTipBase, "s3"), (R.ToolTipText, "ink"),
                      (R.Highlight, "primary"), (R.PlaceholderText, "subtle"),
                      (R.Link, "primary")):
        p.setColor(role, QColor(t[key]))
    p.setColor(R.HighlightedText, QColor("#ffffff"))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["tertiary"]))
    return p


# ───────────────────────────── Widgets ─────────────────────────────────────

def paths_from_mime(md) -> list:
    out = []
    for u in md.urls():
        if not u.isLocalFile():
            continue
        p = Path(u.toLocalFile())
        if p.is_dir():
            out += [str(f) for f in sorted(p.rglob("*"))
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        elif p.is_file():
            out.append(str(p))
    return out


class DropZone(QFrame):
    dropped = Signal(list)
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(190)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Drop videos or folders here")
        title.setObjectName("dzTitle")
        sub = QLabel("or click to browse")
        sub.setObjectName("dzSub")
        for w in (title, sub):
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            lay.addWidget(w)

    def _drag(self, v):
        self.setProperty("drag", v)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag(True)

    def dragLeaveEvent(self, e):
        self._drag(False)

    def dropEvent(self, e):
        self._drag(False)
        e.acceptProposedAction()
        self.dropped.emit(paths_from_mime(e.mimeData()))


class ColorButton(QPushButton):
    changed = Signal()

    def __init__(self, color: str):
        super().__init__()
        self._c = QColor(color)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(120)
        self.clicked.connect(self._pick)
        self._refresh()

    def color(self) -> str:
        return self._c.name()

    def set_color(self, color: str):
        c = QColor(color)
        if c.isValid():
            self._c = c
            self._refresh()

    def _pick(self):
        c = QColorDialog.getColor(self._c, self, "Choose color")
        if c.isValid():
            self._c = c
            self._refresh()
            self.changed.emit()

    def _refresh(self):
        lum = 0.299 * self._c.red() + 0.587 * self._c.green() + 0.114 * self._c.blue()
        fg = "#000000" if lum > 150 else "#ffffff"
        self.setText(self._c.name().upper())
        self.setStyleSheet(
            f"QPushButton {{ background: {self._c.name()}; color: {fg}; "
            f"border: 1px solid rgba(128,128,128,0.55); border-radius: 8px; "
            f"padding: 6px 10px; font-weight: 500; }}"
            f"QPushButton:disabled {{ background: {self._c.name()}; color: {fg}; opacity: 0.5; }}"
        )


def spin(value, lo, hi, step=1, suffix=""):
    s = QSpinBox()
    s.setRange(lo, hi)
    s.setValue(value)
    s.setSingleStep(step)
    s.setSuffix(suffix)
    s.setMinimumWidth(120)
    return s


def card(title: str):
    frame = QFrame()
    frame.setObjectName("card")
    outer = QVBoxLayout(frame)
    outer.setContentsMargins(18, 16, 18, 16)
    outer.setSpacing(12)
    t = QLabel(title)
    t.setObjectName("cardTitle")
    outer.addWidget(t)
    grid = QGridLayout()
    grid.setHorizontalSpacing(14)
    grid.setVerticalSpacing(10)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(3, 1)
    outer.addLayout(grid)
    return frame, grid


def add_row(grid, row, text, widget):
    lbl = QLabel(text)
    grid.addWidget(lbl, row, 0)
    grid.addWidget(widget, row, 1)
    return lbl


def _to_bool(v, default):
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("true", "1", "yes")


def _to_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def add_pair(grid, row, col, text, widget):
    """Label + widget occupying two grid columns, for side-by-side options."""
    lbl = QLabel(text)
    grid.addWidget(lbl, row, col)
    grid.addWidget(widget, row, col + 1)
    return lbl


# ───────────────────────────── Main window ─────────────────────────────────

class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("root")
        self.setWindowTitle("Video Preview Sheet")
        self.setWindowIcon(QIcon(resource_path("app_icon.ico")))
        self.resize(1200, 930)
        self.setMinimumSize(980, 700)
        self.setAcceptDrops(True)

        self.settings_store = QSettings("VideoPreviewSheet", "App")
        self.dark = self.settings_store.value("theme", "dark") != "light"
        self.out_dir: Optional[str] = None
        self.font_path: Optional[str] = None
        self.outputs: list = []
        self._sfx = None
        self.worker: Optional[Worker] = None
        self.running = False

        self._build()
        self.apply_theme()
        QTimer.singleShot(0, self._check_ffmpeg)

    # ---- UI construction
    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(16)

        # header
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        t = QLabel("Video Preview Sheet")
        t.setObjectName("appTitle")
        sub = QLabel("Contact sheets from your videos, powered by FFmpeg")
        sub.setObjectName("appSub")
        titles.addWidget(t)
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch(1)
        self.theme_btn = QPushButton()
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.clicked.connect(self.toggle_theme)
        head.setSpacing(8)
        head.addWidget(self.theme_btn, alignment=Qt.AlignmentFlag.AlignTop)
        self.preset_btn = QPushButton("Save preset")
        self.preset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preset_btn.setToolTip("Save the current settings as your default.\n"
                                   "They'll be loaded automatically the next time you open the app.")
        self.preset_btn.clicked.connect(self._save_preset)
        head.addWidget(self.preset_btn, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(18)
        outer.addLayout(body, 1)

        # left: drop zone + queue
        left = QVBoxLayout()
        left.setSpacing(14)
        body.addLayout(left, 4)

        self.dropzone = DropZone()
        self.dropzone.dropped.connect(self.add_paths)
        self.dropzone.clicked.connect(self.browse)
        left.addWidget(self.dropzone)

        qcard = QFrame()
        qcard.setObjectName("card")
        ql = QVBoxLayout(qcard)
        ql.setContentsMargins(18, 16, 18, 16)
        ql.setSpacing(10)
        qh = QHBoxLayout()
        qt = QLabel("Queue")
        qt.setObjectName("cardTitle")
        self.count_lbl = QLabel("0 videos")
        self.count_lbl.setObjectName("muted")
        qh.addWidget(qt)
        qh.addStretch(1)
        qh.addWidget(self.count_lbl)
        ql.addLayout(qh)

        self.list = QListWidget()
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        ql.addWidget(self.list, 1)
        sc = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.list)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(self.remove_selected)

        qb = QHBoxLayout()
        self.remove_btn = QPushButton("Remove selected")
        self.clear_btn = QPushButton("Clear queue")
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn.clicked.connect(self.clear_queue)
        qb.addWidget(self.remove_btn)
        qb.addWidget(self.clear_btn)
        qb.addStretch(1)
        ql.addLayout(qb)

        self.auto_remove = QCheckBox("Automatically remove videos from the queue after generation")
        self.auto_remove.setChecked(True)
        ql.addWidget(self.auto_remove)
        left.addWidget(qcard, 1)

        # right: settings (scrollable) + run controls
        right = QVBoxLayout()
        right.setSpacing(12)
        body.addLayout(right, 5)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, 8, 0)
        il.setSpacing(12)
        scroll.setWidget(inner)
        right.addWidget(scroll, 1)

        # layout & border
        c, g = card("Layout & border")
        self.cols = spin(8, 1, 40)
        self.rows = spin(7, 1, 40)
        self.width_sp = spin(2000, 320, 16000, 100, " px")
        self.border = spin(4, 0, 200, 1, " px")
        self.border_color = ColorButton("#333333")
        add_pair(g, 0, 0, "Columns", self.cols)
        add_pair(g, 0, 2, "Rows", self.rows)
        add_pair(g, 1, 0, "Width", self.width_sp)
        add_pair(g, 1, 2, "Border", self.border)
        add_pair(g, 2, 0, "Border color", self.border_color)
        il.addWidget(c)

        # timestamps
        c, g = card("Timestamp overlay")
        self.stamp = QCheckBox("Burn-in timestamps")
        self.stamp.setChecked(True)
        self.pos_combo = QComboBox()
        self.pos_combo.addItems(POSITIONS)
        self.pos_combo.setCurrentText("Bottom right")
        self.l_pos = QLabel("Position")
        g.addWidget(self.stamp, 0, 0, 1, 2)
        g.addWidget(self.l_pos, 0, 2)
        g.addWidget(self.pos_combo, 0, 3)
        self.text_color = ColorButton("#ffffff")
        self.font_size = spin(20, 6, 400, 1, " px")
        self.l_text = add_pair(g, 1, 0, "Text color", self.text_color)
        self.l_size = add_pair(g, 1, 2, "Font size", self.font_size)
        self.l_font = QLabel("Font")
        self.font_btn = QPushButton(DEFAULT_FONT_LABEL)
        self.font_btn.setToolTip("Click to choose your own font file (.ttf / .otf)")
        self.font_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.font_btn.clicked.connect(self._choose_font)
        self.font_reset = QPushButton("Reset")
        self.font_reset.setToolTip("Back to Arial")
        self.font_reset.clicked.connect(self._reset_font)
        g.addWidget(self.l_font, 2, 0)
        g.addWidget(self.font_btn, 2, 1, 1, 2)
        g.addWidget(self.font_reset, 2, 3)
        self.bold = QCheckBox("Bold")
        self.bold.setChecked(True)
        self.italic = QCheckBox("Italic")
        self.italic.setChecked(True)
        self.stroke = QCheckBox("Enable stroke")
        self.stroke.setChecked(True)
        self.stroke_color = ColorButton("#000000")
        self.stroke_width = spin(2, 1, 40, 1, " px")
        g.addWidget(self.bold, 3, 0)
        g.addWidget(self.italic, 3, 1)
        g.addWidget(self.stroke, 3, 2, 1, 2)
        self.l_sc = add_pair(g, 4, 0, "Stroke color", self.stroke_color)
        self.l_sw = add_pair(g, 4, 2, "Stroke width", self.stroke_width)
        self.bold.toggled.connect(self._refresh_font_label)
        self.italic.toggled.connect(self._refresh_font_label)
        self.stamp.toggled.connect(self._update_enabled)
        self.stroke.toggled.connect(self._update_enabled)
        il.addWidget(c)

        # output
        c, g = card("Output")
        self.chk_out = QCheckBox("Specify custom output file location")
        self.out_btn = QPushButton("Choose folder…")
        self.out_lbl = QLabel("")
        self.out_lbl.setObjectName("muted")
        self.out_lbl.setWordWrap(True)
        self.chk_rename = QCheckBox("Choose custom rename")
        self.rename_edit = QLineEdit()
        self.rename_edit.setPlaceholderText("File name (no extension)")
        self.chk_sound = QCheckBox("Play a sound when finished")
        self.chk_sound.setChecked(True)
        g.addWidget(self.chk_out, 0, 0, 1, 3)
        g.addWidget(self.out_btn, 0, 3)
        g.addWidget(self.out_lbl, 1, 0, 1, 4)
        g.addWidget(self.chk_rename, 2, 0, 1, 2)
        g.addWidget(self.rename_edit, 2, 2, 1, 2)
        g.addWidget(self.chk_sound, 3, 0, 1, 4)
        self.chk_out.toggled.connect(self._out_toggled)
        self.out_btn.clicked.connect(self._choose_dir)
        self.chk_rename.toggled.connect(self._update_enabled)
        il.addWidget(c)
        il.addStretch(1)

        # run controls
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        right.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(110)
        self.log.setMaximumHeight(130)
        right.addWidget(self.log)

        btns = QHBoxLayout()
        self.gen_btn = QPushButton("Generate Video Preview Sheet")
        self.gen_btn.setObjectName("primary")
        self.gen_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.gen_btn.clicked.connect(self.generate)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        self.open_btn = QPushButton("Open in folder")
        self.open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_btn.clicked.connect(self.open_folder)
        self.open_btn.hide()
        btns.addWidget(self.gen_btn, 1)
        btns.addWidget(self.open_btn)
        btns.addWidget(self.cancel_btn)
        right.addLayout(btns)

        self._load_preset()
        self._update_enabled()
        self._refresh_font_label()
        self._refresh_out_label()
        self._update_count()

    # ---- theme
    def apply_theme(self):
        t = dict(DARK if self.dark else LIGHT)
        t.update(make_icons(t))
        app = QApplication.instance()
        app.setPalette(make_palette(t))
        app.setStyleSheet(QSS.substitute(t))
        self.theme_btn.setText("Light mode" if self.dark else "Dark mode")

    def toggle_theme(self):
        self.dark = not self.dark
        self.settings_store.setValue("theme", "dark" if self.dark else "light")
        self.apply_theme()

    # ---- enable/disable logic
    def _update_enabled(self):
        st = self.stamp.isChecked()
        sk = st and self.stroke.isChecked()
        for w in (self.text_color, self.font_size, self.l_text, self.l_size, self.stroke,
                  self.pos_combo, self.l_pos, self.l_font, self.font_btn, self.font_reset):
            w.setEnabled(st)
        for w in (self.stroke_color, self.stroke_width, self.l_sc, self.l_sw):
            w.setEnabled(sk)
        for w in (self.bold, self.italic):  # style comes from the file when a custom font is used
            w.setEnabled(st and self.font_path is None)
            w.setToolTip("" if self.font_path is None else "Style comes from the chosen font file")
        self.out_btn.setEnabled(self.chk_out.isChecked())
        self.rename_edit.setEnabled(self.chk_rename.isChecked())

    def _out_toggled(self, on):
        if on and not self.out_dir:
            if not self._choose_dir():
                self.chk_out.setChecked(False)
        self._refresh_out_label()
        self._update_enabled()

    def _choose_dir(self) -> bool:
        d = QFileDialog.getExistingDirectory(self, "Choose output folder", self.out_dir or "")
        if not d:
            return False
        self.out_dir = d
        self.chk_out.setChecked(True)
        self._refresh_out_label()
        return True

    def _refresh_out_label(self):
        if self.chk_out.isChecked() and self.out_dir:
            d = self.out_dir
            self.out_lbl.setText("Saving to: " + (d if len(d) <= 48 else "…" + d[-47:]))
            self.out_lbl.setToolTip(d)
        else:
            self.out_lbl.setText("Default: saved next to each video, same name, as .png")
            self.out_lbl.setToolTip("")

    # ---- presets
    def _save_preset(self):
        s = self.settings_store
        s.beginGroup("preset")
        for key, val in (
            ("cols", self.cols.value()), ("rows", self.rows.value()),
            ("width", self.width_sp.value()), ("border", self.border.value()),
            ("border_color", self.border_color.color()),
            ("stamp", self.stamp.isChecked()), ("position", self.pos_combo.currentText()),
            ("text_color", self.text_color.color()), ("font_size", self.font_size.value()),
            ("bold", self.bold.isChecked()), ("italic", self.italic.isChecked()),
            ("font_path", self.font_path or ""),
            ("stroke", self.stroke.isChecked()), ("stroke_color", self.stroke_color.color()),
            ("stroke_width", self.stroke_width.value()),
            ("custom_out", self.chk_out.isChecked()), ("out_dir", self.out_dir or ""),
            ("sound", self.chk_sound.isChecked()), ("auto_remove", self.auto_remove.isChecked()),
            ("saved", True),
        ):
            s.setValue(key, val)
        s.endGroup()
        s.sync()
        self._log("Preset saved. These settings will load automatically next time.")
        self.preset_btn.setText("Saved ✓")
        QTimer.singleShot(1600, lambda: self.preset_btn.setText("Save preset"))

    def _load_preset(self):
        s = self.settings_store
        if not _to_bool(s.value("preset/saved"), False):
            return

        def g(key):
            return s.value("preset/" + key)

        for spin_, key in ((self.cols, "cols"), (self.rows, "rows"), (self.width_sp, "width"),
                           (self.border, "border"), (self.font_size, "font_size"),
                           (self.stroke_width, "stroke_width")):
            spin_.setValue(_to_int(g(key), spin_.value()))
        for btn, key in ((self.border_color, "border_color"), (self.text_color, "text_color"),
                         (self.stroke_color, "stroke_color")):
            if g(key):
                btn.set_color(str(g(key)))
        for box, key in ((self.stamp, "stamp"), (self.bold, "bold"), (self.italic, "italic"),
                         (self.stroke, "stroke"), (self.chk_sound, "sound"),
                         (self.auto_remove, "auto_remove")):
            box.setChecked(_to_bool(g(key), box.isChecked()))
        pos = g("position")
        if pos in POSITIONS:
            self.pos_combo.setCurrentText(pos)
        fp = g("font_path")
        if fp and os.path.isfile(str(fp)):
            self.font_path = str(fp)
            name = Path(self.font_path).stem
            self.font_btn.setText(name if len(name) <= 26 else name[:25] + "…")
            self.font_btn.setToolTip(self.font_path)
        od = g("out_dir")
        if od and os.path.isdir(str(od)):
            self.out_dir = str(od)
            self.chk_out.setChecked(_to_bool(g("custom_out"), False))

    # ---- font
    def _choose_font(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Choose a font", self.font_path or "", "Fonts (*.ttf *.otf *.ttc);;All files (*)")
        if not f:
            return
        try:
            ImageFont.truetype(f, 12)
        except OSError:
            QMessageBox.warning(self, "Font not usable", "That file couldn't be loaded as a font.")
            return
        self.font_path = f
        name = Path(f).stem
        self.font_btn.setText(name if len(name) <= 26 else name[:25] + "…")
        self.font_btn.setToolTip(f)
        self._update_enabled()

    def _refresh_font_label(self):
        if self.font_path is None:
            parts = ["Arial"] + (["Bold"] if self.bold.isChecked() else []) \
                + (["Italic"] if self.italic.isChecked() else [])
            self.font_btn.setText(" ".join(parts))

    def _reset_font(self):
        self.font_path = None
        self._refresh_font_label()
        self.font_btn.setToolTip("Click to choose your own font file (.ttf / .otf)")
        self._update_enabled()

    # ---- queue
    def browse(self):
        exts = " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select videos", "", f"Video files ({exts});;All files (*)")
        if files:
            self.add_paths(files)

    def add_paths(self, paths):
        if self.running:
            return
        existing = {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())}
        added = 0
        for p in paths:
            if p in existing:
                continue
            existing.add(p)
            item = QListWidgetItem(f"{Path(p).name}\nQueued")
            item.setData(Qt.ItemDataRole.UserRole, p)
            item.setToolTip(p)
            item.setSizeHint(QSize(0, 54))
            self.list.addItem(item)
            added += 1
        self._update_count()
        if added:
            self._log(f"Added {added} video(s)")

    def _update_count(self):
        n = self.list.count()
        self.count_lbl.setText(f"{n} video{'s' if n != 1 else ''}")
        self.gen_btn.setEnabled(n > 0 and not self.running)

    def remove_selected(self):
        if self.running:
            return
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self._update_count()

    def clear_queue(self):
        if self.running:
            return
        self.list.clear()
        self._update_count()

    def _find(self, path):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == path:
                return i
        return -1

    def _set_status(self, path, status):
        i = self._find(path)
        if i >= 0:
            self.list.item(i).setText(f"{Path(path).name}\n{status}")

    # ---- drag & drop anywhere on the window
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        self.add_paths(paths_from_mime(e.mimeData()))
        e.acceptProposedAction()

    # ---- generation
    def _collect(self) -> SheetSettings:
        return SheetSettings(
            cols=self.cols.value(), rows=self.rows.value(), width=self.width_sp.value(),
            border=self.border.value(), border_color=self.border_color.color(),
            stamp=self.stamp.isChecked(), text_color=self.text_color.color(),
            font_size=self.font_size.value(), stroke=self.stroke.isChecked(),
            stroke_color=self.stroke_color.color(), stroke_width=self.stroke_width.value(),
            out_dir=self.out_dir if self.chk_out.isChecked() else None,
            rename=(self.rename_edit.text().strip() or None) if self.chk_rename.isChecked() else None,
            font_path=self.font_path, position=self.pos_combo.currentText(),
            bold=self.bold.isChecked(), italic=self.italic.isChecked(),
        )

    def generate(self):
        if not (FFMPEG and FFPROBE):
            self._check_ffmpeg()
            return
        paths = [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]
        if not paths:
            return
        for p in paths:
            self._set_status(p, "Queued")
        self.progress.setValue(0)
        self.outputs = []
        self._set_running(True)
        self.worker = Worker(paths, self._collect())
        self.worker.item_status.connect(self._set_status)
        self.worker.item_done.connect(self._item_done)
        self.worker.overall.connect(self.progress.setValue)
        self.worker.all_done.connect(self._all_done)
        self.worker.start()

    def _item_done(self, path, ok, msg):
        if ok:
            self._log(f"Saved: {msg}")
            self.outputs.append(msg)
            if self.auto_remove.isChecked():
                i = self._find(path)
                if i >= 0:
                    self.list.takeItem(i)
                self._update_count()
            else:
                self._set_status(path, "Done")
        else:
            self._set_status(path, "Failed")
            self._log(f"Failed: {Path(path).name} — {msg}")

    def _all_done(self, cancelled):
        self._set_running(False)
        self._log("Cancelled" if cancelled else "Finished")
        if self.outputs:
            self.open_btn.show()
            if not cancelled and self.chk_sound.isChecked():
                self._play_done_sound()

    def open_folder(self):
        seen = set()
        for f in self.outputs:
            d = str(Path(f).parent)
            if d not in seen and len(seen) < 5:
                seen.add(d)
                reveal_in_folder(f)

    def _play_done_sound(self):
        try:
            from PySide6.QtMultimedia import QSoundEffect
            if self._sfx is None:
                self._sfx = QSoundEffect(self)
                self._sfx.setSource(QUrl.fromLocalFile(make_chime_wav()))
                self._sfx.setVolume(0.7)
            self._sfx.play()
            return
        except Exception:
            pass
        try:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(make_chime_wav(), winsound.SND_FILENAME | winsound.SND_ASYNC)
                return
        except Exception:
            pass
        QApplication.beep()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)

    def _set_running(self, r):
        self.running = r
        self.cancel_btn.setVisible(r)
        if r:
            self.open_btn.hide()
        self.cancel_btn.setEnabled(True)
        for w in (self.remove_btn, self.clear_btn, self.dropzone):
            w.setEnabled(not r)
        self._update_count()

    def _log(self, msg):
        self.log.appendPlainText(msg)

    def _check_ffmpeg(self):
        if FFMPEG and FFPROBE:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("FFmpeg is required")
        box.setText("FFmpeg isn't installed (or isn't on your PATH).")
        box.setInformativeText(
            "This app needs ffmpeg and ffprobe to read your videos.\n\n"
            "Install it by running this in PowerShell or Command Prompt:\n\n"
            f"        {WINGET_CMD}\n\n"
            "Then close and reopen this app.")
        install = None
        if sys.platform == "win32":
            install = box.addButton("Install now", QMessageBox.ButtonRole.AcceptRole)
        copy = box.addButton("Copy command", QMessageBox.ButtonRole.ActionRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is copy:
            QApplication.clipboard().setText(WINGET_CMD)
        elif install is not None and clicked is install:
            subprocess.Popen(f'start "Install FFmpeg" cmd /k {WINGET_CMD}', shell=True)

    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(5000)
        e.accept()


def main():
    if sys.platform == "win32":  # makes the taskbar show our icon instead of python's
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(resource_path("app_icon.ico")))
    fams = set(QFontDatabase.families())
    for name in ("Inter", "SF Pro Display", "Segoe UI", "Helvetica Neue"):
        if name in fams:
            app.setFont(QFont(name, 10))
            break
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
