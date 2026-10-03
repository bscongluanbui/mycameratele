# MyCameraTele — House01 Private Channel Archive

**House01 MVP** dùng một private channel làm kho video, SQLite làm mục lục và
bot hiện có làm giao diện tra cứu. Recording mới chỉ upload vào private channel;
owner/viewer nhận phản hồi trong private chat **khi chủ động yêu cầu**, không nhận
mỗi recording mới. Mục lục: **Camera → Năm → Tháng → Ngày → Video**, có Cũ → Mới.

```text
Camera SD → lịch 15 phút / Start sync
  → HCNetSDK hoặc ISAPI → tải recording đã đóng
  → MP4 remux-copy (-c copy, không encode) → cache trung chuyển + SQLite
  → private storage channel House01 → file_id
  → người được phép yêu cầu bot → phản hồi trong private chat
```

`MEDIA_MODE=remux_copy` là mặc định triển khai mới: thay container thành MP4,
giữ compressed video/audio stream, không AAC conversion, không full decode.
Tùy chọn `raw` (passthrough) giữ nguyên byte và gửi document. House01 là tenant duy
nhất của MVP hiện tại; dashboard quản trị nhiều nhà/control database tập trung
là giai đoạn tiếp theo, không được coi là đã triển khai chỉ vì có `TENANT_ID`.

SDK Linux ARM64 đã khởi tạo và tìm/tải C6N trên VPS qua Tailscale; H6c vẫn cần
kiểm thử riêng. Cổng mở/RTSP live không chứng minh lịch sử SD hoạt động. SDK
chính hãng không bundle trong image, phải mount đúng CPU máy chạy Docker.
File/manifest Studio vẫn là nguồn nhập tùy chọn.

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

Mặc định cloud, `ENABLE_UPLOAD=false`, `KEEP_CACHE=false`, retention 1 giờ;
thiếu bot/channel ID/quyền post vẫn mở dashboard nhưng chưa upload recording. Image dùng
UID/GID `10001:10001`; `input` chỉ đọc. SQLite/cache/tài khoản dashboard nằm trong
named volumes. Giữ project **`ezviz-telegram-archive`** và volume keys
`archive-state`, `archive-cache`, `bot-api-state` khi cập nhật để dùng lại dữ liệu.

### House01 mới và dữ liệu VPS hiện có

Base `compose.yaml` giữ tên project **`ezviz-telegram-archive`** và volume keys
cũ. Với VPS hiện có tại `/home/ubuntu/mycameratele`, giữ nguyên project/volume và
cấu hình House01 trong `.env`; **không thêm `compose.house01.yaml`** khi di trú.
Đổi tenant hoặc destination không tạo cớ reset SQLite/tài khoản/file_id cũ.

Chỉ **cài mới, thư mục/volume mới** mới dùng overlay cô lập House01:

```bash
# New installation only; verify the rendered project name before starting.
docker compose --env-file .env -f compose.yaml -f compose.house01.yaml config --quiet
docker compose --env-file .env -f compose.yaml -f compose.house01.yaml pull archive dashboard
docker compose --env-file .env -f compose.yaml -f compose.house01.yaml up -d archive dashboard
```

Overlay dùng project `mycameratele-house01`, tạo volume có prefix project riêng.
Cài thứ hai trên cùng VPS phải chọn `DASHBOARD_PORT` khác và bot token khác;
không chạy hai consumer polling cùng token. Nếu thêm SDK hoặc Local API, giữ
cùng toàn bộ tập file Compose trong mọi lệnh update. Không dùng `down -v`.

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
  vẫn tải nguyên bản video về cache. Bật lại dùng Start sync để đẩy phần còn chờ.
- **Start all** tạo job cho camera đang bật. **Start sync** ở mỗi card chỉ chạy
  camera đó. Thêm camera đang bật tự xếp job lần đầu. Job lưu trong SQLite,
  chống trùng, tiếp tục kiểm tra sau restart; không tự gửi lại upload mơ hồ.
- Worker tự tạo job mỗi `SD_SYNC_INTERVAL_SECONDS=900` giây. Nút Start bỏ qua
  thời gian chờ này; dashboard/bot chỉ xếp job, worker xử lý tải và upload.
  Mặc định 900 giây tương đương **15 phút**, không quét SD liên tục.
- Telegram có `/sync`, nút **Start sync** ở menu chính và từng mục camera,
  công tắc upload và nút cập nhật trạng thái. Allowlist được kiểm tra trước
  thao tác. Upload channel cần `ENABLE_UPLOAD=true` cùng bot/channel/quyền post
  đã kiểm chứng; không phụ thuộc owner `/start` và không fallback private chat.

