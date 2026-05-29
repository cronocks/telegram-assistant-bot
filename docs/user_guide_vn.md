# Hướng Dẫn Sử Dụng — Family Assistant Bot

> Hướng dẫn này dành cho **người dùng cuối**. Tất cả lệnh đều hoạt động trên Telegram và Web UI.
> Lệnh không phân biệt dấu tiếng Việt — `ghi nho` và `ghi nhớ` đều hoạt động.

---

## Mục lục

1. [Đăng ký & Đăng nhập](#1-đăng-ký--đăng-nhập)
2. [Ghi chú & Nhật ký](#2-ghi-chú--nhật-ký)
3. [Wiki gia đình](#3-wiki-gia-đình)
4. [Trí nhớ cá nhân (L1 Memory)](#4-trí-nhớ-cá-nhân)
5. [Công việc & Lịch học (Tasks)](#5-công-việc--lịch-học)
6. [Kỷ niệm & Nhắc ngày](#6-kỷ-niệm--nhắc-ngày)
7. [Chi tiêu & Ngân sách](#7-chi-tiêu--ngân-sách)
8. [Hỏi đáp tự do](#8-hỏi-đáp-tự-do)
9. [Hồ sơ cá nhân](#9-hồ-sơ-cá-nhân)
10. [Quản trị (Admin)](#10-quản-trị-admin)

---

## 1. Đăng ký & Đăng nhập

### Telegram
Nhận mã mời từ admin → gửi vào bot:
```
dang ky: <mã mời>
```

### Web UI
1. Nhận mật khẩu tạm từ admin
2. Truy cập: `https://assitant-bot.onrender.com/login`
3. Đăng nhập → bot yêu cầu đổi mật khẩu lần đầu
4. Tự đổi mật khẩu web bất cứ lúc nào qua Telegram:
```
doi web pass: <mật khẩu mới>
```

---

## 2. Ghi chú & Nhật ký

### Tạo ghi chú
```
ghi nho hôm nay họp lúc 3h, cần chuẩn bị slide
ghi nho vao cong viec: cần gửi báo cáo trước thứ 6
```
> `ghi nho vao <tên file>:` — thêm vào file đã có (fuzzy match tên)

### Nhật ký hàng ngày
```
nhat ky sáng nay chạy bộ 5km, cảm thấy khỏe
```
> Bot tự tạo file nhật ký theo ngày (UTC+7). Mỗi lần gọi là append thêm, không ghi đè.

### Đọc & tìm kiếm
```
xem nhat ky               — đọc nhật ký hôm nay
xem cong viec             — đọc file "công việc" (fuzzy match)
liet ke                   — liệt kê tất cả file (mới nhất trước)
liet ke 2                 — trang 2
tim báo cáo               — tìm trong nội dung file
tom tat tuan nay          — tóm tắt hoạt động tuần này
```

### Chia sẻ file
```
chia se cong viec         — cho cả nhà cùng đọc
bo chia se cong viec      — đặt lại thành riêng tư
xem scope cong viec       — xem trạng thái chia sẻ hiện tại
```

---

## 3. Wiki gia đình

Wiki là kho tri thức chung — mặc định cả nhà đều xem được.

```
wiki Mật khẩu wifi nhà: abcd1234, router TP-Link WR840N
wiki Lịch học bơi của An: thứ 3 và thứ 5, 17:30-19:00, Hồ bơi Rạch Miễu
```

### Tra cứu wiki
```
hoi wiki mật khẩu wifi là gì?
xem wiki                  — liệt kê tất cả trang wiki
xem wiki wifi             — đọc trang wiki về wifi
```

---

## 4. Trí nhớ cá nhân

Bot lưu thông tin về bạn để trả lời cá nhân hóa hơn.

```
cap nhat tri nho          — bot đọc ghi chú gần đây, tự cập nhật hồ sơ
xem tri nho               — xem snapshot trí nhớ (facts cuộn)
xem ho so                 — xem hồ sơ cá nhân ổn định
```

---

## 5. Công việc & Lịch học

### Tạo task
```
tao task: nộp báo cáo tháng trước 5h chiều thứ 6
task: đặt lịch khám nha khoa tuần tới
```
> Bot tự hiểu ngày giờ từ mô tả tự nhiên bằng tiếng Việt.

### Quản lý task
```
danh sach task            — xem task đang chờ
task 5                    — xem chi tiết task #5
xong task: 5              — đánh dấu hoàn thành
huy task: 5               — hủy task
hoan task: 5 30           — hoãn thêm 30 phút
```

### Lịch học định kỳ
```
lich hoc: học toán thứ 3 và thứ 5 lúc 7 giờ tối
danh sach lich hoc        — xem lịch học đang hoạt động
sua lich hoc: 3 học toán thứ 2, 4, 6 lúc 7 giờ tối
huy lich hoc: 3           — hủy lịch học #3
```

### Tổng kết & cấu hình
```
tom tat hom nay                     — tổng kết task hôm nay
cau hinh tong ket: 21:00            — thay đổi giờ gửi tổng kết
cau hinh tong ket: tat              — tắt tổng kết tự động
cau hinh gio mac dinh: 09:00        — giờ mặc định cho task không có giờ cụ thể
```

---

## 6. Kỷ niệm & Nhắc ngày

Nhắc tự động trước ngày kỷ niệm (30/15/7/3/1 ngày + đúng ngày, lúc 08:00 sáng).

### Thêm kỷ niệm
```
them ky niem: Giỗ ông nội, am 10/3, gio
them ky niem: Kỷ niệm ngày cưới, duong 15/8, cuoi
them ky niem: Sinh nhật mẹ, duong 20/5, khac
```
> `am` = âm lịch (tự động quy đổi mỗi năm), `duong` = dương lịch
> Loại: `gio` (giỗ), `cuoi` (cưới), `khac` (dịp khác)

### Quản lý
```
danh sach ky niem                   — xem tất cả kỷ niệm
ky niem 2                           — xem chi tiết kỷ niệm #2
xoa ky niem: 2                      — xóa kỷ niệm #2
sua ky niem: 2, ten=Giỗ bà nội      — sửa tên
sua ky niem: 2, ngay=am 12/3        — sửa ngày
sua ky niem: 2, nhac=7,3,1,0        — tùy chỉnh nhắc (số ngày trước)
```

---

## 7. Chi tiêu & Ngân sách

### Ghi thu/chi
```
chi: 50k ăn trưa
chi: 200000 đổ xăng
thu: 5tr lương tháng 5
thu: 500k tiền thưởng
```
> Định dạng số linh hoạt: `50k` = 50.000 đ, `5tr` hoặc `5m` = 5.000.000 đ

### Xem & quản lý bút toán
```
danh sach ghi chep        — 20 bút toán gần nhất
ghi chep: 7               — xem chi tiết bút toán #7
sua ghi chep: 7, so=60000 — sửa số tiền
sua ghi chep: 7, mo ta=ăn sáng — sửa mô tả
huy ghi chep: 7           — hủy bút toán (giữ 30 ngày rồi xóa)
```

### Danh mục
```
xem danh muc              — xem danh sách danh mục
them danh muc: Ăn uống, chi        — tạo danh mục "Ăn uống" (loại chi)
them danh muc: Lương, thu          — tạo danh mục "Lương" (loại thu)
them danh muc: Đi lại, chi, chung  — tạo danh mục chung (cần admin/manager)
xoa danh muc: 3           — xóa danh mục #3
sua danh muc: 3 Giao thông — đổi tên danh mục #3
```

### Báo cáo
```
xem chi tieu              — tổng thu/chi 7 ngày qua
bao cao thang             — báo cáo tháng hiện tại (tổng + theo danh mục)
bao cao thang 2026-04     — báo cáo tháng 4/2026
bao cao nam               — báo cáo từng tháng trong năm nay
xem han muc               — xem hạn mức chi và mục tiêu tiết kiệm
```

### Hạn mức & mục tiêu tiết kiệm
```
dat han muc chi: 5000000            — đặt hạn mức chi tháng là 5 triệu
dat muc tieu tiet kiem: 2000000     — đặt mục tiêu tiết kiệm 2 triệu
```
> Bot sẽ cảnh báo khi chi tiêu đạt 80% và 100% hạn mức.
> Mỗi thứ 2 lúc 08:00 bot gửi tóm tắt thu/chi tuần qua.

---

## 8. Hỏi đáp tự do

Gõ câu hỏi bất kỳ — bot tìm trong wiki, ghi chú và trả lời:

```
wifi nhà mình là gì?
lịch học bơi của An như thế nào?
tháng này tôi đã chi bao nhiêu cho ăn uống?
```

---

## 9. Hồ sơ cá nhân

```
toi la ai                           — xem tên, username, role, id
dat username: myhandle               — đặt username (lần đầu trực tiếp; đổi lần sau cần admin duyệt)
dat birthdate: 1990-05-20            — cập nhật ngày sinh (cần admin/manager duyệt)
xem quota                           — xem số token LLM đã dùng tháng này
```

---

## 10. Quản trị (Admin)

### Quản lý người dùng
```
them user: Nguyễn An, member        — tạo mã mời cho user mới
xem danh sach user                  — liệt kê tất cả user
xoa user: An                        — vô hiệu hóa user
doi role: An manager                — đổi role
dat quota: An 100000                — giới hạn token/tháng cho user
reset quota: An                     — reset số dùng về 0
dat cha: Bố An                      — thiết lập quan hệ cha-con
dat web pass: An, matkhau123        — đặt mật khẩu web cho user (user phải đổi lần đầu)
```

### Nâng quyền tạm thời (Sudo)
```
sudo: <mật khẩu>                    — nâng role manager → admin (15 phút)
thoat sudo                          — hạ quyền ngay lập tức
dat mat khau: <mật khẩu mới>        — đặt/đổi mật khẩu admin (chỉ admin thực sự)
```

### Audit & Recycle bin
```
xem audit                           — 50 sự kiện audit gần nhất
xem audit sudo_elevate              — lọc theo loại sự kiện
xem thung rac                       — xem items đã xóa (giữ 180 ngày)
khoi phuc: note 12                  — khôi phục item
xoa han: note 12                    — xóa vĩnh viễn ngay
```

### Backup & Export
```
xuat du lieu                        — export toàn bộ data của mình lên Drive (ZIP)
xuat du lieu: An                    — admin export data của user An
```

---

## Mẹo sử dụng

- **Không cân dấu:** `ghi nho`, `ghi nhớ`, `GHI NHO` đều hoạt động
- **Xem tất cả lệnh:** `/help` → chọn nhóm → `/help chi tieu`, `/help cong viec`, v.v.
- **Web UI:** `https://assitant-bot.onrender.com` — có thể xem/thêm/sửa task, kỷ niệm, chi tiêu qua trình duyệt
- **Hỏi đáp thông minh:** Gõ câu hỏi tự nhiên, bot tự tìm trong wiki + ghi chú của bạn trước khi trả lời
