"""Read Korean Air's website award calendar; not a documented public API."""
import argparse
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
import http.cookiejar
import time
import urllib.error
import urllib.request

URL = 'https://www.koreanair.com/booking/book-and-manage/award-seat-availability'
API_PATH = '/api/hmp/bonusSeatView/bonusSeatView'
LOGIN_PATH = '/api/li/auth/signIn'
LIVE_PATH = '/api/ap/booking/avail/awardAvailability'
FARE_FAMILIES = {'프레스티지석 보너스': 'KEBONUSPR', '일등석 보너스': 'KEBONUSFC'}
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


def login_settings(environ=None):
    environ = os.environ if environ is None else environ
    username = environ.get('KOREANAIR_USERNAME', '')
    password = environ.get('KOREANAIR_PASSWORD', '')
    login_type = environ.get('KOREANAIR_LOGIN_TYPE', 'id')
    if not username or not password:
        raise ValueError('--verify-live에는 KOREANAIR_USERNAME, KOREANAIR_PASSWORD 환경 변수가 필요합니다.')
    if login_type not in ('id', 'skypass'):
        raise ValueError('KOREANAIR_LOGIN_TYPE은 id 또는 skypass여야 합니다.')
    return username, password, login_type


def flight_key(value):
    match = re.fullmatch(r'([A-Z]{2})(\d+)', str(value).replace(' ', '').upper())
    if not match:
        raise ValueError('항공편 번호 형식을 확인할 수 없습니다.')
    return match[1], int(match[2])


def live_result(payload, origin, destination, row):
    """Conservative parser of the website's award-selection response, one adult."""
    if not isinstance(payload, dict) or payload.get('errors') or payload.get('code'):
        raise ValueError('예매 검색 오류 또는 예상하지 못한 응답입니다.')
    bounds = payload.get('upsellBoundAvailList')
    if not isinstance(bounds, list) or len(bounds) != 1 or not isinstance(bounds[0], dict):
        raise ValueError('편도 예매 검색 응답 형식을 확인할 수 없습니다.')
    flights = bounds[0].get('availFlightList')
    if not isinstance(flights, list):
        raise ValueError('예매 검색 항공편 목록이 없습니다.')
    family_code = FARE_FAMILIES[row[3]]
    target_flight = flight_key(row[1])
    matched = False
    found_fare = False
    evidence = []
    for flight in flights:
        if not isinstance(flight, dict) or not isinstance(flight.get('flightInfoList'), list):
            raise ValueError('항공편 상세 형식이 변경되었습니다.')
        segments = flight['flightInfoList']
        if len(segments) != 1:
            continue  # Public-calendar candidates are individual direct flights.
        segment = segments[0]
        if not isinstance(segment, dict):
            raise ValueError('항공편 상세가 JSON 객체가 아닙니다.')
        key = flight_key(str(segment.get('carrierCode', '')) + str(segment.get('flightNumber', '')))
        if key != target_flight:
            continue
        if segment.get('departureAirport') != origin or segment.get('arrivalAirport') != destination:
            raise ValueError('실시간 응답의 항공편 노선이 요청과 다릅니다.')
        departure = str(segment.get('departureDateTime', ''))
        day = parse_date(departure[:10]) if '-' in departure[:10] else parse_date(departure[:8])
        if day is None or day.isoformat() != row[0]:
            raise ValueError('실시간 응답의 항공편 날짜가 요청과 다릅니다.')
        matched = True
        fares = flight.get('commercialFareFamilyList')
        if not isinstance(fares, list):
            raise ValueError('예매 검색 운임 목록 형식이 변경되었습니다.')
        for fare in fares:
            if not isinstance(fare, dict):
                raise ValueError('예매 검색 운임이 JSON 객체가 아닙니다.')
            if fare.get('fareFamily') != family_code:
                continue
            found_fare = True
            for container, flag in ((fare, 'soldout'), (flight, 'soldOut')):
                if flag in container and not isinstance(container[flag], bool):
                    raise ValueError('예매 검색 매진 상태 형식이 변경되었습니다.')
            count = fare.get('seatCount')
            if count == '':
                evidence.append(('not_selectable', None))
                continue
            if isinstance(count, bool) or not re.fullmatch(r'\d+', str(count)):
                raise ValueError('예매 검색 좌석 수를 해석하지 못했습니다.')
            count = int(count)
            if count == 0 or fare.get('soldout') is True or flight.get('soldOut') is True:
                evidence.append(('not_selectable', count))
                continue
            expected_class = 'O' if family_code == 'KEBONUSPR' else 'A'
            if (fare.get('bookingClass') != expected_class
                    or not isinstance(fare.get('recommendId'), str)
                    or not fare['recommendId'].strip()):
                raise ValueError('보너스 예약등급 또는 선택 가능한 운임 식별자를 확인하지 못했습니다.')
            evidence.append(('selectable', count))
    if any(status == 'selectable' for status, _ in evidence):
        return '예매 검색에서 선택 가능', max(n for status, n in evidence if status == 'selectable')
    if evidence:
        return '예매 검색에서 선택 불가', None
    # An empty list may mean no flights, authentication failure, or a changed response.
    if not matched:
        return '미확인: 해당 항공편을 검색 응답에서 찾지 못함', None
    if not found_fare:
        return '미확인: 해당 보너스 운임이 검색 응답에 없음', None
    return '미확인: 예매 검색 응답 해석 불가', None


