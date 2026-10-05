from __future__ import annotations

import time

import uiautomator2 as u2
from rich.console import Console


console = Console()


class DouyinDevice:
    def __init__(self, serial: str | None, package_name: str) -> None:
        self.serial = serial
        self.package_name = package_name
        self.d = u2.connect(serial) if serial else u2.connect()

    def prepare(self) -> None:
        self.d.screen_on()
        self.d.app_start(self.package_name)
        time.sleep(5)

    def search(self, keyword: str) -> None:
        self._tap_search_entry()
        time.sleep(1)
        self._type_keyword(keyword)
        time.sleep(1)
        self._tap_search_button()

    def swipe_pages(self, pages: int, wait_after_swipe: float) -> None:
        for page in range(max(pages, 1)):
            console.print(f"[cyan]Triggering page {page + 1}/{pages}[/cyan]")
            time.sleep(wait_after_swipe)
            self.d.swipe_ext("up", scale=0.78)

    def tap_product_tab(self) -> None:
        selectors = [
            self.d(text="商品"),
            self.d(textContains="商品"),
            self.d(descriptionContains="商品"),
        ]
        for selector in selectors:
            if selector.exists(timeout=2):
                selector.click()
                time.sleep(2)
                return

    def _tap_search_entry(self) -> None:
        selectors = [
            self.d(descriptionContains="搜索"),
            self.d(textContains="搜索"),
            self.d(resourceIdMatches=".*search.*"),
        ]
        for selector in selectors:
            if selector.exists(timeout=2):
                selector.click()
                return
        width, _ = self.d.window_size()
        self.d.click(width * 0.93, 140)

    def _type_keyword(self, keyword: str) -> None:
        edit = self.d(className="android.widget.EditText")
        if edit.exists(timeout=3):
            edit.click()
            time.sleep(0.3)
            try:
                edit.clear_text()
            except Exception:
                self.d.shell(["input", "keyevent", "123"])
                for _ in range(30):
                    self.d.shell(["input", "keyevent", "67"])
            edit.set_text(keyword)
            return
        escaped = keyword.replace(" ", "%s")
        self.d.shell(["input", "text", escaped])

    def _tap_search_button(self) -> None:
        selectors = [
            self.d(text="搜索"),
            self.d(textContains="搜索"),
            self.d(descriptionContains="搜索"),
        ]
        for selector in selectors:
            if selector.exists(timeout=2):
                selector.click()
                return
        self.d.press("enter")
