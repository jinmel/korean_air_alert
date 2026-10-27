"""Hermes Agent cron 실행 스크립트."""
from pathlib import Path
import sys

# 복제한 저장소 경로에 맞게 변경하세요.
# 세션 쿠키 등록은 README의 "세션 쿠키 모드 (권장)"을 참고하세요.
REPO = Path('~/repos/korean_air_alert').expanduser()
ROUTES = ['ICN-JFK', 'JFK-ICN']
# 예: 'http://127.0.0.1:9222' — 세션 만료 시 이 Chrome에서 T를 다시 가져옵니다.
CDP_URL = ''

sys.path.insert(0, str(REPO))
import hermes_alert

args = [argument for route in ROUTES for argument in ('--route', route)]
if CDP_URL:
    args += ['--cdp', CDP_URL]
raise SystemExit(hermes_alert.run(args))
