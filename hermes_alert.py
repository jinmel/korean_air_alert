"""대한항공 보너스 좌석 Hermes cron 알림."""
import argparse
import getpass
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from chrome_session import DEFAULT_CDP_URL, grab_session_token, push_session_token
from korean_award import (
    AwardClient, KST, find_candidates, login_settings, query_window,
    verify_candidates,
)


DEFAULT_STATE = '~/.local/state/korean_air_alert/hermes_seen.json'
DEFAULT_SESSION = '~/.local/state/korean_air_alert/session.json'
BOOKING_URL = 'https://www.koreanair.com/booking/search'


def _read_state(path):
    try:
        with path.open(encoding='utf-8') as state_file:
            state = json.load(state_file)
    except (OSError, ValueError, UnicodeError):
        return {'selectable': {}, 'public': {}}
    if not isinstance(state, dict):
        state = {}
    selectable = state.get('selectable')
    public = state.get('public')
    if not isinstance(selectable, dict):
        selectable = {}
    if not isinstance(public, dict):
        public = {}
    return {
        'selectable': {
            key: value for key, value in selectable.items()
            if isinstance(key, str) and isinstance(value, dict)
        },
        'public': {
            key: value for key, value in public.items()
            if isinstance(key, str) and isinstance(value, dict)
        },
    }


def _write_json_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=path.parent,
                prefix=f'.{path.name}.', suffix='.tmp', delete=False) as state_file:
            temporary = Path(state_file.name)
            json.dump(value, state_file, ensure_ascii=False)
            state_file.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_state(path, state):
    _write_json_atomic(path, state)


def _read_session(path, environ):
    try:
        with path.open(encoding='utf-8') as session_file:
            session = json.load(session_file)
    except FileNotFoundError:
        return environ.get('KOREANAIR_SESSION_T', '').strip()
    except (OSError, ValueError, UnicodeError):
        return ''
    token = session.get('T', '') if isinstance(session, dict) else ''
    return token if isinstance(token, str) else ''


def _cache_rows(entry):
    rows = entry.get('rows') if isinstance(entry, dict) else None
    if not isinstance(rows, list):
        return None
    if any(not isinstance(row, list) or len(row) != 4
           or any(not isinstance(value, str) for value in row) for row in rows):
        return None
    return [tuple(row) for row in rows]


def _cache_is_fresh(entry, checked_at, start, hours):
    if hours <= 0 or not isinstance(entry, dict) or entry.get('start') != start:
        return False
    if _cache_rows(entry) is None:
        return False
    try:
        fetched = datetime.fromisoformat(entry['fetched'])
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=KST)
        else:
            fetched = fetched.astimezone(KST)
    except (KeyError, TypeError, ValueError):
        return False
    age = checked_at - fetched
    return timedelta(0) <= age < timedelta(hours=hours)


def _failure_message(exc, environ, session_tokens=()):
    message = str(exc).splitlines()[0].strip() if str(exc).splitlines() else ''
    secrets = [environ.get(name) for name in (
        'KOREANAIR_USERNAME', 'KOREANAIR_PASSWORD', 'KOREANAIR_SESSION_T')]
    secrets.extend(session_tokens)
    for secret in secrets:
        if secret:
            message = message.replace(secret, '[숨김]')
    return f'대한항공 보너스 알림 실패: {message or type(exc).__name__}'


def _current_time(now):
    if now is None:
        return datetime.now(KST)
    if isinstance(now, datetime):
        return now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST)
    if isinstance(now, date):
        return datetime.combine(now, datetime.min.time(), KST)
    raise TypeError('현재 시각 형식이 올바르지 않습니다.')


def _login_with_session(client, token, cdp_url, seen_tokens):
    """Return the rotated session token, or '' when no session is configured."""
    failure = None
    if token:
        try:
            rotated = client.login_from_session(token)
            seen_tokens.append(rotated)
            return rotated
        except RuntimeError as exc:
            failure = exc
    if not cdp_url:
        if failure:
            raise failure
        return ''
    try:
        grabbed = grab_session_token(cdp_url)
    except RuntimeError as exc:
        if failure:
            raise RuntimeError(f'{failure}; Chrome 세션 가져오기 실패: {exc}') from None
        raise
    seen_tokens.append(grabbed)
    if grabbed == token:
        raise RuntimeError(f'{failure}; Chrome 세션도 같은 만료된 T입니다: '
                           '그 Chrome에서 koreanair.com에 다시 로그인하세요.')
    rotated = client.login_from_session(grabbed)
    seen_tokens.append(rotated)
    return rotated


