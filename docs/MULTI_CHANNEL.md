# Mỗi camera một private channel, một bot quản lý

Luồng media giữ nguyên: SD → cache VPS → MP4 **stream-copy** → Local Bot API
đọc file trực tiếp → channel. Không transcode, không tải lại kho Telegram để
lập mục lục. Cache thành công xóa sau khi lưu xác nhận; file lỗi giữ 72 giờ.

## Thiết lập trên dashboard

1. Tạo một private channel cho mỗi camera, thêm **cùng bot hiện tại** làm admin.
   Bật quyền **Post Messages** và **Edit Messages** (quyền sửa ở channel cũng
   cho phép ghim mục lục).
2. Trong **Camera → Thêm camera / Chỉnh sửa → Gắn với channel**, chọn tên channel.
   Bấm **Làm mới channel** để cập nhật tên và quyền. Channel đã gắn với camera
   khác được đánh dấu và khóa chọn. Tên chỉ để hiển thị; chương trình lưu ID.
   Nếu nhập ID thủ công, mở **Nhập Channel ID thủ công**, điền ID dạng `-100…`.
   Ô **Tên channel hiển thị (tùy chọn)** cho phép đặt/sửa tên riêng, ví dụ
   `Bama · Phòng ngủ`. Bấm **Lưu** để ghi tên; tên xuất hiện trên thẻ camera và
   trong danh sách chọn channel, vẫn giữ sau khi làm mới hoặc khởi động lại.
   Để trống tên sẽ dùng tên Telegram trong danh sách (hoặc ID nếu chưa biết tên).
   Đây là tên trên dashboard, không đổi tên thật của channel trên Telegram và
   không đổi ID/đích upload. Khi đổi hoặc bỏ ID, tên cũ được xóa trừ khi bạn
   đồng thời nhập tên mới cho ID mới. Mỗi camera lưu một ID riêng trên dashboard;
   `TELEGRAM_STORAGE_CHANNEL_ID` trong `.env` chỉ nhận một ID kho chung cũ,
   không nhận danh sách ID cách nhau bằng dấu phẩy.
3. Bấm **Kiểm tra channel** trên thẻ camera. Chỉ ID đúng, channel private và bot
   có quyền mới có trạng thái sẵn sàng. Vẫn giữ chế độ thêm camera thủ công/quét LAN.
   Camera tạm dừng hoặc tắt upload vẫn kiểm tra quyền channel được; kiểm tra
   không tự bật camera/upload hoặc gửi video. Dashboard phân biệt sai ID,
   channel public, bot chưa là admin, thiếu quyền đăng/sửa, giới hạn Telegram
   và lỗi kết nối; không gom mọi lỗi thành thiếu quyền admin.
4. Sau khi các camera đã được gắn channel, đặt trong `.env`:

   ```dotenv
   MULTI_CHANNEL_ROUTING=true
   CHANNEL_INDEX_ENABLED=true
   CHANNEL_INDEX_DEBOUNCE_SECONDS=60
   ```

   ```sh
   docker compose -f compose.yaml -f compose.local.yaml --profile local-api up -d
   ```

   Giữ các override SDK và project name đang dùng nếu nhà đã có cấu hình riêng.
   Camera chưa gắn channel hoặc channel bị tắt dừng riêng camera đó; không gửi
   sang channel mặc định, không đánh dấu clip chờ thành đã upload.

Danh sách channel thuộc **bot đang cấu hình**, không phải tất cả channel của
tài khoản Telegram cá nhân. Bot API không có phương thức liệt kê toàn bộ channel
của tài khoản. Danh sách lưu từ `my_chat_member`/`channel_post`, ID đã cấu hình
trước đó và các camera đã gắn. Với channel mà bot đã làm admin từ trước nhưng
chưa được nhận diện, thay đổi rồi cấp lại quyền admin để phát sinh membership
update hoặc nhập ID ở mục **Nhập ID riêng** một lần. Làm mới chỉ kiểm tra các ID
đã biết; không quét tài khoản Telegram, không tạo/xóa channel.

Người xem tham gia các channel bạn muốn họ xem. Bạn tự gom channel và bot trong
Community; chức năng upload/tìm kiếm không phụ thuộc Community.

## Mục lục tự động

Mỗi channel có một **📌 KHO VIDEO** ghim, dẫn **Năm → Tháng → Ngày**. Mục ngày
gồm sáu khung bốn giờ, tổng video, video đầu/cuối và đường quay lại. Nút khung
giờ mở đúng bài video đầu khung, không khẳng định các clip nằm liền nhau.

- Thời gian lấy từ `start_ms/end_ms` ghi hình, hiển thị theo `DISPLAY_TIMEZONE`
  mặc định `Asia/Ho_Chi_Minh`; không dùng giờ upload.
