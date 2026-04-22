"""
DrissionPage 浏览器自动化 ChatGPT 注册引擎
参考 Chrome 扩展 10 步流程 + chatgpt_register.py 浏览器启动方式
仅执行注册流程（Step 1-5 + 获取 Session），不含 OAuth 登录
"""

import json
import os
import random
import secrets
import string
import time
from datetime import datetime
from typing import Optional

from DrissionPage import ChromiumOptions, ChromiumPage

# ── 常量 ──────────────────────────────────────────────────────────
CHATGPT_URL = "https://chatgpt.com/"
AUTH_URLS = {
    "password": "https://auth.openai.com/create-account/password",
    "email_verification": "https://auth.openai.com/email-verification",
    "about_you": "https://auth.openai.com/about-you",
}
WAIT_TIMEOUT = 30
CODE_TIMEOUT = 90
POLL_INTERVAL = 3
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "Results_ChatGPT")


def _log(tag: str, msg: str, level: str = "INFO"):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] [{level}] [{tag}] {msg}")


# ── 工具函数 ──────────────────────────────────────────────────────

def generate_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%"),
    ]
    pwd += random.choices(chars, k=length - 4)
    random.shuffle(pwd)
    return "".join(pwd)


def generate_name() -> dict:
    first_names = ["James", "Emma", "Liam", "Olivia", "Noah", "Ava", "William", "Sophia", "Lucas", "Mia"]
    last_names = ["Smith", "Johnson", "Brown", "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson"]
    return {"first": random.choice(first_names), "last": random.choice(last_names)}

def generate_birthday() -> dict:
    year = random.randint(1985, 2000)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {"year": year, "month": month, "day": day}


def _is_browser_closed(page) -> bool:
    if page is None:
        return True
    try:
        _ = page.url
        return False
    except Exception:
        return True


def _wait_for_url(page, url_part: str, timeout: int = 15) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _is_browser_closed(page):
            return False
        if url_part in page.url:
            return True
        time.sleep(0.5)
    return False


# ── 浏览器创建 ────────────────────────────────────────────────────

def create_browser(proxy: str = "", headless: bool = False) -> Optional[ChromiumPage]:
    try:
        co = ChromiumOptions()
        co.auto_port()
        co.new_env()
        co.incognito()

        if headless:
            co.headless()

        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--disable-dev-shm-usage")
        co.set_argument("--no-sandbox")
        co.set_argument("--disable-gpu")
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--lang=en-US")
        co.set_argument("--window-size=1920,1080")

        if proxy:
            co.set_proxy(proxy)

        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        co.set_user_agent(ua)

        page = ChromiumPage(addr_or_opts=co)

        # 注入反检测
        page.run_js("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
            Object.defineProperty(navigator, 'language', { get: () => 'en-US' });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)

        _log("Browser", f"浏览器已创建 (proxy={proxy or 'none'}, headless={headless})")
        return page
    except Exception as e:
        _log("Browser", f"创建浏览器失败: {e}", "ERROR")
        return None


# ── CF Worker 邮箱验证码获取 ──────────────────────────────────────

def _cfworker_get_code(
    api_url: str,
    admin_token: str,
    email: str,
    custom_auth: str = "",
    timeout: int = CODE_TIMEOUT,
    exclude_codes: set = None,
) -> Optional[str]:
    """从 CF Worker 轮询获取验证码"""
    import re
    import requests

    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-admin-auth": admin_token,
    }
    if custom_auth:
        headers["x-custom-auth"] = custom_auth

    exclude = exclude_codes or set()
    start = time.time()

    while time.time() - start < timeout:
        try:
            resp = requests.get(
                f"{api_url}/admin/mails",
                params={"limit": 20, "offset": 0, "address": email},
                headers=headers,
                timeout=10,
            )
            if resp.status_code != 200:
                time.sleep(POLL_INTERVAL)
                continue

            data = resp.json()
            mails = data.get("results", data) if isinstance(data, dict) else data
            if not isinstance(mails, list):
                time.sleep(POLL_INTERVAL)
                continue

            for mail in sorted(mails, key=lambda x: x.get("id", 0), reverse=True):
                subject = str(mail.get("subject", ""))
                text = str(mail.get("text", "") or mail.get("raw", ""))
                source = f"{subject} {text}"

                codes = re.findall(r"\b(\d{6})\b", source)
                for code in codes:
                    if code not in exclude:
                        return code
        except Exception as e:
            _log("Mail", f"轮询异常: {e}", "WARN")

        time.sleep(POLL_INTERVAL)

    return None


