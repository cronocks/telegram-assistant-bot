"""tests/test_web_conversation_store.py — Unit tests for SqliteWebConversationStore (FR-5.5)."""
import time

import pytest

from web_conversation_store import SqliteWebConversationStore, _escape_like


@pytest.fixture()
def conv_store(db_conn):
    """SqliteWebConversationStore wired to the shared in-memory DB.

    Inserts seed user rows so the FK constraint on web_conversations.user_id
    is satisfied without depending on other store fixtures.
    """
    db_conn.executescript("""
        INSERT INTO users (id, name, role) VALUES (1, 'User1', 'member');
        INSERT INTO users (id, name, role) VALUES (2, 'User2', 'member');
        INSERT INTO users (id, name, role) VALUES (5, 'User5', 'member');
    """)
    db_conn.commit()
    s = SqliteWebConversationStore.__new__(SqliteWebConversationStore)
    s._conn = db_conn
    return s


# ── _escape_like helper ────────────────────────────────────────────────────────

class TestEscapeLike:
    def test_percent_is_escaped(self):
        assert _escape_like("50%") == r"50\%"

    def test_underscore_is_escaped(self):
        assert _escape_like("user_name") == r"user\_name"

    def test_backslash_is_escaped(self):
        assert _escape_like("a\\b") == r"a\\b"

    def test_plain_text_unchanged(self):
        assert _escape_like("hello world") == "hello world"


# ── create / get ───────────────────────────────────────────────────────────────

class TestCreateGet:
    def test_create_returns_int(self, conv_store):
        cid = conv_store.create(user_id=1)
        assert isinstance(cid, int) and cid > 0

    def test_create_row_exists(self, conv_store):
        cid = conv_store.create(user_id=1)
        row = conv_store.get(cid)
        assert row is not None
        assert row["user_id"] == 1
        assert row["title"] is None

    def test_get_returns_expected_keys(self, conv_store):
        cid = conv_store.create(user_id=1)
        row = conv_store.get(cid)
        assert set(row.keys()) >= {"id", "user_id", "title", "created_at", "updated_at"}

    def test_get_unknown_returns_none(self, conv_store):
        assert conv_store.get(99999) is None


# ── list_for_user ──────────────────────────────────────────────────────────────

class TestListForUser:
    def test_empty_for_new_user(self, conv_store):
        assert conv_store.list_for_user(999) == []

    def test_returns_own_conversations(self, conv_store):
        conv_store.create(user_id=1)
        conv_store.create(user_id=1)
        rows = conv_store.list_for_user(1)
        assert len(rows) == 2

    def test_isolates_per_user(self, conv_store):
        conv_store.create(user_id=1)
        conv_store.create(user_id=2)
        assert len(conv_store.list_for_user(1)) == 1
        assert len(conv_store.list_for_user(2)) == 1

    def test_ordered_by_updated_at_desc(self, conv_store):
        c1 = conv_store.create(user_id=1)
        time.sleep(0.01)
        c2 = conv_store.create(user_id=1)
        # Touch c1 to make it newer
        conv_store.add_message(c1, "user", "ping")
        rows = conv_store.list_for_user(1)
        assert rows[0]["id"] == c1


# ── rename / set_title_if_null ─────────────────────────────────────────────────

class TestRename:
    def test_rename_existing_returns_true(self, conv_store):
        cid = conv_store.create(user_id=1)
        assert conv_store.rename(cid, "New Title") is True

    def test_rename_updates_title(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.rename(cid, "New Title")
        assert conv_store.get(cid)["title"] == "New Title"

    def test_rename_unknown_returns_false(self, conv_store):
        assert conv_store.rename(99999, "Ghost") is False

    def test_rename_strips_whitespace(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.rename(cid, "  padded  ")
        assert conv_store.get(cid)["title"] == "padded"


class TestSetTitleIfNull:
    def test_sets_when_null_returns_true(self, conv_store):
        cid = conv_store.create(user_id=1)
        assert conv_store.set_title_if_null(cid, "Auto Title") is True
        assert conv_store.get(cid)["title"] == "Auto Title"

    def test_skips_when_already_set_returns_false(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.rename(cid, "Manual")
        assert conv_store.set_title_if_null(cid, "Auto") is False
        assert conv_store.get(cid)["title"] == "Manual"


# ── add_message / list_messages / count_messages ───────────────────────────────

class TestMessages:
    def test_add_message_returns_int(self, conv_store):
        cid = conv_store.create(user_id=1)
        mid = conv_store.add_message(cid, "user", "hello")
        assert isinstance(mid, int) and mid > 0

    def test_list_messages_empty(self, conv_store):
        cid = conv_store.create(user_id=1)
        assert conv_store.list_messages(cid) == []

    def test_list_messages_contains_added(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.add_message(cid, "user", "hi")
        conv_store.add_message(cid, "bot", "hello")
        msgs = conv_store.list_messages(cid)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "bot"

    def test_list_messages_chronological_order(self, conv_store):
        cid = conv_store.create(user_id=1)
        for word in ["a", "b", "c"]:
            conv_store.add_message(cid, "user", word)
        texts = [m["text"] for m in conv_store.list_messages(cid)]
        assert texts == ["a", "b", "c"]

    def test_count_messages_zero(self, conv_store):
        cid = conv_store.create(user_id=1)
        assert conv_store.count_messages(cid) == 0

    def test_count_messages_after_adds(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.add_message(cid, "user", "one")
        conv_store.add_message(cid, "bot", "two")
        assert conv_store.count_messages(cid) == 2

    def test_add_message_bumps_updated_at(self, conv_store):
        cid = conv_store.create(user_id=1)
        before = conv_store.get(cid)["updated_at"]
        time.sleep(0.01)
        conv_store.add_message(cid, "user", "ping")
        after = conv_store.get(cid)["updated_at"]
        assert after >= before


# ── search ─────────────────────────────────────────────────────────────────────

class TestSearch:
    def test_finds_matching_message(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.add_message(cid, "user", "python tutorial")
        results = conv_store.search(1, "python")
        assert len(results) == 1
        assert results[0]["conv_id"] == cid

    def test_no_match_returns_empty(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.add_message(cid, "user", "hello world")
        assert conv_store.search(1, "python") == []

    def test_like_metachar_percent_escaped(self, conv_store):
        cid = conv_store.create(user_id=1)
        conv_store.add_message(cid, "user", "50% discount")
        results = conv_store.search(1, "50%")
        assert len(results) == 1

    def test_isolates_per_user(self, conv_store):
        c1 = conv_store.create(user_id=1)
        conv_store.add_message(c1, "user", "secret user1")
        c2 = conv_store.create(user_id=2)
        conv_store.add_message(c2, "user", "secret user2")
        assert len(conv_store.search(1, "secret")) == 1
        assert len(conv_store.search(2, "secret")) == 1


# ── admin_list_for_user ────────────────────────────────────────────────────────

class TestAdminListForUser:
    def test_returns_all_convs_for_target(self, conv_store):
        conv_store.create(user_id=5)
        conv_store.create(user_id=5)
        rows = conv_store.admin_list_for_user(5)
        assert len(rows) == 2
        assert all(r["user_id"] == 5 for r in rows)

    def test_empty_when_no_convs(self, conv_store):
        assert conv_store.admin_list_for_user(999) == []
