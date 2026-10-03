"""Reply only to top-level comments on positively verified owned videos.

Uses the existing publisher session; never exports session data or accepts
arbitrary browser scripts/URLs from callers.
"""
import re
import json
import hashlib
import time
from urllib.parse import urlsplit, parse_qs
from utils.douyin_comment_threads import normalize_comment_text
from utils.publish_sessions import HOME
from utils.douyin_comment_threads import same_reply_text, EDITOR_TEXT_JS

PUBLIC_ROWS_JS = '''nodes=>nodes.map((e,index)=>{
 const read=n=>n.nodeType===Node.TEXT_NODE?n.textContent:(n.nodeType===Node.ELEMENT_NODE&&n.tagName==='IMG'?(n.getAttribute('alt')||''):[...n.childNodes].map(read).join(''));
 const body=e.querySelector('.FduGc_lz');const user=e.querySelector('.comment-item-info-wrap a');
 const id=e.querySelector('[data-e2e="video-comment-more"] [id^="tooltip_"]')?.id?.replace('tooltip_','');
 const parent=e.parentElement?.closest('[data-e2e="comment-item"]');
 let root=e;while(root.parentElement?.closest('[data-e2e="comment-item"]'))root=root.parentElement.closest('[data-e2e="comment-item"]');
 const cid=n=>n?.querySelector('[data-e2e="video-comment-more"] [id^="tooltip_"]')?.id?.replace('tooltip_','');
 const names=e.querySelector('.comment-item-info-wrap .vlTNLkdG');const namesLinks=names?.querySelectorAll(':scope > a');
 const recipient=namesLinks?.length===2&&names.querySelector(':scope > .ym1IYI4C')?namesLinks[1]:null;
 return {index,id,author:user?.querySelector('[data-click-from="title"]')?.innerText||user?.innerText||'',profile_path:user?new URL(user.href).pathname:'',text:body?read(body).trim():'',parent_id:cid(parent)||null,root_id:cid(root),reply_to_path:recipient?new URL(recipient.href).pathname:null};
})'''

# Public identity observed in this account's own profile and author-badged work.
# No credential or hidden page state is stored here.
PUBLIC_IDENTITIES = {}  # Optional in-process overrides; normally use account.public_identity.


def target_row(rows,notice):
    matches=[r for r in rows if r['profile_path']==notice['source_profile_path'] and same_reply_text(r['text'],notice['text'])]
    if len(matches)!=1 or not re.fullmatch(r'\d{10,30}',matches[0].get('id') or ''):
        raise RuntimeError('PUBLIC_COMMENT_TARGET_NOT_UNIQUE')
    return matches[0]


def composer_matches(label,target):
    prefix='回复@'+target['author']+':'
    value=normalize_comment_text(label)
    if not value.startswith(prefix):return False
    return same_reply_text(value[len(prefix):].strip(),target['text'])


