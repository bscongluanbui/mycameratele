# MyCameraTele — Telegram Private Archive

Một bot lưu recording vào **private chat của owner**; owner và các viewer được
cho phép duyệt lại lịch sử ngay trong private chat riêng của mỗi người.
Không cần channel/group. SQLite là mục lục, Telegram chứa media, bot là browser.

```text
MP4 xuất từ EZVIZ Studio + thời gian ghi hình
  → input / manifest → kiểm tra media, remux không transcode
  → cache + SQLite → upload một lần vào private chat owner
  → Camera → Năm → Tháng → Ngày → Video
  → phát lại bằng file_id trong private chat của người được cho phép
```

**Downloader lịch sử SD trực tiếp từ camera vẫn chưa được triển khai.** Luồng
hiện có nhận file xuất bằng Studio; thêm camera/probe TCP/RTSP live không chứng
minh đã tải được recording trên SD. Tài liệu này mô tả phiên bản có private-chat
flow; sửa source tại máy local không tự cập nhật image `latest` trên GHCR.

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
UID/GID `10001:10001`; `input` chỉ đọc. SQLite/cache/token dashboard nằm trong
named volumes. Giữ project **`ezviz-telegram-archive`** và volume keys
`archive-state`, `archive-cache`, `bot-api-state` khi cập nhật để dùng lại dữ liệu.

### Dashboard

```bash
docker compose exec dashboard cat /data/dashboard_token
```

Mở `http://127.0.0.1:8080` và đăng nhập bằng token. Token dashboard khác token bot,
tự tạo và lưu `/data`; `DASHBOARD_TOKEN` có thể override với chuỗi ngẫu nhiên ít
nhất 24 ký tự. Để mở dashboard từ PC qua SSH, thay hai slot bằng máy thật:

```bash
ssh -L 8080:127.0.0.1:8080 USER@DOCKER_HOST
```

Rồi mở `http://127.0.0.1:8080` trên PC. Truy cập LAN trực tiếp chỉ khi bạn đặt
`DASHBOARD_BIND_IP` bằng địa chỉ LAN của máy Docker và recreate dashboard.
Mặc định bind loopback. Cookie đăng nhập có HttpOnly/SameSite/CSRF; Đăng xuất hủy
phiên. Không đưa token/mật khẩu/Wi-Fi vào source, manifest, image hay build args.

## 2. Thêm camera và nhập recording

Trên dashboard, tạo mã camera ổn định, tên hiển thị, model, địa chỉ LAN và các
cổng. Đổi tên không đổi mã/lịch sử; manifest dùng mã, không dùng tên hiển thị.
Camera từ manifest cũ có thể tự đăng ký. Kiểm tra LAN chỉ kiểm tra TCP.

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
`/recent`, `/status` là các shortcut.

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
