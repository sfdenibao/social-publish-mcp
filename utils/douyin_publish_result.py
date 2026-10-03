"""Read-only Douyin management DOM verification; no navigation proves success."""
import uuid
import re
from datetime import datetime, timedelta, timezone

MANAGEMENT_URL = 'https://creator.douyin.com/creator-micro/content/manage'
CHINA_TIME = timezone(timedelta(hours=8))


async def read_records(page):
    if not page.url.startswith(MANAGEMENT_URL):
        raise RuntimeError('MANAGEMENT_PAGE_REQUIRED')
    return await page.locator('[class*="video-card-info-"]').evaluate_all('''nodes => nodes.map(node => ({
        title: node.querySelector('[class*="info-title-text-"]')?.innerText?.trim() || '',
        text: node.innerText || ''
    }))''')


def match_record(records, title, attempted_at, description='', tags=None):
    attempted = datetime.fromisoformat(attempted_at)
    if attempted.tzinfo is None:
        raise ValueError('ATTEMPT_TIMEZONE_REQUIRED')
    # The management card can render the whole caption. Preserve every tag in
    # uploader order; only normalize whitespace from contenteditable/DOM layout.
    if tags:
        expected_titles = {' '.join(' '.join(
            [title, description or '', *('#' + tag for tag in tags)]
        ).split())}
    else:
        expected_titles = {' '.join(value.split()) for value in (
            title, ' '.join(part for part in (title, description) if part)
        )}
    matches = []
    for record in records:
        observed_title = record.get('title')
        if not isinstance(observed_title, str) or ' '.join(observed_title.split()) not in expected_titles:
            continue
        stamp = re.search(r'(\d{4})年(\d{2})月(\d{2})日\s+(\d{2}):(\d{2})', record.get('text', ''))
        if not stamp:
            continue
        published_at = datetime(*map(int, stamp.groups()), tzinfo=CHINA_TIME)
        # UI displays minute precision. Reject older same-title works.
        if not attempted - timedelta(minutes=1) <= published_at <= attempted + timedelta(minutes=10):
            continue
        lines = {line.strip() for line in record['text'].splitlines()}
        statuses = [(label, state) for label, state in (
            ('已发布', 'PUBLISHED'), ('审核中', 'PENDING_REVIEW'),
            ('未通过', 'FAILED'), ('审核未通过', 'FAILED'),
        ) if label in lines]
        if len(statuses) != 1:
            continue
        label, state = statuses[0]
        matches.append(dict(state=state, status_text=label, published_at=published_at.isoformat()))
    if len(matches) != 1:
        return None
    return matches[0]


async def verify_result(page, ledger, task_id, max_checks=6, interval_ms=5000):
    task = ledger.get(task_id)
    for index in range(max_checks):
        try:
            await page.goto(MANAGEMENT_URL, wait_until='domcontentloaded', timeout=30000)
            await page.locator('[class*="video-card-info-"]').first.wait_for(timeout=15000)
            matched = match_record(await read_records(page), task['plan']['title'], task['attempted_at'],
                                   task['plan'].get('description', ''), task['plan'].get('tags'))
            if matched:
                evidence = dict(channel='douyin', account_id=task['plan']['account_id'],
                                title=task['plan']['title'], management_url=MANAGEMENT_URL,
                                observed_at=datetime.now(timezone.utc).isoformat(),
                                matched_by='title_and_publish_time', **matched)
                from pathlib import Path
                screenshot = Path(ledger.path).parent / (task_id + '-management-' + uuid.uuid4().hex[:12] + '.png')
                await page.screenshot(path=str(screenshot), full_page=True)
                evidence['evidence_ref'] = str(screenshot)
                ledger.record_dom_result(task_id, matched['state'], evidence)
                return ledger.get(task_id)
        except Exception:
            # DOM/network failure is not permission to resubmit.
            pass
        if index + 1 < max_checks:
            await page.wait_for_timeout(interval_ms)
    raise RuntimeError('UNKNOWN_RESULT: 请核查平台记录，禁止直接重发')
