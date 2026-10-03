import unittest
from utils.douyin_comments import is_owned_comment_row


class RowIdentityTests(unittest.TestCase):
    def test_real_own_author_row(self):
        self.assertTrue(is_owned_comment_row('包包\n作者\n刚刚\n回复来自codex\n0\n回复\n删除\n举报','回复来自codex','包包'))

    def test_name_in_other_users_comment_does_not_count(self):
        self.assertFalse(is_owned_comment_row('别人\n刚刚\n包包\n作者\n回复来自codex','回复来自codex','包包'))

    def test_substring_and_non_author_are_rejected(self):
        self.assertFalse(is_owned_comment_row('包包的朋友\n作者\n回复来自codex','回复来自codex','包包'))
        self.assertFalse(is_owned_comment_row('包包\n刚刚\n回复来自codex','回复来自codex','包包'))

    def test_different_text_is_not_verification(self):
        self.assertFalse(is_owned_comment_row('包包\n作者\n其他内容','回复来自codex','包包'))


if __name__=='__main__':
    unittest.main()
