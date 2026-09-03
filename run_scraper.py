import sqlite3
import time
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

BASE_URL = "https://www.sut.ru/studentu/raspisanie/raspisanie-zanyatiy-studentov-ochnoy-i-vecherney-form-obucheniya"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}
PAIR_TIMES = {1: "09:00–10:35", 2: "10:45–12:20", 3: "13:00–14:35", 4: "14:45–16:20", 5: "16:30–18:05", 6: "18:15–19:50"}
DAYS_NAMES = {1: "Понедельник", 2: "Вторник", 3: "Среда", 4: "Четверг", 5: "Пятница", 6: "Суббота"}

def get_semester_weeks():
    now = datetime.now()
    year = now.year
    start_date = datetime(year, 2, 5) if now.month < 8 else datetime(year, 9, 1)
    start_monday = start_date - timedelta(days=start_date.weekday())
    weeks = []
    for w in range(1, 19):
        monday = start_monday + timedelta(weeks=w-1)
        weeks.append((w, monday.strftime("%Y-%m-%d")))
    return weeks

def main():
    print("🚀 Старт сбора расписания через домашний IP...")
    session = requests.Session()
    session.headers.update(HEADERS)
    
    res = session.get(BASE_URL, timeout=20)
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
        for f_abbr in ["ИКСС", "ИТПИ", "РСР", "КБ", "СТЭД", "ИМ", "ИНО", "СПБКТ", "СПБ КТ"]:
            if f_abbr in header_text:
                fac = "СПбКТ" if "СПБ" in f_abbr else f_abbr
                break
        if fac == "Другие" and g_name.startswith("К") and not g_name.startswith("КБ"):
            fac = "СПбКТ"

        groups_faculty[g_name] = fac

    print(f"✅ Найдено групп: {len(groups_dict)}")
    weeks_plan = get_semester_weeks()
    tasks_args = []
    for g_name, g_id in groups_dict.items():
        for w_num, w_date in weeks_plan:
            tasks_args.append((g_name, g_id, w_num, w_date))

    def worker_scrape(arg):
        grp_name, grp_id, w_num, w_date = arg
        url = f"{BASE_URL}?group={grp_id}&date={w_date}"
        local_lessons = []
        for attempt in range(3):
            try:
                r = requests.get(url, headers=HEADERS, timeout=14)
                if r.status_code == 200:
                    if "vt258" in r.text:
                        s = BeautifulSoup(r.text, "html.parser")
                        for day_num in range(1, 7):
                            cells = s.find_all("div", class_=f"rasp-day{day_num}")
                            for pair_idx, cell in enumerate(cells, start=1):
                                block = cell.find("div", class_="vt258")
                                if not block: continue
                                subj_el = block.find("div", class_="vt240")
                                type_el = block.find("div", class_="vt243")
                                teach_el = block.find("div", class_="vt241")
                                room_el = block.find("div", class_="vt242")
                                local_lessons.append((
                                    grp_name, day_num, DAYS_NAMES[day_num], pair_idx,
                                    PAIR_TIMES.get(pair_idx, ""),
                                    subj_el.get_text(strip=True) if subj_el else "Без названия",
                                    type_el.get_text(strip=True) if type_el else "",
                                    teach_el.get_text(strip=True) if teach_el else "Не указан",
                                    room_el.get_text(strip=True) if room_el else "Не указана",
                                    w_num, w_date
                                ))
                    return grp_name, local_lessons, True
                elif r.status_code == 429:
                    time.sleep(1.5)
            except Exception:
                time.sleep(0.5)
        return grp_name, local_lessons, False

    all_lessons = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(worker_scrape, arg) for arg in tasks_args]
        done = 0
        for f in as_completed(futures):
            _, res_lessons, ok = f.result()
            if ok and res_lessons: all_lessons.extend(res_lessons)
            done += 1
            if done % 100 == 0 or done == len(tasks_args):
                print(f"[{done}/{len(tasks_args)}] Собрано пар: {len(all_lessons)}")

    conn = sqlite3.connect("schedule.db")
    cursor = conn.cursor()
    cursor.execute("BEGIN TRANSACTION;")
    cursor.execute("CREATE TABLE IF NOT EXISTS groups (group_name TEXT PRIMARY KEY, faculty TEXT);")
    cursor.execute("DELETE FROM groups;")
    cursor.executemany("INSERT INTO groups VALUES (?, ?);", [(g, groups_faculty.get(g, "СПбГУТ")) for g in groups_dict.keys()])
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lessons (
            id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT, day_num INTEGER, day_name TEXT,
            pair_num INTEGER, time_str TEXT, subject TEXT, lesson_type TEXT, teacher TEXT, room TEXT,
            week_num INTEGER, week_date TEXT
        );
    """)
    cursor.execute("DELETE FROM lessons;")
    cursor.executemany("""
        INSERT INTO lessons (group_name, day_num, day_name, pair_num, time_str, subject, lesson_type, teacher, room, week_num, week_date)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, all_lessons)
    cursor.execute("COMMIT;")
    conn.close()
    print("🎉 База schedule.db успешно сформирована!")

if __name__ == "__main__":
    main()
