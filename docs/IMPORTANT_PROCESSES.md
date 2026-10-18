# Các tiến trình quan trọng của hệ thống lưu camera Telegram

> Tài liệu vận hành được đối chiếu trực tiếp với mã nguồn ngày **18/10/2026**.
> Phạm vi là repo `mycameratele`; các giá trị thực tế trên một máy còn phụ thuộc
> `.env`, tập Compose đang dùng và cấu hình lưu trong SQLite. Đây không phải
> biên bản kiểm tra trạng thái VPS/box tại thời điểm đọc tài liệu.

> Cập nhật phục hồi sau reboot: xem
> [CAMERA_RECOVERY_2026-10-18.md](CAMERA_RECOVERY_2026-10-18.md).
> VPS và observer cần đồng bộ NTP; MAC guard vẫn fail-closed khi lệch giờ.

## 1. Mục đích và các nguyên tắc giữ nguyên

Hệ thống lấy **bản ghi đã đóng trên thẻ SD**, lưu tạm trên đĩa VPS, đổi container
sang MP4 bằng **stream-copy**, rồi gửi vào Telegram. Hệ thống không ghi lại luồng
RTSP trực tiếp để thay thế lịch sử SD và không transcode video/audio.

Các nguyên tắc cần giữ khi sửa code hoặc vận hành:

1. Camera có **ID nội bộ ổn định**; tên, IP, MAC và channel là thuộc tính. Đổi IP
   sau DHCP không tạo camera mới, không đổi khóa video và không xóa mật khẩu.
2. Chỉ xóa cache thành công sau khi đã kiểm tra phản hồi Telegram và **commit
   `chat_id`, `message_id`, `file_id`**. Timeout sau gửi là giao dịch chưa rõ kết
   quả, không phải giấy phép tự gửi lại.
3. Quy trình remux chỉ dùng `-c copy`. Codec không phù hợp MP4 đi vào hàng kiểm
   tra; hệ thống không tự chuyển sang encoder hoặc gửi raw thay cho MP4.
4. Worker tải SD, polling menu bot, gửi album và cập nhật chỉ mục chạy tách nhau;
   mỗi thread sở hữu kết nối SQLite riêng. Upload media mới được tuần tự hóa.
5. Mặc định triển khai trực tiếp: box làm Tailscale subnet router và có thể chạy
   observer MAC nhẹ. Box **không tải, lưu tạm hoặc chuyển tiếp file video**.
6. Những file đang tải, media chưa có xác nhận, DB, mật khẩu, SDK đang được bind,
   volume đang dùng và cấu hình mạng không phải rác để xóa tùy tiện.

## 2. Sơ đồ triển khai và trách nhiệm từng thành phần

```text
LAN của từng nhà                                      VPS
┌──────────────────┐     Tailscale subnet route       ┌─────────────────────────┐
│ Camera + thẻ SD  │ ───────────────────────────────> │ archive worker          │
│ TCP 8000 / ISAPI │                                  │ native SDK child        │
└──────────────────┘                                  │ staging → remux MP4     │
        ▲                                             │ SQLite claims/catalog   │
        │ ICMP + netlink                               └─────────┬───────────────┘
┌───────┴──────────┐ metadata qua Tailscale                        │ file:// /cache/…
│ LAN observer    │ ──────────────────────────────────>           ▼
│ không có media  │                                  ┌─────────────────────────┐
└─────────────────┘                                  │ Local Bot API + spool   │
                                                     └─────────┬───────────────┘
                                                               │
                                                     Telegram: channel/camera
                                                               │
                                                     Bot: menu, xem, tải,
                                                     xóa mềm, khôi phục

dashboard ── cấu hình/queue ── SQLite ── worker
route-discovery ── /network/subnets.json ── dashboard chọn subnet
```

| Thành phần | Vai trò thực tế | Trạng thái được giữ ở đâu |
|---|---|---|
| `archive` | Điều phối job, tải SD, ingest/remux, upload, polling bot, backup và dọn cache | `/data/archive.db`, `/cache`, heartbeat |
| Native SDK child | Chỉ đăng nhập/search/download bằng SDK đúng kiến trúc; không truy cập catalog hoặc bot | Tiến trình con và file staging của job |
| `dashboard` | Đăng nhập quản trị, CRUD camera, scan subnet, bind channel/MAC, Start sync, xem log/thống kê | Catalog chung và `/data/dashboard_auth.sqlite` |
| `route-discovery` | Đọc routing table của host, gồm table Tailscale; xuất danh sách subnet | Volume `network-routes`; không dò MAC hoặc tải camera |
| `telegram-bot-api` | Giao tiếp Telegram; đọc MP4 chung theo đường dẫn hoặc nhận multipart | `bot-api-state`, `bot-api-spool` |
| `spool-init` | Chỉnh owner/mode thư mục root của **spool mới**, chạy một lần | Không sửa ownership toàn bộ Bot API state |
| `lan-identity` trên box | Chứng minh IP↔MAC tại LAN bằng ICMP và bảng neighbor | RAM, không catalog, không mật khẩu camera, không media |
| Collector tùy chọn | Tải SD trên LAN rồi chuyển nguồn tới VPS | Dùng riêng khi chủ động bật; khác hoàn toàn observer |

Nguồn: `compose.yaml`, `compose.local.yaml`, `compose.sdk.yaml`,
`compose.identity.yaml`, `compose.identity-client.yaml`, `archive_app/__main__.py`.

### 2.1 Nhiều nhà và nhiều camera

- Mỗi nhà nên có một thư mục triển khai, Compose project, `TENANT_ID`, volume
  state/cache và bot riêng. Ví dụ thư mục `HOUSE_DIR`, không tái dùng volume
  state giữa hai nhà khác nhau.
- Catalog lưu `archive_tenant_id`; mở DB bằng `TENANT_ID` khác sẽ bị chặn.
- Trong một nhà, `MULTI_CHANNEL_ROUTING=true` cho phép **mỗi camera gắn một
  channel riêng** nhưng dùng chung một bot. Chỉ mục lọc đúng camera/channel.
- Thêm các channel vào community là thao tác tổ chức trên Telegram. Repo không
  tự tạo community và không lấy tên community làm khóa định tuyến.
- Không thay Compose project name của hệ thống đang dùng chỉ để đổi tên nhà:
  việc đó có thể tạo một bộ volume mới trống và trông như mất dữ liệu cũ.

## 3. Khởi động, lịch chạy và shutdown

Điểm vào là `python -m archive_app`; image mặc định chạy subcommand `run`.
`__main__.mutation_lock()` khóa độc quyền `STATE_DIR/worker.lock`, tránh hai
archive runner cùng tải/sửa một catalog. Dashboard dùng DB chung nhưng không
chiếm vai trò runner.

Trình tự khởi động runner:

1. Nạp/kiểm tra `Settings.from_env()`, tạo hoặc migrate DB và các index cần thiết.
2. `recover_uploads()` chuyển bản ghi `uploading` bị ngắt thành `upload_unknown`.
3. Đánh dấu cache kiểu legacy cần xử lý lại có kiểm soát; phục hồi ingest dở và
   staging do worker cũ để lại. Chỉ các tên file quản lý hợp lệ được xử lý.
4. Hết hạn media lỗi đủ tuổi; giữ catalog/tombstone để không tải lại do scan.
5. `SyncQueue.recover()` đưa job `running` cũ về `queued`, code
   `worker_restarted`; không tự xóa hoặc gửi lại receipt Telegram.
