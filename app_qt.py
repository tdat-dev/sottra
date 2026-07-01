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
import time
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
import threading

from version import __version__
import updater
import install
import config

from PySide6.QtCore import (
    Qt, QTimer, QRect, QRectF, QByteArray, QObject, Signal, QFileInfo, QSize,
)
from PySide6.QtGui import (
    QPainter, QColor, QLinearGradient, QPainterPath, QPen, QPixmap, QIcon,
    QGuiApplication, QRegion, QFont,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QSystemTrayIcon, QMenu, QFileIconProvider,
)
from PySide6.QtSvg import QSvgRenderer

from engine import SttEngine

# ---------- hằng số thiết kế ----------
# Canvas trong suốt cố định; capsule tự nở/thu bên trong (không resize cửa sổ -> không giật).
# Lúc rảnh = chấm tròn tí hon (gọn). Lúc thu âm = pill dài có sóng âm + đồng hồ (dễ nhận ra).
WIN_W, WIN_H = 208, 54            # đủ chỗ pill nở hết cỡ + quầng sáng
CAP_H = 38                       # cao capsule = đường kính chấm lúc nghỉ
DOT_W = CAP_H                    # rộng lúc nghỉ (chấm tròn)
PILL_W = 182                     # rộng lúc thu âm / đang chép
WAVE_N = 15                      # số cột sóng âm
MARGIN = 4
ICON_PX = 16                     # cạnh icon (mic / app) vẽ trong chấm, đơn vị logical
AMBER = QColor(0xF6, 0xC4, 0x55)  # màu thương hiệu chủ đạo
G1 = QColor(0xF6, 0xC4, 0x55)    # amber sáng
G2 = QColor(0xE0, 0xA0, 0x30)    # amber đậm
BG_TOP = QColor(0x17, 0x17, 0x1F)
BG_BOT = QColor(0x0C, 0x0C, 0x11)


def _amber(a):
    """QColor amber với alpha (0..255) — gọn khi vẽ nhiều lớp."""
    return QColor(0xF6, 0xC4, 0x55, max(0, min(255, int(a))))

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


class UpdateSignals(QObject):
    """Đưa kết quả check/tải cập nhật (thread nền) về GUI thread."""
    available = Signal(object)     # info dict {version, manifest_url, html_url}
    progress = Signal(int, str)    # phần trăm, path đang tải
    done = Signal()
    failed = Signal(str)           # thông điệp lỗi ("__uptodate__" = đã mới nhất)


