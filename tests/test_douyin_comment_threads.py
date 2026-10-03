"""Isolated HTML tests for thread replies; all network requests are aborted.

The row/composer structure follows reply-entry-dom.json captured on 2026-10-03.
Fixtures contain synthetic names/text only. No profile, runtime config, account
cookies, model, or real social page is ever opened. Browser clicks hit local HTML.
"""
from contextlib import asynccontextmanager, nullcontext
import html
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from patchright.async_api import async_playwright
from utils.douyin_comment_threads import DouyinCommentThreadService, normalize_rows


VIDEO = 'fixture-video'
OWNER = '测试作者'
REPLY = '先看看具体步骤，你现在用的是表格还是网页后台？'


def source_row(**changes):
    return dict(author='测试读者', text='每天重复整理订单', published_label='1分钟前',
                is_own=False, is_nested=False, dom_index=0, author_href=None,
                platform_cid=None, row_class='comment-main-YV_lqD',
                parent_class='comment-item-o85vW5', **changes)


class RowNormalizationTests(unittest.TestCase):
    def test_time_and_dom_index_changes_do_not_change_identity(self):
        before = normalize_rows([source_row()], VIDEO)[0]
        later = source_row()
        later.update(published_label='2小时前', dom_index=7)
        after = normalize_rows([later], VIDEO)[0]
        self.assertEqual(before['comment_key'], after['comment_key'])
        self.assertEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(after['raw']['dom_index'], 7)

    def test_same_name_and_text_without_ids_are_ambiguous(self):
        rows = normalize_rows([source_row(), source_row()], VIDEO)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['ambiguous'] and row['raw']['ambiguous'] for row in rows))

    def test_platform_ids_disambiguate_identical_public_text(self):
        first, second = source_row(), source_row()
        first['platform_cid'], second['platform_cid'] = 'first', 'second'
        rows = normalize_rows([first, second], VIDEO)
        self.assertNotEqual(rows[0]['comment_key'], rows[1]['comment_key'])
        self.assertFalse(any(row['ambiguous'] for row in rows))

    def test_nested_or_empty_rows_are_not_original_comments(self):
        nested, empty = source_row(), source_row()
        nested['is_nested'], empty['text'] = True, ''
        self.assertEqual(normalize_rows([nested, empty], VIDEO), [])


def original_html(cid, author, text, *, own=False, placeholder=None, visible=False):
    # Same direct-child selectors and contenteditable placeholder as the observed
    # creator DOM. An IMG contributes alt text but no innerText.
    badge = '<span class="tag-PamXEh">作者</span>' if own else ''
    body = ''.join('<img alt="[赞]">' for _ in range(4)) if text == 'EMOJI' else html.escape(text)
    placeholder = placeholder if placeholder is not None else '回复 ' + author + '：'
    return f'''
      <div class="comment-item-o85vW5" data-thread="{html.escape(cid)}">
        <div class="comment-main-YV_lqD" data-comment-id="{html.escape(cid)}">
          <div class="username-AHNP_w">{html.escape(author)}{badge}</div>
          <div class="time-qUi7iL">4小时前</div><div class="text-uG7vGS">{body}</div>
          <div class="comment-operation-wrap-lc52eY">
            <div class="operations-AVKfA7"><button type="button" class="open-reply">回复</button></div>
            <div class="composer-XoMZWL compact-yiU5He" style="display:{'block' if visible else 'none'}">
              <div class="emoji-input-wrap-n0yWpK"><div class="input-container-gJfG7y">
                <div contenteditable="true" data-placeholder="{html.escape(placeholder)}" class="editable-input-_mF1vt"></div>
              </div><div class="footer-gDBgaD"><div class="right-wJodq_">
                <button type="button">取消</button><button type="button" class="send-reply" disabled>发送</button>
              </div></div></div>
            </div>
          </div>
        </div><div class="reply-list-fixture"></div>
      </div>'''


