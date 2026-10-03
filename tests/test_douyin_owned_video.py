"""Owned-work ordering and frozen-comment scope using isolated local HTML.

No production profile, credentials, network, model calls or social submissions.
"""
from contextlib import asynccontextmanager, nullcontext
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from patchright.async_api import async_playwright
from tests.test_douyin_notifications import comment_html
from utils.douyin_owned_video import DouyinOwnedVideoService
from utils.douyin_notifications import PUBLIC_IDENTITIES


VIDEO = '10000000004'
NOTE = '20000000001'
TOP = '30000000001'
NESTED = '30000000002'
AUTHOR_COMMENT = '30000000003'
REPLY_FLAGGED = '30000000004'
OWN = '/user/owned-fixture'
VIEWER = '/user/viewer-fixture'
NESTED_VIEWER = '/user/nested-fixture'
TITLE = '肥猫太悠闲了'


class OwnedVideoDOMTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        chrome = os.environ.get('SAU_CHROME_PATH', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
        if not Path(chrome).is_file():
            self.skipTest('Installed Chrome required for isolated HTML tests')
        self.temp = tempfile.TemporaryDirectory()
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(executable_path=chrome, headless=True)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.abort())
        self.page = await self.context.new_page()
        self.page.wait_for_timeout = AsyncMock()
        self.identity_patch = patch.dict(PUBLIC_IDENTITIES, {
            'fixture': dict(handle='owner-fixture-handle', profile_path=OWN)})
        self.identity_patch.start()

        @asynccontextmanager
        async def opened(*args, **kwargs):
            yield self.context
        self.account = dict(id='fixture', channel='douyin', display_name='测试作者')
        self.base = SimpleNamespace(
            sessions=SimpleNamespace(account=Mock(return_value=self.account),
                open=Mock(side_effect=opened), probe=AsyncMock(return_value={'status': 'READY'})),
            activity=Mock(side_effect=lambda *_: nullcontext()), _check=AsyncMock(),
            _snapshot=AsyncMock(return_value={
                'screenshot_path': str(Path(self.temp.name) / 'fixture.png'), 'observed_at': 'fixture-time'}),
        )
        self.service = DouyinOwnedVideoService(self.base)
        self.service._video_id = Mock(return_value=VIDEO)
        self.card = dict(position=5, video_position=4, video_id=VIDEO, work_type='video',
                         title=TITLE, url=f'https://www.douyin.com/video/{VIDEO}', href=f'/video/{VIDEO}')
        await self.fixture()
        self.service._open_owned_video = AsyncMock(return_value=(self.page, self.card))

    async def asyncTearDown(self):
        if hasattr(self, 'identity_patch'):
            self.identity_patch.stop()
        if hasattr(self, 'browser'):
            await self.browser.close()
            await self.playwright.stop()
        if hasattr(self, 'temp'):
            self.temp.cleanup()

    async def fixture(self, *, extra_grid='', with_reply_flagged=False):
        nested = comment_html(NESTED, '楼中楼读者', NESTED_VIEWER, '楼中楼不要回复')
        comments = comment_html(TOP, '一级读者', VIEWER, '这猫真悠闲', children=nested)
        comments += comment_html(AUTHOR_COMMENT, '测试作者', OWN, 'EMOJI')
        if with_reply_flagged:
            comments += comment_html(REPLY_FLAGGED, '表面一级实际回复', '/user/reply-fixture', '回复了别人', reply_to=VIEWER)
        self.document = f'''<!doctype html><meta charset="utf-8"><base href="https://www.douyin.com/">
          <div data-e2e="user-detail">
            <div data-e2e="user-info"><h1>测试作者</h1><p>抖音号：owner-fixture-handle</p></div>
            <div id="semiTabpost" aria-selected="true">作品</div>
            <div id="own-posts" data-e2e="scroll-list">
              <a href="/video/10000000001">12<br>第一条</a>
              <a href="/note/{NOTE}">9<br>一条图文</a>
              <a href="/video/10000000002">1.2万<br>第二条</a>
              <a href="/video/10000000003">第三条</a>
              <a href="/video/{VIDEO}">18<br>{TITLE}</a>
            </div>{extra_grid}
          </div>
          <div data-e2e="video-info" data-e2e-aweme-id="{VIDEO}">
            <div data-e2e="feed-video-nickname">@测试作者</div><div data-e2e="video-desc">{TITLE}</div>
          </div>
          <div data-e2e="comment-list">{comments}<p>暂时没有更多评论</p></div>'''
        await self.page.set_content(self.document)

    def frozen_notice(self, *, cid=TOP, profile=VIEWER, text='这猫真悠闲', kind='comment'):
        return dict(kind=kind, author='一级读者', text=text, source_profile_path=profile,
                    raw={'video_id': VIDEO, 'comment_id': cid},
                    context={'video_id': VIDEO, 'comment_id': cid, 'root_id': cid,
                             'is_own_video': True, 'is_top_level': True, 'work_type': 'video'})

    async def test_cards_display_order_and_video_order_are_distinct(self):
        cards = await self.service._cards(self.page)
        self.assertEqual([c['video_id'] for c in cards], ['10000000001', NOTE, '10000000002', '10000000003', VIDEO])
        self.assertEqual([c['position'] for c in cards], [1, 2, 3, 4, 5])
        self.assertEqual([c['video_position'] for c in cards], [1, None, 2, 3, 4])
        self.assertEqual(cards[-1]['title'], TITLE)
        self.assertEqual(cards[2]['title'], '第二条')

    async def test_cards_require_selected_own_posts_tab(self):
        await self.page.locator('#semiTabpost').evaluate("e=>e.setAttribute('aria-selected','false')")
        with self.assertRaisesRegex(RuntimeError, 'OWN_WORKS_TAB_REQUIRED'):
            await self.service._cards(self.page)
        await self.page.locator('#semiTabpost').evaluate('e=>e.remove()')
        with self.assertRaisesRegex(RuntimeError, 'OWN_WORKS_TAB_REQUIRED'):
            await self.service._cards(self.page)

    async def test_cards_ignore_external_wrong_paths_duplicate_and_outside_links(self):
        await self.page.locator('#own-posts').evaluate('''e=>e.insertAdjacentHTML('beforeend',
          '<a href="https://evil.invalid/video/99999999999">wrong host</a>'+
          '<a href="/video/99999999999/extra">wrong path</a>'+
          '<a href="/user/99999999999">profile</a>'+
          '<a href="/video/123">short id</a>'+
          '<a href="/video/10000000004">duplicate</a>')''')
        await self.page.locator('body').evaluate('''e=>e.insertAdjacentHTML('beforeend',
          '<div data-e2e="scroll-list"><a href="/video/99999999999">outside own profile</a></div>')''')
        cards = await self.service._cards(self.page)
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[-1]['video_id'], VIDEO)

    async def test_hidden_other_tab_cards_do_not_change_own_work_order(self):
        await self.fixture(extra_grid='''<div data-e2e="scroll-list" style="display:none">
            <a href="/video/99999999999">hidden liked work</a></div>''')
        cards = await self.service._cards(self.page)
        self.assertEqual(len(cards), 5)
        self.assertNotIn('99999999999', [c['video_id'] for c in cards])

    async def test_all_rows_preserve_ids_and_report_complete_end_marker(self):
        rows, complete = await self.service._all_rows(self.page)
        self.assertTrue(complete)
        self.assertEqual({r['id'] for r in rows}, {TOP, NESTED, AUTHOR_COMMENT})
        self.assertEqual(next(r for r in rows if r['id'] == NESTED)['parent_id'], TOP)

    async def test_read_video_excludes_nested_and_reply_flagged_retains_author_flag(self):
        await self.fixture(with_reply_flagged=True)
        result = await self.service.read_video('fixture', VIDEO)
        self.assertEqual([c['comment_id'] for c in result['comments']], [TOP, AUTHOR_COMMENT])
        self.assertFalse(result['comments'][0]['is_author'])
        self.assertTrue(result['comments'][1]['is_author'])
        self.assertEqual(result['comments'][1]['text'], '[赞][赞]')
        self.assertEqual(result['nested_rows_skipped'], 2)
        self.assertTrue(result['complete'])
        self.assertEqual(result['sends'], 0)
        self.assertTrue(all(self.service._allowed(c['scope']) for c in result['comments']))

    async def test_read_video_rejects_unconfirmed_owner_even_if_open_was_mocked(self):
        await self.page.locator('[data-e2e="user-info"] p').evaluate("e=>e.textContent='抖音号：some-other-handle'")
        with self.assertRaisesRegex(RuntimeError, 'TARGET_SCOPE_NOT_CONFIRMED'):
            await self.service.read_video('fixture', VIDEO)

    async def test_owner_proof_requires_handle_card_current_video_and_video_author(self):
        edits = (
            "document.querySelector('[data-e2e=user-info] p').textContent='抖音号：another-handle'",
            f"document.querySelector('#own-posts a[href=\"/video/{VIDEO}\"]').remove()",
            "document.querySelector('[data-e2e=feed-video-nickname]').textContent='@其他作者'",
            "document.querySelector('#semiTabpost').setAttribute('aria-selected','false')",
            f"document.querySelector('[data-e2e=video-info]').setAttribute('data-e2e-aweme-id','99999999999')",
        )
        for js in edits:
            with self.subTest(js=js):
                await self.fixture()
                await self.page.evaluate(js)
                with self.assertRaisesRegex(RuntimeError, 'TARGET_SCOPE_NOT_CONFIRMED'):
                    await self.service.read_video('fixture', VIDEO)

    async def test_open_target_rejects_wrong_frozen_cid_before_expanding(self):
        self.service._expand_thread = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'TOP_LEVEL_FROZEN'):
            await self.service._open_target(self.context, self.account, self.frozen_notice(cid='55555555555'))
        self.service._expand_thread.assert_not_awaited()

    async def test_open_target_rejects_nested_and_noncomment_requests(self):
        self.service._expand_thread = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'TOP_LEVEL_FROZEN'):
            await self.service._open_target(self.context, self.account,
                self.frozen_notice(cid=NESTED, profile=NESTED_VIEWER, text='楼中楼不要回复'))
        with self.assertRaisesRegex(RuntimeError, 'OUTSIDE_COMMENT_SCOPE'):
            await self.service._open_target(self.context, self.account, self.frozen_notice(kind='reply'))
        self.service._expand_thread.assert_not_awaited()

    async def test_open_target_rejects_header_reply_even_if_dom_parent_looks_top_level(self):
        await self.fixture(with_reply_flagged=True)
        self.service._expand_thread = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, 'TOP_LEVEL_FROZEN'):
            await self.service._open_target(self.context, self.account,
                self.frozen_notice(cid=REPLY_FLAGGED, profile='/user/reply-fixture', text='回复了别人'))

    async def test_open_target_returns_only_frozen_top_level_comment(self):
        self.service._expand_thread = AsyncMock()
        page, video, rows, target = await self.service._open_target(self.context, self.account, self.frozen_notice())
        self.assertIs(page, self.page)
        self.assertEqual(video, VIDEO)
        self.assertEqual(target['id'], TOP)
        self.assertIsNone(target['parent_id'])
        self.service._expand_thread.assert_awaited_once_with(self.page, TOP)

    async def test_open_target_rechecks_level_after_expansion(self):
        async def reparent(*_):
            await self.page.locator(f'[data-fixture-id="{TOP}"]').evaluate(
                f'e=>document.querySelector(\'[data-fixture-id="{AUTHOR_COMMENT}"] > .fixture-children\').append(e)')
        self.service._expand_thread = AsyncMock(side_effect=reparent)
        with self.assertRaises(RuntimeError):
            await self.service._open_target(self.context, self.account, self.frozen_notice())

    async def test_open_owned_video_rejects_note_and_missing_own_card_without_click(self):
        # Exercise the production ownership-card guard, stopping before clicks.
        real = DouyinOwnedVideoService(self.base)
        real._open_inbox = AsyncMock(return_value=(self.page, None, 'owner-fixture-handle'))
        for video in (NOTE, '99999999999'):
            with self.subTest(video=video), self.assertRaisesRegex(RuntimeError, 'TARGET_NOT_IN_OWN_VIDEOS'):
                await real._open_owned_video(self.context, self.account, video)
        with self.assertRaisesRegex(ValueError, 'INVALID_VIDEO_ID'):
            await real._open_owned_video(self.context, self.account, 'not-an-id')


if __name__ == '__main__':
    unittest.main()
