# Mô hình điều hướng video trong channel camera

Mỗi camera dùng một private channel riêng. Người quản lý có thể gom các channel
và bot trong giao diện nhóm/community của Telegram nếu tài khoản đang dùng hỗ
trợ. Ứng dụng không tự tạo community, channel hoặc di chuyển video đã upload.
Bot hiện có giữ chức năng tra cứu, tải album, thùng rác và quản lý quyền truy cập.

## Một Trang chủ ghim, năm lối mở

Mỗi camera/đích channel chỉ giữ một node `root`, một message ID đã xác nhận và
một tin ghim Trang chủ. Nội dung dùng **tên camera**, không đưa mã nội bộ hoặc
danh sách ngày vào tin ghim:

- **🎬 Video mới nhất:** video có thời điểm bắt đầu ghi hình mới nhất.
- **📅 Hôm nay:** mục lục ngày theo múi giờ cấu hình; khi chưa có video hôm nay,
  liên kết chuyển thành **Ngày gần nhất có video**.
- **🗓 Tháng hiện tại:** mục lục tháng hiện tại khi có dữ liệu. Tháng chưa có
  video không tạo trang rỗng hoặc liên kết giả.
- **📁 Kho lưu trữ theo năm:** danh sách năm có dữ liệu, có phân trang.
- **🤖 Bot quản lý:** bot của hệ thống, không phải một bot mới.

Sang tháng/năm mới hoặc tải bù clip cũ chỉ cập nhật đích liên kết. Không ghim thêm
tin theo ngày/tháng và không kéo dài Trang chủ theo lượng lịch sử.

## Cây mục lục

```text
Trang chủ
└── Danh sách năm (20 năm/trang)
    └── Năm → các tháng có video
        └── Tháng → các ngày có video
            └── Ngày → 24 giờ, phân biệt giờ có/chưa có dữ liệu
                ├── Đúng thứ tự, liền nhau: mở video đầu tiên của giờ
                └── Đảo thứ tự/không liền nhau: danh sách giờ (20 clip/trang)
```

Trang năm không liệt kê ngày. Trang tháng có tối đa 31 lựa chọn ngày; không
liệt kê từng video. Trang ngày có tối đa 24 lựa chọn giờ; không liệt kê hàng
trăm clip. Các trang có lối về cấp trên; tháng/ngày trước và sau đi qua **kỳ có
dữ liệu**, không đi qua từng kỳ trống. Thiếu dữ liệu không tự được mô tả là lỗi
camera.

### Mở một giờ

Hệ thống lấy clip theo thời điểm bắt đầu ghi hình. Nếu các `message_id` riêng
biệt liên tiếp và tăng đúng thứ tự ghi hình, liên kết giờ mở video đầu tiên
trong channel. Người xem tiếp tục xem theo dòng thời gian Telegram.

Khi clip tải bù, thứ tự bị đảo hoặc có khoảng cách giữa các bài đăng, hệ thống
tạo danh sách giờ riêng, sắp xếp theo thời gian ghi hình. Mỗi mục dẫn đến đúng
video gốc bằng liên kết `https://t.me/c/<channel>/<message_id>`. Danh sách dài
được chia 20 clip/trang và có nút sang trang; tham chiếu media bị trùng chỉ xuất
hiện một lần trong danh sách. Các clip riêng biệt có cùng mốc bắt đầu/kết thúc
được phân biệt bằng số đoạn, không nhập chung thành một video. Hệ thống không
tải lại media về VPS để tạo mục lục.

Người xem cần tham gia private channel và có quyền Telegram thích hợp để mở
video gốc; allowlist của bot không tự cấp quyền thành viên channel.

## Thời gian và thay đổi dữ liệu

- Tất cả cấp năm/tháng/ngày/giờ dùng `start_ms` trong múi giờ cấu hình, không dùng
  `uploaded_at` hoặc thứ tự ID gửi.
- Clip qua nửa đêm thuộc **ngày và giờ bắt đầu**; caption ghi rõ mốc kết thúc
  sang ngày sau.
- Caption video dùng tên camera, ngày, giờ bắt đầu–kết thúc và thời lượng ngắn;
  remux vẫn là stream copy, không transcode.
- Xóa vào thùng rác loại liên kết video khỏi mục lục; khôi phục bổ sung lại liên
  kết. Các trang đã biết được cập nhật tại chỗ, không nhân bản.
- Đổi tên camera dùng tên mới trong điều hướng nhưng giữ lịch sử và message ID.
- Camera tạm dừng vẫn giữ các trang lịch sử; không tự xóa video hoặc channel.
- Các media của bot khác, đích channel cũ, video chưa upload thành công hoặc
  đang bị xóa không được đưa vào mục lục của channel hiện tại.

Telegram Bot API không cung cấp việc đọc lại toàn bộ lịch sử channel. Clip đã
upload có xác nhận trong SQLite là nguồn xây mục lục; việc tự xóa video ngoài
ứng dụng cần xử lý tham chiếu tương ứng, không giả định bot tự quan sát mọi lần
xóa thủ công.

## Cập nhật bản đang chạy

Các message ID mục lục cũ được giữ khi nâng cấp bảng 4 loại
`root/year/month/day` thành 6 loại `root/years/year/month/day/hour`. Worker tạo
trang danh sách năm và các trang giờ cần thiết, sửa Trang chủ cũ tại chỗ và giữ
một tin ghim. Media, camera, channel bindings và trạng thái upload không được
di chuyển hoặc reset khi nâng cấp.

Mỗi trang được định danh duy nhất bằng camera, channel, loại trang và kỳ. Node
giờ dùng kỳ `YYYY-MM-DDTHH` (trang sau là `YYYY-MM-DDTHH:2`); danh sách năm dùng
`years` (trang sau là `years:2`). Node cũ của kỳ hết dữ liệu không được dùng làm
đích liên kết giả; ID đã biết vẫn có thể dùng khi dữ liệu được khôi phục.

Lượt gửi mục lục bị timeout/chưa biết kết quả giữ trạng thái `needs_reconcile`.
Không tự gửi lại vì có nguy cơ trùng tin. Rebuild chỉ đưa công việc vào hàng
đợi; xác nhận message ID thật mới tiếp tục lượt chưa rõ kết quả. Rebuild bản
thử (`dry_run`) không gọi Telegram.

## Kiểm thử chấp nhận

`tests/test_channel_navigation_model.py` dùng cơ sở dữ liệu và Telegram giả lập,
không gọi camera hoặc Telegram thật. Bộ kiểm thử bao gồm Trang chủ cố định,
liên kết thật, 24 giờ, tải bù, phân trang >20 năm và >20 clip/giờ, năm nhuận,
clip qua nửa đêm, xóa/khôi phục, tên dài/HTML, migration và bảo vệ lượt gửi chưa
có xác nhận. Những kiểm tra worker/lease/rate-limit cũ vẫn được giữ trong
`tests/test_channel_index.py`.

```bash
python -m unittest tests.test_channel_navigation_model tests.test_channel_index -v
```
