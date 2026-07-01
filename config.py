"""
Sottra — cấu hình người dùng
============================
Lưu Groq API key + tuỳ chọn ở %APPDATA%\\Sottra\\config.json (Windows) /
~/.config/Sottra (khác). Cố tình tối giản: không phụ thuộc thư viện ngoài,
đọc/ghi an toàn khi lỗi.
"""

import os
import json
import threading

_LOCK = threading.Lock()

DEFAULTS = {
    "groq_api_key": "",
    "groq_model": "whisper-large-v3",         # chuẩn hơn cho tiếng Việt (turbo nhanh nhưng kém dấu)
    "groq_prompt": "",                        # gợi ý từ vựng/tên riêng để chép đúng hơn
    "refine": True,                           # LLM dọn dấu/chính tả tiếng Việt sau khi chép (+~0.5s)
    "refine_model": "llama-3.3-70b-versatile",  # nhanh + trung thực (không bịa/đổi từ)
    "installed": False,                       # đã tạo lối tắt + bật startup lần đầu chưa
}


def _config_dir():
    base = os.environ.get("APPDATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, "Sottra")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _config_path():
    return os.path.join(_config_dir(), "config.json")


def load():
    """Trả về cấu hình đầy đủ (mặc định + đã lưu + override từ env GROQ_API_KEY)."""
    cfg = dict(DEFAULTS)
    try:
        with open(_config_path(), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            cfg.update({k: saved[k] for k in DEFAULTS if k in saved})
    except Exception:
        pass
    env_key = os.environ.get("GROQ_API_KEY")
    if env_key and not cfg.get("groq_api_key"):
        cfg["groq_api_key"] = env_key.strip()
    return cfg


def save(cfg):
    """Ghi cấu hình (chỉ các khoá hợp lệ). Bỏ qua nếu ghi lỗi."""
    data = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    with _LOCK:
        try:
            with open(_config_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return data
