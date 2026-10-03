"""Exact declaration values supported by the current adapters, never an ownership inference."""
from enum import Enum
from utils.publish_errors import DeclarationError


# Existing user-supplied Douyin modal DOM evidence, documented by
# uploader/douyin_uploader/main.py::set_self_declaration. This is deliberately
# the adapter's verified subset, not an exhaustive claim about platform options.
class SelfDeclaration(str, Enum):
    NONE = ''
    AI_GENERATED = '内容由AI生成'
    REPOSTED_INFORMATION = '内容为转载信息'
    PERSONAL_OPINION = '内容为个人观点或见解'


DOUYIN_DECLARATIONS = tuple(item.value for item in SelfDeclaration if item is not SelfDeclaration.NONE)


def declaration_capability(channel):
    return {
        'supported_values': ['', *DOUYIN_DECLARATIONS] if channel == 'douyin' else [''],
        'allow_empty': True,
        'note': '仅列出当前适配器有既有 DOM 证据的自主声明选项，并非平台全部选项。'
                '“原创”性质与“自主声明”不同；不得自动映射为其他声明或清空。'
                '空字符串表示用户明确不添加自主声明。非空选项执行时仍核验实际弹窗。',
    }


def validate_declaration(channel, declaration):
    """Fail before media/profile/upload side effects; preserve the exact requested value."""
    allowed = declaration_capability(channel)['supported_values']
    if declaration not in allowed:
        raise DeclarationError('UNSUPPORTED_DECLARATION', requested=declaration,
                               available_options=allowed, channel=channel)


def validate_plan_declarations(plan):
    for item in plan.get('items', [plan]):
        validate_declaration(item['channel'], item.get('declaration', ''))