# ── Outlook Graph API 验证码获取 ──────────────────────────────────

def _outlook_get_access_token(client_id: str, refresh_token: str) -> str:
    """用 refresh_token 换取 Graph API access_token"""
    import requests

    endpoints = [
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "https://login.live.com/oauth20_token.srf",
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    ]

    for endpoint in endpoints:
        try:
            resp = requests.post(
                endpoint,
                data={
                    "client_id": client_id,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": "https://graph.microsoft.com/.default",
                },
                timeout=20,
            )
            if resp.status_code >= 400:
                continue
            data = resp.json()
            token = data.get("access_token", "")
            if token:
                return token
        except Exception:
            continue
    return ""


def _outlook_get_code(
    client_id: str,
    refresh_token: str,
    email: str,
    timeout: int = CODE_TIMEOUT,
    exclude_codes: set = None,
) -> Optional[str]:
    """从 Outlook Graph API 轮询获取 OpenAI 验证码"""
    import re
    import requests

    exclude = exclude_codes or set()
    access_token = _outlook_get_access_token(client_id, refresh_token)
    if not access_token:
        _log("Outlook", "获取 Graph API access_token 失败", "ERROR")
        return None

    # OpenAI 验证码邮件的发件人和主题特征
    OPENAI_SENDERS = {"noreply@tm.openai.com", "noreply@openai.com", "noreply@email.openai.com"}
    OPENAI_SUBJECT_KEYWORDS = {"openai", "chatgpt", "verify", "verification", "code", "验证"}

    start = time.time()
    seen_ids = set()

    while time.time() - start < timeout:
        try:
            for folder in ["inbox", "junkemail"]:
                resp = requests.get(
                    f"https://graph.microsoft.com/v1.0/me/mailFolders/{folder}/messages",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={
                        "$top": 10,
                        "$orderby": "receivedDateTime desc",
                        "$select": "id,subject,from,bodyPreview,body,receivedDateTime",
                    },
                    timeout=15,
                )
                if resp.status_code != 200:
                    continue

                messages = resp.json().get("value", [])
                for m in messages:
                    mid = m.get("id", "")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    # 过滤发件人：只看 OpenAI 的邮件
                    sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "").lower()
                    subject = m.get("subject", "")
                    subject_lower = subject.lower()

                    is_openai = sender in OPENAI_SENDERS or any(kw in subject_lower for kw in OPENAI_SUBJECT_KEYWORDS)
                    if not is_openai:
                        continue

                    preview = m.get("bodyPreview", "")
                    body = (m.get("body") or {}).get("content", "")
                    text = f"{subject} {preview} {body}"

                    codes = re.findall(r"\b(\d{6})\b", text)
                    for code in codes:
                        if code not in exclude:
                            _log("Outlook", f"收到验证码: {code}")
                            return code
        except Exception as e:
            _log("Outlook", f"轮询异常: {e}", "WARN")

        time.sleep(POLL_INTERVAL)

    return None


# ── 注册流程 ──────────────────────────────────────────────────────

