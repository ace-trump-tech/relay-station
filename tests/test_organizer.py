import asyncio
import copy
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.canvas import Canvas, canvas_origin
from app.engine import DEFAULTS
from app.main import create_app
from app.organizer import PROFILE, assess, available_minutes, extract_message, profile
from app.store import Store


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv('RELAY_ADMIN_PASSWORD', raising=False)
    return create_app(tmp_path/'data',origin='http://testserver',background=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.headers.update({'Origin':'http://testserver','X-Relay-Request':'1'})
        response=c.post('/api/setup',json={'password':'organizer-test-password'})
        c.headers['X-CSRF-Token']=response.json()['csrf']
        yield c


def make(client, **values):
    response=client.post('/api/items',json={'title':'测试课程作业',**values})
    assert response.status_code==200,response.text
    return response.json()


def pending(app, prefix='item:'):
    with app.state.store.connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM reminders WHERE acknowledged=0 AND id LIKE ?',(prefix+'%',))]


def future(days=3):
    return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat()


def message(ident='1',body='作业请于 2026-11-01 20:00 提交',source='gmail'):
    return {'id':ident,'title':'高等数学第三次课程作业','body':body,'source':source,'sender':'teacher@example.test'}


def test_default_capture_is_review_only_without_extra_message_alerts(app,client):
    app.state.ingest('gmail',{'external_id':'urgent','title':'紧急作业截止通知','body':'明天提交','sender':'teacher'})
    data=client.get('/api/state').json()
    assert len(data['items'])==1
    assert data['items'][0]['status']=='inbox'
    assert data['items'][0]['due_at'] is None
    assert not pending(app,'')


def test_date_extraction_does_not_invent_ambiguous_dates():
    extracted=extract_message(message(),PROFILE,'Asia/Shanghai')
    assert extracted['due_at']=='2026-11-01T12:00:00+00:00'
    assert extracted['verification']=='needs_review'
    for text in ['作业明晚交','作业 2026-11-01 截止','作业 2026-11-01 20:00 或 2026-11-02 18:00','作业 2026-02-30 12:00']:
        assert extract_message(message(body=text),PROFILE,'Asia/Shanghai')['due_at'] is None


def test_exact_dedupe_and_ambiguous_messages_stay_separate(app):
    o=app.state.organizer
    first=o.capture(message())
    same=o.capture(message(ident='2',source='qqmail'))
    assert first['id']==same['id'] and len(same['sources'])==2
    a=o.capture(message('3',body='作业明天交'))
    b=o.capture(message('4',body='作业明天交'))
    assert a['id']!=b['id']


def test_capture_policy_and_manual_capture(app,client):
    pref=copy.deepcopy(PROFILE);pref['sources']['gmail']['capture']=False
    assert client.put('/api/organizer/profile',json=pref).status_code==200
    assert app.state.organizer.capture(message()) is None
    assert app.state.organizer.capture(message(),force=True)['status']=='inbox'
    pref['capture_rules']=[{'id':'1','name':'申请通知','keywords':'新通知','source':'gmail','topic':'升学','importance':'high'}]
    pref['sources']['gmail']['capture']=True
    client.put('/api/organizer/profile',json=pref)
    result=app.state.organizer.capture({**message('new'),'title':'新通知','body':'阅读材料'})
    assert result['topic']=='升学' and result['importance_override']=='high'


def test_reminders_confirmation_edit_ack_and_revised_deadline(app,client):
    item=make(client,due_at=future(),verification='needs_review',status='inbox',remind_offsets=[60])
    assert not pending(app)
    url='/api/items/'+item['id']
    assert client.patch(url,json={'verification':'confirmed','status':'todo'}).status_code==200
    before={r['id'] for r in pending(app)}
    assert len(before)==2
    with app.state.store.connect() as db:
        db.execute('UPDATE reminders SET acknowledged=1')
    client.patch(url,json={'notes':'只修改备忘内容','importance_override':'critical'})
    assert not pending(app)
    client.patch(url,json={'due_at':future(4)})
    assert len(pending(app))==2 and not before.intersection(r['id'] for r in pending(app))
    client.patch(url,json={'remind_mode':'digest'})
    assert not pending(app)
    client.patch(url,json={'remind_mode':'deadline'})
    assert len(pending(app))==2
    client.patch(url,json={'status':'done'})
    assert not pending(app)


def assignment(**extra):
    return {'external_id':'school:3:7','title':'Canvas 线性代数作业','notes':'完成证明',
            'source_url':'https://school.example/courses/3/assignments/7','course':'线性代数',
            'due_at':future(),'submission_status':'unsubmitted',**extra}


def test_canvas_updates_preserve_user_choice_and_suspend_unconfirmed_reminders(app,client):
    o=app.state.organizer
    item=o.canvas_assignment(assignment())
    url='/api/items/'+item['id']
    client.patch(url,json={'importance_override':'low','title':'自己的作业名称','status':'todo','verification':'confirmed'})
    assert pending(app)
    updated=o.canvas_assignment(assignment(due_at=future(5)))
    assert updated['importance_override']=='low' and updated['title']=='自己的作业名称'
    assert updated['due_at']==item['due_at'] and updated['suggested_due_at']!=item['due_at']
    assert updated['verification']=='needs_review' and not pending(app)
    submitted=o.canvas_assignment(assignment(due_at=future(5),submission_status='submitted'))
    assert submitted['status']=='todo'
    assert '已提交' in submitted['changes'][-2]['note'] or any('已提交' in c['note'] for c in submitted['changes'])
    assert o.canvas_assignment(assignment(external_id='new:submitted',submission_status='graded')) is None


def test_manual_importance_controls_attention_without_changing_suggested_score():
    item={'factors':dict.fromkeys(['relevance','loss','commitment'],.1),'importance_override':'critical','due_at':None,'verification':'confirmed'}
    a=assess(item,PROFILE,DEFAULTS,[],'Asia/Shanghai')
    b=assess({**item,'importance_override':None},PROFILE,DEFAULTS,[],'Asia/Shanghai')
    assert a['score']==b['score']==10 and a['importance']=='critical'
    assert a['attention']>b['attention']


def test_capacity_unions_overlapping_courses_and_lunch():
    a=datetime.fromisoformat('2026-10-19T09:00:00+08:00');b=a.replace(hour=18)
    blocks=[{'start':a.isoformat(),'end':a.replace(hour=11).isoformat()},
            {'start':a.replace(hour=10).isoformat(),'end':a.replace(hour=12,minute=30).isoformat()}]
    assert available_minutes(a,b,DEFAULTS,blocks)==300
    assert available_minutes(b,a,DEFAULTS,blocks)==0


def test_weekly_classes_atomic_csv_and_reminder_toggle(app,client):
    response=client.post('/api/blocks',json={'title':'概率论','start':'2026-11-02T09:00:00+08:00','end':'2026-11-02T10:30:00+08:00','weeks':3})
    assert response.status_code==200
    blocks=response.json()['blocks']
    assert len(blocks)==3 and len(pending(app,'class:'))==3
    client.patch('/api/blocks/'+blocks[0]['id'],json={'remind_minutes':None})
    assert len(pending(app,'class:'))==2
    bad='title,start,end\n有效,2026-11-02T09:00:00+08:00,2026-11-02T10:00:00+08:00\n错误,no,no'
    assert client.post('/api/blocks/import',json={'csv':bad}).status_code==422
    assert len(app.state.store.blocks())==3
    assert client.patch('/api/blocks/'+blocks[0]['id'],json={'remind_minutes':True}).status_code==422


def test_confirmed_item_plan_effort_course_avoidance_and_stale_commit(app,client):
    settings={**DEFAULTS,'weekdays':list(range(7))}
    client.put('/api/settings',json=settings)
    day=(datetime.now(timezone.utc)+timedelta(days=1)).date().isoformat()
    client.post('/api/blocks',json={'title':'整天上课','start':day+'T09:00:00+08:00','end':day+'T18:00:00+08:00'})
    item=make(client,include_in_plan=True,estimated_minutes=90,due_at=future(10))
    make(client,include_in_plan=False)
    make(client,include_in_plan=True,verification='needs_review')
    proposal=client.post('/api/items/plan').json()
    assert len(proposal['tasks'])==1
    task=proposal['tasks'][0]
    assert task['item_id']==item['id'] and task['message_id'] is None
    assert (datetime.fromisoformat(task['end'])-datetime.fromisoformat(task['start'])).total_seconds()==5400
    for b in app.state.store.blocks():
        assert not (datetime.fromisoformat(task['start'])<datetime.fromisoformat(b['end']) and datetime.fromisoformat(task['end'])>datetime.fromisoformat(b['start']))
    client.patch('/api/items/'+item['id'],json={'status':'done'})
    assert client.post('/api/plan/'+proposal['id']+'/commit').status_code==409


def test_complete_item_finishes_linked_schedule_and_stops_reminders(app,client):
    item=make(client,include_in_plan=True)
    proposal=client.post('/api/items/plan').json()
    assert client.post('/api/plan/'+proposal['id']+'/commit').status_code==200
    assert len(app.state.store.tasks())==1
    client.patch('/api/items/'+item['id'],json={'status':'done'})
    assert app.state.store.tasks()[0]['done'] and not pending(app,'task:')


def test_merge_keeps_target_choice_and_conflicting_dates_need_review(app,client):
    a=make(client,importance_override='low',due_at=future())
    b=make(client,importance_override='critical',due_at=future(5))
    result=client.post('/api/items/'+b['id']+'/merge/'+a['id']).json()
    assert result['importance_override']=='low' and result['verification']=='needs_review'
    assert next(i for i in app.state.store.items() if i['id']==b['id'])['status']=='dismissed'


def test_validation_session_and_encryption(app,client):
    assert client.post('/api/items',json={'title':'bad','due_at':'2026-10-20T10:00:00'}).status_code==422
    assert client.post('/api/items',json={'title':'bad','remind_offsets':[-1]}).status_code==422
    assert client.post('/api/items',json={'title':'bad','source_url':'javascript:alert(1)'}).status_code==422
    item=make(client,notes='加密的私人备忘')
    assert client.patch('/api/items/'+item['id'],json={'estimated_minutes':-1}).status_code==422
    assert client.post('/api/items',json={'title':'bad'},headers={'X-CSRF-Token':'wrong'}).status_code==403
    assert '加密的私人备忘'.encode() not in app.state.store.path.read_bytes()
    client.post('/api/logout')
    assert client.get('/api/organizer').status_code==401
    assert client.post('/api/blocks',json={}).status_code==401


def test_v02_database_migration_preserves_tasks(tmp_path):
    key=Fernet.generate_key()
    (tmp_path/'vault.key').write_bytes(key)
    with sqlite3.connect(tmp_path/'relay.sqlite3') as db:
        db.execute('CREATE TABLE tasks(id TEXT PRIMARY KEY,message_id TEXT,start TEXT NOT NULL,end TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL)')
        db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?)',('legacy',None,'2026-01-01T09:00:00+00:00','2026-01-01T10:00:00+00:00',0,Fernet(key).encrypt(b'{"id":"legacy","title":"old"}').decode()))
    store=Store(tmp_path)
    with store.connect() as db:
        assert 'item_id' in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}
    assert store.items()==[]
    assert store.tasks()[0]['title']=='old'


