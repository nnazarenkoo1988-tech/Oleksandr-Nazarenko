import os, json, re, hashlib, subprocess
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

TARGET_URL = os.getenv("TARGET_URL")
USER = os.getenv("CABINWEB_USER", "")
PASS = os.getenv("CABINWEB_PASS", "")
SSO  = os.getenv("CABINWEB_SSO","true").lower() in ("1","true","yes","on")
BOT  = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "state.json"

def notify(msg: str):
    print(msg)
    if BOT and CHAT:
        try:
            requests.post(f"https://api.telegram.org/bot{BOT}/sendMessage",
                          json={"chat_id": CHAT, "text": msg}, timeout=20)
        except Exception as e:
            print("[Notify error]", e)

def load_state():
    try:
        return json.load(open(STATE_FILE, "r", encoding="utf-8"))
    except FileNotFoundError:
        return {"hash": None, "months": [], "available_days": {}}

def save_state(state):
    json.dump(state, open(STATE_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

def git_commit_and_push(msg="update state"):
    try:
        subprocess.check_call(["git","config","user.email","bot@users.noreply.github.com"])
        subprocess.check_call(["git","config","user.name","monitor-bot"])
        subprocess.check_call(["git","add", STATE_FILE])
        subprocess.check_call(["git","commit","-m", msg])
        subprocess.check_call(["git","push"])
    except subprocess.CalledProcessError:
        pass

def login_flow(page):
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    # Якщо вже залогінені і бачимо головну CabinWeb — ок
    try:
        page.get_by_text(re.compile(r"Velkommen til CabinWeb", re.I)).wait_for(timeout=3000)
        return
    except PWTimeoutError:
        pass

    # Стартова форма CabinWeb (з чекбоксом "bruk SSO")
    try:
        user_input = page.get_by_placeholder(re.compile(r"Epost|Brukernavn", re.I))
        if user_input:
            user_input.fill(USER)
        # Натискаємо "Logg inn"
        page.get_by_role("button", name=re.compile(r"Logg inn", re.I)).click()
    except Exception:
        pass

    # SSO (типові поля Microsoft/Google/Azure)
    try:
        # email
        email_box = page.locator("#i0116, input[type='email']").first
        if email_box.is_visible():
            email_box.fill(USER)
            page.locator("#idSIButton9, input[type='submit'], button[type='submit']").first.click()
        # пароль (якщо раптом потрібен)
        pwd_box = page.locator("#i0118, input[type='password']").first
        if pwd_box.is_visible() and not SSO:
            pwd_box.fill(PASS)
            page.locator("#idSIButton9, input[type='submit'], button[type='submit']").first.click()
        # stay signed in
        stay = page.locator("#idSIButton9").first
        if stay.is_visible():
            stay.click()
    except Exception:
        pass

    page.wait_for_load_state("networkidle")

def navigate_to_calendar(page):
    """Fortsett → NORBIT-hytta → KALENDER"""
    # 1) Fortsett
    try:
        page.get_by_role("button", name=re.compile(r"Fortsett", re.I)).click(timeout=5000)
    except Exception:
        pass

    page.wait_for_load_state("networkidle")

    # 2) Знайти картку NORBIT-hytta і перейти всередину
    try:
        # клік за назвою об'єкта (посилання або картка)
        page.get_by_text(re.compile(r"^\s*NORBIT[-\s]?hytta\s*$", re.I), exact=False).first.click(timeout=8000)
    except Exception:
        # якщо клік по назві не спрацював — спробуємо кнопку-стрілку на картці
        try:
            card = page.get_by_text(re.compile(r"NORBIT[-\s]?hytta", re.I)).first
            card.scroll_into_view_if_needed(timeout=3000)
            # іноді є кнопка з іконкою ">" праворуч
            page.get_by_role("button").filter(has_text=re.compile(r"^$")).nth(0).click(timeout=3000)
        except Exception:
            pass

    page.wait_for_load_state("networkidle")

    # 3) На сторінці об'єкта натиснути "KALENDER"
    try:
        page.get_by_role("button", name=re.compile(r"Kalender", re.I)).click(timeout=8000)
    except Exception:
        # інколи це не button, а посилання/акордеон
        try:
            page.get_by_text(re.compile(r"^\s*KALENDER\s*$", re.I)).first.click(timeout=8000)
        except Exception:
            pass

    page.wait_for_load_state("networkidle")

def grab_calendar_and_parse(page):
    # Чекаємо, поки з'явиться будь-який календарний елемент
    try:
        # часті випадки: таблиця днів або div із словом "Kalender"
        page.get_by_text(re.compile(r"Kalender", re.I)).wait_for(timeout=5000)
    except PWTimeoutError:
        pass

    html = page.content()

    months = []
    available_days = {}
    try:
        # зчитуємо заголовки місяців (норв/англ)
        month_pat = r"(January|February|March|April|May|June|July|August|September|October|November|December|Januar|Februar|Mars|April|Mai|Juni|Juli|August|September|Oktober|November|Desember)\s+\d{4}"
        # беремо до 6 місяців з екрана
        for _ in range(6):
            loc = page.get_by_text(re.compile(month_pat))
            if loc.count() == 0:
                break
            for i in range(min(loc.count(), 6)):
                t = loc.nth(i).inner_text().strip()
                if t not in months:
                    months.append(t)

        # доступні дні (евристика)
        day_cells = page.locator("button, td, div").filter(has_text=re.compile(r"^\d{1,2}$"))
        avail = []
        for i in range(min(day_cells.count(), 2000)):
            el = day_cells.nth(i)
            try:
                txt = el.inner_text().strip()
                if not txt.isdigit(): 
                    continue
                cls = (el.get_attribute("class") or "").lower()
                aria_dis = (el.get_attribute("aria-disabled") or "").lower()
                disabled = "disabled" in cls or aria_dis in ("true","1")
                if (not disabled) and el.is_visible() and el.is_enabled():
                    avail.append(int(txt))
            except Exception:
                pass
        if months:
            available_days[months[0]] = sorted(set(avail))
    except Exception as e:
        print("[Parse warn]", e)

    h = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return h, months, available_days

def run():
    state = load_state()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        login_flow(page)
        navigate_to_calendar(page)

        h, months, available_days = grab_calendar_and_parse(page)
        browser.close()

    first_run = state.get("hash") is None
    dom_changed = (state.get("hash") != h)
    months_changed = (state.get("months") != months)
    avail_changed = (state.get("available_days") != available_days)

    if first_run:
        state.update({"hash": h, "months": months, "available_days": available_days, "ts": datetime.utcnow().isoformat()+"Z"})
        save_state(state)
        git_commit_and_push("baseline state")
        print("Baseline saved. No notification on first run.")
        return

    if months_changed or avail_changed or dom_changed:
        msgs = []
        if months_changed:
            msgs.append(f"📅 Місяці змінились:\nБуло: {state.get('months')}\nСтало: {months}")
        if avail_changed:
            msgs.append(f"🗓️ Доступні дні змінились (перший видимий місяць): {available_days}")
        if dom_changed and not (months_changed or avail_changed):
            msgs.append("♻️ Календар оновив DOM.")
        notify("\n\n".join(msgs) + f"\n\nПеревір: {TARGET_URL}")

        state.update({"hash": h, "months": months, "available_days": available_days, "ts": datetime.utcnow().isoformat()+"Z"})
        save_state(state)
        git_commit_and_push("state changed")
    else:
        print("Без змін")

if __name__ == "__main__":
    run()
