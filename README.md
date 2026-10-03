# MyCameraTele — Telegram Private Archive

Một bot lưu recording vào **private chat của owner**; owner và các viewer được
cho phép duyệt lại lịch sử ngay trong private chat riêng của mỗi người.
Không cần channel/group. SQLite là mục lục, Telegram chứa media, bot là browser.

```text
Camera SD → lịch quét tự động / Start sync
  → HCNetSDK native hoặc ISAPI của thiết bị → download file đã đóng
  → kiểm tra media, remux MP4 → cache + SQLite → private chat owner
  → Camera → Năm → Tháng → Ngày → Video
  → phát lại bằng file_id trong private chat của người được cho phép
```

**Lấy SD có điều kiện theo giao thức thực tế của camera, không theo tên model.**
Adapter native dùng Linux HCNetSDK chính thức, đúng kiến trúc máy chạy Docker;
ISAPI dùng Digest và chỉ chấp nhận kết quả search recording thật. Thư viện SDK
không đi kèm image; xem mục 2. Các model C6N/H6c và firmware đã cung cấp **chưa
được kiểm thử download trên thiết bị thật**. Cổng mở / RTSP live không chứng minh
đã truy cập được SD. MP4/manifest xuất từ Studio vẫn là nguồn nhập tùy chọn.

## 1. Cài đặt trên Ubuntu / Armbian

