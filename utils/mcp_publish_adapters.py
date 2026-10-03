"""MCP adapters reuse upload preparation and own the single-submit boundary."""
import uuid
import json
import asyncio
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from utils.publish_errors import DeclarationError

CHINA = timezone(timedelta(hours=8))
XHS_MANAGER = 'https://creator.xiaohongshu.com/new/note-manager'
TENCENT_MANAGER = 'https://channels.weixin.qq.com/platform/post/list'


def tencent_management_titles(plan):
    """The manager displays the description editor, including hashtag chips."""
    normalize = lambda value: re.sub(r'\s+', ' ', value).strip()
    return {normalize(plan['title']), normalize(' '.join([plan['title'], plan['description']])),
            normalize(' '.join([plan['title'], *['#' + tag for tag in plan.get('tags', [])], plan['description']]))}


async def verify_tencent(page, ledger, task_id, max_checks=6):
    task = ledger.get(task_id)
    attempted = datetime.fromisoformat(task['attempted_at'])
    for check in range(max_checks):
        await page.goto('https://channels.weixin.qq.com/platform', wait_until='domcontentloaded', timeout=30000)
        await page.get_by_text('内容管理', exact=True).filter(visible=True).first.wait_for(timeout=15000)
        await page.get_by_text('内容管理', exact=True).filter(visible=True).first.click()
        await page.get_by_text('视频', exact=True).filter(visible=True).first.click()
        await page.locator('.post-feed-item').first.wait_for(timeout=20000)
        cards = await page.locator('.post-feed-item').evaluate_all("""ns=>ns.map(n=>({
            title:n.querySelector('.post-title')?.innerText, text:n.innerText
        }))""")
        # Preserve what the management page actually showed even when matching
        # fails. This is diagnostic evidence, never a publication receipt.
        observation = Path(ledger.path).parent / (task_id + '-management-observation.json')
        observation.write_text(json.dumps(dict(
            task_id=task_id, observed_at=datetime.now(timezone.utc).isoformat(),
            management_url=page.url, expected_title=task['plan']['title'],
            attempted_at=task['attempted_at'], cards=cards,
        ), ensure_ascii=False, indent=2), encoding='utf-8')
        candidates = []
        for card in cards:
            if re.sub(r'\s+', ' ', card['title'] or '').strip() not in tencent_management_titles(task['plan']):
                continue
            text = card['text']
            match = re.search(r'(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s+(\d{1,2}):(\d{2})', text)
            if not match:
                if '处理中' in text:
                    # A matching processing card is not public publication and
                    # has no publish timestamp yet. Keep polling its DOM.
                    candidates.append((None, '处理中', 'SUBMITTED'))
                continue
            stamp = datetime(*map(int, match.groups()), tzinfo=CHINA)
            if not attempted - timedelta(minutes=1) <= stamp <= attempted + timedelta(minutes=10):
                continue
            if '审核未通过' in text:
                label, state = '审核未通过', 'FAILED'
            elif '审核中' in text:
                label, state = '审核中', 'PENDING_REVIEW'
            elif '分享' in text and '可见权限' in text:
                label, state = '视频管理正常作品记录（可分享）', 'PUBLISHED'
            else:
                label, state = '视频管理作品记录', 'SUBMITTED'
            candidates.append((stamp.isoformat(), label, state))
        # Two distinct same-title cards in the same minute are ambiguous.
        if len(candidates) == 1:
            stamp, label, state = candidates[0]
            screenshot = Path(ledger.path).parent / (task_id + '-management-' + uuid.uuid4().hex[:12] + '.png')
            await page.screenshot(path=str(screenshot), full_page=True)
            ledger.record_dom_result(task_id, state, dict(channel='wechat_channels',
                account_id=task['plan']['account_id'], title=task['plan']['title'],
                published_at=stamp, status_text=label, matched_by='title_and_publish_time' if stamp else 'exact_composed_content_processing',
                observed_at=datetime.now(timezone.utc).isoformat(), management_url=TENCENT_MANAGER,
                evidence_ref=str(screenshot)))
            return ledger.get(task_id)
        if check + 1 < max_checks:
            await asyncio.sleep(5)
    raise RuntimeError('UNKNOWN_RESULT')


