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
| # | Action | File | Type | Mô tả |
|---|--------|------|------|-------|
| 1 | `CREATE` / `EDIT` / `DELETE` / `RENAME` | `tests/test_xyz.py` | `TEST` | Test cases cho... |
| 2 | `CREATE` / `EDIT` / `DELETE` / `RENAME` | `path/to/impl.py` | `IMPL` | Implementation cho... |

Quy tắc cột **Type**:
- `TEST` — file test (`tests/test_*.py`), phải được liệt kê **trước** file `IMPL` tương ứng
- `IMPL` — source code implementation
- `DOCS` / `CONFIG` — tài liệu, config (không áp dụng TDD cycle)

**Test Strategy:** [Mô tả ngắn: sẽ test những behavior nào, happy path + edge cases chính]

**Approach:** [2-4 câu giải thích hướng kỹ thuật, quyết định chính, trade-off nếu có]

**Risk:** `low` / `medium` / `high`
[1 câu giải thích mức rủi ro]

**Dependencies:** [Liệt kê những gì cần làm trước — migration, env vars, package install — hoặc ghi "None"]

⏸ **Awaiting your approval. Reply "go" to proceed, or share feedback to adjust the plan.**

---

### PHASE 3 — THỰC THI (TDD Cycle)

**Bước 0 — Kiểm tra branch (bắt buộc trước mọi thay đổi file):**
Chạy `git branch --show-current`. Nếu không phải `feature/*` → **dừng lại**, nhắc user:
> "Bạn đang ở branch `<tên branch>`. Hãy chuyển về feature branch trước khi tôi thực hiện thay đổi."
Chỉ tiếp tục sau khi user đã chuyển sang đúng branch.

Chỉ bắt đầu sau khi user xác nhận rõ ràng bằng một trong các từ:
- "go", "proceed", "approved", "ok", "yes", "looks good", "ship it"
- Hoặc tiếng Việt: "được", "ok", "tiến hành", "đồng ý", "làm đi"

Nếu user góp ý thay vì xác nhận → chỉnh lại kế hoạch (quay Phase 2), không tự tiến hành.

Với mỗi feature unit (một hàm / một behavior), thực hiện đúng thứ tự:

**🔴 Red** — Viết test trước, chạy `pytest` xác nhận test FAIL vì đúng lý do (chưa có implementation), không phải lỗi import hay syntax.

**🟢 Green** — Viết implementation tối thiểu đủ để test PASS, chạy `pytest` xác nhận PASS.

**🔵 Refactor** — Dọn code nếu cần (tên biến, trùng lặp, độ rõ ràng), chạy lại `pytest` xác nhận vẫn PASS.

Sau đó lặp lại chu kỳ cho feature unit tiếp theo.

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

## TDD Rules — Không được vi phạm

**Thứ tự bắt buộc:**
- Không viết bất kỳ dòng implementation nào trước khi có test tương ứng
- Không skip bước 🔴 Red — test phải được chạy và FAIL trước khi viết code
- Không gộp nhiều feature units vào một lần Red→Green→Refactor — làm từng unit một

**Test phải fail đúng lý do:**
- FAIL vì `AttributeError` / `ImportError` (chưa có hàm/module) → **chưa đủ**, phải tạo stub trước
- FAIL vì assertion sai kết quả → **đúng**, mới được chuyển sang Green
- Nếu test PASS ngay lần đầu (chưa viết impl) → test đang test sai thứ, phải xem lại

**Stub trước khi test:**
Nếu module/hàm chưa tồn tại, tạo stub rỗng (hàm trả về `None` hoặc `raise NotImplementedError`) trước khi chạy Red, để lỗi là assertion chứ không phải import.

**Khi nào KHÔNG áp dụng TDD cycle:**
- Thay đổi type `DOCS` hoặc `CONFIG` thuần túy
- Sửa lỗi cực nhỏ (typo trong string hiển thị, sửa 1 constant)
- Viết test cho code legacy đã có sẵn (test-after là chấp nhận được cho legacy)

**Báo cáo TDD sau mỗi unit:**
```
🔴 Red:   pytest tests/test_xyz.py::test_abc → FAILED (AssertionError: ...)
🟢 Green: pytest tests/test_xyz.py::test_abc → PASSED
🔵 Refactor: [mô tả thay đổi nếu có] → PASSED
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

### Shell content — English only, không ngoại lệ

Mọi nội dung sẽ đi vào terminal (git bash, PowerShell, etc.) **bắt buộc dùng English**, kể cả comment trong lệnh:

- Commit messages (`git commit -m "..."`)
- Inline comment trong lệnh bash (`# do this`, `# verify result`)
- Content của shell script (`.sh`, `.ps1`, `.bat`)
- **Lệnh bash gợi ý trong chat reply** mà Claude đưa cho user copy-paste (vd block ```bash``` chứa `git add`, `rm -rf`, ...)

**Lý do:** Git bash trên Windows không render Vietnamese diacritics tốt → vỡ font, khó đọc, dễ paste sai lệnh. Giữ shell content thuần ASCII English để tránh.

**Phân biệt rõ với chat text:** Khi Claude giải thích/trao đổi trong chat (user đọc trong Claude UI, không phải terminal) → vẫn dùng Vietnamese có dấu bình thường. Quy tắc này chỉ áp dụng cho nội dung mục tiêu là terminal.

**Ví dụ:**

✅ Đúng:
```bash
# Stage and commit doc changes
git add CLAUDE.md docs/ROADMAP.md
git commit -m "docs: add shell content English rule"
```

❌ Sai:
```bash
# Stage và commit các file doc đã đổi
git add CLAUDE.md docs/ROADMAP.md
git commit -m "docs: thêm quy ước English cho shell content"
```