6. Bật heartbeat 10 giây/lần, thread polling bot, thread album, thread chỉ mục và
   executor SD tối đa 5 camera. SQLite connection không truyền qua các thread.
7. Vòng chính lên lịch camera, nạp manifest mới, backup DB, kiểm tra upload và
   dọn media đủ điều kiện.

### 3.1 Hai khoảng thời gian khác nhau

| Thiết lập | Mặc định | Ý nghĩa |
|---|---:|---|
| `SD_SYNC_INTERVAL_SECONDS` | `900` | Lên lịch kiểm tra SD tự động mỗi 15 phút; hợp lệ 30–86400 giây |
| `SCAN_INTERVAL_SECONDS` | `15` | Chu kỳ vòng điều phối/housekeeping, không phải khoảng tải SD 15 giây |
| Heartbeat | 10 giây | Ghi dấu runner còn sống |
| Observer MAC | 60 giây | Lịch scan LAN mặc định trong `ObserverConfig`, không phải lịch tải SD |

Lần khởi động đầu đặt `next_sync=0`, nên có lượt enqueue tự động sớm, không chờ
đủ 15 phút đầu. Camera có lịch sử tồn đọng được chia batch và có thể nối tiếp
ngay cuối hàng đợi nếu các điều kiện upload/backpressure cho phép. Do đó 900 giây
không có nghĩa mỗi 15 phút chỉ xử lý tối đa 10 file rồi luôn nghỉ.

`Start all`/Start từng camera trên dashboard và `Start sync` trong bot tạo job
ngay, không chờ lịch. Unique index chỉ cho phép một job `queued` hoặc `running`
trên cùng camera; bấm lặp không tạo hai job chạy trùng camera.

### 3.2 Dừng và cập nhật đang có tải

Runner nhận SIGTERM/SIGINT sẽ ngừng nhận công việc mới, giữ khóa runner trong khi
các công việc đã nhận hoàn tất/dừng ở điểm kiểm soát. Giảm worker không cắt ngang
file đang được xử lý. Compose dành `stop_grace_period: 65m` cho archive; dashboard
10 giây và observer 20 giây. Force kill có thể tạo `upload_unknown` nếu xảy ra
giữa POST Telegram và commit receipt, nên phải đối soát thay vì resend mù.

## 4. Thêm camera, dò LAN và nhận diện bằng MAC

### 4.1 Thêm thủ công hoặc scan subnet

Dashboard cho phép giữ cả hai cách:

1. **Thủ công:** tên hiển thị, IP, model tùy chọn, cổng, SD username/password,
   timezone, lookback, số luồng, channel và tùy chọn MAC/Auto IP.
2. **Dò LAN:** chọn subnet do `route-discovery` công bố hoặc nhập IP/CIDR/dải
   `FIRST_IP-LAST_IP`; xem các candidate rồi chọn thêm. Scan không tự thử mật
   khẩu, không tự tải video và không tự ý thêm tất cả camera vào catalog.

`discovery.parse_targets()` mặc định tối đa 1024 host/lượt trong RFC1918;
`DEFAULT_PORTS=(8000,554,80)`. Cổng mở chỉ chứng minh có TCP listener. Cổng 554 mở
không có nghĩa lịch sử SD tải được; phải xác nhận login/search/download thực.
Model nhận diện qua probe có thể để trống mà vẫn cấu hình được SD.

`route-discovery` dùng host network để thấy route của host, ghi snapshot 30
giây/lần. Nó không quảng bá subnet, không bật `--accept-routes`, không sửa
iptables và không tìm MAC xuyên qua subnet router.

### 4.2 MAC không thể đọc đáng tin cậy từ ARP trên VPS

VPS và camera khác broadcast domain; ARP/neigh trên VPS không trả về MAC vật lý
của camera ở nhà. Muốn Auto IP theo MAC phải có **observer tại LAN camera**.

Quy trình đúng:

1. Observer chạy host network trên interface LAN được chỉ định; bind listener
   vào IP Tailscale của box.
2. `scan_identity()` gửi ICMP Echo bằng ping socket không đặc quyền và đọc
   neighbor qua netlink. Chỉ một cặp có phản hồi và neighbor phù hợp, đủ mới,
   không mâu thuẫn mới trở thành bằng chứng `icmp+netlink`.
3. `IdentityState` công bố proof tối đa **120 giây**. Một mục ARP cũ đơn độc
   hoặc chỉ việc nhớ IP cũ không đủ để xác minh.
4. Observer giữ hint các cặp đã từng chứng minh tối đa **24 giờ**, tối đa 254
   host; mỗi scan recheck tối đa 16 cặp bị thiếu. **Hint không phải proof** và
   không được dùng để bỏ qua xác minh khi camera offline.
5. VPS `IdentityClient` lấy snapshot có token, kiểm tra timestamp, subnet,
   uniqueness và evidence. Cache đọc snapshot tối đa 5 giây vẫn kiểm tra TTL.
6. `resolve_camera()`/`Archive.apply_camera_identity()` cập nhật IP của những
   camera Auto IP trong cùng transaction. ID, mật khẩu và đích video không đổi.
7. `SDSource._verify_bound_identity()`/`guard_camera()` kiểm tra lại snapshot và
   xác minh chủ động đúng cặp IP/MAC trước các thao tác SD; không đổi host ngầm
   dưới một session đã search trên thiết bị khác. Proof verify chủ động phải
   trong 5 giây; cache kiểm tra cùng cặp chỉ 1 giây.

MAC được chuẩn hóa lowercase dạng `aa:bb:cc:dd:ee:ff`; đầu vào hỗ trợ uppercase,
dấu `:`/`-` hoặc 12 ký tự hex. Khác hoa/thường không phải nguyên nhân hợp lệ của
lỗi MAC. Địa chỉ all-zero/multicast và cặp bị trùng bị chặn.

Auto IP cần đủ `mac_address`, `identity_subnet`, `auto_ip=true` và observer đúng
subnet. Dải observer hỗ trợ RFC1918 **/24 đến /30**, một interface LAN thật,
không phải `tailscale0`/Docker bridge. ICMP ping socket cần `ping_group_range`
cho UID/GID 10001; không giải quyết bằng cách cấp `NET_RAW` hoặc container
privileged. Observer hiện không đụng cổng 8000 hay media của camera.

### 4.3 Các trạng thái nhận diện

| `identity_status` | Nghĩa thực tế |
|---|---|
| `manual` | IP thủ công, không áp dụng auto MAC guard |
| `pending` | Vừa bật/sửa cấu hình nhận diện, chờ quan sát |
| `verified` | Có bằng chứng mới khớp MAC và IP hiện tại |
| `not_found` | Không có proof MAC trong snapshot mới; không đồng nghĩa nhập sai MAC |
| `stale` | Có quan sát mới nhưng catalog/binding chưa khớp hoặc đã cũ |
| `conflict` | Đụng IP/MAC với camera/cặp khác, cần giải quyết mâu thuẫn |
| `unavailable` | Observer/subnet/token/metadata không dùng được |

Dashboard có overlay metadata mới cho GET nhưng không tự sửa binding catalog
chỉ vì người dùng mở trang. SD guard vẫn kiểm tra độc lập. Vì vậy nhãn MAC và
nhãn cổng mở là hai điều kiện khác nhau, không dùng nhãn này thay thế nhãn kia.

Nguồn: `archive_app/camera_identity.py`, `lan_identity.py`, `lan_echo.py`,
`core.py:apply_camera_identity`, `dashboard.py:camera_identity_overlay`,
`docs/MAC_BINDING.md`.

