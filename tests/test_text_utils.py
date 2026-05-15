"""Tests for text_utils — normalize_vn and match_command."""
import pytest

from text_utils import match_command, normalize_vn


class TestNormalizeVn:
    def test_strips_tone_marks(self):
        assert normalize_vn("ghi nhớ") == "ghi nho"

    def test_strips_vowel_forms(self):
        assert normalize_vn("tuần") == "tuan"
        assert normalize_vn("nhật ký") == "nhat ky"

    def test_handles_d_with_stroke(self):
        assert normalize_vn("đăng ký") == "dang ky"
        assert normalize_vn("Đăng Ký") == "dang ky"

    def test_lowercases(self):
        assert normalize_vn("GHI NHO") == "ghi nho"
        assert normalize_vn("Wiki") == "wiki"

    def test_collapses_whitespace(self):
        assert normalize_vn("ghi  nho   vao") == "ghi nho vao"
        assert normalize_vn("  xem  ") == "xem"

    def test_empty_string(self):
        assert normalize_vn("") == ""

    def test_ascii_unchanged(self):
        assert normalize_vn("hello world") == "hello world"

    def test_mixed_accent_and_ascii(self):
        assert normalize_vn("tóm tắt tuần này") == "tom tat tuan nay"

    def test_uppercase_d_with_stroke(self):
        assert normalize_vn("Đường") == "duong"


class TestMatchCommand:
    CMDS = {
        "GHI_NHO_VAO": ["ghi nhớ vào ", "ghi nho vao "],
        "GHI_NHO":     ["ghi nhớ ", "ghi nho "],
        "XEM_WIKI":    ["xem wiki"],
        "XEM_WIKI_PAGE": ["xem wiki "],
        "XEM":         ["xem "],
        "LIET_KE":     ["liệt kê", "liet ke"],
        "TIM":         ["tìm ", "tim ", "search "],
    }

    def test_basic_match(self):
        result = match_command("ghi nho bai tho", self.CMDS)
        assert result is not None
        cmd, rem = result
        assert cmd == "GHI_NHO"
        assert rem == "bai tho"

    def test_longest_prefix_wins(self):
        # "ghi nhớ vào" must beat "ghi nhớ"
        result = match_command("ghi nhớ vào bai tho", self.CMDS)
        assert result is not None
        cmd, rem = result
        assert cmd == "GHI_NHO_VAO"
        assert rem == "bai tho"

    def test_diacritic_input_matches_ascii_prefix(self):
        # User types with diacritics, prefix defined without
        result = match_command("ghi nhớ vào nội dung", self.CMDS)
        assert result is not None
        assert result[0] == "GHI_NHO_VAO"

    def test_ascii_input_matches_diacritic_prefix(self):
        # User types without diacritics, prefix defined with
        result = match_command("ghi nho vao noi dung", self.CMDS)
        assert result is not None
        assert result[0] == "GHI_NHO_VAO"

    def test_remainder_preserves_original_diacritics(self):
        result = match_command("ghi nho nội dung quan trọng", self.CMDS)
        assert result is not None
        cmd, rem = result
        assert cmd == "GHI_NHO"
        # Remainder keeps original Vietnamese diacritics
        assert rem == "nội dung quan trọng"

    def test_exact_match_no_remainder(self):
        result = match_command("liệt kê", self.CMDS)
        assert result is not None
        cmd, rem = result
        assert cmd == "LIET_KE"
        assert rem == ""

    def test_exact_match_ascii(self):
        result = match_command("liet ke", self.CMDS)
        assert result is not None
        assert result[0] == "LIET_KE"

    def test_no_match_returns_none(self):
        assert match_command("hello world", self.CMDS) is None
        assert match_command("xin chao", self.CMDS) is None

    def test_english_alias(self):
        result = match_command("search python", self.CMDS)
        assert result is not None
        cmd, rem = result
        assert cmd == "TIM"
        assert rem == "python"

    def test_xem_wiki_page_beats_xem_wiki(self):
        # "xem wiki " (with space) should match XEM_WIKI_PAGE, not XEM_WIKI
        result = match_command("xem wiki python", self.CMDS)
        assert result is not None
        assert result[0] == "XEM_WIKI_PAGE"
        assert result[1] == "python"

    def test_xem_wiki_exact_matches_xem_wiki(self):
        result = match_command("xem wiki", self.CMDS)
        assert result is not None
        assert result[0] == "XEM_WIKI"

    def test_empty_input_returns_none(self):
        assert match_command("", self.CMDS) is None

    def test_case_insensitive(self):
        result = match_command("GHI NHO bai tho", self.CMDS)
        assert result is not None
        assert result[0] == "GHI_NHO"
