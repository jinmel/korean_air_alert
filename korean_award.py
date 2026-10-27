"""Read Korean Air's website award calendar; not a documented public API."""
import argparse
import calendar
import json
import re
from datetime import date, datetime, timedelta, timezone
import http.cookiejar
import time
import urllib.error
import urllib.request

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


class AwardClient:
    """Normal HTTP session; cookies remain in memory and proxies are inherited."""

    def __init__(self, base_url='https://www.koreanair.com'):
        self.base_url = base_url.rstrip('/')
        self.page_url = self.base_url + '/booking/book-and-manage/award-seat-availability'
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.headers = {
            'User-Agent': 'KoreanAwardChecker/1.0 (Python urllib)',
            'Accept': 'application/json',
            'Accept-Language': 'ko-KR',
            'Content-Type': 'application/json',
            'Origin': self.base_url,
            'Referer': self.page_url,
            'channel': 'pc',
        }

    def _request(self, url, body=None):
        request = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode('utf-8'),
            headers=self.headers)
        try:
            with self.opener.open(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f'HTTP {exc.code}: 조회를 거부했거나 처리하지 못했습니다. '
                '차단 또는 인증 요구를 확인하세요.') from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f'네트워크 요청 실패 ({type(exc).__name__})') from None
        if b'Access Denied' in raw:
            raise RuntimeError('서버가 접근을 거부했습니다.')
        return raw

    def initialize(self):
        self._request(self.page_url)

    def fetch_month(self, origin, destination, month):
        raw = self._request(self.base_url + API_PATH, {
            'arrivalAirport': destination,
            'departureAirport': origin,
            'departureDate': month.strftime('%Y%m01'),
        })
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ValueError('API가 JSON을 반환하지 않았습니다.') from None
        if not isinstance(payload, dict):
            raise ValueError('API 응답이 JSON 객체가 아닙니다.')
        if (payload.get('departureAirport') != origin
                or payload.get('arrivalAirport') != destination):
            raise ValueError('응답 노선이 요청 노선과 다릅니다.')
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, type=str.upper, help='출발 공항 코드, 예: ICN')
    parser.add_argument('--destination', required=True, type=str.upper, help='도착 공항 코드, 예: JFK')
    parser.add_argument('--month', help='시작 월 YYYY-MM (기본: 한국 시간 이번 달)')
    parser.add_argument('--until', help='종료 월 YYYY-MM (기본: 조회 한도까지)')
    parser.add_argument('--days', type=int, default=360, help='오늘부터 최대 일수 (기본: 360)')
    args = parser.parse_args()
    if not all(re.fullmatch(r'[A-Z]{3}', x) for x in (args.origin, args.destination)):
        parser.error('공항 코드는 영문 세 자리여야 합니다.')
    if args.origin == args.destination:
        parser.error('출발 공항과 도착 공항이 같습니다.')
    today = datetime.now(KST).date()
    try:
        if args.days < 1:
            raise ValueError('일수는 1 이상이어야 합니다.')
        first = month_start(args.month or today.strftime('%Y-%m'))
        limit = today + timedelta(days=args.days)
        if args.until:
            until = month_start(args.until)
            limit = min(limit, until.replace(day=calendar.monthrange(until.year, until.month)[1]))
        if first < today.replace(day=1) or first > limit:
            raise ValueError('시작 월이 조회 기간 밖입니다.')
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))

    start = max(today, first)
    print(f'{args.origin} → {args.destination}: {start} ~ {limit} (한국 시간)', flush=True)
    client = AwardClient()
    client.initialize()
    current, total, completed = first, 0, 0
    while current <= limit:
        try:
            payload = client.fetch_month(args.origin, args.destination, current)
            end = min(limit, current.replace(day=calendar.monthrange(current.year, current.month)[1]))
            rows = extract(payload, max(start, current), end)
        except Exception as exc:
            raise RuntimeError(f'{current:%Y-%m} 조회 미완료: {exc}') from exc
        print(f'\n[{current:%Y-%m}]', flush=True)
        for row in rows:
            print('\t'.join(row), flush=True)
        if not rows:
            print('대상 기간의 O/A 보너스 좌석 표시 없음', flush=True)
        total += len(rows)
        completed += 1
        current = next_month(current)
        if current <= limit:
            time.sleep(2)
    print(f'완료: {completed}개월, {total}건 (좌석 수가 아닌 항공편·등급별 표시 건수)')
    print('공개 현황이며 최종 예약 가능 여부는 예매 화면에서 확인하세요.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'조회 실패: {str(exc).split(chr(10))[0]}\n미조회 기간은 좌석 없음으로 판단하지 마세요.')
        raise SystemExit(1)