## 5. Tải lịch sử SD và điều phối nhiều luồng

### 5.1 Điều kiện trước khi tải

Camera phải đang bật, có IP đúng và thông tin SD hợp lệ. Password/mã xác nhận
được lưu ở file quản lý theo ID camera trong state, không lấy từ Wi-Fi password.
Việc EZVIZ Studio từng nhớ mật khẩu không có nghĩa thiết bị cho SDK login vô
danh. Tên model và số cổng mở cũng không thay thế xác thực SD.

Backend:

- `hcnetsdk`: SDK Linux chính thức đúng **kiến trúc Docker host tải SD**; có
  `libhcnetsdk.so`, `HCNetSDKCom` và các thư viện kèm theo. VPS ARM64 thì SDK phải
  ARM64 kể cả box subnet router dùng kiến trúc khác.
- `isapi`: chỉ dùng khi camera thực sự hỗ trợ search/download ISAPI.
- `auto`: ưu tiên native SDK; chỉ fallback ISAPI khi SDK **thiếu**, không giấu
  sai kiến trúc/SDK hỏng/auth sai bằng fallback tùy tiện.

Camera mặc định `sd_username=admin`, `sd_channel=1`,
`sd_timezone=Asia/Ho_Chi_Minh`, lookback **168 giờ**, hợp lệ 1–720 giờ. Search
chỉ lấy tới `now-120 giây` để tránh file đang mở. Timestamp nội bộ có timezone,
không đoán ngày giờ bằng mtime của file vừa tải.

### 5.2 Hai lớp song song, không phải upload Telegram 10 luồng

| Lớp | Giới hạn | Lưu/cấu hình ở đâu |
|---|---:|---|
| Số camera làm việc đồng thời | 1–5, mặc định 1 | `SD_DOWNLOAD_WORKERS`; Dashboard override `pipeline_sd_download_workers` trong DB |
| File SD đồng thời trên từng camera | 1 hoặc 2, mặc định 1 | `SD_DOWNLOAD_THREADS` làm default; mỗi camera có `sd_download_threads` |
| Batch SD mỗi job | 1–100, mặc định 10 file | `SD_MAX_FILES_PER_JOB` |
| Upload media mới tới Telegram | **1 tại một thời điểm/runner** | `SerializedUploads`, lock dùng chung |

Năm camera × hai luồng cho phép tối đa 10 file SD đang làm việc trong những
điều kiện phù hợp, không bảo đảm 10 lần tốc độ. Tất cả còn chia sẻ uplink nhà,
Tailscale, disk/cache và khả năng firmware/SD của camera. Tăng dần từ 1–2 camera,
1 luồng/camera; đo kết quả và lỗi trước khi tăng.

Thay số camera trong dashboard có hiệu lực cho **job tiếp theo**, không cần
restart worker. Hạ giới hạn để job đã nhận drain, không kill file đang tải.
Mỗi camera chỉ có một job đang active; 2 luồng là hai session tải file bên trong
job đó, không phải hai catalog writer cùng sở hữu camera.

Search native chia page 500, có giới hạn 4000 kết quả, 128 query và tối đa 16
prefix đã biết để tránh vòng scan vô hạn. Sau 10 file mặc định, phần còn lại có
thể xếp cuối hàng đợi FIFO để camera khác có lượt. File đã uploaded, tombstone,
expired hoặc đã biết hợp lệ không bị tải lại chỉ vì scan thấy lại tên.

### 5.3 Cách cô lập SDK và chống treo

`HCNetSDKSource` gọi `_native_worker()` trong subprocess:

- Truyền credential bằng anonymous pipe, không nằm trong command line.
- SDK stdout/stderr không được log thô; chỉ fixed error code được đưa ra ngoài.
- `LD_LIBRARY_PATH` và SSL vendor chỉ áp dụng cho child; parent Telegram/FFmpeg
  tiếp tục dùng CA/SSL hệ thống. Child bỏ các biến Telegram nhạy cảm.
- Init tối đa 90 giây, search exchange 130 giây; download exchange yêu cầu 1805
  giây nhưng bị chặn bởi session operation cap mặc định **1800 giây**. Tiến độ
  file mỗi 2 giây không gia hạn deadline vô hạn.
- Helper thoát/timeout/protocol hỏng sẽ đóng session, kill child nếu cần; Linux
  dùng parent-death signal tránh process SDK mồ côi khi parent bị kill.
- Một native file dưới 1 GiB (`1024^3-1`) để tránh chế độ SDK tự chia file ngầm;
  giới hạn Bot API 2 GB không bỏ giới hạn native này.

`SD_DOWNLOAD_SPEED_KBPS=0` giữ mặc định firmware; giá trị 256–32768 kbps là yêu
cầu tốc độ truyền tới SDK, không encoder. SDK có thể trả `unsupported`/`rejected`;
đọc metric status, không mặc định coi giá trị yêu cầu là tốc độ đạt được.

Nguồn: `archive_app/sd_source.py:SDSource`, `HCNetSDKSource`, `_NativeSession`,
`archive_app/sync.py:SyncQueue`, `archive_app/pipeline_runtime.py:PipelineRuntime`.

## 6. Cache VPS → MP4 → Telegram

### 6.1 Đường đi của một file

```text
Search metadata (camera ID + source + stable recording ID)
  → record_key SHA256, kiểm tra catalog
  → reserve cache (tính cả source và vùng xây MP4)
  → /cache/sd-stage/CAMERA_ID/KEY.RANDOM.part
  → download hoàn tất + kiểm tra nguồn không thay đổi
  → ingest/normalize sang /cache/KEY.mp4
  → status downloaded
  → xác minh channel + claim uploading
  → gửi sendVideo (local_file nếu Local API) + supports_streaming
  → kiểm tra message/chat/media → commit uploaded + outbox chỉ mục
  → xóa các file quản lý của KEY khi retention cho phép
```

Khóa `record_key()` dựa trên camera/source/record_id; với nguồn SD, ID bản ghi
được tạo từ channel SD và thời điểm bắt đầu UTC. Hệ thống dùng metadata search
và khóa ổn định, không phải bóc giải mã toàn video để lập ngày. SHA256 file dùng
kiểm chứng byte/receipt và đường dẫn managed, không thay đổi codec.

### 6.2 Remux, không transcode

`core.normalize()` chọn theo `MEDIA_MODE`:

- `remux_copy`: đầu vào đã là MP4 thì copy nguyên byte. PS/TS được FFmpeg đóng
  lại MP4. Không gọi FFprobe và không decode-validation toàn file.
- `raw`: giữ nguyên nguồn, gửi document; đây là chế độ hỗ trợ riêng, không phải
  fallback âm thầm khi remux thất bại.

Lệnh logic trong `_remux_copy()`:

```sh
ffmpeg -hide_banner -loglevel error -nostdin -y \
  -probesize 1048576 -analyzeduration 1000000 \
  -i SOURCE -map 0:v:0 -map '0:a?' -c copy \
  -movflags +faststart MANAGED_PART.mp4
```

FFmpeg có timeout 300 giây. Nó đọc header/packet để remux; “không transcode”
không có nghĩa chỉ đổi đuôi tên mà không đọc dữ liệu. Output cần header MP4,
không rỗng, không quá 2 lần source; source phải không thay size/mtime/inode trong
khi xử lý. Không có `libx264`, AAC encoder hoặc vòng decode để sửa file lỗi.
Audio không tương thích MP4 hoặc stream thiếu thông số bị đánh lỗi cần xử lý.