class Pill(QWidget):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.state = "loading"
        self.level = 0.0
        self.smooth = 0.0
        self.t = 0.0
        self.spin = 0.0
        self._moved = False

        # Nở/thu: 0 = chấm, 1 = pill dài. Sóng âm cuộn theo mức âm gần đây.
        self.open = 0.0
        self.open_target = 0.0
        self.wave = [0.0] * WAVE_N
        self._rec_start = 0.0
        self._last_w = -1.0

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

        # Auto-update: chỉ khi chạy bản đóng gói (frozen). Check nền lúc khởi động.
        self._update_info = None
        self._usig = UpdateSignals()
        self._usig.available.connect(self._on_update_available)
        self._usig.progress.connect(self._on_update_progress)
        self._usig.done.connect(self._on_update_done)
        self._usig.failed.connect(self._on_update_failed)
        if updater.is_frozen():
            threading.Thread(target=self._check_updates_bg, daemon=True).start()

        # Lần đầu (bản đóng gói): tạo lối tắt + bật chạy cùng Windows -> khỏi vào folder.
        if install.is_frozen():
            cfg = config.load()
            if not cfg.get("installed"):
                made = install.create_shortcuts()
                install.set_startup(True)
                if hasattr(self, "startup_action"):
                    self.startup_action.setChecked(install.startup_enabled())
                cfg["installed"] = True
                config.save(cfg)
                QTimer.singleShot(1800, lambda: self._notify(
                    f"Đã tạo {len(made)} lối tắt & bật khởi động cùng Windows. "
                    "Tắt trong menu khay nếu không muốn.", 7000))

        # Lần đầu chưa có Groq key -> nhắc (app cloud-only, cần key mới nói được)
        if not self.engine.groq_api_key:
            QTimer.singleShot(1500, lambda: self._notify(
                "Cần Groq API key (miễn phí ở console.groq.com). "
                "Chuột phải icon khay → Nhập Groq API key.", 8000))

    # ---------------- vị trí ----------------
    def _place_bottom_center(self):
        g = QGuiApplication.primaryScreen().availableGeometry()
        self.move(g.center().x() - WIN_W // 2, g.bottom() - WIN_H - 64)
        self._sync_mask(force=True)

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
        header = self.menu.addAction(f"Sottra v{__version__}")
        header.setEnabled(False)
        self.menu.addSeparator()
        self.menu.addAction("Bật / tắt nói", self.engine.toggle)
        self.menu.addAction("Hiện pill", self.show)
        self.menu.addSeparator()

        # Chất lượng nhận dạng: large-v3 (chuẩn hơn) hoặc turbo (nhanh hơn)
        q = self.menu.addMenu("Chất lượng nhận dạng")
        self.act_accurate = q.addAction(
            "Chính xác · large-v3", lambda: self._set_model("whisper-large-v3"))
        self.act_fast = q.addAction(
            "Nhanh · turbo", lambda: self._set_model("whisper-large-v3-turbo"))
        self.act_accurate.setCheckable(True)
        self.act_fast.setCheckable(True)
        self._refresh_model_menu()

        # Dọn dấu/chính tả tiếng Việt bằng LLM sau khi chép (+~0.5s)
        self.refine_action = self.menu.addAction(
            "Dọn chính tả bằng AI", self._toggle_refine)
        self.refine_action.setCheckable(True)
        self.refine_action.setChecked(self.engine.refine)

        # Nhận dạng qua Groq (đám mây) -> cần API key (miễn phí ở console.groq.com)
        self.key_action = self.menu.addAction("Nhập Groq API key…", self._enter_groq_key)
        self._refresh_key_action()
        self.menu.addSeparator()

        # Tích hợp Windows (chỉ ở bản đóng gói): startup + lối tắt
        if install.is_frozen():
            self.startup_action = self.menu.addAction(
                "Khởi động cùng Windows", self._toggle_startup)
            self.startup_action.setCheckable(True)
            self.startup_action.setChecked(install.startup_enabled())
            self.menu.addAction("Tạo lối tắt (Start Menu + Desktop)",
                                self._make_shortcuts)
            self.menu.addSeparator()

        # Mục "Cập nhật lên vX…" ẩn cho tới khi phát hiện bản mới.
        self.update_action = self.menu.addAction("Cập nhật…", self._do_update)
        self.update_action.setVisible(False)
        self.check_action = self.menu.addAction("Kiểm tra cập nhật", self._manual_check)
        if not updater.is_frozen():
            self.check_action.setVisible(False)   # dev mode: không có gì để thay
        self.menu.addSeparator()
        self.menu.addAction("Thoát", self._quit)
        self.tray = QSystemTrayIcon(self._make_icon(), self)
        self.tray.setToolTip("Sottra — voice to text")
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.messageClicked.connect(self._on_balloon_clicked)
        self.tray.show()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.Trigger:      # click trái tray -> bật/tắt nói
            self.engine.toggle()

    # ---------------- chất lượng nhận dạng (model Groq) ----------------
    def _refresh_model_menu(self):
        turbo = self.engine.groq_model.endswith("turbo")
        self.act_accurate.setChecked(not turbo)
        self.act_fast.setChecked(turbo)

    def _set_model(self, model):
        self.engine.set_groq_model(model)
        self._refresh_model_menu()
        self._notify("Nhận dạng: "
                     + ("Chính xác (large-v3)" if not model.endswith("turbo")
                        else "Nhanh (turbo)"), 2500)

    def _toggle_refine(self):
        on = self.refine_action.isChecked()
        self.engine.set_refine(on)
        self._notify("Bật dọn chính tả bằng AI (+~0.5s)." if on
                     else "Tắt dọn chính tả bằng AI.", 2500)

    # ---------------- Groq API key ----------------
    def _refresh_key_action(self):
        has = bool(self.engine.groq_api_key)
        self.key_action.setText(
            "Đổi Groq API key…" if has else "Nhập Groq API key…  (cần thiết)")

    # ---------------- tích hợp Windows ----------------
    def _toggle_startup(self):
        on = self.startup_action.isChecked()
        if install.set_startup(on):
            self._notify("Đã bật khởi động cùng Windows." if on
                         else "Đã tắt khởi động cùng Windows.", 2500)
        else:
            self.startup_action.setChecked(not on)     # trả lại trạng thái nếu ghi lỗi
            self._notify("Không đổi được cài đặt khởi động.", 3000)

    def _make_shortcuts(self):
        made = install.create_shortcuts()
        self._notify(f"Đã tạo {len(made)} lối tắt (Start Menu + Desktop)." if made
                     else "Không tạo được lối tắt.", 3000)

    def _enter_groq_key(self):
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        key, ok = QInputDialog.getText(
            None, "Groq API key",
            "Dán API key (miễn phí ở console.groq.com):",
            QLineEdit.Normal, self.engine.groq_api_key)
        if not ok:
            return
        self.engine.set_groq_key(key)
        self._refresh_key_action()
        if self.engine.groq_api_key:
            self._notify("Đã lưu Groq API key.", 2500)

    def _quit(self):
        try:
            self.engine.shutdown()
        except Exception:
            pass
        self.tray.hide()
        QApplication.quit()

    # ---------------- auto-update ----------------
    def _notify(self, msg, ms=6000):
        self.tray.showMessage("Sottra", msg,
                              QSystemTrayIcon.MessageIcon.Information, ms)

    def _check_updates_bg(self):
        info = updater.check_latest()
        if info:
            self._usig.available.emit(info)

    def _manual_check(self):
        self._notify("Đang kiểm tra cập nhật…", 2500)

        def work():
            info = updater.check_latest()
            if info:
                self._usig.available.emit(info)
            else:
                self._usig.failed.emit("__uptodate__")
        threading.Thread(target=work, daemon=True).start()

    def _on_update_available(self, info):
        self._update_info = info
        self.update_action.setText(f"Cập nhật lên v{info['version']}…")
        self.update_action.setVisible(True)
        self._notify(f"Có bản mới v{info['version']} — bấm để cập nhật.")

    def _on_balloon_clicked(self):
        if self._update_info:
            self._do_update()

    def _do_update(self):
        if not self._update_info:
            return
        self.update_action.setEnabled(False)
        self._notify("Bắt đầu tải bản cập nhật…", 2500)
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self):
        try:
            manifest = updater._get_json(self._update_info["manifest_url"])
            staging, _delete = updater.stage_update(
                manifest,
                progress_cb=lambda p, rel: self._usig.progress.emit(p, rel),
            )
            updater.write_helper_and_spawn(staging)
            self._usig.done.emit()
        except Exception as e:
            self._usig.failed.emit(str(e))

    def _on_update_progress(self, pct, rel):
        self.tray.setToolTip(f"Sottra — đang tải cập nhật… {pct}%")

    def _on_update_done(self):
        self._notify("Đã tải xong — đang khởi động lại để cập nhật…", 3000)
        self._quit()

    def _on_update_failed(self, msg):
        if msg == "__uptodate__":
            self._notify("Bạn đang dùng bản mới nhất.", 3000)
            return
        self.update_action.setEnabled(True)
        self.tray.setToolTip("Sottra — voice to text")
        if self._update_info:
            self._notify(f"Cập nhật lỗi: {msg}. Mở trang tải…", 6000)
            try:
                import webbrowser
                webbrowser.open(self._update_info.get("html_url", ""))
            except Exception:
                pass

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
            self.update()        # repaint 1 lần để đổi icon kể cả khi timer đã dừng (idle)
        except Exception:
            pass

    def _icon_for(self, path):
        """QPixmap icon của 1 .exe, scale sắc nét đúng kích thước vẽ (cache theo path).

        Lấy variant gốc lớn nhất (shell cấp tới 128px) rồi thu nhỏ MỘT lần về
        kích thước hiển thị thật (ICON_PX × devicePixelRatio) bằng SmoothTransformation
        -> tránh phóng-rồi-thu 2 lần làm icon vỡ. None nếu không lấy được.
        """
        if path not in self._icon_cache:
            pm = None
            try:
                dpr = self.devicePixelRatioF() or 1.0
                target = max(1, round(ICON_PX * dpr))
                big = self._iconprov.icon(QFileInfo(path)).pixmap(QSize(128, 128))
                if not big.isNull():
                    pm = big.scaled(target, target, Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)
                    pm.setDevicePixelRatio(dpr)
            except Exception:
                pm = None
            self._icon_cache[path] = pm
        return self._icon_cache[path]

    # ---------------- sự kiện engine ----------------
    def on_event(self, ev, pl):
        if ev == "model":
            # Không đè recording/transcribing khi model nạp lại nền sau lúc nhả VRAM
            if pl == "loading" and self.state not in ("recording", "transcribing"):
                self.state = "loading"
                self.open_target = 0.0
                self._ensure_animating()
            elif pl == "ready" and self.state == "loading":
                self.state = "idle"
                self.open_target = 0.0
                self._ensure_animating()
        elif ev == "state":
            self.state = pl
            if pl == "recording":
                self._rec_start = time.monotonic()
                self.wave = [0.0] * WAVE_N          # sóng bắt đầu phẳng
                self.open_target = 1.0              # nở thành pill -> rõ "đang nghe"
            elif pl == "transcribing":
                self.open_target = 1.0              # giữ pill -> rõ "đang chép"
            else:                                   # idle -> thu về chấm
                self.open_target = 0.0
            self._ensure_animating()
        elif ev == "level":
            self.level = float(pl)
        # device/result/error: không hiển thị (chữ đã được gõ ra)

    def _ensure_animating(self):
        """Bật lại vòng vẽ 30fps khi có hoạt động (lúc rảnh _tick tự dừng timer)."""
        if not self.timer.isActive():
            self.timer.start(33)

    # ---------------- animation ----------------
    def _tick(self):
        self.t += 0.08
        self.smooth += (self.level - self.smooth) * 0.4    # mức âm mượt
        self.spin = (self.spin + 12) % 360                 # góc spinner
        self.open += (self.open_target - self.open) * 0.30  # nở/thu mượt
        if abs(self.open_target - self.open) < 0.002:
            self.open = self.open_target
        # Đẩy 1 mẫu sóng mỗi khung khi đang thu -> dải cột cuộn như máy đo giọng
        if self.state == "recording":
            self.wave.append(min(1.0, self.smooth))
            self.wave.pop(0)
        self._sync_mask()                                  # cập nhật vùng bắt chuột theo bề rộng
        self.update()
        # Rảnh + đã thu về chấm + viền lặng -> dừng vẽ (khỏi composite GPU 30fps nền liên tục).
        # Khi recording/transcribing trở lại, on_event gọi _ensure_animating() bật lại.
        if (self.state not in ("recording", "transcribing")
                and self.open < 0.01 and self.smooth < 0.01):
            self.open = 0.0
            self.timer.stop()

    # ---------------- hình học capsule + mặt nạ chuột ----------------
    def _capsule_w(self):
        """Bề rộng capsule hiện tại (px). easeOutCubic: nở dứt khoát, thu mượt."""
        e = max(0.0, min(1.0, self.open))
        e = 1.0 - (1.0 - e) ** 3
        return DOT_W + (PILL_W - DOT_W) * e

    def _sync_mask(self, force=False):
        """Chỉ để capsule (không phải cả canvas trong suốt) bắt chuột -> phần còn lại
        cho click xuyên xuống app phía dưới. Cập nhật khi bề rộng đổi."""
        w = self._capsule_w()
        if not force and abs(w - self._last_w) < 0.5:
            return
        self._last_w = w
        cy = self.height() / 2.0
        left = (self.width() - w) / 2.0
        pad = 8                                            # chừa quầng sáng ngoài viền
        rect = QRect(
            int(left - pad), int(cy - CAP_H / 2.0 - pad),
            int(w + 2 * pad), int(CAP_H + 2 * pad),
        )
        self.setMask(QRegion(rect))

    # ---------------- vẽ (chấm nở thành pill sóng âm) ----------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)

        w = self._capsule_w()
        cy = self.height() / 2.0
        left = (self.width() - w) / 2.0
        rad = CAP_H / 2.0
        cap = QRectF(left, cy - rad, w, CAP_H)
        path = QPainterPath()
        path.addRoundedRect(cap, rad, rad)

        # Nền tối (gradient thương hiệu) + hairline amber luôn có -> "có hồn" lúc đứng yên
        grad = QLinearGradient(cap.topLeft(), cap.bottomLeft())
        grad.setColorAt(0.0, BG_TOP)
        grad.setColorAt(1.0, BG_BOT)
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawPath(path)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_amber(40), 1.0))
        p.drawPath(path)

        # Độ hiện của nội dung bên trong pill (sóng/đồng hồ/chấm nghĩ) theo mức nở
        content = max(0.0, min(1.0, (self.open - 0.35) / 0.5))
        icon_cx = left + rad                               # icon trượt về nắp trái khi nở

        if self.state == "recording":
            self._paint_record_ring(p, cap, rad)
            if content > 0.02:
                self._paint_wave(p, left, w, cy, content)
                self._paint_timer(p, left, w, cy, content)
        elif self.state == "transcribing":
            if content > 0.02:
                self._paint_thinking(p, left, w, cy, content)
            else:
                self._paint_spinner(p, cap, rad)
        elif self.state == "loading":
            self._paint_spinner(p, cap, rad)

        self._paint_icon(p, icon_cx, cy)
        p.end()

    def _paint_record_ring(self, p, cap, rad):
        """Viền amber dày/đậm theo mức âm -> phản hồi tức thì khi nói + quầng khi to tiếng."""
        lvl = min(1.0, self.smooth)
        alpha = int(120 + 135 * min(1.0, self.smooth * 1.3))
        width = 1.6 + 2.6 * lvl
        p.setBrush(Qt.NoBrush)
        inner = cap.adjusted(width / 2, width / 2, -width / 2, -width / 2)
        ipath = QPainterPath()
        ir = max(1.0, rad - width / 2)
        ipath.addRoundedRect(inner, ir, ir)
        p.setPen(QPen(_amber(alpha), width))
        p.drawPath(ipath)
        if lvl > 0.05:                                     # quầng mờ ngoài
            p.setPen(QPen(_amber(int(52 * lvl)), width + 3))
            opath = QPainterPath()
            opath.addRoundedRect(cap, rad, rad)
            p.drawPath(opath)

    def _paint_wave(self, p, left, w, cy, a):
        """Dải cột sóng âm cuộn (giữa icon và đồng hồ) — dấu hiệu 'đang nghe' rõ nhất."""
        x0 = left + CAP_H + 2                              # sau nắp icon
        x1 = left + w - 40                                 # trước đồng hồ
        if x1 - x0 < 10:
            return
        n = len(self.wave)
        step = (x1 - x0) / n
        bw = min(3.4, step * 0.6)
        maxh = CAP_H * 0.66
        p.setPen(Qt.NoPen)
        p.setBrush(_amber(int(235 * a)))
        for i, v in enumerate(self.wave):
            h = 3.0 + max(0.0, min(1.0, v)) * maxh
            x = x0 + step * i + (step - bw) / 2
            p.drawRoundedRect(QRectF(x, cy - h / 2, bw, h), bw / 2, bw / 2)

    def _paint_timer(self, p, left, w, cy, a):
        """Đồng hồ đếm giây bên phải -> thấy nó chạy = biết chắc đang thu, chưa dừng."""
        secs = max(0, int(time.monotonic() - self._rec_start))
        txt = f"{secs // 60}:{secs % 60:02d}"
        f = QFont(); f.setPixelSize(12)
        p.setFont(f)
        p.setPen(_amber(int(225 * a)))
        p.drawText(QRectF(left + w - 40, cy - 9, 32, 18),
                   Qt.AlignVCenter | Qt.AlignRight, txt)

    def _paint_thinking(self, p, left, w, cy, a):
        """Ba chấm nảy sóng — 'đang chép chữ', khác hẳn lúc nghe (không có cột sóng)."""
        x0 = left + CAP_H + 2
        x1 = left + w - 18
        mid = (x0 + x1) / 2.0
        p.setPen(Qt.NoPen)
        for i in range(3):
            ph = math.sin(self.t * 3.0 - i * 0.7)
            off = max(0.0, ph) * 5.0
            p.setBrush(_amber(int((140 + 95 * max(0.0, ph)) * a)))
            p.drawEllipse(QRectF(mid - 12 + i * 12 - 3, cy - 3 - off, 6, 6))

    def _paint_spinner(self, p, cap, rad):
        """Vòng cung xoay quanh chấm — lúc nạp model / bắt đầu chép (khi pill còn nhỏ)."""
        cx, cy = cap.center().x(), cap.center().y()
        p.save()
        p.translate(cx, cy)
        p.rotate(self.spin)
        pen = QPen(G1, 2.2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        rr = rad - 1.8
        p.drawArc(QRectF(-rr, -rr, 2 * rr, 2 * rr), 0, 270 * 16)
        p.restore()

    def _paint_icon(self, p, cx, cy):
        """Icon app đang focus (hoặc mic) — pulse nhẹ khi thu, trượt về nắp trái khi nở."""
        scale = 1.0 + (0.06 * math.sin(self.t * 3) if self.state == "recording" else 0.0)
        isz = ICON_PX * scale
        rect = QRectF(cx - isz / 2, cy - isz / 2, isz, isz)
        if self.app_icon is not None:
            pm = self.app_icon
            p.drawPixmap(rect, pm, QRectF(0, 0, pm.width(), pm.height()))
        else:
            self.mic.render(p, rect)

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