def do_register(
    page: ChromiumPage,
    email: str,
    password: str,
    mail_config: dict,
) -> dict:
    """
    DrissionPage 10 步注册流程（仅执行 Step 1-5 + 获取 Session）

    mail_config: {
        "provider": "cfworker" | "outlook",
        # CF Worker:
        "api_url": str,
        "admin_token": str,
        "custom_auth": str,
        # Outlook:
        "client_id": str,
        "refresh_token": str,
    }
    """
    name = generate_name()
    birthday = generate_birthday()
    full_name = f"{name['first']} {name['last']}"

    try:
        # ── Step 1: 打开 ChatGPT ──
        _log("Step1", "打开 ChatGPT 官网...")
        page.get(CHATGPT_URL, timeout=WAIT_TIMEOUT)
        time.sleep(4)

        if _is_browser_closed(page):
            return {"success": False, "error": "浏览器已关闭"}

        _log("Step1", f"页面已加载: {page.url}")

        # ── Step 2: 点击注册 → 填写邮箱 ──
        _log("Step2", f"填写邮箱: {email}")

        # 查找注册入口
        if "auth.openai.com" not in page.url:
            signup_btn = None
            for label in ["Sign up", "免费注册", "注册", "Get started"]:
                try:
                    btn = page.ele(f"text:{label}", timeout=3)
                    if btn:
                        signup_btn = btn
                        break
                except Exception:
                    continue

            if not signup_btn:
                try:
                    signup_btn = page.ele("@data-testid=login-button", timeout=3)
                except Exception:
                    pass

            if signup_btn:
                signup_btn.click()
                time.sleep(3)
            else:
                _log("Step2", "未找到注册按钮", "WARN")

        # 填写邮箱
        email_input = None
        for sel in ['css:input[type="email"]', 'css:input[name="email"]', 'css:input[name="username"]']:
            try:
                inp = page.ele(sel, timeout=5)
                if inp:
                    email_input = inp
                    break
            except Exception:
                continue

        if not email_input:
            return {"success": False, "error": "未找到邮箱输入框"}

        email_input.click()
        time.sleep(0.2)
        email_input.clear()
        email_input.input(email, clear=False)
        time.sleep(0.3)

        # 点击继续
        page.run_js("""
            const formBtn = document.querySelector('form button[type="submit"]');
            if (formBtn) { formBtn.click(); return true; }
            const popupBtn = document.querySelector('[id^="radix-"] > div > div > div > form > button');
            if (popupBtn) { popupBtn.click(); return true; }
            return false;
        """)
        time.sleep(3)
        _log("Step2", "邮箱已提交")

        # ── Step 3: 填写密码 ──
        _log("Step3", "填写密码...")
        if not _wait_for_url(page, "/create-account/password", timeout=12):
            # 可能已经在密码页
            if "/create-account/password" not in page.url:
                _log("Step3", f"未进入密码页, 当前: {page.url}", "WARN")

        pwd_input = None
        for sel in ['css:input[type="password"]']:
            try:
                inp = page.ele(sel, timeout=8)
                if inp:
                    pwd_input = inp
                    break
            except Exception:
                continue

        if not pwd_input:
            # 可能需要先点击"使用密码继续"链接
            try:
                pwd_link = page.ele('css:a[href="/log-in/password"]', timeout=3) or page.ele('text:使用密码继续', timeout=2)
                if pwd_link:
                    pwd_link.click()
                    time.sleep(2)
                    pwd_input = page.ele('css:input[type="password"]', timeout=5)
            except Exception:
                pass

        if not pwd_input:
            # 检查是否有错误提示
            page_text = (page.html or "").lower()
            if "already exists" in page_text or "已存在" in page_text:
                return {"success": False, "error": "该邮箱已注册"}
            return {"success": False, "error": "未找到密码输入框"}

        page.run_js('arguments[0].scrollIntoView({behavior:"instant",block:"center"})', pwd_input)
        time.sleep(0.3)
        page.run_js('arguments[0].click(); arguments[0].focus()', pwd_input)
        time.sleep(0.2)
        pwd_input.clear()
        pwd_input.input(password, clear=False)
        page.run_js('arguments[0].dispatchEvent(new Event("blur", {bubbles: true}))', pwd_input)
        time.sleep(0.5)

        # 提交密码 - 多种方式尝试
        submitted = page.run_js("""
            const btn = document.querySelector('button[type="submit"]');
            if (btn) { btn.click(); return 'clicked'; }
            const form = document.querySelector('form');
            if (form) { form.submit(); return 'form_submit'; }
            return false;
        """)
        _log("Step3", f"密码提交方式: {submitted}")
        time.sleep(3)

        # 密码提交后可能出现超时错误，需要重试
        for retry in range(3):
            if "/create-account/password" not in page.url:
                break

            # 检查是否是超时错误页
            is_timeout = page.run_js("""
                const text = document.body?.innerText || '';
                return /timed\\s*out|糟糕|出错了|something\\s+went\\s+wrong/i.test(text);
            """)

            if is_timeout:
                _log("Step3", f"检测到超时错误，点击重试 (第{retry+1}次)...", "WARN")
                clicked_retry = page.run_js("""
                    const btns = document.querySelectorAll('button, [role="button"]');
                    for (const b of btns) {
                        const t = (b.textContent || '').trim();
                        if (/重试|try\\s*again/i.test(t)) { b.click(); return true; }
                    }
                    return false;
                """)
                if clicked_retry:
                    time.sleep(3)
                    # 重试后可能需要重新填写密码并提交
                    if "/create-account/password" in page.url:
                        try:
                            pwd_input2 = page.ele('css:input[type="password"]', timeout=5)
                            if pwd_input2:
                                pwd_input2.click()
                                pwd_input2.clear()
                                pwd_input2.input(password, clear=False)
                                time.sleep(0.5)
                                page.run_js("""
                                    const btn = document.querySelector('button[type="submit"]');
                                    if (btn && !btn.disabled) btn.click();
                                """)
                                time.sleep(4)
                        except Exception:
                            pass
                continue

            # 检查邮箱已存在
            page_text = (page.html or "").lower()
            if "already exists" in page_text or "已存在" in page_text:
                return {"success": False, "error": "该邮箱已注册"}

            # 其他错误 - 诊断
            diag = page.run_js("""
                const errors = [];
                document.querySelectorAll('[class*="error"], [role="alert"], .react-aria-FieldError').forEach(el => {
                    const t = (el.textContent || '').trim();
                    if (t) errors.push(t);
                });
                return {
                    url: location.href,
                    errors: errors,
                    bodyPreview: (document.body?.innerText || '').substring(0, 300),
                };
            """)
            _log("Step3", f"密码页诊断: {json.dumps(diag, ensure_ascii=False)[:300]}", "WARN")
            # 尝试再次点击提交
            page.run_js("""
                const btn = document.querySelector('button[type="submit"]');
                if (btn && !btn.disabled) btn.click();
            """)
            time.sleep(4)

        _log("Step3", f"密码提交后: {page.url}")

        # ── Step 4: 获取注册验证码 ──
        _log("Step4", "等待验证码页面...")

        # 检查是否跳转到登录页（邮箱已注册过）
        if "/log-in" in page.url:
            return {"success": False, "error": "该邮箱已注册过 ChatGPT，跳转到了登录页"}

        # 如果仍在密码页，可能需要更多时间
        if "/create-account/password" in page.url:
            _log("Step4", "仍在密码页，等待跳转...", "WARN")
            _wait_for_url(page, "/email-verification", timeout=15)

        # 再次检查登录页
        if "/log-in" in page.url:
            return {"success": False, "error": "该邮箱已注册过 ChatGPT，跳转到了登录页"}

        reached_verify = "/email-verification" in page.url

        if _is_browser_closed(page):
            return {"success": False, "error": "浏览器已关闭"}

        # 检查是否已自动跳过验证
        if AUTH_URLS["about_you"] in page.url:
            _log("Step4", "验证码已自动通过")
        elif not reached_verify:
            # 检查是否有邮箱已存在的错误
            page_text = page.html or ""
            if "already exists" in page_text.lower() or "已存在" in page_text:
                return {"success": False, "error": "该邮箱已注册"}
            return {"success": False, "error": f"未进入验证码页面, 当前: {page.url}"}
        else:
            # 获取验证码
            provider = mail_config.get("provider", "cfworker")
            _log("Step4", f"从 {provider} 轮询验证码...")
            used_codes = set()
            code_verified = False
            start_code = time.time()

            while time.time() - start_code < CODE_TIMEOUT:
                if _is_browser_closed(page):
                    return {"success": False, "error": "浏览器已关闭"}

                if provider == "outlook":
                    code = _outlook_get_code(
                        client_id=mail_config.get("client_id", ""),
                        refresh_token=mail_config.get("refresh_token", ""),
                        email=email,
                        timeout=15,
                        exclude_codes=used_codes,
                    )
                else:
                    code = _cfworker_get_code(
                        api_url=mail_config.get("api_url", ""),
                        admin_token=mail_config.get("admin_token", ""),
                        email=email,
                        custom_auth=mail_config.get("custom_auth", ""),
                        timeout=15,
                        exclude_codes=used_codes,
                    )

                if not code:
                    continue

                _log("Step4", f"获取到验证码: {code}")
                used_codes.add(code)

                # 填写验证码
                filled = _fill_verification_code(page, code)
                if not filled:
                    _log("Step4", "验证码填写失败", "WARN")
                    time.sleep(2)
                    continue

                # 点击提交
                try:
                    submit = page.ele('css:button[type="submit"]', timeout=3)
                    if submit:
                        submit.click()
                except Exception:
                    pass

                time.sleep(3)

                if AUTH_URLS["about_you"] in page.url:
                    code_verified = True
                    break

                # 检查验证码错误
                page_text = (page.html or "").lower()
                if "incorrect" in page_text or "错误" in page_text:
                    _log("Step4", "验证码不正确，继续等待新码...", "WARN")
                    continue

                if AUTH_URLS["about_you"] in page.url:
                    code_verified = True
                    break

            if not code_verified and AUTH_URLS["about_you"] not in page.url:
                return {"success": False, "error": "验证码超时"}

            _log("Step4", "验证码已通过")

        # ── Step 5: 填写姓名和生日 ──
        _log("Step5", f"填写资料: {full_name}")
        if not _wait_for_url(page, "/about-you", timeout=12):
            if "/about-you" not in page.url:
                _log("Step5", f"未进入资料页, 当前: {page.url}", "WARN")

        # 填写姓名
        name_input = None
        for sel in ['css:input[name="name"]', 'css:input[autocomplete="name"]']:
            try:
                inp = page.ele(sel, timeout=5)
                if inp:
                    name_input = inp
                    break
            except Exception:
                continue

        if name_input:
            name_input.click()
            name_input.clear()
            name_input.input(full_name, clear=False)
            time.sleep(0.3)

        # 填写生日 (spinbutton 方式)
        year = str(birthday["year"])
        month = str(birthday["month"]).zfill(2)
        day = str(birthday["day"]).zfill(2)

        fill_birthday_js = f"""
        (async function() {{
            const sleep = (ms) => new Promise(r => setTimeout(r, ms));
            const dateField = document.querySelector('div[role="group"][id*="birthday"]');
            if (!dateField) {{
                // 尝试年龄输入
                const ageInput = document.querySelector('input[name="age"]');
                if (ageInput) {{
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeInputValueSetter.call(ageInput, '{2025 - birthday["year"]}');
                    ageInput.dispatchEvent(new Event('input', {{bubbles: true}}));
                    ageInput.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return 'age';
                }}
                // 尝试 React Aria Select
                const selects = document.querySelectorAll('.react-aria-Select');
                if (selects.length >= 3) {{
                    const setSelect = (root, value) => {{
                        const container = root.closest('[class*="selectItem"]') || root.parentElement;
                        const select = container?.querySelector('[data-testid="hidden-select-container"] select');
                        if (select) {{
                            select.value = value;
                            Array.from(select.options).forEach(o => o.selected = (o.value === value));
                            select.dispatchEvent(new Event('input', {{bubbles: true}}));
                            select.dispatchEvent(new Event('change', {{bubbles: true}}));
                        }}
                    }};
                    setSelect(selects[0], '{year}');
                    await sleep(200);
                    setSelect(selects[1], '{birthday["month"]}');
                    await sleep(200);
                    setSelect(selects[2], '{birthday["day"]}');
                    return 'select';
                }}
                return false;
            }}

            const fillSpinbutton = async (segment, valueStr) => {{
                if (!segment) return;
                segment.focus();
                segment.click();
                await sleep(100);
                for (const char of valueStr) {{
                    segment.dispatchEvent(new KeyboardEvent('keydown', {{key: char, code: 'Digit'+char, bubbles: true}}));
                    segment.dispatchEvent(new InputEvent('beforeinput', {{inputType: 'insertText', data: char, bubbles: true}}));
                    segment.dispatchEvent(new InputEvent('input', {{inputType: 'insertText', data: char, bubbles: true}}));
                    await sleep(50);
                }}
                segment.dispatchEvent(new FocusEvent('blur', {{bubbles: true}}));
                await sleep(100);
            }};

            const yearSeg = dateField.querySelector('[role="spinbutton"][data-type="year"]');
            const monthSeg = dateField.querySelector('[role="spinbutton"][data-type="month"]');
            const daySeg = dateField.querySelector('[role="spinbutton"][data-type="day"]');
            await fillSpinbutton(yearSeg, '{year}');
            await sleep(150);
            await fillSpinbutton(monthSeg, '{month}');
            await sleep(150);
            await fillSpinbutton(daySeg, '{day}');
            return 'spinbutton';
        }})();
        """
        page.run_js(fill_birthday_js)
        time.sleep(1)

        # 勾选同意复选框（韩国 IP 等场景）
        page.run_js("""
            const cb = document.querySelector('input[name="allCheckboxes"][type="checkbox"]');
            if (cb && !cb.checked) {
                const label = cb.closest('label');
                if (label) label.click(); else cb.click();
            }
        """)
        time.sleep(0.5)

        # 点击完成
        try:
            submit_btn = page.ele('css:button[type="submit"]', timeout=5)
            if submit_btn:
                submit_btn.click()
        except Exception:
            pass

        time.sleep(3)
        _log("Step5", "资料已提交")

        # ── Step 6: 获取 Session ──
        _log("Step6", "等待进入已登录页面...")
        start_session = time.time()
        while time.time() - start_session < 30:
            if _is_browser_closed(page):
                return {"success": False, "error": "浏览器已关闭"}

            current_url = page.url
            if "chatgpt.com" in current_url and "auth.openai.com" not in current_url:
                if "/api/auth/callback/" in current_url:
                    time.sleep(5)

                session = _get_session(page)
                if session:
                    session["email"] = email
                    session["password"] = password
                    session["name"] = full_name
                    session["success"] = True
                    _log("Step6", "注册成功，已获取 Session")
                    return session

            time.sleep(1)

        # 即使没拿到 session，如果到了 chatgpt.com 也算注册成功
        if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
            session = _get_session(page) or {}
            session["email"] = email
            session["password"] = password
            session["name"] = full_name
            session["success"] = True
            session["warning"] = "Session 获取可能不完整"
            return session

        return {"success": False, "error": f"注册后未进入已登录页面, 当前: {page.url}"}

    except Exception as e:
        err = str(e)
        if _is_browser_closed(page):
            return {"success": False, "error": "浏览器已关闭"}
        _log("Register", f"异常: {e}", "ERROR")
        return {"success": False, "error": err}


