"""Goofish adapter boundary. Offline fixtures cover the observed JSON contract only.

No captcha solving, risk bypass, profile ratings or private seller information.
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright, TimeoutError as BrowserTimeout
from src.config import DETAIL_API_URL_PATTERN, STATE_FILE
from src.parsers import _parse_search_results_json, _parse_user_items_data
from src.services.search_pagination import is_search_results_response, NEXT_PAGE_SELECTOR
from src.services.market_types import SellerListing, LoginExpired, AccessRestricted, PlatformAccessError


def validate_payload(data):
    ret = ' '.join(map(str, data.get('ret', []))).upper()
    if any(code in ret for code in ['TOKEN_EXPIRED', 'SESSION_EXPIRED', 'ILLEGAL_ACCESS', 'NEED_LOGIN', 'LOGIN_REQUIRED']):
        raise LoginExpired('login_expired')
    if any(code in ret for code in ['USER_VALIDATE', 'RGV587', 'ACCESS_DENIED', 'TRAFFIC_LIMIT']):
        raise AccessRestricted('access_restricted')
    if not ret or 'SUCCESS' not in ret:
        raise PlatformAccessError('platform_response_unrecognized')


async def parse_seller_listing(data):
    validate_payload(data)
    payload = data.get('data', {})
    cards = payload.get('cardList')
    if not isinstance(cards, list):
        raise PlatformAccessError('seller_list_contract_changed')
    parsed = await _parse_user_items_data(cards)
    return SellerListing(items=[{'item_id': str(p['商品ID'] or ''), 'price': p['商品价格'],
                                'raw_status': c.get('cardData', {}).get('itemStatus')}
                               for p, c in zip(parsed, cards)],
                         request_ok=True, page_complete=True, list_complete=payload.get('nextPage') is False)


class PlaywrightMarketAdapter:
    def __init__(self, task, repository, delay=5):
        self.task = task
        self.repo = repository
        self.delay = max(2, delay)
        self.browser = self.context = self.playwright = None

    async def __aenter__(self):
        from src.scraper import _build_context_overrides, _build_extra_headers
        configured = self.task.get('account_state_file')
        if configured:
            # Only managed account filenames, never arbitrary API-supplied paths.
            path = Path('state') / Path(configured).name
            if configured not in {path.name, str(path)} or path.is_symlink():
                raise LoginExpired('invalid_account_state')
        elif Path(STATE_FILE).is_file():
            path = Path(STATE_FILE)
        else:
            paths = sorted(p for p in Path('state').glob('*.json') if not p.is_symlink())
            path = paths[0] if paths else None
        if path is None or not path.is_file():
            raise LoginExpired('account_state_missing')
        try:
            state = json.loads(path.read_text())
        except (ValueError, OSError):
            raise LoginExpired('account_state_invalid') from None
        options = {'locale': 'zh-CN', 'timezone_id': 'Asia/Shanghai'}
        if any(k in state for k in ('env', 'headers', 'page', 'storage')):
            options.update(_build_context_overrides(state))
            options['extra_http_headers'] = _build_extra_headers(state.get('headers'))
            state = {'cookies': state.get('cookies', [])}
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
            self.context = await self.browser.new_context(storage_state=state, **options)
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *_):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def check_access(self, page):
        if 'passport.goofish.com' in page.url or 'mini_login' in page.url:
            raise LoginExpired('login_expired')
        if await page.locator('.baxia-dialog-mask:visible, .J_MIDDLEWARE_FRAME_WIDGET:visible').count():
            raise AccessRestricted('access_restricted')

    async def detail_seller(self, item_id):
        page = await self.context.new_page()
        try:
            async with page.expect_response(lambda r: DETAIL_API_URL_PATTERN in r.url, timeout=20000) as info:
                await page.goto('https://www.goofish.com/item?' + urlencode({'id': item_id}), wait_until='domcontentloaded', timeout=25000)
            await self.check_access(page)
            response = await info.value
            if not response.ok:
                raise PlatformAccessError('detail_http_failed')
            data = await response.json()
            validate_payload(data)
            return data.get('data', {}).get('sellerDO', {}).get('sellerId')
        except BrowserTimeout:
            await self.check_access(page)
            return None
        finally:
            await page.close()
            await asyncio.sleep(self.delay)

    async def search(self):
        page = await self.context.new_page()
        try:
            async with page.expect_response(is_search_results_response, timeout=25000) as info:
                await page.goto('https://www.goofish.com/search?' + urlencode({'q': self.task['keyword']}), wait_until='domcontentloaded', timeout=30000)
            await self.check_access(page)
            response = await info.value
            for page_index in range(self.task['max_pages']):
                if not response.ok:
                    raise PlatformAccessError('search_http_failed')
                data = await response.json()
                validate_payload(data)
                if not isinstance(data.get('data', {}).get('resultList'), list):
                    raise PlatformAccessError('search_contract_changed')
                if not data['data']['resultList']:
                    break
                products = await _parse_search_results_json(data, 'market_tracking')
                if not products:
                    raise PlatformAccessError('search_parse_failed')
                for product in products[:100]:
                    item_id = str(product.get('商品ID') or '')
                    with self.repo.connection() as conn:
                        known = conn.execute('SELECT seller_id FROM market_items WHERE item_id=?', (item_id,)).fetchone()
                    seller = known['seller_id'] if known else None
                    if not seller and item_id.isdigit():
                        try:
                            seller = await self.detail_seller(item_id)
                        except (LoginExpired, AccessRestricted):
                            # Preserve the already observed listing before stopping access.
                            yield {**product, 'seller_id': None, 'raw_status': 0}
                            raise
                        except Exception:
                            seller = None  # durable pending enrichment, never fabricate an ID
                    yield {**product, 'seller_id': seller, 'raw_status': 0}
                if page_index + 1 >= self.task['max_pages']:
                    break
                button = page.locator(NEXT_PAGE_SELECTOR).first
                if not await button.count():
                    break
                await asyncio.sleep(self.delay)
                async with page.expect_response(is_search_results_response, timeout=20000) as info:
                    await button.click(timeout=10000)
                await self.check_access(page)
                response = await info.value
        except BrowserTimeout:
            await self.check_access(page)
            raise PlatformAccessError('search_timeout') from None
        finally:
            await page.close()

    async def seller_items(self, seller_id, max_pages=5):
        page = await self.context.new_page()
        aggregate = SellerListing()
        try:
            async with page.expect_response(lambda r: 'mtop.idle.web.xyh.item.list' in r.url, timeout=20000) as info:
                await page.goto('https://www.goofish.com/personal?' + urlencode({'userId': seller_id}), wait_until='domcontentloaded', timeout=25000)
            await self.check_access(page)
            response = await info.value
            for page_index in range(max_pages):
                if not response.ok:
                    raise PlatformAccessError('seller_http_failed')
                current = await parse_seller_listing(await response.json())
                aggregate.items.extend(current.items)
                aggregate.request_ok = aggregate.page_complete = True
                aggregate.list_complete = current.list_complete
                if current.list_complete or page_index + 1 >= max_pages:
                    break
                await asyncio.sleep(self.delay)
                async with page.expect_response(lambda r: 'mtop.idle.web.xyh.item.list' in r.url, timeout=8000) as info:
                    await page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
                await self.check_access(page)
                response = await info.value
            return aggregate
        except BrowserTimeout:
            await self.check_access(page)
            aggregate.error = 'seller_list_incomplete' if aggregate.request_ok else 'seller_timeout'
            return aggregate
        finally:
            await page.close()
            await asyncio.sleep(self.delay)

    async def item_listing(self, item_id):
        """One registered item page plus a cached, single seller-list page; no search."""
        page = await self.context.new_page()
        try:
            async with page.expect_response(lambda r: DETAIL_API_URL_PATTERN in r.url, timeout=20000) as info:
                await page.goto('https://www.goofish.com/item?' + urlencode({'id': item_id}),
                                wait_until='domcontentloaded', timeout=25000)
            await self.check_access(page)
            response = await info.value
            if not response.ok:
                raise PlatformAccessError('detail_http_failed')
            observation = parse_item_listing(await response.json(), item_id)
            seller = str(observation.get('seller_id') or '')
            if not seller.isdigit() or len(seller)>40:
                return {**observation, 'raw_status': None, 'evidence': 'seller_id_pending'}
            # Existing seller-list contract is known; detail itemStatus codes may
            # differ, so never interpret those numeric codes as a sale by themselves.
            cache = getattr(self, '_result_seller_cache', None)
            if cache is None:
                self._result_seller_cache = cache = {}
            if seller not in cache:
                cache[seller] = await self.seller_items(seller, max_pages=1)
            listing = cache[seller]
            found = next((i for i in listing.items if str(i['item_id'])==str(item_id)), None)
            raw = found.get('raw_status') if found and listing.request_ok and listing.page_complete else None
            return {**observation, 'raw_status': raw,
                    'source': 'seller_item_list+item_detail',
                    'evidence': 'explicit_seller_status_and_detail_price' if raw is not None else 'seller_item_unconfirmed'}
        except BrowserTimeout:
            await self.check_access(page)
            return {'request_ok': False, 'page_complete': False, 'evidence': 'detail_timeout'}
        finally:
            await page.close()
            await asyncio.sleep(self.delay)


def parse_item_listing(data, expected_id):
    """Read price/identity only; detail numeric statuses are never sale evidence.

    Sale status comes from the existing seller-list adapter's explicit 0/1
    contract. Live response fields still require authorized manual verification.
    """
    validate_payload(data)
    item = data.get('data', {}).get('itemDO')
    seller = data.get('data', {}).get('sellerDO') or {}
    if not isinstance(item, dict) or str(item.get('itemId') or '') != str(expected_id):
        raise PlatformAccessError('detail_item_contract_changed')
    return {'request_ok': True, 'page_complete': True, 'raw_status': None,
            'price': item.get('soldPrice') if item.get('soldPrice') is not None else item.get('price'),
            'seller_id': seller.get('sellerId'), 'seller_nickname': seller.get('nick'),
            'source': 'item_detail', 'evidence': 'explicit_detail_item_status'}