async def verify_xhs(page, ledger, task_id, max_checks=6):
    task = ledger.get(task_id)
    attempted = datetime.fromisoformat(task['attempted_at'])
    for attempt in range(max_checks):
        def notes_response(response):
            return '/api/galaxy/v2/creator/note/user/posted' in response.url and response.status == 200
        async with page.expect_response(notes_response, timeout=20000) as initial:
            await page.goto(XHS_MANAGER, wait_until='domcontentloaded', timeout=30000)
        await (await initial.value).body()
        for label, state in [('已发布', 'PUBLISHED'), ('审核中', 'PENDING_REVIEW'), ('未通过', 'FAILED')]:
            tab = page.locator('.tab-item').filter(has_text=re.compile('^' + re.escape(label) + '$'))
            async with page.expect_response(notes_response, timeout=15000) as loaded:
                await tab.click(timeout=10000)
            await (await loaded.value).body()
            # Wait for the response's DOM update, not an arbitrary 1.5s delay.
            await page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')
            if 'tab-item--active' not in (await tab.get_attribute('class') or ''):
                continue
            cards = await page.locator('.note-card').evaluate_all('''ns=>ns.map(n=>({
                title:n.querySelector('.note-card__title')?.innerText,
                time:n.querySelector('.note-card__time')?.innerText
            }))''')
            matched = []
            for card in cards:
                if card['title'] != task['plan']['title'][:20] or not card['time']:
                    continue
                try:
                    stamp = datetime.strptime(card['time'], '%Y-%m-%d %H:%M').replace(tzinfo=CHINA)
                except ValueError:
                    continue
                if attempted - timedelta(minutes=1) <= stamp <= attempted + timedelta(minutes=10):
                    matched.append(stamp)
            if len(matched) == 1:
                screenshot = Path(ledger.path).parent / (task_id + '-management-' + uuid.uuid4().hex[:12] + '.png')
                await page.screenshot(path=str(screenshot), full_page=True)
                evidence = dict(channel='xiaohongshu', account_id=task['plan']['account_id'],
                    title=task['plan']['title'], published_at=matched[0].isoformat(),
                    management_url=XHS_MANAGER, observed_at=datetime.now(timezone.utc).isoformat(),
                    matched_by='title_and_publish_time', status_text=label,
                    evidence_ref=str(screenshot), active_filter=label)
                ledger.record_dom_result(task_id, state, evidence)
                return ledger.get(task_id)
        if attempt + 1 < max_checks:
            await asyncio.sleep(5)
    raise RuntimeError('UNKNOWN_RESULT')


async def submit_once(uploader, page, button_name, verifier):
    ledger, task_id = uploader.publish_ledger, uploader.publish_task_id
    task = ledger.get(task_id)
    if task['state'] == 'PUBLISHED':
        return task
    if not task['attempted_at']:
        guard=getattr(uploader,'before_submit_check',None)
        if guard:
            await guard(page)
        button = page.get_by_role('button', name=button_name, exact=True)
        await button.wait_for(state='visible', timeout=15000)
        for _ in range(90):
            if await button.is_enabled():
                break
            await page.wait_for_timeout(1000)
        if not await button.is_enabled():
            await page.screenshot(path=str(Path(ledger.path).parent / (task_id + '-disabled.png')), full_page=True)
            raise RuntimeError('PUBLISH_BUTTON_DISABLED')
        ledger.event(task_id,'UPLOAD_PREPARE','END',round((time.perf_counter()-getattr(uploader,'upload_started',time.perf_counter()))*1000,2))
        if guard:
            await guard(page)
        ledger.before_click(task_id)
        try:
            with ledger.timed(task_id,'SUBMIT_CLICK'):
                await button.click(timeout=10000)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
    check_page = await page.context.new_page()
    try:
        with ledger.timed(task_id,'DOM_VERIFY'):
            return await verifier(check_page, ledger, task_id, max_checks=1)
    except Exception:
        # Capture the ORIGINAL creation page, where validation messages or a
        # second confirmation dialog may still be blocking the publication.
        diagnostic = Path(ledger.path).parent / (task_id + '-after-click-' + uuid.uuid4().hex[:12])
        snapshot = dict(task_id=task_id, observed_at=datetime.now(timezone.utc).isoformat(), url=str(page.url))
        try:
            await page.screenshot(path=str(diagnostic) + '.png', full_page=True)
            snapshot['text'] = str(await page.evaluate('() => document.body.innerText'))
        except Exception as diagnostic_error:
            snapshot['capture_error'] = type(diagnostic_error).__name__ + ': ' + str(diagnostic_error)[:300]
        try:
            Path(str(diagnostic) + '.json').write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError:
            pass  # A full disk must not change the underlying publish result.
        raise
    finally:
        await check_page.close()


