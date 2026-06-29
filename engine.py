"""
Sottra — STT engine (offline, local-only)
=========================================
Push-to-talk speech-to-text. Giữ phím -> thu âm RAM (16kHz) -> faster-whisper
-> gõ tại con trỏ HOẶC copy clipboard. KHÔNG cloud, KHÔNG LLM API.

Engine tách rời UI: phát sự kiện qua callback `emit(event, payload)`:
    emit("model",  "loading" | "ready")
    emit("state",  "idle" | "recording" | "transcribing")
    emit("level",  float 0..1)              # mức âm thanh realtime
    emit("result", {"text": str, "time": "HH:MM"})
    emit("error",  str)
    emit("device", "CPU·int8" | "GPU·float16")
"""

import sys
import time
import queue
import threading

import numpy as np
import sounddevice as sd
from pynput import keyboard

SAMPLE_RATE = 16000
CHANNELS = 1

# Câu Whisper hay "ảo giác" trên tiếng Việt (outro YouTube) / tiếng video lọt mic -> chặn
_HALLUC_PHRASES = (
    "ghiền mì gõ", "ghien mi go", "đăng ký kênh", "dang ky kenh",
    "subscribe", "ủng hộ kênh", "cảm ơn các bạn đã theo dõi",
    "cảm ơn các bạn đã lắng nghe", "hẹn gặp lại", "đừng quên",
    "để không bỏ lỡ những video", "like và đăng ký", "cảm ơn đã xem",
)


def _is_hallucination(text):
    """True nếu text chỉ là câu outro YouTube quen thuộc (ảo giác / tiếng video lọt mic)."""
    t = text.lower().strip()
    if len(t) > 90:                      # câu dài thì coi như nói thật, cho qua
        return False
    return any(p in t for p in _HALLUC_PHRASES)

# Tên hotkey người dùng chọn -> pynput Key
HOTKEYS = {
    "alt_r": keyboard.Key.alt_r,
    "alt_l": keyboard.Key.alt_l,
    "ctrl_r": keyboard.Key.ctrl_r,
    "f8": keyboard.Key.f8,
    "f9": keyboard.Key.f9,
    "pause": keyboard.Key.pause,
}
HOTKEY_LABELS = {
    "alt_r": "Right Alt",
    "alt_l": "Left Alt",
    "ctrl_r": "Right Ctrl",
    "f8": "F8",
    "f9": "F9",
    "pause": "Pause",
}

# Right Alt trên nhiều layout (VN) = AltGr -> Windows gửi alt_gr (kèm Ctrl giả).
# Chấp nhận cả hai để Right Alt luôn ăn.
HOTKEY_ALIASES = {
    "alt_r": (keyboard.Key.alt_r, keyboard.Key.alt_gr),
    "alt_l": (keyboard.Key.alt_l,),
    "ctrl_r": (keyboard.Key.ctrl_r,),
    "f8": (keyboard.Key.f8,),
    "f9": (keyboard.Key.f9,),
    "pause": (keyboard.Key.pause,),
}


_CUDA_DLL_HANDLES = []   # giữ tham chiếu các handle add_dll_directory (đừng để GC)


def _enable_cuda_dlls():
    """Nạp DLL cuBLAS/cuDNN từ các wheel nvidia-*-cu12 vào DLL search path
    (ctranslate2 cần cublas64_12.dll + cudnn lúc chạy GPU trên Windows).
    Không dùng `import nvidia` vì đó là namespace package (import hay lỗi)."""
    import os, sys, glob, site
    dirs = []
    if getattr(sys, "frozen", False):        # PyInstaller bundle: DLL nằm trong _MEIPASS / cạnh exe
        dirs.append(getattr(sys, "_MEIPASS", ""))
        dirs.append(os.path.dirname(sys.executable))
    roots = [os.path.join(sys.prefix, "Lib", "site-packages")]
    try:
        roots += list(site.getsitepackages())
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    for sp in roots:                          # dev: DLL trong wheel nvidia/*/bin
        dirs += glob.glob(os.path.join(sp, "nvidia", "*", "bin"))
    n = 0
    for d in dict.fromkeys(dirs):
        if d and os.path.isdir(d):
            # PATH là cơ chế ctranslate2 thực sự dùng để tìm cublas64_12.dll
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            try:
                _CUDA_DLL_HANDLES.append(os.add_dll_directory(d))
            except Exception:
                pass
            n += 1
    print(f"[cuda] PATH += {n} thư mục DLL", file=sys.stderr, flush=True)


