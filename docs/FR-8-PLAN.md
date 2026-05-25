# FR-8 — Anniversary / Memorial Reminders — Implementation Plan

> **Status:** ✅ DONE — branch `feature/FR8`, 961 tests passing
> **Branch:** `feature/FR8` (branched off `main`)
> **Approach:** TDD throughout — tests written before implementation in every sub-task.

---

## 1. Goal

FR-8 adds **annual recurring reminders** for life events: giỗ (memorials), kỷ niệm cưới (anniversaries), and other yearly dates. Built on the FR-7 reminder infrastructure but with its own table + engine because:

1. Anniversaries store **raw lunar/solar month-day** and recompute the solar date each year (Decision #47).
2. Offsets are measured in **days** (30/15/7/3/1/0), not seconds.
3. The grace window is **12 hours** (anniversaries are day-scoped, not minute-scoped).

**In scope:**
- `anniversaries` table — CRUD by user.
- Lunar→solar conversion via `lunardate`.
- Annual compute job: scans all active anniversaries, inserts `anniversary_reminders` rows for the current year (idempotent).
- Tick job: scans pending rows every minute, fires due ones at 08:00 VN.
- Parent mirror (runtime under-18 check, consistent with Decision #22).
- Telegram commands: thêm, danh sách, xem, xoá, sửa.
- Web UI: full CRUD at `/anniversaries/*`.

**Out of scope (deferred):**
- Anniversary sharing with family members (private only in v1).
- Lunar leap-month support — `month` stored as 1–12, `isLeapMonth=False`.
- Voice / image input.

---

## 2. Key Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| F1 | Separate `AnniversaryEngine`, not extend `ReminderEngine` | Anniversary lifecycle differs: yearly compute step, day-based offsets, larger grace window. Both engines share `NotificationService`. |
| F2 | Lib `lunardate` (Chinese/Vietnamese share the same lunar system) | Pure Python, no native deps, well-tested. Fallback to day-1 when stored lunar day exceeds the month's actual length in a target year. |
| F3 | Store raw lunar/solar month-day, recompute solar each year | Single source of truth = original date; year-specific solar mapping varies. |
| F4 | Reminders fire at 08:00 VN (not configurable v1) | Early-morning heads-up fits anniversary semantics. |
| F5 | Grace window 12 hours (vs 1h for tasks) | Anniversary is day-scoped; 12h covers reasonable bot downtime without spamming days later. |
| F6 | Annual compute = startup + Jan 1st 00:05 VN + within-60-days seed for next year | Covers fresh deploys mid-year + new-year boundary + 30-day offsets that cross Jan 1. |
| F7 | Idempotency via `UNIQUE(anniversary_id, year, offset_days)` | Compute can run repeatedly without producing duplicates. |
| F8 | Simple regex parser for Telegram input (no LLM) | Date format `<name>, âm/dương DD/MM[, category]` is structured enough; LLM would be wasteful. |
| F9 | Default offsets `30,15,7,3,1,0` (days, CSV) | Matches ROADMAP scope; user-editable per anniversary. |
| F10 | Soft-delete via `deleted_at` (FR-4 recycle bin compatible) | Consistent with `tasks`, `notes`, `wiki_pages`. |

---

## 3. File Layout

### New files

| File | Purpose |
|------|---------|
| `db/migrations/022_anniversaries.sql` | `anniversaries` table |
| `db/migrations/023_anniversary_reminders.sql` | `anniversary_reminders` table with UNIQUE constraint |
| `anniversary_store.py` | `SqliteAnniversaryStore` — CRUD + validation |
| `lunar_utils.py` | `lunar_to_solar()` + `compute_anniversary_solar_date()` |
| `anniversary_engine.py` | `AnniversaryEngine.compute_year()`, `tick()`, `cancel_all_for_anniversary()` |
| `cmd_anniversary.py` | 5 Telegram command handlers + input parsing |
| `templates/anniversaries.html` | List view (extends `base.html`) |
| `templates/anniversary_form.html` | Create + edit form |
| `templates/anniversary_view.html` | Detail view |
| `tests/test_anniversary_store.py` | 20 tests — store CRUD + validation |
| `tests/test_lunar_utils.py` | 10 tests — conversion + edge cases (Feb 29, lunar short months) |
| `tests/test_anniversary_engine.py` | 13 tests — compute_year, tick, parent mirror, grace |
| `tests/test_anniversary_handlers.py` | 23 tests — parsing + dispatch flow |
| `tests/test_anniversary_web.py` | 14 tests — 7 routes + auth + ownership |

### Edited files

| File | Change |
|------|--------|
| `requirements.txt` | + `lunardate==0.2.2` |
| `deps.py` | + `anniversary_store`, `anniversary_engine` on `CoreDeps` |
| `main.py` | Instantiate `SqliteAnniversaryStore` + `AnniversaryEngine`; wire into both `deps` and `web_deps`; pass to `init_web_router` |
| `web_router.py` | + 7 routes `/anniversaries/*` |
| `scheduled_jobs.py` | + `anniversary_tick` (60s) + `compute_anniversary_year` (Jan 1 + startup) |
| `core_handler.py` | + 5 commands dispatch, `/help ky niem` group, `/start` menu entry |

---

## 4. Database Schema

### `anniversaries`
```sql
CREATE TABLE anniversaries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    name                TEXT    NOT NULL,
    date_type           TEXT    NOT NULL,                          -- 'lunar' | 'solar'
    month               INTEGER NOT NULL,                          -- 1-12
    day                 INTEGER NOT NULL,                          -- 1-30 (lunar) or 1-31 (solar)
    category            TEXT    NOT NULL DEFAULT 'khac',           -- 'gio' | 'cuoi' | 'khac'
    reminder_offsets    TEXT    NOT NULL DEFAULT '30,15,7,3,1,0',  -- CSV days before
    enabled             INTEGER NOT NULL DEFAULT 1,
    note                TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    deleted_at          TEXT
);
```

### `anniversary_reminders`
```sql
CREATE TABLE anniversary_reminders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    anniversary_id  INTEGER NOT NULL REFERENCES anniversaries(id),
    year            INTEGER NOT NULL,
    fire_at         TEXT    NOT NULL,                  -- ISO datetime, 08:00 VN
    offset_days     INTEGER NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending', -- 'pending' | 'fired' | 'missed' | 'cancelled'
    fired_at        TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE(anniversary_id, year, offset_days)
);
```

---

## 5. Telegram Command Spec

| Command | Quota-exempt | Description |
|---------|--------------|-------------|
| `them ky niem: <ten>, âm/dương DD/MM[, <loai>]` | ✅ | Add anniversary. Loai: gio / cuoi / khac. |
| `danh sach ky niem` | ✅ | List user's anniversaries. |
| `ky niem <id>` | ✅ | View detail. |
| `xoa ky niem: <id>` | ✅ | Soft-delete + cancel pending reminders. |
| `sua ky niem: <id>, ten=…, ngay=âm/dương DD/MM, loai=…, nhac=<csv>, bat/tat` | ✅ | Edit. |

All commands are quota-exempt (no LLM involved).

---

## 6. Web Routes Spec

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/anniversaries` | List user's anniversaries (HTML). |
| GET | `/anniversaries/new` | Create form. |
| POST | `/anniversaries` | Create — redirect to detail. |
| GET | `/anniversaries/{id}` | Detail view. |
| GET | `/anniversaries/{id}/edit` | Edit form. |
| POST | `/anniversaries/{id}` | Update. |
| POST | `/anniversaries/{id}/delete` | Soft-delete. |

Ownership enforced via `row.user_id == user.id` check; non-owners get 404.

---

## 7. TDD Workflow

Every sub-task followed the strict Red → Green → Refactor cycle from `CLAUDE.md`:

1. Stub the module (returns `None` / empty / 0).
2. Write test file with assertions.
3. Run `pytest` — confirm assertion failures (not import errors).
4. Implement the module.
5. Run `pytest` — confirm all pass.

Result: 80 new tests for FR-8, all green. No regressions (947 total tests pass).

---

## 8. Audit Events Added

| `action` | `target_type` | When |
|---|---|---|
| `anniversary_created` | `anniversary` | Telegram or web create |
| `anniversary_updated` | `anniversary` | Edit |
| `anniversary_deleted` | `anniversary` | Soft-delete |
| `anniversary_reminder_fired` | `anniversary` | Reminder delivered |
| `anniversary_reminder_missed` | `anniversary` | Past grace window (12h) |

---

## 9. Rollout & Migration

- No data migration — new tables only.
- New dependency: `lunardate==0.2.2` (pure Python, no native build).
- Feature is additive — no FR-7 behaviour changes.
- Rollback: revert migrations 022+023, restart. Data lost but no other impact.

---

## 10. Future Work

- **FR-8.5:** Anniversary sharing with family (scope=everyone).
- **FR-8.6:** Lunar leap-month support — when needed.
- **Family-wide anniversaries:** admin creates shared events visible to all members.
- **Configurable fire time:** per-user setting for anniversary morning ping.

---

**End of plan.**
