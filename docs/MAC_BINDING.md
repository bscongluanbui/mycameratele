# Camera theo MAC, tự khôi phục IP sau khi DHCP đổi

## Luồng hoạt động

- Camera vẫn tải SD **trực tiếp lên VPS qua Tailscale**. Remux MP4 giữ `-c copy`;
  upload, dọn cache, channel và lịch sử không thay đổi.
- Box trong LAN chỉ đọc metadata IP–MAC: ICMP echo có challenge mới + bảng
  neighbor Linux (`icmp+netlink`), không mở cổng SDK/RTSP/HTTP của camera.
  Không SDK, mật khẩu camera, video, cache hoặc chuyển tiếp media trên box.
- Mỗi 60 giây box cập nhật danh sách IP–MAC. Trước đăng nhập SDK và mỗi lần tải,
  VPS kiểm chứng riêng IP–MAC. Không thấy MAC, trùng MAC/IP, subnet sai hoặc
  metadata quá 120 giây: tạm dừng tải SD, không đoán mật khẩu/camera.
- Hai camera đổi chéo IP được cập nhật nguyên tử. Chỉ `host` và trạng thái nhận
  diện đổi; camera ID, tên, password, channel và recording key giữ nguyên.
- Observer hỗ trợ subnet IPv4 riêng `/24` đến `/30`, một observer/subnet cho mỗi
  stack. Camera cần trả lời ICMP trong LAN; cổng SDK riêng vẫn dùng khi tải SD,
  nhưng không phải điều kiện để xác minh MAC.
- Sau mất mạng, observer giữ IP–MAC đã xác minh tối đa 24 giờ làm **gợi ý thử
  lại**, không coi đó là bằng chứng còn hiệu lực. Camera chỉ được công bố lại
  sau khi ICMP và bảng neighbor xác minh thành công; bằng chứng quá 120 giây
  vẫn hết hạn như trên.

MAC là nhận diện mạng, không thay thế mật khẩu đăng nhập camera. VPS ở subnet
khác không tự nhìn được MAC camera qua route; vì thế cần observer ở LAN.

## Khi thêm/import camera trên dashboard

1. **Thêm camera** hoặc **Tìm camera trong LAN → Import** như trước.
2. Điền **MAC** của đúng camera, ví dụ `94:ec:13:14:d2:a0` (chấp nhận chữ hoa,
   dấu `-` hoặc 12 ký tự hex; lưu dạng chữ thường có dấu `:`).
3. Chọn **Tự cập nhật IP theo MAC**, điền **Subnet nhận diện**, ví dụ
   `192.168.2.0/24`. IP hiện tại cần nằm trong subnet đó.
4. Lưu. Lượt sync kế tiếp tìm IP theo MAC. Đổi IP không tạo camera mới và không
   chuyển channel. Có thể tắt auto-IP để tiếp tục nhập IP thủ công.

Import có MAC/subnet/checkbox riêng cho từng hàng. MAC quan sát được từ observer
được điền sẵn, nhưng checkbox **không tự bật**. Không lấy MAC của IP cũ làm MAC
của camera cũ sau DHCP nếu chưa xác nhận: IP cũ có thể thuộc camera khác.

## Cài observer trên box LAN

Sử dụng image chứa tính năng này; giữ tag/digest giống bản đã kiểm thử trên VPS.
Các lệnh dưới chạy tại một thư mục riêng, không dùng lại service video collector.

```bash
mkdir -p ~/mycameratele-identity/secrets
cd ~/mycameratele-identity
# Đặt compose.identity.yaml của repo vào thư mục này.
umask 077
python3 -c 'import secrets; print(secrets.token_urlsafe(48))' > secrets/lan-identity-token
sudo chown 10001:10001 secrets/lan-identity-token
sudo chmod 0400 secrets/lan-identity-token
cat > .env <<'EOF'
ARCHIVE_IMAGE=ghcr.io/bscongluanbui/mycameratele:latest
LAN_IDENTITY_BIND=100.92.56.98
LAN_IDENTITY_SUBNET=192.168.2.0/24
LAN_IDENTITY_INTERFACE=eth0
LAN_IDENTITY_PORT=8790
LAN_IDENTITY_TOKEN_PATH=./secrets/lan-identity-token
EOF
docker compose -f compose.identity.yaml pull
docker compose -f compose.identity.yaml up -d
```

Thay IP Tailscale, interface LAN và subnet theo từng nhà. Bind chỉ nhận IP
Tailscale. Token khác nhau cho từng nhà; không đưa token vào Git hoặc URL.
Giới hạn ACL Tailscale cho VPS truy cập TCP 8790 của box. Container user 10001,
không Linux capability, root filesystem chỉ đọc, CPU tối đa 0.15, RAM 96 MB.
Host Linux cần cho UID/GID 10001 dùng ICMP datagram socket qua
`net.ipv4.ping_group_range`; observer không dùng raw socket, không cần thêm
`NET_RAW` và không tự chuyển sang dò TCP khi thiếu quyền ICMP.

## Kết nối VPS

Chuyển **chính file token của nhà đó** qua SSH vào
`/home/ubuntu/bama/secrets/lan-identity-token`, đặt owner `10001:10001`, mode 0400.
Không dùng token bot Telegram làm token observer.

```dotenv
LAN_IDENTITY_URL=http://100.92.56.98:8790
LAN_IDENTITY_TOKEN_PATH=./secrets/lan-identity-token
# Giữ các compose hiện có; chỉ thêm override identity-client vào cuối.
COMPOSE_FILE=compose.yaml:compose.local.yaml:compose.sdk.yaml:compose.identity-client.yaml
```

```bash
cd /home/ubuntu/bama
docker compose --profile local-api config --quiet
docker compose --profile local-api up -d archive dashboard
```

Dashboard giữ phiên đăng nhập và yêu cầu đổi mật khẩu mặc định như trước.
`GET /api/identity/snapshot` chỉ dùng sau đăng nhập; không trả URL/token/password.
Lỗi observer không ảnh hưởng nhập IP thủ công hoặc upload cache đã có.

## Chuyển lại IP thủ công

Tắt checkbox auto-IP trên từng camera, lưu IP hiện tại. Sau khi mọi camera đã
chuyển, bỏ override `compose.identity-client.yaml` và URL khỏi VPS; chạy lại
Compose archive/dashboard. Box có thể dừng riêng `compose.identity.yaml`.
Không xóa volume/database/channel và không bật lại collector media.