### 6.3 Local Bot API đọc MP4 trực tiếp

`compose.local.yaml` mount cùng `archive-cache` tại `/cache` cho worker và Bot
API; Bot API mount read-only. `TELEGRAM_UPLOAD_TRANSPORT=local_file` dùng
`file://` URI đã kiểm tra nằm trong managed cache và đúng key.

Lợi ích: worker không gửi thêm bản sao MP4 bằng HTTP multipart vào Bot API;
giảm một lượt copy/đọc-ghi và vùng HTTP temp. **Không bỏ lượt Bot API upload lên
Telegram**, không bảo đảm TDLib không giữ file nội bộ. Raw/document vẫn dùng
multipart. Chỉ Local API được dùng `local_file`, không áp dụng cloud endpoint.

`check_upload_spool()` kiểm tra trước POST:

- Spool riêng là volume đĩa `bot-api-spool`, không phải RAM tmpfs.
- `BOT_API_SPOOL_MAX_GB=5` là ngân sách hợp tác **5.000.000.000 byte**, không
  filesystem quota và không phải tổng cache video tối đa.
- Multipart dự trù kích thước incoming file; local_file MP4 dự trù thêm **0**
  byte HTTP copy nhưng vẫn kiểm tra spool hiện có và disk free reserve.
- Thiếu dung lượng trả `upload_spool_budget`, giữ trạng thái `downloaded`, đặt
  retry sau 60 giây trước khi gửi; không biến thành upload chưa rõ kết quả.
- Bot API sở hữu file spool đang dùng. Không dọn `bot-api-state` hoặc `spool`
  bằng cron `rm -rf`; worker chỉ đọc kiểm tra admission.

Giới hạn cloud mặc định 50.000.000 byte/file; local override đặt
2.000.000.000 byte/file. Dùng Local API cần `TELEGRAM_API_ID/HASH`, token bot,
profile `local-api` và migration endpoint có kiểm soát. Một token không nên có
hai poller song song trên cloud/local hoặc hai nhà.

### 6.4 Xóa cache thành công và hết hạn file lỗi

Với deployment Compose mặc định `KEEP_CACHE=false`,
`CACHE_RETENTION_HOURS=0`:

1. Validate receipt Telegram; commit `uploaded` và metadata.
2. `Archive.cleanup(KEY)` xóa mọi file quản lý hợp lệ cùng key: MP4, PS/TS/raw,
   partial/sibling và staged source còn lại. Không chỉ xóa file `local_path`.
3. Đánh `cleaned_at`, `cleanup_revision=1`; catalog và link Telegram được giữ.

`CACHE_RETENTION_HOURS=1` giữ thêm một giờ sau xác nhận/commit;
`KEEP_CACHE=true` ngừng dọn media uploaded theo cơ chế trên. File trong input
bind read-only và video nguồn trên **thẻ SD camera** không bị xóa bởi cleanup.

`ERROR_RETENTION_HOURS=72` áp dụng cho media ở `failed`, `needs_review`,
`upload_unknown`, tính từ `error_started_at` đầu tiên, không reset tuổi sau mỗi
retry. `expire_error_media()` commit `media_expired_at` trước unlink và không
đụng camera đang có sync job `running`. Nếu camera vẫn active, việc xóa có thể
muộn hơn mốc 72 giờ. Tombstone và receipt vẫn giữ để không tự tải/gửi lại.

`CACHE_MAX_GB=100`, `CACHE_MIN_FREE_GB=5` là budget cache và free reserve riêng
với spool. `reserve_cache()` tính cộng dồn các job đang chạy; raw/remux cần thêm
vùng làm việc. Thiếu dung lượng là backpressure, không được đẩy file tốt vào
quarantine như codec hỏng.

Nguồn: `core.py:_remux_copy`, `_managed_media_paths`, `cleanup`,
`expire_error_media`; `local_upload.py`; `upload_spool.py`;
`telegram.py:upload_one`; `pipeline_runtime.py:reserve_cache`.

## 7. Catalog, trạng thái job và chống gửi trùng

### 7.1 Hai máy trạng thái khác nhau

| Bảng/trường | Giá trị | Diễn giải |
|---|---|---|
| `recordings.status` | `ingesting` | Đang nhập/chuẩn hóa media, chưa sẵn sàng upload |
| | `downloaded` | File đã vào cache và đủ điều kiện chờ upload nếu gate cho phép |
| | `uploading` | Đã claim một attempt, có thể đang gửi; không được claim lần hai |
| | `uploaded` | Đã validate và lưu receipt Telegram |
| | `failed` | Xử lý nguồn/ingest thất bại; xem `last_error` |
| | `needs_review` | Lỗi cần kiểm tra, gồm remux/API từ chối/path hoặc size |
| | `upload_unknown` | Đã có thể gửi nhưng chưa xác minh được kết quả; không tự resend |
| `sync_jobs.state` | `queued` | Job chờ worker |
| | `running` | Job đang xử lý |
| | `completed` | Lượt đã xong, có thể không có video mới |
| | `blocked` | Dashboard ghi “Cần xử lý”; phải xem `code`, không suy ra treo |
| | `failed` | Job gặp lỗi không xử lý xong |

Các phase job thực tế: `queued`, `probing`, `remuxing`, `sd_search`,
`sd_download`, `sd_complete`, `scanning`, `uploading`, `finished`.
Phase `probing`/số cổng mở chỉ là kiểm tra TCP; kết quả SD được ghi riêng.

Các trường quan trọng ngoài status:

- `attempt_id`, `upload_target_chat_id`: attempt và đích đã claim.
- `storage_kind`, `storage_chat_id`, `storage_message_id`, `bot_id`: vị trí
  Telegram thực sự đã xác nhận, không được viết lại theo channel mới.
- `deleted_at/deleted_by`: xóa mềm khỏi kho chung.
- `error_retry_count/error_retry_at/error_queue_at`: giới hạn thử lại.
- `media_expired_at`: media cục bộ hết hạn; không có nghĩa video Telegram bị xóa.

SQLite dùng WAL, writer transaction `BEGIN IMMEDIATE` cho các claim quan trọng.
Không dùng việc “file không còn trên đĩa” để tự khẳng định upload đã thành công.

### 7.2 Unknown upload phải đối soát

- HTTP 429: đặt backoff, không spam gửi; chờ `retry_after`.
- HTTP 4xx xác định: `needs_review/api_rejected` và phân loại lỗi media/global.
- HTTP 5xx sau gửi, lỗi mạng/timeout/JSON không xác nhận được: `upload_unknown`.
- Worker restart khi row còn `uploading`: `upload_unknown`,
  `process_restart_after_claim`.

CLI `reconcile` chỉ dùng với row `upload_unknown` và **receipt thật** ở đúng
đích đã claim. Không điền đại message/file ID. Lệnh này chiếm `worker.lock`, nên
chỉ chạy khi runner đã dừng sạch; container `run --rm` dùng lại cùng Compose
project/volume và không chạy song song runner. Ví dụ tham số:

```sh
docker compose run --rm --no-deps archive reconcile \
  --key RECORDING_KEY --chat-id CHANNEL_ID \
  --message-id MESSAGE_ID --file-id FILE_ID \
  --file-unique-id FILE_UNIQUE_ID --media-type video
```

### 7.3 Ba lần retry rồi vào hàng lỗi

`error_queue.retry_one()` không thử lại mọi lỗi một cách mù quáng:

- Chọn tối đa **một lỗi cũ** sau một batch SD đã có nhập mới và upload khỏe.
- Chỉ các row `needs_review`, `last_error` là `failed_remux` hoặc
  `api_rejected`, media còn giữ, chưa deleted/expired/parked.
- Retry API cần chứng minh sau lỗi đã có một upload thành công mới của đúng
  camera, bot và channel hiện tại, và lỗi thuộc nhóm có thể thử media.
- Mỗi lượt cách tối thiểu **900 giây**, counter được reserve/commit trước khi
  xử lý; restart không làm mất counter. Đây là tối đa 3 **retry**, không nên
  nhầm với tổng số attempt ban đầu cộng retry.
- Remux thử lại từ raw đã giữ, vẫn stream-copy; uploader bình thường là nơi duy
  nhất gửi Telegram. Cache admission từ chối trước remux hoàn lại reservation
  retry, không tiêu tốn lượt vì disk đầy.
- Đủ 3 retry thất bại thì đặt `error_queue_at`; row không được upload tự động
  nữa. Còn media được giữ tới hạn 72 giờ, catalog không mất.
- Unknown send, lỗi quyền/channel toàn cục, rate limit và hàng backpressure
  không được ném vào retry media để thử gửi lần nữa.

Lỗi remux cũ được giữ cô lập có thể nhường đường cho file khỏe tiếp theo khi
`SyncQueue._quarantined_reviews()` xác nhận đủ điều kiện. “Bỏ qua lỗi” không có
nghĩa mark uploaded, xóa receipt hoặc che mất bộ đếm lỗi.

## 8. Channel riêng, mục lục và bot Telegram

### 8.1 Bot nhận biết channel bằng cách nào?

Bot API không có lệnh liệt kê tùy ý tất cả channel của tài khoản. Repo dùng
`ChannelDirectory`: lưu `my_chat_member`, `channel_post`, các channel ID đã cấu
hình và kết quả `getChat`/kiểm tra quyền. Vì vậy dropdown chỉ hiện những channel
bot **đã biết**, không phải toàn bộ channel Telegram của chủ tài khoản.

Cách cấu hình:

1. Tạo private channel cho từng camera; thêm đúng bot của nhà làm admin.
2. Cho quyền **Post Messages** và **Edit Messages** để lưu video và cập nhật
   chỉ mục. Khi bật index, `verify_channel()` kiểm tra `can_edit_messages`;
   repo không dùng cờ `can_pin_messages` để xác nhận channel sẵn sàng.
3. Dashboard thêm/kiểm tra channel bằng ID dạng `-100...`; refresh danh sách khi
   cần, chọn channel theo tên trong form camera.
4. Bật `channel_enabled`; kiểm tra quyền ra `ready` trước khi chạy.
5. Bật `MULTI_CHANNEL_ROUTING=true` khi các binding đã sẵn sàng. Không có fallback
   về owner hoặc channel chung khi camera thiếu đích.

`TELEGRAM_STORAGE_CHANNEL_ID` trong `.env` là **một ID** cho legacy single-channel,
không phải danh sách phân cách dấu phẩy. Bind nhiều channel lưu theo camera
trong DB qua dashboard. `channel_name` là alias thủ công cho hiển thị, không đổi
ID Telegram hoặc tự đổi tên channel thật. Unique index giữ một channel cho một
camera đang cấu hình; đổi channel chỉ ảnh hưởng upload mới, link cũ vẫn trỏ đích
đã xác nhận.

### 8.2 Mục lục có hệ thống

`ChannelIndex` dựng các node `root`, `years`, `year`, `month`, `day`, `hour`.
Root dùng tên camera thay cho mã nội bộ; bản hiện tại hiển thị Video mới nhất,
Hôm nay (hoặc ngày gần nhất có video), Tháng hiện tại, Kho lưu trữ theo năm và
Bot quản lý. Các node con phân năm/tháng/ngày/giờ, có điều hướng trước/sau dựa
trên bản ghi đã uploaded, chưa xóa mềm. **Chỉ root được tự ghim**; không tự ghim
tất cả tin năm/tháng/ngày/giờ thành nhiều pin.
Không tổ chức bằng cách di chuyển các video Telegram cũ hoặc upload lại chúng.

Sau commit upload hoặc thay tên/xóa/khôi phục, một durable outbox enqueue việc
cập nhật mục lục cùng transaction. Thread index xử lý riêng, debounce mặc định
60 giây (hợp lệ 10–300), không làm upload lại video khi sửa tin nhắn chỉ mục.
Nếu gửi **tin chỉ mục** bị gián đoạn chưa biết message ID, index cũng có trạng
thái cần đối soát thay vì tạo liên tiếp tin trùng.

Các lệnh vận hành:

```sh
# Xem kế hoạch, chưa sửa Telegram
docker compose exec archive python -m archive_app rebuild-index --camera CAMERA_ID

# Sau khi đã xem kế hoạch và xác nhận camera/đích
docker compose exec archive python -m archive_app rebuild-index --camera CAMERA_ID --apply
```

### 8.3 Quyền xem và hành vi menu

- `TELEGRAM_OWNER_USER_ID` là user ID dương; owner luôn có trong viewer set.
- `TELEGRAM_ALLOWED_USER_IDS` là các ID dương, ngăn bằng dấu phẩy/khoảng trắng.
  Chỉ private chat có `chat.id == from.id` và ID được phép mới chạy thao tác.
- Allowlist cho phép xem, tải, xóa mềm và khôi phục kho chung; không phải chỉ
  quyền xem. Quyền vào private channel vẫn là thành viên Telegram độc lập.
- `Xem` trong chế độ channel mở post video gốc Telegram, không dùng VPS player
  và không tải lại qua VPS. Client Telegram có thể cần tải/buffer trước phát;
  Bot API không điều khiển native autoplay của client.
- `Tải`/replay dùng file ID hoặc vị trí đã lưu; không tự tìm video trong channel
  theo tên. Bot không quét ngược toàn bộ lịch sử Telegram để dựng catalog mới.
- Menu con được trình bày theo route hiện tại, có Quay lại; trạng thái route và
  active menu tách riêng khỏi upload. Màu `style=success` là gợi ý cho client hỗ
  trợ, không đảm bảo mọi Telegram client vẽ giống nhau.
- Hôm nay/Hôm qua/6 giờ trước/Tuần này/Tuần trước cố định mốc thời gian tại lúc
  chọn; phân trang/sort không tự trôi cửa sổ sang ngày mới.
- Tùy chọn thời gian chấp nhận `DD/MM/YY` hoặc `DD/MM/YYYY`; năm hai chữ số là
  2000–2099. Có hỗ trợ datetime cũ `DD/MM/YYYY HH:MM`, `YYYY-MM-DD HH:MM`. Chọn
  khoảng tối đa 31 ngày; ngày kết thúc khi nhập date-only được tính trọn ngày
  bằng mốc 00:00 ngày kế tiếp. Múi giờ dùng `DISPLAY_TIMEZONE`; input session 10 phút và
  token range 24 giờ.

### 8.4 Tải toàn bộ thành album

`TelegramBulk` lưu job riêng, snapshot lựa chọn/ordering và gửi lại bằng ID
media, không download cả kho về VPS để ZIP. Batch tối đa 10 item/album;
nhánh xử lý còn lại tuân theo giới hạn API. Mặc định tối đa 5000 item/job,
32 job active, 128 API requests/job và khoảng 31 giây giữa các batch của cùng
chat; tải nhiều album không đồng nghĩa gửi hàng nghìn video ngay tức thì.
Polling bot và SD job vẫn chạy trong lúc album chờ hạn mức.

