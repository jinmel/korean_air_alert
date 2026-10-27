"""Read Korean Air's website award calendar; not a documented public API."""
import argparse
import asyncio
import calendar
import copy
import json
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

URL = 'https://www.koreanair.com/booking/book-and-manage/award-seat-availability'
API_PATH = '/api/hmp/bonusSeatView/bonusSeatView'
KST = timezone(timedelta(hours=9))
CABINS = {'O': '프레스티지석 보너스', 'A': '일등석 보너스'}


def month_start(text):
    if not re.fullmatch(r'\d{4}-\d{2}', text):
        raise ValueError('월은 YYYY-MM 형식이어야 합니다.')
    return date.fromisoformat(text + '-01')


def next_month(day):
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1)


def parse_date(value):
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r'(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})(?:[T ].*)?', value)
    if match:
        try:
            return date(*map(int, match.groups()))
        except ValueError:
            pass
    return None


def change_month(node, source, target):
    """Only alter date-named fields, and fail on ambiguous date ranges."""
    changes = 0

    def walk(value, key=''):
        nonlocal changes
        if isinstance(value, dict):
            return {k: walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, key) for v in value]
        if not isinstance(value, str) or not re.search('date|month', key, re.I):
            return value
        for fmt in ('%Y-%m-%d', '%Y%m%d', '%Y-%m', '%Y%m'):
            try:
                parsed = datetime.strptime(value, fmt).date()
            except ValueError:
                continue
            if parsed.strftime(fmt) != value:
                continue
            if (parsed.year, parsed.month) != (source.year, source.month):
                raise ValueError(f'{key}: 다른 월의 날짜가 있습니다. 편도로 다시 조회하세요.')
            last = calendar.monthrange(target.year, target.month)[1]
            # Keep month-end boundary requests at the end of the new month.
            source_last = calendar.monthrange(source.year, source.month)[1]
            day = last if parsed.day == source_last else min(parsed.day, last)
            changes += 1
            return target.replace(day=day).strftime(fmt)
        return value

    result = walk(copy.deepcopy(node))
    if not changes:
        raise ValueError('날짜/월 요청 필드를 인식하지 못했습니다. 요청 구조 확인이 필요합니다.')
    return result


def extract(payload, start, end):
    rows, dates = set(), set()
    recognized = 0

    def walk(node, inherited_date=None, inherited_flight=''):
        nonlocal recognized
        if isinstance(node, list):
            for item in node:
                walk(item, inherited_date, inherited_flight)
        elif isinstance(node, dict):
            candidates = {
                parsed for key, value in node.items()
                if 'date' in key.lower() and (parsed := parse_date(value))
            }
            if len(candidates) > 1:
                raise ValueError('같은 객체에 날짜가 여러 개 있어 해석할 수 없습니다.')
            day = next(iter(candidates), inherited_date)
            flight = str(node.get('flightNumber') or inherited_flight)
            if 'bookingClass' in node and 'availableSeat' in node:
                recognized += 1
                available = node['availableSeat']
                if not isinstance(available, bool) or day is None:
                    raise ValueError('좌석 날짜 또는 availableSeat 형식이 예상과 다릅니다.')
                dates.add(day)
                code = str(node['bookingClass']).strip().upper()
                if start <= day <= end and code in CABINS and available:
                    rows.add((day.isoformat(), flight,
                              str(node.get('departureTime', '')), CABINS[code]))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, day, flight)

    walk(payload)
    if not recognized:
        raise ValueError('항공편별 좌석 상태가 없어 조회 성공 여부를 확인할 수 없습니다.')
    if not any((d.year, d.month) == (start.year, start.month) for d in dates):
        raise ValueError('응답 날짜가 요청한 월과 다릅니다. 월 변경을 확인할 수 없습니다.')
    return sorted(rows)