Dùng Docker Engine và Compose plugin theo hướng dẫn chính thức cho
[Ubuntu](https://docs.docker.com/engine/install/ubuntu/) hoặc
[Debian](https://docs.docker.com/engine/install/debian/). Kiểm tra userland:

```bash
uname -m
dpkg --print-architecture
docker version
docker compose version
```

Ứng dụng hướng tới `linux/amd64`, `linux/arm64`, `linux/arm/v7`; `aarch64` thường
đi với `arm64`, nhưng kernel 64-bit có thể chạy userland `armhf`. Image Local Bot
API có workflow native `amd64`/`arm64`, không công bố ARMv7 cho image tùy chọn này.
Docker tự chọn platform host; không ép image x86 trên ARM. Manifest/runtime của
**tag hoặc digest được triển khai** phải được đối chiếu với báo cáo CI tương ứng.

```bash
git clone https://github.com/bscongluanbui/mycameratele.git
cd mycameratele
mkdir -p input
cp .env.example .env
chmod 600 .env
docker compose config --quiet
docker compose pull archive dashboard
docker compose up -d archive dashboard
docker compose ps
```

Compose mặc định **pull-only**, không build trên board:

- `ARCHIVE_IMAGE=ghcr.io/bscongluanbui/mycameratele:latest` cho worker/dashboard.
- `BOT_API_IMAGE=ghcr.io/bscongluanbui/mycameratele-bot-api:latest` cho local API.
- Có thể pin tag/digest của phiên bản đã kiểm chứng trong `.env`.

Mặc định cloud test, `ENABLE_UPLOAD=false`, `KEEP_CACHE=true`; một cài đặt chưa
có credential vẫn khởi động dashboard/worker mà chưa gửi Telegram. Image dùng
UID/GID `10001:10001`; `input` chỉ đọc. SQLite/cache/tài khoản dashboard nằm trong
named volumes. Giữ project **`ezviz-telegram-archive`** và volume keys
`archive-state`, `archive-cache`, `bot-api-state` khi cập nhật để dùng lại dữ liệu.

### Dashboard

Mở **`http://IP_VPS:8080`**, thay `IP_VPS` bằng IP public của máy chạy Docker.
Compose mặc định publish `0.0.0.0:8080`; không cần SSH tunnel hay nhập token.
Với máy trong LAN, dùng `http://IP_MAY_DOCKER:8080`. VPS có firewall/security
group thì cho phép TCP ở cổng đã chọn để truy cập từ máy của bạn.

- Lần đầu đăng nhập: **username `admin`, password `admin`**.
- Sau lần đăng nhập đầu, form **Đổi tài khoản** xuất hiện trước các chức năng
  quản lý. Đặt mật khẩu mới từ 8 đến 128 ký tự; có thể giữ username `admin`
  hoặc đổi tên (3–64 ký tự, chữ/số/dấu `.`, `_`, `-`).
- Sau khi lưu, đăng nhập lại bằng tài khoản mới. Các phiên cũ bị hủy.
- Các lần sau, nút **Tài khoản** trên thanh trên cùng cho phép đổi username
  và mật khẩu với xác nhận mật khẩu hiện tại.
- Tài khoản chỉ được tạo **một lần** tại `/data/dashboard_auth.sqlite` trong
  volume `archive-state`. Restart, recreate, pull image mới giữ tài khoản đã
  đổi; không tự reset về `admin/admin`. `DASHBOARD_TOKEN` và file token cũ
  không còn cấp quyền đăng nhập.

Mật khẩu lưu dạng hash có salt riêng bằng PBKDF2-HMAC-SHA256/600.000 vòng, theo
[OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).
Cookie có HttpOnly/SameSite/CSRF; giới hạn lần đăng nhập; Đăng xuất hủy phiên.
Đặt `DASHBOARD_COOKIE_SECURE=true` khi triển khai qua HTTPS; giữ `false` khi
dùng trực tiếp HTTP IP:port. Không đưa mật khẩu thật vào source/image.

Port và interface có thể đổi trong `.env`:

```dotenv
DASHBOARD_BIND_IP=0.0.0.0
DASHBOARD_PORT=8080
DASHBOARD_COOKIE_SECURE=false
```

Nếu nâng cấp từ bản dashboard token, `.env` cũ có thể vẫn bind `127.0.0.1`.
Đổi `DASHBOARD_BIND_IP=0.0.0.0` rồi cập nhật:

```bash
git pull --ff-only
sed -i 's/^DASHBOARD_BIND_IP=.*/DASHBOARD_BIND_IP=0.0.0.0/' .env
docker compose pull archive dashboard
docker compose up -d archive dashboard
```

Nếu dùng local Bot API, giữ cả hai file Compose và profile trong lệnh cập nhật
như mục production bên dưới. Thay đổi tài khoản dashboard không đổi token bot,
allowlist, camera hay kho video. Backup toàn bộ volume `/data` giữ cả tài khoản.

## 2. Camera SD tự động, Start sync và công tắc upload

Dashboard cho thêm camera, đổi tên, địa chỉ, cổng và thông tin SD:

- **Username thiết bị** (thường `admin`) và **mật khẩu thiết bị / mã xác thực**:
  điền trực tiếp trong form, không phải mật khẩu tài khoản EZVIZ cloud.
  Edit để trống giữ mật khẩu cũ; lựa chọn xóa riêng để xóa credential.
  API không trả lại mật khẩu; file secret nằm trong volume `/data`, quyền 0600.
- Backend `auto`: ưu tiên native HCNetSDK khi đã mount SDK; nếu SDK chưa có thì
  thử ISAPI. Chọn `hcnetsdk` để dùng cổng thiết bị 8000; `isapi` dùng HTTP port.
- Channel thường 1, timezone `Asia/Ho_Chi_Minh`, lookback mặc định 168 giờ,
  tối đa 720 giờ. Chỉ nhập clip đã kết thúc ít nhất 2 phút; không ghi RTSP live.
- **Bật camera** điều khiển lấy nguồn + upload. **Upload Telegram** riêng từng
  camera mặc định bật (kể cả camera cũ khi nâng cấp); tắt chỉ ngừng upload,
  vẫn tải/kiểm tra video về cache. Bật lại dùng Start sync để đẩy phần còn chờ.
- **Start all** tạo job cho camera đang bật. **Start sync** ở mỗi card chỉ chạy
  camera đó. Thêm camera đang bật tự xếp job lần đầu. Job lưu trong SQLite,
  chống trùng, tiếp tục kiểm tra sau restart; không tự gửi lại upload mơ hồ.
- Worker tự tạo job mỗi `SD_SYNC_INTERVAL_SECONDS=300` giây. Nút Start bỏ qua
  thời gian chờ này; dashboard/bot chỉ xếp job, worker xử lý tải và upload.
- Telegram có `/sync`, nút **Start sync** ở menu chính và từng mục camera,
  công tắc upload và nút cập nhật trạng thái. Allowlist được kiểm tra trước
  thao tác; các gate owner `/start` và `ENABLE_UPLOAD=true` vẫn giữ nguyên.

Trạng thái hiển thị đang xếp hàng / kiểm tra / tải SD / upload / hoàn tất hoặc
lý do dừng. Không có worker heartbeat thì job vẫn queued; xem `docker compose
ps` và logs worker. Thiếu credential, SDK, tuyến mạng, giao thức không hỗ trợ,
file chưa đóng, cache đầy, Telegram chưa cấu hình đều phải hiện rõ, không giả
báo thành công. Nếu search SD thực sự trả 0 clip, job ghi nhận kết quả rỗng.

### Native HCNetSDK trong Docker

Lấy **Device Network SDK for Linux** từ
[Hikvision SDK](https://www.hikvision.com/en/support/download/sdk/), đúng CPU
**máy chạy container** (VPS x86_64 cần Linux64 x86_64; Armbian aarch64 cần
Linux ARM64). Trang catalogue Linux64 không tự chứng minh hỗ trợ ARM64;
[portal SDK của hãng](https://open.hikvision.com/download/5cda567cf47ae80dd41a54b3?type=20)
có thể cần đăng nhập để lấy gói đúng kiến trúc. Hiện chưa có binary ARM64
được kiểm chứng hoặc bundle trong image. Không dùng DLL Windows từ EZVIZ Studio, không dùng SDK ARM64
trên VPS x86. Giữ đủ thư mục lib, `HCNetSDKCom` và dependency của gói chính thức.
Nếu hãng không cung cấp gói đúng kiến trúc, native backend báo thiếu/incompatible;
ISAPI chỉ hoạt động nếu firmware thực sự có endpoint recording tương ứng.

```bash
# Đặt .so và HCNetSDKCom từ SDK chính thức vào một thư mục trên Docker HOST.
# .env: HCNETSDK_HOST_DIR=/absolute/path/to/official-sdk/lib
# Để VPS chấp nhận subnet route Armbian đã advertise/approve:
sudo tailscale set --accept-routes=true

docker compose -f compose.yaml -f compose.sdk.yaml pull archive dashboard
docker compose -f compose.yaml -f compose.sdk.yaml up -d archive dashboard
```

Với Local Bot API, giữ thêm `-f compose.local.yaml --profile local-api` trong
các lệnh trên. SDK chỉ mount vào worker, read-only; `/input` vẫn read-only,
download SD dùng `/cache/sd-stage`, không ghi vào thư mục Studio nguồn.
Sau lần cài SDK đầu, các lần update chỉ pull/up với cùng tập file Compose.

### VPS → Tailscale → Armbian → LAN camera

Armbian advertise `192.168.31.0/24`, route đã approve và ACL/grants cho phép VPS
đến camera. VPS Linux phải accept routes. Đây là các bước theo
[Tailscale subnet routers](https://tailscale.com/docs/features/subnet-routers).
Docker dùng network bridge mặc định; kiểm tra từ **worker container**, không chỉ
ping từ VPS. Không mở camera port ra Internet.

```bash
docker compose exec -T archive python -c "import socket; s=socket.create_connection(('192.168.31.166',8000),5); s.close(); print('C6N device port reachable')"
docker compose exec -T archive python -c "import socket; s=socket.create_connection(('192.168.31.137',8000),5); s.close(); print('H6c device port reachable')"
```

Các lệnh TCP này chỉ kiểm tra tuyến mạng. Sau đó nhập credential tại dashboard,
Start từng camera và đối chiếu `searched/downloaded/imported/uploaded` cùng
thời gian clip thật. Mã lỗi SDK/API chưa xác nhận tương thích phải được giữ lại.
Không chuyển sang quay live RTSP để thay thế video cũ trên SD.

### Nguồn Studio tùy chọn

Mã camera ổn định, tên hiển thị đổi được mà không đổi mã/lịch sử; manifest dùng
mã, không dùng tên. Camera từ manifest cũ có thể tự đăng ký.

Xuất recording bằng Studio, copy MP4 vào `input`, lấy đúng Start Time/End Time
của dòng đã xuất rồi tạo `input/manifest.json`:

```json
{
  "recordings": [
    {
      "record_id": "studio-recording-001",
      "camera": "living_room",
      "path": "/input/recording-001.mp4",
      "start_time": "REPLACE_WITH_STUDIO_START_ISO8601_OFFSET",
      "end_time": "REPLACE_WITH_STUDIO_END_ISO8601_OFFSET"
    }
  ]
}
```

Thay placeholders bằng thời gian thực, dạng `YYYY-MM-DDTHH:MM:SS+07:00`; parser
sẽ từ chối khi chưa thay. Tên file/PTS không chứng minh giờ ghi hình. SQLite lưu
UTC, `DISPLAY_TIMEZONE` chuyển đổi ngày/tháng tra cứu, `CAMERA_TIMEZONE` phải
khớp timezone của camera. Copy xong MP4 trước, đưa manifest hoàn chỉnh vào sau.
Worker quét `*.json` mỗi `SCAN_INTERVAL_SECONDS`.

```bash
# Dừng worker trước CLI ingest để dùng chung khóa ghi.
docker compose stop archive
docker compose run --rm archive ingest --manifest /input/manifest.json --dry-run
docker compose run --rm archive ingest --manifest /input/manifest.json
docker compose up -d archive
docker compose logs --tail=100 archive
```

Dry-run kiểm tra input/metadata, chưa gửi Telegram. File nguồn không bị xóa.
SQLite đối chiếu khóa camera/source/record ID; upload mơ hồ vào `upload_unknown`
để đối soát, không gửi lại mù. Cache giữ nguyên file chưa upload hoặc chưa commit
metadata thành công. Chỉnh `CACHE_MAX_GB`/`CACHE_MIN_FREE_GB` theo dung lượng ổ.

## 3. Owner, viewers và cloud test

Tạo bot qua [BotFather](https://core.telegram.org/bots/features#botfather).
**Owner ID là số nguyên dương của tài khoản người dùng**, không phải bot token,
username, số điện thoại hoặc ID channel/group âm. Cấu hình owner rõ ràng;
ứng dụng không nhận người nhắn đầu tiên làm owner.

Sửa `.env` trên máy triển khai; thay các slot bằng giá trị thật:

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_OWNER_USER_ID=YOUR_POSITIVE_TELEGRAM_USER_ID
TELEGRAM_BOT_USERNAME=YOUR_BOT_USERNAME_WITHOUT_AT
TELEGRAM_ALLOWED_USER_IDS=OWNER_ID,VIEWER_ID_2,VIEWER_ID_3
TELEGRAM_CHAT_ID=
TELEGRAM_API_BASE=https://api.telegram.org
TELEGRAM_API_MODE=cloud
TELEGRAM_MAX_BYTES=50000000
ENABLE_UPLOAD=false
KEEP_CACHE=true
```

Các ID trong allowlist phải là **số nguyên dương thật**, ngăn bằng dấu phẩy.
Owner được tự thêm vào quyền truy cập nếu không xuất hiện trong danh sách.
Để trống allowlist thì chỉ owner được truy cập; thêm ID để cho người khác xem.
`TELEGRAM_CHAT_ID` chỉ là alias cũ cho **cùng ID owner dương**; cấu hình mới để trống.
Username bot không có `@`; có thể để trống để `/start` lấy qua `getMe` và lưu lại.

```bash
docker compose up -d --force-recreate archive dashboard
```

Owner mở bot trên Telegram và bấm **Start / gửi `/start` trong private chat**.
Ứng dụng lưu trạng thái owner đã bắt đầu; chỉ `/start` đúng owner mới mở gate
cho automatic upload. [Telegram yêu cầu người dùng bắt đầu cuộc trò chuyện](https://core.telegram.org/bots#how-are-bots-different-from-users).
Viewer được cho phép cũng mở bot và `/start`, sau đó duyệt `/archive` trong chat
riêng của mình. Viewer không được thay owner và không mở gate upload của owner.
Mọi ID trong allowlist được **xem, tải, xóa khỏi kho chung và khôi phục** video;
quyền xóa này áp dụng cả video do camera khác ghi, không chỉ bản tin của viewer.

Khi owner `/start` thành công, sửa `.env` thành `ENABLE_UPLOAD=true` rồi recreate:

```bash
docker compose up -d --force-recreate archive dashboard
docker compose logs --tail=100 archive
```

Automatic upload **chỉ đến private chat owner**. Viewer chỉ nhận video khi chủ
động chọn xem, không nhận mọi recording mới. Một worker là consumer polling duy
nhất của bot; không chạy hai cài đặt cùng token. Lệnh/callback và replay đều
kiểm tra người dùng được phép và private chat của chính người đó.

### Tra cứu/phát lại

Dashboard và `/archive` duyệt **Camera → Năm → Tháng → Ngày → Video**, phân trang
và Cũ → Mới / Mới → Cũ theo thời gian ghi hình. Video qua nửa đêm có thể xuất hiện
ở cả hai ngày; end đúng 00:00 không tính sang ngày mới. `/today`, `/yesterday`,
`/last6h`, `/recent`, `/trash`, `/status` là các shortcut. Worker đăng ký Menu
commands của Telegram; gửi `/start` để nhận bàn phím nhanh phía dưới chat:

```text
Hôm nay / Hôm qua / 6 giờ trước → chọn Camera → danh sách video
                              → Xem / Tải / Xóa
```

Hôm nay/Hôm qua theo ngày lịch của `DISPLAY_TIMEZONE`. “6 giờ trước” là **6 giờ
gần nhất đến thời điểm bấm**, không phải một thời điểm đơn lẻ. Bot chỉ liệt kê
camera có video trong khoảng đã chọn, 10 camera/video mỗi trang; có Cũ → Mới /
Mới → Cũ. Video giao với khoảng thời gian được tính, gồm video qua nửa đêm;
video kết thúc đúng đầu khoảng không được tính. Phân trang/sort/quay lại giữ
nguyên khoảng ban đầu; bấm shortcut lại để lấy khoảng mới.

### Xem, tải và Thùng rác chung

- **Xem:** bot gửi video vào chat riêng của người được phép.
- **Tải:** bot gửi lại media gốc và hướng dẫn dùng nút tải / menu Telegram
  `Lưu video` hoặc `Save to Downloads`. Video vẫn dùng sendVideo, document vẫn
  dùng sendDocument; không đổi loại file_id hay re-upload binary. Đây là tải
  bằng client Telegram, không phát đường dẫn download chứa bot token.
- **Xóa:** nút đầu chỉ mở xác nhận; xác nhận gắn với ID người bấm, hết hạn sau
  5 phút. Xác nhận đưa video vào **Thùng rác kho chung**: mọi người mất quyền
  truy cập video đó qua catalog/menu/link bot cũ. Worker không tự nhập lại hay
  re-upload video đã xóa khi quét manifest cũ.
- **Khôi phục:** `/trash` hoặc nút `Khôi phục` đưa cùng video trở lại kho, giữ
  tên camera, thời gian, khóa và file_id; mọi người được phép có thể khôi phục.
  SQLite ghi audit xóa/khôi phục; click lặp không tạo thêm lần xóa.

Thùng rác là **xóa logic khỏi ứng dụng**, giữ metadata để khôi phục, không xóa
bản tin Telegram đã gửi hoặc bản đã tải về thiết bị. Telegram giới hạn
[deleteMessage ở bản tin dưới 48 giờ](https://core.telegram.org/bots/api#deletemessage);
tính năng này không tuyên bố xóa vật lý mọi bản sao media trên Telegram.
Cache đã upload vẫn theo retention hiện có; file nguồn `input` giữ nguyên.

Bot lưu message ID, `file_id` và `file_unique_id` sau upload được xác nhận/commit.
Chọn video gửi lại bằng `sendVideo`/`sendDocument` với **file_id**, không tải về
rồi upload binary lần nữa. Theo [Telegram Sending files](https://core.telegram.org/bots/api#sending-files),
file_id có thể tái sử dụng bởi **cùng bot**, không chuyển sang bot khác.
Dashboard mở link bot dạng `https://t.me/BOT_USERNAME?start=play_RECORD_KEY`;
người mở vẫn phải được phép. Khi username chưa được cấu hình/lấy từ getMe,
dashboard không đoán link channel. Cây thư mục là virtual filesystem trong
SQLite, không phải thư mục vật lý trên Telegram.

Cloud mode chỉ dành cho kiểm tra clip nhỏ, giới hạn binary upload 50 MB;
file vượt ngưỡng được giữ để xử lý, không tự chia/transcode. Dùng `sendVideo`
cho codec phù hợp, còn lại gửi `sendDocument`; không encode lại chỉ để ép video.
Sau khi chuyển sang Local API, bản ghi `needs_review` vì file thiếu/vượt ngưỡng
cũ cần lệnh rõ ràng; worker không tự retry các upload mơ hồ:

```bash
docker compose -f compose.yaml -f compose.local.yaml --profile local-api stop archive
docker compose -f compose.yaml -f compose.local.yaml --profile local-api run --rm archive retry-oversize --key RECORD_KEY
docker compose -f compose.yaml -f compose.local.yaml --profile local-api up -d archive
```

Lệnh chỉ đưa bản ghi chưa có file_id và lỗi `missing_or_oversize_file` vào hàng
đợi khi cache còn tồn tại và vừa ngưỡng API hiện tại; `upload_unknown` cần đối soát.

## 4. Production: official Local Bot API

Local API dùng nguồn chính thức `tdlib/telegram-bot-api`, pin commit trong
`Dockerfile.bot-api`; `compose.local.yaml` chọn endpoint local, tối đa
`2000000000` bytes, `KEEP_CACHE=false`, retention 24 giờ cho cả worker/dashboard.
Không âm thầm đổi sang cloud nếu local API ngừng hoạt động.

Lấy application `api_id`/`api_hash` theo
[hướng dẫn Telegram](https://core.telegram.org/api/obtaining_api_id), thêm `.env`:

```dotenv
TELEGRAM_API_ID=YOUR_APPLICATION_API_ID
TELEGRAM_API_HASH=YOUR_APPLICATION_API_HASH
ENABLE_UPLOAD=false
```

Owner/token/application credentials là trường bắt buộc trong production
Compose override. `ENABLE_UPLOAD` vẫn do bạn chọn trong `.env`; override không
tự bật upload. Nếu bot đang dùng cloud, gọi `logOut` trên cloud trước chuyển
endpoint theo [hướng dẫn migration chính thức](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server).

```bash
docker compose -f compose.yaml -f compose.local.yaml --profile local-api config --quiet
docker compose -f compose.yaml -f compose.local.yaml --profile local-api pull
docker compose -f compose.yaml -f compose.local.yaml --profile local-api up -d
docker compose -f compose.yaml -f compose.local.yaml --profile local-api ps
```

Owner `/start` với endpoint mới; xác nhận trạng thái, rồi đặt `ENABLE_UPLOAD=true`
và chạy lại lệnh `up -d --force-recreate archive dashboard` với **cùng hai file
Compose và profile**. Khi đã chọn local, luôn giữ các cờ này trong lệnh cập nhật;
lệnh chỉ dùng `compose.yaml` sẽ chọn lại cấu hình cloud mặc định.

Local API chỉ trong mạng Compose, **không publish 8081**; đọc `/cache:ro` cùng
đường dẫn worker, trạng thái API ở volume writable riêng. Theo
[Telegram Local Bot API](https://core.telegram.org/bots/api#using-a-local-bot-api-server),
local mode hỗ trợ upload tới 2000 MB. Không cần VPS hoặc middleware.

RAM runtime tùy workload/TDLib; không có một mức cố định suy ra từ dung lượng
file. Theo dõi **min/max/peak** trước và trong upload thực bằng `docker stats`
và log/OOM của host. File URI không có nghĩa toàn bộ server không dùng RAM.
Build TDLib là bài toán khác: compiler/job/architecture thay đổi bộ nhớ, có
[hướng dẫn chính thức cho low-memory build](https://github.com/tdlib/td#building).
Pull image có sẵn tránh compile trên board nhỏ. Test 100 MB, 500 MB, 1 GB là các
mốc nghiệm thu bằng **clip thật**, không phải kết quả đã đạt chỉ vì sparse-file,
mock API, kiểm tra config hoặc unit test thành công.

## 5. Retention và daily SQLite backup

Cloud/debug mặc định `KEEP_CACHE=true`. Production override đặt
`KEEP_CACHE=false`, `CACHE_RETENTION_HOURS=24`: chỉ xóa cache đã upload xác nhận,
có metadata Telegram, SQLite commit thành công và đủ thời gian retention.
File thất bại/mơ hồ/chưa upload được giữ; nguồn `input` không bị cleanup.

Worker tạo SQLite snapshot nhất quán hằng ngày trong `/data/backups`, giữ **7
snapshot ngày gần nhất**. Snapshot nằm cùng volume với DB nên cần copy ra ổ
độc lập nếu muốn chống hỏng ổ. SQLite mất thì media Telegram có thể còn nhưng
mất catalog. Khi thực hiện backup toàn bộ `/data`, dừng worker/dashboard trước:

```bash
mkdir -p backups
sudo chown 10001:10001 backups
sudo chmod 700 backups
docker compose stop archive dashboard
docker compose run --rm --no-deps --entrypoint python \
  -v "$PWD/backups:/backup" archive \
  -c 'import tarfile,time; p="/backup/state-"+time.strftime("%Y%m%d-%H%M%S")+".tar.gz"; t=tarfile.open(p,"w:gz"); t.add("/data",arcname="data"); t.close(); print(p)'
docker compose up -d archive dashboard
```

Nếu production local, thêm `-f compose.yaml -f compose.local.yaml --profile
local-api` ở mọi lệnh trên. Backup `.env`, cache/media nguồn riêng nếu cần bản
sao video độc lập. Telegram là nơi gửi/lưu media, không thay thế chính sách
backup của bạn.

## 6. Migration từ cấu hình channel cũ

1. Giữ **cùng bot/token, project và volumes**, backup trước cập nhật.
2. Đặt `TELEGRAM_OWNER_USER_ID` rõ ràng; xóa giá trị channel âm khỏi alias
   `TELEGRAM_CHAT_ID`, đặt alias trống hoặc bằng owner. Owner được tự cấp quyền;
   các viewer cũ vẫn phải có ID dương trong allowlist mới.
3. Owner `/start`; chọn cloud test hoặc production override; chỉ bật upload
   sau khi gate của owner đã được xác nhận.

Bản ghi cũ cùng message/file ID vẫn ở SQLite; **không reset DB, xóa lịch sử,
reupload toàn bộ hay đổi chủ recording**. `file_id` cũ hợp lệ với cùng bot có
thể gửi lại vào private chat người được phép. Message đã gửi trước đây không
bị xóa/chỉnh sửa; UI mới không tạo link private-channel từ chat ID cũ.
Nếu đổi sang bot khác, file_id của bot cũ không dùng chung; đó là migration
khác, không được coi là replay cùng bot.

### Update/rollback giữ dữ liệu

```bash
git pull --ff-only
docker compose config --quiet
docker compose pull archive dashboard
docker compose up -d archive dashboard
docker compose ps
```

Production local dùng hai Compose files/profile ở trên và pull thêm Bot API.
Trước upgrade, lưu image digest và SQLite snapshot. Rollback image bằng
`ARCHIVE_IMAGE=ghcr.io/bscongluanbui/mycameratele@sha256:PREVIOUS_DIGEST`, pull và
recreate; phiên bản cũ phải tương thích schema hoặc dùng DB backup nhất quán
phù hợp. Giữ volumes, không dùng `down -v`. Đổi image/phục hồi DB không rollback
message đã gửi. Đối chiếu `upload_unknown` với message thật trước dùng CLI
`archive reconcile --help`; không retry binary mù.

## 7. Phát triển/test local, tách khỏi release GHCR

Default Compose luôn pull-only. Build source cần override rõ ràng:

```bash
docker compose -f compose.yaml -f compose.build.yaml build archive dashboard
docker compose -f compose.yaml -f compose.build.yaml run --rm --entrypoint python \
  archive -m unittest discover -s tests -v
docker compose -f compose.yaml -f compose.build.yaml up -d archive dashboard
```

Để test local-mode source, thêm `-f compose.local.yaml --profile local-api`
trước command; production override vẫn yêu cầu owner/token/application
credentials. Sửa file/build/test tại máy local không tự push Git, publish
registry hoặc cập nhật image GHCR. Báo cáo kiểm chứng phải phân biệt config,
unit test, runtime kiến trúc, upload Telegram thật và downloader SD thật.

## Nguồn chính thức

- [EZVIZ Studio export recording](https://support.ezviz.com/faq/article/How-to-download-the-recorded-video-clips)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/)
