"""Read Korean Air's website award calendar; not a documented public API."""
import argparse
import calendar
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
import http.cookiejar
from pathlib import Path
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
import uuid

URL = 'https://www.koreanair.com/booking/book-and-manage/award-seat-availability'
API_PATH = '/api/hmp/bonusSeatView/bonusSeatView'
LOGIN_PATH = '/api/li/auth/signIn'
REFRESH_PATH = '/api/li/auth/refresh'
USER_INFO_PATH = '/api/li/auth/loginUserInfo'
LIVE_PATH = '/api/ap/booking/avail/awardAvailability'
FARE_FAMILIES = {'프레스티지석 보너스': 'KEBONUSPR', '일등석 보너스': 'KEBONUSFC'}
KST = timezone(timedelta(hours=9))
CABINS = {'O': '프레스티지석 보너스', 'A': '일등석 보너스'}
UPGRADE_CABINS = {'Z': '마일리지 승급 좌석'}


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


def extract(payload, start, end, cabins=None):
    cabins = CABINS if cabins is None else cabins
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
                if start <= day <= end and code in cabins and available:
                    rows.add((day.isoformat(), flight,
                              str(node.get('departureTime', '')), cabins[code]))
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


def query_window(today, month=None, until=None, days=360):
    if days < 1:
        raise ValueError('일수는 1 이상이어야 합니다.')
    try:
        first = month_start(month or today.strftime('%Y-%m'))
        limit = today + timedelta(days=days)
        if until:
            end_month = month_start(until)
            limit = min(limit, end_month.replace(
                day=calendar.monthrange(end_month.year, end_month.month)[1]))
    except OverflowError as exc:
        raise ValueError(str(exc)) from None
    if first < today.replace(day=1) or first > limit:
        raise ValueError('시작 월이 조회 기간 밖입니다.')
    return first, max(today, first), limit


def date_window(today, start=None, end=None, days=360):
    if days < 1:
        raise ValueError('일수는 1 이상이어야 합니다.')
    limit = today + timedelta(days=days)

    def parse_day(value, label):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError(f'{label}은 YYYY-MM-DD 형식이어야 합니다.')
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError(f'{label}은 유효한 날짜여야 합니다.') from None

    start_day = parse_day(start, '시작일') or today
    end_day = parse_day(end, '종료일') or limit
    if start_day < today:
        raise ValueError('시작일이 오늘(한국 시간) 이전입니다.')
    if start_day > limit:
        raise ValueError('시작일이 조회 한도를 초과합니다.')
    if end_day < start_day:
        raise ValueError('종료일이 시작일보다 이전입니다.')
    end_day = min(end_day, limit)
    return start_day.replace(day=1), start_day, end_day


def parse_routes(value):
    routes = []
    seen = set()
    for item in value.split(','):
        route = item.strip().upper()
        if not re.fullmatch(r'[A-Z]{3}-[A-Z]{3}', route):
            raise ValueError('--routes는 공항 코드 형식 ORIG-DEST의 쉼표 목록이어야 합니다.')
        origin, destination = route.split('-')
        if origin == destination:
            raise ValueError('노선의 출발 공항과 도착 공항은 달라야 합니다.')
        if route not in seen:
            routes.append((origin, destination))
            seen.add(route)
    if not routes:
        raise ValueError('--routes를 하나 이상 지정해야 합니다.')
    return routes


def find_candidates(client, origin, destination, first, start, limit, out=None, cabins=None):
    current = first
    candidates = []
    completed = 0
    while current <= limit:
        try:
            payload = client.fetch_month(origin, destination, current)
            end = min(limit, current.replace(
                day=calendar.monthrange(current.year, current.month)[1]))
            rows = extract(payload, max(start, current), end, cabins)
        except Exception as exc:
            raise RuntimeError(f'{current:%Y-%m} 조회 미완료: {exc}') from exc
        if callable(out):
            out(f'\n[{current:%Y-%m}]')
            for row in rows:
                out('\t'.join(row))
            if not rows:
                out('대상 기간의 선택한 좌석 종류 후보 표시 없음')
        candidates.extend(rows)
        completed += 1
        current = next_month(current)
        if current <= limit:
            time.sleep(2)
    return candidates, completed


