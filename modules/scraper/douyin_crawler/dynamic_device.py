"""
dynamic_device.py — 通用 Android 模拟器设备交互

基于配置驱动，支持抖音/快手/小红书等不同 App。
通过 shared/platform_configs.py 中的 PLATFORM_EMULATOR_CONFIGS 配置差异。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import uiautomator2 as u2
from rich.console import Console


console = Console()


class EmulatorDevice:
    """通用模拟器设备，支持多平台搜索/上滑/商品tab"""

    def __init__(
        self,
        platform_key: str,
        platform_config: dict[str, Any],
        serial: str | None = None,
    ) -> None:
        self.platform_key = platform_key
        self.config = platform_config
        self.serial = serial
        self.d = u2.connect(serial) if serial else u2.connect()

    def prepare(self) -> None:
        """启动 App"""
        package = self.config.get('package_name', '')
        if not package:
            raise ValueError(f"Platform {self.platform_key} has no package_name")
        self.d.screen_on()
        self.d.app_start(package)
        time.sleep(5)

    def search(self, keyword: str) -> None:
        """通用搜索流程"""
        self._tap_search_entry()
        time.sleep(1)
        self._type_keyword(keyword)
        time.sleep(1)
        self._tap_search_button()

    def swipe_pages(self, pages: int, wait_after_swipe: float | None = None) -> None:
        """通用上滑翻页"""
        wait = wait_after_swipe or self.config.get('swipe_wait', 2.0)
        for page in range(max(pages, 1)):
            console.print(f"[cyan]{self.config['name']} - 翻页 {page + 1}/{pages}[/cyan]")
            time.sleep(wait)
            self.d.swipe_ext("up", scale=0.78)

    def tap_product_tab(self) -> None:
        """通用商品tab点击"""
        texts = self.config.get('product_tab_texts', ['商品'])
        for text_val in texts:
            selectors = [
                self.d(text=text_val),
                self.d(textContains=text_val),
                self.d(descriptionContains=text_val),
            ]
            for selector in selectors:
                if selector.exists(timeout=2):
                    selector.click()
                    time.sleep(2)
                    return

    def get_ui_xml(self) -> str:
        """获取当前界面 XML 层次"""
        return self.d.dump_hierarchy()

    def _tap_search_entry(self) -> None:
        texts = self.config.get('search_entry_texts', ['搜索'])
        for text_val in texts:
            selectors = [
                self.d(descriptionContains=text_val),
                self.d(textContains=text_val),
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
            self.d(textContains="找一找"),
        ]
        for selector in selectors:
            if selector.exists(timeout=2):
                selector.click()
                return
        self.d.press("enter")


def create_device(platform_key: str, serial: str | None = None) -> EmulatorDevice:
    """工厂函数：创建指定平台的模拟器设备"""
    from shared.platform_configs import PLATFORM_EMULATOR_CONFIGS

    config = PLATFORM_EMULATOR_CONFIGS.get(platform_key)
    if not config:
        supported = list(PLATFORM_EMULATOR_CONFIGS.keys())
        raise ValueError(f"不支持的平台: {platform_key!r}. 支持: {', '.join(supported)}")
    return EmulatorDevice(platform_key, config, serial)