Trạng thái hiển thị đang xếp hàng / kiểm tra / tải SD / upload / hoàn tất hoặc
lý do dừng. Không có worker heartbeat thì job vẫn queued; xem `docker compose
ps` và logs worker. Thiếu credential, SDK, tuyến mạng, giao thức không hỗ trợ,
file chưa đóng, cache đầy, Telegram chưa cấu hình đều phải hiện rõ, không giả
báo thành công. Nếu search SD thực sự trả 0 clip, job ghi nhận kết quả rỗng.

### MP4 remux-copy và cache trung chuyển

Mặc định **`MEDIA_MODE=remux_copy`**: FFmpeg chỉ remux container thành MP4 với
`-c copy`. Compressed video/audio streams được giữ nguyên; không encode video,
không đổi audio sang AAC, không resample, không chạy decode toàn bộ để "kiểm tra"
file. Không gọi FFprobe hoặc decode-validation trong remux-copy; header nhỏ và
thời gian recording SDK/manifest đủ cho tên file/mục lục. Nếu stream gốc không mux được vào MP4, lỗi
`failed_remux`/cần xem lại giữ nguồn và không gửi file giả MP4. Không bỏ audio,
không âm thầm transcode hoặc chuyển sang raw để lách lỗi.

MP4 remux-copy có thể đổi byte/container/SHA-256 dù codec không đổi; vì vậy
không gọi nó là byte-identical. Upload dùng file MP4 đã remux; caption/mục lục
lấy thời gian recording SDK/manifest, không suy giờ ghi hình từ PTS hoặc tên file.

Tùy chọn **`MEDIA_MODE=raw`** (passthrough) chép nguyên byte, SHA-256 source/cache/upload
trùng nhau, không gọi FFmpeg hoặc FFprobe mặc định. Header nhỏ chỉ gợi ý đuôi
file; format chưa rõ dùng `.bin`. `PASSTHROUGH_PROBE_METADATA=false` là mặc định;
chỉ bật tùy chọn đọc metadata giới hạn nếu thật sự cần. Raw file gửi document,
không được đổi container để ép Telegram preview.

Worker chỉ dùng cache làm trung chuyển. `KEEP_CACHE=false` và
`CACHE_RETENTION_HOURS=1` xóa cache **sau upload đã xác nhận và metadata đã
commit** được 1 giờ. Đặt retention `0` để xóa ngay sau bước xác nhận/commit.
Recording chưa gửi, lỗi, `upload_unknown` hoặc chưa commit không bị timer này
xóa; database/file_id vẫn giữ để tra cứu/khôi phục. File nguồn Studio không xóa.

Các bản đã lưu trước nâng cấp giữ nguyên file_id/loại media hiện có và lịch sử.
Không re-upload toàn bộ bản cũ. Khi cần lấy lại file chưa gửi để đổi media mode,
phải xử lý riêng trạng thái rõ ràng; không retry một lượt gửi mơ hồ.

### Theo dõi tiến trình thay vì suy từ dung lượng

HCNetSDK có độ trễ mở phiên/tìm recording/khởi động từng file, ngoài thời gian
truyền byte qua VPS → Tailscale → Armbian. Theo dõi `phase`, `sd_searched`,
`sd_downloaded`, `sd_imported`, `uploaded` và **Cập nhật ... trước** trên dashboard.
Số file nhẹ không tự chứng minh job đang treo; bộ đếm và log của đúng job mới
phân biệt tải SD, remux-copy, upload và lỗi. Start chỉ xếp job, không tạo thêm
worker tải chồng. Tắt upload vẫn tải cache; tắt camera dừng nguồn ở điểm kiểm tra.

### Native HCNetSDK trong Docker

