# 대한항공 보너스 좌석 월별 조회

Python 3.10 이상에서 표준 라이브러리만으로 실행합니다. 브라우저, Playwright,
추가 pip 패키지, 로그인은 필요하지 않습니다.

시작 월부터 한국 시간 오늘 + 360일까지 월별로 조회하여 프레스티지석 보너스(O),
일등석 보너스(A)를 출력합니다. 승급(Z 등)은 제외합니다.
문서화된 공식 외부 공개 API가 아니라 공식 웹사이트의 조회 API를 이용합니다.

```bash
# 이번 달부터 조회 한도까지
python korean_award.py --origin ICN --destination JFK

# 시작 월 변경: 해당 월부터 조회 한도까지
python korean_award.py --origin ICN --destination JFK --month 2027-01

# 시작 월과 종료 월 지정
python korean_award.py --origin ICN --destination JFK --month 2027-01 --until 2027-03

# 반대 방향
python korean_award.py --origin JFK --destination ICN
```

## 동작 방식

1. Python urllib로 공식 보너스 좌석 조회 페이지를 열어 HTTP 세션을 초기화합니다.
2. CookieJar가 서버의 쿠키를 메모리에 보관하고 이후 요청에 전달합니다.
3. 동일한 세션으로 다음 API에 월별 POST 요청을 보냅니다.

```text
https://www.koreanair.com/api/hmp/bonusSeatView/bonusSeatView
```

```json
{"arrivalAirport": "JFK", "departureAirport": "ICN", "departureDate": "20270101"}
```

User-Agent는 `KoreanAwardChecker/1.0 (Python urllib)`입니다. 웹사이트 요청에 쓰이는
Origin, Referer, channel 등의 헤더를 포함하며 JavaScript나 브라우저 엔진을 실행하지 않습니다.
브라우저 쿠키 복사, 브라우저 식별값 위장, CAPTCHA 해제, 프록시 순환은 사용하지 않습니다.
실행 환경의 프록시 및 TLS 인증서 검증 설정은 기본값을 따릅니다.

## 범위 및 오류 처리

- 노선 하나의 편도 조회입니다. 반대 방향은 공항 코드를 바꾸어 실행하세요.
- `--month` 기본값은 한국 시간 이번 달입니다. 과거 날짜는 출력하지 않습니다.
- `--until` 생략 시 한도까지 조회합니다. `--days` 기본값은 360입니다.
  이 값을 키워도 서버의 조회 가능 범위가 늘어나는 것은 아닙니다.
- 미래 월을 시작 월로 선택해도 한도는 실행일 기준입니다.
- 월별 요청 사이에 2초 간격을 둡니다. 오류나 차단이 있으면 중단하며 재시도하지 않습니다.
- 미인식 응답, 날짜 누락, 요청과 다른 월/노선, 빈 항공편 목록은 조회 실패로 처리합니다.
  미운항 노선/월도 성공으로 판정하지 못할 수 있습니다.
- 403이나 인증 요구를 좌석 없음으로 해석하지 않습니다. 쿠키와 요청 헤더는 파일에 저장하지 않습니다.
- 공개 현황은 공식 화면 안내상 일 1회 업데이트됩니다. 실시간 예약 가능 여부나 좌석 수가 아닙니다.
- 비공식 API의 요청 구조나 접근 정책이 바뀌면 수정이 필요할 수 있습니다.

## 실제 검증

2026-10-27 한국 시간, 현재 클라우드 환경에서 브라우저 없이 다음 명령으로 검증했습니다.

```bash
python -m unittest -v
python korean_award.py --origin ICN --destination JFK
python korean_award.py --origin ICN --destination JFK --month 2027-01 --until 2027-01
```

- 테스트 10개 통과: 날짜 범위/승급 제외, HTTP 세션 쿠키 유지, 월 변경,
  403·HTML 응답·잘못된 월/노선 거부.
- ICN → JFK, 2026-10-27 ~ 2027-10-22, 13개월 월별 조회 완료(종료 코드 0).
- 프레스티지 보너스 표시: 2026-10-27 KE081, 2027-10-22 KE081/KE085, 총 3건.
- 위 조회에서 일등석 보너스 표시는 없었습니다. 건수는 좌석 수가 아닙니다.
- 시작 월을 2027-01로 지정한 단일 월 조회도 완료했습니다(종료 코드 0).
- 이전 공식 화면에서 확인한 현황 기준 시각은 2026-10-26 23:00 KST입니다.

검증은 위 노선 및 실행 시점에 한정됩니다. 최종 예약 가능 여부는 예매 화면에서 확인하세요.
