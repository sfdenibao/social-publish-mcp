"""Standalone local MCP service. Its worker survives client disconnection."""
import asyncio
import hashlib
import json
import os
import uuid
import time
import inspect
from functools import wraps
from datetime import datetime, timezone
from starlette.responses import HTMLResponse, JSONResponse, Response
from pathlib import Path
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BeforeValidator
from utils.publish_ledger import PublishLedger
from utils.publish_sessions import SessionManager
from utils.publish_worker import PublishWorker
from utils.publish_dispatch import SCHEDULING
from utils.runtime_lock import FileLock
from utils.publish_batches import PublishBatches, ORDER, items_of
from utils.publish_declarations import SelfDeclaration, declaration_capability, validate_declaration, validate_plan_declarations
from utils.publish_errors import DeclarationError
from utils.publish_delete import delete_publish, deletion_receipt
from utils.publish_titles import clean_publish_title, platform_title
from utils.douyin_comments import DouyinCommentService
from utils.douyin_comment_threads import DouyinCommentThreadService
from utils.comment_autoreply_store import CommentAutoreplyStore
from utils.comment_autoreply_worker import CommentAutoreplyWorker
from utils.glm_comment_reply import CommentReplyEngine
from utils.douyin_notifications import DouyinNotificationService
from utils.douyin_notification_store import NotificationStore
from utils.douyin_notification_worker import NotificationWorker
from utils.douyin_owned_video import DouyinOwnedVideoService
from utils.owned_video_reply_store import OwnedVideoReplyStore

ROOT = Path(__file__).resolve().parent
os.environ.setdefault('SAU_CHROME_PATH', 'C:/Program Files/Google/Chrome/Application/chrome.exe')
RUNTIME = Path(os.environ.get('SAU_RUNTIME_DIR', str(ROOT / 'runtime')))
ledger = PublishLedger(RUNTIME / 'publish.sqlite3')
sessions = SessionManager(RUNTIME, ledger)
with ledger.connect() as db:
    db.execute('CREATE TABLE IF NOT EXISTS frozen_plans (id TEXT PRIMARY KEY, body TEXT NOT NULL)')


def accounts_config():
    path = RUNTIME / 'accounts.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else []


