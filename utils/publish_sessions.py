"""One configured profile per account, shared by every MCP browser operation."""
import asyncio
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from utils.publish_ledger import now
from utils.runtime_lock import FileLock

HOME = {
    'douyin': 'https://creator.douyin.com/creator-micro/home',
    'wechat_channels': 'https://channels.weixin.qq.com/platform',
    'xiaohongshu': 'https://creator.xiaohongshu.com/new/note-manager',
}
DOMAINS = {'douyin':'douyin.com','wechat_channels':'weixin.qq.com','xiaohongshu':'xiaohongshu.com'}


class SessionManager:
    def __init__(self, runtime, ledger):
        self.runtime, self.ledger = Path(runtime), ledger
        self.logins = {}
        self.guard = threading.Lock()

    def accounts(self):
        path=self.runtime/'accounts.json'
        return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []

    def account(self, account_id):
        matches=[a for a in self.accounts() if a['id']==account_id]
        if len(matches)!=1:
            raise ValueError('ACCOUNT_NOT_FOUND_OR_DUPLICATE')
        account=matches[0]
        if not account.get('profile') or not account.get('display_name'):
            raise ValueError('ACCOUNT_SESSION_NOT_CONFIGURED')
        return account

    @asynccontextmanager
    async def open(self, account_id, *, headless=True):
        from patchright.async_api import async_playwright
        account=self.account(account_id)
        profile=Path(account['profile']).resolve()
        digest=hashlib.sha256(str(profile).casefold().encode()).hexdigest()
        # Lock location depends on profile, so distinct runtime directories also
        # serialize access to the same browser profile.
        with FileLock(profile.parent/('.'+digest+'.lock')):
            async with async_playwright() as p:
                context=await p.chromium.launch_persistent_context(str(profile),
                    executable_path=os.environ.get('SAU_CHROME_PATH','C:/Program Files/Google/Chrome/Application/chrome.exe'),
                    headless=headless, locale='zh-CN')
                try:
                    marker=profile/'sau-managed-session'
                    if not marker.exists() and account.get('import_session',False):
                        source=Path(account['session_file'])
                        if not source.is_file():
                            raise RuntimeError('SESSION_FILE_MISSING')
                        state=json.loads(source.read_text(encoding='utf-8'))
                        await context.add_cookies(state.get('cookies',[]))
                        page=await context.new_page()
                        for origin in state.get('origins',[]):
                            url=origin['origin'];host=urlparse(url).hostname or ''
                            domain=DOMAINS[account['channel']]
                            if url.startswith('https://') and (host==domain or host.endswith('.'+domain)):
                                await page.goto(url,wait_until='domcontentloaded',timeout=20000)
                                await page.evaluate('(items)=>{for(const i of items)if(localStorage.getItem(i.name)===null)localStorage.setItem(i.name,i.value)}',origin.get('localStorage',[]))
                        await page.close()
                        marker.write_text('imported',encoding='utf-8')
                    yield context
                    if getattr(context,'sau_identity_verified',False):
                        # Save only a positively identified account, atomically.
                        target=Path(account['session_file']);target.parent.mkdir(parents=True,exist_ok=True)
                        temp=target.with_name(target.name+'.tmp')
                        await context.storage_state(path=str(temp))
                        temp.replace(target)
                finally:
                    await context.close()

    async def identity(self, page, account):
        host=urlparse(page.url).hostname or ''
        if host != urlparse(HOME[account['channel']]).hostname:
            return 'CHECK_FAILED'
        path=urlparse(page.url).path
        shell = {'douyin':'/creator-micro/', 'wechat_channels':'/platform', 'xiaohongshu':'/new/'}[account['channel']] in path
        for frame in page.frames:
            if 'qrconnect' in frame.url or 'login' in path:
                return 'LOGIN_REQUIRED'
        if shell:
            import re
            selector={
                'douyin':'[class^="header-"] [class^="name-"]',
                'wechat_channels':'.account-info .name, .finder-info .finder-nickname',
                'xiaohongshu':'.d-topbar .user-info .name-box',
            }[account['channel']]
            for frame in page.frames:
                name=frame.locator(selector).filter(has_text=re.compile('^'+re.escape(account['display_name'])+'$')).filter(visible=True)
                if await name.count():
                    return 'READY'
        for label in ('扫码登录','手机号登录','登录/注册'):
            if await page.get_by_text(label,exact=True).filter(visible=True).count():
                return 'LOGIN_REQUIRED'
        return 'VERIFYING_ACCOUNT' if shell else 'CHECK_FAILED'

    async def probe(self, context, account_id):
        account=self.account(account_id)
        page=await context.new_page()
        try:
            await page.goto(HOME[account['channel']],wait_until='domcontentloaded',timeout=30000)
            status='CHECK_FAILED'
            for _ in range(12):
                status=await self.identity(page,account)
                if status in {'READY','LOGIN_REQUIRED'}:
                    break
                await asyncio.sleep(.5)
            if status=='VERIFYING_ACCOUNT':
                status='ACCOUNT_MISMATCH'
            if status=='READY':
                context.sau_identity_verified=True
            result=dict(status=status,checked_at=now(),display_name=account['display_name'])
            self.ledger.account_status(account_id,result)
            return result
        finally:
            await page.close()

    async def page_blocker(self, page):
        if 'login' in urlparse(page.url).path or any('qrconnect' in f.url for f in page.frames):
            return 'LOGIN_REQUIRED'
        for frame in page.frames:
            for label in ('扫码登录','手机号登录','登录/注册'):
                if await frame.get_by_text(label,exact=True).filter(visible=True).count():
                    return 'LOGIN_REQUIRED'
            for label in ('安全验证','身份验证','短信验证','请完成验证'):
                if await frame.get_by_text(label,exact=True).filter(visible=True).count():
                    return 'SECURITY_VERIFICATION_REQUIRED'
        return None

    async def upload_input_ready(self, page):
        for frame in page.frames:
            inputs=frame.locator('input[type="file"]')
            for index in range(await inputs.count()):
                node=inputs.nth(index)
                accept=(await node.get_attribute('accept') or '').lower()
                if await node.is_enabled() and ('video' in accept or '.mp4' in accept):
                    return True
        return False

    async def publish_page(self, context, account_id, page=None):
        account=self.account(account_id)
        owned=page is None
        page=page or await context.new_page()
        try:
            if account['channel']=='wechat_channels':
                await page.goto(HOME['wechat_channels'],wait_until='domcontentloaded',timeout=30000)
                entry=page.locator('button.weui-desktop-btn').filter(has_text='发表视频').filter(visible=True).first
                for _ in range(30):
                    blocker=await self.page_blocker(page)
                    if blocker:
                        return dict(status=blocker)
                    if await entry.count():
                        break
                    await asyncio.sleep(.5)
                await entry.click(timeout=5000)
            else:
                url={'douyin':'https://creator.douyin.com/creator-micro/content/upload',
                     'xiaohongshu':'https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video'}[account['channel']]
                await page.goto(url,wait_until='domcontentloaded',timeout=30000)
            for _ in range(30):
                blocker=await self.page_blocker(page)
                if blocker:
                    return dict(status=blocker)
                if await self.upload_input_ready(page):
                    return dict(status='READY',publish_page_url=page.url,publish_input_verified=True)
                await asyncio.sleep(.5)
            return dict(status='PUBLISH_PAGE_UNAVAILABLE')
        finally:
            if owned:
                await page.close()

    async def preflight_context(self, context, account_id):
        result=await self.probe(context,account_id)
        if result['status']=='READY':
            result.update(await self.publish_page(context,account_id))
        result['checked_at']=now()
        self.ledger.account_status(account_id,result)
        return result

    async def preflight(self, account_id):
        try:
            async with self.open(account_id) as context:
                return await self.preflight_context(context,account_id)
        except Exception as exc:
            code='PROFILE_BUSY' if str(exc)=='PROFILE_BUSY' else 'CHECK_FAILED'
            result=dict(status=code,checked_at=now())
            if code!='PROFILE_BUSY':
                self.ledger.account_status(account_id,result)
            return result

    async def check(self, account_id):
        try:
            async with self.open(account_id) as context:
                return await self.probe(context,account_id)
        except Exception as exc:
            code='PROFILE_BUSY' if str(exc)=='PROFILE_BUSY' else 'CHECK_FAILED'
            result=dict(status=code,checked_at=now())
            # Busy does not invalidate the last authenticated identity.
            if code!='PROFILE_BUSY':
                self.ledger.account_status(account_id,result)
            return result

    def start_login(self, account_id, visible=True):
        self.account(account_id)
        with self.guard:
            for sid,login in self.logins.items():
                if login['account_id']==account_id and 'finished_at' not in login:
                    return sid
            sid=uuid.uuid4().hex
            self.logins[sid]=dict(account_id=account_id,status='STARTING',created_at=now(),
                                  cancel=threading.Event(),refresh=threading.Event(),qr=None)
            thread=threading.Thread(target=lambda:asyncio.run(self._login(sid,visible)),daemon=True,name='login-'+account_id)
            thread.start()
            return sid

    def login_status(self, sid):
        login=self.logins[sid]
        return {k:v for k,v in login.items() if k not in {'cancel','refresh','qr'}}

    async def _login(self, sid, visible):
        login=self.logins[sid];account_id=login['account_id'];ready=False;started=time.perf_counter()
        try:
            async with self.open(account_id,headless=not visible) as context:
                account=self.account(account_id);page=await context.new_page()
                await page.goto(HOME[account['channel']],wait_until='domcontentloaded',timeout=30000)
                deadline=time.monotonic()+300;refreshed=time.monotonic();mismatch_since=None;upload_stage=False
                while time.monotonic()<deadline and not login['cancel'].is_set():
                    account=self.account(account_id)  # configuration corrections apply live
                    if upload_stage:
                        state=await self.page_blocker(page)
                        if not state:
                            state='READY' if await self.upload_input_ready(page) else 'VERIFYING_PUBLISH_PAGE'
                            if state=='VERIFYING_PUBLISH_PAGE' and page.url.rstrip('/')==HOME[account['channel']].rstrip('/'):
                                state=(await self.publish_page(context,account_id,page=page))['status']
                    else:
                        state=await self.identity(page,account)
                        if state=='READY':
                            context.sau_identity_verified=True
                            state=(await self.publish_page(context,account_id,page=page))['status']
                            upload_stage=True
                    if state=='READY':
                        verified=await self.probe(context,account_id)
                        if verified['status']=='READY':
                            context.sau_identity_verified=True;ready=True;break
                        state=verified['status']
                        if state=='ACCOUNT_MISMATCH':
                            login['status']=state;break
                    if state=='VERIFYING_ACCOUNT':
                        mismatch_since=mismatch_since or time.monotonic()
                        if time.monotonic()-mismatch_since>10:
                            login['status']='ACCOUNT_MISMATCH';break
                    else:
                        mismatch_since=None
                    login['status']='WAITING_FOR_SCAN' if state=='LOGIN_REQUIRED' else state
                    login['qr']=None
                    for frame in page.frames:
                        qr=frame.locator('img.qrcode, img[aria-label="二维码"], [class*="qrcode"] img, #animate_qrcode_container img').filter(visible=True).first
                        if await qr.count():
                            login['qr']=await qr.screenshot();break
                    if login['refresh'].is_set() or (state=='LOGIN_REQUIRED' and time.monotonic()-refreshed>120):
                        login['refresh'].clear();await page.reload(wait_until='domcontentloaded',timeout=30000);refreshed=time.monotonic()
                    await asyncio.sleep(1)
                if not ready and login['status']!='ACCOUNT_MISMATCH':
                    login['status']='CANCELLED' if login['cancel'].is_set() else 'EXPIRED'
            # The profile lock is released BEFORE waiting tasks resume.
            if not ready and login['status']=='ACCOUNT_MISMATCH':
                self.ledger.account_status(account_id,dict(status='ACCOUNT_MISMATCH',checked_at=now()))
            if ready:
                self.ledger.account_status(account_id,dict(status='READY',checked_at=now(),display_name=account['display_name']))
                self.ledger.resume_account(account_id)
                login['status']='READY'
        except Exception as exc:
            login['status']='PROFILE_BUSY' if str(exc)=='PROFILE_BUSY' else 'CHECK_FAILED'
        finally:
            login['qr']=None;login['finished_at']=now();login['elapsed_ms']=round((time.perf_counter()-started)*1000,2)
