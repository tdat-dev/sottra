"""
Sottra — desktop app (pywebview + faster-whisper)
=================================================
Pill nổi siêu nhẹ (WebView2 có sẵn trên Windows). Sống ở SYSTEM TRAY,
KHÔNG hiện trên taskbar. Pill chỉ hiện khi đang nói/dịch, còn lại tự ẩn.
UI trong ./web, backend STT trong engine.py.

Chạy:
    pip install -r requirements.txt
    python app.py
"""

import os
import sys
import json
import time
import ctypes
from ctypes import wintypes
import threading

# Console Windows mặc định cp1252 -> in tiếng Việt có thể UnicodeEncodeError.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import webview

from engine import SttEngine, HOTKEY_LABELS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(BASE_DIR, "web", "index.html")
LOG = os.path.join(BASE_DIR, "sottra.log")

WIN_W, WIN_H = 160, 44
TITLE = "Sottra"

_window = None
_engine = None
_hwnd = None
_tray_icon = None
_hide_timer = None

_user32 = ctypes.windll.user32
_SW_HIDE, _SW_SHOWNA = 0, 8       # SHOWNA = hiện mà KHÔNG cướp focus


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="")


# ----------------------- Cửa sổ: hiện/ẩn + ẩn taskbar -----------------------
def _find_hwnd():
    for _ in range(60):
        h = _user32.FindWindowW(None, TITLE)
        if h:
            return h
        time.sleep(0.05)
    return None


def _round_window(hwnd):
    """Bo góc bằng DWM (Win11) — clip ở tầng compositor nên ăn CẢ child WebView2.
    SetWindowRgn KHÔNG dùng được vì chỉ clip host, không clip child WebView2."""
    # DWMWA_WINDOW_CORNER_PREFERENCE=33, DWMWCP_ROUND=2
    try:
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
    except Exception as e:
        log(f"dwm round failed: {e}")


def _kill_frame(hwnd):
    """Bỏ drop shadow + viền Win11 của window -> hết 'khung mờ' dưới đáy pill."""
    try:                                    # bỏ CS_DROPSHADOW (bóng đổ window class)
        GCL_STYLE, CS_DROPSHADOW = -26, 0x00020000
        st = _user32.GetClassLongW(hwnd, GCL_STYLE)
        _user32.SetClassLongW(hwnd, GCL_STYLE, st & ~CS_DROPSHADOW)
    except Exception as e:
        log(f"shadow off failed: {e}")
    try:                                    # strip frame styles -> DWM không vẽ shadow nữa
        GWL_STYLE = -16
        WS_CAPTION = 0xC00000; WS_THICKFRAME = 0x40000
        WS_BORDER = 0x800000; WS_DLGFRAME = 0x400000
        s = _user32.GetWindowLongW(hwnd, GWL_STYLE)
        s &= ~(WS_CAPTION | WS_THICKFRAME | WS_BORDER | WS_DLGFRAME)
        _user32.SetWindowLongW(hwnd, GWL_STYLE, s)
    except Exception as e:
        log(f"style strip failed: {e}")
    try:                                    # Win11: viền none + không tự bo + TẮT shadow (NC render)
        dwm = ctypes.windll.dwmapi
        dwm.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(ctypes.c_uint(0xFFFFFFFE)), 4)  # BORDER_COLOR none
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(1)), 4)            # CORNER donotround
        dwm.DwmSetWindowAttribute(hwnd, 2,  ctypes.byref(ctypes.c_int(1)), 4)            # NCRENDERING_POLICY=DISABLED -> hết shadow
    except Exception as e:
        log(f"dwm failed: {e}")
    # flush thay đổi frame
    _user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)


def _hide_from_taskbar(hwnd):
    """WS_EX_TOOLWINDOW: bỏ khỏi taskbar (sống ở tray)."""
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW = -20, 0x80, 0x40000
    style = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    _user32.ShowWindow(hwnd, _SW_HIDE)
    _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    _user32.ShowWindow(hwnd, _SW_SHOWNA)


def _show_window():
    if _hwnd:
        _user32.ShowWindow(_hwnd, _SW_SHOWNA)


def _hide_window():
    if _hwnd:
        _user32.ShowWindow(_hwnd, _SW_HIDE)


def _cancel_hide():
    global _hide_timer
    if _hide_timer:
        _hide_timer.cancel()
        _hide_timer = None