- Caption có mã/tên camera, ngày giờ, dung lượng, hashtag camera/năm/tháng/ngày.
- Video gửi bù cập nhật đúng ngày cũ. Qua nửa đêm vẫn gửi một lần; bot tìm kiếm
  giữ truy vấn overlap hiện có. Mục lục tính theo ngày bắt đầu ghi hình.
- Outbox mục lục và ID video được commit cùng transaction SQLite. Worker mục lục
  có kết nối riêng, gộp cập nhật, giữ ID ổn định bằng edit; lỗi mục lục không đưa
  video đã upload vào hàng gửi lại.
- Mục lục chỉ thống kê video hiện có trong channel đang gắn. Link video lịch sử
  ở channel cũ vẫn được bot mở theo placement gốc, kể cả sau đổi channel.
- Xóa/khôi phục trong bot cập nhật chỉ mục chung như trước. Đây là xóa mềm trong
  DB, không xóa vật lý bài Telegram: thành viên channel vẫn có thể mở bài gốc.

## Kiểm tra và phục hồi chỉ mục

```sh
# Chỉ xem kế hoạch, không ghi Telegram (mặc định)
docker compose exec archive python -m archive_app rebuild-index --camera CAM01 --period 2026-10
# Xếp hàng xây lại mục lục, không copy/upload video
docker compose exec archive python -m archive_app rebuild-index --camera CAM01 --period 2026-10 --apply
```

Owner có thể dùng `/rebuild_index CAM01 2026-10 --dry-run` và `--apply` trong bot.
SQLite serialize các job cùng camera/channel bằng lease; các message duy nhất
theo camera/channel/loại/kỳ. Timeout sau `sendMessage` giữ `needs_reconcile`,
không tự đoán rằng Telegram chưa nhận bài. Sau khi owner xác nhận bài đã gửi,
đưa ID chính xác vào:

```sh
docker compose exec archive python -m archive_app reconcile-index \
  --camera CAM01 --chat-id -1001111111111 --type day \
  --period 2026-10-10 --message-id 123
```

Lỗi 429 đợi `retry_after`; quyền bị gỡ giữ job và file, các camera khác tiếp tục.
Mục lục bị xóa được tạo lại có kiểm soát rồi sửa link cha. Mục lục lỗi permission
không liên tục tạo bài mới. Bảng Telegram và kho video có trạng thái độc lập.

## Migration và rollback

- Migration **chỉ thêm** các cột camera `channel_chat_id`, `channel_enabled`,
  `channel_status`, `channel_error`, `channel_name` và recording `upload_target_chat_id`; tái sử
  dụng `storage_chat_id/storage_message_id`, `bot_id`, stable key và timestamps.
- Thêm catalog `telegram_channels`, bảng `channel_index_messages` và outbox mục
  lục. Unique channel binding ngăn hai camera dùng cùng channel trong một DB.
- DB hiện hữu được sao lưu nhất quán bằng SQLite backup API vào
  `/data/migration-backups/multi-channel-*.db` trước khi nâng schema.
- Không backfill placement bằng channel mới; không copy/xóa video cũ. Các ID
  đã commit là đích thực tế từ response Telegram. Trường hợp upload mơ hồ giữ
  `upload_unknown` cùng snapshot đích, chỉ reconcile vào đúng đích đã claim.
- Đổi mapping trong khi camera đang POST video được chặn cho đến khi hoàn tất.
- Rollback routing: đặt `MULTI_CHANNEL_ROUTING=false`, giữ lại ID channel legacy
  đang cấu hình và recreate worker/dashboard bằng Compose đang dùng. Không xóa
  schema thêm, metadata hoặc media Telegram. Khi muốn đọc nhiều channel, giữ
  bot phiên bản mới; tắt flag chỉ ảnh hưởng clip mới, link đã lưu vẫn giữ nguyên.

## Tài liệu đối chiếu

- [Telegram Bot API: quyền, membership updates, gửi/sửa/ghim](https://core.telegram.org/bots/api).
- [Telegram message links](https://core.telegram.org/api/links).
- [Telegram Communities](https://telegram.org/blog/communities-editor-invisible-messages).

## Prompt nghiệm thu có thể dùng lại

```text
Kiểm tra hai camera A/B gắn hai private channel khác nhau: gửi đúng đích,
thiếu mapping không fallback, quyền một channel bị gỡ không chặn camera kia.
Kiểm tra root→năm→tháng→ngày→6 khung giờ; late upload và video qua nửa đêm;
retry/429, restart và sendMessage mơ hồ không tạo trùng. Bot mở legacy + mới
theo ID gốc, album và thùng rác vẫn hoạt động. Remux chỉ stream-copy, cleanup
chỉ sau xác nhận/commit; không tự copy/xóa video lịch sử hoặc đổi bot token.
```
