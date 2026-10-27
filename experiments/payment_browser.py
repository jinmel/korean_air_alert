"""Unfinished payment-screen investigation; does not submit travelers or payments."""
import asyncio
import getpass
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from korean_award import AwardClient

username = os.environ.get('KOREANAIR_USERNAME') or getpass.getpass('Account ID (hidden): ')
password = os.environ.get('KOREANAIR_PASSWORD') or getpass.getpass('Password (hidden): ')
private_values = [username, password]

def redact(text):
 for value in sorted(set(private_values),key=len,reverse=True):
  if isinstance(value,str) and len(value)>=3:
   text=text.replace(value,'[비공개]')
 text=re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[이메일]', text)
 text=re.sub(r'\d{4,}', '[숫자]', text)
 return text

async def main():
 global username,password
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  context=await browser.new_context(locale='ko-KR')
  page=await context.new_page()
  async def guard(route):
   path=urlsplit(route.request.url).path
   # Stop before traveler submission, booking creation, payment or ticket issuance.
   if (route.request.method in ('POST','PUT','PATCH','DELETE') and
       ((path.startswith('/api/pp/') and path not in {
            '/api/pp/payment/GetAvailablePaymentType',
            '/api/pp/payment/GetPromotionList',
        }) or path.startswith('/api/ap/booking/traveller/') or
        path.startswith('/api/ap/long-running/booking/purchase/') or
        re.search(r'/(?:pnrCreation|addFlights|addUpgradeSeg|issueTicketAfterPayment)(?:/|$)',path))):
    print('STOPPED_WRITE',path,flush=True)
    await route.abort()
   else:
    await route.continue_()
  await page.route('**/api/**',guard)
  client=AwardClient()
  os.environ['KOREANAIR_USERNAME']=username
  os.environ['KOREANAIR_PASSWORD']=password
  try:
   await asyncio.to_thread(client.initialize)
   await asyncio.to_thread(client.login_from_env)
  finally:
   os.environ.pop('KOREANAIR_USERNAME',None)
   os.environ.pop('KOREANAIR_PASSWORD',None)
  for value in client.traveler.values():
   if isinstance(value,str):private_values.append(value)
  cookies=[]
  for cookie in client.cookies:
   item={'name':cookie.name,'value':cookie.value,'domain':cookie.domain,
         'path':cookie.path or '/','secure':cookie.secure,
         'httpOnly':cookie.has_nonstandard_attr('HttpOnly')}
   if cookie.expires and cookie.expires>0:item['expires']=cookie.expires
   cookies.append(item)
  await context.add_cookies(cookies)
  await context.add_init_script('sessionStorage.setItem("ksessionId",'+json.dumps(client.headers['ksessionId'])+');')
  username=password=''
  await page.goto('https://www.koreanair.com/booking/search',wait_until='domcontentloaded')
  await page.wait_for_timeout(5000)
  cookie=page.get_by_role('button',name='필수 쿠키만 허용',exact=True)
  if await cookie.is_visible():await cookie.click()
  print('LOGIN_OK',urlsplit(page.url).path,flush=True)
  async def snapshot():
   text=await page.locator('body').aria_snapshot()
   print(redact(text)[:24000],flush=True)
  await snapshot()
  namespace={'page':page,'context':context,'browser':browser,'snapshot':snapshot,
             'asyncio':asyncio,'json':json,'re':re,'redact':redact,'urlsplit':urlsplit,'client':client}
  while True:
   print('READY',flush=True)
   line=await asyncio.to_thread(sys.stdin.readline)
   if not line:break
   try:
    message=json.loads(line)
    if message.get('command')=='close':break
    code=message['code']
    exec('async def step():\n'+''.join(' '+x+'\n' for x in code.splitlines()),namespace)
    await namespace['step']()
   except Exception as exc:
    print('STEP_ERROR',redact(str(exc))[:2000],flush=True)
  await browser.close()

try:
 asyncio.run(main())
except Exception as exc:
 print('BROWSER_ERROR',redact(str(exc))[:1500],flush=True)
