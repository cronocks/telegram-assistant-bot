"""Tests for permissions.py — role helpers and age utilities."""
from datetime import date

import pytest

from interfaces import User
from permissions import age_of, can_manage, has_role, is_adult, role_rank


def _user(role: str, birthdate: date | None = None) -> User:
    return User(id=1, name="Test", role=role, birthdate=birthdate)


class TestRoleRank:
    def test_admin_highest(self):
        assert role_rank("admin") > role_rank("manager")
        assert role_rank("manager") > role_rank("member")
        assert role_rank("member") > role_rank("readonly")

    def test_unknown_role_returns_minus_one(self):
        assert role_rank("superuser") == -1


class TestHasRole:
    def test_exact_match(self):
        assert has_role(_user("admin"), "admin")
        assert has_role(_user("member"), "member")

    def test_multiple_roles(self):
        assert has_role(_user("manager"), "admin", "manager")
        assert not has_role(_user("readonly"), "admin", "manager")

    def test_no_match(self):
        assert not has_role(_user("member"), "admin")


class TestCanManage:
    def test_admin_can_manage(self):
        assert can_manage(_user("admin"))

    def test_manager_can_manage(self):
        assert can_manage(_user("manager"))

    def test_member_cannot_manage(self):
        assert not can_manage(_user("member"))

    def test_readonly_cannot_manage(self):
        assert not can_manage(_user("readonly"))


class TestAgeOf:
    def test_no_birthdate_returns_minus_one(self):
        assert age_of(_user("member", birthdate=None)) == -1

    def test_exactly_18(self):
        today = date(2026, 5, 15)
        bd = date(2008, 5, 15)
        assert age_of(_user("member", bd), today) == 18

    def test_one_day_before_18th_birthday(self):
        today = date(2026, 5, 14)
        bd = date(2008, 5, 15)
        assert age_of(_user("member", bd), today) == 17

    def test_over_18(self):
        today = date(2026, 5, 15)
        bd = date(1990, 1, 1)
        assert age_of(_user("member", bd), today) >= 36

    def test_under_18(self):
        today = date(2026, 5, 15)
        bd = date(2012, 6, 1)
        assert age_of(_user("member", bd), today) < 18


class TestIsAdult:
    def test_adult(self):
        today = date(2026, 5, 15)
        bd = date(2008, 5, 15)
        assert is_adult(_user("member", bd), today) is True

    def test_not_adult(self):
        today = date(2026, 5, 15)
        bd = date(2010, 5, 15)
        assert is_adult(_user("member", bd), today) is False

    def test_unknown_birthdate_is_not_adult(self):
        assert is_adult(_user("member", None)) is False