class DouyinNotificationService:
    def __init__(self, base):
        self.base = base
        self.known_keys = lambda account_id:set()

    def public_identity(self, account_id):
        identity = self.base.sessions.account(account_id).get('public_identity') or PUBLIC_IDENTITIES.get(account_id)
        if not isinstance(identity, dict) or not identity.get('handle') or not re.fullmatch(r'/user/[A-Za-z0-9_-]+', identity.get('profile_path', '')):
            raise RuntimeError('DOUYIN_PUBLIC_IDENTITY_NOT_PINNED')
        return identity

    @staticmethod
    def _video_id(url):
        parts=urlsplit(url)
        if parts.hostname!='www.douyin.com':return ''
        value=parse_qs(parts.query).get('modal_id',[''])[0]
        if not value:
            match=re.search(r'/(?:video|note)/(\d+)',parts.path)
            value=match.group(1) if match else ''
        return value if re.fullmatch(r'\d{10,30}',value) else ''

    async def _open_inbox(self,context,account,expected_handle=None,*,open_panel=True):
        identity=await self.base.sessions.probe(context,account['id'])
        if identity['status']!='READY':raise RuntimeError(identity['status'])
        page=await context.new_page()
        await page.goto('https://www.douyin.com/user/self',wait_until='domcontentloaded',timeout=30000)
        await page.wait_for_function("name => document.body && document.body.innerText.includes(name) && /抖音号[:：]\\s*[^\\s]+/.test(document.body.innerText)",arg=account['display_name'],timeout=25000)
        await page.wait_for_timeout(1500)
        prompt=page.get_by_text(re.compile('是否保存登录信息')).filter(visible=True)
        if await prompt.count():
            cancel=page.get_by_text('取消',exact=True).filter(visible=True)
            if await cancel.count()==1:await cancel.click(timeout=5000)
        body=await page.locator('body').inner_text()
        profile=page.locator('[data-e2e="user-info"]')
        if await profile.count()!=1 or normalize_comment_text(await profile.locator('h1').inner_text())!=account['display_name']:
            raise RuntimeError('DOUYIN_WEB_ACCOUNT_NOT_CONFIRMED')
        match=re.search(r'抖音号[:：]\s*([^\s]+)',await profile.inner_text())
        if not match:raise RuntimeError('DOUYIN_HANDLE_NOT_VISIBLE')
        handle=match.group(1)
        pinned=self.public_identity(account['id'])
        if not pinned or handle!=pinned['handle']:raise RuntimeError('DOUYIN_PUBLIC_IDENTITY_NOT_PINNED')
        if expected_handle and expected_handle!=handle:raise RuntimeError('DOUYIN_WEB_ACCOUNT_CHANGED')
        if not open_panel:return page,None,handle
        entry=page.locator('[data-e2e="something-button"]').filter(has_text=re.compile(r'^通知(?:\s*\d+)?$')).filter(visible=True)
        if await entry.count()!=1:raise RuntimeError('NOTIFICATION_ENTRY_NOT_UNIQUE')
        await entry.click(timeout=5000)
        panel=page.locator('[data-e2e="listDlgTest-container"]')
        await panel.wait_for(timeout=15000)
        await page.wait_for_timeout(1000)
        await self.base._check(page)
        return page,panel,handle

    async def _read_rows(self,panel,handle):
        rows=await panel.locator(':scope > .sKMx4DKd > .v4ybwR9T').evaluate_all('''nodes=>nodes.map((e,index)=>{
          const name=e.querySelector('[data-e2e="user-name-card"]');
          const pre=e.querySelector('pre');const relation=e.querySelector('.Q9Ls5TTX');
          const content=pre?.innerText||'';const text=e.innerText;
          let kind='other';if(text.includes('评论了你的作品'))kind='comment';else if(text.includes('回复了你的评论'))kind='reply';
          else if(text.includes('赞了你的'))kind='likes';else if(text.includes('关注了你'))kind='follow';
          const t=relation?.querySelectorAll('span');
          return {dom_index:index,author:name?.innerText||'',text:content||e.querySelector('.nxdtZKjw')?.innerText||'',kind,
            display_time:t?.length>1?t[1].innerText:(e.querySelector('.KW6kd8QX')?.innerText||''),
            profile_href:name?.closest('a')?.href||'',cover:e.querySelector('img.A2FRf3V9')?.src||'',row_text:text};
        })''')
        result=[]
        for row in rows:
            actor_path=urlsplit(row['profile_href']).path
            cover_path=urlsplit(row['cover']).path
            if not row['author'] or not actor_path.startswith('/user/'):
                continue
            text=normalize_comment_text(row['text'])
            key=hashlib.sha256(json.dumps([actor_path,row['kind'],text,cover_path],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            result.append(dict(notification_key=key,author=row['author'],text=text,kind=row['kind'],
                display_time=row['display_time'],source_profile_path=actor_path,cover_path=cover_path,
                raw={'dom_index':row['dom_index'],'self_handle':handle,'row_text':row['row_text']}))
        return result

    async def scan(self,account_id):
        account=self.base.sessions.account(account_id)
        if account['channel']!='douyin':raise ValueError('DOUYIN_ACCOUNT_REQUIRED')
        with self.base.activity('scan-notifications'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,panel,handle=await self._open_inbox(context,account)
                notices=await self._read_rows(panel,handle)
                known=set(self.known_keys(account_id))
                # First enrollment uses current notifications. Later runs continue
                # through pages until reaching the existing watermark, with no
                # reply-count limit. A stalled UI is a failed scan, not truncation.
                deadline=time.monotonic()+120
                while known and not any(n['notification_key'] in known for n in notices):
                    more=panel.get_by_text('点击加载更多',exact=True)
                    if not await more.count():break
                    old={n['notification_key'] for n in notices}
                    await more.click(timeout=5000);await page.wait_for_timeout(1000)
                    notices=await self._read_rows(panel,handle)
                    if old=={n['notification_key'] for n in notices} or time.monotonic()>deadline:
                        raise RuntimeError('NOTIFICATION_PAGINATION_STALLED')
                snapshot=await self.base._snapshot(page,'notifications-scan')
                return {'account_id':account_id,'account':account['display_name'],'self_handle':handle,
                        'notices':notices,'has_older':bool(await panel.get_by_text('点击加载更多',exact=True).count()),
                        'scope':'visible_notification_page','private_messages_read':False,
                        'evidence':{'url':page.url,'observed_at':snapshot['observed_at'],'screenshot_path':snapshot['screenshot_path']}}

    async def _open_target(self,context,account,notice):
        if notice.get('kind')!='comment':raise RuntimeError('TARGET_OUTSIDE_COMMENT_SCOPE')
        page,panel,handle=await self._open_inbox(context,account,notice.get('raw',{}).get('self_handle'))
        deadline=time.monotonic()+120
        while True:
            notices=await self._read_rows(panel,handle)
            matches=[n for n in notices if n['notification_key']==notice['notification_key']]
            if len(matches)==1:break
            if len(matches)>1:raise RuntimeError('NOTIFICATION_NOT_UNIQUE')
            more=panel.get_by_text('点击加载更多',exact=True)
            if not await more.count() or time.monotonic()>deadline:raise RuntimeError('NOTIFICATION_NOT_VISIBLE')
            previous_keys={n['notification_key'] for n in notices}
            await more.click(timeout=5000);await page.wait_for_timeout(1000)
            if previous_keys=={n['notification_key'] for n in await self._read_rows(panel,handle)}:
                raise RuntimeError('NOTIFICATION_PAGINATION_STALLED')
        actual=matches[0]
        if actual['kind'] not in {'comment','reply'}:raise RuntimeError('NOT_REPLYABLE')
        if any(actual[k]!=notice[k] for k in ('author','text','source_profile_path','cover_path','kind')):
            raise RuntimeError('NOTIFICATION_IDENTITY_CHANGED')
        row=panel.locator(':scope > .sKMx4DKd > .v4ybwR9T').nth(actual['raw']['dom_index'])
        node=await row.locator('pre').element_handle()
        # Match the complete current row immediately before the click.
        if node is None or normalize_comment_text(await node.inner_text())!=notice['text']:
            raise RuntimeError('NOTIFICATION_CLICK_TARGET_CHANGED')
        bound=await node.evaluate('''e=>{const row=e.closest('.v4ybwR9T');const name=row?.querySelector('[data-e2e="user-name-card"]');return {connected:e.isConnected,profile:name?.closest('a')?new URL(name.closest('a').href).pathname:null,author:name?.innerText}}''')
        if not bound['connected'] or bound['profile']!=notice['source_profile_path'] or bound['author']!=notice['author']:
            raise RuntimeError('NOTIFICATION_CLICK_TARGET_CHANGED')
        previous=set(context.pages)
        await node.click(timeout=5000);await page.wait_for_timeout(1500)
        opened=[p for p in context.pages if p not in previous]
        if opened:page=opened[-1]
        await page.locator('[data-e2e="comment-list"]').wait_for(timeout=20000)
        await page.locator('video').evaluate_all('nodes=>nodes.forEach(video=>video.pause())')
        await page.wait_for_timeout(1000)
        await self.base._check(page)
        video_id=self._video_id(page.url)
        if not re.fullmatch(r'\d{10,30}',video_id):raise RuntimeError('NOTIFICATION_WORK_NOT_BOUND')
        rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
        target=target_row(rows,notice)
        return page,video_id,rows,target

    async def _scope_proof(self,page,account,notice,video_id,target):
        """Use the authenticated own-works grid, never display-name equality alone."""
        top=target.get('parent_id') is None and target.get('root_id')==target.get('id') and not target.get('reply_to_path')
        proof={'is_own_video':None,'is_top_level':top,'work_type':None,'scope_version':'own-video-top-level-v1'}
        if notice.get('kind')!='comment' or not top:
            return {**proof,'is_own_video':False,'scope_reason':'NOT_TOP_LEVEL_NOTIFICATION'}
        if self._video_id(page.url)!=video_id:return {**proof,'scope_reason':'WORK_CHANGED'}
        identity=self.public_identity(account['id'])
        profile=page.locator('[data-e2e="user-detail"] [data-e2e="user-info"]')
        if await profile.count()!=1:return {**proof,'scope_reason':'SELF_PROFILE_NOT_VISIBLE'}
        name=await profile.locator('h1').inner_text()
        handle=re.search(r'抖音号[:：]\s*([^\s]+)',await profile.inner_text())
        if normalize_comment_text(name)!=account['display_name'] or not handle or handle.group(1)!=identity['handle']:
            return {**proof,'scope_reason':'SELF_PROFILE_NOT_CONFIRMED'}
        post_tab=page.locator('[data-e2e="user-detail"] #semiTabpost')
        if await post_tab.count()!=1 or await post_tab.get_attribute('aria-selected')!='true':
            return {**proof,'scope_reason':'OWN_WORKS_TAB_NOT_SELECTED'}
        # Observed own-works cards. Likes/recommendations or an arbitrary public
        # video link elsewhere on the page cannot prove ownership.
        links=await page.locator('[data-e2e="user-detail"] [data-e2e="scroll-list"] a[href]').filter(visible=True).evaluate_all('nodes=>nodes.map(e=>e.href)')
        own_paths={urlsplit(url).path for url in links if urlsplit(url).hostname=='www.douyin.com'}
        if '/note/'+video_id in own_paths:return {**proof,'work_type':'note','is_own_video':True,'scope_reason':'NOT_A_VIDEO'}
        if '/video/'+video_id not in own_paths:return {**proof,'scope_reason':'VIDEO_NOT_IN_VERIFIED_OWN_WORKS'}
        info=page.locator('[data-e2e="video-info"][data-e2e-aweme-id="'+video_id+'"]').filter(visible=True)
        if await info.count()!=1:return {**proof,'scope_reason':'CURRENT_VIDEO_INFO_NOT_UNIQUE'}
        owner=info.locator('[data-e2e="feed-video-nickname"]')
        if await owner.count()!=1 or normalize_comment_text(await owner.inner_text())!='@'+account['display_name']:
            return {**proof,'is_own_video':False,'work_type':'video','scope_reason':'VIDEO_AUTHOR_MISMATCH'}
        return {**proof,'is_own_video':True,'work_type':'video','owner_profile_path':identity['profile_path'],
                'owned_work_url':'https://www.douyin.com/video/'+video_id,'scope_reason':'VERIFIED_OWN_WORKS_CARD_AND_VIDEO'}

    @staticmethod
    def _allowed(proof):
        return proof.get('is_own_video') is True and proof.get('is_top_level') is True and proof.get('work_type')=='video'

    async def _bound_row(self,page,notice,expected_id):
        rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
        target=target_row(rows,notice)
        if target['id']!=expected_id:raise RuntimeError('PUBLIC_COMMENT_ID_CHANGED')
        # Ancestor selector from a unique DOM comment ID, never an nth retarget.
        node=page.locator(f'[id="tooltip_{expected_id}"]').locator('xpath=ancestor::*[@data-e2e="comment-item"][1]')
        if await node.count()!=1 or not await node.is_visible():raise RuntimeError('COMMENT_NODE_NOT_UNIQUE')
        return node,target,rows

    async def read_context(self,account_id,notice):
        if notice.get('kind')!='comment':
            return {'comment':notice['text'],'interaction_type':'notification_reply','is_own_video':False,'is_top_level':False,'work_type':'unknown'}
        account=self.base.sessions.account(account_id)
        with self.base.activity('notification-context'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,video_id,rows,target=await self._open_target(context,account,notice)
                scope=await self._scope_proof(page,account,notice,video_id,target)
                if not self._allowed(scope):return {'comment':notice['text'],**scope}
                own=self.public_identity(account_id)['profile_path']
                thread=[r for r in rows if r['root_id']==target['root_id']]
                parent=next((r for r in rows if r['id']==target['parent_id']),None)
                post=page.locator('[data-e2e="video-info"][data-e2e-aweme-id="'+video_id+'"] [data-e2e="video-desc"]').filter(visible=True)
                description=await post.inner_text() if await post.count()==1 else ''
                answered=any(r['profile_path']==own and r['id']!=target['id'] and
                    ((target['parent_id'] is None and r['parent_id']==target['id']) or
                     r['reply_to_path']==target['profile_path']) for r in thread)
                return {'comment':notice['text'],'interaction_type':'video_comment',**scope,
                    'video_id':video_id,'comment_id':target['id'],'root_id':target['root_id'],
                    'post_title':description.splitlines()[0] if description else '', 'post_description':description[:2000],
                    'parent_comment':parent['text'] if parent else '',
                    'thread_context':[{'text':r['text'],'is_own':r['profile_path']==own} for r in thread[:8]],
                    'existing_self_ids':[r['id'] for r in thread if r['profile_path']==own],
                    'is_author':target['profile_path']==own,'already_replied':answered,'self_handle':self.public_identity(account_id)['handle']}

    async def _readback(self,page,notice,target,text,own,previous_ids):
        rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
        return [r for r in rows if r['profile_path']==own and r['root_id']==target['root_id'] and
                r['id'] not in previous_ids and r['id']!=target['id'] and same_reply_text(r['text'],text) and
                ((target['parent_id'] is None and r['parent_id']==target['id'] and not r['reply_to_path']) or
                 (r['reply_to_path']==target['profile_path'] and r['parent_id']==target['root_id']))]

    async def _expand_thread(self,page,root_id):
        root=page.locator(f'[id="tooltip_{root_id}"]').locator('xpath=ancestor::*[@data-e2e="comment-item"][1]')
        deadline=time.monotonic()+30
        while True:
            expand=root.get_by_role('button',name=re.compile('展开.*条回复'))
            if await expand.count()!=1:break
            before=await root.locator('[data-e2e="comment-item"]').count()
            await expand.click(timeout=5000);await page.wait_for_timeout(800)
            after=await root.locator('[data-e2e="comment-item"]').count()
            if after<=before or time.monotonic()>deadline:break

    async def reply(self,account_id,notice,text,before_submit,*,recheck_only=False):
        if not isinstance(text,str) or not 1<=len(text)<=100 or any(ord(c)<32 for c in text):raise ValueError('INVALID_REPLY_TEXT')
        if notice.get('kind')!='comment':return {'status':'SKIP','reason':'NOT_TOP_LEVEL_NOTIFICATION'}
        if not recheck_only and not self._allowed(notice.get('context',{})):
            return {'status':'SKIP','reason':'PREPARED_SCOPE_NOT_VERIFIED'}
        account=self.base.sessions.account(account_id)
        attempted=False
        with self.base.activity('notification-recheck' if recheck_only else 'notification-reply'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,video_id,rows,target=await self._open_target(context,account,notice)
                scope=await self._scope_proof(page,account,notice,video_id,target)
                if not self._allowed(scope):return {'status':'SKIP','reason':'OUTSIDE_OWN_VIDEO_TOP_LEVEL_SCOPE','scope':scope}
                saved=notice.get('context',{})
                if any(saved.get(k)!=v for k,v in [('video_id',video_id),('comment_id',target['id']),('root_id',target['root_id'])]):
                    raise RuntimeError('PREPARED_NOTIFICATION_TARGET_CHANGED')
                own=self.public_identity(account_id)['profile_path']
                if target['profile_path']==own:return {'status':'SKIP','reason':'OWN_COMMENT'}
                if recheck_only:
                    await self._expand_thread(page,target['root_id'])
                prior=await self._readback(page,notice,target,text,own,saved.get('existing_self_ids',[]))
                if prior or recheck_only:
                    snapshot=await self.base._snapshot(page,'notification-readback')
                    return {'status':('VERIFIED' if len(prior)==1 else 'UNKNOWN_RESULT') if recheck_only else 'ALREADY_PRESENT',
                            'matches':prior,'screenshot_path':snapshot['screenshot_path'],'url':page.url}
                row,target,rows=await self._bound_row(page,notice,target['id'])
                # Only the target row's direct operations, excluding nested replies.
                reply_entry=row.locator(':scope > .JPoP3Ezg > .dIlRe7ZV > .Ctt1baq8').get_by_text('回复',exact=True)
                if await reply_entry.count()!=1:raise RuntimeError('PUBLIC_REPLY_BUTTON_NOT_UNIQUE')
                await reply_entry.click(timeout=5000)
                composer=page.locator('.comment-input-container').filter(visible=True)
                if await composer.count()!=1:raise RuntimeError('PUBLIC_COMPOSER_NOT_UNIQUE')
                label=composer.locator('.YpWMCop5')
                if not composer_matches(await label.evaluate(EDITOR_TEXT_JS),target):raise RuntimeError('PUBLIC_RECIPIENT_MISMATCH')
                editor=composer.locator('[contenteditable="true"]')
                await editor.fill(text)
                await page.wait_for_timeout(300)
                await self.base._check(page)
                check=await self.base.sessions.probe(context,account_id)
                if check['status']!='READY':raise RuntimeError(check['status'])
                row,target,rows=await self._bound_row(page,notice,target['id'])
                selected=await row.locator(':scope > .JPoP3Ezg > .dIlRe7ZV > .Ctt1baq8').get_by_text('回复中',exact=True).count()
                if selected!=1 or not composer_matches(await label.evaluate(EDITOR_TEXT_JS),target):raise RuntimeError('PUBLIC_REPLY_TARGET_CHANGED')
                if not same_reply_text(await editor.evaluate(EDITOR_TEXT_JS),text):raise RuntimeError('PUBLIC_REPLY_TEXT_CHANGED')
                send=composer.locator('.commentInput-right-ct .wchsYBpK.jfGCpJo0')
                if await send.count()!=1:raise RuntimeError('PUBLIC_SEND_NOT_READY')
                node=await send.element_handle()
                snapshot=await self.base._snapshot(page,'notification-before-send')
                # Repeat after screenshot; keep the actual node across the guard.
                if not composer_matches(await label.evaluate(EDITOR_TEXT_JS),target) or not same_reply_text(await editor.evaluate(EDITOR_TEXT_JS),text):
                    raise RuntimeError('PUBLIC_COMPOSER_CHANGED')
                if not await node.evaluate("e=>e.isConnected && e.classList.contains('jfGCpJo0')"):
                    raise RuntimeError('PUBLIC_SEND_NODE_CHANGED')
                _,target,_=await self._bound_row(page,notice,target['id'])
                if self._video_id(page.url)!=video_id:raise RuntimeError('PREPARED_NOTIFICATION_TARGET_CHANGED')
                scope=await self._scope_proof(page,account,notice,video_id,target)
                if not self._allowed(scope):raise RuntimeError('TARGET_SCOPE_CHANGED_BEFORE_SUBMIT')
                if not composer_matches(await label.evaluate(EDITOR_TEXT_JS),target) or not same_reply_text(await editor.evaluate(EDITOR_TEXT_JS),text):
                    raise RuntimeError('PUBLIC_COMPOSER_CHANGED')
                try:
                    before_submit();attempted=True
                    await node.click(timeout=5000)
                    await page.wait_for_timeout(1800)
                    # Fresh document confirmation, not just an optimistic new row.
                    await page.reload(wait_until='domcontentloaded',timeout=30000)
                    # Douyin may switch the selected modal on raw reload. Always
                    # reopen the original notification in a new document first.
                    page,checked_video,rows,checked_target=await self._open_target(context,account,notice)
                    if checked_video!=video_id or checked_target['id']!=target['id']:raise RuntimeError('READBACK_TARGET_CHANGED')
                    row,target,rows=await self._bound_row(page,notice,target['id'])
                    await self._expand_thread(page,target['root_id'])
                    matching=await self._readback(page,notice,target,text,own,saved.get('existing_self_ids',[]))
                    result=await self.base._snapshot(page,'notification-after-send')
                    return {'status':'VERIFIED' if len(matching)==1 else 'UNKNOWN_RESULT','matches':matching,
                            'url':page.url,'screenshot_path':result['screenshot_path'],'observed_at':result['observed_at']}
                except Exception as exc:
                    if not attempted:raise
                    return {'status':'UNKNOWN_RESULT','error':type(exc).__name__,'url':page.url,'screenshot_path':snapshot['screenshot_path']}

    async def recheck(self,account_id,notice,text):
        return await self.reply(account_id,notice,text,lambda:None,recheck_only=True)

    async def inspect_target(self,account_id,notification_key,open_reply=False,draft_text=None):
        account=self.base.sessions.account(account_id)
        with self.base.activity('inspect-notification-target'):
            async with self.base.sessions.open(account_id,headless=False) as context:
                page,panel,handle=await self._open_inbox(context,account)
                notices=await self._read_rows(panel,handle)
                matches=[n for n in notices if n['notification_key']==notification_key]
                if len(matches)!=1:raise RuntimeError('NOTIFICATION_NOT_UNIQUE')
                notice=matches[0]
                if notice['kind']!='comment':return {'status':'SKIP','reason':'NOT_TOP_LEVEL_NOTIFICATION'}
                row=panel.locator(':scope > .sKMx4DKd > .v4ybwR9T').nth(notice['raw']['dom_index'])
                previous=set(context.pages)
                await row.locator('pre').click(timeout=5000)
                await page.wait_for_timeout(2000)
                opened=[p for p in context.pages if p not in previous]
                if opened:page=opened[-1]
                await page.wait_for_load_state('domcontentloaded',timeout=20000)
                await page.wait_for_timeout(2500)
                public_rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
                found=[r for r in public_rows if r['profile_path']==notice['source_profile_path'] and same_reply_text(r['text'],notice['text'])]
                if len(found)==1:
                    await self._expand_thread(page,found[0]['root_id'])
                    public_rows=await page.locator('[data-e2e="comment-item"]').evaluate_all(PUBLIC_ROWS_JS)
                if open_reply:
                    matches=[r for r in public_rows if r['profile_path']==notice['source_profile_path'] and same_reply_text(r['text'],notice['text'])]
                    if len(matches)!=1:raise RuntimeError('PUBLIC_COMMENT_TARGET_NOT_UNIQUE')
                    scope=await self._scope_proof(page,account,notice,self._video_id(page.url),matches[0])
                    if not self._allowed(scope):return {'status':'SKIP','reason':'OUTSIDE_OWN_VIDEO_TOP_LEVEL_SCOPE','scope':scope}
                    target=page.locator('[data-e2e="comment-item"]').nth(matches[0]['index'])
                    await target.get_by_text('回复',exact=True).click(timeout=5000)
                    await page.wait_for_timeout(700)
                    if draft_text is not None:
                        if not isinstance(draft_text,str) or not 1<=len(draft_text)<=100:raise ValueError('INVALID_DRAFT')
                        await page.locator('.comment-input-container [contenteditable="true"]').fill(draft_text)
                        await page.wait_for_timeout(500)
                snapshot=await self.base._snapshot(page,'notification-target')
                structures=await page.evaluate('''()=>[...document.querySelectorAll('[data-e2e]')]
                  .filter(e=>e.getClientRects().length&&/comment|reply|video-detail/.test(e.getAttribute('data-e2e')||''))
                  .map(e=>({e2e:e.getAttribute('data-e2e'),text:(e.innerText||'').slice(0,4000),class_name:e.className})).slice(0,35)''')
                markup=await page.locator('[data-e2e="comment-list"]').evaluate_all('''nodes=>nodes.map(e=>{const c=e.cloneNode(true);c.querySelectorAll('svg,script,style').forEach(n=>n.remove());return c.outerHTML.slice(0,45000)})''')
                extras=await page.evaluate('''()=>[...document.querySelectorAll('[contenteditable], [data-e2e]')].filter(e=>e.getClientRects().length&&(/detail|desc|title|input|editor|author/.test(e.getAttribute('data-e2e')||'')||e.hasAttribute('contenteditable'))).map(e=>{const c=e.cloneNode(true);c.querySelectorAll('svg,script,style').forEach(n=>n.remove());return c.outerHTML.slice(0,6000)}).slice(-30)''')
                editors=await page.locator('[contenteditable="true"],textarea').evaluate_all('''nodes=>nodes.filter(e=>e.getClientRects().length).map(e=>{let p=e;for(let i=0;i<7&&p.parentElement;i++)p=p.parentElement;const c=p.cloneNode(true);c.querySelectorAll('svg,script,style').forEach(n=>n.remove());return c.outerHTML.slice(0,22000)})''')
                ownership=await page.evaluate('''()=>{
                  const desc=[...document.querySelectorAll('[data-e2e="video-desc"]')].filter(e=>e.getClientRects().length);
                  const ancestry=desc.map(e=>{const result=[];let p=e;for(let i=0;p&&i<5;i++,p=p.parentElement){const c=p.cloneNode(true);c.querySelectorAll('svg,script,style,video').forEach(n=>n.remove());result.push({tag:p.tagName,class_name:p.className,e2e:p.getAttribute('data-e2e'),markup:c.outerHTML.slice(0,10000)})}return result});
                  const own=document.querySelector('[data-e2e="user-detail"]');
                  const works=own?[...own.querySelectorAll('a[href*="/video/"],a[href*="/note/"]')].slice(0,3).map(e=>({href:e.href,parents:[e.parentElement,e.parentElement?.parentElement,e.parentElement?.parentElement?.parentElement].map(p=>({class_name:p?.className,e2e:p?.getAttribute('data-e2e')}))})):[];
                  return {ancestry,works};
                }''')
                return {'status':'TARGET_INSPECTED_NO_REPLY','notification':notice,'structures':structures,'comment_markup':markup,'extras':extras,'public_rows':public_rows,'editor_markup':editors,'ownership':ownership,**snapshot}

    async def inspect(self, account_id, surface='creator'):
        if surface not in {'creator','douyin','creator_comments','notifications'}:
            raise ValueError('UNSUPPORTED_NOTIFICATION_SURFACE')
        account=self.base.sessions.account(account_id)
        if account['channel']!='douyin':raise ValueError('DOUYIN_ACCOUNT_REQUIRED')
        with self.base.activity('inspect-notifications'):
            async with self.base.sessions.open(account_id,headless=surface not in {'douyin','notifications'}) as context:
                identity=await self.base.sessions.probe(context,account_id)
                if identity['status']!='READY':raise RuntimeError(identity['status'])
                page=await context.new_page()
                response=await page.goto(HOME['douyin'] if surface.startswith('creator') else 'https://www.douyin.com/',wait_until='domcontentloaded',timeout=30000)
                await page.wait_for_function("document.body && document.body.innerText.trim().length > 80",timeout=25000)
                identity_text=None
                if surface=='notifications':
                    prompt=page.get_by_text(re.compile('是否保存登录信息')).filter(visible=True)
                    if await prompt.count():
                        cancel=page.get_by_text('取消',exact=True).filter(visible=True)
                        if await cancel.count()==1:await cancel.click(timeout=5000)
                    mine=page.get_by_role('link',name='我的',exact=True)
                    await mine.click(timeout=5000)
                    await page.wait_for_timeout(1800)
                    identity_text=(await page.locator('body').inner_text())[:5000]
                    if account['display_name'] not in identity_text:
                        raise RuntimeError('DOUYIN_WEB_ACCOUNT_NOT_CONFIRMED')
                    entry=page.locator('[data-e2e="something-button"]').filter(has_text=re.compile(r'^通知(?:\s*\d+)?$')).filter(visible=True)
                    if await entry.count()!=1:raise RuntimeError('NOTIFICATION_ENTRY_NOT_UNIQUE')
                    await entry.click(timeout=5000)
                    await page.wait_for_timeout(1800)
                if surface=='creator_comments':
                    entry=page.get_by_text('评论管理',exact=True).filter(visible=True)
                    await entry.wait_for(timeout=10000)
                    old_pages=set(context.pages)
                    await entry.click(timeout=5000)
                    await page.wait_for_timeout(1500)
                    opened=[p for p in context.pages if p not in old_pages]
                    if opened:page=opened[-1]
                    await page.wait_for_load_state('domcontentloaded',timeout=20000)
                await page.wait_for_timeout(1800)
                snapshot=await self.base._snapshot(page,'notification-entry')
                entry_dom=await page.evaluate('''()=>{
                  const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
                  return [...document.querySelectorAll('a,button,[role="button"],[title],[aria-label],[data-e2e]')]
                    .filter(visible).map(e=>({tag:e.tagName.toLowerCase(),text:(e.innerText||'').trim().slice(0,100),
                      title:e.getAttribute('title'),label:e.getAttribute('aria-label'),href:e.getAttribute('href'),
                      data_e2e:e.getAttribute('data-e2e'),class_name:e.className})).slice(0,160);
                }''')
                panels=await page.evaluate('''()=>[...document.querySelectorAll('[class*="notice"],[class*="notification"],[class*="message"]')]
                  .filter(e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden')
                  .map(e=>({class_name:e.className,role:e.getAttribute('role'),text:(e.innerText||'').slice(0,8000)})).slice(0,35)''')
                notification_dom=None
                if surface=='notifications':
                    title=page.get_by_text('互动消息',exact=True).filter(visible=True)
                    if await title.count()==1:
                        notification_dom=await title.evaluate('''e=>{
                          let p=e;for(let i=0;p&&p!==document.body&&i<10;i++,p=p.parentElement){
                            const t=p.innerText||'';
                            if(t.includes('全部消息')&&(t.includes('赞了你的')||t.includes('评论了你的')||t.includes('回复了你的')||t.includes('暂无'))){
                              const c=p.cloneNode(true);c.querySelectorAll('svg,script,style').forEach(n=>n.remove());
                              return {class_name:p.className,text:t,markup:c.outerHTML.slice(0,50000)};
                            }
                          }return null;
                        }''')
                return {'status':'INSPECTED_NO_REPLY','account_id':account_id,'creator_account':account['display_name'],'http_status':response.status if response else None,
                        'surface':surface,'identity_text':identity_text,'entries':entry_dom,'panels':panels,'notification_dom':notification_dom,**snapshot}
