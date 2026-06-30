# Sottra — Delta auto-update (design)

**Date:** 2026-06-30
**Status:** Approved (pending spec review)
**Repo:** https://github.com/tdat-dev/sottra

## Mục tiêu

App tự cập nhật lên bản mới mà **không phải tải lại 1.47GB mỗi lần**. Phần lớn
bundle (CUDA DLL, PySide6, ctranslate2 ≈ 2.3GB) cố định giữa các phiên bản; đổi
code thường chỉ thay đúng `Sottra.exe` (~14MB). Cập nhật **delta** chỉ tải file đã
đổi, **hỏi người dùng trước** khi áp dụng.

## Quyết định đã chốt

- **Delta** theo file (không tải nguyên zip).
- **Hỏi trước** rồi mới cập nhật. Kiểm tra lúc khởi động + menu tray thủ công.
- Hosting trên **GitHub Releases** (đã dùng cho v1.0.0).
- Chỉ hỗ trợ Windows onedir bundle (frozen). Chạy từ source → updater no-op.

## Kiến trúc

### 1. Phiên bản — `version.py`
Nguồn duy nhất: `__version__ = "1.0.0"`. Dùng bởi `app_qt.py` (hiển thị + so sánh)
và `tools/release.py` (đặt tag, ghi manifest). So sánh kiểu **semver** (tách
`major.minor.patch`, bỏ tiền tố `v`).

### 2. Kho blob địa chỉ-theo-hash (content-addressed)
- Một GitHub Release cố định, tag **`blobs`**, chứa các asset đặt tên theo
  **sha256 hex** của nội dung file. Append-only, không trùng (cùng nội dung →
  cùng hash → 1 blob). Giải quyết được trường hợp user **bỏ qua nhiều phiên bản**:
  app chỉ cần tải các hash nó đang thiếu, bất kể đổi từ version nào.
- Mỗi release phiên bản (`vX.Y.Z`) kèm 2 asset:
  - `manifest.json`: `{ "version": "X.Y.Z", "files": { "<path>": "<sha256>", ... } }`
    — `path` tương đối thư mục cài (vd `Sottra.exe`, `_internal/python311.dll`).
    Không liệt kê chính `manifest.json`.
  - `Sottra-vX.Y.Z-win64-gpu.zip`: bản cài đầy đủ cho người cài mới.

### 3. Script phát hành — `tools/release.py`
Tự động hoá để manifest và blob **không bao giờ lệch nhau**:
1. (Tuỳ chọn) chạy PyInstaller `Sottra.spec`.
2. Duyệt `dist/Sottra`, tính sha256 từng file → `manifest.json` (ghi vào
   `dist/Sottra/manifest.json` để **đóng luôn vào bundle** = local manifest của
   bản cài đó). Loại trừ chính `manifest.json` khỏi danh sách.
3. Đảm bảo release `blobs` tồn tại (tạo nếu chưa). Lấy danh sách asset hiện có;
   **upload chỉ những blob có hash chưa tồn tại** (đổi code → thường chỉ `Sottra.exe`).
   Tên asset = `<sha256>`.
4. Zip full `dist/Sottra` → `Sottra-vX.Y.Z-win64-gpu.zip`.
5. `gh release create vX.Y.Z` kèm `manifest.json` + zip; đặt `--latest`.

> Lưu ý GitHub: tên asset không cho vài ký tự; sha256 hex (0-9a-f) hợp lệ. Mỗi
> release tối đa nhiều asset; số blob tăng dần theo thời gian — chấp nhận được,
> có thể dọn blob cũ sau (ngoài phạm vi bản này).

### 4. Updater trong app — `updater.py`
Hàm thuần, tách khỏi UI, để test được:

- `current_version() -> str` — từ `version.py`.
- `is_newer(remote, local) -> bool` — so sánh semver.
- `check_latest() -> dict | None` — GET
  `api.github.com/repos/tdat-dev/sottra/releases/latest` (không cần auth; giới hạn
  60 req/giờ/IP là đủ). Trả `{version, manifest_url, html_url}` nếu mới hơn, else None.
- `diff_manifest(local, remote) -> (fetch:set[str], delete:set[str])` — **thuần**,
  unit-test trực tiếp. `fetch` = path có hash khác hoặc thiếu local; `delete` =
  path có trong local, không có trong remote.
- `download_blob(sha, dest)` — tải `blobs/<sha>` → verify sha256 → ghi `dest`.
  Sai hash → thử lại 1 lần rồi raise.
- `stage_update(remote_manifest)` — tải mọi blob trong `fetch` về
  `%LOCALAPPDATA%\Sottra\staging\<path>`; ghi `manifest.json` mới vào staging;
  trả về (staging_dir, delete_set).
