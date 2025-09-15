import os, json, re, hashlib, subprocess, sys, time
from datetime import datetime
import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

TARGET_URL = os.getenv("TARGET_URL")
USER = os.getenv("CABINWEB_USER")
PASS = os.getenv("CABINWEB_PASS")
SSO = (os.getenv("CABINWEB_SSO","true").lower() in ("1","true","yes","on"))
BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "state.json"

assert TARGET_URL and USER and PASS, "Missing TARGET_URL / CABINWEB_USER / CABINWEB_PASS"

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
    except subprocess.CalledProcessError as e:
        print("[Git] nothing to commit or push failed:", e)

def login_flow(page):
    """Пробує три варіанти входу: локальна форма; SSO (Azure AD); редіректи."""
    page.goto(TARGET_URL, wait_until="domcontentloaded")

    # Якщо нас одразу пустило (сесія вже є) — добре
    try:
        page.wait_for_selector("#calendar, [id*='calendar'], text=/Kalender|Calendar/i", timeout=3000)
        return
    except PWTimeoutError:
        pass

    # 1) Сторінка CabinWeb з формою ("Epost/Brukernavn", "Logg inn")
    try:
        # Поле користувача за плейсхолдером
        user_input = page.get_by_placeholder(re.compile(r"Epost|Brukernavn", re.I))
        if user_input:
            user_input.fill(USER)
        # Якщо є чекбокс SSO — залишаємо як є; керуємо через секрет CABINWEB_SSO
        if not SSO:
            # спроба зняти "bruk SSO"
            try:
                sso_box = page.get_by_label(re.compile(r"SSO", re.I))
                if sso_box.is_checked():
                    sso_box.click()
            except Exception:
                pass

        # Якщо вже є поле пароля на цій сторінці — заповнюємо і входимо
        pwd_candidates = page.locator("input[type='password']")
        if pwd_candidates.count() > 0 and not SSO:
            pwd_candidates.first.fill(PASS)
            # Кнопка Logg inn
            page.get_by_role("button", name=re.compile(r"Logg inn", re.I)).click()
        else:
            # Інакше тиснемо Logg inn і чекаємо на SSO-сторінку
            page.get_by_role("button", name=re.compile(r"Logg inn", re.I)).click()
    except Exception:
        pass

    # 2) Azure AD / SSO (типові селектори)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        # Етап 1: e-mail
        try:
            email_box = page.locator("#i0116, input[type='email']").first
            if email_box.is_visible():
                email_box.fill(USER)
                page.locator("#idSIButton9, input[type='submit'], button[type='submit']").first.click()
        except Exception:
            pass

        # Етап 2: пароль
        try:
            page.wait_for_timeout(500)
            pwd_box = page.locator("#i0118, input[type='password']").first
            if pwd_box.is_visible():
                pwd_box.fill(PASS)
                page.locator("#idSIButton9, input[type='submit'], button[type='submit']").first.click()
        except Exception:
            pass

        # "Stay signed in?" — Yes
        try:
            page.wait_for_timeout(500)
            stay = page.locator("#idSIButton9").first
            if stay.is_visible():
                stay.click()
        except Exception:
            pass
    except Exception:
        pass

    # Очікуємо на календар
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)
    # якщо календар в iframe — контент все одно змінимо через page.content()
    return

def grab_calendar_and_parse(page):
    html = page.content()
    # Спроба знайти блок календаря, якщо є
    try:
        cal = page.locator("#calendar, [id*='calendar']").first
        if cal.count() > 0 and cal.is_visible():
            html = cal.inner_html(timeout=5000)
    except Exception:
        pass

    months = []
    available_days = {}

    try:
        # заголовки місяців (норвеж./англ.)
        month_pat = r"^(January|February|March|April|May|June|July|August|September|October|November|December|Januar|Februar|Mars|April|Mai|Juni|Juli|August|September|Oktober|November|Desember)\s+\d{4}$"
        month_headers = page.locator(f"text=/{month_pat}/")
        for i in range(month_headers.count()):
            txt = month_headers.nth(i).inner_text().strip()
            months.append(txt)

        # доступні дні (евристика)
        day_cells = page.locator("button, td, div").filter(has_text=re.compile(r"^\d{1,2}$"))
        avail = []
        n = min(day_cells.count(), 2000)
        for i in range(n):
            el = day_cells.nth(i)
            try:
                txt = el.inner_text().strip()
                if not txt.isdigit(): continue
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

        # відкритий календар/сторінка
        page.goto(TARGET_URL, wait_until="networkidle")
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
            msgs.append("♻️ Календар оновив DOM (можливо відкрився новий місяць або косметичні зміни).")
        notify("\n\n".join(msgs) + f"\n\nПеревір: {TARGET_URL}")

        state.update({"hash": h, "months": months, "available_days": available_days, "ts": datetime.utcnow().isoformat()+"Z"})
        save_state(state)
        git_commit_and_push("state changed")
    else:
        print("Без змін")

if __name__ == "__main__":
    run()
