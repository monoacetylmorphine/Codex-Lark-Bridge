import asyncio
import fcntl
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(), override=True)




# 来源：原脚本的首页目标地址，统一使用 HTTPS。
TARGET_URL = "https://sycm.taobao.com/portal/home.htm"
LOGIN_FRAME = "#alibaba-login-box"
# 来源：用户提供的登录后链接 HTML；按 href 和文字定位，忽略隐藏副本。
ANALYSIS_PATH = "/adm/v3/micro/auto_analysis/my_space"
ANALYSIS_SELECTOR = f'a[href="{ANALYSIS_PATH}"]:has-text("自助分析"):visible'
BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "context.json"
# 来源：本项目运行策略；网络操作 30 秒，人工处理最多 10 分钟。
ACTION_TIMEOUT_MS = 30_000
MANUAL_TIMEOUT_SECONDS = 600
logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


class LoginRequired(Exception):
    pass


class ReportConfigurationError(Exception):
    pass


def read_config():
    names = ("SYCM_USERNAME", "SYCM_PASSWORD")
    missing = [name for name in names if not os.environ.get(name)]
    if missing and not os.environ.get("SYCM_COOKIE", "").strip() and not STATE_PATH.is_file():
        raise ConfigurationError("缺少环境变量：" + ", ".join(missing))
    # 默认采用用户提供的业务链接；可选环境变量仍允许覆盖。
    selector = os.environ.get("SYCM_READY_SELECTOR", "").strip() or ANALYSIS_SELECTOR
    return os.environ.get(names[0]), os.environ.get(names[1]), selector


def parse_cookie_header(raw):
    header = raw.strip()
    if header.lower().startswith("cookie:"):
        header = header[7:].strip()
    cookies = []
    names = set()
    for item in header.split(";"):
        if not item.strip():
            continue
        name, separator, value = item.strip().partition("=")
        # 支持聊天 Markdown 中转义的下划线；不解码或改写 Cookie 值。
        name = name.strip().replace("\\_", "_")
        if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ConfigurationError("SYCM_COOKIE 格式错误，应为 Cookie 请求头中的 name=value 列表。")
        if name in names:
            raise ConfigurationError("SYCM_COOKIE 含重名 Cookie，无法从请求头还原各自路径，请使用完整浏览器导出。")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ConfigurationError("SYCM_COOKIE 含控制字符，请提供单行请求头。")
        names.add(name)
        # provenance: unknown；请求头缺少 domain/path/expires/httpOnly/sameSite。
        # 限定到目标 HTTPS 主机根路径，使用会话 Cookie，不臆造跨域权限和有效期。
        cookies.append({"name": name, "value": value, "url": "https://sycm.taobao.com/"})
    if not cookies:
        raise ConfigurationError("SYCM_COOKIE 为空，未导入任何 Cookie。")
    return cookies


def load_state(path):
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or not all(
            isinstance(state.get(key), list) for key in ("cookies", "origins")
        ):
            raise ValueError("invalid storage state")
        return state
    except (ValueError, OSError) as exc:
        # 保留原文件，避免把损坏、权限异常静默当成首次运行。
        raise ConfigurationError("登录态文件无法读取或格式错误，请检查代码同级目录下的 context.json") from exc


def save_state(path, state):
    # 先写临时文件再替换，失败时原会话仍保留；不输出凭据或 Cookie。
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


async def is_ready(page, selector):
    current = urlparse(page.url)
    target = urlparse(TARGET_URL)
    if (current.scheme, current.hostname, current.path.rstrip("/")) != (
        target.scheme, target.hostname, target.path.rstrip("/")
    ):
        return False
    if await page.locator(LOGIN_FRAME).is_visible():
        return False
    return await page.locator(selector or ANALYSIS_SELECTOR).first.is_visible()


async def open_analysis(page):
    await page.locator(ANALYSIS_SELECTOR).first.click()
    await page.wait_for_url(
        lambda url: (
            urlparse(url).scheme == "https"
            and urlparse(url).hostname == "sycm.taobao.com"
            and urlparse(url).path.rstrip("/") == ANALYSIS_PATH
        ),
        wait_until="domcontentloaded",
        timeout=ACTION_TIMEOUT_MS,
    )
    logger.info("已点击自助分析并到达目标地址。")


