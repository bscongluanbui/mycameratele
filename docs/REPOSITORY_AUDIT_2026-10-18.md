# Audit và dọn repo — 18/10/2026

## 1. Phạm vi

- Repository: `mycameratele`; nhánh `main`, baseline `87edd3b14f9d1b642267e768cec7f4f89fb25dd7`.
- Rà toàn bộ **25 module backend**, JavaScript/HTML/CSS, 8 script, các Dockerfile, Compose, workflow, `.env.example` và hướng dẫn vận hành.
- Đối chiếu định nghĩa → điểm gọi → CLI/HTTP callback/worker → test trước khi xóa. Kết quả đếm tên chỉ là bộ lọc, không tự coi callback động là dead code.
- Đợt này chỉ thay nguồn/tài liệu và kiểm thử local; không SSH, sửa mạng, xóa dữ liệu, restart hoặc triển khai trên VPS/box.
- Tài liệu luồng chạy chi tiết: [IMPORTANT_PROCESSES.md](IMPORTANT_PROCESSES.md). Các mô tả mặc định được đối chiếu từ code/Compose; không phải khẳng định cấu hình production hiện tại đã được đo lại.

## 2. Đã loại bỏ hoặc sửa

| Thay đổi | Bằng chứng và tác dụng | Phần giữ nguyên |
|---|---|---|
| Bỏ `ChannelIndex.message_url()` | Không có caller runtime trong repo; chỉ có một assertion gọi wrapper. Dashboard đã lấy URL qua `Archive.cameras()['channel_index_url']`. | Hàm `channel_message_url()`, schema SQLite, URL video, message ID, channel ID và xử lý index vẫn giữ. |
| Đổi assertion sang đường đi production | `tests/test_channel_index.py` kiểm tra `Archive.cameras()` trả đúng URL root sau tạo index. | Không bỏ bài test, không giảm độ phủ điều hướng. |
| Không đóng gói `archive_app/web/test_web.js` vào image | File test Node **87.172 byte** không nằm trong whitelist asset dashboard; Node tests chạy từ checkout CI, không chạy trong image Python. Thêm đúng đường dẫn vào `.dockerignore`. | File vẫn ở Git và CI, các asset `index.html`, `app.js`, `style.css` vẫn đóng gói. Thay đổi có hiệu lực ở lần build sau; chưa đo mức giảm compressed image. |
| Sửa README retention | Compose và `.env.example` hiện dùng `CACHE_RETENTION_HOURS=0`, không phải mặc định 1 giờ. | Có thể chủ động đặt `1` để giữ thêm; upload chưa được xác nhận không bị coi như thành công. |
| Sửa hướng dẫn Local Bot API | Override chọn `local_file`, API đọc `/cache` read-only; MP4 không cần multipart trung gian. | Raw/document và opt-out vẫn multipart; spool 5 GB là admission trên đĩa, không phải RAM/quota cứng. |
| Sửa hướng dẫn collector | Phân biệt collector truyền media tùy chọn với observer MAC metadata-only; bỏ đường dẫn SDK benchmark đã nghỉ dùng, hướng dẫn `./sdk` ổn định. | Giữ source/Compose collector cho hệ thống khác chủ động sử dụng. Không tự bật collector ở Bama. |
| Làm rõ player trong `.env.example` | Chế độ channel mở tin Telegram gốc; URL player VPS chỉ dành cho nhánh `owner_private` tùy chọn. | Không xóa tính năng xem cũ hoặc thay bot/channel của người dùng. |

## 3. Các mục giữ lại có lý do

### Backend

- `TimeMenus.parse_timestamp()` là API/test seam nhỏ cho bộ parse ngày/giờ. Runtime cần thêm cờ date-only nên dùng `_parse_selection()`; không thay API chỉ để giảm ba dòng.
- `sd_collector.py`, `sd_collector_client.py`, `compose.collector.yaml`, `compose.remote-sd.yaml` còn CLI/backend/validator/tài liệu sử dụng. Một deployment không dùng collector không khiến tính năng này thành code chết.
- Player VPS, raw/passthrough, migration SQLite và reconcile vẫn có nhánh sử dụng/test. Chúng bảo vệ khả năng đọc dữ liệu cũ và xử lý upload mơ hồ.
- HTTP `do_HEAD`, `do_PATCH` được dispatch động; closure `running`/`upload_error_type` còn consumer; không xóa theo đếm tên đơn thuần.

### Web / CI / packaging

- **74 function có tên**, **38 helper**, **172 CSS class token** đều có tham chiếu. Chưa có bằng chứng đủ chắc để bỏ thêm handler hoặc selector.
- 8 script có consumer trong workflow/hướng dẫn/smoke. Các Compose giữ vai trò distinct: production, local API, SDK, discovery/identity, collector và local build.
- `tests/` được dùng trong Docker test stage và lệnh test image đã công bố; không cắt khỏi runtime bằng một thay đổi cleanup thiếu kiểm chứng.
- `sample_manifest.json`, `input/.gitkeep`, `action-pins.json` là nguồn ví dụ/metadata có chủ đích, không phải file rác.

## 4. Kiểm kê và nguyên tắc dọn file