### 8.5 Xóa và khôi phục: đúng ý nghĩa hiện tại của code

Nút Xóa có xác nhận, gọi `Archive.soft_delete()` và ghi `recording_audit`
(actor, thời gian, delete/restore). Video bị ẩn khỏi **catalog chung** và mục
lục, có thể khôi phục từ Thùng rác; receipt/file ID được giữ nguyên.

Đây **không phải xóa vật lý post/video khỏi channel Telegram**. Thành viên đã
có link/post vẫn có thể mở trực tiếp. Không nhầm tính năng khôi phục catalog
với khôi phục video đã bị admin xóa trên Telegram.

Nguồn: `channel_directory.py`, `channel_index.py`, `channel_index_schema.py`,
`telegram.py`, `telegram_menu.py`, `telegram_bulk.py`, `core.py:soft_delete`.

## 9. Dashboard, credential và cấu hình bền vững

Dashboard publish `DASHBOARD_BIND_IP=0.0.0.0`, `DASHBOARD_PORT=8080` mặc định:
truy cập `http://VPS_IP:DASHBOARD_PORT`. Cổng có thể khác theo từng nhà.

Lần đầu chưa có `dashboard_auth.sqlite`: username/password **admin/admin**.
Dashboard bắt đổi trước khi dùng chức năng quản lý. Credential mới được PBKDF2
SHA256 600.000 vòng + salt, không lưu password rõ trong auth DB. Password mới
8–128 ký tự; username 3–64 ký tự thuộc `[A-Za-z0-9_.-]`.

Session 12 giờ ở RAM của dashboard, cookie HttpOnly/SameSite=Strict; thay
credential tăng version và hủy session. Sau restart dashboard phải login lại.
Mutation có kiểm tra Origin/CSRF token. `DASHBOARD_COOKIE_SECURE=true` chỉ khi
truy cập bằng HTTPS phù hợp; bật khi vẫn dùng HTTP sẽ làm client không gửi cookie.

| Cấu hình | Nơi lưu/chỉnh | Hiệu lực |
|---|---|---|
| Token, API mode, global upload, tenant, endpoint/volume | `.env` và Compose | Recreate service đọc env; `restart` đơn thuần không nạp env mới |
| Camera name/IP/MAC/credentials/backend/channel | Dashboard → catalog/file secret trong state | Qua transaction và điều kiện không đổi binding khi đang xử lý |
| Worker 1–5 | Dashboard → key `pipeline_sd_download_workers` | Các job kế tiếp, không restart |
| Luồng 1–2/camera | Form camera | Lượt SD sau, job hiện tại dùng config snapshot |
| Username/password dashboard | Màn thay tài khoản | Lưu DB auth riêng, không sửa token bot |
| Channel title/alias | Directory và `camera.channel_name` | Hiển thị khác định tuyến bằng chat ID |

Xóa camera trong dashboard cần gõ đúng tên hiện tại. Nếu có job/ingest/upload/
index đang chạy thì bị chặn thay vì cắt file giữa chừng. Khi xóa được, camera
được tombstone, tắt và tháo binding, password camera bị gỡ; video/catalog cũ
được giữ. ID đã xóa không tự được manifest cũ làm sống lại.

## 10. Bảng giá trị mặc định: không nhầm thư viện với Compose

`Settings` hỗ trợ dùng trực tiếp trong thư viện/tests. Production trong repo
được chọn bởi Compose và `.env.example`, nên đọc đúng cột:

| Biến | `Settings.from_env()` khi không có env | Base Compose / override chuẩn |
|---|---|---|
| `MEDIA_MODE` | `raw` | `remux_copy` |
| `TELEGRAM_DESTINATION` | `owner_private` | `channel` |
| `KEEP_CACHE` | `true` | `false`; local override cố định false |
| `ENABLE_UPLOAD` | `false` | `false`; người triển khai chủ động bật |
| `MULTI_CHANNEL_ROUTING` | `false` | `false`; bật sau binding camera |
| `CHANNEL_INDEX_ENABLED` | `true` | `true` |
| `TELEGRAM_API_MODE` | `cloud` | base cloud; local override `local` |
| `TELEGRAM_API_BASE` | `https://api.telegram.org` | local override `http://telegram-bot-api:8081` |
| `TELEGRAM_UPLOAD_TRANSPORT` | `multipart` | base multipart; local override `local_file` |
| `TELEGRAM_MAX_BYTES` | cloud 50.000.000/local 2.000.000.000 | base 50.000.000; local override 2.000.000.000 |
| `TELEGRAM_LOCAL_UPLOAD_ROOT` | trống | local override `/cache` |
| `CACHE_RETENTION_HOURS` | `0` | `0` |
| `ERROR_RETENTION_HOURS` | `72` | `72` |
| `CACHE_MAX_GB` / `CACHE_MIN_FREE_GB` | `100` / `5` | Qua env_file; cùng default nếu không đặt |
| `BOT_API_SPOOL_MAX_GB` | `5` | `5`, budget đĩa hợp tác |
| `SD_SYNC_INTERVAL_SECONDS` | Runner đọc `900` | `900` |
| `SD_MAX_FILES_PER_JOB` | `10` | `10` |
| `SD_DOWNLOAD_WORKERS` | `1` | `1`, DB override được ưu tiên |
| `SD_DOWNLOAD_THREADS` | `1` | `1`, camera override được ưu tiên |
| `SD_DOWNLOAD_SPEED_KBPS` | `0` | `0`, không phải bitrate encoder |
| `DISPLAY_TIMEZONE` | `Asia/Ho_Chi_Minh` | cùng default |
| `LAN_IDENTITY_URL` | trống | Cần `compose.identity-client.yaml` khi dùng Auto IP |
| `SD_COLLECTOR_URL` | trống | Chỉ `compose.remote-sd.yaml` đặt từ `REMOTE_SD_COLLECTOR_URL` |

GB/MB trong budget và UI dùng hệ thập phân 1e9/1e6; không phải GiB/MiB.
Mật khẩu/token thật chỉ nằm ở `.env` hoặc secret file của từng hệ thống, không
đưa vào Markdown, commit, log hoặc command line chẩn đoán.

## 11. Tailscale, box metadata và collector tùy chọn

### 11.1 Chế độ trực tiếp được ưu tiên cho box nhẹ

VPS phải có route tới đúng LAN nhà; Tailscale subnet router quảng bá subnet đã
được duyệt và VPS chấp nhận route theo cấu hình hệ thống. Observer dùng IP
Tailscale riêng, nhưng media VPS→camera vẫn đi qua subnet route. Tắt observer
không biến nó thành collector; tắt Auto IP cho camera thì có thể dùng IP thủ công.

Trước khi kết luận camera hỏng, đối chiếu:

1. Các route quảng bá trùng subnet từ nhà/box khác.
2. IP camera mới sau DHCP và MAC đúng tại LAN.
3. Camera SD port trực tiếp từ VPS qua Tailscale và từ máy LAN.
4. Phân biệt route, NAT/conntrack, DNS và camera auth; probe TCP timeout không
   phải bằng chứng mật khẩu đã mất.

Repo không tự đổi primary IP box, tạo IP phụ, SNAT, policy route, DHCP pool,
router advertisement, IPv6 address hoặc IPv6 proxy. Một workaround nguồn IPv4
`.250` từng dùng tại Bama là thay đổi runtime riêng của hiện trường, **không
phải default hoặc cấu hình persistent trong repo**. Không sao chép sang nhà
khác, không gỡ khi dọn rác; phải kiểm tra DHCP reservation và phụ thuộc thực tế.