def test_item_schedule_reminders_honor_off_and_per_item_push_opt_in(app,client,monkeypatch):
    app.state.store.set('settings',{**DEFAULTS,'quiet_start':'00:00','quiet_end':'00:00'})
    app.state.store.set('push:feishu',{'webhook':'unused','secret':'unused'})
    item=make(client,include_in_plan=True,remind_mode='off')
    proposal=client.post('/api/items/plan').json()
    assert client.post('/api/plan/'+proposal['id']+'/commit').status_code==200
    with app.state.store.connect() as db:
        for row in db.execute('SELECT * FROM reminders').fetchall():
            data=app.state.store.decrypt(row['payload']);data['due']='2020-01-01T00:00:00+00:00'
            db.execute('UPDATE reminders SET due=?,payload=? WHERE id=?',(data['due'],app.state.store.encrypt(data),row['id']))
    calls=[]
    async def push(*args):calls.append(args)
    monkeypatch.setattr('app.main.push_feishu',push)
    assert not client.get('/api/reminders').json()
    client.patch('/api/items/'+item['id'],json={'remind_mode':'deadline'})
    assert len(client.get('/api/reminders').json())==1
    asyncio.run(app.state.deliver_reminders());assert not calls
    client.patch('/api/items/'+item['id'],json={'notify_feishu':True})
    asyncio.run(app.state.deliver_reminders());assert len(calls)==1


