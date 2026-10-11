# LAN collector cho Bama

## Luồng dữ liệu

```text
Camera 192.168.2.x → Armbian trong LAN → Tailscale → cache VPS
                                            ↓
                         remux MP4 bằng stream copy → Telegram
```

- Armbian chỉ tìm và tải bản ghi gốc qua SDK chính hãng. Armbian không remux, không transcode và không giữ kho video.
- File PS/TS tạm trên Armbian được xóa ngay khi kết thúc chuyển sang VPS; nhánh ngắt kết nối hoặc lỗi cũng dọn file. Không chờ Telegram upload và không áp dụng hạn giữ file lỗi 3 ngày trên Armbian.
- VPS vẫn quản lý thứ tự, chống trùng, SQLite, bot, channel và MP4. Remux giữ nguyên codec bằng stream copy. File lỗi trên VPS vẫn dùng chính sách hiện có.
- Cache collector có ngân sách 2 GB và chừa ít nhất 2 GB trống. Đây là kiểm soát admission của ứng dụng, không phải quota cứng cho Docker volume.
- Tối đa 10 phiên toàn collector, tối đa 2 phiên/camera; phiên hết hạn sau 900 giây không hoạt động. Giữ cài đặt số luồng hiện tại trên dashboard, không tự tăng trong lúc chuyển đổi.

## Hai compose độc lập

| Máy | File compose | Chức năng |
| --- | --- | --- |
| Armbian `100.92.56.98` | `compose.collector.yaml` riêng | SDK và truyền file gốc |
| VPS Bama | `compose.yaml` + `compose.local.yaml` + `compose.sdk.yaml` + `compose.remote-sd.yaml` | Kho video, remux MP4, Telegram, dashboard |

Collector không dùng `.env` của Bama, không nhận bot token, channel ID hay database của VPS. Chỉ port `100.92.56.98:8787` được publish; không mở `0.0.0.0:8787` trên host. HTTP này đi trong kết nối Tailscale mã hóa. Cấu hình ACL Tailscale chỉ cho VPS truy cập port 8787 của collector.

## Cấu hình Armbian

Đặt file compose riêng tại `/root/mycameratele-collector/compose.collector.yaml`. SDK ARM64 đã kiểm chứng nằm tại `/root/mycameratele-speed-audit-20261011/sdk`; giữ đầy đủ `libhcnetsdk.so`, `HCNetSDKCom` và thư viện đi kèm. Chỉ mount SDK read-only.

Tạo token ngẫu nhiên một lần, không đặt token trong `.env` hoặc dán vào chat:

```bash
sudo install -d -m 700 /root/mycameratele-collector/secrets
sudo python3 - <<'PY'
from pathlib import Path
import os,secrets
path=Path('/root/mycameratele-collector/secrets/sd-collector.token')
descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(descriptor,'w') as stream:
    stream.write(secrets.token_urlsafe(48)+'\n')
os.chown(path,10001,10001)
os.chmod(path,0o600)
PY
```

File token có mode `0600`, owner UID/GID `10001:10001`, nằm trong thư mục host private. Container chạy cùng UID 10001 và mount đúng file read-only. Chuyển **cùng file token** sang VPS qua SSH/SCP, không in nội dung token ra terminal/log.

File `/root/mycameratele-collector/.env` chỉ chứa thông số collector:

```dotenv
COLLECTOR_IMAGE=ghcr.io/bscongluanbui/mycameratele@sha256:IMAGE_DIGEST
COLLECTOR_BIND_IP=100.92.56.98
HCNETSDK_HOST_DIR=/root/mycameratele-speed-audit-20261011/sdk
COLLECTOR_TOKEN_HOST_FILE=/root/mycameratele-collector/secrets/sd-collector.token
COLLECTOR_ALLOWED_CIDRS=192.168.2.0/24
COLLECTOR_MAX_SESSIONS=10
COLLECTOR_PER_CAMERA_SESSIONS=2
COLLECTOR_IDLE_TIMEOUT_SECONDS=900
COLLECTOR_CACHE_MAX_GB=2
COLLECTOR_MIN_FREE_GB=2
```

