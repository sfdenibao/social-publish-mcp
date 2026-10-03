"""DOM-only comment scanning within the publisher's existing owned session."""
import asyncio
import hashlib
import json
import re
import unicodedata
from collections import Counter
from utils.glm_comment_reply import canonical_emoji_reply


ROWS_JS = r'''nodes => nodes.map((e,index) => {
  const own = sel => e.querySelector(':scope > '+sel);
  const user=own('[class*="username-"]');
  const content=own('[class*="text-"]');
  const time=own('[class*="time-"]');
  const read=n=>n.nodeType===Node.TEXT_NODE?n.textContent:
    (n.nodeType===Node.ELEMENT_NODE&&n.tagName==='IMG'?(n.getAttribute('alt')||''):
      [...n.childNodes].map(read).join(''));
  const name=user?[...user.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim():'';
  const badge=user?[...user.querySelectorAll('span')].map(s=>s.innerText.trim()):[];
  const a=user?.querySelector('a[href]');
  const cid=e.getAttribute('data-comment-id')||e.getAttribute('data-cid')||null;
  return {dom_index:index,author:name,text:content?read(content).trim():'',
    published_label:time?.innerText.trim()||'',is_own:badge.includes('作者'),
    author_href:a?.getAttribute('href')||null,platform_cid:cid,
    row_class:e.className,parent_class:e.parentElement?.className||'',
    row_text:e.innerText,is_nested:!!e.parentElement?.parentElement?.closest('[class*="comment-main-"],[class*="comment-item-"]')};
})'''


def normalize_comment_text(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split())


def same_reply_text(actual, expected):
    if normalize_comment_text(actual)==normalize_comment_text(expected):
        return True
    canonical=canonical_emoji_reply(actual)
    return canonical is not None and canonical==canonical_emoji_reply(expected)


EDITOR_TEXT_JS = '''e=>{const read=n=>n.nodeType===Node.TEXT_NODE?n.textContent:
  (n.nodeType===Node.ELEMENT_NODE&&n.tagName==='IMG'?(n.getAttribute('alt')||''):[...n.childNodes].map(read).join(''));
  return read(e).trim();}'''


def normalize_rows(rows, video_id):
    result=[]
    for row in rows:
        if row.get('is_nested'):
            continue
        author=normalize_comment_text(row.get('author',''))
        text=normalize_comment_text(row.get('text',''))
        if not author or not text:
            continue
        identity=json.dumps([str(video_id),row.get('author_href') or author,text],ensure_ascii=False,separators=(',',':'))
        fingerprint=hashlib.sha256(identity.encode('utf-8')).hexdigest()
        cid=row.get('platform_cid')
        key='cid:'+cid if cid else 'dom:'+fingerprint
        result.append(dict(comment_key=key,author=author,text=text,
          published_label=row.get('published_label',''),fingerprint=fingerprint,
          platform_cid=cid,thread_key=key,is_own=bool(row.get('is_own')),
          raw={k:row.get(k) for k in ['dom_index','author_href','row_class','parent_class']}))
    counts=Counter(r['comment_key'] for r in result)
    for row in result:
        row['ambiguous']=counts[row['comment_key']]>1
        row['raw']['ambiguous']=row['ambiguous']
    return result