def watch_browser_lifecycle(browser, context):
    status = {"closing": False, "pages": 0}

    def record(message):
        if not status["closing"]:
            logger.warning("浏览器事件：%s。", message)

    def watch_page(page):
        status["pages"] += 1
        number = status["pages"]
        logger.info("浏览器事件：创建页面 #%s。", number)
        page.on("close", lambda _: record(f"页面 #{number} 关闭"))
        page.on("crash", lambda _: record(f"页面 #{number} 崩溃"))

    context.on("page", watch_page)
    context.on("close", lambda _: record("context 关闭"))
    browser.on("disconnected", lambda _: record("浏览器连接断开"))
    for page in context.pages:
        watch_page(page)
    return status


async def watch_login_redirect(page):
    while True:
        if page.is_closed():
            raise LoginRequired("等待报表期间页面已关闭。")
        current = urlparse(page.url)
        # 来源：现有生意参谋登录地址；只匹配 pathname，不误判 _target 查询参数。
        login_url = current.hostname == "sycm.taobao.com" and current.path == "/custom/login.htm"
        if login_url or await page.locator(LOGIN_FRAME).is_visible():
            raise LoginRequired(
                "进入取数报表后检测到登录页或登录框，当前会话未被接受；"
                "尚未点击自动更新。请在普通浏览器确认报表可访问，并更新完整登录态。"
            )
        await asyncio.sleep(0.5)