Inventory đầu đợt không có media, database, archive build, bytecode hoặc file ignored thừa trong **repo này**. Không tạo số liệu xóa giả từ thư mục đã sạch.

Fixture tạm phát sinh khi kiểm thử chỉ được dọn sau khi tiến trình sở hữu đã kết thúc, theo đúng thư mục được tạo; không dùng `git clean -fdx`, wildcard xóa media hoặc `docker system prune` trên môi trường đang chạy.

Các mục sau không thuộc rác và phải được bảo toàn:

- `.env`, `secrets/`, `sdk/`, credentials và token;
- `archive-state`, `archive-cache`, `bot-api-state`, spool đang dùng;
- bản ghi đang tải/gửi, lỗi chưa hết hạn, `upload_unknown` cần đối soát;
- DB, receipt Telegram, stable camera ID, channel binding, index message ID;
- backup/rollback gần nhất, Tailscale route, rule egress và cấu hình dịch vụ IPv6 riêng trên box.

`.gitignore`/`.dockerignore` là lớp chống đưa file vào Git/build, **không** phải bằng chứng cho phép xóa nội dung runtime. Hướng dẫn bộ dọn cache do ứng dụng quản lý ở [CACHE_RETENTION.md](CACHE_RETENTION.md).

## 5. Quy trình kiểm chứng

1. Lưu ZIP nguồn baseline và SHA-256 trước khi sửa.
2. Chạy full Python suite baseline trong container local, network disabled, không mount credential/state/media.
3. Sửa đúng các mục đã xác nhận; giữ tên test và kiểm tra đường production thay wrapper.
4. Chạy full suite mã đã sửa bằng cùng runtime; Node helper tests, manifest self-test và validator Compose.
5. Kiểm tra chỉ thay các file đã duyệt; Python compile không tạo bytecode; kiểm tra link tài liệu và whitespace.
6. Khôi phục **bản sao riêng** bằng script có marker + kiểm tra hash, kiểm chứng file thêm mới bị gỡ đúng và DB/secret giả lập không đổi. Repo hiện tại vẫn giữ thay đổi.
7. Dọn source-copy/test fixture do lượt audit tạo ra sau khi test xong. Lưu báo cáo và ZIP phục hồi, không chạy rollback lên production.

### Lệnh tái hiện từ root repo

```bash
# Không cần thông số camera/Telegram cho unit tests.
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest discover -s tests -v
node archive_app/web/test_web.js
PYTHONDONTWRITEBYTECODE=1 python -B scripts/ci_validate_manifest.py --self-test
PYTHONDONTWRITEBYTECODE=1 python -B scripts/ci_validate_private_compose.py
PYTHONDONTWRITEBYTECODE=1 python -B scripts/ci_validate_collector_compose.py
git diff --check
```

Một số test dùng filesystem POSIX/socket/FFmpeg. Khi môi trường host khác Linux, chạy trong image Linux có FFmpeg; không coi lỗi quyền sandbox hoặc thiếu dependency là bằng chứng pipeline production lỗi. Test giả lập không thay cho đo tải thật từ camera hay upload thật lên Telegram.

### Kết quả nghiệm thu

| Kiểm tra | Baseline | Sau cleanup |
|---|---|---|
| Full Python suite, Linux container cô lập | **1.425 tests, OK**, 230,558 giây | **1.425 tests, OK**, 195,869 giây |
| Channel index focused | **45 tests, OK** | **45 tests, OK** |
| Node helper tests | Có trong audit web | **922/922** |
| OCI manifest validator self-test | Không đổi logic validator | **6/6** |
| House01/private Compose | — | **PASS**: retention 0h, lỗi 72h, SD 900s, local-file MP4, spool đĩa 5 GB |
| Collector Compose | — | **PASS**, tùy chọn còn được giữ |
| AST/hash/Markdown link/fence/whitespace | Baseline giữ hash | **PASS** |
| Rollback trên bản sao riêng | Nguồn gốc được giữ | **PASS**, hash gốc khôi phục, DB/secret giả lập không đổi |

Tất cả phép kiểm tra nghiệm thu exit **0**. Chênh thời gian chạy test không phải phép đo tốc độ camera/upload. Hồ sơ riêng giữ exact command, input, literal output, SHA-256, source ZIP và script rollback có guard. Không tự coi unit/config test là upload thật.

Inventory cuối: không còn `__pycache__` hoặc `tests/.tmp*` trong repo. Bản sao source baseline của lượt kiểm thử được dọn sau khi xác nhận hash; ZIP baseline/modified và chứng cứ khôi phục được giữ ngoài repo.

## 6. Những việc không tự thực hiện

- Không đổi chức năng scheduler, số luồng, retry, encoder hoặc policy cleanup.
- Không build/publish Docker `latest`, không push Git hoặc deploy VPS/box trong đợt repo audit này.
- Không gỡ `.250`, route Tailscale hoặc sửa app IPv6. Những thay đổi mạng cần kiểm tra riêng và lưu rollback riêng.
- Không bỏ code cũ còn được hỗ trợ chỉ vì không dùng ở Bama; muốn ngừng tính năng phải có kế hoạch deprecation, migration dữ liệu và test riêng.