def verify_candidates(client, origin, destination, candidates, out=None):
    checked_dates = {}
    results = []
    for row in candidates:
        if row[0] not in checked_dates:
            if checked_dates:
                time.sleep(2)
            day = date.fromisoformat(row[0])
            try:
                checked_dates[row[0]] = client.fetch_live(origin, destination, day)
            except RuntimeError:
                if callable(out):
                    out(f'{row[0]}: 조회 실패. 이후 실시간 검증을 중단합니다.')
                raise
        try:
            status, seats = live_result(
                checked_dates[row[0]], origin, destination, row)
        except (ValueError, TypeError, KeyError, IndexError):
            status, seats = '미확인: 예매 검색 응답 해석 실패', None
        results.append((row, status, seats))
        if callable(out):
            stamp = datetime.now(KST).isoformat(timespec='seconds')
            count = f' / 응답 좌석 수 {seats}' if seats is not None else ''
            out('\t'.join(row) + f'\t{status}{count} / 확인 {stamp}')
    return results


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
            'ksessionId': uuid.uuid4().hex + datetime.now(KST).strftime('%Y%m%d') + 'W',
        }

    def _request(self, url, body=None):
        headers = dict(self.headers)
        headers['timestamp'] = str(time.time_ns() // 1_000_000)
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
            status = exc.code
            exc.close()
            raise RuntimeError(
                f'HTTP {status}: 조회를 거부했거나 처리하지 못했습니다. '
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
        except Exception as exc:
            # Preserve only our HTTP status, never a response body or private fields.
            status = re.match(r'^HTTP (\d{3}):', str(exc))
            reason = f' (HTTP {status[1]})' if status else ''
            raise RuntimeError(f'로그인 실패{reason}: 계정 정보, 추가 인증 또는 접근 제한을 확인하세요. 자동 재시도하지 않습니다.') from None
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

    def login_from_session(self, token):
        self.traveler = None
        parsed_url = urlsplit(self.base_url)
        host = parsed_url.hostname or ''
        is_korean_air = host == 'koreanair.com' or host.endswith('.koreanair.com')
        domain = '.koreanair.com' if is_korean_air else host
        try:
            if not isinstance(token, str) or not token.strip():
                raise ValueError('세션 쿠키가 비어 있습니다.')
            self.cookies.set_cookie(http.cookiejar.Cookie(
                version=0, name='T', value=token, port=None, port_specified=False,
                domain=domain, domain_specified=is_korean_air,
                domain_initial_dot=domain.startswith('.'), path='/', path_specified=True,
                secure=parsed_url.scheme == 'https', expires=None, discard=True,
                comment=None, comment_url=None, rest={'HttpOnly': None}, rfc2109=False))
            self._request(self.base_url + REFRESH_PATH)
            target_domain = 'koreanair.com' if is_korean_air else host.lower()
            rotated = token
            for cookie in self.cookies:
                cookie_domain = cookie.domain.lstrip('.').lower()
                if (cookie.name == 'T'
                        and (cookie_domain.endswith('koreanair.com')
                             if is_korean_air else cookie_domain == target_domain)):
                    rotated = cookie.value
            raw = self._request(self.base_url + USER_INFO_PATH)
            payload = json.loads(raw)
            info = payload.get('userInfo', payload) if isinstance(payload, dict) else None
            required = ('skypassNumber', 'englishLastName', 'englishFirstName')
            if not isinstance(info, dict) or not all(
                    isinstance(info.get(key), str) and info[key].strip() for key in required):
                raise ValueError('로그인 회원 정보를 확인할 수 없습니다.')
            self.traveler = {
                'travellerType': 'ADT', 'fqtvNumber': info['skypassNumber'],
                'lastName': info['englishLastName'], 'firstName': info['englishFirstName'],
            }
            return rotated
        except Exception as exc:
            self.traveler = None
            status = re.match(r'^HTTP (\d{3}):', str(exc))
            reason = f' (HTTP {status[1]})' if status else ''
            raise RuntimeError(
                '세션 만료 또는 로그인 상태가 아닙니다: 브라우저에서 다시 로그인한 뒤 '
                f'세션 쿠키 T를 다시 등록하세요.{reason}') from None

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


def _cli_error_message(exc, environ, tokens):
    message = str(exc).splitlines()[0].strip() if str(exc).splitlines() else ''
    secrets = [environ.get(name) for name in (
        'KOREANAIR_USERNAME', 'KOREANAIR_PASSWORD', 'KOREANAIR_SESSION_T')]
    secrets.extend(tokens)
    for secret in secrets:
        if secret:
            message = message.replace(secret, '[숨김]')
    return message or type(exc).__name__


def _result_row(row, verify_live):
    cabin = row[3]
    seat_type = 'upgrade' if cabin in UPGRADE_CABINS.values() else 'award'
    if not verify_live:
        live_status, live_detail = 'not_checked', None
    elif seat_type == 'upgrade':
        live_status, live_detail = 'unsupported', '실시간 확인 미지원: 공개 현황 기준'
    else:
        live_status, live_detail = 'unknown', None
    return {
        'date': row[0], 'flight': row[1], 'departure_time': row[2],
        'seat_type': seat_type, 'cabin': cabin,
        'live_status': live_status, 'live_detail': live_detail, 'seats': None,
    }


def _summary(routes):
    results = [row for route in routes for row in route['results']]
    return {
        'public_candidates': len(results),
        'selectable': sum(row['live_status'] == 'selectable' for row in results),
        'not_selectable': sum(row['live_status'] == 'not_selectable' for row in results),
        'unknown': sum(row['live_status'] == 'unknown' for row in results),
        'unsupported': sum(row['live_status'] == 'unsupported' for row in results),
        'route_errors': sum(route['status'] == 'error' for route in routes),
    }


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False))


