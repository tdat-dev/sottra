"""
Sottra — STT engine (cloud-only, Groq)
======================================
Push-to-talk: giữ/nhấn phím -> thu âm RAM (16kHz) -> gửi lên Groq
(whisper-large-v3-turbo) -> gõ tại con trỏ HOẶC copy clipboard.
NHẸ MÁY: KHÔNG chạy model cục bộ, KHÔNG cần GPU/CUDA. Cần internet + Groq key.

Engine tách rời UI qua callback emit(event, payload):
    emit("model",  "ready")                 # giữ để tương thích UI (không còn nạp model)
    emit("state",  "idle" | "recording" | "transcribing")
    emit("level",  float 0..1)              # mức âm thanh realtime
    emit("result", {"text": str, "time": "HH:MM"})
    emit("error",  str)
    emit("device", "Groq·turbo")
"""

import sys
import time
import queue
import threading

import numpy as np
import sounddevice as sd
from pynput import keyboard

import config

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


# Prompt dọn chính tả: CHỈ thêm dấu/sửa dấu câu, GIỮ NGUYÊN từ (không bịa/đổi/dịch).
# Ít-shot để model bám đúng hành vi (đã test: llama-3.3-70b trung thực, ~0.5s).
_REFINE_SYS = (
    "Bạn là bộ THÊM DẤU và sửa chính tả tiếng Việt cho văn bản nhận dạng giọng nói.\n"
    "QUY TẮC:\n"
    "- Chỉ thêm/sửa DẤU THANH, DẤU CÂU và VIẾT HOA.\n"
    "- GIỮ NGUYÊN từng từ: KHÔNG thay từ này bằng từ khác, KHÔNG thêm/bớt từ, KHÔNG dịch.\n"
    "- Giữ nguyên từ tiếng Anh (Hello, AI, email...).\n"
    "- Nếu một từ nghe vô nghĩa, CỨ GIỮ NGUYÊN, không đoán từ khác.\n"
    "- Chỉ trả về đúng văn bản, không giải thích.\n"
    "Ví dụ:\n"
    "vào: hôm nay minh gưi email cho khach hang\n"
    "ra: Hôm nay mình gửi email cho khách hàng\n"
    "vào: Hello đây la ban thu AI\n"
    "ra: Hello, đây là bản thu AI"
)


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


