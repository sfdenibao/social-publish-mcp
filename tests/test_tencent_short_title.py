import pytest

from uploader.tencent_uploader.main import format_str_for_short_title


@pytest.mark.parametrize('source,expected', [
    ('肥猫太悠闲了', '肥猫太悠闲了'),
    ('肥猫太悠闲了，', '肥猫太悠闲了'),
    ('肥猫，太悠闲了！', '肥猫太悠闲了'),
    ('猫猫', ''),
    ('，！？', ''),
    ('Abc123', 'Abc123'),
    ('一二三四五六七八九十一二三四五六', '一二三四五六七八九十一二三四五六'),
    ('一二三四五六七八九十一二三四五六七', '一二三四五六七八九十一二三四五六'),
])
def test_short_title_preserves_boundaries_without_punctuation_or_invented_filler(source, expected):
    assert format_str_for_short_title(source) == expected