# QUAN TRỌNG: chạy NGAY lúc import module, TRƯỚC mọi `import ctranslate2`
# (detect_device/faster_whisper import nó). add_dll_directory phải có hiệu lực
# trước khi backend CUDA của ctranslate2 được nạp, nếu không cublas vẫn không thấy.
_enable_cuda_dlls()


def detect_device():
    """Ưu tiên GPU (CUDA) cho tốc độ dịch ~0.5s; không có GPU thì về CPU int8.
    Ép CPU bằng biến môi trường SOTTRA_DEVICE=cpu."""
    import os
    if os.environ.get("SOTTRA_DEVICE", "").lower() != "cpu":
        try:
            from ctranslate2 import get_cuda_device_count
            if get_cuda_device_count() > 0:
                # int8_float16: nhẹ VRAM hơn + nhanh hơn float16, giữ độ chính xác
                return "cuda", "int8_float16", "GPU·int8"
        except Exception:
            pass
    return "cpu", "int8", "CPU·int8"


class SttEngine:
    def __init__(self, emit):
        self.emit = emit
        self.audio_queue = queue.Queue()
        self.stream = None
        self.recording = False
        self.model = None
        self.kb = keyboard.Controller()
        self._listener = None

        # Cấu hình runtime (UI có thể đổi)
        self.model_size = "large-v3"
        self.language = "vi"
        self.output_mode = "type"        # "type" | "clipboard"
        self.hotkey_name = "alt_r"
        self.hotkey_keys = HOTKEY_ALIASES[self.hotkey_name]
        self._hotkey_down = False        # chống auto-repeat: chỉ toggle ở lần nhấn đầu

        self._cur_level = 0.0
        self._pump = None
        self._pump_stop = threading.Event()

    # ----------------------- Vòng đời -----------------------
    def start(self):
        """Load model (thread riêng) rồi bật global listener."""
        threading.Thread(target=self._load_model, daemon=True).start()
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()

    def _load_model(self):
        self.emit("model", "loading")
        device, compute_type, label = detect_device()
        self.emit("device", label)
        try:
            from faster_whisper import WhisperModel
            self.model = WhisperModel(
                self.model_size, device=device, compute_type=compute_type
            )
            # Warmup: dịch dummy 1 lần để GPU nóng kernel -> câu thật đầu không chậm
            list(self.model.transcribe(
                np.zeros(16000, dtype=np.float32),
                language=self.language, beam_size=1,
            )[0])
        except Exception as e:
            self.emit("error", f"Không tải được mô hình: {e}")
            return
        self.emit("model", "ready")
        self.emit("state", "idle")

    def shutdown(self):
        if self._listener is not None:
            self._listener.stop()
        self._safe_close_stream()

    # ----------------------- Cấu hình -----------------------
    def set_output_mode(self, mode):
        if mode in ("type", "clipboard"):
            self.output_mode = mode

    def set_hotkey(self, name):
        if name in HOTKEY_ALIASES:
            self.hotkey_name = name
            self.hotkey_keys = HOTKEY_ALIASES[name]

    def reload_model(self, model_size):
        """Đổi kích thước model -> nạp lại."""
        self.model_size = model_size
        self.model = None
        threading.Thread(target=self._load_model, daemon=True).start()

    # ----------------------- Thu âm (RAM) -----------------------
    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Audio] {status}", file=sys.stderr)
        self.audio_queue.put(indata.copy())
        # Chỉ tính mức âm thanh, KHÔNG gọi evaluate_js ở đây (tránh nghẽn audio)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        self._cur_level = min(1.0, rms * 9.0)

    def _level_pump(self):
        """Đẩy mức âm thanh sang UI ~22fps từ thread riêng."""
        while not self._pump_stop.is_set():
            self.emit("level", round(self._cur_level, 3))
            time.sleep(0.045)

    def _start_recording(self):
        if self.recording or self.model is None:
            return
        # Xoá queue TRƯỚC khi start, nếu không sẽ vứt mất block âm thanh đầu (mất chữ đầu)
        with self.audio_queue.mutex:
            self.audio_queue.queue.clear()
        try:
            self.stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="float32",
                latency="low",                 # giảm độ trễ khởi động stream
                callback=self._audio_callback,
            )
            self.stream.start()
        except Exception as e:
            self.emit("error", f"Không mở được micro: {e}")
            self.stream = None
            return

        self.recording = True
        self.emit("state", "recording")
        self._pump_stop.clear()
        self._pump = threading.Thread(target=self._level_pump, daemon=True)
        self._pump.start()

    def _stop_recording(self):
        if not self.recording:
            return
        self.recording = False
        self._pump_stop.set()
        self._cur_level = 0.0
        self._safe_close_stream()
        self.emit("level", 0.0)

        blocks = []
        while not self.audio_queue.empty():
            blocks.append(self.audio_queue.get())
        if not blocks:
            self.emit("state", "idle")
            return

        audio = np.concatenate(blocks, axis=0).flatten().astype(np.float32)
        if audio.shape[0] < SAMPLE_RATE * 0.3:     # < 0.3s -> nhấn nhầm
            self.emit("state", "idle")
            return

        threading.Thread(
            target=self._transcribe_and_emit, args=(audio,), daemon=True
        ).start()

    def _safe_close_stream(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                print(f"[Stream] {e}", file=sys.stderr)
            self.stream = None

    # ------------------- Dịch + xuất chữ -------------------
    def _transcribe_and_emit(self, audio):
        # Bỏ qua clip gần như im lặng / chỉ có tiếng thở -> tránh Whisper ảo giác
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        dur = audio.shape[0] / SAMPLE_RATE
        print(f"[audio] dur={dur:.1f}s peak={peak:.4f} rms={rms:.4f}",
              file=sys.stderr, flush=True)
        if peak < 0.035 or rms < 0.009:
            print("[audio] -> SKIP (im lặng)", file=sys.stderr, flush=True)
            self.emit("state", "idle")
            return
        self.emit("state", "transcribing")
        # Khử ồn nền — TẮT để test (noisereduce có thể làm méo giọng -> sai chữ)
        if getattr(self, "denoise", False):
            try:
                import noisereduce as nr
                audio = nr.reduce_noise(
                    y=audio, sr=SAMPLE_RATE, stationary=False, prop_decrease=0.75
                ).astype(np.float32)
            except Exception as e:
                print(f"[nr] bỏ qua khử ồn: {e}", file=sys.stderr, flush=True)
        try:
            segments, _ = self.model.transcribe(
                audio, language=self.language,
                beam_size=1,
                temperature=0,                     # tắt fallback nhiệt -> bớt ảo giác
                condition_on_previous_text=False,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                compression_ratio_threshold=2.4,   # loại đoạn lặp lại (dấu hiệu ảo giác)
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=300, speech_pad_ms=300),
            )
            text = "".join(seg.text for seg in segments).strip()
        except Exception as e:
            self.emit("error", f"Lỗi nhận dạng: {e}")
            self.emit("state", "idle")
            return
        finally:
            del audio                              # giải phóng âm thanh khỏi RAM ngay

        halluc = _is_hallucination(text)
        print(f"[stt] text={text!r} halluc={halluc}", file=sys.stderr, flush=True)
        if not text or halluc:
            self.emit("state", "idle")
            return

        self._deliver(text)
        self.emit("result", {"text": text, "time": time.strftime("%H:%M")})
        self.emit("state", "idle")

    def _deliver(self, text):
        """Gõ tại con trỏ, hoặc copy clipboard, tuỳ output_mode."""
        if self.output_mode == "clipboard":
            try:
                import pyperclip
                pyperclip.copy(text)
                return
            except Exception:
                pass  # không có pyperclip -> rơi xuống chế độ gõ
        try:
            self.kb.type(text + " ")
        except Exception as e:
            self.emit("error", f"Lỗi xuất chữ: {e}")

    # ------------------- Hotkey -------------------
    def toggle(self):
        """Bật/tắt thu âm — dùng cho cả phím tắt lẫn nút mic."""
        if self.model is None:
            return
        if self.recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _on_press(self, key):
        # Toggle: chỉ lật ở lần nhấn ĐẦU, bỏ qua auto-repeat khi giữ phím
        if key in self.hotkey_keys and not self._hotkey_down:
            self._hotkey_down = True
            self.toggle()

    def _on_release(self, key):
        if key in self.hotkey_keys:
            self._hotkey_down = False