async def select_route(page, origin, destination, month):
    cookie = page.get_by_role('button', name='필수 쿠키만 허용', exact=True)
    try:
        await cookie.wait_for(state='visible', timeout=5000)
        await cookie.click()
    except Exception:
        if await cookie.is_visible():
            raise
    await page.locator('label[for=bonusTripType_OW]').click()
    for button, label, airport in (
        ('From 출발지', '출발지 검색', origin),
        ('To 도착지', '도착지 검색', destination),
    ):
        await page.get_by_role('button', name=button, exact=True).click()
        await page.get_by_role('combobox', name=label, exact=True).press_sequentially(airport, delay=100)
        await page.get_by_role('option', name=re.compile('^' + re.escape(airport) + ' ')).click()
    await page.get_by_role('button', name='탑승월 가는 달', exact=True).click()
    group = page.get_by_role('group', name=str(month.year), exact=True)
    await group.get_by_role('button', name=f'{month.month}월', exact=True).click()
    await page.get_by_role('button', name='선택', exact=True).click()
    await page.get_by_role('button', name='조회', exact=True).click()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--month', help='시작 월 YYYY-MM (기본: 한국 시간 이번 달)')
    parser.add_argument('--until', help='종료 월 YYYY-MM (기본: 조회 한도까지)')
    parser.add_argument('--days', type=int, default=360, help='오늘부터 최대 일수 (기본: 360)')
    parser.add_argument('--origin', type=str.upper, help='출발 공항 코드, 예: ICN')
    parser.add_argument('--destination', type=str.upper, help='도착 공항 코드, 예: JFK')
    parser.add_argument('--headless', action='store_true', help='브라우저 숨기기; 공항 미지정 시 접근 진단만 수행')
    parser.add_argument('--capture-timeout', type=int, default=600)
    args = parser.parse_args()
    if bool(args.origin) != bool(args.destination):
        parser.error('--origin과 --destination은 함께 지정하세요.')
    if args.origin and not all(re.fullmatch(r'[A-Z]{3}', x) for x in (args.origin, args.destination)):
        parser.error('공항 코드는 영문 세 자리여야 합니다.')
    today = datetime.now(KST).date()
    try:
        if args.days < 1 or args.capture_timeout < 1:
            raise ValueError('일수와 대기 시간은 1 이상이어야 합니다.')
        first = month_start(args.month or today.strftime('%Y-%m'))
        limit = today + timedelta(days=args.days)
        if args.until:
            until = month_start(args.until)
            limit = min(limit, until.replace(day=calendar.monthrange(until.year, until.month)[1]))
        if first < today.replace(day=1) or first > limit:
            raise ValueError('시작 월이 조회 기간 밖입니다.')
    except ValueError as exc:
        parser.error(str(exc))

    start = max(today, first)
    print(f'조회 기간: {start} ~ {limit} (한국 시간)', flush=True)
    if not args.origin:
        print(f'브라우저에서 편도·노선·{first:%Y-%m}을 선택하고 조회하세요.', flush=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless)
        context = await browser.new_context(locale='ko-KR')
        page = await context.new_page()
        captured = asyncio.get_running_loop().create_future()

        def capture(response):
            url = urlsplit(response.url)
            if (url.hostname == 'www.koreanair.com'
                    and url.path.rstrip('/') == API_PATH
                    and response.request.method == 'POST' and not captured.done()):
                captured.set_result(response)

        context.on('response', capture)
        try:
            navigation = await page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            text = await page.locator('body').inner_text()
            if (navigation and navigation.status >= 400) or 'Access Denied' in text:
                raise RuntimeError(f'대한항공 사이트에서 접근을 거부했습니다 (HTTP {navigation.status if navigation else "unknown"})')
            if args.origin:
                print(f'{args.origin} → {args.destination}: 시작 월 자동 선택', flush=True)
                await select_route(page, args.origin, args.destination, first)
            elif args.headless:
                print('페이지 접근 성공. 수동 조회 및 API 검증은 수행하지 않았습니다.')
                return
            first_response = await asyncio.wait_for(captured, args.capture_timeout)
            if first_response.status != 200:
                raise RuntimeError(f'최초 조회 HTTP {first_response.status}')
            template = json.loads(first_response.request.post_data or '')
            change_month(template, first, first)
            headers = await first_response.request.all_headers()
            headers = {k: v for k, v in headers.items()
                       if k.lower() in {'accept', 'content-type', 'channel', 'ksessionid'}}
            current, total = first, 0
            while current <= limit:
                response = None
                try:
                    if current == first:
                        payload = await first_response.json()
                    else:
                        response = await page.evaluate(
                            """async ({url, headers, body}) => {
                                const r = await fetch(url, {
                                    method: 'POST', credentials: 'same-origin',
                                    headers, body: JSON.stringify(body),
                                    signal: AbortSignal.timeout(60000)
                                });
                                return {status: r.status, text: await r.text()};
                            }""",
                            {'url': first_response.url, 'headers': headers,
                             'body': change_month(template, first, current)})
                        if response['status'] != 200:
                            raise RuntimeError(f"HTTP {response['status']}")
                        payload = json.loads(response['text'])
                    end = min(limit, current.replace(day=calendar.monthrange(current.year, current.month)[1]))
                    rows = extract(payload, max(start, current), end)
                    print(f'\n[{current:%Y-%m}]', flush=True)
                    for row in rows:
                        print('\t'.join(row), flush=True)
                    if not rows:
                        print('대상 기간의 O/A 보너스 좌석 표시 없음', flush=True)
                    total += len(rows)
                except Exception as exc:
                    raise RuntimeError(f'{current:%Y-%m} 조회 미완료: {exc}') from exc
                current = next_month(current)
                if current <= limit:
                    await asyncio.sleep(2)
            print(f'완료: {total}건 (좌석 수가 아닌 항공편·등급별 표시 건수)')
            print('공개 현황이며 최종 예약 가능 여부는 예매 화면에서 확인하세요.')
        finally:
            await browser.close()


if __name__ == '__main__':
    from playwright.async_api import async_playwright
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'조회 실패: {str(exc).split(chr(10))[0]}\n미조회 기간은 좌석 없음으로 판단하지 마세요.')
        raise SystemExit(1)
