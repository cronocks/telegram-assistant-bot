# 🚨 Git Operations — User-Owned (MANDATORY)

> **TUYỆT ĐỐI:** Claude KHÔNG tự ý chạy bất kỳ lệnh git nào trong project này. Tất cả git operations (`branch`, `checkout`, `fetch`, `pull`, `add`, `commit`, `push`, `merge`, `rebase`, `reset`, `cherry-pick`, `tag`, xóa branch local/remote, ...) **do user thực hiện**.

Khi user yêu cầu Claude chạy một lệnh git cụ thể (ngoại lệ duy nhất), bắt buộc protocol 4 bước:

1. **Show** — Hiển thị chính xác lệnh sẽ chạy (đầy đủ flags, arguments)
2. **Wait** — Dừng lại, chờ user xác nhận lại ("go", "ok", "xác nhận", ...) **kể cả khi user vừa mới yêu cầu** — đây là confirm lần thứ 2 có chủ ý
3. **Execute** — Chạy đúng lệnh đã show, không thêm / bớt / sửa argument
4. **Report** — Hiển thị output thật của lệnh

Edit file (Edit/Write tool) **không phải** git operation — Claude vẫn được phép edit theo Workflow bên dưới. File edit không tự động đẩy lên git; user vẫn add/commit/push.

**Read-only được miễn protocol** — Claude tự do thực hiện không cần confirm:
- Đọc file (Read, Glob, Grep)
- Git read-only: `git log`, `git show`, `git diff` (view), `git status`, `git branch` (list), `git ls-remote`, `git fetch` / `git fetch -p`
- Bash view-only: `ls`, `cat`, `head`, `tail`

Tiêu chí: không thay đổi working tree, không thay đổi branch state, không push/pull commit. Khi nghi ngờ → áp dụng protocol 4 bước (an toàn hơn).

**Chi tiết đầy đủ, examples Good/Bad, và root-cause incident:** xem `docs/ROADMAP.md` Section 3.6.

---

# Workflow — Plan Before You Touch

Quy trình bắt buộc cho mọi thao tác file trong project này.
Claude phải lập kế hoạch trước, user xác nhận, sau đó mới thực thi. Không có ngoại lệ.

---

## Khi nào áp dụng

Áp dụng cho **mọi** thao tác tạo, sửa, xóa, đổi tên file — bao gồm source code, config, script, migration, hoặc bất kỳ file nào trong project. Không bỏ qua kể cả thay đổi "nhỏ" hay "rõ ràng".

---

## 3 Phase bắt buộc

### PHASE 1 — HIỂU YÊU CẦU
Trước khi lập kế hoạch, đảm bảo hiểu đúng yêu cầu:
- Đọc kỹ lại yêu cầu
- Nếu còn mơ hồ → hỏi MỘT câu làm rõ, chờ trả lời
- Không tự suy đoán và tiến hành; không hỏi nhiều câu cùng lúc

### PHASE 2 — LẬP KẾ HOẠCH (bắt buộc trước khi chạm vào file)
Trình bày kế hoạch theo đúng format sau:

---
**📋 Plan**

**Goal:** [tóm tắt 1 câu về kết quả đạt được]

**Changes:**
| # | Action | File | Mô tả |
|---|--------|------|-------|
| 1 | `CREATE` / `EDIT` / `DELETE` / `RENAME` | `path/to/file` | Làm gì và tại sao |
| 2 | ... | ... | ... |

**Approach:** [2-4 câu giải thích hướng kỹ thuật, quyết định chính, trade-off nếu có]

**Risk:** `low` / `medium` / `high`
[1 câu giải thích mức rủi ro]

**Dependencies:** [Liệt kê những gì cần làm trước — migration, env vars, package install — hoặc ghi "None"]

⏸ **Awaiting your approval. Reply "go" to proceed, or share feedback to adjust the plan.**

---

### PHASE 3 — THỰC THI
Chỉ bắt đầu sau khi user xác nhận rõ ràng bằng một trong các từ:
- "go", "proceed", "approved", "ok", "yes", "looks good", "ship it"
- Hoặc tiếng Việt: "được", "ok", "tiến hành", "đồng ý", "làm đi"

Nếu user góp ý thay vì xác nhận → chỉnh lại kế hoạch (quay Phase 2), không tự tiến hành.

---

## Quy tắc không được vi phạm

**Trước khi thực thi:**
- Không chạm vào file trước khi được duyệt
- Không bắt đầu bằng thay đổi "nhỏ" rồi xin phép thay đổi "lớn" sau
- Không chia kế hoạch thành nhiều phần để che giấu phạm vi thật — trình bày toàn bộ ngay từ đầu
- Nếu phát hiện cần thêm file trong lúc thực thi → dừng lại, trình bày, xác nhận lại

**Trong khi thực thi:**
- Thực hiện đúng những gì đã được duyệt — không thêm cải tiến ngoài scope
- Nếu phát hiện kế hoạch sai trong lúc thực thi → dừng, giải thích, lập kế hoạch mới
- Hoàn thành TẤT CẢ thay đổi đã duyệt trong một lượt — không để codebase ở trạng thái broken

**Báo cáo sau khi hoàn thành:**

---
**✅ Done**
| File | Action | Status |
|------|--------|--------|
| `path/to/file` | CREATED / EDITED / DELETED | ✅ |

[Những việc cần làm tiếp theo nếu có: lệnh cần chạy, env vars cần set, điều cần verify]

---

## Mức độ rủi ro

| Mức | Khi nào dùng |
|---|---|
| `low` | Chỉ tạo file mới, không chạm code cũ, phạm vi độc lập |
| `medium` | Sửa file hiện có, thay đổi interface/type, thêm dependency |
| `high` | Sửa shared utility, thay đổi DB schema, chỉnh auth/config, xóa file, refactor nhiều module |

Với thay đổi `high` risk: thêm mục **Impact** liệt kê các phần khác của codebase có thể bị ảnh hưởng.

---

## Scope Creep — Nghiêm cấm

Nếu trong lúc lập kế hoạch hoặc thực thi phát hiện điều gì đó có thể cải thiện thêm:
- KHÔNG thêm vào kế hoạch hiện tại một cách ngầm định
- Đề cập SAU KHI hoàn thành task đã được duyệt, như một gợi ý riêng
- Để user quyết định có làm hay không

```
✅ Đúng:
"Đã xong các thay đổi đã duyệt. Tôi cũng thấy X có thể cải thiện —
bạn muốn tôi lập kế hoạch riêng cho phần đó không?"

❌ Sai:
[Âm thầm sửa X trong lúc thực hiện task đã duyệt]
```

---

## Quy ước viết code

**Tất cả code, comment, docstring viết bằng tiếng Anh.**

Áp dụng cho:
- Tên biến, hàm, class, file, module
- Inline comment (`# ...`)
- Docstring (`"""..."""`)
- Log messages (`print`, `logger.info`, error message trong exception)
- Commit messages

Ngoại lệ — VẪN tiếng Việt:
- String hiển thị cho user (tin nhắn Telegram, Discord, web UI reply)
- Constants/identifiers map trực tiếp với user input tiếng Việt (vd `PREFIX_GHI_NHO_VAO`, `EXACT_XEM_NHAT_KY`) — giữ nguyên để trace dễ về intent
- Tài liệu tiếng Việt (README, ADR, design notes)
- CLAUDE.md và các file workflow

Khi sửa file cũ có comment tiếng Việt: dịch sang tiếng Anh trong cùng lần edit (không tách 2 pass).
