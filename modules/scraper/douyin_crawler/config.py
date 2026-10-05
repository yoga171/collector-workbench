from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CrawlConfig:
    keyword: str
    pages: int
    excel_path: Path
    output_dir: Path
    serial: str | None = None
    package_name: str = "com.ss.android.ugc.aweme"
    proxy_port: int = 8080
    wait_after_search: float = 4.0
    wait_after_swipe: float = 2.2

    @property
    def capture_path(self) -> Path:
        return self.output_dir / "captured_products.jsonl"