`IMAGE_DIGEST` là digest bản release đã kiểm thử, không phải giá trị mẫu để chạy nguyên xi.

```bash
cd /root/mycameratele-collector
docker compose --env-file .env -f compose.collector.yaml config --quiet
docker compose --env-file .env -f compose.collector.yaml pull
docker compose --env-file .env -f compose.collector.yaml up -d --wait
curl --fail http://100.92.56.98:8787/health
```

Health chỉ trả `{"ok":true}`; không trả danh sách camera, đường dẫn hay token. Các thao tác tìm/tải bản ghi yêu cầu token riêng và chỉ cho phép IP camera trong `COLLECTOR_ALLOWED_CIDRS`.

## Cấu hình VPS Bama

Lưu bản sao token tại `/home/ubuntu/.mycameratele-bama/sd-collector.token`, mode `0600`, owner `10001:10001`; thư mục chứa mode `0700`. Không thêm file token vào Git. Thêm ba trường sau vào `/home/ubuntu/bama/.env`, giữ nguyên toàn bộ các trường khác:

```dotenv
COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.sdk.yaml:compose.remote-sd.yaml
REMOTE_SD_COLLECTOR_URL=http://100.92.56.98:8787
SD_COLLECTOR_TOKEN_HOST_FILE=/home/ubuntu/.mycameratele-bama/sd-collector.token
```

Nếu `COMPOSE_FILE` đã có file override khác, giữ nguyên danh sách đó và chỉ nối `:compose.remote-sd.yaml`. Override ánh xạ `REMOTE_SD_COLLECTOR_URL` thành `SD_COLLECTOR_URL` **chỉ trong archive**; cách đặt tên này tránh `.env` vô tình bật collector trên dashboard. `SD_COLLECTOR_TOKEN_FILE=/run/secrets/sd-collector-token` cũng được override thiết lập bên trong archive; không cần thêm giá trị token vào `.env`.

Trước khi đổi image/transport, dừng archive có thời gian đủ dài để hoàn tất file hiện tại và lưu xác nhận Telegram. Không kill container đang tải/upload, không reset database, không thay ID camera hoặc channel. Sau khi archive đã thoát sạch, khởi động bản mới:

```bash
cd /home/ubuntu/bama
docker compose config --quiet
docker compose pull archive dashboard route-discovery
docker compose stop --timeout 3900 archive
docker compose up -d --no-deps --wait archive dashboard route-discovery
```

Giữ `compose.sdk.yaml` và SDK trên VPS để rollback chủ động. Khi đã chọn collector, lỗi kết nối phải được hiển thị và thử lại; không tự âm thầm đổi transport giữa chừng.

## Kiểm tra sau chuyển đổi

1. Collector và ba app service Bama healthy; Local Bot API và hai nhà khác không bị restart.
2. Giữ nguyên camera, tên, channel, thông tin đăng nhập, số luồng dashboard, ID tin ghim và mọi receipt đã xác nhận.
3. Tiến trình tải đi qua collector; file gốc tới VPS rồi mới remux/upload theo luồng hiện có.
4. Chờ các phiên truyền kết thúc rồi kiểm tra `/cache` của collector không còn file video. Không kiểm tra cache rỗng khi vẫn có phiên đang tải.
5. Telegram vẫn dùng Local Bot API đọc MP4 trực tiếp, không đổi sang transcode.

## Rollback transport

Dừng archive sạch trước, bỏ `compose.remote-sd.yaml` khỏi danh sách compose và bỏ `SD_COLLECTOR_URL`/`SD_COLLECTOR_TOKEN_FILE` khỏi môi trường archive. Khởi động bằng cấu hình native SDK cũ. Giữ nguyên SQLite và receipts mới; **không chép đè database bằng bản backup trước nâng cấp**. Nếu đổi image, dùng image digest cũ đã lưu và kiểm tra healthy sau khởi động.

Collector có thể dừng riêng sau khi các phiên truyền đã kết thúc. Giữ volume/cache và token cho lần bật lại; chỉ xóa file tạm do collector quản lý theo vòng đời truyền, không xóa thư mục SDK hoặc dữ liệu Bama.
