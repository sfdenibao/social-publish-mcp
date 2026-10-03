"""Validate public contracts without loading live accounts or starting workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ReleaseContracts(unittest.TestCase):
    def test_platform_titles_and_real_work_context(self):
        code = r'''
import json
from pathlib import Path
import publish_mcp as s
(s.RUNTIME/'accounts.json').write_text(json.dumps([{'id':c,'channel':c} for c in s.ORDER]))
media=s.RUNTIME/'fixture.mp4';media.write_bytes(b'fixture-not-a-real-video')
title='一二三四五六七八九十一二三四五六七八九十一'
r=s.prepare_publish(str(media),title,'猫咪躲进纸箱','',[])
assert len(r['titles']['xiaohongshu'])==20 and r['title_changed']
assert r['titles']['douyin']==title
assert s.frozen_plan(r['plan_id'])['items'][2]['title']==title[:20]
task={'plan':{'title':'猫咪纸箱','description':'猫咪躲进纸箱'}}
s.comment_service.published_task=lambda _: (task,{'id':'account'})
s.autoreply_store.history=lambda *a,**k: []
context=s.autoreply_context({'account_id':'account','publish_task_id':'id','video_id':'1234567890123','id':'job','config':{'post_context':'obsolete unrelated promotion'}})
assert context['post_description']=='猫咪躲进纸箱'
from utils.douyin_notifications import DouyinNotificationService
from types import SimpleNamespace
service=DouyinNotificationService(SimpleNamespace(sessions=SimpleNamespace(account=lambda _: {'public_identity':{'handle':'example','profile_path':'/user/example'}})))
assert service.public_identity('account')['handle']=='example'
'''
        with tempfile.TemporaryDirectory() as temp:
            result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).resolve().parents[1],
                env={**os.environ,'SAU_RUNTIME_DIR':temp},capture_output=True,text=True,encoding='utf-8')
            self.assertEqual(result.returncode,0,result.stderr)