Lấy **Device Network SDK for Linux** từ
[Hikvision SDK](https://www.hikvision.com/en/support/download/sdk/), đúng CPU
**máy chạy container** (VPS x86_64 cần Linux64 x86_64; Armbian aarch64 cần
Linux ARM64). Trang catalogue Linux64 không tự chứng minh hỗ trợ ARM64;
[portal SDK của hãng](https://open.hikvision.com/download/5cda567cf47ae80dd41a54b3?type=20)
có thể cần đăng nhập để lấy gói đúng kiến trúc. Gói ARM64 chính hãng đã được
kiểm tra ZIP/ELF/header và khởi tạo trên VPS ARM64; binary vẫn không bundle
trong image. Không dùng DLL Windows từ EZVIZ Studio, không dùng SDK ARM64
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

SDK native chạy trong **child process cô lập**. `LD_LIBRARY_PATH` và các đường
dẫn CA/library riêng của SDK chỉ đặt cho child, không đặt global trên worker:
Telegram HTTPS của parent dùng OpenSSL/CA hệ thống. Giữ overlay SDK mới khi
update để tránh library SDK ghi đè TLS của Python/Telegram. Không thêm thư viện
SDK vào `LD_LIBRARY_PATH` của toàn container.

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

## 3. Private channel House01, owner và allowlist

Dùng **bot hiện có**, không tạo lại bot hoặc đổi token trong migration. Tạo/chọn
một **private channel House01** trong Telegram, thêm bot làm administrator và
cho quyền **Post messages**. Lấy numeric channel ID dạng `-100...`; đó là
`TELEGRAM_STORAGE_CHANNEL_ID`, không phải USER ID của owner.

```dotenv
TENANT_ID=house01
TELEGRAM_DESTINATION=channel
TELEGRAM_STORAGE_CHANNEL_ID=YOUR_NEGATIVE_PRIVATE_CHANNEL_ID
TELEGRAM_BOT_TOKEN=YOUR_EXISTING_BOT_TOKEN
TELEGRAM_OWNER_USER_ID=YOUR_POSITIVE_USER_ID
TELEGRAM_BOT_USERNAME=YOUR_BOT_USERNAME_WITHOUT_AT
TELEGRAM_ALLOWED_USER_IDS=OWNER_ID,VIEWER_ID_2,VIEWER_ID_3
TELEGRAM_CHAT_ID=
MEDIA_MODE=remux_copy
KEEP_CACHE=false
CACHE_RETENTION_HOURS=1
SD_SYNC_INTERVAL_SECONDS=900
ENABLE_UPLOAD=false
```

Thay slot bằng số thật. Bot/channel identity và quyền post được kiểm chứng trước
upload. Channel ID trống/sai, bot thiếu quyền hoặc channel không truy cập được
**chặn upload**, không tự post recording vào private chat owner. Sau khi cấu hình
và quyền post đạt, đặt `ENABLE_UPLOAD=true` rồi recreate cùng project/volumes.
Channel automatic upload không cần owner `/start`; đó là gate khác với private
reply. Một token chỉ có một polling worker.

Owner/viewer dùng USER ID dương trong allowlist, owner được tự thêm. Mỗi người
mở bot `/start` để nhận phản hồi riêng; bot chỉ gửi media khi người đó yêu cầu
Xem/Tải qua menu. Viewer không nhận toàn bộ recording mới. Private-channel
membership riêng không thay thế allowlist của bot và ngược lại.

MVP giữ quyền chung hiện có: mọi ID được phép có thể xem/tải/xóa logic/khôi phục
recording trong kho House01. Dashboard bootstrap admin/admin vẫn yêu cầu đổi
mật khẩu và giữ tài khoản trong state volume. Cơ chế tài khoản/roles quản trị
nhiều nhà là công việc tiếp theo, không được suy ra là hoàn tất từ House01 MVP.

Chế độ cũ `TELEGRAM_DESTINATION=owner_private` vẫn là lựa chọn rõ ràng để dùng private
chat owner; USER ID phải dương, owner `/start` mở gate của chế độ này.
`TELEGRAM_CHAT_ID` chỉ là alias owner cũ, không dùng thay storage channel ID.

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

- **Xem:** bot gửi lại media qua file_id vào private chat người được phép
  khi có yêu cầu. MP4 phù hợp có thể preview; định dạng/codec khác có thể cần
  tải về phần mềm tương thích, không encode lại chỉ để ép preview.
- **Tải:** bot gửi lại media gốc và hướng dẫn dùng nút tải / menu Telegram
  `Save to Downloads`. Loại lưu (video/document) theo media phù hợp và giữ khi
  replay: video dùng sendVideo, document tiếp tục
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
rồi upload binary lần nữa. Recording tự động ở channel, không ở owner chat.
Theo [Telegram Sending files](https://core.telegram.org/bots/api#sending-files),
file_id có thể tái sử dụng bởi **cùng bot**, không chuyển sang bot khác.
Dashboard mở link bot dạng `https://t.me/BOT_USERNAME?start=play_RECORD_KEY`;
người mở vẫn phải được phép. Khi username chưa được cấu hình/lấy từ getMe,
dashboard không đoán link channel. Cây thư mục là virtual filesystem trong
SQLite, không phải thư mục vật lý trên Telegram.

Cloud mode chỉ dành cho kiểm tra clip nhỏ, giới hạn binary upload 50 MB;
file vượt ngưỡng được giữ để xử lý, không tự chia/transcode. MP4 remux-copy và
raw passthrough tuân thủ MEDIA_MODE; không đổi codec để vượt giới hạn API.
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
`2000000000` bytes, `KEEP_CACHE=false`, retention mặc định 1 giờ cho worker/dashboard.
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
tự bật upload hoặc đổi destination. Nếu bot đang dùng cloud, gọi `logOut` trên cloud trước chuyển
endpoint theo [hướng dẫn migration chính thức](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server).

```bash
docker compose -f compose.yaml -f compose.local.yaml --profile local-api config --quiet
docker compose -f compose.yaml -f compose.local.yaml --profile local-api pull
docker compose -f compose.yaml -f compose.local.yaml --profile local-api up -d
docker compose -f compose.yaml -f compose.local.yaml --profile local-api ps
```

Xác nhận bot/channel/quyền post với endpoint mới, rồi đặt `ENABLE_UPLOAD=true`
và chạy lại lệnh `up -d --force-recreate archive dashboard` với **cùng hai file
Compose và profile**. Khi đã chọn local, luôn giữ các cờ này trong lệnh cập nhật;
lệnh chỉ dùng `compose.yaml` sẽ chọn lại cấu hình cloud mặc định.

Local API chỉ trong mạng Compose, **không publish 8081**. Worker stream multipart
bytes qua HTTP; không dùng `file://`, không mount cache worker vào API. Volume
`bot-api-state` giữ working-state của API qua restart, không đổi tên/xóa khi update. Theo
[Telegram Local Bot API](https://core.telegram.org/bots/api#using-a-local-bot-api-server),
local mode hỗ trợ upload tới 2000 MB. Không cần VPS hoặc middleware.

RAM runtime tùy workload/TDLib; không có một mức cố định suy ra từ dung lượng
file. Theo dõi **min/max/peak** trước và trong upload thực bằng `docker stats`
và log/OOM của host. Multipart stream tránh nạp cả file vào RAM của worker;
Bot API/TDLib vẫn có working-state, buffer và cache riêng của server.
Build TDLib là bài toán khác: compiler/job/architecture thay đổi bộ nhớ, có
[hướng dẫn chính thức cho low-memory build](https://github.com/tdlib/td#building).
Pull image có sẵn tránh compile trên board nhỏ. Test 100 MB, 500 MB, 1 GB là các
mốc nghiệm thu bằng **clip thật**, không phải kết quả đã đạt chỉ vì sparse-file,
mock API, kiểm tra config hoặc unit test thành công.

## 5. Retention và daily SQLite backup

House01/cloud/local mặc định `KEEP_CACHE=false`, `CACHE_RETENTION_HOURS=1`;
đặt retention `0` để cleanup ngay. Chỉ xóa cache đã upload xác nhận,
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

## 6. Migration VPS hiện có sang House01

1. Giữ bot/token, thư mục `/home/ubuntu/mycameratele`, project
   `ezviz-telegram-archive` và toàn bộ volumes hiện có; backup `/data` trước.
2. Chọn private channel và thêm bot admin/quyền post. Nhập `TENANT_ID=house01`,
   `TELEGRAM_DESTINATION=channel`, channel ID thật; giữ owner/allowlist dương.
3. Giữ API mode hiện tại (cloud/local), không đồng thời đổi endpoint khi đổi
   storage destination. Cấu hình Local API là bước riêng, có cloud logOut.
4. Chọn `MEDIA_MODE=remux_copy`, retention 1 giờ, SD 900; giữ `ENABLE_UPLOAD=false`
   trong bước kiểm tra channel/media. Chỉ bật sau khi gate được xác nhận.
5. Dùng **base + SDK overlay hiện có**, không thêm overlay project House01 mới:

```bash
cd /home/ubuntu/mycameratele
git pull --ff-only
docker compose -f compose.yaml -f compose.sdk.yaml config --quiet
docker compose -f compose.yaml -f compose.sdk.yaml pull archive dashboard
docker compose -f compose.yaml -f compose.sdk.yaml up -d archive dashboard
```

Nếu VPS đang Local API, giữ thêm `-f compose.local.yaml --profile local-api`
trong mọi lệnh; không thêm nếu VPS đang cloud. Database tự migration tại chỗ,
không reset cameras/dashboard login/catalog/file_id/message metadata cũ. Bản
upload cũ ở owner chat vẫn replay bằng cùng bot; không chuyển/re-upload tất cả
vào channel mới. Recording mới dùng destination mới; `upload_unknown` cần đối
soát trước bất kỳ retry nào. Không dùng `down -v` hoặc đổi project để "update".

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