class SttEngine:
    def __init__(self, emit):
        self.emit = emit
        self.audio_queue = queue.Queue()
        self.stream = None
        self.recording = False
        self.kb = keyboard.Controller()
        self._listener = None

        # Cấu hình runtime (UI có thể đổi)
        self.language = "vi"
        self.output_mode = "type"        # "type" | "clipboard"
        self.hotkey_name = "alt_r"
        self.hotkey_keys = HOTKEY_ALIASES[self.hotkey_name]
        self._hotkey_down = False        # chống auto-repeat: chỉ toggle ở lần nhấn đầu

        # Groq (đám mây)
        _c = config.load()
        self.groq_api_key = _c["groq_api_key"]
        self.groq_model = _c["groq_model"]
        self.groq_prompt = _c["groq_prompt"]
        self.refine = _c["refine"]              # dọn chính tả bằng LLM sau khi chép
        self.refine_model = _c["refine_model"]

        self._cur_level = 0.0
        self._pump = None
        self._pump_stop = threading.Event()
        self._transcribing = False

    # ----------------------- Vòng đời -----------------------
    def start(self):
        """Bật global listener. Cloud-only -> báo UI sẵn sàng ngay (không nạp model)."""
        self.emit("device", self._label())
        self.emit("model", "ready")
        self.emit("state", "idle")
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()

    def _label(self):
        return "Groq·" + self.groq_model.replace("whisper-", "").replace("-v3", "")

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

    def set_groq_key(self, key):
        """Lưu Groq API key (rỗng = xoá)."""
        self.groq_api_key = (key or "").strip()
        cfg = config.load()
        cfg["groq_api_key"] = self.groq_api_key
        config.save(cfg)

    def set_groq_model(self, model):
        """Đổi model Groq (large-v3 = chuẩn hơn, turbo = nhanh hơn), lưu cấu hình."""
        self.groq_model = model
        cfg = config.load()
        cfg["groq_model"] = model
        config.save(cfg)
        self.emit("device", self._label())

    def set_refine(self, on):
        """Bật/tắt dọn chính tả bằng LLM, lưu cấu hình."""
        self.refine = bool(on)
        cfg = config.load()
        cfg["refine"] = self.refine
        config.save(cfg)

    # ----------------------- Thu âm (RAM) -----------------------
    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            print(f"[Audio] {status}", file=sys.stderr)
        self.audio_queue.put(indata.copy())
        # Chỉ tính mức âm thanh cho UI (không gọi gì nặng ở đây -> tránh nghẽn audio)
        rms = float(np.sqrt(np.mean(indata.astype(np.float32) ** 2)))
        self._cur_level = min(1.0, rms * 9.0)

    def _level_pump(self):
        """Đẩy mức âm thanh sang UI ~22fps từ thread riêng."""
        while not self._pump_stop.is_set():
            self.emit("level", round(self._cur_level, 3))
            time.sleep(0.045)

    def _start_recording(self):
        if self.recording:
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

    # ------------------- Dịch (Groq) + xuất chữ -------------------
    def _transcribe_and_emit(self, audio):
        # Bỏ qua clip gần như im lặng / chỉ có tiếng thở -> khỏi tốn quota + tránh ảo giác
        peak = float(np.max(np.abs(audio)))
        rms = float(np.sqrt(np.mean(audio ** 2)))
        dur = audio.shape[0] / SAMPLE_RATE
        print(f"[audio] dur={dur:.1f}s peak={peak:.4f} rms={rms:.4f}",
              file=sys.stderr, flush=True)
        if peak < 0.035 or rms < 0.009:
            print("[audio] -> SKIP (im lặng)", file=sys.stderr, flush=True)
            self.emit("state", "idle")
            return

        if not self.groq_api_key:
            self.emit("error", "Chưa có Groq API key — vào menu khay để nhập")
            self.emit("state", "idle")
            return

        self.emit("state", "transcribing")
        self._transcribing = True
        text = ""
        try:
            text = self._transcribe_groq(audio)
        except Exception as e:
            print(f"[groq] lỗi: {e}", file=sys.stderr, flush=True)
            self.emit("error", f"Groq lỗi: {e}")
            self.emit("state", "idle")
            return
        finally:
            self._transcribing = False
            del audio                              # giải phóng âm thanh khỏi RAM ngay

        halluc = _is_hallucination(text)
        print(f"[stt] text={text!r} halluc={halluc}", file=sys.stderr, flush=True)
        if not text or halluc:
            self.emit("state", "idle")
            return

        if self.refine:                        # LLM dọn dấu/chính tả (giữ "transcribing")
            text = self._refine_text(text)

        self._deliver(text)
        self.emit("result", {"text": text, "time": time.strftime("%H:%M")})
        self.emit("state", "idle")

    def _transcribe_groq(self, audio):
        """Gửi audio lên Groq (OpenAI-compatible) -> text. Raise nếu lỗi mạng/API.

        Dùng stdlib (urllib + wave) để chạy được cả trong bản đóng gói PyInstaller
        mà không cần thêm phụ thuộc (requests/httpx/groq SDK)."""
        import io
        import wave
        import json as _json
        import uuid
        import urllib.request

        # float32 [-1,1] -> WAV PCM 16-bit mono 16kHz
        pcm = np.clip(audio, -1.0, 1.0)
        pcm16 = (pcm * 32767.0).astype("<i2").tobytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm16)
        wav_bytes = buf.getvalue()

        boundary = "----SottraBoundary" + uuid.uuid4().hex

        def _field(name, value):
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")

        body = b""
        body += _field("model", self.groq_model)
        if self.language:
            body += _field("language", self.language)
        body += _field("temperature", "0")
        body += _field("response_format", "json")
        if self.groq_prompt:
            body += _field("prompt", self.groq_prompt)
        body += (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
            "Content-Type: audio/wav\r\n\r\n"
        ).encode("utf-8")
        body += wav_bytes + b"\r\n"
        body += f"--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.groq_api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                # Cloudflare của Groq chặn UA mặc định "Python-urllib" (lỗi 1010) -> đặt UA riêng
                "User-Agent": "Sottra/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        return (data.get("text") or "").strip()

    def _refine_text(self, text):
        """Nhờ LLM Groq dọn dấu/chính tả tiếng Việt. MỌI lỗi -> trả text gốc
        (không bao giờ để bước dọn làm hỏng/chậm treo kết quả)."""
        import json as _json
        import re
        import urllib.request
        try:
            body = _json.dumps({
                "model": self.refine_model,
                "temperature": 0,
                "max_tokens": 600,
                "messages": [
                    {"role": "system", "content": _REFINE_SYS},
                    {"role": "user", "content": text},
                ],
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {self.groq_api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Sottra/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            out = data["choices"][0]["message"]["content"] or ""
            out = re.sub(r"<think>.*?</think>", "", out, flags=re.S)  # model reasoning (nếu có)
            out = out.strip().strip('"').strip()
            # An toàn: rỗng hoặc dài bất thường (model "nói thêm") -> giữ bản gốc
            if not out or len(out) > len(text) * 2.5 + 40:
                return text
            return out
        except Exception as e:
            print(f"[refine] bỏ qua: {e}", file=sys.stderr, flush=True)
            return text

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