async def wait_for_report_form(page, group):
    form_task = asyncio.create_task(group.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS))
    login_task = asyncio.create_task(watch_login_redirect(page))
    try:
        done, _ = await asyncio.wait((form_task, login_task), return_when=asyncio.FIRST_COMPLETED)
        if login_task in done:
            await login_task
        await form_task
    finally:
        for task in (form_task, login_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(form_task, login_task, return_exceptions=True)


async def configure_report(page):
    # 来源：用户提供的取数报表入口及表单 HTML。
    stage = "点击取数报表入口"
    try:
        logger.info("取数报表：%s。", stage)
        await page.locator("div.nameWrapper:visible").filter(
            has_text=re.compile(r"^\s*取数报表\s*$")
        ).first.click()
        stage = "等待可见的更新设置表单"
        logger.info("取数报表：%s。", stage)
        group = page.locator('.create-data-fetch-content #isAutoUpdate:visible')
        await wait_for_report_form(page, group)
        # 完整匹配，避免把“不自动更新”误当成“自动更新”。
        label = group.locator("label").filter(has_text=re.compile(r"^\s*自动更新\s*$"))
        auto_update = label.locator('input[type="radio"][value="1"]')
        stage = "点击自动更新并等待选中"
        logger.info("取数报表：%s。", stage)
        if not await auto_update.is_checked():
            await label.click(timeout=ACTION_TIMEOUT_MS)
        # :checked 检查实时状态，等待页面事件处理完成，不读静态 checked 属性。
        await label.locator('input[type="radio"][value="1"]:checked').wait_for(
            state="attached", timeout=ACTION_TIMEOUT_MS
        )
        logger.info("自动更新已确认选中。")
        pc = page.get_by_role("checkbox", name="PC端", exact=True)
        wireless = page.get_by_role("checkbox", name="无线端", exact=True)
        stage = "取消勾选PC端"
        logger.info("取数报表：%s。", stage)
        await pc.uncheck()
        stage = "取消勾选无线端"
        logger.info("取数报表：%s。", stage)
        await wireless.uncheck()
        stage = "复核最终选项状态"
        if not await auto_update.is_checked() or await pc.is_checked() or await wireless.is_checked():
            raise RuntimeError("选项状态被页面重置")
    except LoginRequired:
        raise
    except Exception as exc:
        logger.error(
            "报表失败时状态：当前页面已关闭=%s，context 内页面数=%s。",
            page.is_closed(), len(page.context.pages),
        )
        # 只记录步骤和异常类型，不输出可能包含凭据的浏览器调用日志。
        raise ReportConfigurationError(f"取数报表失败：{stage}（{type(exc).__name__}）。") from exc
    logger.info("取数报表已选择自动更新，PC端和无线端均已取消勾选；尚未提交报表。")


async def wait_until_ready(page, selector, timeout_seconds):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if page.is_closed():
            raise LoginRequired("浏览器页面已关闭，登录未完成。")
        if await is_ready(page, selector):
            return
        await asyncio.sleep(1)
    raise LoginRequired("等待登录成功超时；原登录态未更新。请检查验证状态；若设置了 SYCM_READY_SELECTOR，也请检查其有效性。")


async def ensure_login(page, username, password, selector, timeout_error):
    await page.goto(TARGET_URL, wait_until="domcontentloaded")
    # 等待应用渲染或登录框出现，不凭 Cookie 文件存在就判定成功。
    deadline = asyncio.get_running_loop().time() + ACTION_TIMEOUT_MS / 1000
    while asyncio.get_running_loop().time() < deadline:
        if await is_ready(page, selector):
            logger.info("首页登录标志校验通过；报表页面访问权限仍需后续验证。")
            return
        if await page.locator(LOGIN_FRAME).is_visible():
            break
        await asyncio.sleep(1)

    logger.warning("尚未检测到登录成功标志，请在浏览器完成登录或验证。最多等待 10 分钟，成功后自动继续。")
    if username and password and await page.locator(LOGIN_FRAME).is_visible():
        frame = page.frame_locator(LOGIN_FRAME)
        try:
            # 来源：原脚本；线上表单发生变化时交由人工处理。
            await frame.get_by_placeholder(re.compile("账号名")).fill(username)
            await frame.get_by_placeholder(re.compile("密码")).fill(password)
            # 最多自动提交一次，按钮不可用或验证阻塞时不循环重试。
            await frame.get_by_role("button", name="登录", exact=True).click(timeout=3000)
        except timeout_error:
            logger.warning("自动填写或提交未完成，请在浏览器中手动完成登录。")
    await wait_until_ready(page, selector, MANUAL_TIMEOUT_SECONDS)


async def main():
    username, password, selector = read_config()
    raw_cookie = os.environ.get("SYCM_COOKIE", "").strip()
    imported_cookies = parse_cookie_header(raw_cookie) if raw_cookie else None
    if imported_cookies:
        # 显式导入时只验证这份会话，失败后不自动提交账号密码。
        username = password = None
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ConfigurationError("未安装 Playwright，请按 README.md 安装依赖和 WebKit。") from exc

    # macOS/Linux 文件锁：避免计划任务重叠写入登录态；进程退出即释放。
    with (BASE_DIR / ".sycm.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ConfigurationError("已有脚本实例正在运行，本次退出。") from exc
        state = None if imported_cookies else load_state(STATE_PATH)
        async with async_playwright() as p:
            browser = await p.webkit.launch(headless=False)
            lifecycle = None
            try:
                context = await browser.new_context(storage_state=state)
                lifecycle = watch_browser_lifecycle(browser, context)
                if imported_cookies:
                    await context.add_cookies(imported_cookies)
                    logger.info("已导入 Cookie，将访问首页验证会话；尚未保存 context.json。")
                context.set_default_timeout(ACTION_TIMEOUT_MS)
                page = await context.new_page()
                await ensure_login(page, username, password, selector, PlaywrightTimeoutError)
                save_state(STATE_PATH, await context.storage_state(indexed_db=True))
                logger.info("已确认登录成功并自动保存 context.json。")
                await open_analysis(page)
                await configure_report(page)
            finally:
                logger.info("脚本开始清理浏览器（流程已结束或发生错误）。")
                if lifecycle is not None:
                    lifecycle["closing"] = True
                await browser.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(main())
    except ConfigurationError as exc:
        logger.error("%s", exc)
        raise SystemExit(2)
    except LoginRequired as exc:
        logger.error("%s", exc)
        raise SystemExit(3)
    except ReportConfigurationError as exc:
        logger.error("%s", exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        logger.warning("已取消运行。")
        raise SystemExit(130)
    except Exception as exc:
        # 不打印页面源码、请求参数或可能含敏感输入的 Playwright 调用日志。
        logger.error("运行失败（%s）；请检查网络、浏览器安装与页面定位。", type(exc).__name__)
        raise SystemExit(1)