def media_digest(path):
    with open(path, 'rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest()


def declaration_input(value):
    """Keep the native enum schema and explain unknown values before function dispatch."""
    try:
        validate_declaration('douyin', value)
    except DeclarationError as exc:
        raise ValueError(json.dumps(exc.to_dict(), ensure_ascii=False)) from exc
    return value


async def execute_task(task):
    validate_plan_declarations(task['plan'])
    from utils.mcp_publish_adapters import make_douyin_uploader, make_xhs_uploader, make_tencent_uploader
    plan=task['plan'];account=sessions.account(plan['account_id'])
    if account['channel']!=plan['channel']:
        raise ValueError('ACCOUNT_CHANNEL_MISMATCH')
    with ledger.timed(task['id'],'MEDIA_CHECK'):
        if await asyncio.to_thread(media_digest,plan['media_path'])!=plan['media_sha256']:
            raise ValueError('MEDIA_CHANGED_AFTER_PREPARE')
    opened=time.perf_counter();ledger.event(task['id'],'PROFILE_OPEN','START')
    async with sessions.open(account['id'],headless=False) as context:
        ledger.event(task['id'],'PROFILE_OPEN','END',(time.perf_counter()-opened)*1000)
        with ledger.timed(task['id'],'ACCOUNT_CHECK'):
            status=await sessions.preflight_context(context,account['id'])
            if status['status']!='READY':
                raise RuntimeError(status['status'])
        factory={'douyin':make_douyin_uploader,'xiaohongshu':make_xhs_uploader,'wechat_channels':make_tencent_uploader}[plan['channel']]
        uploader=factory(plan,account)
        uploader.publish_ledger=ledger;uploader.publish_task_id=task['id']
        uploader.session_context=context;uploader.upload_started=time.perf_counter()
        async def before_submit_check(page):
            blocker=await sessions.page_blocker(page)
            if blocker:
                raise RuntimeError(blocker)
            identity=await sessions.probe(context,account['id'])
            if identity['status']!='READY':
                raise RuntimeError(identity['status'])
        uploader.before_submit_check=before_submit_check
        ledger.event(task['id'],'UPLOAD_PREPARE','START')
        await asyncio.wait_for(uploader.upload(None),timeout=360)


async def reconcile(task, force=False):
    from utils.mcp_publish_adapters import verify_xhs, verify_tencent
    from utils.douyin_publish_result import verify_result
    account=sessions.account(task['plan']['account_id'])
    captured={}
    class EvidenceOnly:
        path=ledger.path
        def get(self, task_id):
            return ledger.get(task_id)
        def record_dom_result(self, task_id, state, evidence):
            captured.update(state=state,evidence=evidence)
    target=EvidenceOnly() if force else ledger
    with ledger.timed(task['id'],'AUTO_DOM_CHECK'):
        async with sessions.open(account['id']) as context:
            status=await sessions.probe(context,account['id'])
            if status['status']!='READY':
                raise RuntimeError(status['status'])
            verify={'douyin':verify_result,'xiaohongshu':verify_xhs,'wechat_channels':verify_tencent}[account['channel']]
            await verify(await context.new_page(),target,task['id'],max_checks=1)
    return captured if force else ledger.get(task['id'])


batches=PublishBatches(ledger,sessions,reconcile)
worker_runtime=PublishWorker(ledger,execute_task,reconcile,sessions)
worker_runtime.batches=batches


mcp = FastMCP('Social Auto Upload', host='127.0.0.1', port=int(os.environ.get('SAU_MCP_PORT', '5410')))
comment_service = DouyinCommentService(ledger, sessions, RUNTIME)
comment_threads = DouyinCommentThreadService(comment_service)
notification_service = DouyinNotificationService(comment_service)
owned_video_service = DouyinOwnedVideoService(comment_service)


@mcp.tool()
async def inspect_douyin_own_work_order(account_id: str='douyin-main') -> dict:
    """Read the authenticated account's own works in visible display order. Never replies."""
    return await asyncio.to_thread(lambda:asyncio.run(owned_video_service.inspect_works(account_id)))


@mcp.tool()
async def read_douyin_owned_video_comments(video_id: str, account_id: str='douyin-main') -> dict:
    """Read only top-level comments on one positively verified owned video. Never sends or enables a watcher."""
    try:
        return await asyncio.to_thread(lambda:asyncio.run(owned_video_service.read_video(account_id,video_id)))
    except Exception:
        import traceback
        (comment_service.directory/'owned-video-read-diagnostic.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise


@mcp.tool()
async def inspect_douyin_notifications(account_id: str='douyin-main', surface: str='creator') -> dict:
    """Read the owned account's visible notification entry. No replies or private-message sends."""
    return await asyncio.to_thread(lambda:asyncio.run(notification_service.inspect(account_id,surface)))


@mcp.tool()
async def read_douyin_notifications(account_id: str='douyin-main') -> dict:
    """Read the account's current interaction notifications only; not private messages. Never replies."""
    return await asyncio.to_thread(lambda:asyncio.run(notification_service.scan(account_id)))


@mcp.tool()
async def inspect_douyin_notification_target(notification_key: str, account_id: str='douyin-main', open_reply: bool=False, draft_text: str | None=None) -> dict:
    """Inspect the bound reply composer, optionally fill a draft. Never submits; draft is discarded on close."""
    return await asyncio.to_thread(lambda:asyncio.run(notification_service.inspect_target(account_id,notification_key,open_reply,draft_text)))
autoreply_store = CommentAutoreplyStore(RUNTIME / 'comment-autoreply.sqlite3')
reply_model = CommentReplyEngine(RUNTIME / 'comment-reply-model.local.json')
owned_video_store = OwnedVideoReplyStore(RUNTIME / 'owned-video-replies.sqlite3')
_owned_video_runner = None


def get_owned_video_runner():
    global _owned_video_runner
    if _owned_video_runner is None:
        from utils.owned_video_reply_runner import OwnedVideoReplyRunner
        _owned_video_runner=OwnedVideoReplyRunner(owned_video_store,owned_video_service,reply_model)
    return _owned_video_runner


@mcp.tool()
async def prepare_douyin_owned_video_replies(video_id: str, account_id: str='douyin-main') -> dict:
    """Freeze and draft replies to top-level comments on ONE explicitly requested owned video. Never posts or enables a scheduler."""
    runner=get_owned_video_runner()
    try:
        return await asyncio.to_thread(lambda:asyncio.run(runner.prepare(account_id,video_id)))
    except Exception:
        import traceback
        (comment_service.directory/'owned-video-prepare-diagnostic.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise


@mcp.tool()
async def submit_douyin_owned_video_replies(run_id: str, expected_video_id: str) -> dict:
    """Submit only the frozen run explicitly authorized by the user. Keeps all standing watchers paused; uncertain sends are never repeated."""
    runner=get_owned_video_runner()
    return await asyncio.to_thread(lambda:asyncio.run(runner.submit(run_id,expected_video_id)))


@mcp.tool()
def get_douyin_owned_video_reply_run(run_id: str) -> dict:
    """Read this one-video run's frozen targets, drafts and at-most-once submission evidence."""
    return owned_video_store.get_run(run_id)
notification_store = NotificationStore(RUNTIME / 'douyin-notifications.sqlite3')
def notification_known_keys(account_id):
    try:return {n['notification_key'] for n in notification_store.history(account_id,limit=2147483647)}
    except KeyError:return set()
notification_service.known_keys=notification_known_keys
notification_worker = NotificationWorker(notification_store,notification_service,reply_model)


@mcp.tool()
def configure_douyin_notifications(account_id: str='douyin-main', interval_seconds: int=300) -> dict:
    """Configure only top-level comments on the account's own videos. No nested replies or private messages; disabled by default."""
    account=sessions.account(account_id)
    if account['channel']!='douyin':raise ValueError('DOUYIN_ACCOUNT_REQUIRED')
    if not 60<=interval_seconds<=86400:raise ValueError('INVALID_POLL_INTERVAL')
    return {'settings':notification_store.configure(account_id,interval_seconds=interval_seconds),'daily_reply_limit':None,'per_run_reply_limit':None,'private_messages':False,'scope':'own-video-top-level-v1'}


@mcp.tool()
def set_douyin_notifications_enabled(enabled: bool, account_id: str='douyin-main') -> dict:
    """Enable or pause user-authorized notification replies. Disables overlapping single-work watchers for this account."""
    if enabled:
        if not reply_model.status()['ready']:raise ValueError('MODEL_NOT_CONFIGURED')
        for job in autoreply_store.list_jobs():
            if job['account_id']==account_id:autoreply_store.set_enabled(job['id'],False)
    return notification_store.set_enabled(account_id,enabled)


@mcp.tool()
async def run_douyin_notifications_once(account_id: str='douyin-main', send: bool=False) -> dict:
    """Scan notifications; send only with an enabled standing permission. Unknown submits are never resent."""
    return await asyncio.to_thread(lambda:asyncio.run(notification_worker.run_once(account_id,send=send,force=True)))


@mcp.tool()
async def preview_douyin_notification_reply(notification_key: str, account_id: str='douyin-main') -> dict:
    """Read actual original conversation and generate a draft only. Never submits or changes send state."""
    notice=notification_store.get(account_id,notification_key)
    context=await asyncio.to_thread(lambda:asyncio.run(notification_service.read_context(account_id,notice)))
    decision=await asyncio.to_thread(reply_model.generate,notice['text'],context)
    return {'notification_key':notification_key,'context':context,'decision':decision,'sends':0}


@mcp.tool()
def get_douyin_notifications_status(account_id: str='douyin-main') -> dict:
    """Read notification watcher status and recent public interactions, never private messages or credentials."""
    return {**notification_store.status(account_id),'worker':notification_worker.status(),'model':reply_model.status(),'scope':'own-video-top-level-v1',
            'daily_reply_limit':None,'per_run_reply_limit':None,'private_messages':False,'recent':notification_store.history(account_id)}


def autoreply_context(job):
    task, account = comment_service.published_task(job['publish_task_id'])
    if account['id'] != job['account_id']:
        raise ValueError('AUTOREPLY_ACCOUNT_CHANGED')
    config = job['config']
    # Only public task facts go to the model. No profile, credentials or tool list.
    return dict(post_title=task['plan']['title'],video_id=job['video_id'],is_own_video=True,is_top_level=True,work_type='video',
                post_description=task['plan'].get('description',''),
                recent_replies=[c['reply'] for c in autoreply_store.history(job['id'],limit=10,state='VERIFIED') if c['reply']][:5])


autoreply_worker = CommentAutoreplyWorker(autoreply_store, comment_threads, reply_model, autoreply_context,max_per_run=None)


@mcp.tool()
def configure_douyin_autoreply(publish_task_id: str, expected_video_id: str,
                              interval_seconds: int=300, daily_reply_limit: int | None=None) -> dict:
    """Create/update a disabled-by-default auto-reply job for one owned published video.

    Does not send comments. First complete scan baselines existing comments; only
    subsequently observed comments are eligible. None means unlimited daily replies.
    """
    import re
    if not re.fullmatch(r'\d{10,30}',expected_video_id):raise ValueError('INVALID_VIDEO_ID')
    if not 60<=interval_seconds<=86400 or (daily_reply_limit is not None and (type(daily_reply_limit) is not int or daily_reply_limit<1)):
        raise ValueError('AUTOREPLY_LIMIT_OUT_OF_RANGE')
    task,account=comment_service.published_task(publish_task_id)
    job=autoreply_store.upsert_job(account['id'],publish_task_id,expected_video_id,
        interval_seconds=interval_seconds,min_reply_interval=0,daily_limit=daily_reply_limit,
        max_model_calls_day=None if daily_reply_limit is None else daily_reply_limit*2,config={
            'post_context':task['plan'].get('description',''),
            'style':'自然简短务实，先回应具体麻烦，最多追问一个关键问题；不夸大、不报价、不承诺工期；需要时如实说明AI助手身份。'})
    return {'job':job,'model':reply_model.status(),'state':'CONFIGURED','sends':0}


@mcp.tool()
async def preview_douyin_auto_reply(text: str) -> dict:
    """Call the configured Qwen/GLM for one sample decision. Never posts to Douyin."""
    return await asyncio.to_thread(reply_model.generate,text,{
        'is_own_video':True,'is_top_level':True,'work_type':'video','campaign':False,
        'post_title':'评论回复预览', 'post_description':''})


@mcp.tool()
async def run_douyin_autoreply_once(job_id: str, send: bool=False) -> dict:
    """Run a bounded scan. send=True can reply only if the job is explicitly enabled.

    First scan only records a baseline. Does not override pauses, identity checks,
    quotas or submission guards. False never calls the model or posts.
    """
    return await asyncio.to_thread(lambda:asyncio.run(autoreply_worker.run_once(job_id,send=send,force=True)))


@mcp.tool()
def set_douyin_autoreply_enabled(job_id: str, enabled: bool) -> dict:
    """Enable/pause standing auto-reply permission for the fixed video.

    Enable only when the human has explicitly authorized automatic replies. Requires
    a complete initial baseline and configured model. New comments use the approved
    public context/style; uncertain sends are checked read-only and never repeated.
    """
    job=autoreply_store.get_job(job_id)
    if enabled:
        if not job['baseline_complete']:raise ValueError('COMPLETE_BASELINE_REQUIRED')
        if not reply_model.status()['ready']:raise ValueError('MODEL_NOT_CONFIGURED')
        comment_service.published_task(job['publish_task_id'])
    return autoreply_store.set_enabled(job_id,enabled)


@mcp.tool()
def get_douyin_autoreply_status(job_id: str | None=None) -> dict:
    """Read safe model metadata, enabled jobs, counters, handoff items and recent decisions."""
    jobs=[autoreply_store.get_job(job_id)] if job_id else autoreply_store.list_jobs()
    return {'worker':autoreply_worker.status(),'model':reply_model.status(),
            'jobs':[{**autoreply_store.status(j['id']),
                     'recent':autoreply_store.history(j['id'],limit=20),
                     'events':autoreply_store.event_history(j['id'],limit=10)} for j in jobs]}


@mcp.tool()
async def read_douyin_comments(publish_task_id: str, expected_video_id: str, max_scrolls: int=5) -> dict:
    """Read bounded top-level comments and emoji alt text; no model or posting. Reports completeness."""
    return await asyncio.to_thread(lambda: asyncio.run(comment_threads.scan(publish_task_id,expected_video_id,max_scrolls)))


@mcp.tool()
async def inspect_douyin_reply_entry(publish_task_id: str, expected_video_id: str, comment_key: str) -> dict:
    """Read one existing comment's reply composer. Does not fill or send any content."""
    return await asyncio.to_thread(lambda: asyncio.run(comment_threads.inspect_reply_entry(publish_task_id,expected_video_id,comment_key)))


@mcp.tool()
async def inspect_douyin_published_video_analytics(publish_task_id: str, expected_video_id: str) -> dict:
    """Read overview, traffic and audience analytics for one verified owned published work. No edits or posts."""
    return await asyncio.to_thread(lambda:asyncio.run(comment_service.inspect_analytics(publish_task_id,expected_video_id)))


@mcp.tool()
async def inspect_douyin_comments(publish_task_id: str, open_work: bool=False, open_comments: bool=False, public_view: bool=False) -> dict:
    """Read visible DOM for one previously PUBLISHED Douyin work in its existing account.

    Reuses the service-owned locked session. Never posts, exports credentials, or
    accepts arbitrary URLs/selectors/scripts. open_work only opens the matched work.
    """
    with comment_service.activity('inspect'):
        return await asyncio.to_thread(lambda: asyncio.run(comment_service.inspect(publish_task_id, open_work, open_comments, public_view)))


@mcp.tool()
def prepare_douyin_comment(publish_task_id: str, text: str, idempotency_key: str) -> dict:
    """Freeze one top-level comment for a previously published own Douyin video. No posting."""
    return comment_service.prepare(publish_task_id, text, idempotency_key)


@mcp.tool()
def get_douyin_comment_status(comment_task_id: str) -> dict:
    """Read durable single-comment state. UNKNOWN_RESULT is not permission to retry."""
    return comment_service.comments.get(comment_task_id)


@mcp.tool()
async def submit_douyin_comment(comment_task_id: str, expected_video_id: str) -> dict:
    """Send the exact frozen top-level comment ONCE on the user's previously published work.

    Requires explicit user authorization for that text and work. Uses the existing
    publisher account/session, journals before clicking, and verifies after reload.
    An attempted/unknown task never clicks again; use recheck_douyin_comment instead.
    """
    return await asyncio.to_thread(lambda: asyncio.run(comment_service.post(comment_task_id, expected_video_id)))


@mcp.tool()
async def recheck_douyin_comment(comment_task_id: str) -> dict:
    """Read the same work's comments again. Never sends or retries a comment."""
    return await asyncio.to_thread(lambda: asyncio.run(comment_service.recheck(comment_task_id)))


@mcp.tool()
def list_accounts() -> list[dict]:
    """List local account aliases without credentials. UNKNOWN is not logged-in proof."""
    result=[]
    for account in accounts_config():
        status=ledger.account_status(account['id'])
        checked=status.get('checked_at')
        if status['status']=='READY' and (not checked or (datetime.now(timezone.utc)-datetime.fromisoformat(checked)).total_seconds()>300):
            status={**status,'status':'STALE'}
        result.append({**status, **{k:account.get(k) for k in ('id','channel','display_name','default')}})
    return result



@mcp.tool()
def list_publish_channels() -> list[dict]:
    """Report implemented adapters separately from real publication acceptance."""
    accounts=list_accounts()
    result=[]
    for channel in ('douyin','wechat_channels','xiaohongshu'):
        configured=[a for a in accounts if a['channel']==channel]
        ready=[a for a in configured if a['status']=='READY']
        with ledger.connect() as db:
            accepted=any(json.loads(row[0])['channel']==channel for row in db.execute("SELECT plan FROM publish_attempts WHERE state='PUBLISHED'"))
        result.append(dict(channel=channel,implemented=True,available=bool(ready),supported_media=['video'],
                           accounts=configured,tool_publish_acceptance='PASSED' if accepted else 'NOT_TESTED',
                           declaration=declaration_capability(channel),
                           reason=None if ready else 'ACCOUNT_CHECK_REQUIRED'))
    return result



@mcp.tool()
def prepare_publish(media_path: str, title: str, description: str,
                    declaration: Annotated[SelfDeclaration, BeforeValidator(declaration_input)],
                    tags: list[str], channels: list[str] | None = None,
                    accounts: dict[str, str] | None = None) -> dict:
    """Freeze one ordered batch; default includes ALL three platforms, even logged-out ones.

    No upload occurs here. submit_publish triggers a fresh all-account preflight barrier.
    declaration is an exact supported self-declaration from list_publish_channels;
    "原创" is not a supported self-declaration. Never map or clear it automatically.
    """
    selected=list(ORDER) if channels is None else channels
    if not selected or len(selected)!=len(set(selected)) or any(c not in ORDER for c in selected):
        raise ValueError('INVALID_CHANNELS')
    selected=sorted(selected,key=ORDER.index)
    declaration = declaration.value if isinstance(declaration, SelfDeclaration) else declaration
    try:
        for channel in selected:
            validate_declaration(channel, declaration)
    except DeclarationError as exc:
        raise ValueError(json.dumps(exc.to_dict(), ensure_ascii=False)) from exc
    path=Path(media_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() not in {'.mp4','.mov','.mkv','.webm'} or path.stat().st_size==0:
        raise ValueError('INVALID_VIDEO')
    original_title = title
    title = clean_publish_title(title)
    digest=media_digest(path)
    items=[]
    for channel in selected:
        candidates=[a for a in accounts_config() if a['channel']==channel]
        requested=(accounts or {}).get(channel)
        if requested:
            candidates=[a for a in candidates if a['id']==requested]
        elif len(candidates)>1:
            candidates=[a for a in candidates if a.get('default')]
        if len(candidates)!=1:
            raise ValueError('EXPLICIT_ACCOUNT_REQUIRED:'+channel)
        items.append(dict(channel=channel,account_id=candidates[0]['id'],media_path=str(path),
                          media_sha256=digest,title=platform_title(title,channel),description=description,tags=tags,declaration=declaration))
    plan=items[0] if len(items)==1 else dict(items=items)
    plan_id=uuid.uuid4().hex
    with ledger.connect() as db:
        db.execute('INSERT INTO frozen_plans VALUES (?,?)',(plan_id,json.dumps(plan,ensure_ascii=False)))
    return dict(plan_id=plan_id,plan=plan,order=selected,preflight_required=True,
                title=items[0]['title'] if len(items)==1 else title,
                title_changed=any(item['title'] != original_title for item in items),
                titles={item['channel']:item['title'] for item in items})


def serialized_mutation(fn):
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def async_wrapped(*args, **kwargs):
            if not worker_runtime.mutation_lock.acquire(blocking=False):
                raise ValueError('TASK_BUSY')
            try:
                return await fn(*args, **kwargs)
            finally:
                worker_runtime.mutation_lock.release()
        return async_wrapped
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not worker_runtime.mutation_lock.acquire(blocking=False):
            raise ValueError('TASK_BUSY')
        try:
            return fn(*args, **kwargs)
        finally:
            worker_runtime.mutation_lock.release()
    return wrapped


@mcp.tool()
@serialized_mutation
def delete_publish_task(target_id: str) -> dict:
    """Delete an unsubmitted batch, standalone task, or frozen plan by its MCP ID.

    Requires explicit user deletion intent. Batch children require the batch ID.
    Busy, published, or attempted/unknown-result tasks are rejected. Never deletes
    media files or platform content. NOT_FOUND does not prove a remote non-publication.
    Caller must synchronize its local record only after a confirmed DELETED result.
    Success is durably audited. Repeating the exact target returns the original receipt.
    get_publish_batch/get_publish_status can recover that receipt read-only after a timeout.
    """
    return delete_publish(ledger, target_id)


def frozen_plan(plan_id):
    with ledger.connect() as db:
        row=db.execute('SELECT body FROM frozen_plans WHERE id=?',(plan_id,)).fetchone()
    if row is None:
        raise ValueError('PLAN_NOT_FOUND')
    return json.loads(row['body'])


@mcp.tool()
async def preflight_publish(plan_id: str) -> dict:
    """Read-only checks of every account and video upload page; never uploads or submits."""
    checks=await asyncio.to_thread(lambda:asyncio.run(batches.inspect(frozen_plan(plan_id))))
    ready=[c['status']=='READY' and c.get('publish_input_verified') is True for c in checks]
    return dict(ready=any(ready),any_ready=any(ready),all_ready=bool(ready) and all(ready),checks=checks,
                note='Read-only snapshot; each authorized target repeats its own preflight before upload',**SCHEDULING)


@mcp.tool()
def get_publish_batch(batch_id: str) -> dict:
    """Independent target status, per-account checks, login sessions, and partial completion."""
    try:
        return batches.get(batch_id)
    except KeyError:
        receipt=deletion_receipt(ledger,batch_id)
        if receipt and batch_id in receipt['deleted_batch_ids']:
            return receipt
        raise


@mcp.tool()
@serialized_mutation
def resume_publish_batch(batch_id: str) -> dict:
    """Resume unsubmitted targets independently; attempted targets only reconcile, never repeat a click."""
    try:
        return batches.resume(batch_id)
    except DeclarationError as exc:
        raise ValueError(json.dumps(exc.to_dict(), ensure_ascii=False)) from exc


@mcp.tool()
@serialized_mutation
async def recover_uncertain_publish(task_id: str, expected_attempted_at: str, request_id: str,
                              reason: str, acknowledge_duplicate_risk: bool=False) -> dict:
    """Explicit one-shot retry of an old UNKNOWN_RESULT after fresh reconciliation.

    Requires a separate confirmation acknowledging possible duplicate publication.
    Keeps the original task/batch and journals the uncertain attempt. Does NOT
    certify non-publication. Reusing request_id never grants another attempt.
    Currently supported when a fresh WeChat management observation is available.
    """
    from utils.publish_recovery import recover_uncertain
    return await asyncio.to_thread(recover_uncertain, ledger, batches, reconcile, task_id=task_id,
        expected_attempted_at=expected_attempted_at, request_id=request_id,
        reason=reason, acknowledge_duplicate_risk=acknowledge_duplicate_risk)


@mcp.tool()
@serialized_mutation
def submit_publish(plan_id: str, idempotency_key: str) -> dict:
    """Authorize the frozen batch for REAL publication, independently after each target passes preflight."""
    try:
        batch=batches.submit(idempotency_key,frozen_plan(plan_id))
    except DeclarationError as exc:
        raise ValueError(json.dumps(exc.to_dict(), ensure_ascii=False)) from exc
    return dict(batch_id=batch['id'],task_id=batch['tasks'][0]['id'] if len(batch['tasks'])==1 else batch['id'],
                state=batch['state'],tasks=[dict(task_id=t['id'],channel=t['plan']['channel'],state=t['state'],
                reused=not t['owned']) for t in batch['tasks']],**SCHEDULING)


@mcp.tool()
def get_publish_status(task_id: str) -> dict:
    """Return durable state, or the original DELETED receipt for an exact removed task/plan ID.

    No deletion occurs here. Missing targets without a recorded receipt remain errors.
    UNKNOWN_RESULT must be reconciled before any retry.
    """
    receipt=deletion_receipt(ledger,task_id)
    if receipt:
        return receipt
    with ledger.connect() as db:
        is_batch=db.execute('SELECT 1 FROM publish_batches WHERE id=?',(task_id,)).fetchone()
    if is_batch:
        return batches.get(task_id)
    task = ledger.get(task_id)
    return {**{key:task[key] for key in ('id','state','created_at','attempted_at','evidence')}, **ledger.diagnostics(task_id)}


@mcp.tool()
def get_publish_evidence(task_id: str) -> dict:
    """Return only matching work evidence, never cookies or page dumps."""
    return dict(task_id=task_id, evidence=ledger.get(task_id)['evidence'])


@mcp.tool()
@serialized_mutation
def retry_preparation(task_id: str) -> dict:
    """Retry only a failure proven to have happened BEFORE any publish click."""
    with ledger.connect() as db:
        if db.execute('SELECT 1 FROM batch_tasks WHERE task_id=?',(task_id,)).fetchone():
            raise ValueError('USE_RESUME_PUBLISH_BATCH')
        try:
            validate_plan_declarations(ledger.get(task_id)['plan'])
        except DeclarationError as exc:
            raise ValueError(json.dumps(exc.to_dict(), ensure_ascii=False)) from exc
        result = db.execute("UPDATE publish_attempts SET state='QUEUED' WHERE id=? AND state='FAILED' AND attempted_at IS NULL", (task_id,))
        if result.rowcount != 1:
            raise ValueError('UNSAFE_RETRY_REJECTED')
        db.execute('UPDATE task_runtime SET next_check=0,check_count=0,error=NULL WHERE task_id=?',(task_id,))
    ledger.event(task_id,'TASK','MANUAL_PREPARATION_RETRY')
    return get_publish_status(task_id)


@mcp.tool()
async def recheck_publish_result(task_id: str, force: bool=False) -> dict:
    """Read-only DOM verification. force also inspects completed work without changing its saved result."""
    task=ledger.get(task_id)
    if not task['attempted_at']:
        raise ValueError('SUBMISSION_NOT_ATTEMPTED')
    if task['state'] in {'PUBLISHED','FAILED'} and not force:
        return get_publish_status(task_id)
    result=await asyncio.to_thread(lambda:asyncio.run(reconcile(task,force)))
    return result if force else get_publish_status(task_id)


@mcp.tool()
async def check_account_status(account_id: str) -> dict:
    """Check identity using the same locked profile used for publication."""
    return await asyncio.to_thread(lambda:asyncio.run(sessions.check(account_id)))


@mcp.tool()
def start_account_login(account_id: str, visible: bool=True) -> dict:
    """Open the service-owned official browser; return a fixed local session page.

    Successful login also checks the video publish page before saving and releasing its profile.
    """
    sid=sessions.start_login(account_id,visible)
    return dict(session_id=sid,login_url=f"http://127.0.0.1:{os.environ.get('SAU_MCP_PORT','5410')}/login/{sid}",
                **sessions.login_status(sid))


@mcp.tool()
def get_login_status(session_id: str) -> dict:
    """Return login state and identity result, never QR bytes or credentials."""
    return sessions.login_status(session_id)


@mcp.tool()
def get_service_status() -> dict:
    """Check process-owned worker health."""
    return {**worker_runtime.status(), 'comment_active_operations': comment_service.active_operations(),
            'auto_reply': autoreply_worker.status(),'notifications':notification_worker.status(),
            'owned_video_replies':_owned_video_runner.status() if _owned_video_runner else {'busy':False}}


@mcp.custom_route('/login/{sid}',methods=['GET'])
async def login_page(request):
    sid=request.path_params['sid']
    if sid not in sessions.logins:
        return Response(status_code=404)
    return HTMLResponse('<!doctype html><meta charset="utf-8"><title>发布账号登录</title>\n<style>body{font:18px system-ui;max-width:680px;margin:40px auto}img{max-width:280px}</style>\n<h1>发布账号登录</h1><p>请在服务打开的官方浏览器中登录。成功后服务会自动保存并关闭窗口。</p>\n<p id="s">正在连接…</p><img id="qr" hidden><p><button onclick="act(\'refresh\')">刷新二维码</button> <button onclick="act(\'cancel\')">取消登录</button></p>\n<script>const base=location.pathname;async function act(a){await fetch(base+\'/\'+a,{method:\'POST\'})}\nsetInterval(async()=>{const r=await fetch(base+\'/status\');const s=await r.json();document.querySelector(\'#s\').textContent=s.status;\nlet img=document.querySelector(\'#qr\');img.hidden=s.status!==\'WAITING_FOR_SCAN\';if(!img.hidden)img.src=base+\'/qr?t=\'+Date.now();},1500)</script>',headers={'Cache-Control':'no-store'})


@mcp.custom_route('/login/{sid}/{action}',methods=['GET','POST'])
async def login_action(request):
    sid=request.path_params['sid'];action=request.path_params['action']
    if sid not in sessions.logins:
        return Response(status_code=404)
    login=sessions.logins[sid]
    if request.method=='GET' and action=='status':
        return JSONResponse(sessions.login_status(sid),headers={'Cache-Control':'no-store'})
    if request.method=='GET' and action=='qr':
        return Response(login['qr'] or b'',status_code=200 if login['qr'] else 404,media_type='image/png',headers={'Cache-Control':'no-store'})
    if request.method=='POST' and action in {'refresh','cancel'}:
        origin=request.headers.get('origin')
        if origin and origin!=str(request.base_url).rstrip('/'):
            return Response(status_code=403)
        login[action].set()
        return JSONResponse({'accepted':True})
    return Response(status_code=405)


if __name__ == '__main__':
    with FileLock(RUNTIME/'service.lock'):
        worker_runtime.start()
        autoreply_worker.start()
        notification_worker.start()
        try:
            mcp.run(transport='streamable-http')
        finally:
            worker_runtime.stop.set()
            autoreply_worker.stop()
            notification_worker.stop()
