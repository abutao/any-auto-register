"""
ChatGPT 纯协议注册引擎（线性流程，无需手机号）

流程:
0. 访问 chatgpt.com → 获取 cookie
1. GET /api/auth/csrf → csrfToken
2. POST /api/auth/signin/openai → authorize URL
3. GET authorize → 跳转到 create-account/password
4. POST /api/accounts/user/register → 注册 (带 Sentinel token)
5. GET /api/accounts/email-otp/send → 发送验证码
6. POST /api/accounts/email-otp/validate → 验证码验证
7. POST /api/accounts/create_account → 提交姓名+生日
8. GET callback → 完成注册
9. 保存 Cookie JSON 文件

参考: gpt2api-clean/scripts/chatgpt_register.py
"""

import json
import os
import random
import secrets
import string
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

from curl_cffi import requests as curl_requests


# ── 指纹随机化 ────────────────────────────────────────────────

_CHROME_PROFILES = [
    {
        "major": 131, "impersonate": "chrome131",
        "full": "131.0.0.0",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    },
    {
        "major": 136, "impersonate": "chrome136",
        "full": "136.0.0.0",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    },
]


def _random_chrome():
    """返回随机 Chrome 指纹配置"""
    # 过滤掉不支持的 impersonate
    supported = []
    try:
        for p in _CHROME_PROFILES:
            try:
                s = curl_requests.Session(impersonate=p["impersonate"])
                s.close()
                supported.append(p)
            except Exception:
                pass
    except Exception:
        pass
    profile = random.choice(supported) if supported else _CHROME_PROFILES[0]
    ua = (
        f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{profile['full']} Safari/537.36"
    )
    return profile["impersonate"], ua, profile["sec_ch_ua"], profile["full"]


def _make_trace_headers():
    """生成 Datadog trace headers（反指纹）"""
    trace_id = random.randint(10**17, 10**18 - 1)
    parent_id = random.randint(10**17, 10**18 - 1)
    tp = f"00-{uuid.uuid4().hex}-{format(parent_id, '016x')}-01"
    return {
        "traceparent": tp, "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum", "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(trace_id), "x-datadog-parent-id": str(parent_id),
    }


def _random_name():
    first = random.choice([
        "James", "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Sophia",
        "Mason", "Isabella", "Lucas", "Mia", "Alexander", "Charlotte", "Benjamin",
    ])
    last = random.choice([
        "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
        "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore",
    ])
    return first, last


def _random_birthday():
    year = random.randint(1985, 2002)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year}-{month:02d}-{day:02d}"


def _delay(lo=0.3, hi=1.0):
    time.sleep(random.uniform(lo, hi))


# ── Sentinel Token PoW (完整 SDK 模拟) ────────────────────

import base64

