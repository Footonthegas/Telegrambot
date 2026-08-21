import requests
import sys
sys.path.insert(0, r'C:\Users\Theam\OneDrive\Desktop\attendance_bot')
from captcha_solver import solve_captcha_from_bytes

session = requests.Session()
session.get('https://www.imsnsit.org/imsnsit/')
session.get('https://www.imsnsit.org/imsnsit/plum5_fw_login.php?t=sw&w=1')
session.get('https://www.imsnsit.org/imsnsit/student.htm')
session.get('https://www.imsnsit.org/imsnsit/student_login110.php')
resp = session.get('https://www.imsnsit.org/imsnsit/student_login.php')
html = resp.text
import re
capsrc = re.search(r"<img src='([^']+captcha[^']+)' id='captchaimg'", html)
if capsrc:
    captcha_url = 'https://www.imsnsit.org/imsnsit/' + capsrc.group(1)
    captcha_resp = session.get(captcha_url, headers={'Referer': 'https://www.imsnsit.org/imsnsit/student_login.php'})
    print(f'CAPTCHA size: {len(captcha_resp.content)} bytes')
    result = solve_captcha_from_bytes(captcha_resp.content)
    print(f'CAPTCHA result: {result!r}')
else:
    print('No CAPTCHA found in login page')