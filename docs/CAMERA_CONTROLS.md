# Quản lý camera và luồng tải trên dashboard

## Số camera đồng thời

Mở **Hệ thống → Luồng tải camera**, chọn **1–5 camera** rồi lưu.
Đây là số camera được nhận công việc đồng thời, không phải số lần upload Telegram.
Giá trị được lưu trong SQLite `/data/archive.db`, vẫn còn sau khi restart/update Docker.
Nếu chưa lưu trên dashboard, ứng dụng dùng `SD_DOWNLOAD_WORKERS` trong `.env`.
Sau khi đã lưu, dashboard được ưu tiên hơn biến môi trường này.

Thay đổi áp dụng khi nhận công việc mới, không cần restart container. Khi giảm giới
hạn, công việc đã nhận vẫn hoàn tất; worker chờ số công việc đang chạy giảm
xuống dưới giới hạn mới trước khi nhận thêm.

## Một camera tải hai file song song

Mở **Camera → Chỉnh sửa → Nguồn video SD → Số file tải SD cùng lúc**, chọn **1**
hoặc **2**, rồi lưu. Mặc định là **1**; `SD_DOWNLOAD_THREADS` chỉ đặt mặc định cho
camera được thêm mới, không tự thay cấu hình camera cũ.

Mỗi luồng tải một recording khác nhau bằng phiên SDK riêng. Hai luồng không chia
nhau tải cùng một file. Việc lập chỉ mục/remux tiếp nhận file theo thời gian ghi;
luồng upload Telegram vẫn được tuần tự hóa. Remux giữ `-c copy`, không transcode.
Ví dụ **5 camera × 2 file** cho phép tối đa **10 lượt tải SD** đồng thời; lượt tìm
kiếm SD và phiên tải được tách riêng, vẫn dùng chung giới hạn cache/ổ đĩa.

Tốc độ thực tế phụ thuộc camera, thẻ SD, mạng nhà và Tailscale. Chọn 2 không đảm
bảo nhanh gấp đôi; nếu thiết bị giới hạn phiên kết nối, chọn lại 1. Cấu hình mới
áp dụng từ lượt sync kế tiếp; nút Start sync đưa camera vào hàng đợi hiện có.

## Xóa camera

Trong danh sách thiết bị, bấm **Xóa camera**, nhập chính xác tên camera để xác
nhận, rồi bấm **Xóa camera** trong hộp thoại.

- Camera bị gỡ khỏi danh sách hoạt động; dừng các lượt sync/upload mới.
- Job đang chờ bị hủy, mật khẩu thiết bị đã lưu được xóa, liên kết channel được gỡ.
- Channel, video Telegram, file ID, message ID và lịch sử video được giữ nguyên.
- Camera có công việc đang chạy phải chờ công việc đó kết thúc trước khi xóa.
- Mã camera được giữ cho lịch sử, không dùng lại cho camera mới. Khi thêm lại,
  dùng **Thêm camera** thủ công và nhập mã mới; địa chỉ IP có thể giữ nguyên.
- Các mục Hôm nay/Hôm qua/kho video vẫn đọc được video lịch sử đã lưu.

Ứng dụng giữ bản ghi camera không hoạt động (`deleted_at`) thay vì xóa catalog
lịch sử. Không reset database hoặc dùng `docker compose down -v` khi cập nhật.

## API cho dashboard

Các API cần phiên đăng nhập, mật khẩu ban đầu đã đổi, cùng origin và CSRF token.

```http
GET /api/pipeline-settings

PATCH /api/pipeline-settings
Content-Type: application/json
X-CSRF-Token: SESSION_CSRF

{"sd_download_workers":5}
```

```http
PATCH /api/cameras/CAMERA_ID
Content-Type: application/json
X-CSRF-Token: SESSION_CSRF

{"sd_download_threads":2}
```

```http
DELETE /api/cameras/CAMERA_ID
Content-Type: application/json
X-CSRF-Token: SESSION_CSRF

{"confirm_name":"Tên camera hiện tại"}
```

Xóa trả `camera_busy` (409) nếu còn công việc đang chạy. Thêm camera với mã đã
giữ cho lịch sử trả `camera_id_reserved` (409); chọn mã mới.

## Cập nhật và quay lại phiên bản cũ

Giữ nguyên `.env`, compose project, SDK mount và các volumes. Backup SQLite bằng
SQLite backup API trước khi nâng cấp. Bản này chỉ thêm cột/cấu hình; không đổi
đích upload hoặc số luồng của camera hiện có.

Rollback image/code không khôi phục camera đã xóa. Phiên bản cũ chưa hiểu
`deleted_at` có thể hiển thị lại camera đã ngừng hoạt động; không khôi phục database
cũ đè lên các xác nhận upload phát sinh sau backup.
