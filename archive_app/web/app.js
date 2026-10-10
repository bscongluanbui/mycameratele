/* No CDN, no stored credentials, no credentials in camera metadata. */
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
        return url.protocol === "https:" && url.hostname === "t.me" && !url.username && !url.password && !url.port && !url.hash && /^\/[A-Za-z][A-Za-z0-9_]{4,31}$/.test(url.pathname) && /^\?start=play_[a-f0-9]{32}$/.test(url.search) ? url.href : null;
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
      for (const key of ["name", "model", "host", "device_port", "rtsp_port", "http_port", "enabled", "upload_enabled", "sd_backend", "sd_username", "sd_channel", "sd_timezone", "sd_lookback_hours"]) {
        if (edited[key] !== undefined && edited[key] !== original[key]) patch[key] = edited[key];
      }
      if (typeof edited.sd_password === "string" && edited.sd_password) patch.sd_password = edited.sd_password;
      if (edited.sd_password_clear === true) patch.sd_password_clear = true;
      return patch;
    },
    uploadEnabled(camera) { return camera?.upload_enabled !== false; },
    syncActive(job) { return !!job && ["queued", "running"].includes(job.state); },
    syncState(job) {
      const values = { queued: ["Đang chờ", "neutral"], running: ["Đang chạy", "blue"], completed: ["Hoàn tất", "green"], blocked: ["Cần xử lý", "amber"], failed: ["Lỗi", "red"] };
      return values[job?.state] || ["Chưa Start", "neutral"];
    },
    syncPhase(job) {
      const phases = { sd_search: "Tìm recording SD", sd_complete: "Đã xử lý SD", scanning: "Kiểm tra nguồn", finished: "Kết thúc", queued: "Hàng đợi", probing: "Kiểm tra đường đến camera", preflight: "Kiểm tra nguồn SD", sd_download: "Tải SD", downloading: "Tải SD", download: "Tải SD", ingesting: "Lập chỉ mục", ingest: "Lập chỉ mục", normalize: "Chuẩn bị media (không encode)", uploading: "Upload Telegram", upload: "Upload Telegram", completed: "Kết thúc", done: "Kết thúc" };
      return phases[job?.phase] || job?.phase || "—";
    },
    syncHelp(job) {
      const code = String(job?.code || "");
      if (code.includes("sdk_missing") || code.includes("sdk_not_configured")) return "Dùng compose.sdk.yaml và HCNETSDK_DIR để gắn SDK đúng kiến trúc vào /opt/hcnetsdk theo README, rồi Start lại. Cổng Device mở không đồng nghĩa SDK đã sẵn sàng.";
      if (code.includes("source_missing") || code.includes("unsupported") || code.includes("isapi_unavailable")) return "Kiểm tra nguồn SD / HCNetSDK và tài khoản thiết bị. C6N có thể mở cổng Device 8000 nhưng không có ISAPI ở HTTP 80.";
      if (code.includes("password") || code.includes("credential") || code.includes("auth")) return "Chỉnh sửa Camera → Nguồn video SD để kiểm tra tên đăng nhập thiết bị và mật khẩu / mã xác thực.";
      if (code.includes("unreachable") || code.includes("connect") || code.includes("timeout") || code.includes("network")) return "Kiểm tra VPS nhận subnet route Tailscale từ Armbian và truy cập được IP LAN của camera. Camera không có HTTP / ISAPI cần SDK tương thích qua cổng Device.";
      return "";
    },
    syncStatistics(job) {
      const labels = { sd_searched: "Tìm thấy SD", sd_found: "Tìm thấy SD", sd_downloaded: "Đã tải SD", sd_imported: "Đã nhập SD", imported: "Đã nhập", uploaded: "Đã upload", pending: "Chờ upload", failed: "Lỗi", already_known: "Đã có" };
      return Object.entries(job?.statistics || {}).filter(([key, value]) => labels[key] && typeof value === "number" && Number.isFinite(value) && value >= 0).map(([key, value]) => `${labels[key]}: ${value}`).join(" · ");
    },
    syncUpdated(job, now = Date.now()) {
      const updated = helpers.checkedMillis(job?.updated_at);
      if (updated === null || !Number.isFinite(now) || !Number.isFinite(new Date(updated).getTime())) return "";
      const seconds = Math.max(0, Math.floor((now - updated) / 1000));
      const age = seconds < 60 ? `${seconds} giây` : seconds < 3600 ? `${Math.floor(seconds / 60)} phút` : `${Math.floor(seconds / 3600)} giờ`;
      return `Cập nhật ${age} trước · ${new Date(updated).toLocaleString("vi-VN")}`;
    },
    syncSummary(latest) {
      const jobs = Object.values(latest || {});
      const running = jobs.filter(job => job?.state === "running").length;
      const queued = jobs.filter(job => job?.state === "queued").length;
      const blocked = jobs.filter(job => ["blocked", "failed"].includes(job?.state)).length;
      return running || queued || blocked ? `${running} đang chạy · ${queued} chờ · ${blocked} cần xử lý` : "Không có sync đang chạy";
    },
    syncLine(job) {
      if (!job) return "Bấm Start sync để bắt đầu";
      const statistics = job.statistics || {}, parts = [helpers.syncPhase(job)];
      for (const [key, label] of [["sd_searched", "SD"], ["sd_downloaded", "Tải"], ["uploaded", "Upload"], ["failed", "Lỗi"]]) {
        const value = statistics[key];
        if (typeof value === "number" && Number.isFinite(value) && value >= 0) parts.push(`${label} ${value}`);
      }
      return parts.join(" · ");
    },
    syncDetailFields(job) {
      const labels = { manifests: "Manifest", matched: "Khớp nguồn", imported: "Đã nhập", already_known: "Đã có", uploaded: "Đã upload", failed: "Lỗi", ready: "Sẵn sàng", pending: "Chờ upload", remuxed: "Đã remux", remux_failed: "Lỗi remux", remux_budget_blocked: "Chờ dung lượng cache", needs_review: "Cần kiểm tra", upload_unknown: "Upload chưa xác nhận", deleted: "Đã xóa", failed_records: "Bản ghi lỗi", probe_tcp_open: "Cổng TCP mở", probe_error_type: "Lỗi kết nối", sd_backend: "Bộ tải SD", sd_searched: "Tìm thấy SD", sd_downloaded: "Đã tải SD", sd_imported: "Đã nhập SD", sd_deferred: "SD chờ tải", sd_backlog: "SD tồn đọng", sd_error_code: "Mã lỗi SD" };
      return Object.entries(job?.statistics || {}).filter(([key, value]) => Object.prototype.hasOwnProperty.call(labels, key) && (value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value) && value >= 0))).map(([key, value]) => [labels[key], value === null || value === "" ? "—" : String(value)]);
    },
    syncTimestamp(value, compact = false) {
      const milliseconds = helpers.checkedMillis(value);
      if (milliseconds === null || !Number.isFinite(new Date(milliseconds).getTime())) return "—";
      return new Date(milliseconds).toLocaleString("vi-VN", compact ? { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" } : undefined);
    },
    syncLogFilter(jobs, camera = "", status = "") {
      return (Array.isArray(jobs) ? jobs : []).filter(job => (!camera || job.camera_id === camera) && (!status || (status === "active" ? helpers.syncActive(job) : status === "errors" ? ["blocked", "failed"].includes(job.state) : job.state === status)));
    },
    discoveryActive(scan) { return !!scan && scan.state === "running"; },
    discoveryTarget(value, maximum = 1024) {
      const target = String(value || "").trim();
      const ipv4 = text => {
        if (!/^\d+(\.\d+){3}$/.test(text)) return null;
        const parts = text.split(".");
        if (parts.some(part => String(Number(part)) !== part || Number(part) > 255)) return null;
        return parts.reduce((total, part) => total * 256 + Number(part), 0);
      };
      const privateIp = ip => (ip >= 167772160 && ip <= 184549375) || (ip >= 2886729728 && ip <= 2887778303) || (ip >= 3232235520 && ip <= 3232301055);
      let first, last, count;
      if (target.includes("/")) {
        const parts = target.split("/"); const address = ipv4(parts[0]);
        if (parts.length !== 2 || address === null || !/^\d{1,2}$/.test(parts[1]) || Number(parts[1]) > 32) return {error: "Nhập CIDR hợp lệ, ví dụ 192.168.31.0/24."};
        const prefix = Number(parts[1]), size = 2 ** (32 - prefix);
        first = Math.floor(address / size) * size; last = first + size - 1; count = size > 2 ? size - 2 : size;
      } else {
        const parts = target.split(/\s*-\s*/);
        if (parts.length > 2 || (first = ipv4(parts[0])) === null || (last = ipv4(parts[1] || parts[0])) === null || last < first) return {error: "Nhập IPv4, CIDR hoặc dải IP đầu-cuối hợp lệ."};
        count = last - first + 1;
      }
      if (!privateIp(first) || !privateIp(last)) return {error: "Chọn dải LAN riêng: 10.x, 172.16–31.x hoặc 192.168.x."};
      if (count > maximum) return {error: `Mỗi lượt quét tối đa ${maximum} IP. Chọn dải nhỏ hơn.`};
      return {target, count};
    },
    discoveryKnown(result, cameras) {
      return result.existing_camera_id || (Array.isArray(cameras) ? cameras : []).find(camera => String(camera.host || "").toLowerCase() === String(result.host || "").toLowerCase() && Number(camera.device_port || 8000) === Number(result.device_port || 8000))?.id || "";
    },
    discoveryPayload(result, name, cameras, username = "admin", password = "") {
      if (!helpers.validHost(result.host) || !/^\d+(\.\d+){3}$/.test(result.host)) throw new Error("IP camera chưa hợp lệ. Quét lại dải IP.");
      if (helpers.discoveryKnown(result, cameras)) throw new Error("Camera này đã có trong danh sách.");
      if (!String(name).trim() || String(name).trim().length > 120) throw new Error("Tên camera cần có 1–120 ký tự.");
      if (!helpers.validSdPassword(password)) throw new Error("Mật khẩu thiết bị cần dài tối đa 64 byte UTF-8 và không chứa ký tự NUL.");
      const ids = new Set((Array.isArray(cameras) ? cameras : []).map(camera => camera.id));
      const base = `cam-${result.host.replaceAll(".", "-")}`;
      let id = base, suffix = 2;
      while (ids.has(id)) id = `${base}-${suffix++}`;
      const payload = {id, name: String(name).trim(), model: String(result.model || "").slice(0, 100), host: result.host, device_port: result.device_port || 8000, rtsp_port: result.rtsp_port || 554, http_port: result.http_port || 80, enabled: !!password, upload_enabled: true, sd_backend: "auto", sd_username: String(username).trim() || "admin"};
      if (password) payload.sd_password = password;
      return payload;
    },
    validSdPassword(password) { return typeof password === "string" && !password.includes("\0") && new TextEncoder().encode(password).length <= 64; },
    validUsername(value) { return typeof value === "string" && /^[A-Za-z0-9_.-]{3,64}$/.test(value); },
    accountValidation(values) {
      if (!helpers.validUsername(values.username)) return { field: "account-username", message: "Tên đăng nhập cần có 3–64 ký tự: chữ, số, dấu _, dấu . hoặc dấu -." };
      if (typeof values.current_password !== "string" || !values.current_password) return { field: "account-current-password", message: "Nhập mật khẩu hiện tại để xác nhận thay đổi." };
      if (typeof values.new_password !== "string" || Array.from(values.new_password).length < 8 || Array.from(values.new_password).length > 128) return { field: "account-new-password", message: "Mật khẩu mới cần có 8–128 ký tự." };
      if (values.new_password !== values.confirm_password) return { field: "account-confirm-password", message: "Mật khẩu nhập lại chưa khớp với mật khẩu mới." };
      return null;
    }
  };
  // Export pure helpers for dependency-free Node regression tests.
  if (typeof module !== "undefined" && module.exports) module.exports = helpers;
  if (typeof document === "undefined") return;

  const $ = id => document.getElementById(id);
  const state = { cameras: [], status: {}, csrf: "", probes: new Map(), calendar: [], offset: 0, limit: 25, total: 0, archiveRequest: 0, calendarRequest: 0, editing: null, view: "cameras", authenticated: false, sessionRevision: 0, account: null, accountRequired: false, loginBusy: false, accountBusy: false, logoutBusy: false, sync: {jobs: [], latest: {}, worker_alive: false}, syncRequest: 0, syncTimer: null, syncBusy: new Set(), syncSummary: "", expandedSync: new Set(), discovery: {scan: null, choices: new Map(), timer: null, revision: 0, starting: false, adding: false, cancelling: false} };
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
  function clearPasswords(form) {
    for (const input of form.querySelectorAll("input[data-password-field], input[type=password]")) { input.value = ""; input.type = "password"; }
    for (const toggle of form.querySelectorAll("[data-password-target]")) { toggle.textContent = "Hiện"; toggle.setAttribute("aria-pressed", "false"); toggle.setAttribute("aria-label", toggle.dataset.showLabel); }
  }
  function clearFormError(form, errorBox) {
    errorBox.hidden = true; errorBox.textContent = "";
    for (const input of form.querySelectorAll("[aria-invalid]")) input.removeAttribute("aria-invalid");
  }
  function formError(form, errorBox, message, field = "") {
    clearFormError(form, errorBox); errorBox.textContent = message; errorBox.hidden = false;
    if (field && $(field)) { $(field).setAttribute("aria-invalid", "true"); $(field).focus(); } else errorBox.focus();
  }
  function showLogin({ username = state.account?.username || "", message = "" } = {}) {
    resetDiscovery();
    stopSyncPolling(); state.syncRequest++; state.sync = {jobs: [], latest: {}, worker_alive: false}; state.syncBusy.clear(); state.expandedSync.clear();
    state.authenticated = false; state.sessionRevision++; state.archiveRequest++; state.calendarRequest++;
    state.account = null; state.accountRequired = false; state.csrf = ""; state.cameras = []; state.status = {}; state.calendar = []; state.probes.clear();
    $("app-shell").hidden = true; $("account-screen").hidden = true; $("boot-loading").hidden = true; $("login-screen").hidden = false;
    if ($("camera-dialog").open) $("camera-dialog").close();
    clearPasswords($("login-form")); clearPasswords($("account-form"));
    clearFormError($("login-form"), $("login-error")); clearFormError($("account-form"), $("account-error"));
    $("login-username").value = username; $("login-status").textContent = message; $("login-status").hidden = !message;
    (username ? $("login-password") : $("login-username")).focus();
  }
  function showAccount(account, required = !!account?.password_change_required) {
    closeDiscovery();
    stopSyncPolling(); state.syncRequest++;
    state.account = account; state.accountRequired = required; state.archiveRequest++; state.calendarRequest++;
    $("app-shell").hidden = true; $("login-screen").hidden = true; $("boot-loading").hidden = true; $("account-screen").hidden = false;
    if ($("camera-dialog").open) $("camera-dialog").close();
    $("account-form").reset(); clearPasswords($("account-form")); clearFormError($("account-form"), $("account-error"));
    $("account-username").value = account?.username || "admin";
    $("account-title").textContent = required ? "Thiết lập tài khoản." : "Tài khoản quản trị.";
    $("account-intro").textContent = required ? "Bạn đã đăng nhập. Đổi mật khẩu mặc định để tiếp tục quản lý camera và thư viện video." : "Đổi tên đăng nhập và mật khẩu cho dashboard.";
    $("account-required-note").hidden = !required; $("account-cancel").hidden = required; $("account-status").hidden = true;
    $("account-new-password").focus();
  }
  async function acceptAccount(account, csrf) {
    state.authenticated = true; state.account = account; state.csrf = csrf || state.csrf; state.accountRequired = !!account?.password_change_required;
    $("session-username").textContent = account?.username || "Phiên quản trị";
    if (state.accountRequired) showAccount(account, true); else { $("account-screen").hidden = true; await refresh(); }
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
      const code = result.code || "";
      if (response.status === 401 && path !== "/api/login" && code !== "current_password_invalid") showLogin({ message: state.authenticated ? "Phiên đăng nhập đã kết thúc. Đăng nhập lại để tiếp tục." : "" });
      if (response.status === 409 && code === "password_change_required") showAccount({ ...(state.account || { username: "admin" }), password_change_required: true }, true);
      const messages = { current_password_invalid: "Mật khẩu hiện tại chưa đúng. Kiểm tra lại rồi thử lưu.", authentication_failed: "Tên đăng nhập hoặc mật khẩu chưa đúng.", invalid_credentials: "Tên đăng nhập hoặc mật khẩu chưa đúng.", authentication_required: "Phiên đăng nhập đã kết thúc. Đăng nhập lại để tiếp tục.", password_change_required: "Đổi mật khẩu mặc định trước khi mở dashboard." };
      const error = new Error(messages[code] || result.error || result.message || `Yêu cầu chưa hoàn tất (HTTP ${response.status}).`);
      error.httpStatus = response.status; error.code = code; error.existingCameraId = result.existing_camera_id; throw error;
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
  function rememberSyncRows(target) {
    const focused = document.activeElement;
    let focusKey = null;
    for (const row of target.querySelectorAll("details[data-sync-key]")) {
      if (row.open) state.expandedSync.add(row.dataset.syncKey); else state.expandedSync.delete(row.dataset.syncKey);
      if (row.querySelector("summary") === focused) focusKey = row.dataset.syncKey;
    }
    return focusKey;
  }
  function restoreSyncFocus(target, key) {
    if (!key) return;
    for (const row of target.querySelectorAll("details[data-sync-key]")) {
      if (row.dataset.syncKey === key) { row.querySelector("summary")?.focus({preventScroll: true}); break; }
    }
  }
  function syncDisclosure(job, camera, scope) {
    const details = node("details", scope === "logs" ? "sync-log-row" : "camera-sync-row");
    const key = scope === "logs" ? `logs:${job.id}` : `camera:${camera.id}`;
    details.dataset.syncKey = key; details.open = state.expandedSync.has(key);
    const summary = node("summary", "sync-row-summary");
    const name = job?.camera_name || camera.name || camera.id;
    if (scope === "logs") {
      const time = node("time", "sync-log-time", helpers.syncTimestamp(job.created_at, true));
      const milliseconds = helpers.checkedMillis(job.created_at);
      if (milliseconds !== null && Number.isFinite(new Date(milliseconds).getTime())) time.dateTime = new Date(milliseconds).toISOString();
      time.title = helpers.syncTimestamp(job.created_at);
      summary.append(time, node("strong", "sync-log-camera", name));
    }
    summary.append(chip(...helpers.syncState(job)), node("span", "sync-row-line", helpers.syncLine(job)), node("span", "sync-detail-label", "Detail"));
    summary.setAttribute("aria-label", `Chi tiết đồng bộ ${name}${scope === "logs" ? ` · ${helpers.syncTimestamp(job.created_at)}` : ""} · ${helpers.syncState(job)[0]} · ${helpers.syncLine(job)}`);
    const body = node("div", "sync-row-detail");
    if (!job) body.append(node("p", "small muted", "Chưa có công việc sync. Bấm Start sync để bắt đầu."));
    else {
      body.append(node("p", "sync-detail-message", job.message || "Chưa có thông báo"));
      const fields = node("dl", "sync-detail-fields");
      const metadata = [["Camera", name], ["Trạng thái", helpers.syncState(job)[0]], ["Giai đoạn", helpers.syncPhase(job)], ["Nguồn", job.source || "—"], ["Tạo lúc", helpers.syncTimestamp(job.created_at)], ["Bắt đầu", helpers.syncTimestamp(job.started_at)], ["Cập nhật", helpers.syncTimestamp(job.updated_at)], ["Kết thúc", helpers.syncTimestamp(job.finished_at)]];
      if (job.code) metadata.push(["Mã kết quả", job.code]);
      if (job.id) metadata.push(["Mã công việc", job.id]);
      for (const [label, value] of [...metadata, ...helpers.syncDetailFields(job)]) {
        const field = node("div"); field.append(node("dt", "", label), node("dd", "", value)); fields.append(field);
      }
      body.append(fields);
      if (helpers.syncHelp(job)) body.append(node("p", "field-hint sync-detail-help", helpers.syncHelp(job)));
    }
    details.append(summary, body);
    details.addEventListener("toggle", () => {
      // Ignore delayed toggle events from nodes replaced by a polling refresh.
      if (!document.contains(details)) return;
      if (details.open) state.expandedSync.add(key); else state.expandedSync.delete(key);
    });
    return details;
  }
  function renderCameras() {
    const focused = document.activeElement;
    const focusKey = focused?.dataset?.cameraStart ? ["cameraStart", focused.dataset.cameraStart] : focused?.dataset?.cameraUpload ? ["cameraUpload", focused.dataset.cameraUpload] : null;
    const syncFocus = rememberSyncRows($("camera-grid"));
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
      const job = state.sync.latest?.[camera.id] || camera.sync;
      const syncDetail = syncDisclosure(job, camera, "camera");
      const upload = node("button", "upload-switch"); upload.type = "button"; upload.setAttribute("role", "switch");
      upload.setAttribute("aria-checked", String(helpers.uploadEnabled(camera))); upload.setAttribute("aria-label", `Upload Telegram cho ${camera.name || camera.id}`);
      upload.dataset.cameraUpload = camera.id; upload.disabled = state.syncBusy.has("upload:" + camera.id);
      upload.append(node("span", "switch-track"), node("span", "", `Upload ${helpers.uploadEnabled(camera) ? "ON" : "OFF"}`));
      upload.addEventListener("click", () => toggleUpload(camera, upload));
      const uploadBox = node("div", "camera-upload-control"); uploadBox.append(upload, node("p", "field-hint", "OFF chỉ dừng upload; tải SD vẫn tiếp tục."));
      const actions = node("div", "camera-card-bottom");
      const start = button(helpers.syncActive(job) ? "Đã xếp hàng" : "Start sync", "primary", () => startSync(camera.id, start), "network");
      start.dataset.cameraStart = camera.id; start.disabled = camera.enabled === false || helpers.syncActive(job) || state.syncBusy.has(camera.id);
      start.setAttribute("aria-label", `Start sync ${camera.name || camera.id}`); actions.append(start);
      actions.append(button("Thư viện", "primary", () => openCameraArchive(camera.id), "folder"), button("Chỉnh sửa", "ghost", () => openCameraForm(camera), "edit"));
      const probeButton = button("Kiểm tra LAN", "ghost", () => probeCamera(camera, probeButton), "network"); actions.append(probeButton);
      card.append(top, info, syncDetail, uploadBox, actions); $("camera-grid").append(card);
    }
    renderStats();
    $("start-all-button").disabled = !state.cameras.some(camera => camera.enabled !== false) || state.syncBusy.has("all");
    if (focusKey) for (const control of $("camera-grid").querySelectorAll("button")) { if (control.dataset[focusKey[0]] === focusKey[1] && !control.disabled) { control.focus({preventScroll: true}); break; } }
    restoreSyncFocus($("camera-grid"), syncFocus);
  }
  function stopSyncPolling() { if (state.syncTimer !== null) clearTimeout(state.syncTimer); state.syncTimer = null; }
  function scheduleSyncPolling() {
    stopSyncPolling();
    if (!state.authenticated || state.accountRequired || !$("account-screen").hidden) return;
    const active = Object.values(state.sync.latest || {}).some(helpers.syncActive);
    state.syncTimer = setTimeout(() => { state.syncTimer = null; loadSync(); }, active ? 2500 : 15000);
  }
  function renderSync() {
    const summary = helpers.syncSummary(state.sync.latest);
    if (summary !== state.syncSummary) { state.syncSummary = summary; $("sync-summary").textContent = summary; }
    $("sync-worker-status").textContent = state.sync.worker_alive ? "Worker hoạt động" : "Chưa thấy heartbeat worker";
    $("sync-worker-status").className = `status-chip ${state.sync.worker_alive ? "green" : "amber"}`;
    $("logs-worker-status").textContent = $("sync-worker-status").textContent;
    $("logs-worker-status").className = $("sync-worker-status").className;
    const oldCamera = $("logs-camera").value;
    $("logs-camera").replaceChildren(option("", "Tất cả camera"));
    for (const camera of state.cameras) $("logs-camera").append(option(camera.id, camera.name || camera.id));
    // Deleted cameras may still have history, which remains available in Logs.
    const known = new Set(state.cameras.map(camera => camera.id));
    for (const job of state.sync.jobs || []) if (!known.has(job.camera_id)) { known.add(job.camera_id); $("logs-camera").append(option(job.camera_id, job.camera_name || job.camera_id)); }
    if (known.has(oldCamera)) $("logs-camera").value = oldCamera;
    renderLogs();
  }
  function renderLogs() {
    const allJobs = Array.isArray(state.sync.jobs) ? state.sync.jobs : [];
    const jobs = helpers.syncLogFilter(allJobs, $("logs-camera").value, $("logs-status").value);
    const target = $("sync-jobs"), focusKey = rememberSyncRows(target); target.replaceChildren();
    for (const job of jobs) target.append(syncDisclosure(job, {id: job.camera_id, name: cameraName(job.camera_id)}, "logs"));
    $("logs-empty").hidden = jobs.length > 0;
    $("logs-empty").textContent = allJobs.length ? "Không có lượt đồng bộ phù hợp với bộ lọc." : "Chưa có lịch sử đồng bộ. Bấm Start sync để bắt đầu.";
    $("logs-count").textContent = `${jobs.length} / ${allJobs.length} lượt gần nhất`;
    restoreSyncFocus(target, focusKey);
  }
  async function loadSync() {
    if (!state.authenticated || state.accountRequired || !$("account-screen").hidden) return;
    const revision = state.sessionRevision, request = ++state.syncRequest;
    try {
      const result = await api("/api/sync?limit=100");
      if (!state.authenticated || state.accountRequired || revision !== state.sessionRevision || request !== state.syncRequest || !$("account-screen").hidden) return;
      const previous = JSON.stringify(Object.values(state.sync.latest || {}).map(job => [job.id, job.state, job.statistics]));
      state.sync = { jobs: Array.isArray(result.jobs) ? result.jobs : [], latest: result.latest || {}, worker_alive: result.worker_alive === true };
      $("sync-error").hidden = true; $("logs-error").hidden = true; renderSync(); renderCameras();
      const next = JSON.stringify(Object.values(state.sync.latest).map(job => [job.id, job.state, job.statistics]));
      if (previous !== next && state.sync.jobs.length && !Object.values(state.sync.latest).some(helpers.syncActive)) await refresh();
    } catch (error) {
      if (revision === state.sessionRevision && error.httpStatus !== 401 && error.code !== "password_change_required") {
        for (const id of ["sync-error", "logs-error"]) { $(id).textContent = error.message; $(id).hidden = false; }
      }
    } finally { if (revision === state.sessionRevision && request === state.syncRequest) scheduleSyncPolling(); }
  }
  async function startSync(cameraId = null, control = $("start-all-button")) {
    if (!state.authenticated || state.accountRequired) return;
    const key = cameraId || "all", revision = state.sessionRevision;
    if (state.syncBusy.has(key)) return;
    state.syncBusy.add(key); control.disabled = true;
    try {
      const result = await api("/api/sync", { method: "POST", body: { camera_id: cameraId } });
      if (!state.authenticated || revision !== state.sessionRevision) return;
      const count = Array.isArray(result.jobs) ? result.jobs.length : 0;
      toast(`Đã tiếp nhận Start cho ${count} camera. Theo dõi tiến trình đồng bộ.`); await loadSync();
    } catch (error) { if (revision === state.sessionRevision && error.httpStatus !== 401) toast(error.message, true); }
    finally { state.syncBusy.delete(key); if (revision === state.sessionRevision) { control.disabled = false; renderCameras(); } }
  }
  async function toggleUpload(camera, control) {
    if (!state.authenticated || state.accountRequired) return;
    const key = "upload:" + camera.id, revision = state.sessionRevision;
    if (state.syncBusy.has(key)) return;
    state.syncBusy.add(key); control.disabled = true;
    try {
      await api(`/api/cameras/${encodeURIComponent(camera.id)}`, { method: "PATCH", body: { upload_enabled: !helpers.uploadEnabled(camera) } });
      if (!state.authenticated || revision !== state.sessionRevision) return;
      toast(`Upload ${!helpers.uploadEnabled(camera) ? "ON" : "OFF"} cho ${camera.name || camera.id}. Tải SD không bị tạm dừng.`); await refresh();
    } catch (error) { if (revision === state.sessionRevision && error.httpStatus !== 401) toast(error.message, true); }
    finally { state.syncBusy.delete(key); if (revision === state.sessionRevision) renderCameras(); }
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
      ["Nơi lưu video", status.telegram_destination === "channel" ? "Private channel" : ["owner_private", "owner_private_chat"].includes(status.telegram_destination) ? "Chat riêng của owner" : "—"],
      ["Owner /start", status.owner_started === true ? "Đã kết nối" : "Chờ owner mở bot và /start"],
      ["ID được phép xem", status.allowed_users_count ?? "—"],
      ["Bot API", status.api_mode || status.telegram_api_mode || "—"],
      ["Kiến trúc", status.architecture || status.machine || "—"],
      ["Hàng đợi upload", status.queue?.downloaded ?? status.queue?.pending ?? status.queue_count ?? "—"],
      ["Heartbeat worker", status.heartbeat?.worker_alive === true ? "Đang hoạt động" : status.heartbeat?.worker_alive === false ? "Không thấy heartbeat mới" : status.worker?.heartbeat || status.heartbeat || "—"]
    ];
    $("system-details").replaceChildren();
    for (const [label, value] of details) { const row = node("div"); row.append(node("dt", "", label), node("dd", "", typeof value === "object" ? "Có dữ liệu" : value)); $("system-details").append(row); }
    const adapter = typeof status.sd_adapter === "object" ? (status.sd_adapter.status || status.sd_adapter.name || "auto") : String(status.sd_adapter || "auto");
    $("system-sd-status").textContent = "Theo dõi tiến trình sync";
    $("system-sd-description").textContent = `Nguồn SD: ${adapter}. Worker chọn ISAPI hoặc HCNetSDK theo cấu hình camera; House01 dùng MP4 remux-copy (không encode/AAC/full decode) rồi lưu private channel. Bot trả media khi có yêu cầu. Cache trung chuyển mặc định 1 giờ. HCNetSDK cần compose.sdk.yaml / HCNETSDK_DIR và SDK đúng kiến trúc tại /opt/hcnetsdk. VPS cần route camera LAN qua Tailscale / Armbian; cổng mở không xác nhận đã tải được SD.`;
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
    if (!state.authenticated || state.accountRequired) return;
    const revision = state.sessionRevision;
    $("refresh-button").disabled = true; $("camera-loading").hidden = false; globalError("");
    try {
      const status = await api("/api/status");
      if (!state.authenticated || state.accountRequired || revision !== state.sessionRevision) return;
      const response = await api("/api/cameras");
      if (!state.authenticated || state.accountRequired || revision !== state.sessionRevision) return;
      state.status = status; state.csrf = status.csrf_token || state.csrf; state.cameras = Array.isArray(response.cameras) ? response.cameras : [];
      $("boot-loading").hidden = true; $("login-screen").hidden = true; if ($("account-screen").hidden) $("app-shell").hidden = false;
      renderCameras(); refreshCameraOptions(); renderSystem();
      await loadSync();
      if (state.view === "archive") { await loadCalendar(true); await loadArchive(); }
    } catch (error) {
      if (error.httpStatus !== 401 && error.code !== "password_change_required") {
        if (!state.authenticated) { $("boot-loading").hidden = true; $("login-screen").hidden = false; $("login-error").hidden = false; $("login-error").textContent = error.message; }
        else { $("boot-loading").hidden = true; $("login-screen").hidden = true; if ($("account-screen").hidden) $("app-shell").hidden = false; globalError(error.message); }
        $("connection-state").textContent = "Mất kết nối dashboard";
      }
    } finally { $("refresh-button").disabled = false; $("camera-loading").hidden = true; }
  }
  function switchView(name, load = true) {
    if (!["cameras", "archive", "logs", "system"].includes(name)) name = "cameras";
    state.view = name;
    for (const item of document.querySelectorAll(".view")) item.hidden = item.id !== `view-${name}`;
    for (const link of document.querySelectorAll("[data-view]")) { const active = link.dataset.view === name; link.classList.toggle("active", active); if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current"); }
    $("topbar-page").textContent = { cameras: "Camera", archive: "Thư viện video", logs: "Logs", system: "Hệ thống" }[name];
    if (name === "archive" && load && state.authenticated && !state.accountRequired) { loadCalendar(true).then(loadArchive).catch(error => globalError(error.message)); }
    if (name === "system") renderSystem();
    if (name === "logs") { renderLogs(); if (load && state.authenticated && !state.accountRequired) loadSync(); }
  }
  async function openCameraArchive(id) {
    $("filter-camera").value = id; state.offset = 0;
    for (const name of ["year", "month", "day"]) $("filter-" + name).value = "";
    if (location.hash !== "#archive") { location.hash = "archive"; } else { await loadCalendar(false); await loadArchive(); }
  }
  function stopDiscoveryPolling() { clearTimeout(state.discovery.timer); state.discovery.timer = null; }
  function resetDiscovery() {
    stopDiscoveryPolling(); state.discovery.revision++;
    state.discovery.scan = null; state.discovery.choices.clear(); state.discovery.starting = false; state.discovery.adding = false; state.discovery.cancelling = false;
    $("discovery-sd-password").value = "";
    if ($("discovery-dialog").open) $("discovery-dialog").close();
  }
  function closeDiscovery() {
    const scan = state.discovery.scan;
    if (helpers.discoveryActive(scan) && state.authenticated) api(`/api/discovery/scans/${encodeURIComponent(scan.id)}/cancel`, {method: "POST", body: {}}).catch(() => {});
    resetDiscovery();
  }
  function discoveryError(message, field = "") {
    $("discovery-error").textContent = message || ""; $("discovery-error").hidden = !message;
    $("discovery-target").removeAttribute("aria-invalid");
    if (message && field) { $(field).setAttribute("aria-invalid", "true"); $(field).focus(); }
  }
  function discoveryControls() {
    const d = state.discovery, active = helpers.discoveryActive(d.scan);
    const editable = [...d.choices.values()].filter(choice => !choice.added && !helpers.discoveryKnown(choice.result, state.cameras));
    const selected = editable.filter(choice => choice.selected).length;
    for (const id of ["discovery-target", "discovery-subnet", "discovery-refresh-routes", "discovery-start"]) $(id).disabled = d.starting || d.adding || active;
    $("discovery-start").textContent = d.starting ? "Đang bắt đầu…" : d.scan ? "Quét lại" : "Bắt đầu quét";
    $("discovery-cancel").hidden = !active; $("discovery-cancel").disabled = d.cancelling;
    $("discovery-cancel").textContent = d.cancelling ? "Đang dừng…" : "Dừng quét";
    $("discovery-add").textContent = d.adding ? "Đang thêm camera…" : `Thêm camera đã chọn (${selected})`;
    $("discovery-add").disabled = !selected || d.adding || d.starting || active;
    $("discovery-select-all").disabled = !editable.length || d.adding;
    $("discovery-select-all").checked = editable.length > 0 && selected === editable.length;
    $("discovery-select-all").indeterminate = selected > 0 && selected < editable.length;
    for (const id of ["close-discovery-dialog", "discovery-done", "discovery-sd-username", "discovery-sd-password"]) $(id).disabled = d.adding;
  }
  function renderDiscoveryResults() {
    const target = $("discovery-results");
    for (const choice of state.discovery.choices.values()) {
      const result = choice.result, known = helpers.discoveryKnown(result, state.cameras);
      if (!choice.element) {
        const row = node("article", "discovery-result"), top = node("div", "discovery-result-top"), selectLabel = node("label", "discovery-result-select");
        const check = node("input"); check.type = "checkbox"; check.setAttribute("aria-label", `Chọn camera ${result.host}`);
        selectLabel.append(check, node("strong", "mono", result.host));
        const identity = node("span", "discovery-result-identity", [result.vendor, result.model].filter(Boolean).join(" · ") || "Chưa xác định model");
        const confidence = chip(result.confidence === "identified" ? "Nhận diện camera" : "Có thể là camera", result.confidence === "identified" ? "green" : "amber");
        top.append(selectLabel, confidence);
        const nameField = node("div", "field"), nameLabel = node("label", "", `Tên camera · ${result.host}`), nameInput = node("input");
        nameInput.id = `discovery-name-${result.host.replaceAll(".", "-")}`; nameInput.value = choice.name; nameInput.maxLength = 120; nameLabel.htmlFor = nameInput.id;
        nameField.append(nameLabel, nameInput);
        const ports = node("p", "field-hint", `Cổng mở: ${(Array.isArray(result.ports) ? result.ports : []).join(", ") || "—"}`), status = node("p", "discovery-result-status small"); status.setAttribute("role", "status");
        row.append(top, identity, ports, nameField, status); target.append(row);
        check.addEventListener("change", () => { choice.selected = check.checked; discoveryControls(); });
        nameInput.addEventListener("input", () => { choice.name = nameInput.value; });
        choice.element = row; choice.check = check; choice.nameInput = nameInput; choice.status = status;
      }
      choice.check.checked = !!choice.selected && !known && !choice.added;
      choice.check.disabled = !!known || choice.added || state.discovery.adding;
      choice.nameInput.disabled = !!known || choice.added || state.discovery.adding;
      choice.status.textContent = choice.error || (choice.added ? "Đã thêm" : known ? `Đã cấu hình · ${cameraName(known)}` : "");
      choice.status.classList.toggle("error-text", !!choice.error);
      choice.status.hidden = !choice.status.textContent;
    }
    $("discovery-empty").hidden = state.discovery.choices.size > 0 || helpers.discoveryActive(state.discovery.scan);
    discoveryControls();
  }
  function renderDiscoveryJob(scan) {
    state.discovery.scan = scan;
    $("discovery-progress-section").hidden = false; $("discovery-results-section").hidden = false;
    const names = {running: ["Đang quét", "blue"], completed: ["Hoàn tất", "green"], cancelled: ["Đã dừng", "neutral"], failed: ["Lỗi quét", "red"]};
    const [label, tone] = names[scan.state] || ["Đang chờ", "neutral"];
    $("discovery-state").textContent = label; $("discovery-state").className = `status-chip ${tone}`;
    const total = Math.max(0, Number(scan.total) || 0), scanned = Math.min(total, Math.max(0, Number(scan.scanned) || 0));
    $("discovery-progress").max = Math.max(1, total); $("discovery-progress").value = scanned;
    for (const result of Array.isArray(scan.results) ? scan.results : []) {
      if (!result?.host) continue;
      const previous = state.discovery.choices.get(result.host);
      if (previous) previous.result = result;
      else state.discovery.choices.set(result.host, {result, name: `${result.model || "Camera"} ${result.host}`, selected: false, added: false, error: ""});
    }
    $("discovery-progress-label").textContent = `${scanned.toLocaleString("vi-VN")} / ${total.toLocaleString("vi-VN")} IP · ${state.discovery.choices.size} camera / ứng viên · ${scan.target || ""}`;
    $("discovery-empty").textContent = scan.state === "cancelled" ? "Đã dừng quét. Bạn có thể quét lại hoặc đổi dải IP." : "Chưa phát hiện camera. Kiểm tra subnet route và dải IP.";
    if (scan.error) discoveryError(String(scan.error));
    renderDiscoveryResults();
  }
  async function loadDiscoverySubnets() {
    const revision = state.discovery.revision, session = state.sessionRevision;
    $("discovery-refresh-routes").disabled = true;
    try {
      const data = await api("/api/discovery/subnets");
      if (revision !== state.discovery.revision || session !== state.sessionRevision || !$("discovery-dialog").open) return;
      const old = $("discovery-subnet").value, subnets = Array.isArray(data.subnets) ? data.subnets : [];
      $("discovery-subnet").replaceChildren(option("", "Nhập dải IP riêng"));
      for (const subnet of subnets) if (typeof subnet.cidr === "string") $("discovery-subnet").append(option(subnet.cidr, subnet.label || `${subnet.cidr} · ${subnet.source === "tailscale" ? "Tailscale" : "LAN"}${subnet.interface ? ` · ${subnet.interface}` : ""}`));
      if (subnets.some(subnet => subnet.cidr === old)) $("discovery-subnet").value = old;
      $("discovery-route-note").textContent = data.stale ? "Danh sách route đã cũ. Làm mới hoặc nhập dải IP riêng." : subnets.length ? `${subnets.length} subnet VPS đang thấy. Chọn đúng dải của nhà cần thêm camera.` : "Chưa thấy subnet LAN. Có thể nhập dải IP riêng; kiểm tra Tailscale nhận route nếu quét không có kết quả.";
      if (data.error && !subnets.length) $("discovery-route-note").textContent = "Chưa đọc được route của VPS. Nhập dải IP riêng hoặc kiểm tra dịch vụ route-discovery.";
    } catch (error) {
      if (revision === state.discovery.revision && session === state.sessionRevision && $("discovery-dialog").open) $("discovery-route-note").textContent = error.message;
    } finally { if (revision === state.discovery.revision && session === state.sessionRevision) discoveryControls(); }
  }
  async function openDiscovery() {
    if (!state.authenticated || state.accountRequired) return;
    resetDiscovery(); $("discovery-scan-form").reset(); $("discovery-sd-username").value = "admin";
    $("discovery-results").replaceChildren(); $("discovery-progress-section").hidden = true; $("discovery-results-section").hidden = true; $("discovery-add-status").hidden = true;
    $("discovery-subnet").replaceChildren(option("", "Nhập dải IP riêng")); $("discovery-route-note").textContent = "Đang đọc route của VPS…";
    discoveryError(""); discoveryControls(); $("discovery-dialog").showModal(); $("discovery-subnet").focus(); await loadDiscoverySubnets();
  }
  function scheduleDiscoveryPolling(revision, session) {
    stopDiscoveryPolling();
    if (!helpers.discoveryActive(state.discovery.scan) || !$("discovery-dialog").open || revision !== state.discovery.revision || session !== state.sessionRevision) return;
    state.discovery.timer = setTimeout(() => pollDiscovery(revision, session), 1000);
  }
  async function pollDiscovery(revision, session) {
    state.discovery.timer = null; const id = state.discovery.scan?.id;
    if (!id || !state.authenticated || !$("discovery-dialog").open || revision !== state.discovery.revision || session !== state.sessionRevision) return;
    try {
      const data = await api(`/api/discovery/scans/${encodeURIComponent(id)}`);
      if (revision !== state.discovery.revision || session !== state.sessionRevision || !$("discovery-dialog").open) return;
      discoveryError(""); renderDiscoveryJob(data.scan);
    } catch (error) {
      if (revision === state.discovery.revision && session === state.sessionRevision && $("discovery-dialog").open) discoveryError(error.message);
    } finally { scheduleDiscoveryPolling(revision, session); }
  }
  async function startDiscovery(event) {
    event.preventDefault(); if (state.discovery.starting || state.discovery.adding || helpers.discoveryActive(state.discovery.scan)) return;
    const parsed = helpers.discoveryTarget($("discovery-target").value);
    discoveryError(""); if (parsed.error) { discoveryError(parsed.error, "discovery-target"); return; }
    stopDiscoveryPolling(); const revision = ++state.discovery.revision, session = state.sessionRevision;
    state.discovery.starting = true; state.discovery.scan = null; state.discovery.choices.clear(); $("discovery-results").replaceChildren();
    $("discovery-results-section").hidden = true; $("discovery-progress-section").hidden = true; $("discovery-add-status").hidden = true; discoveryControls();
    try {
      const data = await api("/api/discovery/scans", {method: "POST", body: {target: parsed.target}});
      if (revision !== state.discovery.revision || session !== state.sessionRevision || !$("discovery-dialog").open) {
        if (session === state.sessionRevision && state.authenticated && data.scan?.id) api(`/api/discovery/scans/${encodeURIComponent(data.scan.id)}/cancel`, {method: "POST", body: {}}).catch(() => {});
        return;
      }
      renderDiscoveryJob(data.scan); scheduleDiscoveryPolling(revision, session);
    } catch (error) { if (revision === state.discovery.revision && session === state.sessionRevision && $("discovery-dialog").open) discoveryError(error.message); }
    finally { if (revision === state.discovery.revision && session === state.sessionRevision) { state.discovery.starting = false; discoveryControls(); } }
  }
  async function cancelDiscovery() {
    if (state.discovery.cancelling || !helpers.discoveryActive(state.discovery.scan)) return;
    stopDiscoveryPolling();
    const revision = ++state.discovery.revision, session = state.sessionRevision, id = state.discovery.scan.id;
    state.discovery.cancelling = true; discoveryControls();
    try {
      const data = await api(`/api/discovery/scans/${encodeURIComponent(id)}/cancel`, {method: "POST", body: {}});
      if (revision === state.discovery.revision && session === state.sessionRevision && $("discovery-dialog").open) { renderDiscoveryJob(data.scan); scheduleDiscoveryPolling(revision, session); }
    } catch (error) { if (revision === state.discovery.revision && session === state.sessionRevision) discoveryError(error.message); }
    finally { if (revision === state.discovery.revision && session === state.sessionRevision) { state.discovery.cancelling = false; discoveryControls(); } }
  }
  async function addDiscoveredCameras() {
    const d = state.discovery;
    if (d.adding || d.starting || helpers.discoveryActive(d.scan)) return;
    const selected = [...d.choices.values()].filter(choice => choice.selected && !choice.added && !helpers.discoveryKnown(choice.result, state.cameras));
    if (!selected.length) return;
    const username = $("discovery-sd-username").value.trim() || "admin", password = $("discovery-sd-password").value;
    discoveryError("");
    for (const choice of selected) {
      try { helpers.discoveryPayload(choice.result, choice.name, state.cameras, username, password); }
      catch (error) {
        discoveryError(error.message);
        if (!String(choice.name).trim() || String(choice.name).trim().length > 120) choice.nameInput.focus();
        else if (!helpers.validSdPassword(password)) $("discovery-sd-password").focus();
        else $("discovery-error").focus();
        return;
      }
    }
    const revision = d.revision, session = state.sessionRevision; d.adding = true; renderDiscoveryResults();
    $("discovery-add-status").hidden = false; let added = 0, failed = 0, existing = 0;
    // Sequential creation makes each partial result explicit and avoids a burst of SD sync jobs.
    for (const choice of selected) {
      if (!state.authenticated || revision !== d.revision || session !== state.sessionRevision) break;
      $("discovery-add-status").textContent = `Đang thêm ${added + failed + existing + 1} / ${selected.length} camera…`;
      try {
        const payload = helpers.discoveryPayload(choice.result, choice.name, state.cameras, username, password);
        await api("/api/cameras", {method: "POST", body: {...payload, discovery_scan_id: d.scan.id}});
        if (revision !== d.revision || session !== state.sessionRevision) break;
        // Retain only non-secret camera metadata in browser state.
        const {sd_password, ...metadata} = payload; state.cameras.push(metadata);
        choice.added = true; choice.selected = false; choice.error = ""; added++;
      } catch (error) {
        if (revision !== d.revision || session !== state.sessionRevision) break;
        if (error.code === "camera_already_exists" && typeof error.existingCameraId === "string") {
          choice.result.existing_camera_id = error.existingCameraId; choice.selected = false; choice.error = ""; existing++;
        } else { choice.error = error.message; failed++; }
        if (error.httpStatus === 401 || error.code === "password_change_required") break;
      }
      renderDiscoveryResults();
    }
    if (revision !== d.revision || session !== state.sessionRevision) return;
    d.adding = false; $("discovery-sd-password").value = ""; renderDiscoveryResults();
    $("discovery-add-status").textContent = `Đã thêm ${added} camera${existing ? ` · ${existing} đã có sẵn` : ""}${failed ? ` · ${failed} chưa thêm được` : ""}.${added ? password ? " Đã bật và gửi Start." : " Đang tạm dừng; vào Chỉnh sửa để nhập mật khẩu và bật." : ""}`;
    if (failed) discoveryError("Một số camera chưa được thêm. Xem kết quả từng camera rồi thử lại.");
    if (added || existing || failed) await refresh();
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
    $("camera-upload-enabled").checked = helpers.uploadEnabled(camera);
    $("camera-sd-backend").value = camera?.sd_backend || "auto"; $("camera-sd-username").value = camera?.sd_username || "admin";
    $("camera-sd-channel").value = camera?.sd_channel || 1; $("camera-sd-timezone").value = camera?.sd_timezone || "Asia/Ho_Chi_Minh"; $("camera-sd-lookback").value = camera?.sd_lookback_hours || 168;
    $("camera-sd-password").value = ""; $("camera-sd-password-clear").checked = false; $("camera-sd-clear-control").hidden = !camera?.sd_password_configured;
    $("camera-sd-password-hint").textContent = camera?.sd_password_configured ? "Đã có mật khẩu thiết bị. Để trống sẽ giữ nguyên; mật khẩu hiện có không hiển thị." : "Chưa lưu mật khẩu thiết bị. Nhập mật khẩu / mã xác thực của camera để thử tải SD.";
    $("camera-dialog").showModal(); (camera ? $("camera-name") : $("camera-id")).focus();
  }
  async function saveCamera(event) {
    event.preventDefault(); if (!$("camera-form").reportValidity()) return;
    const values = new FormData($("camera-form"));
    const data = { id: String(values.get("id") || "").trim(), name: String(values.get("name") || "").trim(), model: String(values.get("model") || "").trim(), host: String(values.get("host") || "").trim(), enabled: $("camera-enabled").checked, upload_enabled: $("camera-upload-enabled").checked,
      sd_backend: $("camera-sd-backend").value, sd_username: $("camera-sd-username").value.trim(), sd_channel: Number($("camera-sd-channel").value), sd_timezone: $("camera-sd-timezone").value.trim(), sd_lookback_hours: Number($("camera-sd-lookback").value) };
    if ($("camera-sd-password").value) data.sd_password = $("camera-sd-password").value;
    if ($("camera-sd-password-clear").checked) data.sd_password_clear = true;
    for (const name of ["device_port", "rtsp_port", "http_port"]) data[name] = Number(values.get(name));
    const errorBox = $("camera-form-error"); errorBox.hidden = true;
    if (!data.name || !helpers.validHost(data.host)) { errorBox.textContent = !data.name ? "Nhập tên hiển thị cho camera." : "Địa chỉ LAN cần là IPv4 hợp lệ hoặc hostname, không có http://, đường dẫn hay mật khẩu."; errorBox.hidden = false; errorBox.focus(); return; }
    if (data.sd_password && data.sd_password_clear) { errorBox.textContent = "Chọn nhập mật khẩu mới hoặc xóa mật khẩu đã lưu, không chọn cả hai."; errorBox.hidden = false; errorBox.focus(); return; }
    if (data.sd_password && !helpers.validSdPassword(data.sd_password)) { errorBox.textContent = "Mật khẩu thiết bị cần dài tối đa 64 byte UTF-8 và không chứa ký tự NUL."; errorBox.hidden = false; $("camera-sd-password").focus(); return; }
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
      $("camera-sd-password").value = ""; $("camera-dialog").close(); await refresh(); toast(wasEditing ? "Đã cập nhật camera. Mã và lịch sử video được giữ nguyên." : data.enabled ? "Đã thêm camera và gửi Start vào hàng đợi. Theo dõi tiến trình đồng bộ." : "Đã thêm camera ở trạng thái tạm dừng. Bật camera trước khi Start.");
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
    if (!state.authenticated || state.accountRequired) return;
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
      if (url) { const link = node("a", "telegram-link", "Xem trong bot"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; link.setAttribute("aria-label", `Xem video ${record.camera_name || cameraName(record.camera)} lúc ${time} ngày ${date} trong bot Telegram`); link.append(icon("external")); telegramCell.append(link); }
      else telegramCell.append(node("span", "small muted", record.telegram_available ? "Xem bằng /archive trong bot" : "Chưa lưu Telegram")); row.append(telegramCell); $("archive-rows").append(row);
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
    if (!state.authenticated || state.accountRequired) return;
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
    event.preventDefault(); if (state.loginBusy) return;
    const form = $("login-form"), errorBox = $("login-error"), username = $("login-username").value.trim(), password = $("login-password").value;
    clearFormError(form, errorBox);
    if (!helpers.validUsername(username)) { formError(form, errorBox, "Nhập tên đăng nhập gồm 3–64 ký tự chữ, số, dấu _, dấu . hoặc dấu -.", "login-username"); return; }
    if (!password) { formError(form, errorBox, "Nhập mật khẩu để đăng nhập.", "login-password"); return; }
    state.loginBusy = true; $("login-submit").disabled = true; form.setAttribute("aria-busy", "true");
    $("login-status").textContent = "Đang đăng nhập…"; $("login-status").hidden = false;
    try {
      const result = await api("/api/login", { method: "POST", body: { username, password } });
      if (result.authenticated !== true || !result.account) throw new Error("Dashboard chưa xác nhận phiên đăng nhập. Thử lại.");
      clearPasswords(form); $("login-status").hidden = true; await acceptAccount(result.account, result.csrf_token);
    } catch (error) { $("login-status").hidden = true; formError(form, errorBox, error.message, "login-password"); }
    finally { state.loginBusy = false; $("login-submit").disabled = false; form.removeAttribute("aria-busy"); }
  });
  for (const toggle of document.querySelectorAll("[data-password-target]")) {
    toggle.dataset.showLabel = toggle.getAttribute("aria-label"); $(toggle.dataset.passwordTarget).dataset.passwordField = "true";
    toggle.addEventListener("click", () => {
      const input = $(toggle.dataset.passwordTarget), visible = input.type === "password";
      input.type = visible ? "text" : "password"; toggle.textContent = visible ? "Ẩn" : "Hiện";
      toggle.setAttribute("aria-pressed", String(visible)); toggle.setAttribute("aria-label", visible ? toggle.dataset.showLabel.replace("Hiện", "Ẩn") : toggle.dataset.showLabel);
    });
  }
  for (const formId of ["login-form", "account-form"]) $(formId).addEventListener("input", event => { event.target.removeAttribute("aria-invalid"); });
  $("account-button").addEventListener("click", async () => {
    $("account-button").disabled = true;
    try { const account = await api("/api/account"); state.csrf = account.csrf_token || state.csrf; showAccount(account); }
    catch (error) { if (error.httpStatus !== 401) toast(error.message, true); }
    finally { $("account-button").disabled = false; }
  });
  $("account-cancel").addEventListener("click", () => {
    if (state.accountRequired || state.accountBusy) return;
    clearPasswords($("account-form")); $("account-screen").hidden = true; $("app-shell").hidden = false; $("account-button").focus(); loadSync();
  });
  $("account-form").addEventListener("submit", async event => {
    event.preventDefault(); if (state.accountBusy) return;
    const form = $("account-form"), errorBox = $("account-error");
    const values = { username: $("account-username").value.trim(), current_password: $("account-current-password").value, new_password: $("account-new-password").value, confirm_password: $("account-confirm-password").value };
    const invalid = helpers.accountValidation(values); clearFormError(form, errorBox);
    if (invalid) { formError(form, errorBox, invalid.message, invalid.field); return; }
    state.accountBusy = true; $("account-save").disabled = true; $("account-cancel").disabled = true; $("account-logout").disabled = true; form.setAttribute("aria-busy", "true");
    $("account-status").textContent = "Đang lưu tài khoản…"; $("account-status").hidden = false;
    try {
      const result = await api("/api/account", { method: "POST", body: { username: values.username, current_password: values.current_password, new_password: values.new_password } });
      if (result.credentials_updated !== true || result.authenticated !== false) throw new Error("Dashboard chưa xác nhận thay đổi tài khoản. Thử lại.");
      showLogin({ username: values.username, message: "Đã đổi tài khoản. Đăng nhập lại bằng mật khẩu mới." }); toast("Đã lưu tài khoản và kết thúc mọi phiên đăng nhập.");
    } catch (error) {
      if (error.httpStatus !== 401 || error.code === "current_password_invalid") formError(form, errorBox, error.message, error.code === "current_password_invalid" ? "account-current-password" : "");
    } finally { state.accountBusy = false; $("account-save").disabled = false; $("account-cancel").disabled = false; $("account-logout").disabled = false; form.removeAttribute("aria-busy"); $("account-status").hidden = true; }
  });
  $("refresh-button").addEventListener("click", refresh); $("camera-search").addEventListener("input", renderCameras);
  $("start-all-button").addEventListener("click", () => startSync());
  $("logs-refresh").addEventListener("click", loadSync);
  for (const id of ["logs-camera", "logs-status"]) $(id).addEventListener("change", renderLogs);
  async function logout() {
    if (state.logoutBusy) return;
    state.logoutBusy = true; $("logout-button").disabled = true; $("account-logout").disabled = true;
    try { await api("/api/logout", { method: "POST", body: {} }); globalError(""); showLogin(); toast("Đã kết thúc phiên quản trị."); }
    catch (error) { if (error.httpStatus !== 401) toast(error.message, true); }
    finally { state.logoutBusy = false; $("logout-button").disabled = false; $("account-logout").disabled = false; }
  }
  $("logout-button").addEventListener("click", logout); $("account-logout").addEventListener("click", logout);
  $("add-camera-button").addEventListener("click", () => openCameraForm()); $("empty-add-camera").addEventListener("click", () => openCameraForm());
  $("discover-camera-button").addEventListener("click", openDiscovery);
  $("discovery-scan-form").addEventListener("submit", startDiscovery);
  $("discovery-refresh-routes").addEventListener("click", loadDiscoverySubnets);
  $("discovery-subnet").addEventListener("change", () => { $("discovery-target").value = $("discovery-subnet").value; discoveryError(""); $("discovery-target").focus(); });
  $("discovery-target").addEventListener("input", () => { $("discovery-subnet").value = ""; $("discovery-target").removeAttribute("aria-invalid"); });
  $("discovery-cancel").addEventListener("click", cancelDiscovery);
  $("discovery-select-all").addEventListener("change", () => {
    for (const choice of state.discovery.choices.values()) if (!choice.added && !helpers.discoveryKnown(choice.result, state.cameras)) choice.selected = $("discovery-select-all").checked;
    renderDiscoveryResults();
  });
  $("discovery-add").addEventListener("click", addDiscoveredCameras);
  for (const id of ["close-discovery-dialog", "discovery-done"]) $(id).addEventListener("click", () => { if (!state.discovery.adding) closeDiscovery(); });
  $("discovery-dialog").addEventListener("cancel", event => { event.preventDefault(); if (!state.discovery.adding) closeDiscovery(); });
  $("discovery-dialog").addEventListener("close", () => { if (state.discovery.scan || state.discovery.timer || $("discovery-sd-password").value) closeDiscovery(); });
  for (const id of ["close-camera-dialog", "cancel-camera-dialog"]) $(id).addEventListener("click", () => { $("camera-sd-password").value = ""; $("camera-dialog").close(); });
  $("camera-dialog").addEventListener("close", () => { $("camera-sd-password").value = ""; });
  $("camera-form").addEventListener("submit", saveCamera);
  $("archive-filters").addEventListener("submit", event => event.preventDefault());
  $("filter-camera").addEventListener("change", async () => { state.offset = 0; try { await loadCalendar(false); await loadArchive(); } catch (error) { globalError(error.message); } });
  $("filter-year").addEventListener("change", () => { $("filter-month").value = ""; $("filter-day").value = ""; updateCalendarOptions(true); state.offset = 0; loadArchive(); });
  $("filter-month").addEventListener("change", () => { $("filter-day").value = ""; updateCalendarOptions(true); state.offset = 0; loadArchive(); });
  for (const name of ["day", "status", "order"]) $("filter-" + name).addEventListener("change", () => { state.offset = 0; loadArchive(); });
  $("previous-page").addEventListener("click", () => { state.offset = Math.max(0, state.offset - state.limit); loadArchive(); });
  $("next-page").addEventListener("click", () => { state.offset += state.limit; loadArchive(); });
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1)));
  async function boot() {
    try { const account = await api("/api/account"); await acceptAccount(account, account.csrf_token); }
    catch (error) { if (error.httpStatus !== 401) { showLogin(); formError($("login-form"), $("login-error"), error.message); } }
  }
  switchView(location.hash.slice(1), false); boot();
})();
