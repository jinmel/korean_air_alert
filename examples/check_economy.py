import getpass
import os
import sys
from pathlib import Path
import time
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korean_award import AwardClient, KST, flight_key, parse_date

def main():
 os.environ['KOREANAIR_USERNAME'] = os.environ.get('KOREANAIR_USERNAME') or getpass.getpass('Account ID (hidden): ')
 os.environ['KOREANAIR_PASSWORD'] = os.environ.get('KOREANAIR_PASSWORD') or getpass.getpass('Password (hidden): ')
 client = AwardClient()
 try:
  client.initialize()
  client.login_from_env()
  print('로그인 성공. 본인 성인 1명 일반석 보너스 검색.', flush=True)
  today = datetime.now(KST).date()
  for index, offset in enumerate((1, 7, 30, 360)):
   if index:
    time.sleep(2)
   day = today + timedelta(days=offset)
   payload = client.fetch_live('ICN', 'JFK', day)
   if not isinstance(payload, dict) or payload.get('errors') or payload.get('code'):
    raise RuntimeError('예매 검색 오류: 예약 가능 여부 미확인')
   bounds = payload.get('upsellBoundAvailList')
   if not isinstance(bounds, list) or len(bounds) != 1:
    raise RuntimeError('예매 검색 응답 구조 미확인')
   flights = bounds[0].get('availFlightList')
   if not isinstance(flights, list):
    raise RuntimeError('예매 검색 항공편 목록 미확인')
   matches = []
   for flight in flights:
    segments = flight.get('flightInfoList', [])
    if len(segments) != 1:
     continue
    seg = segments[0]
    departure = str(seg.get('departureDateTime', ''))
    result_day = parse_date(departure[:10]) if '-' in departure[:10] else parse_date(departure[:8])
    if (result_day != day or seg.get('departureAirport') != 'ICN'
        or seg.get('arrivalAirport') != 'JFK'):
     raise RuntimeError('예매 검색 응답의 노선 또는 날짜 불일치')
    for fare in flight.get('commercialFareFamilyList', []):
     if fare.get('fareFamily') != 'KEBONUSEY' or fare.get('bookingClass') != 'X':
      continue
     count = fare.get('seatCount')
     if (isinstance(count, bool) or not str(count).isdigit() or int(count) <= 0
         or fare.get('soldout') is not False or flight.get('soldOut') is True
         or not fare.get('recommendId')):
      continue
     carrier, number = flight_key(str(seg.get('carrierCode', '')) + str(seg.get('flightNumber', '')))
     departure_time = departure[8:10]+':'+departure[10:12] if '-' not in departure[:10] else departure[11:16]
     matches.append((day.isoformat(), f'{carrier}{number:03}', departure_time,
                     '일반석 보너스 X / 선택 가능', '응답 좌석 수 '+str(count),
                     '마일리지 '+str(fare.get('totalMileage', '미확인'))))
   print(f'검색일 {day}: 선택 가능 일반석 보너스 {len(matches)}건', flush=True)
   for row in matches:
    print('\t'.join(row), flush=True)
   if matches:
    print('확인 시각 '+datetime.now(KST).isoformat(timespec='seconds'), flush=True)
    return
  print('확인한 날짜에서는 선택 가능한 일반석 보너스를 찾지 못했습니다.', flush=True)
 finally:
  os.environ.pop('KOREANAIR_USERNAME', None)
  os.environ.pop('KOREANAIR_PASSWORD', None)

try:
 main()
except Exception as exc:
 print('조회 실패: '+str(exc).split('\n')[0], flush=True)
 raise SystemExit(1)