def _schedule_hide(delay=1.5):
    global _hide_timer
    _cancel_hide()
    _hide_timer = threading.Timer(delay, _hide_window)
    _hide_timer.daemon = True
    _hide_timer.start()


# ----------------------- Sự kiện engine -> JS -----------------------
def emit(event, payload=None):
    if event != "level":
        log(f"EMIT {event} -> {payload}")
    if _window is None:
        return
    try:
        _window.evaluate_js(
            f"window.__emit({json.dumps(event)}, {json.dumps(payload)})"
        )
    except Exception as e:
        log(f"  evaluate_js FAILED: {e}")


# ----------------------- Tray icon -----------------------
def _make_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    a = (233, 185, 73, 255)
    d.rounded_rectangle([26, 12, 38, 34], radius=6, fill=a)   # thân mic
    d.arc([20, 20, 44, 44], start=0, end=180, fill=a, width=4)  # vành
    d.line([32, 44, 32, 52], fill=a, width=4)                 # chân
    d.line([24, 52, 40, 52], fill=a, width=4)                 # đế
    return img


def _tray_thread():
    import pystray
    from pystray import MenuItem as Item, Menu
    menu = Menu(
        Item("Bật / tắt nói", lambda i, it: _engine.toggle(), default=True),
        Item("Hiện pill", lambda i, it: _show_window()),
        Item("Thoát", lambda i, it: _quit()),
    )
    global _tray_icon
    _tray_icon = pystray.Icon("sottra", _make_icon_image(), "Sottra — voice to text", menu)
    _tray_icon.run()


def _quit():
    _cancel_hide()
    try:
        if _tray_icon:
            _tray_icon.stop()
    except Exception:
        pass
    if _engine:
        _engine.shutdown()
    if _window:
        try:
            _window.destroy()
        except Exception:
            pass


# ----------------------- JS API -----------------------
class Api:
    def ready(self):
        emit("hotkeys", HOTKEY_LABELS)
        emit("config", {
            "output_mode": _engine.output_mode,
            "hotkey": _engine.hotkey_name,
            "model": _engine.model_size,
        })
        _engine.start()
        return True

    def toggle(self):
        _engine.toggle()

    def set_output_mode(self, mode):
        _engine.set_output_mode(mode)
        return mode

    def set_hotkey(self, name):
        _engine.set_hotkey(name)
        return name

    def set_model(self, size):
        _engine.reload_model(size)
        return size

    def jslog(self, msg):
        log(f"  JS-GOT {msg}")

    def close(self):
        _quit()


# ----------------------- Khởi động -----------------------
def _reposition():
    """Đáy-giữa màn hình CHÍNH. Defer để thắng việc pywebview tự đặt vị trí."""
    if not _hwnd:
        return
    r = wintypes.RECT()
    _user32.GetWindowRect(_hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    pw, ph = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    x = (pw - w) // 2
    y = ph - h - 96
    ok = _user32.SetWindowPos(_hwnd, 0, x, y, 0, 0, 0x0001 | 0x0004 | 0x0010)  # NOSIZE|NOZORDER|NOACTIVATE
    log(f"reposition -> {x},{y} (was {r.left},{r.top}) ok={ok}")


def _on_start(window=None):
    global _hwnd
    _hwnd = _find_hwnd()
    if _hwnd:
        try:
            _reposition()
            threading.Timer(0.7, _reposition).start()   # lặp lại sau khi pywebview xong
        except Exception as e:
            log(f"position failed: {e}")
        try:
            _hide_from_taskbar(_hwnd)
        except Exception as e:
            log(f"toolwindow failed: {e}")
        try:
            _round_window(_hwnd)
        except Exception as e:
            log(f"round failed: {e}")
    threading.Thread(target=_tray_thread, daemon=True).start()


def main():
    global _window, _engine
    _engine = SttEngine(emit)
    _window = webview.create_window(
        TITLE,
        INDEX,
        js_api=Api(),
        width=WIN_W,
        height=WIN_H,
        min_size=(110, 34),
        resizable=False,
        frameless=True,
        easy_drag=True,
        on_top=True,
        shadow=False,                 # TẮT shadow pywebview -> hết 'khung mờ' dưới pill
        background_color="#0c0c11",   # trùng đáy gradient pill
    )
    webview.start(_on_start, _window)


if __name__ == "__main__":
    main()
