"""
CLI — 多平台商品采集命令行入口

支持抖音/快手/小红书等平台的 Android 模拟器商品采集。
通过 --platform 参数切换平台。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

from douyin_crawler import __version__
from douyin_crawler.config import CrawlConfig
from douyin_crawler.device import DouyinDevice  # 保留向后兼容
from douyin_crawler.dynamic_device import create_device, EmulatorDevice
from douyin_crawler.excel import jsonl_to_excel, rows_to_excel
from douyin_crawler.ui_scraper import extract_products_for_platform

# 导入共享平台配置
sys.path.insert(0, str(Path(__file__).parent.parent / 'shared'))
try:
    from platform_configs import PLATFORM_EMULATOR_CONFIGS
except ImportError:
    PLATFORM_EMULATOR_CONFIGS = {
        'douyin': {'name': '抖音', 'package_name': 'com.ss.android.ugc.aweme'},
    }

app = typer.Typer(help="Multi-platform crawler for Android Emulator, mitmproxy, adb and uiautomator2.")
console = Console()


@app.command()
def version() -> None:
    console.print(__version__)


@app.command()
def run(
    keyword: str = typer.Option(..., help="Product search keyword, for example nike"),
    pages: int = typer.Option(5, help="Swipe pages"),
    excel: Path = typer.Option(Path("output/products.xlsx"), help="Excel output file"),
    serial: str | None = typer.Option(None, help="adb serial, for example emulator-5554"),
    platform: str = typer.Option("douyin", help="Platform: douyin, kuaishou, xhs"),
    proxy_port: int = typer.Option(8080, help="mitmproxy listen port"),
) -> None:
    """API 拦截模式采集（需破解证书锁定）"""
    plat_cfg = PLATFORM_EMULATOR_CONFIGS.get(platform, {})
    if not plat_cfg:
        console.print(f"[red]不支持平台: {platform}[/red]")
        raise typer.Exit(code=1)

    config = CrawlConfig(
        keyword=keyword,
        pages=pages,
        excel_path=_excel_path(excel),
        output_dir=_excel_path(excel).parent,
        serial=serial,
        package_name=plat_cfg.get('package_name', ''),
        proxy_port=proxy_port,
        wait_after_search=plat_cfg.get('wait_after_search', 4.0),
        wait_after_swipe=plat_cfg.get('swipe_wait', 2.2),
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    if config.capture_path.exists():
        config.capture_path.unlink()

    mitm = start_mitmdump(config, platform)
    try:
        console.print(f"[cyan]Starting {plat_cfg['name']} automation (API mode)...[/cyan]")
        device = create_device(platform, serial)
        device.prepare()
        device.search(config.keyword)
        time.sleep(config.wait_after_search)
        device.swipe_pages(config.pages, config.wait_after_swipe)
        time.sleep(2)
    finally:
        mitm.terminate()
        try:
            mitm.wait(timeout=8)
        except subprocess.TimeoutExpired:
            mitm.kill()

    count = jsonl_to_excel(config.capture_path, config.excel_path)
    console.print(f"[green]Done.[/green] Exported {count} rows to {config.excel_path}")
    if count == 0:
        log_path = config.output_dir / "mitmproxy.log"
        if _has_certificate_trust_error(log_path):
            console.print(
                f"[yellow]{plat_cfg['name']} rejected the mitmproxy certificate. "
                "Use run-ui for visible page data.[/yellow]"
            )
        else:
            console.print("[yellow]No API products were parsed. Check mitmproxy visibility.[/yellow]")


@app.command("run-ui")
def run_ui(
    keyword: str = typer.Option(..., help="Product search keyword, for example nike"),
    pages: int = typer.Option(5, help="Swipe pages"),
    excel: Path = typer.Option(Path("output/products_ui.xlsx"), help="Excel output file"),
    serial: str | None = typer.Option(None, help="adb serial, for example emulator-5554"),
    platform: str = typer.Option("douyin", help="Platform: douyin, kuaishou, xhs"),
) -> None:
    """UI 可见文本采集模式（不需证书）"""
    plat_cfg = PLATFORM_EMULATOR_CONFIGS.get(platform, {})
    if not plat_cfg:
        console.print(f"[red]不支持平台: {platform}[/red]")
        raise typer.Exit(code=1)

    excel_path = _excel_path(excel)
    console.print(f"[cyan]Starting {plat_cfg['name']} UI-visible collection...[/cyan]")

    device = create_device(platform, serial)
    device.prepare()
    device.search(keyword)
    time.sleep(plat_cfg.get('wait_after_search', 4.0))
    device.tap_product_tab()

    rows = []
    for page in range(max(pages, 1)):
        console.print(f"[cyan]Reading visible page {page + 1}/{pages}[/cyan]")
        time.sleep(2)
        xml = device.get_ui_xml()
        rows.extend(extract_products_for_platform(platform, xml, keyword))
        device.d.swipe_ext("up", scale=0.78)

    count = rows_to_excel(rows, excel_path)
    console.print(f"[green]Done.[/green] Exported {count} visible rows to {excel_path}")
    if count == 0:
        console.print("[yellow]No visible products were parsed. Confirm the app loaded results.[/yellow]")


def start_mitmdump(config: CrawlConfig, platform: str = "douyin") -> subprocess.Popen:
    addon_path = Path(__file__).with_name("mitm_addon.py")
    log_path = config.output_dir / "mitmproxy.log"
    env = os.environ.copy()
    env["DOUYIN_CAPTURE_PATH"] = str(config.capture_path)
    env["DOUYIN_KEYWORD"] = config.keyword
    env["DOUYIN_PLATFORM"] = platform

    mitmdump_bin = Path(sys.executable).with_name("mitmdump.exe")
    command = [
        str(mitmdump_bin) if mitmdump_bin.exists() else "mitmdump",
        "-p",
        str(config.proxy_port),
        "-s",
        str(addon_path),
        "--set",
        "block_global=false",
    ]
    console.print(f"[cyan]Starting mitmdump on port {config.proxy_port}; log: {log_path}[/cyan]")
    try:
        log_file = log_path.open("w", encoding="utf-8", errors="replace")
        return subprocess.Popen(command, env=env, stdout=log_file, stderr=subprocess.STDOUT)
    except FileNotFoundError:
        console.print("[red]mitmdump not found. Install requirements in the project venv first.[/red]")
        raise typer.Exit(code=1)


def _excel_path(path: Path) -> Path:
    if path.parent in (Path(""), Path(".")):
        return Path("output") / path.name
    return path


def _has_certificate_trust_error(log_path: Path) -> bool:
    if not log_path.exists():
        return False
    text = log_path.read_text(encoding="utf-8", errors="ignore").lower()
    return "does not trust the proxy" in text or "certificate unknown" in text


if __name__ == "__main__":
    app()