def _fill_verification_code(page, code: str) -> bool:
    """填写 6 位验证码（兼容单输入框和分离输入框）"""
    try:
        # 尝试单输入框
        result = page.run_js(f"""
            const selectors = [
                'input[name="code"]',
                'input[autocomplete="one-time-code"]',
                'input[type="text"][maxlength="6"]',
                'input[inputmode="numeric"]',
            ];
            for (const sel of selectors) {{
                const input = document.querySelector(sel);
                if (input && input.offsetWidth > 0) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeSetter.call(input, '{code}');
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
            }}
            // 尝试分离的单字符输入框
            const singles = Array.from(document.querySelectorAll('input[maxlength="1"]'))
                .filter(el => el.offsetWidth > 0);
            if (singles.length >= 6) {{
                const code = '{code}';
                for (let i = 0; i < 6; i++) {{
                    const nativeSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                    nativeSetter.call(singles[i], code[i]);
                    singles[i].dispatchEvent(new Event('input', {{bubbles: true}}));
                    singles[i].dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
                return true;
            }}
            return false;
        """)
        return bool(result)
    except Exception:
        return False


def _get_session(page) -> Optional[dict]:
    """从浏览器获取 Session 信息（含完整 CDP cookies）"""
    try:
        cookies_kv = {}
        cdp_cookies_raw = []
        try:
            cdp_result = page.run_cdp("Network.getAllCookies")
            if cdp_result and cdp_result.get("cookies"):
                cdp_cookies_raw = cdp_result["cookies"]
                for c in cdp_cookies_raw:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    if name and value:
                        cookies_kv[name] = value
        except Exception:
            for c in page.cookies():
                name = c.get("name", "")
                value = c.get("value", "")
                if name and value:
                    cookies_kv[name] = value

        session_token = cookies_kv.get("__Secure-next-auth.session-token", "")
        access_token = ""

        # 尝试从 /api/auth/session 获取 access_token
        try:
            resp = page.run_js("""
                return fetch('/api/auth/session', {credentials: 'include'})
                    .then(r => r.json())
                    .catch(() => null);
            """)
            if resp and isinstance(resp, dict):
                access_token = resp.get("accessToken", "")
        except Exception:
            pass

        return {
            "session_token": session_token,
            "access_token": access_token,
            "cookies": cookies_kv,
            "cdp_cookies": cdp_cookies_raw,
        }
    except Exception:
        return None


