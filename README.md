# MyCameraTele

Dashboard Docker quản lý camera EZVIZ và lưu video đã xuất lên Telegram.

```text
Video xuất từ EZVIZ Studio + thời gian ghi hình
  → thư mục input / manifest
  → chuẩn hóa MP4, kiểm tra media và SHA-256
  → SQLite + hàng đợi upload
  → Telegram + dashboard: Camera → Năm → Tháng → Ngày
```

**Tải lịch sử SD trực tiếp từ camera chưa được triển khai.** Thêm camera hoặc
thấy cổng TCP mở không đồng nghĩa đã tải được video SD. RTSP live không thay
thế lịch sử recording trên thẻ nhớ. Luồng hiện tại nhận video xuất bằng Studio.

## 1. Cài lần đầu trên Ubuntu / Armbian

Chuẩn bị Docker Engine, Compose plugin và Git. Tham khảo tài liệu Docker cho
[Ubuntu](https://docs.docker.com/engine/install/ubuntu/) hoặc
[Debian](https://docs.docker.com/engine/install/debian/).
Image hướng tới Linux `amd64`, `arm64`, `arm/v7`; Docker tự chọn biến thể theo
kiến trúc host. `aarch64` thường tương ứng userland `arm64`; đối chiếu bằng
`uname -m` và `dpkg --print-architecture`. Không ép platform x86 trên board ARM.

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

Compose mặc định **chỉ tải image GHCR, không build trên board**:

- `ghcr.io/bscongluanbui/mycameratele:latest` cho worker và dashboard.
- `ghcr.io/bscongluanbui/mycameratele-bot-api:latest` cho profile local API tùy chọn.
- `ARCHIVE_IMAGE` / `BOT_API_IMAGE` trong `.env` có thể chọn tag hoặc digest khác.

`ENABLE_UPLOAD=false` mặc định. Worker lập chỉ mục nhưng chưa gửi video.
SQLite, token dashboard và cache nằm trong named volumes; thư mục `input` được
mount chỉ đọc. Image dùng UID/GID `10001:10001`; file nguồn cần đọc được bởi UID
này. Không đổi tên project Compose hoặc volume khi cập nhật một cài đặt đang có.

### Mở dashboard

```bash
docker compose exec dashboard cat /data/dashboard_token
```

Mở `http://127.0.0.1:8080` trên máy Docker và nhập token. Token tự tạo, lưu trong
volume `/data`; token này khác token bot Telegram. Nếu đặt `DASHBOARD_TOKEN` trong
`.env`, dùng chuỗi ngẫu nhiên ít nhất 24 ký tự và đăng nhập bằng giá trị đó.
Token override không ghi đè file token cũ.

Từ PC khác, dùng tunnel (thay `USER` và `DOCKER_HOST`):

```bash
ssh -L 8080:127.0.0.1:8080 USER@DOCKER_HOST
```

Rồi mở `http://127.0.0.1:8080` trên PC. Để truy cập trực tiếp trong LAN, đặt
`DASHBOARD_BIND_IP` bằng địa chỉ LAN thực của máy Docker, chọn `DASHBOARD_PORT`
trong `.env`, rồi chạy:

```bash
docker compose up -d --force-recreate dashboard
```

Truy cập `http://DOCKER_HOST:8080` với `DOCKER_HOST` là địa chỉ thực. Mặc định
dashboard bind loopback, không mở trên mọi interface. Phiên đăng nhập có cookie
HttpOnly, SameSite và CSRF; nút Đăng xuất hủy phiên trên server.

## 2. Thêm camera và nhập video

Trên dashboard, thêm camera với mã ổn định, tên hiển thị, model, địa chỉ LAN và
cổng device/HTTP/RTSP. Có thể đổi tên hoặc tạm dừng camera. **Mã camera không
đổi** để lịch sử không bị tách; trường `camera` trong manifest dùng mã, không
dùng tên hiển thị. Camera từ manifest cũ tự đăng ký với tên ban đầu bằng mã.
Nút Kiểm tra LAN chỉ kiểm tra TCP; khả năng tải SD cần kiểm chứng từng model.

Xuất một recording bằng EZVIZ Studio, lấy đúng Start Time / End Time của dòng
recording đó. Copy file vào `input`, rồi tạo `input/manifest.json`:

```json
{
  "recordings": [
    {
      "record_id": "studio-recording-001",
      "camera": "living_room",
      "path": "/input/recording-001.mp4",
      "start_time": "2026-01-01T08:00:00+07:00",
      "end_time": "2026-01-01T08:01:00+07:00"
    }
  ]
}
```

Thay mã, file và **hai thời điểm ví dụ** bằng dữ liệu recording thực. Thời gian
phải có UTC offset; tên file và PTS không phải bằng chứng giờ ghi hình.
`DISPLAY_TIMEZONE` quyết định ngày/tháng khi tra cứu. Copy xong video trước,
đưa manifest hoàn chỉnh vào sau. Worker quét các manifest `*.json` mỗi
`SCAN_INTERVAL_SECONDS`; không cần dừng dịch vụ để thêm file.

Kiểm tra manifest mà chưa nhập dữ liệu:

```bash
docker compose stop archive
docker compose run --rm archive ingest --manifest /input/manifest.json --dry-run
docker compose up -d archive
docker compose logs --tail=100 archive
```

Nếu muốn gọi CLI ingest thật thay cho worker, dừng worker trước để dùng chung
khóa ghi:

```bash
docker compose stop archive
docker compose run --rm archive ingest --manifest /input/manifest.json
docker compose up -d archive
```

File nguồn không bị xóa. SQLite chống nhập trùng theo camera/source/record ID;
`upload_unknown` giữ lại trường hợp kết quả upload chưa rõ để đối chiếu, không
tự gửi lại mù. `KEEP_CACHE=true` giữ bản cache sau upload; điều chỉnh
`CACHE_MAX_GB` và `CACHE_MIN_FREE_GB` theo dung lượng ổ đĩa.

## 3. Telegram và cây tra cứu

Tạo bot qua [BotFather](https://core.telegram.org/bots/features#botfather), thêm
bot vào channel lưu trữ với quyền đăng bài. Sửa `.env` trên máy triển khai:

```dotenv
TELEGRAM_BOT_TOKEN=YOUR_BOT_TOKEN
TELEGRAM_CHAT_ID=YOUR_CHANNEL_CHAT_ID
TELEGRAM_ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
ENABLE_UPLOAD=true
```

Allowlist nhận nhiều ID ngăn bởi dấu phẩy. Worker là consumer polling duy nhất
của bot; không chạy thêm một cài đặt khác với cùng token. Áp dụng cấu hình:

```bash
docker compose up -d --force-recreate archive dashboard
docker compose logs --tail=100 archive
```

Nhắn `/archive` cho bot bằng tài khoản trong allowlist. Dashboard và bot duyệt
**Tên camera → Năm → Tháng → Ngày**, có phân trang và **Cũ → Mới / Mới → Cũ**
theo giờ ghi hình, không theo ngày upload hay tên file. Đổi tên camera cập nhật
cây tra cứu và caption upload mới; không sửa message Telegram đã gửi.
Video qua nửa đêm có thể hiện ở cả hai ngày; end đúng 00:00 không tính sang
ngày mới.

Telegram lưu các message, không tạo thư mục vật lý hoặc sắp xếp lại lịch sử chat.
SQLite và bot quản lý cây tra cứu, liên kết tới message gốc. Tài khoản mở link
channel riêng cần là thành viên channel. Dashboard mặc định hiện video đã
upload; chọn Tất cả trạng thái để xem hàng đợi.

Cloud Bot API dùng ngưỡng upload mặc định 50 MB. File vượt giới hạn được giữ
để xử lý; bộ chia clip lớn tự động chưa được triển khai. Xem
[Telegram Bot API](https://core.telegram.org/bots/api#sendvideo).

### Local Bot API tùy chọn

Image local Bot API được build riêng cho `amd64` và `arm64`; image ứng dụng
chính có thêm `arm/v7`. Profile này không cần bật khi dùng cloud Bot API.

Lấy application credentials tại [my.telegram.org](https://core.telegram.org/api/obtaining_api_id)
và cấu hình `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` trong `.env`:

```bash
docker compose --profile local-api pull telegram-bot-api
docker compose --profile local-api up -d telegram-bot-api
```

Khi chuyển bot đang dùng cloud sang local, thực hiện `logOut` trên cloud theo
[hướng dẫn chính thức](https://github.com/tdlib/telegram-bot-api#moving-a-bot-to-a-local-server),
rồi đổi:

```dotenv
TELEGRAM_API_BASE=http://telegram-bot-api:8081
TELEGRAM_API_MODE=local
TELEGRAM_MAX_BYTES=2000000000
```

```bash
docker compose --profile local-api up -d --force-recreate archive dashboard
```

Local API ở mạng Compose, không publish cổng 8081 ra host; đọc cùng `/cache`
để gửi file URI. Telegram công bố giới hạn local mode tới 2000 MB tại
[Using a local Bot API server](https://core.telegram.org/bots/api#using-a-local-bot-api-server).
Đổi API không bổ sung chức năng tải SD từ camera.

## 4. Cập nhật: pull image, giữ nguyên dữ liệu

Không cần build lại. Ghi lại digest và backup trước khi nâng phiên bản; `git
pull` cập nhật Compose, tài liệu và file mẫu, không thay thế `.env` đang dùng.

Nếu Compose đã cập nhật và `ARCHIVE_IMAGE` vẫn dùng tag mặc định `latest`, hai
lệnh ngắn gọn là:

```bash
docker pull ghcr.io/bscongluanbui/mycameratele:latest
docker compose up -d
```

```bash
docker image inspect "$(docker compose images -q archive | head -n 1)" \
  --format '{{join .RepoDigests "\n"}}'
git pull --ff-only
docker compose config --quiet
docker compose pull archive dashboard
docker compose up -d archive dashboard
docker compose ps
docker compose logs --tail=100 archive dashboard
```

Nếu dùng local API, thêm `--profile local-api` và pull cả `telegram-bot-api`.
Các volume vẫn dùng project `ezviz-telegram-archive` và khóa
`archive-state`, `archive-cache`, `bot-api-state` như trước.

### Backup và rollback bằng digest

Dừng cả worker và dashboard để backup SQLite nhất quán. Lệnh sau lưu `/data`,
bao gồm database và token, vào thư mục backup trên host; thư mục backup này cần
được bảo quản cùng cấu hình `.env`:

```bash
mkdir -p backups
# Chỉ đổi quyền thư mục backup riêng để UID ứng dụng ghi được:
sudo chown 10001:10001 backups
sudo chmod 700 backups
docker compose stop archive dashboard
docker compose run --rm --no-deps --entrypoint python \
  -v "$PWD/backups:/backup" archive \
  -c 'import tarfile,time; p="/backup/state-"+time.strftime("%Y%m%d-%H%M%S")+".tar.gz"; t=tarfile.open(p,"w:gz"); t.add("/data",arcname="data"); t.close(); print(p)'
docker compose up -d archive dashboard
```

Backup cache `/cache` và video nguồn `input` riêng nếu cần bản sao media độc lập.
Để quay lại image đã ghi nhận, đặt trong `.env`:

```dotenv
ARCHIVE_IMAGE=ghcr.io/bscongluanbui/mycameratele@sha256:PREVIOUS_DIGEST
```

Sau đó `docker compose pull archive dashboard` và `docker compose up -d archive
dashboard`. Cách này đổi mã chạy nhưng giữ nguyên volumes. Một phiên bản cũ
phải tương thích schema database hiện tại; nếu cần phục hồi database, dừng
cả hai dịch vụ và dùng backup nhất quán tương ứng. Không xóa volume khi rollback.

```bash
# Dừng toàn bộ dịch vụ, giữ nguyên dữ liệu:
docker compose --profile local-api down
```

`down -v` xóa volume; không dùng cho cập nhật/rollback giữ dữ liệu. Đổi image hay
phục hồi SQLite không xóa/rollback message đã gửi lên Telegram. Với
`upload_unknown`, đối chiếu message thật rồi dùng `archive reconcile --help`.

## 5. Phát triển tại máy local

Build chỉ được bật khi thêm override, không ảnh hưởng lệnh cài đặt pull-only:

```bash
docker compose -f compose.yaml -f compose.build.yaml build archive dashboard
docker compose -f compose.yaml -f compose.build.yaml up -d archive dashboard
docker compose -f compose.yaml -f compose.build.yaml run --rm --entrypoint python \
  archive -m unittest discover -s tests -v
```

Override dùng `mycameratele:dev` và `mycameratele-bot-api:dev`; local Bot API
biên dịch từ nguồn chính thức cần RAM/thời gian, `BOT_API_BUILD_JOBS` điều chỉnh
số job. `docker-bake.hcl` phục vụ kiểm tra/build đa kiến trúc. Kết quả build,
unit test và probe TCP không thay thế kiểm chứng tải một recording SD thực trên
từng model/firmware/kiến trúc.

## Tài liệu liên quan

- [EZVIZ: tải recording bằng Studio](https://support.ezviz.com/faq/article/How-to-download-the-recorded-video-clips)
- [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/)
- [Telegram Bot API](https://core.telegram.org/bots/api)