def make_xhs_uploader(plan, account):
    from uploader.xiaohongshu_uploader.main import XiaoHongShuVideo

    class McpXhsVideo(XiaoHongShuVideo):
        async def upload(self, playwright):
            self.file_path=str(self.validate_video_file(self.file_path))
            page=await self.session_context.new_page()
            try:
                await asyncio.wait_for(self.upload_video_content(page),timeout=240)
            finally:
                await page.close()

        async def check_original_declaration(self, page):
            if plan['declaration']:
                raise DeclarationError('UNSUPPORTED_EXPLICIT_DECLARATION',
                    requested=plan['declaration'], channel='xiaohongshu')

        async def submit_publish(self, page):
            return await submit_once(self, page, '发布', verify_xhs)

    return McpXhsVideo(title=plan['title'], file_path=plan['media_path'],
        tags=plan['tags'], publish_date=0, account_file=account['session_file'],
        desc=plan['description'], headless=False)


def make_tencent_uploader(plan, account):
    from uploader.tencent_uploader.main import TencentVideo

    class McpTencentVideo(TencentVideo):
        async def apply_original_statement(self, page):
            if plan['declaration']:
                raise DeclarationError('UNSUPPORTED_EXPLICIT_DECLARATION',
                    requested=plan['declaration'], channel='wechat_channels')

        async def submit_publish(self, page):
            return await submit_once(self, page, '发表', verify_tencent)

        async def upload(self, playwright):
            self.file_path = str(self.validate_video_file(self.file_path))
            context = self.session_context
            try:
                page = await context.new_page()
                await self.open_upload_page(page)
                await self.upload_video_file(page, self.file_path)
                await self.prepare_video_for_publish(page)
                await self.wait_for_upload_complete(page, timeout_seconds=180)
                await self.apply_original_statement(page)
                await self.set_short_title(page, self.title, self.short_title)
                await self.submit_publish(page)
            finally:
                if 'page' in locals():
                    await page.close()

    return McpTencentVideo(title=plan['title'], file_path=plan['media_path'],
        tags=plan['tags'], publish_date=0, account_file=account['session_file'],
        desc=plan['description'], short_title=plan['title'], headless=False)


def make_douyin_uploader(plan, account):
    from uploader.douyin_uploader.main import DouYinVideo
    from utils.douyin_publish_result import verify_result

    class McpDouyinVideo(DouYinVideo):
        async def validate_base_args(self):
            if not getattr(self.session_context,'sau_identity_verified',False):
                raise RuntimeError('ACCOUNT_NOT_VERIFIED')
            if self.publish_date != 0:
                raise ValueError('SCHEDULED_PUBLISH_NOT_SUPPORTED')

        async def submit_publish(self, page):
            await self._clear_blocking_overlays(page)
            return await submit_once(self,page,'发布',verify_result)

    return McpDouyinVideo(title=plan['title'],file_path=plan['media_path'],tags=plan['tags'],
        publish_date=0,account_file=account['session_file'],desc=plan['description'],
        declaration=plan['declaration'] or None,headless=False)