class DouyinCommentThreadService:
    def __init__(self, base):
        self.base=base

    async def _rows(self,page,video_id):
        rows=await page.locator('[class*="comment-main-"]').evaluate_all(ROWS_JS)
        return normalize_rows(rows,video_id)

    async def _unique_row(self,page,video_id,observation):
        rows=await self._rows(page,video_id)
        matches=[r for r in rows if r['comment_key']==observation['comment_key']]
        if len(matches)!=1 or matches[0]['ambiguous']:
            raise RuntimeError('COMMENT_TARGET_NOT_UNIQUE')
        actual=matches[0]
        if any(actual[k]!=observation[k] for k in ['author','text','fingerprint']):
            raise RuntimeError('COMMENT_TARGET_CHANGED')
        return page.locator('[class*="comment-main-"]').nth(actual['raw']['dom_index']),actual

    async def _find_target(self,page,video_id,observation,max_scrolls=5):
        for attempt in range(max_scrolls+1):
            rows=await self._rows(page,video_id)
            if any(r['comment_key']==observation['comment_key'] for r in rows):
                return await self._unique_row(page,video_id,observation)
            if attempt<max_scrolls:
                await page.mouse.wheel(0,700)
                await asyncio.sleep(.6)
        raise RuntimeError('COMMENT_TARGET_NOT_OBSERVED')

    async def _thread_replies(self,row,account_name):
        # The observed comment-item is the smallest parent containing the original
        # row and its thread. Never match a reply elsewhere on the page.
        parent=row.locator('xpath=..')
        expand=parent.get_by_text(re.compile(r'^(展开|查看|更多).*\d*.*回复')).filter(visible=True)
        if await expand.count()==1:
            await expand.click(timeout=3000)
            await asyncio.sleep(.5)
        raw=await parent.evaluate('''e=>{
          const original=e.querySelector(':scope > [class*="comment-main-"]');
          return [...e.querySelectorAll('[class*="username-"]')].map(user=>{
            let p=user.parentElement;
            while(p && p!==e && !p.querySelector(':scope > [class*="text-"]'))p=p.parentElement;
            if(!p||p===e||p===original)return null;
            const body=p.querySelector(':scope > [class*="text-"]');
            const author=[...user.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim();
            const read=n=>n.nodeType===Node.TEXT_NODE?n.textContent:
              (n.nodeType===Node.ELEMENT_NODE&&n.tagName==='IMG'?(n.getAttribute('alt')||''):[...n.childNodes].map(read).join(''));
            return {author,owner:[...user.querySelectorAll('span')].some(n=>n.innerText.trim()==='作者'),text:body?read(body).trim():''};
          }).filter(Boolean);
        }''')
        return [r for r in raw if r.get('owner') and normalize_comment_text(r.get('author',''))==account_name]

    async def reply(self,publish_task_id,expected_video_id,observation,reply_text,before_submit,*,recheck_only=False):
        if not isinstance(reply_text,str) or not 1<=len(reply_text)<=100 or any(ord(c)<32 for c in reply_text):
            raise ValueError('INVALID_REPLY_TEXT')
        if observation.get('is_own') or observation.get('ambiguous') or observation.get('raw',{}).get('ambiguous'):
            return {'state':'SKIP','evidence':{'reason':'OWN_OR_AMBIGUOUS'}}
        task,account=self.base.published_task(publish_task_id)
        attempted=False
        with self.base.activity('thread-recheck' if recheck_only else 'reply-to-comment'):
            async with self.base.sessions.open(account['id'],headless=True) as context:
                identity=await self.base.sessions.probe(context,account['id'])
                if identity['status']!='READY':raise RuntimeError(identity['status'])
                page=await self.base._open_comments(context,task,expected_video_id)
                row,actual=await self._find_target(page,expected_video_id,observation)
                prior=await self._thread_replies(row,account['display_name'])
                matching=[r for r in prior if same_reply_text(r['text'],reply_text)]
                if matching:
                    snap=await self.base._snapshot(page,'autoreply-already-visible')
                    state=('VERIFIED' if len(matching)==1 else 'UNKNOWN_RESULT') if recheck_only else 'SKIP'
                    return {'state':state,'evidence':{
                        'reason':'MATCHED_EXISTING_REPLY','url':page.url,'parent_key':actual['comment_key'],
                        'matches':matching,'screenshot_path':snap['screenshot_path'],'observed_at':snap['observed_at']}}
                if recheck_only:
                    return {'state':'UNKNOWN_RESULT','evidence':{'reason':'REPLY_NOT_YET_OBSERVED','url':page.url,'parent_key':actual['comment_key']}}
                if prior:
                    return {'state':'SKIP','evidence':{'reason':'OWNER_ALREADY_REPLIED','parent_key':actual['comment_key']}}
                try:
                    button=row.locator(':scope > [class*="comment-operation-wrap-"] > [class*="operations-"]').get_by_role('button',name='回复',exact=True)
                    if await button.count()!=1:raise RuntimeError('REPLY_BUTTON_NOT_UNIQUE')
                    await button.click(timeout=5000)
                    composer=row.locator(':scope > [class*="comment-operation-wrap-"] > [class*="composer-"]').filter(visible=True)
                    if await composer.count()!=1:raise RuntimeError('THREAD_COMPOSER_NOT_UNIQUE')
                    editor=composer.locator('[contenteditable="true"]')
                    send=composer.get_by_role('button',name='发送',exact=True)
                    if await editor.count()!=1 or await send.count()!=1:raise RuntimeError('THREAD_EDITOR_NOT_UNIQUE')
                    placeholder=await editor.get_attribute('data-placeholder')
                    if normalize_comment_text(placeholder or '')!=normalize_comment_text('回复 '+actual['author']+'：'):
                        raise RuntimeError('REPLY_RECIPIENT_MISMATCH')
                    await editor.fill(reply_text)
                    if not same_reply_text(await editor.evaluate(EDITOR_TEXT_JS),reply_text) or not await send.is_enabled():raise RuntimeError('REPLY_NOT_READY')
                    await self.base._check(page)
                    check=await self.base.sessions.probe(context,account['id'])
                    if check['status']!='READY':raise RuntimeError(check['status'])
                    await self.base._snapshot(page,'autoreply-before-send')
                    # New comments can reorder nth() locators during identity checks.
                    # Rebind the original row, recheck its recipient/text, then retain
                    # the actual button node so a later index shift cannot retarget it.
                    row,actual=await self._unique_row(page,expected_video_id,observation)
                    composer=row.locator(':scope > [class*="comment-operation-wrap-"] > [class*="composer-"]').filter(visible=True)
                    if await composer.count()!=1:raise RuntimeError('THREAD_COMPOSER_CHANGED')
                    editor=composer.locator('[contenteditable="true"]')
                    send=composer.get_by_role('button',name='发送',exact=True)
                    if await editor.count()!=1 or await send.count()!=1:raise RuntimeError('THREAD_EDITOR_CHANGED')
                    if normalize_comment_text(await editor.get_attribute('data-placeholder') or '')!=normalize_comment_text('回复 '+actual['author']+'：'):
                        raise RuntimeError('REPLY_RECIPIENT_CHANGED')
                    if not same_reply_text(await editor.evaluate(EDITOR_TEXT_JS),reply_text) or not await send.is_enabled():raise RuntimeError('REPLY_CONTENT_CHANGED')
                    send_node=await send.element_handle()
                    if send_node is None:raise RuntimeError('REPLY_BUTTON_DETACHED')
                    binding=await send_node.evaluate('''e=>{
                      const c=e.closest('[class*="composer-"]');
                      const r=e.closest('[class*="comment-main-"]');
                      const user=r?.querySelector(':scope > [class*="username-"]');
                      const input=c?.querySelector('[contenteditable="true"]');
                      const content=r?.querySelector(':scope > [class*="text-"]');
                      const read=n=>n.nodeType===Node.TEXT_NODE?n.textContent:
                        (n.nodeType===Node.ELEMENT_NODE&&n.tagName==='IMG'?(n.getAttribute('alt')||''):[...n.childNodes].map(read).join(''));
                      return {connected:e.isConnected,disabled:e.disabled,
                        author:user?[...user.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE).map(n=>n.textContent).join('').trim():'',
                        text:content?read(content).trim():'',placeholder:input?.getAttribute('data-placeholder'),reply:input?read(input).trim():''};
                    }''')
                    if not binding.get('connected') or binding.get('disabled'):
                        raise RuntimeError('REPLY_BUTTON_CHANGED')
                    if (normalize_comment_text(binding.get('author',''))!=actual['author'] or
                        normalize_comment_text(binding.get('text',''))!=actual['text'] or
                        normalize_comment_text(binding.get('placeholder',''))!=normalize_comment_text('回复 '+actual['author']+'：') or
                        not same_reply_text(binding.get('reply',''),reply_text)):
                        raise RuntimeError('REPLY_BINDING_CHANGED')
                    before_submit()
                    attempted=True
                    await send_node.click(timeout=5000)
                    await asyncio.sleep(2)
                    initial=await self.base._snapshot(page,'autoreply-after-send')
                    # New navigation and DOM read; do not rely on optimistic UI insertion.
                    page=await self.base._open_comments(context,task,expected_video_id)
                    row,actual=await self._find_target(page,expected_video_id,observation)
                    replies=await self._thread_replies(row,account['display_name'])
                    matching=[r for r in replies if same_reply_text(r['text'],reply_text)]
                    snap=await self.base._snapshot(page,'autoreply-verified')
                    return {'state':'VERIFIED' if len(matching)==1 else 'UNKNOWN_RESULT','evidence':{
                        'url':page.url,'parent_key':actual['comment_key'],'matches':matching,
                        'verified_after_reload':len(matching)==1,'screenshot_path':snap['screenshot_path'],
                        'initial_screenshot_path':initial['screenshot_path'],'observed_at':snap['observed_at']}}
                except Exception as exc:
                    code=str(exc) if re.fullmatch(r'[A-Z][A-Z0-9_]{2,80}',str(exc)) else type(exc).__name__
                    return {'state':'UNKNOWN_RESULT' if attempted else 'BLOCKED','error_code':code,
                            'evidence':{'error_type':type(exc).__name__,'message':' '.join(str(exc).split())[:300],
                                        'parent_key':actual['comment_key'],'attempted':attempted}}

    async def scan(self,publish_task_id,expected_video_id,max_scrolls=5):
        if not 0<=max_scrolls<=20:
            raise ValueError('INVALID_SCAN_LIMIT')
        task,account=self.base.published_task(publish_task_id)
        with self.base.activity('scan-new-comments'):
            async with self.base.sessions.open(account['id'],headless=True) as context:
                identity=await self.base.sessions.probe(context,account['id'])
                if identity['status']!='READY':raise RuntimeError(identity['status'])
                page=await self.base._open_comments(context,task,expected_video_id)
                seen={};complete=False
                for step in range(max_scrolls+1):
                    await self.base._check(page)
                    rows=await self._rows(page,expected_video_id)
                    for row in rows:
                        old=seen.get(row['comment_key'])
                        if old and old.get('ambiguous'):row['ambiguous']=True;row['raw']['ambiguous']=True
                        seen[row['comment_key']]=row
                    ends=page.get_by_text(re.compile(r'^(没有更多评论|暂无更多评论)$')).filter(visible=True)
                    if await ends.count():complete=True;break
                    if step<max_scrolls:
                        await page.mouse.wheel(0,700)
                        await asyncio.sleep(.8)
                snap=await self.base._snapshot(page,'autoreply-scan')
                return dict(account=account['display_name'],account_id=account['id'],
                  publish_task_id=publish_task_id,video_id=expected_video_id,comments=list(seen.values()),
                  complete=complete,observed_at=snap['observed_at'],url=page.url,screenshot_path=snap['screenshot_path'],
                  evidence={'url':page.url,'observed_at':snap['observed_at'],'screenshot_path':snap['screenshot_path'],'complete':complete})

    async def inspect_reply_entry(self,publish_task_id,expected_video_id,comment_key):
        """Opens a reply composer only; never fills or clicks send."""
        task,account=self.base.published_task(publish_task_id)
        with self.base.activity('inspect-reply-entry'):
            async with self.base.sessions.open(account['id'],headless=True) as context:
                identity=await self.base.sessions.probe(context,account['id'])
                if identity['status']!='READY':raise RuntimeError(identity['status'])
                page=await self.base._open_comments(context,task,expected_video_id)
                matches=[r for r in await self._rows(page,expected_video_id) if r['comment_key']==comment_key]
                if len(matches)!=1 or matches[0]['ambiguous']:raise RuntimeError('COMMENT_TARGET_NOT_UNIQUE')
                row,actual=await self._unique_row(page,expected_video_id,matches[0])
                await row.get_by_role('button',name='回复',exact=True).click(timeout=5000)
                await asyncio.sleep(.5)
                snap=await self.base._snapshot(page,'reply-entry')
                markup=await row.evaluate('''e=>{const c=e.parentElement.cloneNode(true);c.querySelectorAll('svg,script,style').forEach(n=>n.remove());return c.outerHTML.slice(0,18000)}''')
                return dict(status='COMPOSER_INSPECTED_NO_SEND',target=actual,parent_markup=markup,**snap)