Box có thể đồng thời chạy dự án **`clbip` tạo/proxy IPv6**, là ứng dụng khác.
Không prune container/volume của dự án đó hoặc sửa interface, DNS, sysctl,
iptables/nftables chỉ để dọn camera. Việc IPv6 có lỗi cần điều tra tuyến IPv6/
DNS/proxy riêng, không mặc định giải thích bằng hai địa chỉ IPv4.

### 11.2 Collector là lựa chọn khác, không phải bắt buộc

`compose.collector.yaml` trên LAN + `compose.remote-sd.yaml` trên VPS chủ động
chọn đường `camera → collector cache → VPS`. Collector có SDK cùng kiến trúc
box, nhận credential camera cho phiên tải và chuyển raw nguồn; VPS vẫn catalog,
remux và upload. Không remux/transcode tại collector.

Collector dùng token riêng và truyền nguồn theo phiên HTTP. File tạm được xóa
trong `finally` khi phiên truyền kết thúc, kể cả nhánh lỗi/ngắt kết nối; không có
bước chờ xác nhận Telegram hoặc một ACK catalog từ VPS để giữ file tại box.
VPS kiểm tra đủ dữ liệu và tự chịu trách nhiệm catalog/upload; đọc
`docs/LAN_COLLECTOR.md` trước khi dùng. **Không bật collector để
chữa nhãn MAC** và không trộn token SD collector với observer. Với yêu cầu box
không trung chuyển của Bama, giữ override remote SD tắt và không khởi động
collector, chỉ giữ observer nếu cần Auto IP.

## 12. Quy trình cài/update/backup/rollback thực dụng

Các lệnh dưới đây chạy trên host Linux trong đúng thư mục nhà. Thay `HOUSE_DIR`
và endpoint riêng; không đổi project name hoặc volume cũ. Cấu hình mẫu chuẩn
VPS trực tiếp + Local API + SDK + MAC observer:

```sh
cd HOUSE_DIR

# Có thể lưu hai biến này trong .env; giữ nguyên project cũ khi nâng cấp.
export COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.sdk.yaml:compose.identity-client.yaml
export COMPOSE_PROFILES=local-api

docker compose config --quiet
docker compose ps
docker compose exec archive python -m archive_app doctor
docker compose exec archive python -m archive_app health
```

`docker compose config` không kèm `--quiet` có thể in token đã interpolate; không
đưa output đó vào chat/log công khai. Nếu không dùng Auto IP, bỏ đúng identity
client override và tắt Auto IP theo cấu hình camera; nếu không dùng Local API,
chọn đúng cloud mode/limit, không chỉ xóa file Compose một cách tùy tiện.

### 12.1 Backup trước thay đổi

```sh
# Lên lịch bảo trì để runner hoàn tất lượt đã nhận rồi mới dừng sạch.
# CLI backup cũng chiếm worker.lock; không exec nó song song runner đang chạy.
docker compose stop archive

# Snapshot catalog nhất quán bằng SQLite backup API trên cùng volume/project.
docker compose run --rm --no-deps archive backup

# Chỉ bật lại sau khi thao tác bảo trì/kiểm tra snapshot cần thiết đã hoàn tất.
docker compose up -d archive
```

Backup catalog tự động mỗi ngày theo timezone hiển thị, giữ ít nhất 7 snapshot
gần nhất, kể cả lịch có khoảng trống do mất điện. Backup dùng lock, partial,
`PRAGMA quick_check`, fsync và atomic rename. Migration multi-channel có snapshot
catalog trước schema migration nếu cấu trúc cũ phù hợp.

**Backup DB tự động không bao gồm** MP4/cache, `.env`, secret camera, token
observer/collector và auth DB. Cần backup riêng các file đó trong vùng riêng tư.
Phục hồi catalog về quá khứ có thể làm mất receipt mới và gây gửi trùng; trước
restore phải đối soát mốc upload, không ghi đè DB đang chạy.

### 12.2 Update Git và image

```sh
cd HOUSE_DIR
git status --short
git pull --ff-only
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --since 10m --tail 100 archive
```

`pull` chỉ tải image; `up -d` mới recreate service dùng image/config mới.
`restart` không thay image hoặc env. Ghi lại commit, digest image hiện tại và
tập Compose trước update. Dùng tag/digest cố định khi cần quay lại chính xác,
không coi `latest` là bản rollback bất biến. Không dùng `down -v` cho hệ thống
đang giữ catalog. Chỉ rebuild/publish image khi code/image thực sự đổi;
documentation-only không đòi restart pipeline.

Sau update kiểm tra health, worker heartbeat, một job SD thật, receipt Telegram,
việc dọn MP4/raw, menu bot và freshness observer. Số tests trong một báo cáo là
kết quả của commit đó, không phải cam kết cố định cho mọi phiên bản sau.

### 12.3 Rollback đúng lớp

1. Đổi `ARCHIVE_IMAGE` về digest/tag đã lưu và `up -d` trên cùng project/volume;
   không tự downgrade schema hoặc hồi DB nếu code cũ không tương thích.
2. Giữ `TENANT_ID`, ID camera, channel history và secret cùng bộ state.
3. Chỉ restore DB từ backup đã kiểm tra khi thật sự cần; ngừng writer trước và
   đối soát receipt sau mốc backup.
4. Rollback IP alias/SNAT/IPv6 là cấu hình host riêng, dùng bản sao/rollback đã
   ghi nhận của đúng thay đổi; không trộn với rollback app.
5. Kiểm tra khôi phục trên bản sao trước, rồi mới áp dụng production theo yêu
   cầu. Không dùng test rollback để làm gián đoạn upload đang khỏe.

## 13. Sau mất điện/modem đổi IP

1. Kiểm tra box đã lên LAN và Tailscale; default gateway/IPv4/IPv6 không bị lệch.
2. Kiểm tra subnet quảng bá và route VPS đang chọn. Gỡ quảng bá nhầm ở nguồn cấu
   hình của nó, không xóa hàng loạt route để thử.
3. Observer phải lên đúng interface/subnet và có proof mới. So sánh MAC với tên
   camera, không sửa theo IP cũ vì DHCP có thể cấp IP đó cho thiết bị khác.
4. Lượt sync sau `resolve_camera()` chuyển binding theo snapshot mới, giữ ID và
   password. Nếu đang giữa job đã search trên IP cũ, guard dừng có kiểm soát;
   job tiếp theo mới dùng mapping mới.
5. Kiểm tra mã SD thực tế. Studio qua IP LAN hoạt động chỉ chứng minh thiết bị
   truy cập được từ máy đó, chưa chứng minh tuyến/địa chỉ nguồn của VPS đúng.
6. Kiểm tra `upload_unknown` do cúp điện đúng lúc POST; đối soát thay vì xóa hàng
   đợi hoặc reset upload count. Camera/cached media khác vẫn phải được giữ.

## 14. Đọc “Cần xử lý”, tốc độ và chọn bước kiểm tra

