import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sycm


def make_page(url=sycm.TARGET_URL, ready=True, login=False):
    page = MagicMock()
    page.url = url
    page.is_closed.return_value = False
    login_locator = MagicMock()
    login_locator.is_visible = AsyncMock(return_value=login)
    ready_locator = MagicMock()
    ready_locator.first.is_visible = AsyncMock(return_value=ready)
    page.locator.side_effect = lambda selector: (
        login_locator if selector == sycm.LOGIN_FRAME else ready_locator
    )
    page.goto = AsyncMock()
    return page


class ConfigurationTests(unittest.TestCase):
    def test_missing_variables_report_names(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sycm.STATE_PATH.__class__, "is_file", return_value=False):
            with self.assertRaises(sycm.ConfigurationError) as caught:
                sycm.read_config()
        for name in ("SYCM_USERNAME", "SYCM_PASSWORD"):
            self.assertIn(name, str(caught.exception))
        self.assertNotIn("SYCM_READY_SELECTOR", str(caught.exception))

    def test_cookie_import_does_not_require_password(self):
        with patch.dict(os.environ, {"SYCM_COOKIE": "test_cookie=test-value"}, clear=True):
            self.assertEqual(sycm.read_config(), (None, None, sycm.ANALYSIS_SELECTOR))

    def test_saved_state_does_not_require_password(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sycm.STATE_PATH.__class__, "is_file", return_value=True):
            self.assertEqual(sycm.read_config(), (None, None, sycm.ANALYSIS_SELECTOR))

    def test_cookie_parser_preserves_values_and_scopes_host(self):
        cookies = sycm.parse_cookie_header(r"Cookie: \_test\_=a=b%2B+; other=; third=null;")
        self.assertEqual([cookie["value"] for cookie in cookies], ["a=b%2B+", "", "null"])
        self.assertEqual(cookies[0]["name"], "_test_")
        self.assertTrue(all(cookie["url"] == "https://sycm.taobao.com/" for cookie in cookies))

    def test_invalid_cookie_is_rejected_without_echoing_values(self):
        for raw in ("Cookie:", "bad", "a=x; a=y", "a=private\nvalue", "bad name=private"):
            with self.subTest(raw=raw), self.assertRaises(sycm.ConfigurationError) as caught:
                sycm.parse_cookie_header(raw)
            self.assertNotIn("private", str(caught.exception))

    def test_selector_is_optional(self):
        for selector in (None, "", "  ", "#private"):
            # 测试数据，不是真实凭据。
            environment = {"SYCM_USERNAME": "test-user", "SYCM_PASSWORD": "test-only"}
            if selector is not None:
                environment["SYCM_READY_SELECTOR"] = selector
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(sycm.read_config()[2], (selector.strip() if selector else "") or sycm.ANALYSIS_SELECTOR)

    def test_state_path_is_next_to_script(self):
        self.assertEqual(sycm.STATE_PATH, Path(sycm.__file__).resolve().with_name("context.json"))

    def test_state_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            self.assertIsNone(sycm.load_state(path))
            state = {"cookies": [], "origins": []}
            sycm.save_state(path, state)
            self.assertEqual(sycm.load_state(path), state)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_corrupt_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(sycm.ConfigurationError):
                sycm.load_state(path)
            self.assertEqual(path.read_text(), "broken")

    def test_failed_save_preserves_previous_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "context.json"
            state = {"cookies": [], "origins": []}
            sycm.save_state(path, state)
            with patch("sycm.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    sycm.save_state(path, {"cookies": [], "origins": [{"origin": "changed"}]})
            self.assertEqual(sycm.load_state(path), state)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_cookie_only_session_failure_never_submits_credentials(self):
        page = make_page(login=True)
        with patch("sycm.wait_until_ready", new_callable=AsyncMock) as wait:
            await sycm.ensure_login(page, None, None, None, asyncio.TimeoutError)
        page.frame_locator.assert_not_called()
        wait.assert_awaited_once()
    async def test_missing_selector_uses_url_and_login_frame_checks(self):
        self.assertTrue(await sycm.is_ready(make_page(), None))
        self.assertFalse(await sycm.is_ready(make_page(login=True), None))
        self.assertFalse(await sycm.is_ready(make_page(url="https://example.com"), None))

    async def test_default_selector_requires_visible_analysis_link(self):
        page = make_page(ready=False)
        self.assertFalse(await sycm.is_ready(page, None))
        page.locator.assert_called_with(sycm.ANALYSIS_SELECTOR)

    async def test_ready_session_continues_without_terminal_input(self):
        with patch("builtins.input", side_effect=AssertionError("不应读取终端")):
            await sycm.ensure_login(make_page(), None, None, None, asyncio.TimeoutError)
            await sycm.wait_until_ready(make_page(), None, 1)

    async def test_click_analysis_waits_for_expected_destination(self):
        page = make_page()
        page.locator(sycm.ANALYSIS_SELECTOR).first.click = AsyncMock()
        page.wait_for_url = AsyncMock()
        await sycm.open_analysis(page)
        page.locator(sycm.ANALYSIS_SELECTOR).first.click.assert_awaited_once()
        predicate = page.wait_for_url.call_args.args[0]
        self.assertTrue(predicate("https://sycm.taobao.com" + sycm.ANALYSIS_PATH + "?test=1"))
        self.assertFalse(predicate(sycm.TARGET_URL))
        self.assertFalse(predicate("https://example.com" + sycm.ANALYSIS_PATH))

    async def test_failed_analysis_navigation_is_not_reported_as_success(self):
        page = make_page()
        page.locator(sycm.ANALYSIS_SELECTOR).first.click = AsyncMock()
        page.wait_for_url = AsyncMock(side_effect=asyncio.TimeoutError)
        with self.assertRaises(asyncio.TimeoutError):
            await sycm.open_analysis(page)

    async def test_ready_requires_target_element_and_no_login(self):
        cases = [
            (sycm.TARGET_URL, True, False, True),
            (sycm.TARGET_URL, False, False, False),
            (sycm.TARGET_URL, True, True, False),
            ("https://sycm.taobao.com/custom/login.htm", True, False, False),
            ("https://example.com/portal/home.htm", True, False, False),
        ]
        for url, ready, login, expected in cases:
            with self.subTest(url=url, ready=ready, login=login):
                self.assertEqual(await sycm.is_ready(make_page(url, ready, login), "#private"), expected)

    async def test_valid_session_skips_credentials_and_submit(self):
        page = make_page()
        await sycm.ensure_login(page, "", "", "#private", asyncio.TimeoutError)
        page.frame_locator.assert_not_called()

    async def test_blocked_submit_waits_for_manual_login_without_retry(self):
        page = make_page(login=True)
        field = MagicMock()
        field.fill = AsyncMock()
        frame = page.frame_locator.return_value
        frame.get_by_placeholder.return_value = field
        button = frame.get_by_role.return_value
        button.click = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch("sycm.wait_until_ready", new_callable=AsyncMock) as wait:
            # 测试数据，不是真实凭据。
            await sycm.ensure_login(page, "test-user", "test-only", "#private", asyncio.TimeoutError)
        button.click.assert_awaited_once()
        wait.assert_awaited_once()

    async def test_manual_wait_timeout(self):
        with self.assertRaises(sycm.LoginRequired):
            await sycm.wait_until_ready(make_page(ready=False), "#private", 0)

    async def test_closed_page_does_not_succeed(self):
        page = make_page()
        page.is_closed.return_value = True
        with self.assertRaises(sycm.LoginRequired):
            await sycm.wait_until_ready(page, "#private", 1)


class LifecycleTests(unittest.TestCase):
    def test_tracks_close_crash_and_disconnect_but_ignores_cleanup(self):
        browser, context, page = MagicMock(), MagicMock(), MagicMock()
        context.pages = [page]
        status = sycm.watch_browser_lifecycle(browser, context)
        page_handlers = dict(call.args for call in page.on.call_args_list)
        browser_handlers = dict(call.args for call in browser.on.call_args_list)
        with self.assertLogs(sycm.logger, level="WARNING") as messages:
            page_handlers["crash"](page)
            page_handlers["close"](page)
            browser_handlers["disconnected"](browser)
        self.assertEqual(len(messages.output), 3)
        status["closing"] = True
        with patch.object(sycm.logger, "warning") as warning:
            page_handlers["close"](page)
            browser_handlers["disconnected"](browser)
        warning.assert_not_called()


class ReportTests(unittest.IsolatedAsyncioTestCase):
    def make_report_page(self, initially_checked):
        page = MagicMock()
        page.url = "https://sycm.taobao.com/adm/v3/micro/auto_analysis/datafetch/create"
        page.is_closed.return_value = False
        menu = MagicMock()
        menu.filter.return_value.first.click = AsyncMock()
        controls = []
        for initial in initially_checked:
            control = MagicMock()
            state = {"checked": initial}

            async def check(state=state, **kwargs):
                state["checked"] = True

            async def uncheck(state=state):
                state["checked"] = False

            async def is_checked(state=state):
                return state["checked"]

            control.check = AsyncMock(side_effect=check)
            control.uncheck = AsyncMock(side_effect=uncheck)
            control.is_checked = AsyncMock(side_effect=is_checked)
            controls.append(control)
        group = MagicMock()
        group.wait_for = AsyncMock()
        label = group.locator.return_value.filter.return_value
        label.click = controls[0].check
        checked = MagicMock()
        checked.wait_for = AsyncMock()
        label.locator.side_effect = lambda selector: checked if selector.endswith(":checked") else controls[0]
        login = MagicMock()
        login.is_visible = AsyncMock(return_value=False)
        page.locator.side_effect = lambda selector: (
            login if selector == sycm.LOGIN_FRAME else menu if selector == "div.nameWrapper:visible" else group
        )
        page.get_by_role.side_effect = lambda role, name, exact: controls[1] if name == "PC端" else controls[2]
        return page, controls

    async def test_report_options_converge_and_remain_idempotent(self):
        for initial in ((False, True, True), (True, False, False), (True, True, False)):
            with self.subTest(initial=initial):
                page, controls = self.make_report_page(initial)
                await sycm.configure_report(page)
                await sycm.configure_report(page)
                self.assertEqual([await control.is_checked() for control in controls], [True, False, False])

    async def test_page_rejecting_uncheck_fails(self):
        page, controls = self.make_report_page((True, True, True))
        controls[2].uncheck = AsyncMock()
        with self.assertRaisesRegex(sycm.ReportConfigurationError, "复核最终选项状态"):
            await sycm.configure_report(page)

    async def test_missing_control_timeout_stops_workflow(self):
        page, controls = self.make_report_page((False, True, True))
        controls[0].check.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(sycm.ReportConfigurationError, "点击自动更新并等待选中"):
            await sycm.configure_report(page)
        controls[1].uncheck.assert_not_awaited()

    async def test_login_redirect_cancels_form_wait(self):
        page, controls = self.make_report_page((False, True, True))
        page.url = "https://sycm.taobao.com/custom/login.htm?_target=private"
        group = page.locator('.create-data-fetch-content #isAutoUpdate:visible')

        async def pending(**kwargs):
            await asyncio.Event().wait()

        group.wait_for.side_effect = pending
        with self.assertRaisesRegex(sycm.LoginRequired, "当前会话未被接受"):
            await asyncio.wait_for(sycm.configure_report(page), timeout=1)
        controls[0].check.assert_not_awaited()

    async def test_visible_login_iframe_cancels_form_wait(self):
        page, controls = self.make_report_page((False, True, True))
        page.locator(sycm.LOGIN_FRAME).is_visible.return_value = True
        with self.assertRaises(sycm.LoginRequired):
            await sycm.watch_login_redirect(page)
        controls[2].uncheck.assert_not_awaited()

    async def test_missing_form_reports_wait_stage(self):
        page, controls = self.make_report_page((False, True, True))
        page.locator('.create-data-fetch-content #isAutoUpdate:visible').wait_for.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(sycm.ReportConfigurationError, "等待可见的更新设置表单"):
            await sycm.configure_report(page)
        controls[0].check.assert_not_awaited()

    async def test_click_without_state_change_stops_before_checkboxes(self):
        page, controls = self.make_report_page((False, True, True))
        group = page.locator('.create-data-fetch-content #isAutoUpdate:visible')
        label = group.locator.return_value.filter.return_value
        label.click = AsyncMock()
        label.locator('input[type="radio"][value="1"]:checked').wait_for.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(sycm.ReportConfigurationError, "点击自动更新并等待选中"):
            await sycm.configure_report(page)
        controls[1].uncheck.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
