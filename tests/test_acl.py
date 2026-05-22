"""Tests for acl.py — can_read() and filter_visible(), FR-3 baseline + FR-4 stealth-read."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import acl
from interfaces import User


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(user_id: int, role: str = "member") -> User:
    return User(id=user_id, name=f"User{user_id}", role=role)


def _today_minus_years(years: int) -> date:
    """Return today minus N years (handles leap years by shifting Feb 29 to Mar 1)."""
    today = date.today()
    try:
        return today.replace(year=today.year - years)
    except ValueError:
        # Feb 29 in a non-leap year → use Feb 28
        return today.replace(year=today.year - years, day=28)


# ─────────────────────────────────────────────────────────────────────────────
# FR-3 baseline behavior (preserved when user_store is None)
# ─────────────────────────────────────────────────────────────────────────────

class TestCanRead:
    # scope='everyone' → readable by anyone regardless of role or ownership
    def test_everyone_scope_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "everyone", owner_user_id=1) == (True, False)

    def test_everyone_scope_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "everyone", owner_user_id=2) == (True, False)

    def test_everyone_scope_non_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "everyone", owner_user_id=99) == (True, False)

    def test_everyone_scope_non_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "everyone", owner_user_id=99) == (True, False)

    def test_everyone_scope_non_owner_manager(self):
        u = _user(3, "manager")
        assert acl.can_read(u, "everyone", owner_user_id=99) == (True, False)

    def test_everyone_scope_non_owner_readonly(self):
        u = _user(4, "readonly")
        assert acl.can_read(u, "everyone", owner_user_id=99) == (True, False)

    # scope='private' → only owner can read (no user_store → no stealth)
    def test_private_scope_owner_admin(self):
        u = _user(1, "admin")
        assert acl.can_read(u, "private", owner_user_id=1) == (True, False)

    def test_private_scope_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "private", owner_user_id=2) == (True, False)

    def test_private_scope_non_owner_admin_no_user_store(self):
        # FR-3 behavior: without user_store, admin cannot bypass private.
        u = _user(1, "admin")
        assert acl.can_read(u, "private", owner_user_id=99) == (False, False)

    def test_private_scope_non_owner_manager(self):
        u = _user(3, "manager")
        assert acl.can_read(u, "private", owner_user_id=99) == (False, False)

    def test_private_scope_non_owner_member(self):
        u = _user(2, "member")
        assert acl.can_read(u, "private", owner_user_id=99) == (False, False)

    def test_private_scope_non_owner_readonly(self):
        u = _user(4, "readonly")
        assert acl.can_read(u, "private", owner_user_id=99) == (False, False)

    # note_shares (reserved, FR-4+)
    def test_private_scope_explicit_share_grants_access(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[5, 6]) == (True, False)

    def test_private_scope_not_in_shares(self):
        u = _user(7, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[5, 6]) == (False, False)

    def test_private_scope_empty_shares(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=[]) == (False, False)

    def test_private_scope_none_shares(self):
        u = _user(5, "member")
        assert acl.can_read(u, "private", owner_user_id=99, note_shares=None) == (False, False)


# ─────────────────────────────────────────────────────────────────────────────
# FR-4 stealth-read — viewer-role × owner-status matrix
# ─────────────────────────────────────────────────────────────────────────────


class TestStealthReadMatrix:
    """Validate the (allowed, is_stealth) tuple across role/owner combinations."""

    def test_admin_reads_minor_child_with_parent(self, store, sample_admin, member_user):
        """Canonical stealth case: admin + child <18 + parent_link."""
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            sample_admin, "private", owner_user_id=child.id, user_store=store,
        ) == (True, True)

    def test_admin_blocked_when_child_turns_18_today(self, store, sample_admin, member_user):
        """Sinh nhật đúng hôm nay — đã tròn 18 → out of stealth."""
        child = store.create_user(name="Kid18", role="member", birthdate=_today_minus_years(18))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            sample_admin, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_admin_blocked_for_adult_child_with_parent(self, store, sample_admin, member_user):
        """Con lớn (25 tuổi) còn parent_link cũng không stealth."""
        child = store.create_user(name="Adult", role="member", birthdate=_today_minus_years(25))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            sample_admin, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_admin_blocked_for_adult_no_parent(self, store, sample_admin):
        """Adult không có parent_link → không stealth."""
        adult = store.create_user(name="Solo", role="member", birthdate=_today_minus_years(30))

        assert acl.can_read(
            sample_admin, "private", owner_user_id=adult.id, user_store=store,
        ) == (False, False)

    def test_admin_blocked_for_minor_without_parent_link(self, store, sample_admin):
        """D3 yêu cầu BOTH age<18 AND parent_link."""
        child = store.create_user(name="Orphan", role="member", birthdate=_today_minus_years(10))
        # Intentionally no set_parent call.

        assert acl.can_read(
            sample_admin, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_admin_blocked_when_birthdate_missing(self, store, sample_admin, member_user):
        """Không có birthdate → không xác định tuổi → không stealth."""
        child = store.create_user(name="UnknownAge", role="member", birthdate=None)
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            sample_admin, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_admin_blocked_for_unknown_user(self, store, sample_admin):
        """User id không tồn tại → defensive False."""
        assert acl.can_read(
            sample_admin, "private", owner_user_id=99999, user_store=store,
        ) == (False, False)

    def test_manager_blocked_from_stealth(self, store, sample_admin, member_user):
        """Chỉ admin được stealth; manager không được."""
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)
        manager = store.create_user(name="Mgr", role="manager")

        assert acl.can_read(
            manager, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_member_blocked_from_stealth(self, store, sample_admin, member_user):
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            member_user, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_readonly_blocked_from_stealth(self, store, sample_admin, member_user):
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)
        ro = store.create_user(name="Guest", role="readonly")

        assert acl.can_read(
            ro, "private", owner_user_id=child.id, user_store=store,
        ) == (False, False)

    def test_self_read_not_marked_stealth(self, store, sample_admin, member_user):
        """Nếu viewer chính là chủ thì không phải stealth dù mọi điều kiện stealth khớp."""
        # Make sample_admin themselves a minor (theoretical) and a child.
        # We use a different admin who is also a registered minor child.
        admin_kid = store.create_user(name="MiniAdmin", role="admin", birthdate=_today_minus_years(10))
        store.set_parent(user_id=admin_kid.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            admin_kid, "private", owner_user_id=admin_kid.id, user_store=store,
        ) == (True, False)

    def test_admin_reads_minor_child_everyone_scope_not_stealth(
        self, store, sample_admin, member_user,
    ):
        """Scope=everyone không bao giờ qua đường stealth."""
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        assert acl.can_read(
            sample_admin, "everyone", owner_user_id=child.id, user_store=store,
        ) == (True, False)


# ─────────────────────────────────────────────────────────────────────────────
# FR-4 stealth-read — boundary tests around the 18th birthday
# ─────────────────────────────────────────────────────────────────────────────


class TestStealthReadBoundary:

    def _child_with_birthdate(self, store, parent_user, bd: date, name: str = "Kid"):
        kid = store.create_user(name=name, role="member", birthdate=bd)
        store.set_parent(user_id=kid.id, parent_id=parent_user.id, set_by=parent_user.id)
        return kid

    def test_birthday_yesterday_already_18(self, store, sample_admin, member_user):
        bd = date.today() - timedelta(days=1)
        bd = bd.replace(year=bd.year - 18)
        kid = self._child_with_birthdate(store, member_user, bd, "YesterdayKid")
        allowed, is_stealth = acl.can_read(
            sample_admin, "private", owner_user_id=kid.id, user_store=store,
        )
        assert (allowed, is_stealth) == (False, False)

    def test_birthday_tomorrow_still_17(self, store, sample_admin, member_user):
        bd = date.today() + timedelta(days=1)
        bd = bd.replace(year=bd.year - 18)
        kid = self._child_with_birthdate(store, member_user, bd, "TomorrowKid")
        allowed, is_stealth = acl.can_read(
            sample_admin, "private", owner_user_id=kid.id, user_store=store,
        )
        assert (allowed, is_stealth) == (True, True)

    def test_birthday_in_a_month_still_17(self, store, sample_admin, member_user):
        # Choose a future month/day clearly past today.
        today = date.today()
        # Pick something ~30 days ahead, then push 18 years back.
        bd_future = today + timedelta(days=30)
        bd = bd_future.replace(year=bd_future.year - 18)
        kid = self._child_with_birthdate(store, member_user, bd, "NextMonthKid")
        allowed, is_stealth = acl.can_read(
            sample_admin, "private", owner_user_id=kid.id, user_store=store,
        )
        assert (allowed, is_stealth) == (True, True)

    def test_birthday_passed_earlier_this_year(self, store, sample_admin, member_user):
        today = date.today()
        bd_past = today - timedelta(days=30)
        bd = bd_past.replace(year=bd_past.year - 18)
        kid = self._child_with_birthdate(store, member_user, bd, "PassedKid")
        allowed, is_stealth = acl.can_read(
            sample_admin, "private", owner_user_id=kid.id, user_store=store,
        )
        assert (allowed, is_stealth) == (False, False)

    def test_age_helper_handles_feb29_birthday(self):
        # Internal helper smoke: born 2008-02-29, evaluated on 2026-02-28.
        from acl import _age_in_years

        bd = date(2008, 2, 29)
        # Day before 18th: still 17 because (2, 28) < (2, 29).
        assert _age_in_years(bd, date(2026, 2, 28)) == 17
        # On Mar 1: turned 18 (in our convention).
        assert _age_in_years(bd, date(2026, 3, 1)) == 18

    def test_age_helper_basic(self):
        from acl import _age_in_years

        # Birthday already happened this year.
        assert _age_in_years(date(2000, 1, 1), date(2026, 5, 21)) == 26
        # Birthday hasn't happened yet this year.
        assert _age_in_years(date(2000, 12, 31), date(2026, 5, 21)) == 25


# ─────────────────────────────────────────────────────────────────────────────
# Backward compatibility — user_store=None disables stealth
# ─────────────────────────────────────────────────────────────────────────────


class TestStealthReadBackwardCompat:

    def test_no_user_store_means_no_stealth_for_admin(self):
        admin = _user(1, "admin")
        # Even with admin + private + other-owner, no user_store → no stealth.
        assert acl.can_read(admin, "private", owner_user_id=99) == (False, False)

    def test_note_shares_short_circuits_before_stealth_check(self, store, sample_admin, member_user):
        """Explicit share grants access without invoking stealth path."""
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        # sample_admin would also qualify for stealth, but explicit share wins → is_stealth=False.
        result = acl.can_read(
            sample_admin, "private", owner_user_id=child.id,
            user_store=store, note_shares=[sample_admin.id],
        )
        assert result == (True, False)

    def test_user_store_none_with_admin_minor_owner_still_blocked(self):
        """Even semantically a minor, without user_store we can't verify → blocked."""
        admin = _user(1, "admin")
        # owner_user_id=99 doesn't exist anywhere, but user_store=None so we never look.
        assert acl.can_read(admin, "private", owner_user_id=99) == (False, False)


