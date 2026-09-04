import sqlite3
import time
import requests
import sys
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

BASE_URL = "https://www.sut.ru/studentu/raspisanie/raspisanie-zanyatiy-studentov-ochnoy-i-vecherney-form-obucheniya"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}
PAIR_TIMES = {1: "09:00–10:35", 2: "10:45–12:20", 3: "13:00–14:35", 4: "14:45–16:20", 5: "16:30–18:05", 6: "18:15–19:50"}
DAYS_NAMES = {1: "Понедельник", 2: "Вторник", 3: "Среда", 4: "Четверг", 5: "Пятница", 6: "Суббота"}

def get_semester_weeks():
    now = datetime.now(timezone.utc) + timedelta(hours=3) # MSK
    year = now.year
    start_date = datetime(year, 2, 5) if now.month < 8 else datetime(year, 9, 1)
    start_monday = start_date - timedelta(days=start_date.weekday())
    weeks = []
    for w in range(1, 19):
        monday = start_monday + timedelta(weeks=w-1)
        weeks.append((w, monday.strftime("%Y-%m-%d")))
    return weeks

def get_current_week_num(weeks_plan):
    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    for w_num, w_date in weeks_plan:
        m_date = datetime.strptime(w_date, "%Y-%m-%d").replace(tzinfo=timezone(timedelta(hours=3)))
        if m_date <= now_msk < m_date + timedelta(days=7):
            return w_num
    return 1

def main():
    is_full_scan = "--full" in sys.argv
    weeks_plan = get_semester_weeks()
    current_w = get_current_week_num(weeks_plan)
    
    # Определяем, какие недели парсить
    if is_full_scan:
        target_weeks = [w[0] for w in weeks_plan]
        mode_text = "ПОЛНЫЙ (18 недель)"
    else:
        # Текущая + следующая (но не выше 18)
        next_w = current_w + 1
        target_weeks = [current_w]
        if next_w <= 18: target_weeks.append(next_w)
        mode_text = f"БЫСТРЫЙ (недели: {target_weeks})"

    print(f"🚀 Старт парсинга. Режим: {mode_text}")
    
    session = requests.Session()
    res = session.get(BASE_URL, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(res.text, "html.parser")
    
    groups_dict = {}
    groups_faculty = {}

    for link in soup.find_all("a", class_="vt256"):
        g_name = link.get("data-nm", "").strip().upper()
        g_id = link.get("data-i", "").strip()
        if not g_name or not g_id: continue
        groups_dict[g_name] = g_id
        header = link.find_previous(["h2", "h3", "h4", "h5"])
        header_text = header.get_text(strip=True).upper() if header else ""
        fac = "Другие"
        for f_abbr in ["ИКСС", "ИТПИ", "РСР", "КБ", "СТЭД", "ИМ", "ИНО", "СПБКТ"]:
            if f_abbr in header_text:
                fac = "СПбКТ" if "СПБ" in f_abbr else f_abbr
                break
        if fac == "Другие" and g_name.startsWith("К") and not g_name.startsWith("КБ"): fac = "СПбКТ"
        groups_faculty[g_name] = fac

    tasks_args = []
    for g_name, g_id in groups_dict.items():
        for w_num, w_date in weeks_plan:
            if w_num in target_weeks:
                tasks_args.append((g_name, g_id, w_num, w_date))

    def worker_scrape(arg):
        grp_name, grp_id, w_num, w_date = arg
        url = f"{BASE_URL}?group={grp_id}&date={w_date}"
        local_lessons = []
        try:
            r = requests.get(url, headers=HEADERS, timeout=14)
            if r.status_code == 200 and "vt258" in r.text:
                s = BeautifulSoup(r.text, "html.parser")
                for day_num in range(1, 7):
                    cells = s.find_all("div", class_=f"rasp-day{day_num}")
                    for pair_idx, cell in enumerate(cells, start=1):
                        block = cell.find("div", class_="vt258")
                        if not block: continue
                        local_lessons.append((
                            grp_name, day_num, DAYS_NAMES[day_num], pair_idx,
                            PAIR_TIMES.get(pair_idx, ""),
                            block.find("div", class_="vt240").get_text(strip=True),
                            block.find("div", class_="vt243").get_text(strip=True) if block.find("div", class_="vt243") else "",
                            block.find("div", class_="vt241").get_text(strip=True) if block.find("div", class_="vt241") else "Не указан",
                            block.find("div", class_="vt242").get_text(strip=True) if block.find("div", class_="vt242") else "Не указана",
                            w_num, w_date
                        ))
            return local_lessons, True
        except: return [], False

    all_new_lessons = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_scrape, arg) for arg in tasks_args]
        done = 0
        for f in as_completed(futures):
            res_lessons, ok = f.result()
            if ok: all_new_lessons.extend(res_lessons)
            done += 1
            if done % 100 == 0: print(f"Прогресс: {done}/{len(tasks_args)}")

    # СОХРАНЕНИЕ (УМНОЕ ОБНОВЛЕНИЕ)
    conn = sqlite3.connect("schedule.db")
    cur = conn.cursor()
    cur.execute("BEGIN TRANSACTION;")
    
    # 1. Метаданные
    cur.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);")
    moscow_now = datetime.now(timezone.utc) + timedelta(hours=3)
    cur.execute("INSERT OR REPLACE INTO metadata VALUES ('build_time', ?);", (moscow_now.strftime("%d.%m.%Y в %H:%M"),))
    
    # 2. Группы
    cur.execute("CREATE TABLE IF NOT EXISTS groups (group_name TEXT PRIMARY KEY, faculty TEXT);")
    cur.executemany("INSERT OR REPLACE INTO groups VALUES (?, ?);", [(g, groups_faculty.get(g, "СПбГУТ")) for g in groups_dict.keys()])

    # 3. Уроки (удаляем только те недели, которые перепарсили)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, day_num INTEGER, day_name TEXT,
            pair_num INTEGER, time_str TEXT, subject TEXT, lesson_type TEXT, teacher TEXT, room TEXT,
            week_num INTEGER, week_date TEXT
        );
    """)
    
    weeks_placeholder = ','.join(['?'] * len(target_weeks))
    cur.execute(f"DELETE FROM lessons WHERE week_num IN ({weeks_placeholder})", target_weeks)
    
    cur.executemany("""
        INSERT INTO lessons (group_name, day_num, day_name, pair_num, time_str, subject, lesson_type, teacher, room, week_num, week_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, all_new_lessons)

    cur.execute("COMMIT;")
    conn.close()
    print(f"✅ База обновлена! Режим: {mode_text}")

if __name__ == "__main__":
    main()
