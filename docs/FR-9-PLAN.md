# FR-9 — Expense Tracking (Ledger) — Implementation Plan

> **Status:** PENDING — branch `feature/FR9` (branched off `main`)
> **Approach:** TDD throughout — tests written before implementation in every sub-task.

---

## 1. Goal

FR-9 adds **personal & family expense tracking** with category classification, monthly budgets, savings targets, and weekly reports. The feature is built on the FR-4 notification infrastructure and reuses the FR-7 audit framework.

**In scope:**
- `categories` table — per-user CRUD with optional family-shared scope (`user_id IS NULL`).
- `ledger_entries` table — income/expense entries, amount stored as VND integer (Decision #26).
- `monthly_budgets` table — per-user expense budget + savings target per month.
- `ledger_parser` — fast-path Vietnamese keyword detection + LLM fallback (Haiku 4.5) for category classification.
- Telegram commands: thêm thu/chi, danh sách, xem, sửa, hủy, báo cáo, hạn mức.
- Web UI: 4 pages (list, form, monthly report, manage categories).
- Weekly summary scheduled job (Mon 08:00 VN) — reports previous week.
- Real-time threshold alerts (80% / 100% of monthly expense budget).
- Audit events for all CRUD + threshold breaches.

**Out of scope (deferred):**
- Multi-currency (VND only v1).
- Shared family wallet (per-user wallet only; family-shared *categories* are in scope, but entries stay per-user).
- Budget alerts via dedicated channel (uses existing notification flow).
- Receipt OCR / image input.
- Recurring entries (vd lương hàng tháng tự động) — manual entry v1.

---

## 2. Key Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| G1 | Separate stores per concern (`LedgerStore`, `CategoryStore`, `BudgetStore`) | Single Responsibility — easier to test in isolation; matches existing pattern (`task_store`, `reminder_store`) |
| G2 | Amount stored as INTEGER VND, never FLOAT | Decision #26 — avoid float precision loss for money |
| G3 | Fast-path keyword + LLM fallback for category | Most entries use clear keywords (`chi:`, `thu:`); LLM only when keyword absent — minimizes quota cost |
| G4 | LLM category classification counts user quota | Consistent with `task_parser` (FR-7); user sees quota usage transparently |
| G5 | Family-shared categories via `user_id IS NULL` | Schema in ROADMAP supports it; only admin/manager can create shared categories (default member can only create personal) |
| G6 | Seed default categories on first user entry | Lazy seed — when user runs first ledger command, insert 8 expense + 3 income categories tagged to that user; family-shared seed is opt-in via admin |
| G7 | Soft-delete via `voided_at` + 30-day auto-purge | Consistent with FR-4 recycle bin pattern but with shorter retention (financial data churns faster than notes) |
| G8 | Single command for set + edit budget (UPSERT) | `dat han muc chi: <số>` upserts row for current month — no separate "edit" command |
| G9 | Savings target = derived from (income − expense) | User sets target number only; actual savings auto-computed — no "transfer to savings account" concept needed |
| G10 | Weekly report scope: previous full week (Mon→Sun) | Sent Mon 08:00 VN; covers a complete bounded period (not partial current week) |
| G11 | Threshold alerts (80% / 100%) fire once per threshold per month | Stored in `monthly_budgets.alerts_sent` JSON column to avoid re-spam |
| G12 | VND formatting: `50.000` display, flexible input | Accepts `50000`, `50k`, `50.000`, `50,000`, `50tr`, `5m` (suffix-based shortcuts) |

---

## 3. File Layout

### New files

| File | Purpose |
|------|---------|
| `db/migrations/025_categories.sql` | `categories` table |
| `db/migrations/026_ledger_entries.sql` | `ledger_entries` table |
| `db/migrations/027_monthly_budgets.sql` | `monthly_budgets` table |
| `category_store.py` | `SqliteCategoryStore` — CRUD + family-shared scope |
| `ledger_store.py` | `SqliteLedgerStore` — entry CRUD + monthly aggregates + 7-day query |
| `budget_store.py` | `SqliteBudgetStore` — upsert by `(user_id, month)`, threshold alert state |
| `ledger_parser.py` | `LedgerParser` — amount parsing (k/tr/m suffix) + category classification (fast-path + LLM) |
| `ledger_reports.py` | `LedgerReports` — monthly summary, yearly breakdown, weekly report, threshold check |
| `cmd_ledger.py` | Telegram command handlers (12 commands) |
| `templates/ledger.html` | List view with month filter |
| `templates/ledger_form.html` | Create + edit form |
| `templates/ledger_view.html` | Detail view |
| `templates/ledger_report.html` | Monthly report (totals, by category, budget progress) |
| `templates/ledger_categories.html` | Category management |
| `templates/ledger_budget.html` | Set budget + savings target form |
| `tests/test_category_store.py` | 12 tests — category CRUD + family-shared |
| `tests/test_ledger_store.py` | 18 tests — entry CRUD + soft-delete + aggregates |
| `tests/test_budget_store.py` | 10 tests — upsert + threshold state |
| `tests/test_ledger_parser.py` | 20 tests — amount parsing + fast-path keywords + LLM fallback |
| `tests/test_ledger_reports.py` | 15 tests — monthly, yearly, weekly aggregates + threshold logic |
| `tests/test_ledger_handlers.py` | 30 tests — all 12 commands + edge cases |
| `tests/test_ledger_web.py` | 18 tests — web routes + auth + ownership |

### Edited files

| File | Change |
|------|--------|
| `deps.py` | + `category_store`, `ledger_store`, `budget_store`, `ledger_parser`, `ledger_reports` on `CoreDeps` |
| `main.py` | Instantiate 5 new components; wire into both `deps` and `web_deps` |
| `web_router.py` | + 13 routes `/ledger/*` |
| `scheduled_jobs.py` | + `send_weekly_summary` (Mon 08:00 VN) + `purge_voided_ledger_30d` (daily 03:00 VN) |
| `core_handler.py` | + 12 commands dispatch, `/help ghi chep` group, `/start` menu entry |

---

## 4. Database Schema

### `categories` (migration 025)
```sql
CREATE TABLE IF NOT EXISTS categories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER REFERENCES users(id),                -- NULL = family-shared
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL,                            -- 'income' | 'expense'
    parent_id    INTEGER REFERENCES categories(id),           -- nested categories (v1: unused, schema-ready)
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    deleted_at   TEXT
);

CREATE INDEX idx_categories_user ON categories(user_id) WHERE deleted_at IS NULL;
CREATE INDEX idx_categories_kind ON categories(kind) WHERE deleted_at IS NULL;
```

### `ledger_entries` (migration 026)
```sql
CREATE TABLE IF NOT EXISTS ledger_entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT    NOT NULL,                           -- 'income' | 'expense'
    amount        INTEGER NOT NULL,                           -- VND, never FLOAT
    category_id   INTEGER REFERENCES categories(id),
    note          TEXT,                                       -- raw user description
    occurred_at   TEXT    NOT NULL,                           -- ISO datetime VN
    source        TEXT    NOT NULL,                           -- 'telegram' | 'web'
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    voided_at     TEXT                                        -- soft-delete
);

CREATE INDEX idx_ledger_user_occurred ON ledger_entries(user_id, occurred_at DESC)
    WHERE voided_at IS NULL;
CREATE INDEX idx_ledger_user_cat_occurred ON ledger_entries(user_id, category_id, occurred_at DESC)
    WHERE voided_at IS NULL;
```

### `monthly_budgets` (migration 027)
```sql
CREATE TABLE IF NOT EXISTS monthly_budgets (
    user_id          INTEGER NOT NULL REFERENCES users(id),
    month            TEXT    NOT NULL,                        -- 'YYYY-MM'
    expense_budget   INTEGER,                                 -- VND; NULL = not set
    savings_target   INTEGER,                                 -- VND; NULL = not set
    alerts_sent      TEXT    NOT NULL DEFAULT '[]',           -- JSON array: e.g. ["80", "100"]
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    PRIMARY KEY (user_id, month)
);
```

---

## 5. Telegram Command Spec

All ledger commands are **quota-exempt for fast-path** (keyword present); fast-path can route to LLM for category classification when description is ambiguous — that LLM call counts quota.

### Ghi chép (entries)

| Command | Description | Quota |
|---------|-------------|-------|
| `chi: <số> <mô tả>` | Quick expense. Amount: `50k`, `50.000`, `5tr`, `5m` all work | exempt (fast-path) / LLM if category unclear |
| `thu: <số> <mô tả>` | Quick income | exempt / LLM |
| `ghi chep: <id>` | View entry detail | exempt |
| `danh sach ghi chep` | List recent 20 entries | exempt |
| `sua ghi chep: <id>, so=…, mo ta=…, danh muc=…` | Edit entry | exempt |
| `huy ghi chep: <id>` | Soft-delete (sets `voided_at`) | exempt |

### Danh mục (categories)

| Command | Description | Who |
|---------|-------------|-----|
| `xem danh muc` | List user's categories + family-shared ones | any |
| `them danh muc: <tên>, chi\|thu[, chung]` | Add category. `chung` = family-shared (admin/manager only) | any (chung needs admin/manager) |
| `xoa danh muc: <id>` | Soft-delete (entries keep ref but category shows "(đã xóa)") | owner or admin |
| `sua danh muc: <id> <tên mới>` | Rename | owner or admin |

### Báo cáo (reports)

| Command | Description |
|---------|-------------|
| `bao cao thang` | Current month summary: total income, expense, savings; breakdown by category; budget progress |
| `bao cao thang <YYYY-MM>` | Specific month |
| `bao cao nam` | Yearly: month-by-month income/expense rows |
| `xem chi tieu` | Last 7 days: daily totals + running sum |

### Hạn mức (budget)

| Command | Description |
|---------|-------------|
| `dat han muc chi: <số>` | UPSERT `expense_budget` for current month |
| `dat muc tieu tiet kiem: <số>` | UPSERT `savings_target` for current month |
| `xem han muc` | Show current month budget + savings target + progress |

---

## 6. Web Routes Spec

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/ledger` | List entries (filter `?month=YYYY-MM`, `?kind=expense\|income`, `?category_id=N`) |
| GET | `/ledger/new` | Create form |
| POST | `/ledger` | Create entry — redirect to detail |
| GET | `/ledger/{id}` | Detail view |
| GET | `/ledger/{id}/edit` | Edit form |
| POST | `/ledger/{id}` | Update |
| POST | `/ledger/{id}/void` | Soft-delete |
| GET | `/ledger/categories` | Manage categories |
| POST | `/ledger/categories` | Create category |
| POST | `/ledger/categories/{id}` | Update / delete |
| GET | `/ledger/report` | Monthly report (default current month) |
| GET | `/ledger/report?month=YYYY-MM` | Specific month |
| GET | `/ledger/budget` | View + edit budget form |
| POST | `/ledger/budget` | UPSERT current month budget + savings target |

Ownership enforced via `row.user_id == user.id`; admin stealth-read extended for under-18 owners (consistent with FR-4 Decision #62). Family-shared categories visible to all.

---

## 7. Parser Spec — `ledger_parser.py`

### Amount parsing (deterministic)

Input forms accepted:
| Input | Parsed VND |
|-------|------------|
| `50000` | 50,000 |
| `50.000` | 50,000 |
| `50,000` | 50,000 |
| `50k` | 50,000 |
| `1.5tr` | 1,500,000 |
| `2tr` | 2,000,000 |
| `2m` | 2,000,000 (m = million synonym) |

Regex: `^([\d.,]+)\s*(k|tr|m)?$` after whitespace strip; suffix multipliers `k=1e3`, `tr=1e6`, `m=1e6`.

### Category classification (2-tier)

**Tier 1 — Fast-path keyword lookup:**
- Match category by name fuzzy contains in description (vd `ăn trưa` → "Ăn uống").
- If exactly one category matches → use it, no LLM call.

**Tier 2 — LLM fallback (Haiku 4.5):**
- When 0 or >1 categories match → LLM tool-use call with user's categories as enum.
- Returns `category_id` + `confidence`. If confidence < 0.6 → fallback to "Khác" (Other).
- Costs counted via `cost_monitor` consistent with `task_parser`.

---

## 8. Reports & Weekly Summary

### `bao cao thang` payload structure

```
📊 Tháng 6/2026
─────────────────────
💰 Thu:    8.500.000
💸 Chi:    6.200.000
💵 Tiết kiệm: 2.300.000 / 5.000.000 (46%)

Hạn mức chi: 6.200.000 / 10.000.000 (62%) ✅

Theo danh mục (chi):
  Ăn uống      2.100.000 (34%)
  Đi lại         800.000 (13%)
  Hóa đơn      1.500.000 (24%)
  Mua sắm      1.200.000 (19%)
  Khác           600.000 (10%)
```

### Weekly summary (Mon 08:00 VN)

Covers previous Mon→Sun. For each user with at least 1 entry in window:

```
📊 Tổng kết tuần (08/06 — 14/06)
─────────────────────
💸 Chi tuần: 2.300.000
   Tháng: 4.800.000 / 10.000.000 (48%) — còn 17 ngày
💰 Tiết kiệm hiện tại: 1.200.000 / 5.000.000 (24%)
   ⚠ Tốc độ hiện tại: dự kiến đạt ~70% mục tiêu
```

### Threshold alert logic

After every INSERT into `ledger_entries` (kind='expense'):
1. Compute `month_expense_sum`.
2. Read `monthly_budgets.expense_budget` + `alerts_sent` for current month.
3. If budget not set → skip.
4. If `month_expense_sum / budget >= 1.0` and `"100"` not in alerts → enqueue alert + append `"100"` to alerts_sent.
5. Else if `>= 0.8` and `"80"` not in alerts → enqueue alert + append `"80"`.

Alerts go through `NotificationService.enqueue()` — survive crash.

---

## 9. Audit Events Added

| `action` | `target_type` | When |
|---|---|---|
| `ledger_created` | `ledger_entry` | New entry via Telegram or web |
| `ledger_updated` | `ledger_entry` | Edit entry |
| `ledger_voided` | `ledger_entry` | Soft-delete |
| `ledger_restored` | `ledger_entry` | Admin restores from recycle bin |
| `ledger_purged` | `ledger_entry` | Auto-purge after 30d (`voided_at` < now - 30d) |
| `category_created` | `category` | New category |
| `category_updated` | `category` | Rename |
| `category_deleted` | `category` | Soft-delete |
| `budget_set` | `user` | UPSERT `expense_budget` or `savings_target`; payload distinguishes which |
| `budget_threshold_alert` | `user` | 80% or 100% threshold breached |
| `weekly_summary_sent` | `user` | Weekly summary delivered |

---

## 10. TDD Workflow

Every sub-task follows strict Red → Green → Refactor cycle from `CLAUDE.md`:

**Implementation order (one feature unit at a time):**
1. Migration 025 → `category_store.py` (CRUD + family-shared)
2. Migration 026 → `ledger_store.py` (CRUD + aggregates)
3. Migration 027 → `budget_store.py` (UPSERT + alerts state)
4. `ledger_parser.py` — amount parser first (deterministic, easy), then fast-path keyword, then LLM fallback
5. `ledger_reports.py` — monthly summary, then weekly, then threshold check
6. `cmd_ledger.py` — entry commands first (chi/thu/danh sach/xem), then categories, then reports, then budget
7. Web routes — list/CRUD first, then report, then budget
8. Scheduled jobs — `purge_voided_ledger_30d` (simple), then `send_weekly_summary` (complex)
9. Wiring in `deps.py`, `main.py`, `web_router.py`, `core_handler.py`, `scheduled_jobs.py`

**Test target:** ~123 new tests; full suite must remain green.

---

## 11. Rollout & Migration

- No data migration — new tables only.
- No new external dependencies (no new pip package).
- Feature is additive — no FR-7/FR-8 behaviour changes.
- Rollback: revert migrations 025/026/027 + remove cmd handlers, restart. Data lost but no other impact.
- Seed default categories: lazy — first time user invokes any ledger command, if user has zero categories → seed 8 expense + 3 income tagged to that user.

**Default seed categories:**

Expense (8): `Ăn uống`, `Đi lại`, `Hóa đơn`, `Mua sắm`, `Sức khỏe`, `Giải trí`, `Học`, `Khác`
Income (3): `Lương`, `Quà`, `Khác`

---

## 12. Future Work

- **FR-9.5:** Shared family wallet — entries with `scope='family'`, aggregated reports.
- **FR-9.6:** Recurring entries — lương hàng tháng tự động ghi.
- **FR-9.7:** Receipt OCR — upload image, LLM extracts amount + category.
- **FR-9.8:** Per-month budget templates — set defaults across all months.
- **FR-9.9:** Budget alerts integrated with reminder system (FR-7) for proactive end-of-month notifications.

---

**End of plan.**