def run(argv=None, environ=None, client_factory=AwardClient, state_path=None, now=None):
    parser = argparse.ArgumentParser(description='대한항공 보너스 좌석 cron 알림')
    parser.add_argument('--route', action='append',
                        help='출발-도착 공항 코드, 예: ICN-JFK (반복 가능)')
    parser.add_argument('--days', type=int, default=360, help='조회 일수 (기본: 360)')
    parser.add_argument('--state', help='알림 상태 파일 경로')
    parser.add_argument('--session', help='브라우저 세션 쿠키 파일 경로')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--save-session', action='store_true',
                      help='브라우저 세션 쿠키를 확인하고 저장합니다.')
    mode.add_argument('--grab-session', action='store_true',
                      help='Chrome 원격 디버깅에서 세션 쿠키를 가져와 저장합니다.')
    parser.add_argument('--cdp', help='세션 만료 시 T를 가져올 Chrome 원격 디버깅 주소, '
                        f'예: {DEFAULT_CDP_URL}')
    parser.add_argument('--public-cache-hours', type=float, default=3,
                        help='공개 현황 캐시 시간 (기본: 3, 0은 비활성화)')
    args = parser.parse_args(argv)
    routes = args.route or []
    for route in routes:
        if not re.fullmatch(r'[A-Z]{3}-[A-Z]{3}', route):
            parser.error('--route는 대문자 공항 코드 형식 ORIG-DEST여야 합니다.')
        origin, destination = route.split('-')
        if origin == destination:
            parser.error('--route의 출발 공항과 도착 공항은 달라야 합니다.')
    if not args.save_session and not args.grab_session and not routes:
        parser.error('--route를 하나 이상 지정해야 합니다.')
    if args.days < 1:
        parser.error('--days는 1 이상이어야 합니다.')
    if not math.isfinite(args.public_cache_hours) or args.public_cache_hours < 0:
        parser.error('--public-cache-hours는 0 이상이어야 합니다.')

    environ = os.environ if environ is None else environ
    selected_state = (
        state_path or args.state or environ.get('KOREANAIR_ALERT_STATE') or DEFAULT_STATE)
    path = Path(selected_state).expanduser()
    selected_session = (
        args.session or environ.get('KOREANAIR_SESSION_FILE') or DEFAULT_SESSION)
    session_path = Path(selected_session).expanduser()
    cdp_url = args.cdp or environ.get('KOREANAIR_CDP_URL') or ''
    session_tokens = []
    try:
        checked_at = _current_time(now)
        stamp = checked_at.isoformat(timespec='seconds')
        if args.save_session or args.grab_session:
            if args.save_session:
                token = getpass.getpass('세션 쿠키 T (입력 숨김): ').strip()
            else:
                token = grab_session_token(cdp_url or DEFAULT_CDP_URL)
            session_tokens.append(token)
            client = client_factory()
            client.initialize()
            rotated = client.login_from_session(token)
            session_tokens.append(rotated)
            _write_json_atomic(session_path, {'T': rotated, 'updated': stamp})
            source = 'Chrome에서 가져옴, ' if args.grab_session else ''
            print(f'세션 저장 완료 ({source}확인 {stamp})')
            return 0

        today = checked_at.date()
        client = client_factory()
        client.initialize()
        session_token = _read_session(session_path, environ)
        session_tokens.append(session_token)
        session_token = _login_with_session(client, session_token, cdp_url, session_tokens)
        if session_token:
            _write_json_atomic(session_path, {'T': session_token, 'updated': stamp})
            if cdp_url:
                try:
                    push_session_token(session_token, cdp_url)
                except Exception:
                    pass

        state = _read_state(path)
        public = dict(state['public'])
        candidates_by_route = {}
        for route in routes:
            origin, destination = route.split('-')
            first, start, limit = query_window(today, days=args.days)
            start_text = start.isoformat()
            entry = public.get(route)
            if _cache_is_fresh(entry, checked_at, start_text, args.public_cache_hours):
                candidates_by_route[route] = _cache_rows(entry)
                continue
            rows = find_candidates(
                client, origin, destination, first, start, limit, out=None)[0]
            candidates_by_route[route] = rows
            public[route] = {
                'fetched': stamp, 'start': start_text,
                'rows': [list(row) for row in rows],
            }

        previous = state['selectable']
        if not any(candidates_by_route.values()):
            _write_state(path, {'selectable': {}, 'public': public})
            return 0

        if not session_token:
            login_settings(environ)
            client.login_from_env(environ)
        selectable = {}
        new_alerts = []
        unknown_count = 0
        for route, candidates in candidates_by_route.items():
            origin, destination = route.split('-')
            results = verify_candidates(client, origin, destination, candidates)
            for row, status, seats in results:
                if status == '예매 검색에서 선택 가능' and seats is not None:
                    key = f'{route}|{row[0]}|{row[1]}|{row[3]}'
                    old = previous.get(key)
                    first_seen = (
                        old.get('first_seen')
                        if isinstance(old, dict) and isinstance(old.get('first_seen'), str)
                        else checked_at.isoformat(timespec='seconds'))
                    selectable[key] = {'first_seen': first_seen, 'seats': seats}
                    if key not in previous:
                        new_alerts.append((route, row, seats))
                elif status.startswith('미확인'):
                    unknown_count += 1

        _write_state(path, {'selectable': selectable, 'public': public})
        lines = []
        if new_alerts:
            lines.append(f'대한항공 보너스 좌석 예매 가능 (확인 {stamp})')
            for route, row, seats in new_alerts:
                origin, destination = route.split('-')
                lines.append(
                    f'{origin}→{destination} {row[0]} {row[1]} {row[2]} {row[3]} / '
                    f'응답 좌석 수 {seats}')
            lines.extend((
                f'예매: {BOOKING_URL}',
                '예매 검색 시점의 상태이며 좌석 확보·예약·결제는 하지 않았습니다.',
            ))
        if unknown_count:
            lines.append(f'미확인 {unknown_count}건: 좌석 없음으로 판단하지 마세요.')
        if lines:
            print('\n'.join(lines))
        return 1 if unknown_count else 0
    except Exception as exc:
        print(_failure_message(exc, environ, session_tokens))
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
