"""
Sottra — voice-to-text pill (PySide6 / Qt)
==========================================
UI tự vẽ bằng QPainter trên 1 mặt phẳng trong suốt -> bo tròn lozenge MƯỢT thật,
KHÔNG dính giới hạn WebView2 (không child-window, không viền trắng, không shadow lỗi).
Backend STT trong engine.py giữ nguyên.

Chạy:
    pip install -r requirements.txt
    python app_qt.py
"""

import sys
import os
import math
import ctypes
import tempfile
import traceback

# App windowed (PyInstaller) -> sys.stdout/stderr = None, print() sẽ hỏng + không thấy lỗi.
# Đổi hướng MỌI log + traceback ra file để chẩn đoán được.
_LOG = os.path.join(tempfile.gettempdir(), "sottra_qt.log")
try:
    _logf = open(_LOG, "w", encoding="utf-8", buffering=1)
    sys.stdout = _logf
    sys.stderr = _logf
    import faulthandler
    faulthandler.enable(file=_logf)
    sys.excepthook = lambda t, v, tb: traceback.print_exception(t, v, tb, file=_logf)
except Exception:
    pass

import ctypes.wintypes

from PySide6.QtCore import Qt, QTimer, QRectF, QByteArray, QObject, Signal, QFileInfo
from PySide6.QtGui import (
    QPainter, QColor, QLinearGradient, QPainterPath, QPen, QPixmap, QIcon,
    QGuiApplication,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QMenu, QFileIconProvider,
)
from PySide6.QtSvg import QSvgRenderer

from engine import SttEngine

# ---------- hằng số thiết kế ----------
WIN_W, WIN_H = 184, 52
MARGIN = 6                       # lề trong suốt quanh pill
N = 17                           # số thanh sóng
MIN_H, MAX_H = 2.0, 22.0
G1 = QColor(0xF6, 0xC4, 0x55)    # vàng sáng
G2 = QColor(0xE0, 0xA0, 0x30)    # vàng đậm
MUTED = QColor(0x8A, 0x8A, 0x93)
BG_TOP = QColor(0x17, 0x17, 0x1F)
BG_BOT = QColor(0x0C, 0x0C, 0x11)

MIC_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
    'stroke="#f6c455" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="9" y="2" width="6" height="11" rx="3"/>'
    '<path d="M5 10a7 7 0 0 0 14 0"/>'
    '<line x1="8" y1="21" x2="16" y2="21"/>'
    '<line x1="12" y1="17" x2="12" y2="21"/></svg>'
)

# Logo thương hiệu Sottra (waveform amber trên squircle tối) — dùng cho icon tray.
# Giữ đồng bộ với icon.svg / icon.ico ở thư mục gốc.
BRAND_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">'
    '<defs>'
    '<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#17171F"/><stop offset="1" stop-color="#0C0C11"/></linearGradient>'
    '<linearGradient id="amber" x1="0" y1="0" x2="0" y2="1">'
    '<stop offset="0" stop-color="#F6C455"/><stop offset="1" stop-color="#E0A030"/></linearGradient>'
    '</defs>'
    '<rect x="8" y="8" width="240" height="240" rx="60" fill="url(#bg)"/>'
    '<rect x="9.5" y="9.5" width="237" height="237" rx="58.5" fill="none" '
    'stroke="#F6C455" stroke-opacity="0.14" stroke-width="1.5"/>'
    '<g fill="url(#amber)">'
    '<rect x="45" y="96" width="22" height="64" rx="11"/>'
    '<rect x="81" y="76" width="22" height="104" rx="11"/>'
    '<rect x="117" y="53" width="22" height="150" rx="11"/>'
    '<rect x="153" y="76" width="22" height="104" rx="11"/>'
    '<rect x="189" y="96" width="22" height="64" rx="11"/>'
    '</g></svg>'
)


class Bridge(QObject):
    """Đưa sự kiện engine (chạy ở thread nền) về GUI thread an toàn."""
    sig = Signal(str, object)