# ─────────────────────────────────────────────────────────────────────────────
# filter_visible — FR-3 baseline preserved + FR-4 stealth marking
# ─────────────────────────────────────────────────────────────────────────────


class TestFilterVisible:
    def test_empty_list_returns_empty(self):
        u = _user(1)
        assert acl.filter_visible(u, []) == []

    def test_everyone_scope_passes_through(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "everyone", "owner_user_id": 99}]
        result = acl.filter_visible(u, rows)
        assert len(result) == 1
        assert result[0]["drive_file_id"] == "f1"

    def test_private_owner_visible(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "private", "owner_user_id": 1}]
        result = acl.filter_visible(u, rows)
        assert len(result) == 1

    def test_private_non_owner_invisible(self):
        u = _user(2)
        rows = [{"drive_file_id": "f1", "scope": "private", "owner_user_id": 1}]
        assert acl.filter_visible(u, rows) == []

    def test_mixed_rows_filtered_correctly(self):
        u = _user(1)
        rows = [
            {"drive_file_id": "pub",    "scope": "everyone", "owner_user_id": 99},
            {"drive_file_id": "mine",   "scope": "private",  "owner_user_id": 1},
            {"drive_file_id": "theirs", "scope": "private",  "owner_user_id": 2},
        ]
        result = acl.filter_visible(u, rows)
        ids = {r["drive_file_id"] for r in result}
        assert ids == {"pub", "mine"}

    def test_row_missing_scope_excluded(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "owner_user_id": 1}]
        assert acl.filter_visible(u, rows) == []

    def test_row_missing_owner_excluded(self):
        u = _user(1)
        rows = [{"drive_file_id": "f1", "scope": "everyone"}]
        assert acl.filter_visible(u, rows) == []

    def test_all_private_non_owner_returns_empty(self):
        u = _user(5)
        rows = [
            {"drive_file_id": f"f{i}", "scope": "private", "owner_user_id": i}
            for i in range(1, 5)
        ]
        assert acl.filter_visible(u, rows) == []


