"""Explicit one-work operations through the publisher-owned session only."""
import re
import time
from urllib.parse import urlsplit
from utils.douyin_notifications import DouyinNotificationService, PUBLIC_ROWS_JS, target_row
from utils.douyin_comment_threads import normalize_comment_text


class DouyinOwnedVideoService(DouyinNotificationService):
    async def _cards(self,page):
        tab=page.locator('[data-e2e="user-detail"] #semiTabpost')
        if await tab.count()!=1 or await tab.get_attribute('aria-selected')!='true':raise RuntimeError('OWN_WORKS_TAB_REQUIRED')
        raw=await page.locator('[data-e2e="user-detail"] [data-e2e="scroll-list"] a[href]').filter(visible=True).evaluate_all('nodes=>nodes.map(e=>({url:e.href,href:e.getAttribute("href"),text:e.innerText}))')
        cards=[];seen=set();video_position=0
        for row in raw:
            parts=urlsplit(row['url']);m=re.fullmatch(r'/(video|note)/(\d{10,30})',parts.path)
            if parts.hostname!='www.douyin.com' or not m or m.group(2) in seen:continue
            seen.add(m.group(2))
            if m.group(1)=='video':video_position+=1
            lines=[s.strip() for s in row['text'].splitlines() if s.strip()]
            if lines and re.fullmatch(r'[\d.]+[万亿wW]?',lines[0]):lines=lines[1:]
            cards.append({'position':len(cards)+1,'video_position':video_position if m.group(1)=='video' else None,
                'video_id':m.group(2),'work_type':m.group(1),'title':' '.join(lines)[:1000],'url':row['url'],'href':row['href']})
        return cards

    async def inspect_works(self,account_id):
        account=self.base.sessions.account(account_id)
        with self.base.activity('inspect-own-work-order'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,_,handle=await self._open_inbox(context,account,open_panel=False)
                cards=await self._cards(page)
                snapshot=await self.base._snapshot(page,'owned-work-order')
                return {'account_id':account_id,'account':account['display_name'],'handle':handle,'works':cards,
                    'order':'profile_display_order_newest_first','sends':0,'screenshot_path':snapshot['screenshot_path']}

    async def _open_owned_video(self,context,account,video_id):
        if not re.fullmatch(r'\d{10,30}',video_id):raise ValueError('INVALID_VIDEO_ID')
        page,_,handle=await self._open_inbox(context,account,open_panel=False)
        cards=await self._cards(page)
        matches=[c for c in cards if c['video_id']==video_id and c['work_type']=='video']
        if len(matches)!=1:raise RuntimeError('TARGET_NOT_IN_OWN_VIDEOS')
        card=matches[0]
        # The URL is an observed, verified own-work card; callers cannot supply
        # arbitrary URLs or substitute another account's video.
        anchors=page.locator('[data-e2e="user-detail"] [data-e2e="scroll-list"] a[href]').filter(visible=True)
        positions=await anchors.evaluate_all('(nodes,url)=>nodes.flatMap((e,i)=>e.href===url?[i]:[])',card['url'])
        if len(positions)!=1:raise RuntimeError('OWN_VIDEO_CARD_NOT_UNIQUE')
        anchor=await anchors.nth(positions[0]).element_handle()
        if anchor is None or await anchor.evaluate('e=>e.href')!=card['url']:raise RuntimeError('OWN_VIDEO_CARD_CHANGED')
        previous=set(context.pages)
        await anchor.click(timeout=5000);await page.wait_for_timeout(1200)
        opened=[p for p in context.pages if p not in previous]
        if opened:page=opened[-1]
        await page.locator('[data-e2e="video-info"][data-e2e-aweme-id="'+video_id+'"]').filter(visible=True).wait_for(timeout=20000)
        if self._video_id(page.url)!=video_id:raise RuntimeError('OWN_VIDEO_OPEN_MISMATCH')
        # Short videos can advance to a neighboring work while reading/replying.
        # Pause visible media; the selected work must remain the frozen target.
        await page.locator('video').evaluate_all('nodes=>nodes.forEach(video=>video.pause())')
        comments=page.locator('[data-e2e="comment-list"]').filter(visible=True)
        await page.wait_for_timeout(1200)
        if await comments.count()!=1:
            info=page.locator('[data-e2e="video-info"][data-e2e-aweme-id="'+video_id+'"]')
            player=info.locator('xpath=ancestor::*[.//*[@data-e2e="feed-comment-icon"]][1]')
            entry=player.locator('[data-e2e="feed-comment-icon"]')
            if await entry.count()!=1:
                snapshot=await self.base._snapshot(page,'owned-video-comment-entry-missing')
                raise RuntimeError('COMMENT_ENTRY_NOT_UNIQUE')
            await entry.click(timeout=5000)
        await comments.wait_for(timeout=15000)
        await page.wait_for_timeout(900)
        await self.base._check(page)
        return page,card

    async def _all_rows(self,page):
        area=page.locator('[data-e2e="comment-list"]').filter(visible=True)
        deadline=time.monotonic()+90;seen={};stalled=0;complete=False
        while time.monotonic()<deadline:
            rows=await area.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
            old=set(seen)
            for row in rows:
                if row.get('id'):seen[row['id']]=row
            body=await area.inner_text()
            if '暂时没有更多评论' in body or '暂无评论' in body or '暂无评论，来抢沙发' in body:
                complete=True;break
            stalled=stalled+1 if set(seen)==old else 0
            if stalled>=3:break
            await area.evaluate('e=>{let p=e;while(p&&p.scrollHeight<=p.clientHeight+2)p=p.parentElement;if(p)p.scrollTop=p.scrollHeight}')
            await page.wait_for_timeout(800)
        return list(seen.values()),complete

    async def read_video(self,account_id,video_id):
        account=self.base.sessions.account(account_id)
        with self.base.activity('read-own-video-comments'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,card=await self._open_owned_video(context,account,video_id)
                rows,complete=await self._all_rows(page)
                comments=[];own=self.public_identity(account_id)['profile_path']
                for r in rows:
                    if r['parent_id'] is not None or r['root_id']!=r['id'] or r['reply_to_path']:continue
                    notice={'kind':'comment','author':r['author'],'text':normalize_comment_text(r['text']),'source_profile_path':r['profile_path']}
                    scope=await self._scope_proof(page,account,notice,video_id,r)
                    if not self._allowed(scope):raise RuntimeError('TARGET_SCOPE_NOT_CONFIRMED')
                    comments.append({'comment_id':r['id'],'author':r['author'],'text':normalize_comment_text(r['text']),
                        'profile_path':r['profile_path'],'is_author':r['profile_path']==own,'scope':scope})
                snapshot=await self.base._snapshot(page,'owned-video-comments')
                return {'account_id':account_id,'video_id':video_id,'card':card,'comments':comments,'complete':complete,
                    'all_rows_seen':len(rows),'nested_rows_skipped':len(rows)-len(comments),'sends':0,'screenshot_path':snapshot['screenshot_path']}

    async def _open_target(self,context,account,notice):
        if notice.get('kind')!='comment':raise RuntimeError('TARGET_OUTSIDE_COMMENT_SCOPE')
        raw=notice.get('raw',{});video_id=raw.get('video_id');cid=raw.get('comment_id')
        page,card=await self._open_owned_video(context,account,video_id)
        rows,_=await self._all_rows(page)
        target=target_row(rows,notice)
        if target['id']!=cid or target['parent_id'] is not None or target['root_id']!=cid or target.get('reply_to_path'):raise RuntimeError('TARGET_NOT_TOP_LEVEL_FROZEN_COMMENT')
        await self._expand_thread(page,cid)
        rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
        target=target_row(rows,notice)
        if target['id']!=cid or target['parent_id'] is not None or target['root_id']!=cid or target.get('reply_to_path'):raise RuntimeError('TARGET_COMMENT_CHANGED')
        return page,video_id,rows,target
