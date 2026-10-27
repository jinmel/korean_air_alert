"""대한항공 보너스 좌석 Hermes cron 알림."""
import argparse
import json
import os
import re
import tempfile
from datetime import date, datetime
from pathlib import Path

from korean_award import (
    AwardClient, KST, find_candidates, login_settings, query_window,
    verify_candidates,
)


DEFAULT_STATE = '~/.local/state/korean_air_alert/hermes_seen.json'
BOOKING_URL = 'https://www.koreanair.com/booking/search'


def _read_state(path):
    try:
        with path.open(encoding='utf-8') as state_file:
            state = json.load(state_file)
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(state, dict) or not isinstance(state.get('selectable'), dict):
        return {}
    return {
        key: value for key, value in state['selectable'].items()
        if isinstance(key, str) and isinstance(value, dict)
    }


def _write_state(path, selectable):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=path.parent,
                prefix=f'.{path.name}.', suffix='.tmp', delete=False) as state_file:
            temporary = Path(state_file.name)
            json.dump({'selectable': selectable}, state_file, ensure_ascii=False)
            state_file.write('\n')
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _failure_message(exc, environ):
    message = str(exc).splitlines()[0].strip() if str(exc).splitlines() else ''
    for name in ('KOREANAIR_USERNAME', 'KOREANAIR_PASSWORD'):
        secret = environ.get(name)
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


def run(argv=None, environ=None, client_factory=AwardClient, state_path=None, now=None):
    parser = argparse.ArgumentParser(description='대한항공 보너스 좌석 cron 알림')
    parser.add_argument('--route', action='append', required=True,
                        help='출발-도착 공항 코드, 예: ICN-JFK (반복 가능)')
    parser.add_argument('--days', type=int, default=360, help='조회 일수 (기본: 360)')
    parser.add_argument('--state', help='알림 상태 파일 경로')
    args = parser.parse_args(argv)
    for route in args.route:
        if not re.fullmatch(r'[A-Z]{3}-[A-Z]{3}', route):
            parser.error('--route는 대문자 공항 코드 형식 ORIG-DEST여야 합니다.')
        origin, destination = route.split('-')
        if origin == destination:
            parser.error('--route의 출발 공항과 도착 공항은 달라야 합니다.')
    if args.days < 1:
        parser.error('--days는 1 이상이어야 합니다.')

    environ = os.environ if environ is None else environ
    selected_state = (
        state_path or args.state or environ.get('KOREANAIR_ALERT_STATE') or DEFAULT_STATE)
    path = Path(selected_state).expanduser()
    try:
        checked_at = _current_time(now)
        today = checked_at.date()
        client = client_factory()
        client.initialize()
        candidates_by_route = {}
        for route in args.route:
            origin, destination = route.split('-')
            first, start, limit = query_window(today, days=args.days)
            candidates_by_route[route] = find_candidates(
                client, origin, destination, first, start, limit, out=None)[0]

        previous = _read_state(path)
        if not any(candidates_by_route.values()):
            _write_state(path, {})
            return 0

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

        _write_state(path, selectable)
        lines = []
        stamp = checked_at.isoformat(timespec='seconds')
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
        print(_failure_message(exc, environ))
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
