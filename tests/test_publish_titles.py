import unittest
from utils.publish_titles import clean_publish_title, platform_title


class TitleTests(unittest.TestCase):
    def test_platform_title_matches_upload(self):
        title=clean_publish_title('一二三四五六七八九十一二三四五六七八九十一')
        self.assertEqual(len(platform_title(title,'xiaohongshu')),20)
        self.assertEqual(platform_title(title,'douyin'),title)
        self.assertEqual(platform_title(title,'wechat_channels'),title)
    def test_special_characters(self):
        self.assertEqual(clean_publish_title(' 肥猫太悠闲了，,！🐱\u200b\x00 '), '肥猫太悠闲了')

    def test_text_numbers_and_spaces(self):
        self.assertEqual(clean_publish_title('猫咪\n Cafe\u0301 2026'), '猫咪 Cafe\u0301 2026')
        self.assertEqual(clean_publish_title('第九艺术（AI发布测试）'), '第九艺术AI发布测试')

    def test_empty_and_length_checked_after_cleaning(self):
        for value in ('，🐱\u200b', 'a'*31, '\u0301'):
            with self.assertRaisesRegex(ValueError, 'INVALID_TITLE'):
                clean_publish_title(value)
        self.assertEqual(clean_publish_title('a'*30+'，'), 'a'*30)

    def test_idempotent(self):
        value=clean_publish_title(' 猫咪，🐱\t日常！ ')
        self.assertEqual(clean_publish_title(value),value)
