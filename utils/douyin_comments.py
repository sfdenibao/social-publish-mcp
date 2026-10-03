"""Narrow service-owned DOM operations for a previously published Douyin work.

Browser sessions remain inside the publisher. No cookie/profile paths or arbitrary
browser commands are accepted from the caller.
"""
import asyncio
import json
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

from utils.douyin_publish_result import MANAGEMENT_URL, match_record
from utils.publish_ledger import now
from utils.comment_ledger import CommentLedger


def is_owned_comment_row(row_text, comment_text, account_name):
    lines = [line.strip() for line in row_text.splitlines() if line.strip()]
    return lines[:2] == [account_name, '作者'] and comment_text in lines[2:]


class DouyinCommentService:
    def __init__(self, ledger, sessions, runtime):
        self.ledger = ledger
        self.sessions = sessions
        self.directory = Path(runtime) / 'comment-tests'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.comments = CommentLedger(self.directory / 'comments.sqlite3')
        self._active = set()
        self._guard = threading.Lock()

    @contextmanager
    def activity(self, name):
        token = name + ':' + uuid.uuid4().hex[:8]
        with self._guard:
            self._active.add(token)
        try:
            yield
        finally:
            with self._guard:
                self._active.discard(token)

    def active_operations(self):
        with self._guard:
            return sorted(self._active)

    def prepare(self, publish_task_id, text, idempotency_key):
        task, account = self.published_task(publish_task_id)
        return self.comments.prepare(account['id'], task['id'], text, idempotency_key)

    def published_task(self, task_id):
        task = self.ledger.get(task_id)
        if task['state'] != 'PUBLISHED' or task['plan']['channel'] != 'douyin':
            raise ValueError('PUBLISHED_DOUYIN_TASK_REQUIRED')
        if not task.get('attempted_at'):
            raise ValueError('PUBLICATION_EVIDENCE_REQUIRED')
        account = self.sessions.account(task['plan']['account_id'])
        if account['channel'] != 'douyin':
            raise ValueError('ACCOUNT_CHANNEL_MISMATCH')
        return task, account

    async def _check(self, page):
        blocker = await self.sessions.page_blocker(page)
        if blocker:
            raise RuntimeError(blocker)

    async def _work_card(self, page, task):
        await page.goto(MANAGEMENT_URL, wait_until='domcontentloaded', timeout=30000)
        await self._check(page)
        cards = page.locator('[class*="video-card-info-"]')
        await cards.first.wait_for(timeout=20000)
        matches = []
        for index in range(await cards.count()):
            card = cards.nth(index)
            record = await card.evaluate('''node => ({
              title: node.querySelector('[class*="info-title-text-"]')?.innerText?.trim() || '',
              text: node.innerText || ''
            })''')
            plan = task['plan']
            if match_record([record], plan['title'], task['attempted_at'],
                            plan.get('description', ''), plan.get('tags')):
                matches.append(card)
        if len(matches) != 1:
            raise RuntimeError('WORK_NOT_UNIQUELY_MATCHED')
        return matches[0]

    async def _open_comments(self, context, task, expected_video_id=None):
        page = await context.new_page()
        card = await self._work_card(page, task)
        previous_pages = set(context.pages)
        await card.locator('[data-comment-entry-guide-target="true"]').click(timeout=5000)
        for _ in range(30):
            opened = [candidate for candidate in context.pages if candidate not in previous_pages]
            if opened:
                page = opened[-1]
                break
            if '/work-detail/' in page.url:
                break
            await asyncio.sleep(.1)
        try:
            await page.wait_for_url(re.compile(r'/work-detail/\d+.*tabKey=commentManagement'), wait_until='domcontentloaded', timeout=20000)
        except Exception:
            diagnostic=await self._snapshot(page,'open-comments-failure')
            (self.directory/'latest-open-comments-failure.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),encoding='utf-8')
            raise
        if expected_video_id is not None:
            if not re.fullmatch(r'\d{10,30}', expected_video_id):
                raise ValueError('INVALID_EXPECTED_VIDEO_ID')
            actual=re.search(r'/work-detail/(\d+)', page.url)
            if not actual or actual.group(1)!=expected_video_id:
                raise RuntimeError('WORK_ID_MISMATCH')
        await page.get_by_text('加载中，请稍候...', exact=True).wait_for(state='hidden', timeout=20000)
        await page.locator('[contenteditable="true"][class*="editable-input-"]').wait_for(timeout=20000)
        await page.get_by_text('全部评论', exact=True).wait_for(timeout=10000)
        await asyncio.sleep(1)
        await self._check(page)
        return page

    async def _matching_comments(self, page, text, account_name):
        nodes = page.get_by_text(text, exact=True).filter(visible=True)
        matches = []
        for index in range(await nodes.count()):
            item = await nodes.nth(index).evaluate('''e => {
              if(e.isContentEditable || e.closest('[contenteditable="true"]')) return null;
              const p=e.closest('[class*="comment-main-"]');
              if(!p)return null;
              return {text:e.innerText,row:{text:p.innerText,tag:p.tagName.toLowerCase(),
                class_name:p.className,id:p.id||null,data_id:p.getAttribute('data-id')}};
            }''')
            if item and is_owned_comment_row(item['row']['text'],text,account_name):
                matches.append(dict(text=text, account=account_name, row=item['row']))
        return matches

    async def post(self, comment_task_id, expected_video_id=None):
        request = self.comments.get(comment_task_id)
        if request['attempted_at'] or request['state'] in {'VERIFIED','ALREADY_PRESENT'}:
            return request
        task, account = self.published_task(request['work_id'])
        if request['account_id'] != account['id']:
            raise ValueError('ACCOUNT_CHANGED')
        with self.activity('post'):
            try:
                async with self.sessions.open(account['id'], headless=False) as context:
                    identity=await self.sessions.probe(context, account['id'])
                    if identity['status']!='READY':
                        raise RuntimeError(identity['status'])
                    page=await self._open_comments(context, task, expected_video_id)
                    present=await self._matching_comments(page, request['text'], account['display_name'])
                    if present:
                        evidence=await self._snapshot(page,'already-present')
                        evidence.update(matches=present,verified_after_reload=False)
                        return self.comments.record(request['id'],'ALREADY_PRESENT',evidence)
                    editor=page.locator('[contenteditable="true"][class*="editable-input-"]').filter(visible=True)
                    send=page.get_by_role('button',name='发送',exact=True).filter(visible=True)
                    if await editor.count()!=1 or await send.count()!=1:
                        raise RuntimeError('COMMENT_COMPOSER_NOT_UNIQUE')
                    await editor.fill(request['text'])
                    if await editor.inner_text()!=request['text'] or not await send.is_enabled():
                        raise RuntimeError('COMMENT_TEXT_OR_BUTTON_NOT_READY')
                    await self._check(page)
                    identity=await self.sessions.probe(context,account['id'])
                    if identity['status']!='READY':
                        raise RuntimeError(identity['status'])
                    before=await self._snapshot(page,'before-submit')
                    self.comments.mark_attempted(request['id'])
                    # This is the only submit click. Its permission is durably consumed above.
                    await send.click(timeout=5000)
                    await asyncio.sleep(2)
                    after=await self._snapshot(page,'after-submit')
                    await page.reload(wait_until='domcontentloaded',timeout=30000)
                    await page.get_by_text('加载中，请稍候...',exact=True).wait_for(state='hidden',timeout=20000)
                    await page.locator('[contenteditable="true"][class*="editable-input-"]').wait_for(timeout=20000)
                    await asyncio.sleep(2)
                    matches=await self._matching_comments(page,request['text'],account['display_name'])
                    final=await self._snapshot(page,'after-reload')
                    final.update(matches=matches,verified_after_reload=len(matches)==1,
                                 before_screenshot=before['screenshot_path'],after_screenshot=after['screenshot_path'])
                    return self.comments.record(request['id'],'VERIFIED' if len(matches)==1 else 'UNKNOWN_RESULT',final)
            except Exception as exc:
                current=self.comments.get(request['id'])
                if current['attempted_at']:
                    return self.comments.record(request['id'],'UNKNOWN_RESULT',{'error_type':type(exc).__name__,
                                               'message':str(exc)[:500],'observed_at':now()})
                return self.comments.fail_before_attempt(request['id'],' '.join(str(exc).split())[:1200] or type(exc).__name__)

    async def recheck(self, comment_task_id):
        request=self.comments.get(comment_task_id)
        if not request['attempted_at']:
            return request
        task,account=self.published_task(request['work_id'])
        if request['account_id'] != account['id']:
            raise ValueError('ACCOUNT_CHANGED')
        with self.activity('recheck'):
            async with self.sessions.open(account['id'],headless=False) as context:
                identity=await self.sessions.probe(context,account['id'])
                if identity['status']!='READY':
                    raise RuntimeError(identity['status'])
                page=await self._open_comments(context,task)
                matches=await self._matching_comments(page,request['text'],account['display_name'])
                evidence=await self._snapshot(page,'recheck')
                evidence.update(matches=matches,verified_after_reload=len(matches)==1)
                if len(matches)==1:
                    return self.comments.record(request['id'],'VERIFIED',evidence)
                return dict(**request,recheck_state='NOT_OBSERVED',recheck_evidence=evidence)

    async def _snapshot(self, page, prefix):
        await self._check(page)
        image_path = self.directory / f'{prefix}-{uuid.uuid4().hex[:10]}.png'
        await page.screenshot(path=str(image_path), full_page=False)
        visible = await page.evaluate('''() => {
          const vis = e => !!(e.getClientRects().length) && getComputedStyle(e).visibility !== 'hidden';
          const fields = [...document.querySelectorAll('input,textarea,[contenteditable="true"],button,[role="button"],[role="textbox"]')]
            .filter(vis).map(e => ({tag:e.tagName.toLowerCase(), role:e.getAttribute('role'),
              type:e.getAttribute('type'), text:(e.innerText||'').trim().slice(0,120),
              placeholder:e.getAttribute('placeholder'), aria_label:e.getAttribute('aria-label'),
              class_name:e.className, data_e2e:e.getAttribute('data-e2e')})).slice(0,100);
          const links = [...document.querySelectorAll('a[href]')].filter(vis)
            .map(e => ({text:(e.innerText||'').trim().slice(0,100),href:e.href})).slice(0,60);
          const comment_rows=[...document.querySelectorAll('[class*="comment-main-"]')].filter(vis).slice(0,30)
            .map(e=>({text:e.innerText,markup:e.outerHTML.slice(0,14000)}));
          return {text:document.body.innerText.slice(0,16000),fields,links,comment_rows};
        }''')
        return dict(url=page.url, observed_at=now(), screenshot_path=str(image_path), **visible)

    async def inspect(self, publish_task_id, open_work=False, open_comments=False, public_view=False):
        task, account = self.published_task(publish_task_id)
        async with self.sessions.open(account['id'], headless=False) as context:
            identity = await self.sessions.probe(context, account['id'])
            if identity['status'] != 'READY':
                raise RuntimeError(identity['status'])
            page = await context.new_page()
            card = await self._work_card(page, task)
            scoped_dom = await card.evaluate('''e => ({text:e.innerText,
              markup:e.parentElement.outerHTML.slice(0,18000),
              links:[...e.parentElement.querySelectorAll('a[href]')].map(a=>({text:a.innerText,href:a.href}))})''')
            if open_work or open_comments or public_view:
                old_pages = set(context.pages)
                if open_comments and not public_view:
                    await card.locator('[data-comment-entry-guide-target="true"]').click(timeout=5000)
                else:
                    await card.locator('[class*="info-title-text-"]').click(timeout=5000)
                await asyncio.sleep(1)
                opened = [p for p in context.pages if p not in old_pages]
                if opened:
                    page = opened[-1]
                await page.wait_for_load_state('domcontentloaded', timeout=20000)
                await page.get_by_text('加载中，请稍候...', exact=True).wait_for(state='hidden', timeout=20000)
                if public_view:
                    match = re.search(r'/work-detail/(\d+)', page.url)
                    if not match:
                        raise RuntimeError('VERIFIED_WORK_ID_UNAVAILABLE')
                    await page.goto('https://www.douyin.com/video/' + match.group(1), wait_until='domcontentloaded', timeout=30000)
                    await asyncio.sleep(4)
                else:
                    await asyncio.sleep(2)
            result = await self._snapshot(page, 'inspect')
            if not (open_work or open_comments or public_view):
                result['text'] = scoped_dom['text']
            return dict(status='INSPECTED', account=account['display_name'],
                        publish_task_id=publish_task_id, title=task['plan']['title'],
                        work_opened=bool(open_work or open_comments or public_view), work_card=scoped_dom, **result)

    async def inspect_analytics(self,publish_task_id,expected_video_id):
        """Read only the known published work's analytics tabs; no export/edit/post."""
        task,account=self.published_task(publish_task_id)
        with self.activity('inspect-work-analytics'):
            async with self.sessions.open(account['id'],headless=False) as context:
                identity=await self.sessions.probe(context,account['id'])
                if identity['status']!='READY':raise RuntimeError(identity['status'])
                page=await context.new_page();card=await self._work_card(page,task)
                previous=set(context.pages)
                await card.locator('[class*="info-title-text-"]').click(timeout=5000)
                await page.wait_for_timeout(1200)
                opened=[p for p in context.pages if p not in previous]
                if opened:page=opened[-1]
                await page.wait_for_load_state('domcontentloaded',timeout=20000)
                if not re.search('/work-detail/'+re.escape(expected_video_id)+r'(?:\?|$)',page.url):raise RuntimeError('ANALYTICS_VIDEO_MISMATCH')
                await page.get_by_text('加载中，请稍候...',exact=True).wait_for(state='hidden',timeout=20000)
                await page.wait_for_timeout(1300)
                result={'account':account['display_name'],'video_id':expected_video_id,'publish_task_id':publish_task_id,'tabs':{}}
                for label in ('总览','流量分析','观众分析','评论热词'):
                    tab=page.get_by_text(label,exact=True).filter(visible=True)
                    if await tab.count()!=1:
                        result['tabs'][label]={'status':'NOT_AVAILABLE'};continue
                    await tab.click(timeout=5000);await page.wait_for_timeout(1300)
                    result['tabs'][label]=await self._snapshot(page,'analytics-'+str(len(result['tabs'])))
                return result
