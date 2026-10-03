"""Public notification DOM tests with local HTML and all network aborted.

No real profile, cookies, runtime files, credentials, models or social pages.
Click handlers are local fixture events, and reload is simulated readback.
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
from utils.douyin_notifications import (
    DouyinNotificationService, PUBLIC_ROWS_JS, PUBLIC_IDENTITIES,
    composer_matches, target_row,
)


ROOT = '1111111111111111111'
TARGET = '2222222222222222222'
OTHER = '3333333333333333333'
VIDEO = '8888888888888888888'
OWN = '/user/fixture-owner'
ACTOR = '/user/fixture-reader-a'
OTHER_ACTOR = '/user/fixture-reader-b'
TEXT = '每天重复整理订单'
REPLY = '你现在用的是表格还是网页后台？'


def public_row(cid=TARGET, profile=ACTOR, text=TEXT, author='同名读者', parent=ROOT):
    return dict(id=cid, profile_path=profile, text=text, author=author,
                parent_id=parent, root_id=ROOT, reply_to_path=None, index=0)


def notice():
    return dict(notification_key='fixture-key', author='同名读者', text=TEXT,
                source_profile_path=ACTOR, kind='comment', cover_path='/fixture.jpg',
                context=dict(video_id=VIDEO, comment_id=TARGET, root_id=TARGET,
                             existing_self_ids=[],is_own_video=True,is_top_level=True,work_type='video'))


class NotificationIdentityTests(unittest.TestCase):
    def test_video_id_parses_modal_video_note_and_rejects_unbound_urls(self):
        service = DouyinNotificationService(None)
        for url in (f'https://www.douyin.com/user/self?modal_id={VIDEO}',
                    f'https://www.douyin.com/video/{VIDEO}',
                    f'https://www.douyin.com/note/{VIDEO}?foo=bar'):
            with self.subTest(url=url):
                self.assertEqual(service._video_id(url), VIDEO)
        for url in ('about:blank', 'https://www.douyin.com/',
                    'https://www.douyin.com/user/self?modal_id=not-an-id',
                    f'https://other.invalid/video/{VIDEO}'):
            with self.subTest(url=url):
                self.assertEqual(service._video_id(url), '')

    def test_same_display_name_and_text_require_matching_profile(self):
        rows = [public_row(profile=OTHER_ACTOR, cid=OTHER), public_row()]
        self.assertEqual(target_row(rows, notice())['id'], TARGET)

    def test_same_profile_and_text_more_than_once_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, 'NOT_UNIQUE'):
            target_row([public_row(), public_row(cid=OTHER)], notice())

    def test_missing_and_malformed_platform_ids_are_not_targets(self):
        for rows in ([], [public_row(cid='not-an-id')], [public_row(profile=OTHER_ACTOR)]):
            with self.subTest(rows=rows), self.assertRaisesRegex(RuntimeError, 'NOT_UNIQUE'):
                target_row(rows, notice())

    def test_emoji_target_requires_alias_equivalence_not_arbitrary_text(self):
        item = notice()
        item['text'] = '👍'
        self.assertEqual(target_row([public_row(text='[赞]')], item)['id'], TARGET)
        with self.assertRaisesRegex(RuntimeError, 'NOT_UNIQUE'):
            target_row([public_row(text='无关内容')], item)

    def test_composer_requires_recipient_and_complete_original_text(self):
        target = public_row()
        self.assertTrue(composer_matches('回复@同名读者：' + TEXT, target))
        for label in ('回复@另一个人：' + TEXT, '回复@同名读者：每天重复',
                      '回复@同名读者：' + TEXT + '更多内容', TEXT,
                      '回复@同名读者的朋友：' + TEXT):
            with self.subTest(label=label):
                self.assertFalse(composer_matches(label, target))
        self.assertTrue(composer_matches('回复@同名读者：[赞]', public_row(text='👍')))


def comment_html(cid, author, profile, text, *, children='', reply_to=None):
    body = '<img alt="[赞]"><img alt="[赞]">' if text == 'EMOJI' else html.escape(text)
    mention = f'<div class="ym1IYI4C"></div><a href="{html.escape(reply_to)}" aria-label="reply-recipient"></a>' if reply_to else ''
    return f'''<div data-e2e="comment-item" data-fixture-id="{cid}">
      <div class="JPoP3Ezg"><div class="dIlRe7ZV">
        <div class="comment-item-info-wrap"><div class="vlTNLkdG"><a href="{html.escape(profile)}"><span data-click-from="title">{html.escape(author)}</span></a>{mention}</div>
          <div data-e2e="video-comment-more"><span id="tooltip_{cid}"></span></div></div>
        <div class="FduGc_lz">{body}</div>
        <div class="Ctt1baq8"><button class="reply-entry">回复</button></div>
      </div></div><div class="fixture-children">{children}</div></div>'''


SCRIPT = '''<script>(()=>{
document.body.dataset.selected='';document.body.dataset.sends='0';
const composer=document.querySelector('.comment-input-container');
for(const row of document.querySelectorAll('[data-fixture-id]')){
 const entry=row.querySelector(':scope > .JPoP3Ezg > .dIlRe7ZV > .Ctt1baq8 > .reply-entry');
 entry.onclick=()=>{
  document.querySelectorAll('.reply-entry').forEach(e=>e.textContent='回复');entry.textContent='回复中';
  document.body.dataset.selected=row.dataset.fixtureId;composer.style.display='block';
  const author=row.querySelector('.comment-item-info-wrap a').innerText;
  const text=row.querySelector('.FduGc_lz').innerText;
  composer.querySelector('.YpWMCop5').textContent='回复@'+author+'：'+text;
 };
}
document.querySelector('.wchsYBpK.jfGCpJo0').onclick=async()=>{
 document.body.dataset.sends=Number(document.body.dataset.sends)+1;
 await window.recordFixtureSend({parent:document.body.dataset.selected,text:composer.querySelector('[contenteditable]').innerText});
};
})();</script>'''


class NotificationDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chrome = os.environ.get('SAU_CHROME_PATH', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not Path(chrome).is_file():
            self.skipTest('Installed Chrome required for isolated HTML fixtures')
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=chrome, headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.page.wait_for_timeout = AsyncMock()
        self.events, self.submissions = [], []
        self.before_submit = Mock(side_effect=lambda: self.events.append('guard'))

        def record(item):
            self.events.append('send')
            self.submissions.append(item)
        await self.page.expose_function('recordFixtureSend', record)
        self.identity_patch = patch.dict(PUBLIC_IDENTITIES, {
            'fixture': {'handle': 'fixture-owner-handle', 'profile_path': OWN}})
        self.identity_patch.start()

        @asynccontextmanager
        async def opened(*args, **kwargs):
            yield self.context
        self.account = dict(id='fixture', channel='douyin', display_name='测试作者')
        self.base = SimpleNamespace(
            activity=Mock(side_effect=lambda *_: nullcontext()),
            sessions=SimpleNamespace(account=Mock(return_value=self.account),
                open=Mock(side_effect=opened), probe=AsyncMock(return_value={'status': 'READY'})),
            _check=AsyncMock(), _snapshot=AsyncMock(return_value={
                'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}),
        )
        self.service = DouyinNotificationService(self.base)
        # Local about:blank is not a Douyin URL. Mock only work identity here;
        # the real URL parser has separate pure tests above.
        self.service._video_id = Mock(return_value=VIDEO)
        self.item = notice()
        self.opens = 0
        await self.fixture()

        async def open_target(*_):
            self.opens += 1
            if self.opens > 1:
                await self.persisted_readback()
            rows = await self.rows()
            return self.page, VIDEO, rows, target_row(rows, self.item)
        self.service._open_target = AsyncMock(side_effect=open_target)
        self.page.reload = AsyncMock(side_effect=self.persisted_readback)

    async def asyncTearDown(self):
        if hasattr(self, 'identity_patch'):
            self.identity_patch.stop()
        if hasattr(self, 'browser'):
            await self.browser.close()
            await self.playwright.stop()
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    async def fixture(self, *, first_text=TEXT,nested=False):
        children = comment_html(TARGET, '同名读者', ACTOR, first_text)
        children += comment_html(OTHER, '同名读者', OTHER_ACTOR, TEXT)
        root = comment_html(ROOT, '测试作者', OWN, '上层原评论', children=children if nested else '')
        if not nested:root+=children
        self.document = '''<!doctype html><meta charset="utf-8"><base href="https://fixture.invalid/">
            <style>[contenteditable]{min-width:300px;min-height:30px}button{min-height:25px}</style>'''+f'''
            <div data-e2e="user-detail"><div data-e2e="user-info"><h1>测试作者</h1><p>抖音号：fixture-owner-handle</p></div>
            <div id="semiTabpost" aria-selected="true"></div><div data-e2e="scroll-list"><a href="https://www.douyin.com/video/{VIDEO}">自己的作品</a></div></div>
            <div data-e2e="video-info" data-e2e-aweme-id="{VIDEO}"><div data-e2e="feed-video-nickname">@测试作者</div>
            <div data-e2e="video-desc">这是测试视频的真实可见标题</div></div><div data-e2e="comment-list">''' + root + '''</div>
            <div class="comment-input-container" style="display:none"><div class="YpWMCop5"></div>
              <div contenteditable="true"></div><div class="commentInput-right-ct">
                <button class="wchsYBpK jfGCpJo0">发送</button></div></div>''' + SCRIPT
        await self.page.set_content(self.document)

    async def rows(self):
        return await self.page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)

    async def add_reply(self, parent, text, *, cid='4444444444444444444', reply_to=None):
        markup = comment_html(cid, '测试作者', OWN, text, reply_to=reply_to)
        await self.page.locator(f'[data-fixture-id="{parent}"] > .fixture-children').evaluate(
            '(e, markup)=>e.insertAdjacentHTML("beforeend",markup)', markup)

    async def persisted_readback(self, *args, **kwargs):
        await self.page.set_content(self.document)
        for index, item in enumerate(self.submissions):
            # Public Douyin presents replies-to-replies flattened under the root,
            # with a recipient profile link. Match the actual observed structure.
            await self.add_reply(TARGET, item['text'], cid=str(4444444444444444444 + index), reply_to=None)

    async def reply(self, **kwargs):
        return await self.service.reply('fixture', self.item, REPLY, self.before_submit, **kwargs)

    async def test_public_rows_nested_ids_profiles_and_emoji_alt(self):
        await self.fixture(first_text='EMOJI',nested=True)
        rows = await self.rows()
        root, child, sibling = rows
        self.assertEqual(root['id'], ROOT)
        self.assertIsNone(root['parent_id'])
        self.assertEqual(child['id'], TARGET)
        self.assertEqual(child['parent_id'], ROOT)
        self.assertEqual(child['root_id'], ROOT)
        self.assertEqual(child['text'], '[赞][赞]')
        self.assertEqual(child['profile_path'], ACTOR)
        self.assertEqual(sibling['profile_path'], OTHER_ACTOR)

    async def test_bound_row_selects_nearest_comment_not_parent_containing_id(self):
        row, target, _ = await self.service._bound_row(self.page, self.item, TARGET)
        self.assertEqual(await row.count(), 1)
        self.assertEqual(await row.get_attribute('data-fixture-id'), TARGET)
        self.assertEqual(target['profile_path'], ACTOR)

    async def test_hidden_comment_node_is_not_a_bound_target(self):
        await self.page.locator(f'[data-fixture-id="{TARGET}"]').evaluate("e=>e.style.display='none'")
        with self.assertRaises(RuntimeError):
            await self.service._bound_row(self.page, self.item, TARGET)

    async def test_context_requires_owned_video_and_top_level(self):
        context = await self.service.read_context('fixture', self.item)
        self.assertEqual(context['interaction_type'], 'video_comment')
        self.assertEqual(context['parent_comment'], '')
        self.assertEqual(context['post_title'], '这是测试视频的真实可见标题')
        self.assertEqual(context['existing_self_ids'], [])
        self.assertTrue(context['is_own_video']);self.assertTrue(context['is_top_level'])
        self.assertEqual(context['work_type'],'video')

    async def test_correct_target_single_guarded_click_and_fresh_readback(self):
        result = await self.reply()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(self.submissions, [{'parent': TARGET, 'text': REPLY}])
        self.assertEqual(self.events, ['guard', 'send'])
        self.page.reload.assert_awaited_once()
        self.assertEqual(self.service._open_target.await_count, 2)

    async def test_work_id_changed_during_final_snapshot_blocks_send(self):
        async def snapshot(_, label):
            if label == 'notification-before-send':
                self.service._video_id.return_value = '9999999999999999999'
            return {'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}
        self.base._snapshot.side_effect = snapshot
        with self.assertRaisesRegex(RuntimeError, 'PREPARED_NOTIFICATION_TARGET_CHANGED'):
            await self.reply()
        self.assertEqual(self.submissions, [])
        self.before_submit.assert_not_called()

    async def test_reload_to_other_work_requires_notification_reopen_for_readback(self):
        real_fixture_open = self.service._open_target.side_effect

        async def reopen(*args):
            self.service._video_id.return_value = VIDEO
            return await real_fixture_open(*args)

        async def redirected_reload(*args, **kwargs):
            self.service._video_id.return_value = '9999999999999999999'
            await self.page.set_content('<main>Different work after modal URL reload</main>')
        self.service._open_target.side_effect = reopen
        self.page.reload.side_effect = redirected_reload
        result = await self.reply()
        self.assertEqual(result['status'], 'VERIFIED')
        self.assertEqual(self.submissions, [{'parent': TARGET, 'text': REPLY}])
        self.assertEqual(self.service._open_target.await_count, 2)
        self.before_submit.assert_called_once()

    async def test_readback_reopens_wrong_work_stays_unknown_without_resend(self):
        fixture_open = self.service._open_target.side_effect

        async def wrong_reopened_work(*args):
            page, video, rows, target = await fixture_open(*args)
            if self.opens > 1:
                video = '9999999999999999999'
            return page, video, rows, target
        self.service._open_target.side_effect = wrong_reopened_work
        result = await self.reply()
        self.assertEqual(result['status'], 'UNKNOWN_RESULT')
        self.assertEqual(self.submissions, [{'parent': TARGET, 'text': REPLY}])
        self.before_submit.assert_called_once()

    async def test_changed_target_text_before_submit_never_clicks_send(self):
        async def change(_):
            await self.page.locator(f'[data-fixture-id="{TARGET}"] .FduGc_lz').evaluate("e=>e.textContent='changed-original'")
        self.base._check.side_effect = change
        with self.assertRaisesRegex(RuntimeError, 'PUBLIC_COMMENT_TARGET_NOT_UNIQUE'):
            await self.reply()
        self.assertEqual(self.submissions, [])
        self.before_submit.assert_not_called()

    async def test_changed_composer_recipient_or_text_before_send_never_clicks(self):
        for expression in (
            "document.querySelector('.YpWMCop5').textContent='回复@另外的人：每天重复整理订单'",
            "document.querySelector('[contenteditable]').innerText='unexpected-draft'",
        ):
            await self.fixture()
            self.opens = 0
            async def change(_, js=expression):
                await self.page.evaluate(js)
            self.base._check.side_effect = change
            with self.assertRaisesRegex(RuntimeError, 'PUBLIC_REPLY_(TARGET|TEXT)_CHANGED'):
                await self.reply()
        self.assertEqual(self.submissions, [])
        self.before_submit.assert_not_called()

    async def test_unknown_result_recheck_never_submits_twice(self):
        self.page.reload.side_effect = RuntimeError('fixture-after-click-timeout')
        first = await self.reply()
        self.assertEqual(first['status'], 'UNKNOWN_RESULT')
        second = await self.reply(recheck_only=True)
        self.assertEqual(second['status'], 'VERIFIED')
        self.assertEqual(len(self.submissions), 1)
        self.before_submit.assert_called_once()

    async def test_recheck_missing_reply_is_unknown_with_no_send(self):
        result = await self.reply(recheck_only=True)
        self.assertEqual(result['status'], 'UNKNOWN_RESULT')
        self.assertEqual(self.submissions, [])
        self.before_submit.assert_not_called()

    async def test_same_root_reply_to_other_recipient_cannot_verify_target(self):
        await self.add_reply(TARGET, REPLY, reply_to=OTHER_ACTOR)
        target = target_row(await self.rows(), self.item)
        observed = await self.service._readback(self.page, self.item, target, REPLY, OWN, [ROOT])
        self.assertEqual(observed, [])

    async def test_previous_self_comment_ids_cannot_verify_new_reply(self):
        await self.add_reply(TARGET, REPLY)
        target = target_row(await self.rows(), self.item)
        observed = await self.service._readback(self.page, self.item, target, REPLY, OWN, [ROOT, '4444444444444444444'])
        self.assertEqual(observed, [])

    async def test_nested_notification_never_opens_browser_or_model_context(self):
        self.item['kind']='reply'
        result=await self.reply()
        self.assertEqual(result['status'],'SKIP')
        context=await self.service.read_context('fixture',self.item)
        self.assertFalse(context['is_top_level'])
        self.base.sessions.open.assert_not_called()
        self.before_submit.assert_not_called()

    async def test_missing_old_prepared_scope_never_opens_browser(self):
        del self.item['context']['is_own_video']
        self.assertEqual((await self.reply())['status'],'SKIP')
        self.base.sessions.open.assert_not_called()

    async def test_nested_dom_cannot_pass_even_if_notification_claims_comment(self):
        await self.fixture(nested=True)
        result=await self.reply()
        self.assertEqual(result['status'],'SKIP')
        self.before_submit.assert_not_called()

    async def test_wrong_owner_unproven_card_wrong_tab_and_note_fail_closed(self):
        for expression in [
            "document.querySelector('[data-e2e=feed-video-nickname]').textContent='@别人的账号'",
            "document.querySelector('[data-e2e=scroll-list] a').remove()",
            "document.querySelector('#semiTabpost').setAttribute('aria-selected','false')",
            "document.querySelector('[data-e2e=scroll-list] a').href='https://www.douyin.com/note/"+VIDEO+"'",
            "document.querySelector('[data-e2e=user-info] p').textContent='抖音号：someone-else'",
        ]:
            await self.fixture();self.opens=0
            await self.page.evaluate(expression)
            self.assertEqual((await self.reply())['status'],'SKIP')
        self.before_submit.assert_not_called();self.assertEqual(self.submissions,[])

    async def test_scope_changes_at_final_snapshot_block_send(self):
        async def remove_card(_,label):
            await self.page.locator('[data-e2e="scroll-list"] a').evaluate('e=>e.remove()')
            return {'screenshot_path':'fixture.png'}
        self.base._snapshot.side_effect=remove_card
        with self.assertRaisesRegex(RuntimeError,'TARGET_SCOPE_CHANGED_BEFORE_SUBMIT'):
            await self.reply()
        self.before_submit.assert_not_called();self.assertEqual(self.submissions,[])

    async def test_recipient_is_header_link_not_body_mention(self):
        await self.add_reply(TARGET,'你好',reply_to=OTHER_ACTOR)
        rows=await self.rows()
        self.assertEqual(next(r for r in rows if r['id']=='4444444444444444444')['reply_to_path'],OTHER_ACTOR)
        await self.page.locator('[data-fixture-id="'+TARGET+'"] > .JPoP3Ezg > .dIlRe7ZV > .FduGc_lz').evaluate("e=>e.insertAdjacentHTML('beforeend','<a href=\"https://fixture.invalid/user/mentioned\">路人</a>')")
        rows=await self.rows()
        self.assertIsNone(next(r for r in rows if r['id']==TARGET)['reply_to_path'])


if __name__ == '__main__':
    unittest.main()
