# File lỗi và hàng lỗi

- File lỗi cũ được thử lại tối đa **3 lần**. Bộ đếm nằm trong SQLite,
  không đặt lại khi restart, cập nhật image hoặc bấm Start sync.
- Tool xử lý video mới trước, sau đó thử lại tối đa một file lỗi cũ mỗi
  lượt sync. Các lần thử cách nhau ít nhất 15 phút; không lặp nóng.
- Remux chỉ dùng stream-copy (`-c copy`), không transcode hay bỏ audio.
- MP4 bị Telegram từ chối rõ ràng chỉ được thử lại sau khi cùng camera,
  channel và bot có xác nhận upload mới trong lượt hiện tại.
- File vẫn lỗi sau 3 lần vào **Hàng lỗi**, ngừng tự upload/remux và không
  chặn lịch sử video tốt. `error_queue_at`, `error_retry_count` và
  `error_retry_at` lưu trạng thái; không sửa tay để gửi lại hàng loạt.
- `uploading` / `upload_unknown` không tự thử lại, kể cả có cờ hàng lỗi:
  chúng cần đối soát vì Telegram có thể đã nhận video.
- Telegram 429, hết dung lượng và lỗi quyền/kết nối hiện tại vẫn là trạng
  thái chờ hoặc lỗi pipeline; tool không coi chúng là video đã lưu.
- Cache lỗi xóa sau **72 giờ kể từ lỗi đầu tiên**, không gia hạn khi retry.
  MP4, PS/TS và staging của file upload thành công được xóa sau receipt.
- Dashboard hiển thị riêng **Thử lại**, **File lỗi giữ lại**, **Hàng lỗi**;
  trạng thái chờ lượt tiếp không dùng nhãn “Cần xử lý”.

SDK chạy trên VPS tải SD trực tiếp qua Tailscale. Box chỉ định tuyến subnet;
chức năng này không bật lại bộ tải/trạm trung chuyển trên box.
