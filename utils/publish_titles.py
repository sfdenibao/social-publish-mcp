"""Normalize titles before freezing plans so submission and verification agree."""
import unicodedata


def platform_title(title: str, channel: str) -> str:
    """Match the platform's actual stored title, including legacy XHS plans."""
    return title[:20] if channel == 'xiaohongshu' else title


def clean_publish_title(title: str) -> str:
    chars = []
    for char in title:
        category = unicodedata.category(char)
        if char.isspace():
            chars.append(' ')
        elif category[0] in {'L', 'N', 'M'} and char not in {'\ufe0e', '\ufe0f', '\u20e3'}:
            chars.append(char)
    value = ' '.join(''.join(chars).split())
    if not any(char.isalnum() for char in value) or len(value) > 30:
        raise ValueError('INVALID_TITLE')
    return value