| Hiển thị/code | Kiểm tra đúng | Không suy diễn |
|---|---|---|
| `blocked / sd_batch_limit` | Batch vừa nhường hàng; xem có job `queued` kế tiếp và `sd_requeued` | Không tự kết luận SDK treo |
| `upload_disabled` / `camera_upload_disabled` | Global `ENABLE_UPLOAD` và switch Upload từng camera | Start sync không tự bật global upload |
| `camera_channel_not_ready`, `channel_*` | Đúng bot token, ID private channel, Post/Edit permissions, binding active | Tên bot giống nhau chưa đủ xác nhận đúng bot ID |
| `owner_not_started` | Chế độ owner_private cần owner `/start` | Không áp đặt gate này cho channel storage |
| `sd_credentials_missing` / `sd_auth_failed` | Credential của đúng camera; ID ổn định, auth SDK trực tiếp | Cổng mở hoặc cloud xem được không chứng minh SD auth |
| `sd_sdk_missing` / arch/load error | SDK đúng CPU host, đường bind đủ thư viện | Không chép SDK x86 sang ARM64 |
| `sd_native_worker_timeout` | Deadline operation, kết nối, camera, source bytes có tăng không | Progress tick không cho phép kéo timeout mãi |
| `sd_identity_*` / nhãn MAC | Observer health/token/subnet/proof TTL/ICMP/neighbor/conflict | MAC uppercase không phải lỗi nếu đã normalize |
| “Chưa thấy cổng mở” | Route đúng nhà, IP sau DHCP, probe từ VPS và LAN | Không phải bằng chứng mật khẩu bị xóa |
| `failed_remux` / `sd_remux_*` | Header/codec/timestamp của file, retained raw, retry/quarantine | Không thêm transcoder để che lỗi |
| `upload_spool_budget` / `remux_cache_budget` | Dung lượng spool/cache/disk và reservation đang giữ | Không xóa media đang in-flight |
| `rate_limited` | Chờ retry deadline Telegram | Không tăng upload thread hoặc reset counter liên tục |
| `upload_unknown` | Đối soát receipt đúng camera/bot/channel | Không chuyển `downloaded` hàng loạt để gửi lại |
| `error_queue_at` có giá trị | Đã park sau retry; xem counter, retention và lỗi cụ thể | Không còn là hàng upload bình thường |
| Video/Đã lưu không bằng nhau | Xem từng status, deleted, expired, queued error | Không phải mọi phần chênh đều là dữ liệu mất |

### 14.1 Ý nghĩa thông số tốc độ

- SD: byte đang tăng tại staging, elapsed và trung bình file Camera→VPS. Chưa có
  byte thì trạng thái chờ, không giả một số tốc độ 0 như phép đo hoàn chỉnh.
- Upload: Local Bot API không trả byte-progress cloud. UI hiển thị file đang
  gửi/thời gian chờ và **tốc độ trung bình sau xác nhận**: số byte file chia
  thời gian request thành công; không phải tốc độ dây tức thời.
- `upload_seconds` có thể gồm chờ lock; `upload_request_seconds` chỉ cộng request
  thành công, tránh dùng cả thời gian chờ làm tốc độ mạng.
- Metric upload gần nhất được giữ qua các batch để không biến mất giữa các job.
- UI dùng MB/s (1e6 byte/s). `0.3 MB/s` khác `0.3 Mb/s` (bit/s); phải thống nhất
  đơn vị trước khi so với băng thông mạng.

Đo tối ưu nên tách: search/login, download SD, ingest/remux, chờ upload,
request Telegram và backlog. So cùng file ở LAN/VPS nếu cần, đặt timeout và dọn
file đo riêng sau phép thử; không dùng file chưa xác nhận của pipeline làm rác.

## 15. Chính sách dọn rác và bảo trì code

Có thể dọn sau khi xác minh: `__pycache__`/`.pyc`, fixture sinh tự động, image
không còn container/config/rollback tham chiếu, volume kiểm thử có project label
đúng và không có container dùng, SDK benchmark duplicate đã không còn bind.

Cần giữ: source SDK mount đang chạy, backup/rollback gần nhất, input thật,
catalog/auth/secrets, media chưa xác nhận, TDLib state/spool active, cấu hình
Tailscale/IPv6/proxy/alias/NAT và code tùy chọn còn có entrypoint/Compose/tests.
Một module ít import không có nghĩa code chết: CLI `lan-identity`, `collector`,
`network-routes`, workflow CI và command entrypoint đều có thể gọi động.

Mỗi lần dọn nên ghi: exact path/project/tag, loại dữ liệu, reference check,
backup hash nếu cần, lượng xóa thực tế, containers StartedAt trước/sau và test
khôi phục trên bản sao. Không cộng kích thước tag Docker thành dung lượng đã
thu hồi: các tag có thể chia sẻ cùng layer. Không chạy global prune cho host
đang có ứng dụng khác.

## 16. Bản đồ nguồn để tra cứu/sửa tiếp

| Nguồn | Hàm/lớp hoặc nội dung chính |
|---|---|
| `archive_app/__main__.py` | CLI, `mutation_lock`, startup recovery, scheduler, polling/index/bulk threads |
| `archive_app/core.py` | `Settings`, `Archive`, DB/schema, `record_key`, normalize/remux, claim/receipt, cleanup/backup |
| `archive_app/sync.py` | `SyncQueue.enqueue/recover/_claim/_run/_finish`, stats, gates và FIFO continuation |
| `archive_app/pipeline_runtime.py` | Worker admission 1–5, upload serialization và cache reservations |
| `archive_app/sd_source.py` | ISAPI/native provider, subprocess protocol, search paging, staged download, 1–2 file threads |
| `archive_app/camera_identity.py` | MAC normalization, client snapshot, `resolve_camera`, `guard_camera` |
| `archive_app/lan_identity.py`, `lan_echo.py` | ICMP/netlink proofs, TTL/hints, metadata API |
| `archive_app/network_routes.py`, `discovery.py` | Route publication và scan candidate LAN |
| `archive_app/error_queue.py` | `retry_one`, giới hạn 3 retry/900 giây và parking |
| `archive_app/telegram.py` | Verify exact bot/channel, send/receipt/backoff, poll/authorization/navigation |
| `archive_app/telegram_menu.py` | Time window/input/ordering và camera/video menus |
| `archive_app/telegram_bulk.py` | Album job, file-ID replay, rate/paging/cancel/recovery |
| `archive_app/channel_directory.py` | Channel catalog qua event/config/getChat, alias khác ID |
| `archive_app/channel_index.py` | Durable index outbox, root/year/month/day/hour, render/rebuild/reconcile |
| `archive_app/local_upload.py`, `upload_spool.py` | Managed file URI và admission spool 5 GB |
| `archive_app/dashboard.py`, `dashboard_auth.py`, `web/` | HTTP API, auth, CSRF, UI/control/log/metrics |
| `archive_app/sd_collector.py`, `sd_collector_client.py` | Collector LAN **tùy chọn**, không phải observer |
| `compose*.yaml`, `.env.example`, `Dockerfile*` | Roles/mounts/defaults/override/multiarch test stage |
| `tests/`, `scripts/`, `.github/workflows/` | Unit/integration/smoke/Compose/image-manifest checks |

Tài liệu chuyên đề vẫn được giữ để tra sâu: `MAC_BINDING.md`,
`SD_PARALLEL_DOWNLOADS.md`, `ERROR_QUEUE.md`, `LOCAL_FILE_UPLOAD.md`,
`CACHE_RETENTION.md`, `CHANNEL_NAVIGATION_MODEL.md`, `MULTI_CHANNEL.md`,
`CAMERA_CONTROLS.md`, `LAN_COLLECTOR.md`. Khi tài liệu cũ mô tả khác defaults
hoặc luồng hiện tại, đối chiếu source/Compose và phần phân biệt defaults ở trên
trước khi vận hành; không xóa thiết kế lịch sử chỉ vì tính năng hiện tạm tắt.