class Pill(QWidget):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.state = "loading"
        self.level = 0.0
        self.smooth = 0.0
        self.t = 0.0
        self.spin = 0.0
        self.bars = [MIN_H] * N
        self.env = [math.sin(math.pi * (i + 0.5) / N) for i in range(N)]
        self._moved = False

        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
            | Qt.WindowDoesNotAcceptFocus          # không nhận keyboard focus
        )                                          # Tool -> KHÔNG hiện trên taskbar
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)  # hiện mà không activate
        self.setWindowTitle("Sottra")
        self.setFixedSize(WIN_W, WIN_H)
        self.mic = QSvgRenderer(QByteArray(MIC_SVG.encode("utf-8")))
        self.brand = QSvgRenderer(QByteArray(BRAND_SVG.encode("utf-8")))

        # Icon của app/website đang focus (thay cho mic). None -> fallback mic.
        self._iconprov = QFileIconProvider()
        self._icon_cache = {}          # exe path -> QPixmap | None
        self.app_icon = None           # QPixmap đang hiển thị
        self._cur_exe = None           # exe của foreground gần nhất

        self._build_tray()
        self._place_bottom_center()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(33)                       # ~30fps

        # Poll cửa sổ foreground để lấy icon app (tách khỏi vòng vẽ 30fps).
        self._detect_foreground_icon()
        self.detect_timer = QTimer(self)
        self.detect_timer.timeout.connect(self._detect_foreground_icon)
        self.detect_timer.start(500)

    # ---------------- vị trí ----------------
    def _place_bottom_center(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.center().x() - WIN_W // 2, g.bottom() - WIN_H - 80)

    def showEvent(self, e):
        super().showEvent(e)
        # WS_EX_NOACTIVATE: click vào pill KHÔNG cướp focus khỏi ô nhập
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            GWL_EXSTYLE, WS_EX_NOACTIVATE = -20, 0x08000000
            ex = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_NOACTIVATE)
        except Exception:
            pass

    # ---------------- tray ----------------
    def _make_icon(self):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.brand.render(p, QRectF(0, 0, 64, 64))
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self.menu = QMenu()
        self.menu.addAction("Bật / tắt nói", self.engine.toggle)
        self.menu.addAction("Hiện pill", self.show)
        self.menu.addSeparator()
        self.menu.addAction("Thoát", self._quit)
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("Sottra — voice to text")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.show()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:      # click trái tray -> bật/tắt nói
            self.engine.toggle()

    def _quit(self):
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()

    # ---------------- icon app đang focus ----------------
    def _detect_foreground_icon(self):
        """Lấy .exe của cửa sổ đang focus -> icon app. Fallback mic nếu fail."""
        try:
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            u.GetForegroundWindow.restype = ctypes.wintypes.HWND
            hwnd = u.GetForegroundWindow()
            if not hwnd or int(hwnd) == int(self.winId()):
                return                                  # không có / chính là pill
            pid = ctypes.wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k.OpenProcess.restype = ctypes.wintypes.HANDLE
            k.OpenProcess.argtypes = [ctypes.wintypes.DWORD,
                                      ctypes.wintypes.BOOL,
                                      ctypes.wintypes.DWORD]
            h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
            if not h:
                return                                  # process admin -> giữ icon cũ
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = ctypes.wintypes.DWORD(1024)
                ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                path = buf.value if ok else ""
            finally:
                k.CloseHandle(h)
            if not path or path == self._cur_exe:
                return                                  # chưa đổi app -> khỏi truy vấn icon
            self._cur_exe = path
            self.app_icon = self._icon_for(path)
        except Exception:
            pass

    def _icon_for(self, path):
        """QPixmap icon của 1 .exe (cache theo đường dẫn). None nếu không lấy được."""
        if path not in self._icon_cache:
            pm = None
            try:
                qi = self._iconprov.icon(QFileInfo(path))
                p = qi.pixmap(64, 64)
                if not p.isNull():
                    pm = p
            except Exception:
                pm = None
            self._icon_cache[path] = pm
        return self._icon_cache[path]

    # ---------------- sự kiện engine ----------------
    def on_event(self, ev, pl):
        if ev == "model":
            if pl == "loading":
                self.state = "loading"
        elif ev == "state":
            self.state = pl
        elif ev == "level":
            self.level = float(pl)
        # device/result/error: không hiển thị (chữ đã được gõ ra)

    # ---------------- animation ----------------
    def _tick(self):
        self.t += 0.08
        self.smooth += (self.level - self.smooth) * 0.4
        self.spin = (self.spin + 12) % 360
        for i in range(N):
            if self.state == "recording":
                n = 0.5 + 0.5 * abs(math.sin(self.t * 1.7 + i * 0.9)
                                    + 0.5 * math.sin(self.t * 2.9 + i * 1.7)) / 1.5
                target = MIN_H + self.smooth * (MAX_H - MIN_H) * self.env[i] * min(1.0, n)
            elif self.state == "transcribing":
                w = 0.5 + 0.5 * math.sin(self.t * 3 - i * 0.5)
                target = MIN_H + self.env[i] * 3 + w * 6
            else:
                target = MIN_H
            self.bars[i] += (target - self.bars[i]) * 0.45
        self.update()

    # ---------------- vẽ ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(MARGIN, MARGIN, self.width() - 2 * MARGIN, self.height() - 2 * MARGIN)
        radius = rect.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)

        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0.0, BG_TOP)
        grad.setColorAt(1.0, BG_BOT)
        p.fillPath(path, grad)

        if self.state == "recording":              # viền amber mảnh bên trong
            inner = QRectF(rect).adjusted(0.7, 0.7, -0.7, -0.7)
            ip = QPainterPath()
            ip.addRoundedRect(inner, radius, radius)
            p.setPen(QPen(QColor(246, 196, 85, 80), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(ip)

        cy = rect.center().y()
        icon = 17.0
        ix = rect.left() + 14
        # pulse nhẹ khi recording
        scale = 1.0 + (0.06 * math.sin(self.t * 3) if self.state == "recording" else 0.0)
        isz = icon * scale
        icon_rect = QRectF(ix + (icon - isz) / 2, cy - isz / 2, isz, isz)

        if self.state == "transcribing":           # spinner thay mic
            p.save()
            p.translate(icon_rect.center())
            p.rotate(self.spin)
            pen = QPen(G1, 2.0)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawArc(QRectF(-7, -7, 14, 14), 0, 270 * 16)
            p.restore()
        elif self.app_icon is not None:            # icon app đang focus
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            p.drawPixmap(icon_rect, self.app_icon, QRectF(self.app_icon.rect()))
        else:                                      # fallback: mic
            self.mic.render(p, icon_rect)

        wx0 = ix + icon + 12
        wx1 = rect.right() - 14
        if self.state in ("idle", "loading"):      # đường tĩnh lặng (gradient)
            lg = QLinearGradient(wx0, 0, wx1, 0)
            lg.setColorAt(0.0, QColor(246, 196, 85, 0))
            lg.setColorAt(0.5, QColor(246, 196, 85, 130))
            lg.setColorAt(1.0, QColor(246, 196, 85, 0))
            line = QRectF(wx0, cy - 1, wx1 - wx0, 2)
            lp = QPainterPath()
            lp.addRoundedRect(line, 1, 1)
            p.fillPath(lp, lg)
        else:                                      # sóng
            gap = 2.0
            bw = (wx1 - wx0 - gap * (N - 1)) / N
            for i in range(N):
                hh = max(MIN_H, self.bars[i])
                bx = wx0 + i * (bw + gap)
                br = QRectF(bx, cy - hh / 2, bw, hh)
                bg = QLinearGradient(0, cy - hh / 2, 0, cy + hh / 2)
                bg.setColorAt(0.0, G1)
                bg.setColorAt(1.0, G2)
                bp = QPainterPath()
                r = min(bw / 2, 2.0)
                bp.addRoundedRect(br, r, r)
                p.fillPath(bp, bg)
        p.end()

    # ---------------- chuột: click=toggle, kéo=di chuyển ----------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint()
            self._wpos = self.frameGeometry().topLeft()
            self._moved = False

    def mouseMoveEvent(self, e):
        if hasattr(self, "_press"):
            d = e.globalPosition().toPoint() - self._press
            if d.manhattanLength() > 4:
                self._moved = True
                self.move(self._wpos + d)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and not self._moved:
            self.engine.toggle()
        self._moved = False


def main():
    try:
        print("[main] start", flush=True)
        app = QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)       # tray giữ app sống khi ẩn pill
        bridge = Bridge()
        engine = SttEngine(lambda ev, pl=None: bridge.sig.emit(ev, pl))
        pill = Pill(engine)
        bridge.sig.connect(pill.on_event)
        pill.show()
        print(f"[main] window shown, hwnd={int(pill.winId())}", flush=True)
        engine.start()
        print("[main] engine started, entering loop", flush=True)
        sys.exit(app.exec())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