class _SentinelGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id, user_agent):
        self.device_id = device_id
        self.user_agent = user_agent
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        h &= 0xFFFFFFFF
        return format(h, "08x")

    def _get_config(self):
        now_str = time.strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime())
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        nav_prop = random.choice([
            "vendorSub", "productSub", "vendor", "maxTouchPoints",
            "scheduling", "userActivation", "doNotTrack", "geolocation",
            "connection", "plugins", "mimeTypes", "pdfViewerEnabled",
            "webkitTemporaryStorage", "webkitPersistentStorage",
            "hardwareConcurrency", "cookieEnabled", "credentials",
            "mediaDevices", "permissions", "locks", "ink",
        ])
        return [
            "1920x1080", now_str, 4294705152, random.random(), self.user_agent,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, "en-US", "en-US,en", random.random(),
            f"{nav_prop}-undefined",
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            perf_now, self.sid, "", random.choice([4, 8, 12, 16]), time_origin,
        ]

    @staticmethod
    def _b64(data) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def generate_requirements_token(self) -> str:
        config = self._get_config()
        config[3] = 1
        config[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(config)

    def generate_pow_token(self, seed: str, difficulty: str) -> str:
        difficulty = str(difficulty or "0")
        start_time = time.time()
        config = self._get_config()
        for i in range(self.MAX_ATTEMPTS):
            config[3] = i
            config[9] = round((time.time() - start_time) * 1000)
            data = self._b64(config)
            hash_hex = self._fnv1a_32(seed + data)
            if hash_hex[:len(difficulty)] <= difficulty:
                return "gAAAAAB" + data + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def _fetch_sentinel_challenge(session, device_id, flow, ua, sec_ch_ua, impersonate):
    gen = _SentinelGenerator(device_id, ua)
    req_body = {"p": gen.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": ua,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    try:
        resp = session.post(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            data=json.dumps(req_body), headers=headers,
            impersonate=impersonate, timeout=20,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception:
        return None


def _build_sentinel_token(session, device_id, ua, sec_ch_ua, impersonate, flow="authorize_continue", log_fn=None):
    """构建完整 Sentinel Token (JSON 格式)"""
    challenge = _fetch_sentinel_challenge(session, device_id, flow, ua, sec_ch_ua, impersonate)
    if not challenge:
        return ""
    c_value = challenge.get("token", "")
    if not c_value:
        return ""
    gen = _SentinelGenerator(device_id, ua)
    pow_data = challenge.get("proofofwork") or {}
    if pow_data.get("required") and pow_data.get("seed"):
        p_value = gen.generate_pow_token(seed=pow_data["seed"], difficulty=pow_data.get("difficulty", "0"))
    else:
        p_value = gen.generate_requirements_token()
    return json.dumps({"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow}, separators=(",", ":"))


# ── Cookie 收集与保存 ─────────────────────────────────────────

def _collect_cookies(session) -> dict:
    cookie_dict = {}
    jar = getattr(getattr(session, "cookies", None), "jar", None)
    if jar:
        try:
            for c in list(jar):
                name = str(getattr(c, "name", "") or "").strip()
                value = str(getattr(c, "value", "") or "")
                if name:
                    cookie_dict[name] = value
        except Exception:
            pass
    if not cookie_dict:
        try:
            for name, value in session.cookies.items():
                if str(name or "").strip():
                    cookie_dict[str(name)] = str(value or "")
        except Exception:
            pass
    return cookie_dict


def _save_cookies(email: str, session, output_dir: str = "cookies") -> str:
    """保存 session cookies 到 JSON 文件"""
    cookie_dict = _collect_cookies(session)
    payload = {
        "email": str(email or "").strip(),
        "cookies": cookie_dict,
        "payment_cookies": cookie_dict,
        "saved_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
    }

    os.makedirs(output_dir, exist_ok=True)
    safe_name = email.replace("@", "_at_").replace(".", "_")
    file_path = os.path.join(output_dir, f"{safe_name}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return os.path.abspath(file_path)


# ── 注册引擎 ─────────────────────────────────────────────────

class ChatGPTProtocolRegister:
    """ChatGPT 纯协议注册（线性流程，无需手机号）"""

    BASE = "https://chatgpt.com"
    AUTH = "https://auth.openai.com"

    def __init__(
        self,
        proxy: Optional[str] = None,
        log_fn: Callable = print,
        cookie_dir: str = "cookies",
        browser_bootstrap: bool = True,
    ):
        self.proxy = proxy
        self.log = log_fn
        self.cookie_dir = cookie_dir
        self._browser_bootstrap = browser_bootstrap

        # 随机指纹
        self.impersonate, self.ua, self.sec_ch_ua, self.chrome_full = _random_chrome()
        self.device_id = str(uuid.uuid4())
        self.auth_session_logging_id = str(uuid.uuid4())
        self._callback_url = None

        # Session
        self.session = curl_requests.Session(impersonate=self.impersonate)
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}

        self.session.headers.update({
            "User-Agent": self.ua,
            "Accept-Language": random.choice([
                "en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8",
            ]),
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        self.session.cookies.set("oai-did", self.device_id, domain="chatgpt.com")

    # ── 浏览器预热：获取 cf_clearance ──

    def _bootstrap_clearance(self):
        """用 DrissionPage 快速访问 auth.openai.com 拿到 cf_clearance，注入到 curl_cffi session"""
        self.log("[Bootstrap] 启动浏览器获取 Cloudflare clearance...")
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage

            co = ChromiumOptions()
            co.auto_port()
            co.new_env()
            co.incognito()
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-sandbox")
            co.set_argument("--disable-gpu")
            co.set_argument("--lang=en-US")
            co.set_argument("--window-size=1920,1080")
            co.set_user_agent(self.ua)
            if self.proxy:
                co.set_proxy(self.proxy)

            page = ChromiumPage(addr_or_opts=co)
            try:
                page.run_js('Object.defineProperty(navigator,"webdriver",{get:()=>undefined});window.chrome={runtime:{}};')

                # 访问 chatgpt.com 获取初始 cookies
                page.get("https://chatgpt.com/", timeout=30)
                time.sleep(4)

                # 访问 auth.openai.com 获取 cf_clearance
                page.get(f"{self.AUTH}/log-in", timeout=20)

                # 等待 cf_clearance 出现（Cloudflare Challenge 需要几秒）
                has_clearance = False
                for _ in range(15):
                    time.sleep(1)
                    cdp_check = page.run_cdp("Network.getAllCookies")
                    if any(
                        c.get("name") == "cf_clearance" and "openai" in c.get("domain", "")
                        for c in (cdp_check.get("cookies") or [])
                    ):
                        has_clearance = True
                        break

                # 也访问 sentinel 域获取其 clearance
                if has_clearance:
                    try:
                        page.get("https://sentinel.openai.com/", timeout=10)
                        time.sleep(3)
                    except Exception:
                        pass

                # 提取所有 cookies
                cdp_cookies = page.run_cdp("Network.getAllCookies")
                browser_cookies = cdp_cookies.get("cookies", []) if cdp_cookies else []

                injected = 0
                target_domains = (".chatgpt.com", "chatgpt.com", ".auth.openai.com", "auth.openai.com", ".openai.com")
                target_names = {
                    "cf_clearance", "__cf_bm", "_cfuvid", "__cflb",
                    "oai-did", "oai-sc", "oai-chat-web-route",
                    "__Host-next-auth.csrf-token", "__Secure-next-auth.callback-url",
                    "__Secure-next-auth.state",
                }

                for c in browser_cookies:
                    name = c.get("name", "")
                    value = c.get("value", "")
                    domain = c.get("domain", "")
                    path = c.get("path", "/")

                    if not name or not value:
                        continue

                    is_target = any(domain.endswith(d) or domain == d for d in target_domains)
                    if not is_target and name not in target_names:
                        continue

                    # 确保 domain 格式正确（curl_cffi 需要不带前导点）
                    clean_domain = domain.lstrip(".")
                    try:
                        self.session.cookies.set(name, value, domain=clean_domain, path=path)
                        injected += 1
                    except TypeError:
                        try:
                            self.session.cookies.set(name, value, domain=clean_domain)
                            injected += 1
                        except Exception:
                            pass
                    # 也设置带点的版本（兼容性）
                    if not domain.startswith("."):
                        try:
                            self.session.cookies.set(name, value, domain=f".{domain}", path=path)
                        except Exception:
                            pass

                has_clearance = any(
                    c.get("name") == "cf_clearance" and "openai" in c.get("domain", "")
                    for c in browser_cookies
                )
                self.log(f"[Bootstrap] 注入 {injected} 个 cookies, cf_clearance={'有' if has_clearance else '无'}")

            finally:
                try:
                    page.quit(force=True)
                except Exception:
                    pass

        except ImportError:
            self.log("[Bootstrap] DrissionPage 未安装，跳过浏览器预热")
        except Exception as e:
            self.log(f"[Bootstrap] 浏览器预热失败: {e}")

    def _sentinel_token(self, flow="authorize_continue") -> str:
        return _build_sentinel_token(
            self.session, self.device_id, self.ua, self.sec_ch_ua, self.impersonate,
            flow=flow, log_fn=self.log,
        )

    def _auth_api_headers(self, referer: str, sentinel_token: str = "") -> dict:
        """构建 auth API 请求头（与 OpenAI-register 对齐）"""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.AUTH,
            "Referer": referer,
            "User-Agent": self.ua,
            "oai-did": self.device_id,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-full-version": f'"{self.chrome_full}"',
            "sec-ch-ua-platform-version": f'"{random.randint(10,15)}.0.0"',
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": random.choice(["en-US,en;q=0.9", "en-US,en;q=0.9,zh-CN;q=0.8"]),
            "Priority": "u=1, i",
        }
        headers.update(_make_trace_headers())
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        return headers

    # ── Step 0: 访问首页 ──

    def visit_homepage(self):
        self.log("Step0: 访问 chatgpt.com...")
        r = self.session.get(
            f"{self.BASE}/",
            headers={"Accept": "text/html,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
        )
        self.log(f"  HTTP {r.status_code}, cookies={len(self.session.cookies)}")

    # ── Step 1: CSRF ──

    def get_csrf(self) -> str:
        self.log("Step1: 获取 CSRF Token...")
        r = self.session.get(
            f"{self.BASE}/api/auth/csrf",
            headers={"Accept": "application/json", "Referer": f"{self.BASE}/"},
        )
        data = r.json()
        token = data.get("csrfToken", "")
        if not token:
            raise RuntimeError("CSRF Token 获取失败")
        self.log(f"  csrf={token[:20]}...")
        return token

    # ── Step 2: Signin ──

    def signin(self, email: str, csrf: str) -> str:
        self.log(f"Step2: Signin {email}...")
        r = self.session.post(
            f"{self.BASE}/api/auth/signin/openai",
            params={
                "prompt": "login", "ext-oai-did": self.device_id,
                "auth_session_logging_id": self.auth_session_logging_id,
                "screen_hint": "login_or_signup", "login_hint": email,
            },
            data={"callbackUrl": f"{self.BASE}/", "csrfToken": csrf, "json": "true"},
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json", "Referer": f"{self.BASE}/", "Origin": self.BASE,
            },
        )
        data = r.json()
        url = data.get("url", "")
        if not url:
            raise RuntimeError(f"Signin 失败: {data}")
        return url

    # ── Step 3: Authorize ──

    def authorize(self, url: str) -> str:
        self.log("Step3: Authorize...")
        r = self.session.get(
            url,
            headers={"Accept": "text/html,*/*;q=0.8", "Referer": f"{self.BASE}/", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
        )
        final_url = str(r.url)
        final_path = urlparse(final_url).path
        self.log(f"  跳转到: {final_path}")
        return final_url

    # ── Step 4: Register ──

    def register_account(self, email: str, password: str):
        self.log("Step4: 提交注册...")
        # 确保 auth 域也有 oai-did cookie
        self.session.cookies.set("oai-did", self.device_id, domain=".auth.openai.com")
        self.session.cookies.set("oai-did", self.device_id, domain="auth.openai.com")

        if self._browser_bootstrap:
            # 浏览器模式：密码提交需要浏览器绕过 TLS 检测
            # 注意：浏览器会产生独立的 login_session，后续 OTP 验证也需要在浏览器中完成
            # 所以这里标记一下，让 register() 知道需要跳过协议的 send_otp/validate_otp
            self._browser_register_done = False
            self._browser_otp_done = False
            return self._register_via_browser_full(email, password)

        return self._register_account_via_protocol(email, password)

    def _register_account_via_protocol(self, email: str, password: str):
        """纯协议提交注册（可能被 TLS 指纹检测拦截）"""
        sentinel = self._sentinel_token(flow="username_password_create")
        if sentinel:
            self.log("  Sentinel token OK")
        else:
            self.log("  Sentinel token 获取失败，继续尝试")
        headers = self._auth_api_headers(
            referer=f"{self.AUTH}/create-account/password",
            sentinel_token=sentinel or "",
        )
        r = self.session.post(
            f"{self.AUTH}/api/accounts/user/register",
            json={"username": email, "password": password},
            headers=headers,
            allow_redirects=False,
        )
        data = r.json() if r.content else {}
        self.log(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"注册失败 ({r.status_code}): {json.dumps(data, ensure_ascii=False)[:300]}")
        return data

    def _register_via_browser_full(self, email: str, password: str):
        """用浏览器完成注册+密码提交，OTP 由外部 otp_callback 在 register() 中处理后回填"""
        self.log("  [Browser] 使用浏览器完成注册流程...")
        try:
            from DrissionPage import ChromiumOptions, ChromiumPage

            co = ChromiumOptions()
            co.auto_port()
            co.new_env()
            co.incognito()
            co.set_argument("--disable-blink-features=AutomationControlled")
            co.set_argument("--no-sandbox")
            co.set_argument("--lang=en-US")
            co.set_argument("--window-size=1920,1080")
            co.set_user_agent(self.ua)
            if self.proxy:
                co.set_proxy(self.proxy)

            page = ChromiumPage(addr_or_opts=co)
            page.run_js('Object.defineProperty(navigator,"webdriver",{get:()=>undefined});window.chrome={runtime:{}};')

            # 从首页开始
            page.get(f"{self.BASE}/", timeout=30)
            time.sleep(4)

            # 点注册
            for label in ["Sign up", "注册", "Get started"]:
                try:
                    btn = page.ele(f"text:{label}", timeout=3)
                    if btn:
                        btn.click()
                        break
                except Exception:
                    continue
            time.sleep(4)

            # 填邮箱
            email_input = page.ele('css:input[type="email"]', timeout=8) or page.ele('css:input[name="email"]', timeout=3)
            if email_input:
                email_input.click()
                email_input.input(email, clear=True)
                time.sleep(0.3)
                page.run_js('document.querySelector("form button[type=submit]")?.click()')
                time.sleep(4)

            # 检查是否已注册
            if "/log-in" in page.url:
                page.quit(force=True)
                raise RuntimeError("该邮箱已注册过 ChatGPT")

            # 等待密码页
            for _ in range(10):
                if "/create-account/password" in page.url:
                    break
                time.sleep(1)

            # 填密码
            pwd_input = page.ele('css:input[type="password"]', timeout=8)
            if not pwd_input:
                page.quit(force=True)
                raise RuntimeError(f"浏览器未找到密码输入框, URL: {page.url}")

            pwd_input.click()
            pwd_input.input(password, clear=True)
            time.sleep(0.5)
            page.run_js('document.querySelector("button[type=submit]")?.click()')
            time.sleep(5)

            # 处理超时重试
            for retry in range(3):
                if "/email-verification" in page.url or "/about-you" in page.url:
                    break
                if "/create-account/password" in page.url:
                    is_timeout = page.run_js('return /timed.out|糟糕|出错了/i.test(document.body?.innerText||"")')
                    if is_timeout:
                        self.log(f"  [Browser] 超时，重试 ({retry+1})...")
                        page.run_js('const bs=document.querySelectorAll("button,[role=button]");for(const b of bs){if(/重试|try.again/i.test(b.textContent)){b.click();break;}}')
                        time.sleep(3)
                        pwd2 = page.ele('css:input[type="password"]', timeout=5)
                        if pwd2:
                            pwd2.click()
                            pwd2.input(password, clear=True)
                            time.sleep(0.5)
                            page.run_js('document.querySelector("button[type=submit]")?.click()')
                            time.sleep(5)
                    else:
                        time.sleep(2)

            if "/email-verification" not in page.url and "/about-you" not in page.url:
                page.quit(force=True)
                raise RuntimeError(f"浏览器注册失败: {page.url}")

            self.log("  [Browser] 密码提交成功，进入验证码页面")
            self._browser_register_done = True
            # 保留 page 引用，供后续 OTP 使用
            self._browser_page = page
            return {"status": "ok"}

        except ImportError:
            self.log("  [Browser] DrissionPage 未安装，回退协议模式")
            return self._register_account_via_protocol(email, password)

    def _browser_fill_otp(self, code: str):
        """在浏览器中填入验证码"""
        page = getattr(self, "_browser_page", None)
        if not page:
            raise RuntimeError("浏览器页面不可用")

        self.log(f"  [Browser] 填入验证码 {code}...")
        filled = page.run_js(f'''
            const selectors = [
                'input[name="code"]', 'input[autocomplete="one-time-code"]',
                'input[type="text"][maxlength="6"]', 'input[inputmode="numeric"]',
            ];
            for (const sel of selectors) {{
                const input = document.querySelector(sel);
                if (input && input.offsetWidth > 0) {{
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(input, '{code}');
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return true;
                }}
            }}
            const singles = Array.from(document.querySelectorAll('input[maxlength="1"]')).filter(e => e.offsetWidth > 0);
            if (singles.length >= 6) {{
                for (let i = 0; i < 6; i++) {{
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    setter.call(singles[i], '{code}'[i]);
                    singles[i].dispatchEvent(new Event('input', {{bubbles: true}}));
                    singles[i].dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
                return true;
            }}
            return false;
        ''')

        if not filled:
            raise RuntimeError("浏览器未找到验证码输入框")

        # 提交
        time.sleep(0.5)
        page.run_js('document.querySelector("button[type=submit]")?.click()')
        time.sleep(5)

        # 检查结果
        if "/about-you" in page.url:
            self.log("  [Browser] 验证码通过")
            self._browser_otp_done = True
            return True

        # 检查是否验证码错误
        page_text = (page.html or "").lower()
        if "incorrect" in page_text or "错误" in page_text or "wrong" in page_text:
            self.log("  [Browser] 验证码不正确")
            return False

        if "/about-you" not in page.url and "/email-verification" in page.url:
            self.log("  [Browser] 验证码可能不正确，仍在验证页")
            return False

        self.log(f"  [Browser] 验证码提交后: {page.url}")
        self._browser_otp_done = True
        return True

    def _browser_fill_profile(self, name: str, birthdate: str):
        """在浏览器中填写个人信息"""
        page = getattr(self, "_browser_page", None)
        if not page:
            return

        if "/about-you" not in page.url:
            return

        self.log(f"  [Browser] 填写个人信息 {name}...")
        # 填名字
        name_input = page.ele('css:input[name="name"]', timeout=5) or page.ele('css:input[autocomplete="name"]', timeout=3)
        if name_input:
            name_input.click()
            name_input.input(name, clear=True)
            time.sleep(0.3)

        # 填生日
        year, month, day = birthdate.split("-")
        page.run_js(f'''
        (async function() {{
            const sleep = ms => new Promise(r => setTimeout(r, ms));
            const df = document.querySelector('div[role="group"][id*="birthday"]');
            if (df) {{
                const fill = async (seg, val) => {{
                    if (!seg) return;
                    seg.focus(); seg.click(); await sleep(100);
                    for (const ch of val) {{
                        seg.dispatchEvent(new KeyboardEvent('keydown', {{key:ch, bubbles:true}}));
                        seg.dispatchEvent(new InputEvent('input', {{inputType:'insertText', data:ch, bubbles:true}}));
                        await sleep(50);
                    }}
                    seg.dispatchEvent(new FocusEvent('blur', {{bubbles:true}})); await sleep(100);
                }};
                await fill(df.querySelector('[data-type="year"]'), '{year}');
                await fill(df.querySelector('[data-type="month"]'), '{month.zfill(2)}');
                await fill(df.querySelector('[data-type="day"]'), '{day.zfill(2)}');
            }} else {{
                const age = document.querySelector('input[name="age"]');
                if (age) {{
                    const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
                    s.call(age, '{2026 - int(year)}');
                    age.dispatchEvent(new Event('input', {{bubbles:true}}));
                }}
            }}
        }})();
        ''')
        time.sleep(1)

        # 提交
        page.run_js('document.querySelector("button[type=submit]")?.click()')
        time.sleep(3)

    def _browser_cleanup(self):
        """关闭浏览器并回注 cookies"""
        page = getattr(self, "_browser_page", None)
        if not page:
            return
        self._extract_browser_cookies(page)
        try:
            page.quit(force=True)
        except Exception:
            pass
        self._browser_page = None

    def _extract_browser_cookies(self, page):
        """从浏览器提取 cookies 回注到 curl_cffi session"""
        try:
            cdp_cookies = page.run_cdp("Network.getAllCookies")
            for c in (cdp_cookies.get("cookies") or []):
                name = c.get("name", "")
                value = c.get("value", "")
                domain = c.get("domain", "").lstrip(".")
                if name and value and ("openai" in domain or "chatgpt" in domain):
                    try:
                        self.session.cookies.set(name, value, domain=domain, path=c.get("path", "/"))
                    except Exception:
                        try:
                            self.session.cookies.set(name, value, domain=domain)
                        except Exception:
                            pass
        except Exception:
            pass

    # ── Step 5: Send OTP ──

    def send_otp(self):
        self.log("Step5: 发送邮箱验证码...")
        r = self.session.get(
            f"{self.AUTH}/api/accounts/email-otp/send",
            headers={
                "Accept": "text/html,*/*;q=0.8",
                "Referer": f"{self.AUTH}/create-account/password",
                "Upgrade-Insecure-Requests": "1",
            },
            allow_redirects=True,
        )
        self.log(f"  HTTP {r.status_code}")

    # ── Step 6: Validate OTP ──

    def validate_otp(self, code: str):
        self.log(f"Step6: 验证码验证 {code}...")
        headers = self._auth_api_headers(referer=f"{self.AUTH}/email-verification")
        r = self.session.post(
            f"{self.AUTH}/api/accounts/email-otp/validate",
            json={"code": code},
            headers=headers,
        )
        data = r.json() if r.content else {}
        self.log(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"验证码验证失败 ({r.status_code}): {json.dumps(data, ensure_ascii=False)[:300]}")
        # 提取 continue_url
        page_type = str((data.get("page") or {}).get("type", "")).strip() if isinstance(data, dict) else ""
        continue_url = str(data.get("continue_url") or data.get("url") or "").strip() if isinstance(data, dict) else ""
        return data, page_type, continue_url

    # ── Step 7: Create Account ──

    def create_account(self, name: str, birthdate: str):
        self.log(f"Step7: 提交个人信息 {name}...")
        headers = {
            "Content-Type": "application/json", "Accept": "application/json",
            "Referer": f"{self.AUTH}/about-you", "Origin": self.AUTH,
        }
        headers.update(_make_trace_headers())
        r = self.session.post(
            f"{self.AUTH}/api/accounts/create_account",
            json={"name": name, "birthdate": birthdate},
            headers=headers,
        )
        data = r.json() if r.content else {}
        self.log(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            raise RuntimeError(f"创建账号失败 ({r.status_code}): {json.dumps(data, ensure_ascii=False)[:300]}")
        cb = data.get("continue_url") or data.get("url") or data.get("redirect_url")
        if cb:
            self._callback_url = cb
        return data

    # ── Step 8: Callback ──

    def callback(self, url: str = None):
        url = url or self._callback_url
        if not url:
            self.log("  跳过 callback（无 URL）")
            return
        self.log("Step8: Callback 确认...")
        r = self.session.get(
            url,
            headers={"Accept": "text/html,*/*;q=0.8", "Upgrade-Insecure-Requests": "1"},
            allow_redirects=True,
        )
        self.log(f"  HTTP {r.status_code} → {str(r.url)[:80]}")

    # ── 完整注册流程 ──

    def register(
        self,
        email: str,
        password: str,
        otp_callback: Optional[Callable[[], str]] = None,
    ) -> dict:
        first_name, last_name = _random_name()
        birthdate = _random_birthday()
        full_name = f"{first_name} {last_name}"

        # Browser bootstrap: 获取 cf_clearance
        if self._browser_bootstrap:
            self._bootstrap_clearance()
            _delay(0.3, 0.5)

        # Step 0
        self.visit_homepage()
        _delay(0.3, 0.8)

        # Step 1
        csrf = self.get_csrf()
        _delay(0.2, 0.5)

        # Step 2
        auth_url = self.signin(email, csrf)
        _delay(0.3, 0.8)

        # Step 3
        final_url = self.authorize(auth_url)
        final_path = urlparse(final_url).path
        _delay(0.3, 0.8)

        need_otp = False

        if "create-account/password" in final_path:
            self.log("  → 全新注册流程")
            # Step 4
            _delay(0.5, 1.0)
            self.register_account(email, password)
            _delay(0.3, 0.8)

            # 如果用了浏览器模式，OTP 和 profile 也在浏览器中完成
            if getattr(self, "_browser_register_done", False):
                if not otp_callback:
                    raise RuntimeError("需要 otp_callback 获取验证码")
                _delay(1.0, 2.0)
                code = otp_callback() or ""
                if not code:
                    self._browser_cleanup()
                    raise RuntimeError("未获取到验证码")

                ok = self._browser_fill_otp(code)
                if not ok:
                    # 可能验证码错，再试一次
                    _delay(2.0, 3.0)
                    code2 = otp_callback() or ""
                    if code2 and code2 != code:
                        ok = self._browser_fill_otp(code2)
                if not ok:
                    self._browser_cleanup()
                    raise RuntimeError("验证码验证失败")

                # 填 profile
                self._browser_fill_profile(full_name, birthdate)
                time.sleep(3)

                # 等待跳转到 chatgpt.com
                page = getattr(self, "_browser_page", None)
                if page:
                    for _ in range(15):
                        if "chatgpt.com" in page.url and "auth.openai.com" not in page.url:
                            break
                        time.sleep(1)

                self._browser_cleanup()
                return self._build_result(email, password, full_name)

            # Step 5 (协议模式)
            self.send_otp()
            need_otp = True
        elif "email-verification" in final_path or "email-otp" in final_path:
            self.log("  → OTP 验证阶段")
            need_otp = True
        elif "about-you" in final_path:
            self.log("  → 填写信息阶段")
            _delay(0.5, 1.0)
            self.create_account(full_name, birthdate)
            _delay(0.2, 0.5)
            self.callback()
            return self._build_result(email, password, full_name)
        elif "callback" in final_path or "chatgpt.com" in final_url:
            self.log("  → 注册最终确认阶段")
            return self._build_result(email, password, full_name)
        else:
            self.log(f"  → 未知跳转: {final_url}")
            _delay(0.5, 1.0)
            self.register_account(email, password)
            self.send_otp()
            need_otp = True

        # OTP 流程
        if need_otp:
            if not otp_callback:
                raise RuntimeError("需要 otp_callback 获取验证码")
            _delay(1.0, 2.0)
            code = otp_callback() or ""
            if not code:
                raise RuntimeError("未获取到验证码")

            _delay(0.3, 0.8)
            data, page_type, continue_url = self.validate_otp(code)

            # OTP 后可能进入 workspace
            if page_type in ("workspace", "organization") or "workspace" in continue_url:
                self.log("  OTP 后进入 workspace 流程")
                self.callback(continue_url)
                return self._build_result(email, password, full_name)

            if "callback" in continue_url or "chatgpt.com" in continue_url:
                _delay(0.2, 0.5)
                self.callback(continue_url)
                return self._build_result(email, password, full_name)

        # Step 7: Create Account
        _delay(0.5, 1.5)
        self.create_account(full_name, birthdate)
        _delay(0.2, 0.5)
        self.callback()

        return self._build_result(email, password, full_name)

    def _build_result(self, email: str, password: str, name: str) -> dict:
        # 保存 Cookie
        cookie_path = ""
        try:
            cookie_path = _save_cookies(email, self.session, output_dir=self.cookie_dir)
            self.log(f"  Cookie 已保存: {cookie_path}")
        except Exception as e:
            self.log(f"  Cookie 保存失败: {e}")

        cookies = _collect_cookies(self.session)
        session_token = cookies.get("__Secure-next-auth.session-token", "")

        self.log(f"✅ ChatGPT 注册成功: {email}")
        return {
            "email": email,
            "password": password,
            "name": name,
            "cookies": cookies,
            "cookie_file": cookie_path,
            "session_token": session_token,
        }
