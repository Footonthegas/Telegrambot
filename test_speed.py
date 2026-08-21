import time
import sys
sys.path.insert(0, r'C:\Users\Theam\OneDrive\Desktop\attendance_bot')
from fast_scraper import fetch_attendance_sync

user_id = '2024UME4113'
password = 'Amanguliani@12345'
year = '2026-27'
semester = '5'
runs = 3

times = []
for i in range(runs):
    start = time.time()
    data, timeline, status = fetch_attendance_sync(user_id, password, year, semester)
    elapsed = (time.time() - start) * 1000
    times.append(elapsed)
    print(f'Python run {i+1}: status={status}, elapsed={elapsed:.0f}ms, subjects={len(data)}')
    for code, entry in sorted(data.items()):
        print(f'  {code}: present={entry["present"]}, absent={entry["absent"]}, total={entry["total"]}')
    if i < runs - 1:
        time.sleep(1)

avg = sum(times) / len(times)
print(f'\nPython average: {avg:.0f}ms over {runs} runs')