def save_cookies(result: dict, output_path: str = "") -> dict:
    """
    保存注册结果的 cookies 到 JSON 文件

    result: do_register / register_chatgpt 的返回值
    output_path: 指定保存路径，留空则自动生成到 Results_ChatGPT/
    """
    if not result.get("success"):
        return {"success": False, "error": "注册未成功，无 cookies 可保存"}

    cookies = result.get("cookies", {})
    cdp_cookies = result.get("cdp_cookies", [])
    email = result.get("email", "unknown")

    if not cookies and not cdp_cookies:
        return {"success": False, "error": "cookies 为空"}

    try:
        if output_path:
            cookie_path = output_path
            if not os.path.isabs(cookie_path):
                cookie_path = os.path.join(os.getcwd(), cookie_path)
        else:
            os.makedirs(RESULTS_DIR, exist_ok=True)
            ts = int(time.time() * 1000)
            safe_email = email.replace("@", "_at_").replace(".", "_")
            cookie_path = os.path.join(RESULTS_DIR, f"cookie_{safe_email}_{ts}.json")

        payload = {
            "email": email,
            "password": result.get("password", ""),
            "name": result.get("name", ""),
            "session_token": result.get("session_token", ""),
            "access_token": result.get("access_token", ""),
            "cookies": cookies,
            "cdp_cookies": cdp_cookies,
            "saved_at": datetime.now().isoformat(),
            "source": "drission_register",
        }

        # 确保目标目录存在
        os.makedirs(os.path.dirname(cookie_path), exist_ok=True)

        with open(cookie_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        _log("Save", f"Cookies 已保存: {cookie_path}")
        return {"success": True, "path": cookie_path}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── 主入口 ────────────────────────────────────────────────────────

def register_chatgpt(
    email: str = "",
    password: str = "",
    proxy: str = "http://127.0.0.1:7890",
    headless: bool = False,
    cfworker_api_url: str = "",
    cfworker_admin_token: str = "",
    cfworker_custom_auth: str = "",
    cfworker_domain: str = "",
    save_cookie_path: str = "",
    mail_provider: str = "cfworker",
    outlook_client_id: str = "",
    outlook_refresh_token: str = "",
) -> dict:
    """
    一键注册 ChatGPT 账号

    mail_provider: "cfworker" 或 "outlook"
    cfworker_*: CF Worker 参数（mail_provider=cfworker 时使用）
    outlook_*: Outlook 参数（mail_provider=outlook 时使用）
    """
    import requests

    page = None
    try:
        # 根据 mail_provider 确定邮箱来源
        if mail_provider == "outlook":
            # Outlook: 邮箱由外部传入，必须提供 email
            if not email:
                return {"success": False, "error": "Outlook 模式需要提供邮箱地址"}
            _log("Main", f"Outlook 邮箱: {email}")

        elif not email:
            # CF Worker: 自动生成邮箱
            if not cfworker_api_url:
                return {"success": False, "error": "未提供邮箱且 CF Worker 未配置"}

            headers = {
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "x-admin-auth": cfworker_admin_token,
            }
            if cfworker_custom_auth:
                headers["x-custom-auth"] = cfworker_custom_auth

            name_part = "".join(random.choices(string.ascii_lowercase, k=6)) + "".join(random.choices(string.digits, k=4))
            payload = {"enablePrefix": True, "name": name_part}
            if cfworker_domain:
                payload["domain"] = cfworker_domain

            resp = requests.post(
                f"{cfworker_api_url.rstrip('/')}/admin/new_address",
                headers=headers,
                json=payload,
                timeout=15,
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"CF Worker 创建邮箱失败: HTTP {resp.status_code}"}

            data = resp.json()
            email = data.get("email", data.get("address", ""))
            if not email:
                return {"success": False, "error": "CF Worker 未返回邮箱地址"}

            _log("Main", f"已生成邮箱: {email}")

        if not password:
            password = generate_password()
            _log("Main", f"已生成密码: {password}")

        # 构建 mail_config
        if mail_provider == "outlook":
            mail_config = {
                "provider": "outlook",
                "client_id": outlook_client_id,
                "refresh_token": outlook_refresh_token,
            }
        else:
            mail_config = {
                "provider": "cfworker",
                "api_url": cfworker_api_url.rstrip("/") if cfworker_api_url else "",
                "admin_token": cfworker_admin_token,
                "custom_auth": cfworker_custom_auth,
            }

        # 创建浏览器
        page = create_browser(proxy=proxy, headless=headless)
        if not page:
            return {"success": False, "error": "浏览器创建失败"}

        # 执行注册
        result = do_register(
            page=page,
            email=email,
            password=password,
            mail_config=mail_config,
        )

        # 注册成功后自动保存 Cookies
        if result.get("success"):
            save_result = save_cookies(result, output_path=save_cookie_path)
            if save_result.get("success"):
                result["cookie_file"] = save_result["path"]
            else:
                result["cookie_save_error"] = save_result.get("error", "")

        return result

    finally:
        if page:
            try:
                page.quit(force=True)
            except Exception:
                pass


# ── CLI ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="DrissionPage ChatGPT 注册")
    parser.add_argument("--email", default="", help="注册邮箱（留空自动生成）")
    parser.add_argument("--password", default="", help="密码（留空自动生成）")
    parser.add_argument("--proxy", default="http://127.0.0.1:7890", help="代理")
    parser.add_argument("--headless", action="store_true", help="无头模式")
    parser.add_argument("--output", "-o", default="", help="Cookie 保存路径（留空自动生成）")
    parser.add_argument("--cfworker-api", default="", help="CF Worker API URL")
    parser.add_argument("--cfworker-token", default="", help="CF Worker Admin Token")
    parser.add_argument("--cfworker-auth", default="", help="CF Worker Custom Auth")
    parser.add_argument("--cfworker-domain", default="", help="CF Worker 域名")
    parser.add_argument("--mail-provider", default="cfworker", choices=["cfworker", "outlook"], help="邮箱服务")
    parser.add_argument("--outlook-client-id", default="", help="Outlook Client ID")
    parser.add_argument("--outlook-refresh-token", default="", help="Outlook Refresh Token")
    args = parser.parse_args()

    result = register_chatgpt(
        email=args.email,
        password=args.password,
        proxy=args.proxy,
        headless=args.headless,
        cfworker_api_url=args.cfworker_api,
        cfworker_admin_token=args.cfworker_token,
        cfworker_custom_auth=args.cfworker_auth,
        cfworker_domain=args.cfworker_domain,
        save_cookie_path=args.output,
        mail_provider=args.mail_provider,
        outlook_client_id=args.outlook_client_id,
        outlook_refresh_token=args.outlook_refresh_token,
    )

    print("\n" + "=" * 60)
    if result.get("success"):
        print("注册成功!")
        print(f"  邮箱: {result.get('email')}")
        print(f"  密码: {result.get('password')}")
        print(f"  姓名: {result.get('name')}")
        if result.get("session_token"):
            print(f"  Session Token: {result['session_token'][:50]}...")
        if result.get("access_token"):
            print(f"  Access Token: {result['access_token'][:50]}...")
        if result.get("cookie_file"):
            print(f"  Cookie 文件: {result['cookie_file']}")
    else:
        print(f"注册失败: {result.get('error')}")
    print("=" * 60)
