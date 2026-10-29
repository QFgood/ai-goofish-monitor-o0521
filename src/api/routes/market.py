"""Authenticated market_tracking API. Amounts are integer fen unless documented otherwise."""
from datetime import date, timedelta
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, ConfigDict

from src.api.auth import require_user
from src.infrastructure.persistence.market_repository import MarketRepository
from src.services.market_tracking_service import MarketTrackingService
from src.services.market_statistics_service import MarketStatisticsService
from src.services.item_lifecycle_service import ItemLifecycleService
from src.services.market_types import stamp

router = APIRouter(prefix='/api/market', tags=['market_tracking'], dependencies=[Depends(require_user)])


def repository():
    return MarketRepository()


def runner(request: Request):
    service = getattr(request.app.state, 'market_runner', None)
    if service is None:
        raise HTTPException(503, '市场调度服务未启动')
    return service


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    task_name: str = Field(min_length=1, max_length=100)
    keyword: str = Field(min_length=1, max_length=200)
    mode: str = Field(default='market_tracking', pattern='^market_tracking$')
    enabled: bool = True
    tracking_days: int = Field(default=30, ge=1, le=365)
    max_pages: int = Field(default=1, ge=1, le=5)
    account_state_file: str | None = Field(default=None, max_length=100)
    discovery_time: str = Field(default='08:30', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    revisit_time: str = Field(default='09:30', pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')
    notifications: bool = True

    @field_validator('account_state_file')
    @classmethod
    def account_filename(cls, value):
        if value is not None and not re.fullmatch(r'(?:state/)?[\w-]+\.json', value):
            raise ValueError('仅允许 state 目录中的账号 JSON 文件名')
        return value


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool | None = None
    notifications: bool | None = None


class Correction(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    product_category: str = Field(min_length=1, max_length=100)
    product_variant: str = Field(min_length=1, max_length=200)
    excluded: bool


def filters(product_variant: str | None = Query(None, max_length=200),
            product_category: str | None = Query(None, max_length=100), region: str | None = Query(None, max_length=100),
            task_id: int | None = Query(None, ge=1), min_price: int | None = Query(None, ge=0),
            max_price: int | None = Query(None, ge=0), include_excluded: bool = False,
            listing_status: str | None = Query(None, pattern='^(active|sold|offline|unknown|stale_unconfirmed)$')):
    if min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(422, '最低价格不能高于最高价格（金额单位为分）')
    return {k: v for k, v in locals().items()}


def not_found(call):
    try:
        return call()
    except ValueError:
        raise HTTPException(404, '市场任务或商品不存在') from None


@router.get('/tasks')
def tasks(repo=Depends(repository)):
    return {'items': MarketTrackingService(repo).tasks()}


@router.post('/tasks', status_code=201)
def create_task(body: TaskCreate, repo=Depends(repository)):
    return {'task': MarketTrackingService(repo).create_task(body.model_dump())}


@router.post('/tasks/pause-all')
def pause_all(repo=Depends(repository)):
    MarketTrackingService(repo).pause_all()
    return {'status': 'paused'}


@router.patch('/tasks/{task_id}')
def update_task(task_id: int, body: TaskUpdate, repo=Depends(repository)):
    not_found(lambda: MarketTrackingService(repo).update_task(task_id, body.model_dump(exclude_none=True)))
    return {'status': 'updated'}


@router.delete('/tasks/{task_id}')
def delete_task(task_id: int, repo=Depends(repository)):
    not_found(lambda: MarketTrackingService(repo).delete_task(task_id))
    return {'status': 'deleted', 'history_retained': True}


def assert_idle(repo):
    with repo.connection() as conn:
        if conn.execute("SELECT 1 FROM market_leases WHERE name='worker' AND expires_at>?", (stamp(),)).fetchone():
            raise HTTPException(409, '市场采集正在执行，请稍后重试')


@router.post('/tasks/{task_id}/run', status_code=202)
async def run_search(task_id: int, service=Depends(runner), repo=Depends(repository)):
    assert_idle(repo)
    with repo.connection(True) as conn:
        if not conn.execute('UPDATE market_tasks SET next_discovery_at=? WHERE id=? AND enabled=1 AND deleted_at IS NULL', (stamp(), task_id)).rowcount:
            raise HTTPException(409, '任务不存在或已暂停')
    service.dispatch(service.search(task_id))
    return {'status': 'queued', 'task_id': task_id}


@router.get('/items')
def items(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), selected=Depends(filters), repo=Depends(repository)):
    return MarketStatisticsService(repo).items(page, limit, **selected)


@router.get('/items/{item_id}')
def get_item(item_id: str, repo=Depends(repository)):
    return not_found(lambda: MarketStatisticsService(repo).history(item_id, limit=1))


@router.get('/items/{item_id}/history')
def history(item_id: str, page: int = Query(1, ge=1), limit: int = Query(100, ge=1, le=200), repo=Depends(repository)):
    return not_found(lambda: MarketStatisticsService(repo).history(item_id, page, limit))


@router.patch('/items/{item_id}/classification')
def correct(item_id: str, body: Correction, actor=Depends(require_user), repo=Depends(repository)):
    return {'item': not_found(lambda: ItemLifecycleService(repo).correct_classification(item_id, body.model_dump(), actor))}


@router.get('/trends')
def trends(start: date | None = None, end: date | None = None, selected=Depends(filters), repo=Depends(repository)):
    end = end or date.fromisoformat(stamp()[:10])
    start = start or end-timedelta(days=29)
    try:
        return MarketStatisticsService(repo).trends(start.isoformat(), end.isoformat(), **selected)
    except ValueError:
        raise HTTPException(422, '日期范围应在0到366天之间') from None


@router.get('/stats')
def stats(selected=Depends(filters), repo=Depends(repository)):
    return MarketStatisticsService(repo).summary(**selected)


@router.get('/revisits')
def revisits(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), repo=Depends(repository)):
    with repo.connection() as conn:
        total = conn.execute('SELECT count(*) FROM market_revisit_jobs').fetchone()[0]
        rows = conn.execute('''SELECT j.*,i.title,i.seller_id,i.listing_status FROM market_revisit_jobs j JOIN market_items i USING(item_id)
            ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,j.next_run_at LIMIT ? OFFSET ?''', (limit, (page-1)*limit)).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit}


@router.post('/revisits/run', status_code=202)
async def run_revisits(service=Depends(runner), repo=Depends(repository)):
    assert_idle(repo)
    service.dispatch(service.revisits())
    return {'status': 'queued', 'scope': 'due_only'}


@router.get('/runs')
def runs(page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100), repo=Depends(repository)):
    with repo.connection() as conn:
        total = conn.execute('SELECT count(*) FROM market_runs').fetchone()[0]
        rows = conn.execute('SELECT * FROM market_runs ORDER BY started_at DESC,rowid DESC LIMIT ? OFFSET ?', (limit, (page-1)*limit)).fetchall()
    return {'items': [dict(r) for r in rows], 'total': total, 'page': page, 'limit': limit}


@router.get('/export')
def export(selected=Depends(filters), repo=Depends(repository)):
    return StreamingResponse(MarketStatisticsService(repo).export(**selected), media_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition': 'attachment; filename="goofish-market.csv"'})
