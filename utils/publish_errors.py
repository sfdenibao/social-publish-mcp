"""Safe, typed publishing failures; never expose a browser exception or page dump."""
import re


DECLARATION_MESSAGES = {
    'DECLARATION_DIALOG_UNAVAILABLE': '未能打开平台自主声明弹窗，尚未点击发布。',
    'DECLARATION_OPTION_NOT_FOUND': '平台自主声明中没有找到所选选项，请核对声明；尚未点击发布。',
    'DECLARATION_CONFIRM_FAILED': '平台自主声明未能确认，尚未点击发布。',
    'DECLARATION_SELECTION_FAILED': '平台自主声明未能选中，尚未点击发布。',
    'UNSUPPORTED_DECLARATION': '当前平台适配器不支持该自主声明。原创不是自主声明选项，不能自动改为其他声明或不添加。',
    'UNSUPPORTED_EXPLICIT_DECLARATION': '当前平台适配器不支持该自主声明，不能自动忽略后继续发布。',
}


def _label(value):
    if not isinstance(value, str):
        return ''
    text = ''.join(c for c in value if c.isprintable())[:160]
    # Only selected/available UI labels are accepted, not arbitrary diagnostics.
    if re.search(r'https?://|(?:cookie|token|password|authorization|session)\s*[:=]|\bbearer\s', text, re.I):
        return '[REDACTED]'
    return text


class DeclarationError(RuntimeError):
    def __init__(self, code='DECLARATION_SELECTION_FAILED', *, requested='', available_options=(), channel=None):
        if code not in DECLARATION_MESSAGES:
            raise ValueError('UNKNOWN_DECLARATION_ERROR_CODE')
        self.code = code
        self.message = DECLARATION_MESSAGES[code]
        self.stage = 'DECLARATION'
        self.details = {'requested': _label(requested)}
        if isinstance(available_options, (list, tuple)):
            self.details['available_options'] = list(dict.fromkeys(_label(v) for v in available_options if isinstance(v, str)))[:24]
        if channel in {'douyin', 'wechat_channels', 'xiaohongshu'}:
            self.details['channel'] = channel
        # Keeps legacy str(exc)/code comparisons deterministic and non-sensitive.
        super().__init__(code)

    def to_dict(self):
        return {'code': self.code, 'message': self.message, 'stage': self.stage,
                'details': {key: list(value) if isinstance(value, list) else value for key, value in self.details.items()}}

    @property
    def detail(self):
        return self.to_dict()


def safe_execution_error(exc, known_codes=(), *, stage='UPLOAD_PREPARE'):
    if isinstance(exc, DeclarationError):
        return exc.to_dict()
    raw = str(exc)
    if raw in DECLARATION_MESSAGES:
        return DeclarationError(raw).to_dict()
    code = raw if raw in known_codes else 'EXECUTION_FAILED'
    return {'code': code, 'message': '发布准备失败，请查看失败阶段后处理。' if code == 'EXECUTION_FAILED' else '发布准备受阻：' + code,
            'stage': stage, 'details': {}}
