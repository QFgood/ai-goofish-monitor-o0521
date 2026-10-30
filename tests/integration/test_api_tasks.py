import asyncio
import time


def test_create_list_update_delete_task(api_client, api_context, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200
    created = response.json()["task"]
    assert created["task_name"] == sample_task_payload["task_name"]
    assert created["analyze_images"] is True
    assert created["next_run_at"] == "2026-03-19T08:15:00+08:00"

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    tasks = response.json()
    assert len(tasks) == 1
    assert tasks[0]["keyword"] == sample_task_payload["keyword"]
    assert tasks[0]["analyze_images"] is True
    assert tasks[0]["next_run_at"] == "2026-03-19T08:15:00+08:00"

    response = api_client.patch("/api/tasks/0", json={"enabled": False, "analyze_images": False})
    assert response.status_code == 200
    updated = response.json()["task"]
    assert updated["enabled"] is False
    assert updated["analyze_images"] is False
    assert updated["next_run_at"] is None

    response = api_client.delete("/api/tasks/0")
    assert response.status_code == 200

    response = api_client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == []


def test_start_stop_task_updates_status(api_client, api_context, sample_task_payload):
    response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert response.status_code == 200

    response = api_client.post("/api/tasks/start/0")
    assert response.status_code == 200

    response = api_client.get("/api/tasks/0")
    assert response.status_code == 200
    assert response.json()["is_running"] is True

    response = api_client.post("/api/tasks/stop/0")
    assert response.status_code == 200

    response = api_client.get("/api/tasks/0")
    assert response.status_code == 200
    assert response.json()["is_running"] is False

    process_service = api_context["process_service"]
    assert process_service.started == [(0, sample_task_payload["task_name"])]
    assert process_service.stopped == [0]


def test_generate_keyword_mode_task_without_ai_criteria(api_client):
    payload = {
        "task_name": "A7M4 关键词筛选",
        "keyword": "sony a7m4",
        "description": "",
        "decision_mode": "keyword",
        "keyword_rules": ["a7m4", "验货宝"],
        "max_pages": 2,
        "personal_only": True,
    }

    response = api_client.post("/api/tasks/generate", json=payload)
    assert response.status_code == 200
    created = response.json()["task"]
    assert created["decision_mode"] == "keyword"
    assert created["ai_prompt_criteria_file"] == ""
    assert created["keyword_rules"] == ["a7m4", "验货宝"]


def test_generate_ai_task_returns_job_and_completes_async(api_client, api_context, monkeypatch):
    payload = {
        "task_name": "Apple Watch S10",
        "keyword": "apple watch s10",
        "description": "只看国行蜂窝版，电池健康高于 95%，拒绝维修机。",
        "analyze_images": False,
        "decision_mode": "ai",
        "max_pages": 2,
        "personal_only": True,
    }

    async def fake_generate_criteria(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return "[V6.3 核心升级]\\nApple Watch criteria"

    monkeypatch.setattr(
        "src.services.task_generation_runner.generate_criteria",
        fake_generate_criteria,
    )

    response = api_client.post("/api/tasks/generate", json=payload)

    assert response.status_code == 202
    job = response.json()["job"]
    assert isinstance(job["job_id"], str)
    assert job["status"] in {"queued", "running"}
    assert job["task"] is None

    status_response = api_client.get(f"/api/tasks/generate-jobs/{job['job_id']}")
    assert status_response.status_code == 200

    for _ in range(50):
        status_response = api_client.get(f"/api/tasks/generate-jobs/{job['job_id']}")
        latest_job = status_response.json()["job"]
        if latest_job["status"] == "completed":
            break
        time.sleep(0.02)
    else:
        raise AssertionError("任务生成作业未在预期时间内完成")

    assert latest_job["task"]["task_name"] == payload["task_name"]
    assert latest_job["task"]["ai_prompt_criteria_file"].endswith("_criteria.txt")
    assert latest_job["task"]["analyze_images"] is False
    assert api_context["scheduler_service"].reload_calls == 1


def test_create_task_accepts_cron_alias(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["cron"] = "@daily"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 200
    assert response.json()["task"]["cron"] == "0 0 * * *"


def test_create_task_rejects_fixed_account_strategy_without_state_file(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["account_strategy"] = "fixed"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 422


def test_create_task_accepts_rotate_account_strategy(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["account_strategy"] = "rotate"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 200
    task = response.json()["task"]
    assert task["account_strategy"] == "rotate"


def test_update_task_accepts_six_field_cron_expression(api_client, sample_task_payload):
    create_response = api_client.post("/api/tasks/", json=sample_task_payload)
    assert create_response.status_code == 200

    response = api_client.patch("/api/tasks/0", json={"cron": "0 0 8 * * *"})

    assert response.status_code == 200

    task_response = api_client.get("/api/tasks/0")
    assert task_response.status_code == 200
    assert task_response.json()["cron"] == "0 0 8 * * *"


def test_create_task_rejects_invalid_cron_expression(api_client, sample_task_payload):
    payload = dict(sample_task_payload)
    payload["cron"] = "every day at 8"

    response = api_client.post("/api/tasks/", json=payload)

    assert response.status_code == 422


def test_delete_task_stops_runtime_and_reindexes_process_state(
    api_client,
    api_context,
    sample_task_payload,
):
    second_payload = dict(sample_task_payload)
    second_payload["task_name"] = "Sony A7CR"
    second_payload["keyword"] = "sony a7cr"
    second_payload["ai_prompt_criteria_file"] = "prompts/sony_a7cr_criteria.txt"

    assert api_client.post("/api/tasks/", json=sample_task_payload).status_code == 200
    assert api_client.post("/api/tasks/", json=second_payload).status_code == 200
    assert api_client.post("/api/tasks/start/0").status_code == 200

    response = api_client.delete("/api/tasks/0")

    assert response.status_code == 200
    process_service = api_context["process_service"]
    assert process_service.stopped == [0]
    assert process_service.reindexed == []


def test_revisit_settings_creation_update_and_delete_preserves_registered_results(api_client,api_context,sample_task_payload,monkeypatch,tmp_path):
    from src.services.result_storage_service import save_result_record
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('APP_DATABASE_FILE',str(api_context['db_path']))
    payload={**sample_task_payload,'revisit_enabled':True,'revisit_cron':'0 20 * * *','revisit_limit':3,'revisit_days':15}
    created=api_client.post('/api/tasks/',json=payload).json()['task']
    assert created['revisit_limit']==3 and created['revisit_cron']=='0 20 * * *'
    asyncio.run(save_result_record({'任务名称':created['task_name'],'商品信息':{'商品ID':'99001',
        '商品标题':'offline fixture','当前售价':'5000','商品链接':'https://www.goofish.com/item?id=99001'}},created['keyword']))
    status=api_client.get(f"/api/tasks/{created['id']}/revisits").json()
    assert status['registered']==1
    assert api_client.patch(f"/api/tasks/{created['id']}",json={'revisit_limit':101}).status_code==422
    updated=api_client.patch(f"/api/tasks/{created['id']}",json={'revisit_enabled':False}).json()['task']
    assert updated['revisit_enabled'] is False and updated['revisit_cron']=='0 20 * * *'
    assert api_client.delete(f"/api/tasks/{created['id']}").status_code==200
    with sqlite_connection() as conn:
        assert conn.execute('SELECT count(*) FROM result_items').fetchone()[0]==1
        assert conn.execute('SELECT archived FROM result_listing_memberships').fetchone()[0]==1


def test_manual_revisit_api_accepts_background_work_and_exposes_persisted_result(api_context,monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from src.services.result_revisit_service import ResultRevisitService
    from src.services.result_revisit_manager import ResultRevisitManager
    from src.services.result_storage_service import save_result_record
    from src.api.auth import APIAuthenticationMiddleware
    class Adapter:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def item_listing(self,item_id):
            return {'request_ok':True,'page_complete':True,'raw_status':1,'price':'4500'}
    def record():return {'任务名称':'Mini','商品信息':{'商品ID':'1001','当前售价':'5000','商品标题':'Offline Fixture'},'卖家信息':{}}
    monkeypatch.chdir(tmp_path);monkeypatch.setenv('APP_DATABASE_FILE',str(api_context['db_path']))
    monkeypatch.setenv('WEB_AUTH_ENABLED','false')
    app=api_context['app'];app.add_middleware(APIAuthenticationMiddleware)
    manager=ResultRevisitManager(ResultRevisitService(lambda _:Adapter(),db_path=str(api_context['db_path'])))
    app.state.result_revisit_manager=manager
    with TestClient(app) as client:
        task=client.post('/api/tasks/',json={'task_name':'Mini','keyword':'mini','decision_mode':'keyword','keyword_rules':['mini'],
            'revisit_enabled':False}).json()['task']
        asyncio.run(save_result_record(record(),'mini'))
        response=client.post(f"/api/tasks/{task['id']}/revisits/run")
        assert response.status_code==202
        run_id=response.json()['run_id']
        for _ in range(100):
            latest=client.get('/api/tasks').json()[0]
            if latest['revisit_run']['status']!='running':break
            time.sleep(.01)
        assert latest['revisit_run']['run_id']==run_id
        assert latest['revisit_run']['reviewed']==1 and latest['revisit_run']['sold']==1
        assert latest['revisit_enabled'] is False
        assert client.post('/api/tasks/9999/revisits/run').status_code==404
        client.patch(f"/api/tasks/{task['id']}",json={'enabled':False})
        assert client.post(f"/api/tasks/{task['id']}/revisits/run").status_code==400


def test_manual_revisit_api_conflict_and_server_auth_guard(api_context,monkeypatch,tmp_path):
    from fastapi.testclient import TestClient
    from src.api.auth import APIAuthenticationMiddleware
    from src.services.result_revisit_service import ResultRevisitService
    monkeypatch.chdir(tmp_path);monkeypatch.setenv('APP_DATABASE_FILE',str(api_context['db_path']))
    monkeypatch.setenv('WEB_AUTH_ENABLED','false')
    app=api_context['app'];app.add_middleware(APIAuthenticationMiddleware)
    with TestClient(app) as client:
        task=client.post('/api/tasks/',json={'task_name':'Mini','keyword':'mini','decision_mode':'keyword','keyword_rules':['mini']}).json()['task']
        prepared=ResultRevisitService().prepare_run(task['id'],manual=True)
        assert client.post(f"/api/tasks/{task['id']}/revisits/run").status_code==409
        assert client.post(f"/api/tasks/{task['id']}/revisits/run",headers={'Origin':'https://example.invalid'}).status_code==403
        monkeypatch.setenv('WEB_AUTH_ENABLED','true')
        assert client.post(f"/api/tasks/{task['id']}/revisits/run").status_code==401
