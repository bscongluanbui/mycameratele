# Tải SD công bằng và song song

## Cấu hình `.env`

```dotenv
SD_SYNC_INTERVAL_SECONDS=900
SD_MAX_FILES_PER_JOB=10
SD_DOWNLOAD_WORKERS=5
SD_DOWNLOAD_SPEED_KBPS=0
```

- `SD_DOWNLOAD_WORKERS`: 1–5 camera đồng thời; mặc định 1 để giữ hành vi cũ.
- `SD_MAX_FILES_PER_JOB`: 1–100 video/camera/lượt, mặc định 10. Camera còn backlog được đưa về cuối FIFO ngay nếu lượt vừa rồi có tiến triển và không có lỗi/đang chờ xác nhận.
- `SD_DOWNLOAD_SPEED_KBPS`: 0 giữ tốc độ mặc định; hoặc 256–32768 **kilobit/giây**. 32768 là giới hạn ứng dụng, không phải cam kết thiết bị hỗ trợ. Thử 8192 trên cùng một recording nhỏ và so sánh byte/hash, thời gian tải trước khi bật rộng.
- Giữ 900 giây để kiểm tra video mới sau khi bắt kịp lịch sử. Không chờ 15 phút giữa các batch backlog thành công.

Áp dụng sau khi cập nhật:

```sh
git pull --ff-only
docker compose pull archive dashboard route-discovery
docker compose up -d --no-deps archive dashboard route-discovery
```

## Luồng xử lý

Mỗi thread sở hữu SQLite connection, Telegram client và SDK child riêng; mỗi camera tối đa một job active. Các camera khác nhau tải song song. **Media upload được tuần tự hóa** qua claim → phản hồi Telegram → catalog commit → cleanup. Menu bot, album file-ID và channel index vẫn hoạt động độc lập.

Cache admission có reservation nguyên tử chung trong tiến trình trước khi tải. Reservation bao gồm raw staging và MP4 làm việc, tồn tại qua ingest; nested ingest tái sử dụng reservation, không tính hai lần cùng file. Khi hết cache hoặc đĩa chạm mức free tối thiểu, job dừng có backpressure thay vì tải vượt ngân sách. Chỉ một runner được phép dùng state/cache nhờ worker lock; đừng scale nhiều replica archive trên cùng volume.

Remux luôn stream-copy (`-c copy`), không transcode. Local Bot API đọc file trực tiếp; cleanup chỉ diễn ra sau xác nhận Telegram và commit. Recording mơ hồ `upload_unknown` không tự gửi lại. Các clip lỗi tiếp tục áp dụng retention 72 giờ.

## Tiến trình dashboard

Dashboard poll khoảng 2,5 giây khi có job active. Dòng tiến trình ưu tiên `TB VPS→Telegram … MB/s`: tổng byte của các file đã được Telegram xác nhận chia cho thời gian yêu cầu gửi thành công, không gồm chờ khóa uploader, kiểm tra channel hay remux. Đây là tốc độ hiệu dụng trung bình, không phải tốc độ mạng tức thời. Khi chuyển sang batch tiếp theo, `Upload gần nhất … MB/s` giữ số đo của file đã xác nhận gần nhất; Detail có thời điểm xác nhận.

Local Bot API đọc MP4 trực tiếp không cung cấp tiến trình byte lên Telegram. Trong lúc gửi, dashboard hiển thị dung lượng file, số giây chờ và `chờ xác nhận`, không dựng phần trăm hoặc tốc độ 0 giả. `TB Camera→VPS` là tổng byte SD đã tải xong chia cho tổng thời gian download; với collector, thời gian này gồm cả Camera→Armbian và Armbian→VPS, chưa tách riêng từng chặng. Với hai download song song, đó là trung bình trên thời gian các lượt tải, không phải tổng băng thông theo thời gian thực. `upload_seconds` vẫn là thời gian xử lý gồm chờ lượt và không dùng tính tốc độ Telegram. Detail giữ số liệu từng khâu và trạng thái SDK (`off`, `applied`, `unsupported`, `rejected`).

File SD có lỗi định dạng/thông số media xác định được được cách ly chỉ sau khi đã lưu bản nguồn nguyên vẹn và xác minh chưa có lượt gửi Telegram. Các file tốt phía sau tiếp tục chạy; `Cách ly` và mã lỗi nằm trong Detail. Bản lỗi giữ theo `ERROR_RETENTION_HOURS` (mặc định 72 giờ), không tự gửi lại, không transcode hoặc bỏ audio. Lỗi công cụ, timeout, cache, database và lượt gửi mơ hồ vẫn dừng để kiểm tra.

## Tăng tốc SDK

Chỉ gọi download control `NET_DVR_SETSPEED=24`, input DWORD 4 byte, sau PLAYSTART thành công. Không gọi chuyển mã 33 hay chuyển container phía SDK. Setter bị từ chối không khởi động lại download; giữ tốc độ mặc định và ghi mã lỗi. Một session đã bị từ chối không gửi lại setter trên từng file. `applied` chỉ nghĩa là SDK nhận lệnh, chưa chứng minh tốc độ thực tế tăng.

Nguồn: [Hikvision download control](https://open.hikvision.com/hardware/definitions/NET_DVR_PlayBackControl_V40_Download.html).

## Đo và rollback

Đo riêng SD seconds/bytes, ingest seconds, upload seconds; so sánh 1, 2 rồi 5 camera và quan sát tổng throughput, không chỉ tốc độ một camera. Mạng uplink/Wi-Fi/SD dùng chung có thể giới hạn tổng tốc độ; 5 thread không đồng nghĩa nhanh gấp 5.

Rollback cấu hình: `SD_DOWNLOAD_WORKERS=1`, `SD_DOWNLOAD_SPEED_KBPS=0`, recreate archive. Muốn trở lại batch cũ đặt `SD_MAX_FILES_PER_JOB=100`. Không đổi recording ID, channel, allowlist hay dữ liệu Telegram. SIGTERM dừng nhận job mới và kết thúc file hiện tại trước khi thoát. Upload đang chạy được hoàn tất; các file đang đợi khóa uploader vẫn nằm trong cache, không bắt đầu lượt gửi mới sau khi worker nhận tín hiệu dừng. Compose cấp grace 65 phút cho download/upload có timeout dài. Không ép kill nếu còn upload chưa có xác nhận.
