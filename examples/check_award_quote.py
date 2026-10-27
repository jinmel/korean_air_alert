import getpass
import json
import os
import re
import sys
from pathlib import Path
from datetime import datetime, timedelta
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korean_award import AwardClient, KST, flight_key


def main():
 os.environ['KOREANAIR_USERNAME'] = os.environ.get('KOREANAIR_USERNAME') or getpass.getpass('Account ID (hidden): ')
 os.environ['KOREANAIR_PASSWORD'] = os.environ.get('KOREANAIR_PASSWORD') or getpass.getpass('Password (hidden): ')
 client = AwardClient()
 try:
  client.initialize()
  client.login_from_env()
  day = datetime.now(KST).date() + timedelta(days=1)
  result = client.fetch_live('ICN', 'JFK', day)
  if not isinstance(result, dict) or result.get('code') or result.get('errors'):
   raise RuntimeError('예매 검색 실패')
  chosen = None
  for flight in result.get('upsellBoundAvailList', [{}])[0].get('availFlightList', []):
   segments = flight.get('flightInfoList', [])
   if len(segments) != 1:
    continue
   seg = segments[0]
   if (seg.get('departureAirport') != 'ICN' or seg.get('arrivalAirport') != 'JFK'
       or str(seg.get('departureDateTime', ''))[:8] != day.strftime('%Y%m%d')):
    continue
   for fare in flight.get('commercialFareFamilyList', []):
    count = fare.get('seatCount')
    if (fare.get('fareFamily') == 'KEBONUSEY' and fare.get('bookingClass') == 'X'
        and str(count).isdigit() and int(count) > 0 and fare.get('soldout') is False
        and fare.get('recommendId') and flight.get('flightId')):
     chosen = flight, fare, seg
     break
   if chosen:
    break
  if chosen is None:
   raise RuntimeError('내일 일반석 보너스를 선택할 수 없습니다.')
  flight, fare, seg = chosen
  carrier, number = flight_key(str(seg['carrierCode'])+str(seg['flightNumber']))
  print('선택:', day, f'{carrier}{number:03}', '일반석 보너스 X', flush=True)
  body = {'recommendList': [{'recommendId': fare['recommendId'], 'flightId': flight['flightId']}]}
  raw = client._request(client.base_url + '/api/ap/booking/avail/fareInformation', body)
  quote = json.loads(raw)
  if not isinstance(quote, dict) or quote.get('code') or quote.get('errors'):
   raise RuntimeError('운임 계산 API 성공을 확인하지 못했습니다.')
  pricing = quote.get('pnrFareInfo')
  if not isinstance(pricing, dict):
   print('운임 응답에 예상한 요약 정보가 없습니다.', flush=True)
   print('응답 필드:', ','.join(quote.keys()), flush=True)
   return
  safe = {}
  for key in ('mileage', 'currency', 'currencyCode', 'fare', 'totalFare', 'baseFare', 'tax', 'totalTax', 'totalAmount', 'fareAmount', 'taxAmount', 'total', 'totalFee', 'fuelSurcharge'):
   value = pricing.get(key)
   if isinstance(value, (int, float, str)) and re.fullmatch(r'(?:[A-Z]{3}|\d+(?:\.\d+)?)', str(value)):
    safe[key] = value
  print('운임 계산 성공:', json.dumps(safe,ensure_ascii=False), flush=True)
  print('운임 요약 필드:', ','.join(pricing.keys()), flush=True)
  required = pricing.get('mileage')
  if required is None:
   raise RuntimeError('확정 계산의 필요 마일리지를 확인할 수 없습니다.')
  profile = json.loads(client._request(client.base_url + '/api/li/auth/loginUserInfo'))
  remaining = profile.get('remainMileage') if isinstance(profile,dict) else None
  if str(required).isdigit() and str(remaining).isdigit():
   print('본인 마일리지 충족 여부:', int(remaining)>=int(required), flush=True)
  else:
   print('본인 마일리지 충족 여부: 미확인', flush=True)
  print('확인 시각:', datetime.now(KST).isoformat(timespec='seconds'), flush=True)
  print('예약 생성·확정·결제 API는 호출하지 않았습니다.', flush=True)
 finally:
  os.environ.pop('KOREANAIR_USERNAME', None)
  os.environ.pop('KOREANAIR_PASSWORD', None)

try:
 main()
except Exception as exc:
 print('확인 실패:', str(exc).split('\n')[0], flush=True)
 raise SystemExit(1)