class TestFilterVisibleStealth:
    """FR-4: filter_visible marks stealth-revealed rows with is_stealth_read=True."""

    def test_stealth_row_marked(self, store, sample_admin, member_user):
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        rows = [
            {"drive_file_id": "kid-priv", "scope": "private", "owner_user_id": child.id},
        ]
        result = acl.filter_visible(sample_admin, rows, user_store=store)
        assert len(result) == 1
        assert result[0]["is_stealth_read"] is True

    def test_no_user_store_means_no_stealth_key(self, store, sample_admin, member_user):
        """Without user_store, no row should pick up is_stealth_read."""
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        rows = [
            {"drive_file_id": "kid-priv", "scope": "private", "owner_user_id": child.id},
        ]
        # admin owns nothing here AND no user_store → row filtered out, none marked.
        result = acl.filter_visible(sample_admin, rows)
        assert result == []

    def test_own_private_row_not_marked_stealth(self, store, sample_admin, member_user):
        """Owner-self pass should not carry is_stealth_read."""
        # Make sample_admin a registered minor child (theoretical).
        admin_kid = store.create_user(name="MiniAdmin", role="admin", birthdate=_today_minus_years(10))
        store.set_parent(user_id=admin_kid.id, parent_id=member_user.id, set_by=sample_admin.id)

        rows = [
            {"drive_file_id": "self-priv", "scope": "private", "owner_user_id": admin_kid.id},
        ]
        result = acl.filter_visible(admin_kid, rows, user_store=store)
        assert len(result) == 1
        assert "is_stealth_read" not in result[0]

    def test_everyone_scope_not_marked_stealth(self, store, sample_admin, member_user):
        child = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=child.id, parent_id=member_user.id, set_by=sample_admin.id)

        rows = [
            {"drive_file_id": "pub", "scope": "everyone", "owner_user_id": child.id},
        ]
        result = acl.filter_visible(sample_admin, rows, user_store=store)
        assert len(result) == 1
        assert "is_stealth_read" not in result[0]

    def test_mixed_minor_and_adult_only_minor_marked(self, store, sample_admin, member_user):
        kid = store.create_user(name="Kid", role="member", birthdate=_today_minus_years(10))
        store.set_parent(user_id=kid.id, parent_id=member_user.id, set_by=sample_admin.id)
        adult = store.create_user(name="Adult", role="member", birthdate=_today_minus_years(30))
        store.set_parent(user_id=adult.id, parent_id=member_user.id, set_by=sample_admin.id)

        rows = [
            {"drive_file_id": "kid-priv",   "scope": "private", "owner_user_id": kid.id},
            {"drive_file_id": "adult-priv", "scope": "private", "owner_user_id": adult.id},
            {"drive_file_id": "pub",        "scope": "everyone", "owner_user_id": adult.id},
        ]
        result = acl.filter_visible(sample_admin, rows, user_store=store)
        by_id = {r["drive_file_id"]: r for r in result}

        # Adult private was blocked.
        assert "adult-priv" not in by_id
        # Kid private was admitted with stealth flag.
        assert by_id["kid-priv"]["is_stealth_read"] is True
        # Everyone-scope admitted without stealth flag.
        assert "is_stealth_read" not in by_id["pub"]
