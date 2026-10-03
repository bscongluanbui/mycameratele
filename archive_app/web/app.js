/* No CDN, no stored admin token, no credentials in camera metadata. */
(() => {
  "use strict";
  const STATUS = {
    uploaded: ["Đã lưu Telegram", "green"], downloaded: ["Chờ upload", "neutral"],
    uploading: ["Đang upload", "neutral"], upload_unknown: ["Cần đối chiếu upload", "amber"],
    needs_review: ["Cần kiểm tra", "amber"], failed: ["Lỗi xử lý", "red"],
    ingesting: ["Đang nhập video", "neutral"], pending: ["Đang chờ", "neutral"]
  };
  const helpers = {
    formatBytes(value) {
      if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value)) || Number(value) < 0) return "—";
      let size = Number(value), unit = 0;
      const units = ["B", "KB", "MB", "GB", "TB"];
      while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit++; }
      return `${size.toLocaleString("vi-VN", { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
    },
    formatDuration(start, end) {
      if (start === null || end === null || start === undefined || end === undefined) return "—";
      const seconds = Math.round((Number(end) - Number(start)) / 1000);
      if (!Number.isFinite(seconds) || seconds < 0) return "—";
      if (seconds < 60) return `${seconds} giây`;
      const minutes = Math.floor(seconds / 60), remainder = seconds % 60;
      return `${minutes} phút${remainder ? ` ${remainder} giây` : ""}`;
    },
    telegramUrl(value) {
      if (typeof value !== "string") return null;
      try {
        const url = new URL(value);
        return url.protocol === "https:" && url.hostname === "t.me" && !url.username && !url.password && !url.port && /^\/[^\s]+$/.test(url.pathname) ? url.href : null;
      } catch (_) { return null; }
    },
    validHost(value) {
      if (typeof value !== "string" || !value || value.length > 253) return false;
      if (/^\d+(\.\d+){3}$/.test(value)) return value.split(".").every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255);
      return value.split(".").every(label => label.length > 0 && label.length <= 63 && /^[a-z\d](?:[a-z\d-]*[a-z\d])?$/i.test(label));
    },
    status(value) { return STATUS[String(value).toLowerCase()] || [value ? String(value) : "Chưa rõ", "neutral"]; },
    probeState(value) {
      if (value === true || value === "open" || value === "connected") return ["Mở", "green"];
      if (value === false || value === "closed" || value === "refused") return ["Đóng / không phản hồi", "red"];
      if (value === "unconfirmed") return ["Chưa xác nhận", "amber"];
      if (value && typeof value === "object") return helpers.probeState(value.open ?? value.connected ?? value.status ?? value.result);
      if (value === null || value === undefined) return ["Chưa kiểm tra", "neutral"];
      return [String(value).slice(0, 100), "amber"];
    },
    cameraCount(camera, type) {
      const value = type === "uploaded" ? (camera.uploaded_count ?? camera.counts?.uploaded) : (camera.record_count ?? camera.recording_count ?? camera.counts?.total ?? camera.counts?.recordings);
      return Number.isFinite(Number(value)) && value !== null && value !== undefined ? Number(value) : null;
    },
    checkedMillis(value) {
      if (typeof value === "number" && Number.isFinite(value)) return value * 1000;
      if (typeof value === "string" && value) { const parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null; }
      return null;
    },
    cameraPatch(original, edited) {
      const patch = {};
      for (const key of ["name", "model", "host", "device_port", "rtsp_port", "http_port", "enabled"]) {
        if (edited[key] !== undefined && edited[key] !== original[key]) patch[key] = edited[key];
      }
      return patch;
    }
  };
  // Export pure helpers for dependency-free Node regression tests.
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const state = { cameras: [], status: {}, csrf: "", probes: new Map(), calendar: [], offset: 0, limit: 25, total: 0, archiveRequest: 0, calendarRequest: 0, editing: null, view: "cameras", authenticated: false };
  let toastTimer;
  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }
  const iconPaths = {
    camera: '<rect x="3" y="6" width="13" height="12" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
    edit: '<path d="m16 3 5 5-12 12H4v-5zM13 6l5 5"/>',
    network: '<circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M12 7v6M5 17v-4h14v4"/>',
    video: '<path d="M6 3h8l4 4v14H6zM14 3v5h4"/><path d="m10 12 5 3-5 3z"/>',
    external: '<path d="M14 3h7v7M21 3l-9 9M10 3H3v18h18v-7"/>',
    folder: '<path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3"/>'
  };
  function icon(name) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    item.setAttribute("viewBox", "0 0 24 24"); item.setAttribute("aria-hidden", "true");
    // Only constant, local SVG paths are inserted here. All API data uses textContent.
    item.innerHTML = iconPaths[name] || iconPaths.camera;
    return item;
  }
  function button(text, classes, onClick, iconName) {
    const item = node("button", `button ${classes}`); item.type = "button";
    if (iconName) item.append(icon(iconName)); item.append(node("span", "", text));
    item.addEventListener("click", onClick); return item;
  }
  function chip(label, tone = "neutral") { return node("span", `status-chip ${tone}`, label); }
  function displayCount(value) { return Number.isFinite(Number(value)) && value !== null && value !== undefined ? Number(value).toLocaleString("vi-VN") : "—"; }
  function toast(message, error = false) {
    clearTimeout(toastTimer); $("toast").textContent = message; $("toast").classList.toggle("error", error); $("toast").hidden = false;
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 8000 : 4500);
  }
  function globalError(message) { $("global-error").textContent = message || ""; $("global-error").hidden = !message; }
  function showLogin() {
    state.authenticated = false; state.archiveRequest++; state.calendarRequest++;
    $("app-shell").hidden = true; $("boot-loading").hidden = true; $("login-screen").hidden = false;
    if ($("camera-dialog").open) $("camera-dialog").close();
    $("login-token").focus();
  }
  async function api(path, options = {}) {
    const method = options.method || "GET";
    const headers = { "Accept": "application/json", ...(options.headers || {}) };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    if (!["GET", "HEAD"].includes(method) && path !== "/api/login") headers["X-CSRF-Token"] = state.csrf;
    let response;
    try { response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store", body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
    catch (_) { throw new Error("Mất kết nối với dashboard. Kiểm tra dịch vụ Docker rồi thử lại."); }
    let result;
    try { result = await response.json(); } catch (_) { throw new Error(`Dashboard trả về dữ liệu không hợp lệ (HTTP ${response.status}).`); }
    if (!response.ok) {
      if (response.status === 401 && path !== "/api/login") showLogin();
      const error = new Error(result.error || result.message || `Yêu cầu chưa hoàn tất (HTTP ${response.status}).`);
      error.httpStatus = response.status; throw error;
    }
    return result;
  }
  function cameraName(id) { return state.cameras.find(camera => camera.id === id)?.name || id || "Camera chưa đặt tên"; }
  function timeParts(timestamp) {
    if (timestamp === null || timestamp === undefined || !Number.isFinite(Number(timestamp))) return ["—", ""];
    const date = new Date(Number(timestamp)); if (!Number.isFinite(date.getTime())) return ["—", ""];
    let timezone = state.status.timezone || "Asia/Bangkok";
    try {
      return [new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: timezone }).format(date), new Intl.DateTimeFormat("vi-VN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, timeZone: timezone }).format(date)];
    } catch (_) {
      // Fixed-offset config is also accepted by the Python service. Render explicitly,
      // rather than silently falling back to the browser's different local timezone.
      const match = /^UTC([+-])(\d{2}):(\d{2})$/.exec(timezone);
      if (!match) return [date.toISOString().slice(0, 10), `${date.toISOString().slice(11, 19)} UTC`];
      const shift = (Number(match[2]) * 60 + Number(match[3])) * (match[1] === "+" ? 1 : -1) * 60000;
      const adjusted = new Date(date.getTime() + shift);
      return [new Intl.DateTimeFormat("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" }).format(adjusted), adjusted.toISOString().slice(11, 19)];
    }
  }
  function renderStats() {
    $("stat-cameras").textContent = displayCount(state.cameras.length);
    $("nav-camera-count").textContent = displayCount(state.cameras.length);
    $("stat-enabled").textContent = displayCount(state.cameras.filter(camera => camera.enabled !== false).length);
    const counts = state.status.counts || {};
    let recordings = counts.recordings ?? counts.total ?? state.status.recording_count;
    let uploaded = counts.uploaded ?? state.status.uploaded_count ?? state.status.queue?.uploaded;
    if (uploaded === undefined && state.status.queue && typeof state.status.queue === "object") uploaded = 0;
    if (recordings === undefined && state.cameras.length && state.cameras.every(camera => helpers.cameraCount(camera, "total") !== null)) recordings = state.cameras.reduce((sum, camera) => sum + helpers.cameraCount(camera, "total"), 0);
    if (uploaded === undefined && state.cameras.length && state.cameras.every(camera => helpers.cameraCount(camera, "uploaded") !== null)) uploaded = state.cameras.reduce((sum, camera) => sum + helpers.cameraCount(camera, "uploaded"), 0);
    $("stat-recordings").textContent = displayCount(recordings); $("stat-uploaded").textContent = displayCount(uploaded);
  }
  function infoRow(label, value, mono = false) {
    const row = node("div", "camera-info-row"); row.append(node("span", "label", label));
    if (value instanceof Node) row.append(value); else row.append(node("span", `value${mono ? " mono" : ""}`, value)); return row;
  }
  function probeSummary(probe) {
    if (!probe) return ["Chưa kiểm tra LAN", "neutral"];
    const values = Object.values(probe.tcp || {});
    const open = values.filter(value => helpers.probeState(value)[1] === "green").length;
    return open ? [`${open}/${values.length} cổng mở`, "green"] : ["Chưa thấy cổng mở", "amber"];
  }
  function renderCameras() {
    const query = $("camera-search").value.toLocaleLowerCase("vi-VN").trim();
    const cameras = state.cameras.filter(camera => [camera.name, camera.id, camera.model, camera.host].some(value => String(value || "").toLocaleLowerCase("vi-VN").includes(query)));
    $("camera-grid").replaceChildren(); $("camera-empty").hidden = state.cameras.length > 0; $("camera-no-match").hidden = !state.cameras.length || cameras.length > 0;
    for (const camera of cameras) {
      const card = node("article", "camera-card");
      const top = node("div", "camera-card-top"), symbol = node("div", "camera-symbol"), title = node("div", "camera-card-title"); symbol.append(icon("camera"));
      title.append(node("h3", "", camera.name || camera.id), node("p", "camera-model", camera.model || "Chưa khai báo model"));
      const indicator = node("span", `camera-enabled-indicator${camera.enabled === false ? " off" : ""}`); indicator.setAttribute("aria-hidden", "true"); top.append(symbol, title, indicator);
      const info = node("div", "camera-info"), summary = probeSummary(state.probes.get(camera.id) || camera.probe);
      info.append(infoRow("Địa chỉ LAN", camera.host || "Chưa khai báo", true), infoRow("Mã camera", camera.id, true), infoRow("Kết nối", chip(...summary)), infoRow("Cấu hình", chip(camera.enabled === false ? "Đã tạm dừng" : "Đang bật", camera.enabled === false ? "neutral" : "green")));
      const total = helpers.cameraCount(camera, "total"), uploaded = helpers.cameraCount(camera, "uploaded");
      info.append(infoRow("Video / Đã lưu", `${displayCount(total)} / ${displayCount(uploaded)}`));
      const actions = node("div", "camera-card-bottom");
      actions.append(button("Thư viện", "primary", () => openCameraArchive(camera.id), "folder"), button("Chỉnh sửa", "ghost", () => openCameraForm(camera), "edit"));
      const probeButton = button("Kiểm tra LAN", "ghost", () => probeCamera(camera, probeButton), "network"); actions.append(probeButton);
      card.append(top, info, actions); $("camera-grid").append(card);
    }
    renderStats();
  }
  function option(value, label) { const item = node("option", "", label); item.value = String(value); return item; }
  function refreshCameraOptions() {
    const old = $("filter-camera").value;
    $("filter-camera").replaceChildren(option("", "Tất cả camera"));
    for (const camera of [...state.cameras].sort((a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id), "vi"))) $("filter-camera").append(option(camera.id, camera.name || camera.id));
    if (state.cameras.some(camera => camera.id === old)) $("filter-camera").value = old;
  }
  function renderSystem() {
    const status = state.status, timezone = status.timezone || "Chưa rõ";
    $("connection-state").textContent = "Dashboard đã kết nối"; $("sidebar-timezone").textContent = timezone;
    $("system-service-status").textContent = "API hoạt động"; $("system-service-status").className = "status-chip green";
    const details = [
      ["Phiên bản", status.version || "—"], ["Múi giờ lưu trữ", timezone],
      ["Upload Telegram", status.upload_enabled === true ? "Đã bật" : status.upload_enabled === false ? "Đang tắt" : "Chưa rõ"],
      ["Bot API", status.api_mode || status.telegram_api_mode || "—"],
      ["Kiến trúc", status.architecture || status.machine || "—"],
      ["Hàng đợi upload", status.queue?.downloaded ?? status.queue?.pending ?? status.queue_count ?? "—"],
      ["Heartbeat worker", status.heartbeat?.worker_alive === true ? "Đang hoạt động" : status.heartbeat?.worker_alive === false ? "Không thấy heartbeat mới" : status.worker?.heartbeat || status.heartbeat || "—"]
    ];
    $("system-details").replaceChildren();
    for (const [label, value] of details) { const row = node("div"); row.append(node("dt", "", label), node("dd", "", typeof value === "object" ? "Có dữ liệu" : value)); $("system-details").append(row); }
    const adapter = typeof status.sd_adapter === "object" ? (status.sd_adapter.status || status.sd_adapter.name || "not_implemented") : String(status.sd_adapter || "not_implemented");
    const noAutomaticDownload = status.sd_auto_download === "not_implemented" || ["not_implemented", "none", "", "exported-file-ingest"].includes(adapter);
    $("system-sd-status").textContent = noAutomaticDownload ? "Chưa triển khai tải SD" : "Cần kiểm chứng thiết bị";
    $("system-sd-description").textContent = noAutomaticDownload
      ? "Bộ tải lịch sử SD trực tiếp chưa được triển khai. Hiện hệ thống xử lý video đã xuất từ Studio; thêm camera không tự tạo bộ tải SD."
      : `Adapter: ${adapter}. Kết quả TCP không xác nhận khả năng đọc hoặc tải lịch sử SD.`;
    renderProbes();
  }
  function renderProbes() {
    const target = $("system-probes"); target.replaceChildren();
    if (!state.probes.size) { target.textContent = "Chưa có kết quả kiểm tra trong phiên này. Chọn “Kiểm tra LAN” ở thẻ camera."; return; }
    for (const [id, result] of state.probes) {
      const item = node("div", "probe-result"), top = node("div", "probe-result-top");
      const checkedAt = helpers.checkedMillis(result.checked_at);
      const checked = checkedAt === null ? "Vừa kiểm tra" : timeParts(checkedAt).join(" · ");
      top.append(node("span", "probe-result-name", `${cameraName(id)} · ${result.host || ""}`), node("span", "small muted", checked));
      const chips = node("div", "probe-chips");
      for (const [port, value] of Object.entries(result.tcp || {})) { const [label, tone] = helpers.probeState(value); chips.append(chip(`${port}: ${label}`, tone)); }
      if (!chips.childNodes.length) chips.append(chip("Không có kết quả TCP", "neutral"));
      item.append(top, chips, node("p", "probe-caption", "Lịch sử SD: chưa kiểm chứng · Không thực hiện đăng nhập camera.")); target.append(item);
    }
  }
  async function refresh() {
    $("refresh-button").disabled = true; $("camera-loading").hidden = false; globalError("");
    try {
      const status = await api("/api/status"); state.status = status; state.csrf = status.csrf_token || "";
      const response = await api("/api/cameras"); state.cameras = Array.isArray(response.cameras) ? response.cameras : [];
      state.authenticated = true; $("boot-loading").hidden = true; $("login-screen").hidden = true; $("app-shell").hidden = false;
      renderCameras(); refreshCameraOptions(); renderSystem();
      if (state.view === "archive") { await loadCalendar(true); await loadArchive(); }
    } catch (error) {
      if (error.httpStatus !== 401) {
        if (!state.authenticated) { $("boot-loading").hidden = true; $("login-screen").hidden = false; $("login-error").hidden = false; $("login-error").textContent = error.message; }
        else globalError(error.message);
        $("connection-state").textContent = "Mất kết nối dashboard";
      }
    } finally { $("refresh-button").disabled = false; $("camera-loading").hidden = true; }
  }
  function switchView(name, load = true) {
    if (!["cameras", "archive", "system"].includes(name)) name = "cameras";
    state.view = name;
    for (const item of document.querySelectorAll(".view")) item.hidden = item.id !== `view-${name}`;
    for (const link of document.querySelectorAll("[data-view]")) { const active = link.dataset.view === name; link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); }
    $("topbar-page").textContent = { cameras: "Camera", archive: "Thư viện video", system: "Hệ thống" }[name];
    if (name === "archive" && load && state.authenticated) { loadCalendar(true).then(loadArchive).catch(error => globalError(error.message)); }
    if (name === "system") renderSystem();
  }
  async function openCameraArchive(id) {
    $("filter-camera").value = id; state.offset = 0;
    for (const name of ["year", "month", "day"]) $("filter-" + name).value = "";
    if (location.hash !== "#archive") { location.hash = "archive"; } else { await loadCalendar(false); await loadArchive(); }
  }
  function openCameraForm(camera = null) {
    state.editing = camera?.id || null; $("camera-form").reset(); $("camera-form-error").hidden = true;
    $("camera-dialog-title").textContent = camera ? "Chỉnh sửa camera" : "Thêm camera";
    $("camera-id").value = camera?.id || ""; $("camera-id").readOnly = !!camera;
    // Existing stable IDs can contain uppercase letters. Never force a new-ID
    // convention on an immutable legacy ID during a display-name edit.
    if (camera) $("camera-id").removeAttribute("pattern");
    else $("camera-id").setAttribute("pattern", "[a-z0-9][a-z0-9_\\-]{0,63}");
    $("camera-name").value = camera?.name || ""; $("camera-model").value = camera?.model || ""; $("camera-host").value = camera?.host || "";
    for (const [name, fallback] of [["device_port", 8000], ["rtsp_port", 554], ["http_port", 80]]) $("camera-" + name.replaceAll("_", "-")).value = camera?.[name] || fallback;
    $("camera-enabled").checked = camera?.enabled !== false;
    $("camera-dialog").showModal(); (camera ? $("camera-name") : $("camera-id")).focus();
  }
  async function saveCamera(event) {
    event.preventDefault(); if (!$("camera-form").reportValidity()) return;
    const values = new FormData($("camera-form"));
    const data = { id: String(values.get("id") || "").trim(), name: String(values.get("name") || "").trim(), model: String(values.get("model") || "").trim(), host: String(values.get("host") || "").trim(), enabled: $("camera-enabled").checked };
    for (const name of ["device_port", "rtsp_port", "http_port"]) data[name] = Number(values.get(name));
    const errorBox = $("camera-form-error"); errorBox.hidden = true;
    if (!data.name || !helpers.validHost(data.host)) { errorBox.textContent = !data.name ? "Nhập tên hiển thị cho camera." : "Địa chỉ LAN cần là IPv4 hợp lệ hoặc hostname, không có http://, đường dẫn hay mật khẩu."; errorBox.hidden = false; errorBox.focus(); return; }
    $("save-camera-button").disabled = true;
    try {
      const wasEditing = !!state.editing;
      if (state.editing) {
        const original = state.cameras.find(camera => camera.id === state.editing) || {};
        const patch = helpers.cameraPatch(original, data);
        if (!Object.keys(patch).length) { $("camera-dialog").close(); toast("Không có thay đổi để lưu."); return; }
        await api(`/api/cameras/${encodeURIComponent(state.editing)}`, { method: "PATCH", body: patch });
        if (["host", "device_port", "rtsp_port", "http_port"].some(key => key in patch)) state.probes.delete(state.editing);
      }
      else await api("/api/cameras", { method: "POST", body: data });
      $("camera-dialog").close(); await refresh(); toast(wasEditing ? "Đã cập nhật camera. Mã và lịch sử video được giữ nguyên." : "Đã thêm camera. Chọn “Kiểm tra LAN” để xem phản hồi cổng.");
    } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; errorBox.focus(); }
    finally { $("save-camera-button").disabled = false; }
  }
  async function probeCamera(camera, control) {
    control.disabled = true; const oldText = control.querySelector("span").textContent; control.querySelector("span").textContent = "Đang kiểm tra…";
    try { const result = await api(`/api/cameras/${encodeURIComponent(camera.id)}/probe`, { method: "POST", body: {} }); state.probes.set(camera.id, result); renderCameras(); renderProbes(); const summary = probeSummary(result); toast(`${camera.name || camera.id}: ${summary[0]}. Tải lịch sử SD vẫn cần kiểm chứng riêng.`); }
    catch (error) { toast(error.message, true); }
    finally { control.disabled = false; control.querySelector("span").textContent = oldText; }
  }
  function setOptions(select, placeholder, entries, preserve = "") {
    select.replaceChildren(option("", placeholder)); for (const [value, label] of entries) select.append(option(value, label));
    if (entries.some(([value]) => String(value) === String(preserve))) select.value = preserve;
    select.disabled = !entries.length;
  }
  function updateCalendarOptions(preserve = false) {
    const oldYear = preserve ? $("filter-year").value : "", oldMonth = preserve ? $("filter-month").value : "", oldDay = preserve ? $("filter-day").value : "";
    const years = [...state.calendar].sort((a, b) => Number(b.year) - Number(a.year));
    setOptions($("filter-year"), "Tất cả năm", years.map(item => [item.year, item.year]), oldYear);
    const year = years.find(item => String(item.year) === $("filter-year").value);
    const months = [...(year?.months || [])].sort((a, b) => Number(a.month) - Number(b.month));
    setOptions($("filter-month"), "Tất cả tháng", months.map(item => [item.month, `Tháng ${String(item.month).padStart(2, "0")}`]), oldMonth);
    const month = months.find(item => String(item.month) === $("filter-month").value);
    const days = [...(month?.days || [])].map(value => typeof value === "object" ? value.day : value).sort((a, b) => Number(a) - Number(b));
    setOptions($("filter-day"), "Tất cả ngày", days.map(day => [day, `Ngày ${String(day).padStart(2, "0")}`]), oldDay);
  }
  async function loadCalendar(preserve = false) {
    const camera = $("filter-camera").value, request = ++state.calendarRequest;
    if (!camera) { state.calendar = []; updateCalendarOptions(false); return; }
    if (!preserve) { state.calendar = []; updateCalendarOptions(false); }
    const result = await api(`/api/calendar?camera=${encodeURIComponent(camera)}`);
    if (request !== state.calendarRequest || $("filter-camera").value !== camera) return;
    state.calendar = Array.isArray(result.years) ? result.years : []; updateCalendarOptions(preserve);
  }
  function breadcrumb() {
    const parts = [$("filter-camera").value ? cameraName($("filter-camera").value) : "Tất cả camera"];
    if ($("filter-year").value) parts.push($("filter-year").value);
    if ($("filter-month").value) parts.push(`Tháng ${$("filter-month").value.padStart(2, "0")}`);
    if ($("filter-day").value) parts.push(`Ngày ${$("filter-day").value.padStart(2, "0")}`);
    $("archive-breadcrumb").textContent = parts.join(" / ");
  }
  function renderRecordings(records) {
    $("archive-rows").replaceChildren();
    for (const record of records) {
      const row = node("tr"), first = node("td"), video = node("div", "video-cell"), symbol = node("span", "video-icon"), labels = node("div"); symbol.append(icon("video"));
      labels.append(node("div", "video-title", record.camera_name || cameraName(record.camera)), node("div", "video-id", String(record.key || record.record_key || "").slice(0, 24)));
      video.append(symbol, labels); first.append(video); row.append(first);
      const dateCell = node("td"), [date, time] = timeParts(record.start_ms); dateCell.append(node("span", "video-date", date), node("span", "video-time", time)); row.append(dateCell);
      row.append(node("td", "", helpers.formatDuration(record.start_ms, record.end_ms)), node("td", "", helpers.formatBytes(record.file_size ?? record.size_bytes)));
      const statusCell = node("td"); statusCell.append(chip(...helpers.status(record.status))); row.append(statusCell);
      const telegramCell = node("td", "align-right"), url = helpers.telegramUrl(record.telegram_url);
      if (url) { const link = node("a", "telegram-link", "Mở video"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.setAttribute("aria-label", `Mở video ${record.camera_name || cameraName(record.camera)} lúc ${time} ngày ${date} trên Telegram`); link.append(icon("external")); telegramCell.append(link); }
      else telegramCell.append(node("span", "small muted", "Chưa có liên kết")); row.append(telegramCell); $("archive-rows").append(row);
    }
    $("archive-table-wrap").hidden = !records.length; $("archive-empty").hidden = !!records.length;
    $("archive-result-count").textContent = `${displayCount(state.total)} video phù hợp`;
    $("archive-count-badge").textContent = `${displayCount(state.total)} video`;
    $("archive-pagination").hidden = !state.total;
    const first = records.length ? state.offset + 1 : 0, last = state.offset + records.length;
    $("pagination-summary").textContent = `${first}–${last} trong ${displayCount(state.total)} video`;
    $("previous-page").disabled = state.offset === 0; $("next-page").disabled = state.offset + state.limit >= state.total;
  }
  async function loadArchive() {
    if (!state.authenticated) return;
    const request = ++state.archiveRequest, params = new URLSearchParams();
    for (const field of ["camera", "year", "month", "day", "status", "order"]) { const value = $("filter-" + field).value; if (value) params.set(field, value); }
    params.set("offset", String(state.offset)); params.set("limit", String(state.limit));
    breadcrumb(); $("archive-loading").hidden = false; $("archive-table-wrap").hidden = true; $("archive-empty").hidden = true; $("archive-pagination").hidden = true; globalError("");
    try {
      const result = await api(`/api/archive?${params}`);
      if (request !== state.archiveRequest) return;
      state.total = Number(result.total) || 0; state.offset = Number(result.offset) || 0; state.limit = Number(result.limit) || 25;
      renderRecordings(Array.isArray(result.recordings) ? result.recordings : []);
    } catch (error) { if (request === state.archiveRequest && error.httpStatus !== 401) globalError(error.message); }
    finally { if (request === state.archiveRequest) $("archive-loading").hidden = true; }
  }

  $("login-form").addEventListener("submit", async event => {
    event.preventDefault(); const control = $("login-form").querySelector("button"); control.disabled = true; $("login-error").hidden = true;
    try { await api("/api/login", { method: "POST", body: { token: $("login-token").value } }); $("login-token").value = ""; await refresh(); }
    catch (error) { $("login-error").textContent = error.message; $("login-error").hidden = false; $("login-token").focus(); }
    finally { control.disabled = false; }
  });
  $("refresh-button").addEventListener("click", refresh); $("camera-search").addEventListener("input", renderCameras);
  $("logout-button").addEventListener("click", async () => {
    $("logout-button").disabled = true;
    try { await api("/api/logout", { method: "POST", body: {} }); state.csrf = ""; state.cameras = []; state.status = {}; state.probes.clear(); globalError(""); showLogin(); toast("Đã kết thúc phiên quản trị."); }
    catch (error) { if (error.httpStatus !== 401) toast(error.message, true); }
    finally { $("logout-button").disabled = false; }
  });
  $("add-camera-button").addEventListener("click", () => openCameraForm()); $("empty-add-camera").addEventListener("click", () => openCameraForm());
  for (const id of ["close-camera-dialog", "cancel-camera-dialog"]) $(id).addEventListener("click", () => $("camera-dialog").close());
  $("camera-form").addEventListener("submit", saveCamera);
  $("archive-filters").addEventListener("submit", event => event.preventDefault());
  $("filter-camera").addEventListener("change", async () => { state.offset = 0; try { await loadCalendar(false); await loadArchive(); } catch (error) { globalError(error.message); } });
  $("filter-year").addEventListener("change", () => { $("filter-month").value = ""; $("filter-day").value = ""; updateCalendarOptions(true); state.offset = 0; loadArchive(); });
  $("filter-month").addEventListener("change", () => { $("filter-day").value = ""; updateCalendarOptions(true); state.offset = 0; loadArchive(); });
  for (const name of ["day", "status", "order"]) $("filter-" + name).addEventListener("change", () => { state.offset = 0; loadArchive(); });
  $("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadArchive(); });
  $("next-page").addEventListener("click", () => { state.offset += state.limit; loadArchive(); });
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1)));
  switchView(location.hash.slice(1), false); refresh();
})();