- `apply_and_restart(staging_dir, delete_set)` — sinh helper PowerShell, spawn
  detached, rồi `QApplication.quit()`.

### 5. Helper áp dụng — `apply_update.ps1` (sinh lúc chạy)
App đang chạy khoá `Sottra.exe` + DLL đã nạp → phải để **tiến trình ngoài** làm
sau khi app thoát. Ghi ra `%LOCALAPPDATA%\Sottra\apply_update.ps1`, gọi:
`powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File apply_update.ps1 <pid> <install_dir> <staging_dir>`
Các bước trong helper:
1. Đợi tiến trình `<pid>` thoát (`Wait-Process` / vòng lặp, timeout ~30s).
2. Copy đè cây file từ `<staging_dir>` sang `<install_dir>` (robocopy).
3. Xoá các file trong `delete_set` (truyền qua file `delete.txt` trong staging).
4. Khởi động lại `<install_dir>\Sottra.exe`.
5. Dọn staging. Ghi log mọi bước ra `%LOCALAPPDATA%\Sottra\update.log`.

### 6. Wiring UI — `app_qt.py`
- Menu tray thêm: **"Kiểm tra cập nhật"** (thủ công) và mục động
  **"Cập nhật lên vX.Y…"** (chỉ hiện khi có bản mới).
- Khởi động: thread nền chạy `check_latest()`; kết quả đẩy về GUI qua `Bridge`
  signal (đã có sẵn pattern) → hiện **balloon tray** + bật mục menu.
- Bấm cập nhật → xác nhận → `stage_update` (tiến độ show ở **tooltip tray**:
  "Đang tải cập nhật… 40%") → `apply_and_restart`.
- Hiển thị version hiện tại ở tooltip/menu tray.

## Luồng dữ liệu

```
startup ──► check_latest() ──(mới hơn?)──► Bridge ──► tray balloon + menu
                                                          │ user đồng ý
                                                          ▼
                          tải manifest mới ─► diff vs local manifest.json
                                                          │
                                       fetch blobs ─► staging (verify hash)
                                                          │
                              sinh apply_update.ps1 ─► spawn detached ─► quit()
                                                          │ (app thoát)
                              helper: đợi pid → đè file → xoá thừa → relaunch
```

## Xử lý lỗi
- Không mạng / API lỗi / rate-limit → log, app chạy bình thường, không phiền user.
- Blob thiếu trong store (lỗi release) → huỷ apply, fallback mở `html_url` release.
- Hash sai sau tải → thử lại 1 lần, fail → huỷ (không đụng bản đang cài).
- Helper đè lỗi (khoá file/quyền) → ghi `update.log`; bản cũ còn nguyên, app vẫn chạy.
- Chạy từ source (`sys.frozen` false) → updater **no-op** (chỉ log "dev mode").
- Cài trong thư mục cần quyền admin → khuyến nghị cài nơi user ghi được (giải nén
  vào thư mục cá nhân); helper chạy cùng quyền với app.

## Test
- **Unit:** `diff_manifest` (các tổ hợp thêm/đổi/xoá/giống hệt), `is_newer`
  (1.0.0 vs 1.0.1, 1.2.0 vs 1.10.0, bằng nhau, có/không tiền tố `v`).
- **Release script dry-run:** sinh manifest + xác định blob cần upload mà không
  upload thật; kiểm tra đổi code chỉ ra 1 blob (`Sottra.exe`).
- **E2E thật:** bump `1.0.1`, đổi 1 dòng code, `release.py` publish → chạy exe
  `1.0.0` → xác nhận phát hiện bản mới → tải đúng ~14MB (`Sottra.exe`) → restart
  thành `1.0.1`. Kiểm tra `update.log`.

## Phạm vi loại trừ (YAGNI)
- Không tự động im lặng (đã chọn hỏi trước).
- Không dọn blob cũ / không nén delta nhị phân từng file (chỉ thay nguyên file).
- Không hỗ trợ rollback tự động (bản cũ vẫn còn tới khi helper đè xong; nếu lỗi
  giữa chừng, file chưa đè vẫn dùng được — đủ an toàn cho v1).
- Chưa làm bản CPU-only (theo dõi riêng).

## File ảnh hưởng
- **Mới:** `version.py`, `updater.py`, `tools/release.py`.
- **Sửa:** `app_qt.py` (tray + startup check), `README.md` (mục cập nhật),
  `Sottra.spec` (đảm bảo `version.py` vào bundle; `manifest.json` do release.py
  ghi vào `dist` sau build nên không cần khai trong spec).
