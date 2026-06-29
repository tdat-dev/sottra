# Sottra

**Push-to-Talk Speech-to-Text** chạy hoàn toàn offline. Giữ một phím tắt toàn cục,
nói, nhả phím — chữ tiếng Việt tự chèn vào nơi con trỏ đang đứng. Không gọi bất kỳ
API cloud/LLM nào.

> Tên *Sottra* lấy từ **sotto voce** (nói rất khẽ) — đúng tinh thần "nói thầm ra chữ".

Giao diện là một **pill** nổi, siêu nhẹ, vẽ bằng Qt (PySide6 / QPainter). Backend nhận
dạng dùng **faster-whisper** cục bộ.

## Tính năng

- **Offline 100%** — audio không rời khỏi máy.
- **Gõ tại con trỏ** hoặc **Clipboard**.
- **Icon theo app đang focus** — pill tự nhận diện ứng dụng bạn đang gõ vào
  (Chrome, VS Code, Word…) và hiển thị icon của app đó thay cho mic. Khi đang
  dịch sẽ hiện spinner; không lấy được icon thì fallback về mic.
- **Tự nhận GPU** — có CUDA → `int8_float16`, không có → CPU `int8`.

## Cài đặt

```bash
pip install -r requirements.txt
```

> Lần chạy đầu sẽ tự tải weights mô hình (small ≈ 460MB) về cache — cần mạng một
> lần, sau đó offline hoàn toàn.

## Chạy

```bash
python app_qt.py
```

Trên Windows, để chèn chữ được vào mọi ứng dụng (kể cả app chạy quyền admin),
nên mở terminal **bằng quyền Administrator**.

## Dùng

1. Giữ **Right Alt** (đổi được trong Cài đặt) → pill chuyển vàng, sóng nở theo âm
   lượng thật khi bạn nói.
2. Nhả phím → spinner quay (đang dịch) → chữ tự chèn tại con trỏ.
3. Click trái lên pill (hoặc icon tray) để bật/tắt nói; kéo để di chuyển pill.

## Đóng gói (.exe)

```bash
pip install pyinstaller
pyinstaller Sottra.spec
```

Kết quả ở `dist/Sottra/Sottra.exe`. Icon lấy từ `icon.ico`.

## Cấu trúc

```
sottra/
├── app_qt.py         # pill Qt (PySide6) + tray + nhận diện icon app — bản chính
├── app.py            # bản pywebview cũ (giữ tham khảo)
├── engine.py         # STT engine: hotkey, thu âm RAM, faster-whisper, xuất chữ
├── icon.svg          # logo nguồn (waveform amber)
├── icon.ico / icon.png
├── Sottra.spec       # cấu hình PyInstaller
├── requirements.txt
└── web/              # frontend cho bản pywebview cũ
```

## Tuỳ chỉnh nhanh

- **Mô hình**: tiny/base/small/medium (đổi sẽ nạp lại).
- **Phím tắt**: Right/Left Alt, Right Ctrl, F8, F9, Pause.
- **Ép CPU**: đặt biến môi trường `SOTTRA_DEVICE=cpu`.

## Bản quyền

Phát hành theo giấy phép [MIT](LICENSE) © 2026 tdat-dev.
