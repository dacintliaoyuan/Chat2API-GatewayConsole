"""Private browser driver for the local Rust gateway, over inherited pipes."""

import json
import sys
from urllib.parse import urlsplit

from playwright.sync_api import Error, sync_playwright


def reply(value=None, error=None):
    print(json.dumps({"value": value, "error": error}), flush=True)


def unique_control(page, selectors):
    # As in the old gateway, several selectors can identify the same control.
    first = None
    for selector in selectors:
        for element in page.locator(selector).element_handles():
            if not element.is_visible():
                element.dispose()
                continue
            if first is None:
                first = element
                continue
            same = element.evaluate("(el, other) => el === other", first)
            element.dispose()
            if not same:
                first.dispose()
                return None
    return first


class BrowserPage:
    def __init__(self, page, initial_url):
        self.page = page
        self.target = None
        self.app_origin = self.origin(initial_url)

    @staticmethod
    def origin(url):
        parts = urlsplit(url)
        default_port = {"http": 80, "https": 443}.get(parts.scheme)
        return parts.scheme, parts.hostname, parts.port or default_port

    def drive(self, op, args):
        page = self.page
        if op == "url":
            return page.url
        if op == "is_closed":
            return page.is_closed()
        if op == "front":
            return page.bring_to_front()
        if op == "goto":
            page.goto(args, wait_until="domcontentloaded", timeout=15000)
            return None
        if op == "reload":
            page.reload(wait_until="domcontentloaded", timeout=15000)
            return None
        if op == "read_token":
            if self.origin(page.url) == self.app_origin:
                return page.evaluate("() => localStorage.getItem('token')")
            return None
        if op == "drop_token":
            return page.evaluate("() => localStorage.removeItem('token')")
        if op == "catalog_access":
            if self.origin(page.url) != self.app_origin:
                return False
            return page.evaluate(
                """async ([base, candidate, timeout]) => {
                    if (location.origin !== new URL(base).origin ||
                        localStorage.getItem('token') !== candidate) return false;
                    try {
                        const r = await fetch('/api/models', {
                            headers: {Accept: 'application/json', Authorization: 'Bearer ' + candidate},
                            credentials: 'same-origin', redirect: 'error', cache: 'no-store',
                            signal: AbortSignal.timeout(timeout),
                        });
                        return r.status === 200 && Array.isArray((await r.json()).data);
                    } catch { return false; }
                }""",
                args,
            )
        if op == "visible":
            for selector in args:
                locator = page.locator(selector)
                if any(locator.nth(index).is_visible() for index in range(locator.count())):
                    return True
            return False
        if op == "target":
            if self.target is not None:
                self.target.dispose()
            selectors = args["selectors"]
            marker = args["marker"]
            self.target = unique_control(page, selectors)
            if self.target is not None:
                self.target.evaluate(
                    "(el, id) => el.setAttribute('data-um-playwright-target', id)", marker
                )
            return self.target is not None
        if op == "click":
            target, self.target = self.target, None
            if target is None:
                raise ValueError("login target absent")
            try:
                return target.click(timeout=15000)
            finally:
                target.dispose()
        selectors = args["selectors"] if op == "fill" else args
        field = unique_control(page, selectors)
        if field is None:
            if op == "field":
                return "missing"
            if op == "value":
                return None
            raise ValueError("login field absent")
        try:
            if op == "fill":
                return field.fill(args["value"], timeout=5000)
            if op == "field":
                if field.input_value(timeout=5000).strip():
                    return "filled"
                return field.evaluate("""el => el.matches(':-webkit-autofill')
                    ? 'protected' : 'empty'""")
            raise ValueError("unknown operation")
        finally:
            field.dispose()


def main():
    request = json.loads(sys.stdin.readline())
    with sync_playwright() as playwright:
        options = {
            "headless": not request["headful"],
            "timeout": 25000,
            "executable_path": request["executable"],
        }
        if request["headful"]:
            options.update(args=["--start-maximized"], no_viewport=True)
        context = playwright.chromium.launch_persistent_context(request["profile_dir"], **options)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            driver = BrowserPage(page, request["initial_url"])
            reply(True)
            for line in sys.stdin:
                command = json.loads(line)
                if command["op"] == "close":
                    break
                try:
                    reply(driver.drive(command["op"], command["args"]))
                except (Error, ValueError, KeyError) as exc:
                    # Playwright error text can contain page or form data.
                    reply(error=type(exc).__name__)
        finally:
            context.close()
    reply(True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — 进程边界只返回异常类型，避免原始错误泄露表单数据。
        reply(error=type(exc).__name__)
        sys.exit(1)