def main(argv=None, environ=None, client_factory=AwardClient, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--routes', help='노선 쉼표 목록, 예: ICN-JFK,ICN-FRA')
    parser.add_argument('--origin', help='출발 공항 코드, 예: ICN (기존 단일 노선 옵션)')
    parser.add_argument('--destination', help='도착 공항 코드, 예: JFK (기존 단일 노선 옵션)')
    parser.add_argument('--start', help='조회 시작일 YYYY-MM-DD')
    parser.add_argument('--end', help='조회 종료일 YYYY-MM-DD (포함)')
    parser.add_argument('--month', help='시작 월 YYYY-MM (기본: 한국 시간 이번 달)')
    parser.add_argument('--until', help='종료 월 YYYY-MM (기존 옵션)')
    parser.add_argument('--days', type=int, default=360, help='오늘부터 최대 일수 (기본: 360)')
    parser.add_argument('--seat-types', default='award',
                        help='좌석 종류 쉼표 목록: award,upgrade (기본: award)')
    parser.add_argument('--verify-live', action='store_true',
                        help='후보를 실제 예매 검색으로 재확인(본인 성인 1명)')
    parser.add_argument('--session', help='세션 쿠키 JSON 파일 경로')
    parser.add_argument('--json', action='store_true', help='결과를 JSON 한 개로 출력')
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ

    if args.routes and (args.origin or args.destination):
        parser.error('--routes와 --origin/--destination은 함께 사용할 수 없습니다.')
    if args.routes:
        try:
            routes = parse_routes(args.routes)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.origin or args.destination:
        if not args.origin or not args.destination:
            parser.error('--origin과 --destination을 함께 지정해야 합니다.')
        legacy_route = f'{args.origin.strip().upper()}-{args.destination.strip().upper()}'
        try:
            routes = parse_routes(legacy_route)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        parser.error('--routes 또는 --origin과 --destination을 지정해야 합니다.')
    if bool(args.month or args.until) and bool(args.start or args.end):
        parser.error('--month/--until과 --start/--end는 함께 사용할 수 없습니다.')

    seat_types = []
    for value in args.seat_types.split(','):
        seat_type = value.strip().lower()
        if seat_type not in ('award', 'upgrade'):
            parser.error('--seat-types는 award,upgrade 중 하나 이상의 쉼표 목록이어야 합니다.')
        if seat_type not in seat_types:
            seat_types.append(seat_type)
    if not seat_types:
        parser.error('--seat-types를 하나 이상 지정해야 합니다.')

    if now is None:
        checked_at = datetime.now(KST)
    elif isinstance(now, datetime):
        checked_at = now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    elif isinstance(now, date):
        checked_at = datetime.combine(now, datetime.min.time(), KST)
    else:
        raise TypeError('현재 시각 형식이 올바르지 않습니다.')
    today = checked_at.date()
    try:
        if args.month or args.until:
            first, start, limit = query_window(today, args.month, args.until, args.days)
        else:
            first, start, limit = date_window(today, args.start, args.end, args.days)
    except (ValueError, OverflowError) as exc:
        parser.error(str(exc))

    window = {'start': start.isoformat(), 'end': limit.isoformat()}
    stamp = checked_at.isoformat(timespec='seconds')
    cabins = {}
    if 'award' in seat_types:
        cabins.update(CABINS)
    if 'upgrade' in seat_types:
        cabins.update(UPGRADE_CABINS)
    client = client_factory()
    seen_tokens = []
    route_results = []
    session_logged_in = False
    session_login_attempted = False
    stop_live = False
    stop_message = '이전 실시간 검색 실패로 중단'

    def emit_progress(line):
        if not args.json:
            print(line, flush=True)

    def add_public_results(route_result, rows):
        route_result['results'] = [_result_row(row, args.verify_live) for row in rows]

    try:
        client.initialize()
        for route_index, (origin, destination) in enumerate(routes):
            route_name = f'{origin}-{destination}'
            route_result = {
                'route': route_name, 'status': 'ok', 'error': None,
                'months_completed': 0, 'results': [],
            }
            route_results.append(route_result)
            if not args.json:
                print(f'{origin} → {destination}: {start} ~ {limit} (한국 시간)')
            completed_months = [0]

            def progress(line):
                if line.startswith('\n['):
                    completed_months[0] += 1
                emit_progress(line)

            try:
                rows, completed = find_candidates(
                    client, origin, destination, first, start, limit,
                    out=progress, cabins=cabins)
                route_result['months_completed'] = completed
            except Exception as exc:
                route_result['months_completed'] = completed_months[0]
                route_result['status'] = 'error'
                route_result['error'] = _cli_error_message(exc, environ, seen_tokens)
                if not args.json:
                    print(f'조회 실패: {route_result["error"]}')
                if route_index + 1 < len(routes):
                    time.sleep(2)
                continue

            if not args.json:
                print(f'완료: {completed}개월, {len(rows)}건 '
                      '(좌석 수가 아닌 항공편·등급별 표시 건수)')
            add_public_results(route_result, rows)
            award_rows = [row for row in rows if row[3] in CABINS.values()]
            row_results = {
                row: result for row, result in zip(rows, route_result['results'])
            }
            if args.verify_live and stop_live:
                route_result['status'] = 'error'
                route_result['error'] = stop_message
                continue
            if args.verify_live and award_rows:
                try:
                    if not session_login_attempted:
                        session_login_attempted = True
                        from hermes_alert import (
                            DEFAULT_SESSION, _read_session, _write_json_atomic,
                        )
                        selected_session = (
                            args.session or environ.get('KOREANAIR_SESSION_FILE')
                            or DEFAULT_SESSION)
                        session_path = Path(selected_session).expanduser()
                        token = _read_session(session_path, environ)
                        if token:
                            seen_tokens.append(token)
                            rotated = client.login_from_session(token)
                            if not isinstance(rotated, str) or not rotated:
                                raise RuntimeError('세션 쿠키 갱신 결과를 확인하지 못했습니다.')
                            if rotated:
                                seen_tokens.append(rotated)
                            _write_json_atomic(session_path, {
                                'T': rotated, 'updated': stamp,
                            })
                            session_logged_in = True
                        else:
                            login_settings(environ)
                            client.login_from_env(environ)
                            session_logged_in = True
                    elif not session_logged_in:
                        raise RuntimeError(stop_message)
                    results = verify_candidates(
                        client, origin, destination, award_rows)
                    for row, status, seats in results:
                        result = row_results[row]
                        result['live_detail'] = status
                        result['seats'] = seats
                        if status == '예매 검색에서 선택 가능':
                            result['live_status'] = 'selectable'
                        elif status == '예매 검색에서 선택 불가':
                            result['live_status'] = 'not_selectable'
                        else:
                            result['live_status'] = 'unknown'
                    if not args.json:
                        for row, status, seats in results:
                            count = f' / 응답 좌석 수 {seats}' if seats is not None else ''
                            print(f'\t{row[0]}\t{row[1]}\t{row[2]}\t{row[3]}\t'
                                  f'{status}{count}')
                except Exception as exc:
                    route_result['status'] = 'error'
                    route_result['error'] = _cli_error_message(exc, environ, seen_tokens)
                    stop_live = True
                    stop_message = '이전 실시간 검색 실패로 중단'
                    for row in award_rows:
                        row_results[row]['live_status'] = 'unknown'
                        row_results[row]['live_detail'] = None
                    if not args.json:
                        print(f'실시간 조회 실패: {route_result["error"]}')
            if route_index + 1 < len(routes):
                time.sleep(2)

        summary = _summary(route_results)
        payload = {
            'checked_at': stamp, 'window': window, 'seat_types': seat_types,
            'verify_live': args.verify_live, 'routes': route_results,
            'summary': summary,
        }
        if args.json:
            _print_json(payload)
        else:
            if not args.verify_live:
                print('공개 현황이며 최종 예약 가능 여부는 예매 화면에서 확인하세요.')
            print('요약: ' + ', '.join(
                f'{key}={value}' for key, value in summary.items()))
        return 1 if summary['route_errors'] or summary['unknown'] else 0
    except Exception as exc:
        message = _cli_error_message(exc, environ, seen_tokens)
        if args.json:
            _print_json({
                'error': message, 'checked_at': stamp, 'window': window,
                'seat_types': seat_types, 'verify_live': args.verify_live,
                'routes': route_results, 'summary': _summary(route_results),
            })
        else:
            print(f'조회 실패: {message}\n미조회 기간은 좌석 없음으로 판단하지 마세요.')
        return 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print(f'조회 실패: {str(exc).split(chr(10))[0]}\n미조회 기간은 좌석 없음으로 판단하지 마세요.')
        raise SystemExit(1)
