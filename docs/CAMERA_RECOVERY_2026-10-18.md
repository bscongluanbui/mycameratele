# Bama: mất xác minh MAC sau khởi động lại — 18/10/2026

## Nguyên nhân đã đo trên máy thật

- VPS ghi epoch `1792287478`; box ghi `1792284158` trong hai lần kiểm tra
  gần nhau. Box chậm khoảng **55 phút**, không phải lỗi chữ hoa/thường của MAC.
- `timedatectl` trên box báo `System clock synchronized: no`, NTP inactive.
  DietPi dùng `CONFIG_NTP_MODE=2`: chạy đồng bộ theo lịch rồi dừng daemon.
  Box không có RTC giữ giờ đúng; sau reboot, `fake-hwclock` nạp giờ lưu cũ.
- Worker nhận `sd_identity_invalid` và dừng trước bước đăng nhập SD vì proof
  vượt TTL 120 giây. Dashboard trước đây che mã này bằng nhãn chung.
- DHCP cũng đã đổi IP: Giữa nhà `.15`, Ngoài sân `.3`, Thùng tiền `.7`,
  Tivi `.2`. Đây chỉ là kết quả tại thời điểm kiểm tra, **không hard-code**.
- Sau đồng bộ giờ, cả bốn MAC được xác minh và VPS mở TCP/8000 tới bốn IP
  trên trong khoảng 0,057–0,063 giây. Không cần nhập lại mật khẩu camera.

## Sửa trên host

Đã sao lưu `/boot/dietpi.txt` rồi đổi đúng một field `CONFIG_NTP_MODE=2` thành
`CONFIG_NTP_MODE=4` trên **box Bama**, để DietPi giữ NTP chạy liên tục:

```sh
timedatectl set-timezone Asia/Ho_Chi_Minh  # GMT+7; NTP vẫn đồng bộ epoch UTC
systemctl enable --now systemd-timesyncd
timedatectl show -p NTPSynchronized -p NTP
# Cần NTPSynchronized=yes và NTP=yes, không chỉ service active.
fake-hwclock save
```

Chỉ áp dụng field DietPi trên máy thực sự chạy DietPi. Ubuntu/Armbian dùng
dịch vụ đồng bộ giờ đang quản lý máy; không chạy hai NTP daemon cạnh tranh.
Không thay DNS, route, firewall, IP tĩnh hay cấu hình IPv6 để chữa lệch giờ.
Không tắt guard MAC hoặc tăng TTL để che lỗi đồng hồ.

Bản sao và hoàn tác riêng tư trên box:
`/root/mycameratele-lan-identity/backups/camera-recovery-20261018/`.
`ROLLBACK_TIME.sh` chỉ phục hồi chính sách giờ cũ, không đổi route/IPv6.
Không ghi token hay mật khẩu vào báo cáo công khai.

Đã kiểm tra `Timezone=Asia/Ho_Chi_Minh`, `NTP=yes`,
`NTPSynchronized=yes`, service `enabled` và giờ địa phương có offset `+0700`.
Đây là kiểm chứng cấu hình khởi động, không phải thử cắt điện máy thật.

## Sửa trong code

1. Observer trả `server_time` hiện tại, khác thời gian proof đã lưu.
2. Client so sánh giờ khi **nhận** phản hồi; lệch trên 5 giây trả
   `sd_identity_clock_skew`, vẫn chặn sử dụng dữ liệu chưa xác minh.
   Observer phiên bản cũ không có field này vẫn phải qua TTL hiện hữu.
3. Cache client tự lấy lại snapshot nếu đồng hồ client lùi, thay vì đợi giờ
   bắt kịp. Không tái sử dụng proof quá hạn.
4. ARP cũ sau đổi DHCP không còn tạo xung đột giả khi IP cũ đã bị kiểm tra
   chủ động và không trả lời. IP chưa kiểm tra hoặc cả hai IP cùng trả lời
   vẫn bị chặn. Verify có giới hạn số IP và thời gian, rồi kiểm tra lại MAC.
5. Dashboard giữ mã lỗi đã lọc và hiện nhãn riêng, ví dụ **Đồng hồ box lệch giờ**.

## Kiểm chứng và vận hành sau reboot

1. Kiểm tra NTP trên VPS và box; đảm bảo giờ đồng bộ trước khi kiểm tra MAC.
2. Đợi một vòng observer (khoảng 60–80 giây), kiểm tra bằng chứng ICMP +
   netlink mới, đúng subnet/interface và MAC duy nhất.
3. Sync tự gắn IP theo MAC; ID camera, secret, channel và lịch sử giữ nguyên.
4. Kiểm tra job có `sd_backend=hcnetsdk`, không còn lỗi `sd_identity_*`;
   dùng xác nhận Telegram trong DB để kết luận upload, không chỉ HTTP health.
5. Media vẫn đi trực tiếp SD → VPS qua Tailscale → remux `-c copy` → Telegram.
   Observer chỉ truyền metadata, không lưu hoặc chuyển tiếp video trên box.
6. Giữ nguyên queue lỗi ba lần retry, upload chưa rõ kết quả và backup riêng tư.
   Chỉ xóa tệp chẩn đoán/test do lần kiểm tra sinh ra, không xóa volume sống.

Các phép thử hồi quy bao gồm DHCP còn ARP IP cũ, IP cạnh tranh vẫn sống,
MAC thay đổi khi verify, đồng hồ lệch, proof quá hạn dù giờ server đúng,
đồng hồ client lùi và nhãn lỗi dashboard. Bộ test dùng dữ liệu giả, không
đăng nhập camera hay gửi Telegram.