def test_preparation_change_rejects_stale_item_plan(app,client):
    item=make(client,include_in_plan=True)
    proposal=client.post('/api/items/plan').json()
    client.patch('/api/items/'+item['id'],json={'estimated_minutes':120})
    assert client.post('/api/plan/'+proposal['id']+'/commit').status_code==409


def test_canvas_get_only_and_no_token_leak(app,client,monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        assert request.method=='GET' and request.headers['authorization']=='Bearer test-canvas-access-token'
        path=request.url.path
        if path.endswith('/profile'):return httpx.Response(200,json={'name':'学生'})
        if path.endswith('/courses'):return httpx.Response(200,json=[{'id':3,'name':'数学'}])
        if path.endswith('/assignments'):return httpx.Response(200,json=[{'id':7,'name':'习题','due_at':future(),'description':'<b>数学</b>','submission':{'workflow_state':'unsubmitted'}}])
        if path.endswith('/calendar_events'):return httpx.Response(200,json=[])
        raise AssertionError(path)
    real=httpx.AsyncClient
    monkeypatch.setattr('app.canvas.httpx.AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))
    config={'base_url':'https://school.example','access_token':'test-canvas-access-token','sync_calendar':True}
    assert client.post('/api/connectors/canvas',json=config).status_code==200
    assert client.post('/api/sync').status_code==200
    assert len(app.state.store.items())==1 and len(calls)>=4
    assert config['access_token'] not in client.get('/api/state').text
    assert config['access_token'] not in client.get('/api/organizer').text
    assert config['access_token'].encode() not in app.state.store.path.read_bytes()
    p=profile(app.state.store);p['sources']['canvas']['enabled']=False
    client.put('/api/organizer/profile',json=p)
    count=len(calls);client.post('/api/sync');assert len(calls)==count


def test_canvas_pagination_never_sends_token_to_foreign_origin():
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200,json=[],headers={'Link':'<https://evil.example/api/v1/courses>; rel="next"'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with pytest.raises(ValueError,match='foreign'):
                await Canvas({'base_url':'https://school.example','access_token':'secret-token'}).pages(c,'/api/v1/courses')
    asyncio.run(run())
    assert len(calls)==1
    for url in ['http://school.example','https://user:pass@school.example','https://school.example/login']:
        with pytest.raises(ValueError):canvas_origin(url)
