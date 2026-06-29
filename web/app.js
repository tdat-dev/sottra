/* ============================================================
   Sottra — voice pill frontend
   Python -> JS: window.__emit(event, payload)
   JS -> Python: window.pywebview.api.*
   ============================================================ */
(() => {
  "use strict";
  const body = document.body;
  const bars = Array.from(document.querySelectorAll(".bar"));
  const N = bars.length;

  let level = 0;                       // mức âm thanh hiện tại (0..1) từ Python
  let smooth = 0;                      // làm mượt
  const cur = new Array(N).fill(0);    // chiều cao hiện tại mỗi bar
  const MIN_H = 2, MAX_H = 22;
  // envelope hình chuông: giữa cao, mép thấp -> dáng waveform tiếng nói
  const env = Array.from({length: N}, (_, i) => Math.sin(Math.PI * (i + 0.5) / N));

  function api() { return window.pywebview && window.pywebview.api; }

  /* ---------- Vòng lặp vẽ sóng ---------- */
  let t = 0;
  function frame() {
    t += 0.08;
    const state = body.dataset.state;
    smooth += (level - smooth) * 0.4;

    for (let i = 0; i < N; i++) {
      let target;
      if (state === "recording") {
        // waveform: envelope × mức âm thanh thật × dao động riêng từng thanh
        const n = 0.5 + 0.5 * Math.abs(Math.sin(t * 1.7 + i * 0.9)
                                     + 0.5 * Math.sin(t * 2.9 + i * 1.7)) / 1.5;
        target = MIN_H + smooth * (MAX_H - MIN_H) * env[i] * Math.min(1, n);
      } else if (state === "transcribing") {
        // đang xử lý (không nghe): sóng thấp chạy dọc dải
        const w = 0.5 + 0.5 * Math.sin(t * 3 - i * 0.5);
        target = MIN_H + env[i] * 3 + w * 6;
      } else {
        // idle / loading: bars ẩn (CSS), baseline hiện -> đường tĩnh lặng
        target = MIN_H;
      }
      cur[i] += (target - cur[i]) * 0.45;
      bars[i].style.height = cur[i].toFixed(1) + "px";
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);

  /* ---------- Trạng thái ---------- */
  function setState(s) {
    body.dataset.state = s;
    if (s !== "recording") level = 0;
    if (s === "done") setTimeout(() => {
      if (body.dataset.state === "done") setState("idle");
    }, 800);
  }

  /* ---------- Nhận sự kiện từ Python ---------- */
  window.__emit = (event, payload) => {
    switch (event) {
      case "level": level = payload; break;
      case "model": if (payload === "loading") setState("loading"); break;
      case "state": setState(payload); break;
      case "result": flash(payload); break;
      case "error": console.error("Sottra:", payload); break;
    }
    if (event !== "level") api()?.jslog(event + ":" + JSON.stringify(payload));
  };

  function flash() { /* kết quả đã chèn ở phía Python; sóng tự về idle qua state */ }

  /* ---------- Nút mic: bật/tắt thu âm ---------- */
  document.getElementById("btn-mic")
    .addEventListener("click", (e) => { e.stopPropagation(); api()?.toggle(); });

  /* ---------- Đóng ---------- */
  document.getElementById("btn-close")
    .addEventListener("click", (e) => { e.stopPropagation(); api()?.close(); });

  /* ---------- Khởi động ---------- */
  window.addEventListener("pywebviewready", () => { api()?.ready(); });
})();