class AwardClient:
    """Normal HTTP session; cookies remain in memory and proxies are inherited."""

    def __init__(self, base_url='https://www.koreanair.com'):
        self.base_url = base_url.rstrip('/')
        self.page_url = self.base_url + '/booking/book-and-manage/award-seat-availability'
        self.cookies = http.cookiejar.CookieJar()
        self.traveler = None
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies))
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
        headers = dict(self.headers)
        if body is not None:
            for cookie in self.cookies:
                if cookie.name == 'XSRF-TOKEN' and not cookie.is_expired():
                    from urllib.parse import unquote
                    headers['X-XSRF-TOKEN'] = unquote(cookie.value)
        request = urllib.request.Request(
            url, data=None if body is None else json.dumps(body).encode('utf-8'),
            headers=headers)
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

    def login_from_env(self, environ=None):
        self.traveler = None
        username, password, login_type = login_settings(environ)
        body = {'skypassNumber' if login_type == 'skypass' else 'userId': username,
                'password': password}
        try:
            raw = self._request(self.base_url + LOGIN_PATH, body)
            payload = json.loads(raw)
        except Exception:
            # Login responses can echo private fields. Never propagate their text.
            raise RuntimeError('로그인 실패: 계정 정보, 추가 인증 또는 접근 제한을 확인하세요. 자동 재시도하지 않습니다.') from None
        finally:
            body.clear()
            del password, username
        info = payload.get('userInfo') if isinstance(payload, dict) else None
        if (not isinstance(info, dict) or not info
                or payload.get('signinStatus') is False or payload.get('code') or payload.get('errors')):
            raise RuntimeError('로그인 성공을 확인하지 못했습니다. 계정 정보 또는 추가 인증을 확인하세요.')
        required = ('skypassNumber', 'englishLastName', 'englishFirstName')
        if not all(isinstance(info.get(key), str) and info[key].strip() for key in required):
            raise RuntimeError('로그인 응답에서 본인 탑승객 정보를 확인하지 못했습니다.')
        self.traveler = {'travellerType': 'ADT', 'fqtvNumber': info['skypassNumber'],
                         'lastName': info['englishLastName'], 'firstName': info['englishFirstName']}

    def fetch_live(self, origin, destination, day):
        if self.traveler is None:
            raise RuntimeError('실시간 예매 조회 전에 로그인이 필요합니다.')
        body = {
            'commercialFareFamilies': ['KEBONUSNEW'], 'currency': 'KRW', 'sta': False,
            'segmentList': [{'departureDate': day.strftime('%Y%m%d'),
                             'departureAirport': origin, 'arrivalAirport': destination}],
            'travelers': [self.traveler], 'corporateCode': 'string',
        }
        try:
            raw = self._request(self.base_url + LIVE_PATH, body)
            payload = json.loads(raw)
        except Exception:
            raise RuntimeError('예매 검색 실패: 세션 만료, 추가 인증, 접근 제한 또는 응답 형식을 확인하세요.') from None
        return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, type=str.upper, help='출발 공항 코드, 예: ICN')
    parser.add_argument('--destination', required=True, type=str.upper, help='도착 공항 코드, 예: JFK')
    parser.add_argument('--month', help='시작 월 YYYY-MM (기본: 한국 시간 이번 달)')
    parser.add_argument('--until', help='종료 월 YYYY-MM (기본: 조회 한도까지)')
    parser.add_argument('--days', type=int, default=360, help='오늘부터 최대 일수 (기본: 360)')
    parser.add_argument('--verify-live', action='store_true', help='환경 변수로 로그인 후 후보 날짜를 실제 예매 검색으로 재확인(본인 성인 1명)')
    args = parser.parse_args()
    if not all(re.fullmatch(r'[A-Z]{3}', x) for x in (args.origin, args.destination)):
        parser.error('공항 코드는 영문 세 자리여야 합니다.')
    if args.origin == args.destination:
        parser.error('출발 공항과 도착 공항이 같습니다.')
    if args.verify_live:
        try:
            login_settings()
        except ValueError as exc:
            parser.error(str(exc))
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
    candidates = []
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
        candidates.extend(rows)
        total += len(rows)
        completed += 1
        current = next_month(current)
        if current <= limit:
            time.sleep(2)
    print(f'완료: {completed}개월, {total}건 (좌석 수가 아닌 항공편·등급별 표시 건수)')
    if not args.verify_live:
        print('공개 현황이며 최종 예약 가능 여부는 예매 화면에서 확인하세요.')
        return
    if not candidates:
        print('공개 현황의 후보가 없어 실시간 검색을 수행하지 않았습니다. 전체 날짜에 대한 실시간 검색 결과가 아닙니다.')
        return
    print('\n실제 예매 검색 재확인: 로그인한 본인 성인 1명', flush=True)
    client.login_from_env()
    checked_dates = {}
    incomplete = False
    for row in candidates:
        if row[0] not in checked_dates:
            if checked_dates:
                time.sleep(2)
            try:
                checked_dates[row[0]] = client.fetch_live(
                    args.origin, args.destination, date.fromisoformat(row[0]))
            except RuntimeError:
                print(f'{row[0]}: 조회 실패. 이후 실시간 검증을 중단합니다.', flush=True)
                raise
        try:
            status, seats = live_result(checked_dates[row[0]], args.origin, args.destination, row)
        except (ValueError, TypeError, KeyError, IndexError):
            status, seats = '미확인: 예매 검색 응답 해석 실패', None
        incomplete |= status.startswith('미확인')
        stamp = datetime.now(KST).isoformat(timespec='seconds')
        count = f' / 응답 좌석 수 {seats}' if seats is not None else ''
        print('\t'.join(row) + f'\t{status}{count} / 확인 {stamp}', flush=True)
    print('예매 검색에서 선택 가능한 상태를 확인한 것이며, 좌석 확보·예약 생성·결제는 하지 않습니다.')
    if incomplete:
        raise RuntimeError('일부 후보의 실시간 상태를 확인하지 못했습니다. 미확인을 좌석 없음으로 판단하지 마세요.')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'조회 실패: {str(exc).split(chr(10))[0]}\n미조회 기간은 좌석 없음으로 판단하지 마세요.')
        raise SystemExit(1)