SCRIPT = '''<script>
window.localSubmissions=[]; document.body.dataset.topLevelSubmissions='0';
document.querySelector('#top-send').onclick=()=>document.body.dataset.topLevelSubmissions=Number(document.body.dataset.topLevelSubmissions)+1;
for(const row of document.querySelectorAll('[data-comment-id]')) {
  const composer=row.querySelector('[class*="composer-"]');
  const editor=composer.querySelector('[contenteditable]');
  const send=composer.querySelector('.send-reply');
  row.querySelector('.open-reply').onclick=()=>composer.style.display='block';
  editor.oninput=()=>send.disabled=!editor.innerText.trim();
  send.onclick=async()=>{
    const sent={parent:row.dataset.commentId, text:editor.innerText};
    window.localSubmissions.push(sent); await window.recordLocalSend(sent);
  };
}
</script>'''


class ThreadDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chrome = os.environ.get('SAU_CHROME_PATH', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not Path(chrome).is_file():
            self.skipTest('Installed Chrome required for isolated HTML DOM fixtures')
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=chrome, headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.events, self.submissions = [], []
        self.callback = Mock(side_effect=lambda: self.events.append('before_submit'))

        def record(sent):
            self.events.append('send')
            self.submissions.append(sent)
        await self.page.expose_function('recordLocalSend', record)
        self.account = dict(id='isolated-account', display_name=OWNER)
        self.task = dict(id='isolated-publish', plan=dict(title='isolated-work'))

        @asynccontextmanager
        async def opened(*args, **kwargs):
            yield self.context

        self.base = SimpleNamespace(
            published_task=Mock(return_value=(self.task, self.account)),
            activity=Mock(side_effect=lambda *_: nullcontext()),
            sessions=SimpleNamespace(open=Mock(side_effect=opened), probe=AsyncMock(return_value={'status': 'READY'})),
            _check=AsyncMock(),
            _snapshot=AsyncMock(return_value={
                'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}),
        )
        self.open_count = 0

        async def open_comments(*_):
            self.open_count += 1
            # Simulated server readback: replace the document after submission,
            # then insert only saved thread records. No optimistic DOM is used.
            if self.open_count > 1:
                await self.page.set_content(self.document)
                for item in self.submissions:
                    await self.add_owner_reply(item['parent'], item['text'])
            return self.page
        self.base._open_comments = AsyncMock(side_effect=open_comments)
        self.service = DouyinCommentThreadService(self.base)
        self.sleep_patch = patch('utils.douyin_comment_threads.asyncio.sleep', new=AsyncMock())
        self.sleep_patch.start()
        await self.fixture()

    async def asyncTearDown(self):
        if hasattr(self, 'sleep_patch'):
            self.sleep_patch.stop()
        if hasattr(self, 'browser'):
            await self.browser.close()
            await self.playwright.stop()
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    async def fixture(self, *, first_text='每天重复整理订单', first_placeholder=None, own=False):
        self.document = '''<!doctype html><meta charset="utf-8">
            <style>[contenteditable]{min-height:25px;min-width:300px}button{min-height:24px}</style>
            <div class="composer-top"><div id="top-editor" contenteditable="true"></div>
              <button id="top-send">发送</button></div><main id="comments">'''
        self.document += original_html('first', '测试读者', first_text, own=own, placeholder=first_placeholder)
        self.document += original_html('second', '另一读者', '想整理旅行照片')
        self.document += '</main><p>没有更多评论</p>' + SCRIPT
        await self.page.set_content(self.document)
        self.observation = (await self.service._rows(self.page, VIDEO))[0]

    async def add_owner_reply(self, parent, text):
        await self.page.locator(f'[data-thread="{parent}"] > .reply-list-fixture').evaluate(
            '''(e, data)=>{
              const wrapper=document.createElement('div');wrapper.className='thread-reply-fixture';
              const name=document.createElement('div');name.className='username-AHNP_w';name.textContent=data.owner;
              const badge=document.createElement('span');badge.textContent='作者';name.append(badge);
              const body=document.createElement('div');body.className='text-uG7vGS';body.textContent=data.text;
              wrapper.append(name,body);e.append(wrapper);
            }''', dict(owner=OWNER, text=text))

    async def call_reply(self, **kwargs):
        return await self.service.reply('isolated-publish', VIDEO, self.observation, REPLY, self.callback, **kwargs)

    async def assert_no_send(self):
        self.assertEqual(self.submissions, [])
        self.assertEqual(await self.page.evaluate('Number(document.body.dataset.topLevelSubmissions)'), 0)
        self.callback.assert_not_called()

    async def test_rows_js_preserves_emoji_alt_and_author_badge(self):
        await self.fixture(first_text='EMOJI', own=True)
        rows = await self.service._rows(self.page, VIDEO)
        self.assertEqual(rows[0]['text'], '[赞][赞][赞][赞]')
        self.assertEqual(rows[0]['author'], '测试读者')
        self.assertTrue(rows[0]['is_own'])
        self.assertFalse(rows[1]['is_own'])

    async def test_scan_uses_local_dom_and_reports_end_marker(self):
        result = await self.service.scan('isolated-publish', VIDEO, max_scrolls=0)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['comments']), 2)
        await self.assert_no_send()

    async def test_author_and_ambiguous_observations_skip_without_open(self):
        for extra in ({'is_own': True}, {'ambiguous': True}, {'raw': {'ambiguous': True}}):
            self.observation.update(extra)
            result = await self.call_reply()
            self.assertEqual(result['state'], 'SKIP')
        self.base.sessions.open.assert_not_called()
        await self.assert_no_send()

    async def test_reorders_before_open_are_resolved_by_identity_not_saved_index(self):
        await self.page.evaluate("document.querySelector('#comments').prepend(document.querySelector('[data-thread=second]'))")
        result = await self.call_reply()
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(self.submissions, [{'parent': 'first', 'text': REPLY}])

    async def test_scoped_composer_callback_before_one_click_and_readback(self):
        result = await self.call_reply()
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertTrue(result['evidence']['verified_after_reload'])
        self.assertEqual(result['evidence']['parent_key'], self.observation['comment_key'])
        self.assertEqual(self.events, ['before_submit', 'send'])
        self.assertEqual(self.submissions, [{'parent': 'first', 'text': REPLY}])
        self.assertEqual(await self.page.locator('#top-editor').inner_text(), '')
        self.assertEqual(await self.page.evaluate('Number(document.body.dataset.topLevelSubmissions)'), 0)
        self.assertEqual(self.base._open_comments.await_count, 2)

    async def test_wrong_recipient_does_not_fill_or_send(self):
        await self.fixture(first_placeholder='回复 错误读者：')
        result = await self.call_reply()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn('REPLY_RECIPIENT_MISMATCH', result['evidence']['message'])
        self.assertEqual(await self.page.locator('[data-comment-id=first] [contenteditable]').inner_text(), '')
        await self.assert_no_send()

    async def test_changed_original_text_before_send_blocks(self):
        async def change(_):
            await self.page.locator('[data-comment-id=first] > [class*="text-"]').evaluate("e=>e.textContent='changed-comment'")
        self.base._check.side_effect = change
        result = await self.call_reply()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertIn('COMMENT_TARGET_CHANGED', result['evidence']['message'])
        await self.assert_no_send()

    async def test_callback_failure_prevents_send(self):
        self.callback.side_effect = RuntimeError('guard-not-committed')
        result = await self.call_reply()
        self.assertEqual(result['state'], 'BLOCKED')
        self.assertFalse(result['evidence']['attempted'])
        self.assertEqual(self.submissions, [])

    async def test_unknown_result_recheck_does_not_send_again(self):
        async def snapshot(_, label):
            if label == 'autoreply-after-send':
                raise RuntimeError('fixture-after-click-crash')
            return {'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}
        self.base._snapshot.side_effect = snapshot
        first = await self.call_reply()
        self.assertEqual(first['state'], 'UNKNOWN_RESULT')
        second = await self.call_reply(recheck_only=True)
        self.assertEqual(second['state'], 'VERIFIED')
        self.assertEqual(len(self.submissions), 1)
        self.callback.assert_called_once()

    async def test_recheck_absent_reply_never_opens_composer_or_sends(self):
        result = await self.call_reply(recheck_only=True)
        self.assertEqual(result['state'], 'UNKNOWN_RESULT')
        self.assertFalse(await self.page.locator('[data-comment-id=first] [class*="composer-"]').is_visible())
        await self.assert_no_send()

    async def test_identical_reply_in_other_thread_does_not_verify_target(self):
        await self.add_owner_reply('second', REPLY)
        row = self.page.locator('[data-comment-id=first]')
        self.assertEqual(await self.service._thread_replies(row, OWNER), [])
        result = await self.call_reply(recheck_only=True)
        self.assertEqual(result['state'], 'UNKNOWN_RESULT')
        await self.assert_no_send()

    async def test_after_send_same_text_readback_on_different_parent_is_unknown(self):
        opened = 0

        async def wrong_thread_readback(*_):
            nonlocal opened
            opened += 1
            if opened > 1:
                await self.page.set_content(self.document)
                await self.add_owner_reply('second', REPLY)
            return self.page
        self.base._open_comments.side_effect = wrong_thread_readback
        result = await self.call_reply()
        self.assertEqual(self.submissions, [{'parent': 'first', 'text': REPLY}])
        self.assertEqual(result['state'], 'UNKNOWN_RESULT')
        self.assertFalse(result['evidence']['verified_after_reload'])
        self.assertEqual(result['evidence']['matches'], [])
        self.callback.assert_called_once()

    async def test_existing_owner_reply_in_same_thread_prevents_second_reply(self):
        await self.add_owner_reply('first', '已经有一条不同的回复')
        result = await self.call_reply()
        self.assertEqual(result['state'], 'SKIP')
        self.assertEqual(result['evidence']['reason'], 'OWNER_ALREADY_REPLIED')
        await self.assert_no_send()

    async def test_inspect_composer_does_not_fill_or_send(self):
        result = await self.service.inspect_reply_entry('isolated-publish', VIDEO, self.observation['comment_key'])
        self.assertEqual(result['status'], 'COMPOSER_INSPECTED_NO_SEND')
        self.assertIn('data-placeholder="回复 测试读者："', result['parent_markup'])
        self.assertEqual(await self.page.locator('[data-comment-id=first] [contenteditable]').inner_text(), '')
        await self.assert_no_send()

    async def test_reorder_during_final_checks_cannot_send_to_other_thread(self):
        async def reorder(_):
            # The other composer is already open with an unsent human draft.
            # A stale nth(index) locator must never send that draft.
            other = self.page.locator('[data-comment-id=second]')
            await other.locator('.open-reply').click()
            await other.locator('[contenteditable]').fill('other-thread-draft')
            await self.page.evaluate("document.querySelector('#comments').prepend(document.querySelector('[data-thread=second]'))")
        self.base._check.side_effect = reorder
        result = await self.call_reply()
        self.assertEqual([item for item in self.submissions if item['parent'] != 'first'], [])
        if self.submissions:
            self.assertEqual(self.submissions, [{'parent': 'first', 'text': REPLY}])
        else:
            self.assertEqual(result['state'], 'BLOCKED')

    async def test_index_shift_during_screenshot_keeps_original_button_node(self):
        async def snapshot(_, label):
            if label == 'autoreply-before-send':
                await self.page.evaluate("document.querySelector('#comments').prepend(document.querySelector('[data-thread=second]'))")
            return {'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}
        self.base._snapshot.side_effect = snapshot
        result = await self.call_reply()
        self.assertEqual(result['state'], 'VERIFIED')
        self.assertEqual(self.submissions, [{'parent': 'first', 'text': REPLY}])

    async def test_reply_text_changed_during_screenshot_is_not_sent(self):
        async def snapshot(_, label):
            if label == 'autoreply-before-send':
                await self.page.locator('[data-comment-id=first] [contenteditable]').evaluate("e=>e.innerText='unexpected-edited-reply'")
            return {'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}
        self.base._snapshot.side_effect = snapshot
        result = await self.call_reply()
        self.assertEqual(self.submissions, [])
        self.assertEqual(result['state'], 'BLOCKED')


if __name__ == '__main__':
    unittest.main()
