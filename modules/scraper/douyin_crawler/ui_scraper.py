from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass
from typing import Iterable
from xml.etree import ElementTree


PRICE_RE = re.compile(r"(?:¥|￥)\s*([0-9]+(?:\.[0-9]+)?)")
PAY_RE = re.compile(r"([0-9.]+万?|[0-9]+)\s*(?:人付款|已售|销量|付款)")
NOISE = {
    "搜索",
    "综合",
    "视频",
    "用户",
    "商品",
    "直播",
    "团购",
    "首页",
    "朋友",
    "消息",
    "我",
}


def extract_visible_products(xml: str, keyword: str) -> list[dict]:
    structured = _extract_search_cards(xml, keyword)
    if structured:
        return structured

    texts = _visible_texts(xml)
    rows: list[dict] = []
    seen_titles: set[str] = set()
    for index, text in enumerate(texts):
        price_match = PRICE_RE.search(text)
        if not price_match:
            continue
        title = _nearest_title(texts[:index])
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        window = texts[index : index + 5]
        rows.append(
            {
                "row_id": f"ui:{title}:{price_match.group(1)}",
                "keyword": keyword,
                "product_id": "",
                "title": title,
                "original_price": "",
                "sale_price": price_match.group(1),
                "pay_count": _first_match(PAY_RE, window),
                "image_url": "",
                "shop_name": _shop_name(window),
                "source_url": "ui://douyin/search",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "ui_visible",
            }
        )
    return rows or _fallback_visible_rows(texts, keyword)


@dataclass
class UiNode:
    text: str
    resource_id: str
    bounds: tuple[int, int, int, int]

    @property
    def center_x(self) -> int:
        return (self.bounds[0] + self.bounds[2]) // 2

    @property
    def center_y(self) -> int:
        return (self.bounds[1] + self.bounds[3]) // 2


def _extract_search_cards(xml: str, keyword: str) -> list[dict]:
    root = ElementTree.fromstring(xml)
    nodes = []
    for node in root.iter("node"):
        text = (node.get("text") or node.get("content-desc") or "").strip()
        if not text:
            continue
        nodes.append(UiNode(text, node.get("resource-id") or "", _parse_bounds(node.get("bounds") or "")))

    desc_nodes = [node for node in nodes if node.resource_id.endswith(":id/desc")]
    if not desc_nodes:
        return []

    rows = []
    for desc in desc_nodes:
        column_left = desc.bounds[0]
        column_right = desc.bounds[2]
        below = [
            node
            for node in nodes
            if column_left - 40 <= node.center_x <= column_right + 40
            and desc.bounds[3] <= node.center_y <= desc.bounds[3] + 220
        ]
        author = _first_resource(below, ":id/aca")
        publish_date = _first_resource(below, ":id/wbl")
        like_count = _first_resource(below, ":id/lb9")
        price = PRICE_RE.search(desc.text)
        rows.append(
            {
                "row_id": f"ui-card:{desc.text}:{author}:{publish_date}",
                "keyword": keyword,
                "product_id": "",
                "title": desc.text,
                "original_price": "",
                "sale_price": price.group(1) if price else "",
                "pay_count": "",
                "image_url": "",
                "shop_name": author,
                "source_url": "ui://douyin/search",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "ui_search_card",
                "like_count": like_count,
                "publish_date": publish_date,
            }
        )
    return rows


def _visible_texts(xml: str) -> list[str]:
    root = ElementTree.fromstring(xml)
    values: list[str] = []
    for node in root.iter("node"):
        for attr in ("text", "content-desc"):
            value = (node.get(attr) or "").strip()
            if value and value not in values:
                values.append(value)
    return values


def _parse_bounds(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value)
    if not match:
        return (0, 0, 0, 0)
    return tuple(int(item) for item in match.groups())


def _first_resource(nodes: list[UiNode], suffix: str) -> str:
    for node in nodes:
        if node.resource_id.endswith(suffix):
            return node.text
    return ""


def _nearest_title(previous: list[str]) -> str:
    for value in reversed(previous[-8:]):
        clean = value.strip()
        if not clean or clean in NOISE:
            continue
        if PRICE_RE.search(clean) or PAY_RE.search(clean):
            continue
        if len(clean) < 3:
            continue
        return clean
    return ""


def _first_match(pattern: re.Pattern[str], values: Iterable[str]) -> str:
    for value in values:
        match = pattern.search(value)
        if match:
            return match.group(0)
    return ""


def _shop_name(values: Iterable[str]) -> str:
    for value in values:
        if "店" in value or "旗舰" in value:
            return value
    return ""


def _fallback_visible_rows(texts: list[str], keyword: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for value in texts:
        clean = value.strip()
        if clean in seen or clean in NOISE:
            continue
        if len(clean) < 6 or re.fullmatch(r"[0-9:.]+", clean):
            continue
        if "按钮" in clean or "notification" in clean.lower():
            continue
        if keyword.lower() not in clean.lower() and "耐克" not in clean:
            continue
        seen.add(clean)
        rows.append(
            {
                "row_id": f"ui-visible:{clean}",
                "keyword": keyword,
                "product_id": "",
                "title": clean,
                "original_price": "",
                "sale_price": "",
                "pay_count": "",
                "image_url": "",
                "shop_name": "",
                "source_url": "ui://douyin/search",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "ui_visible_text",
            }
        )
    return rows


# ==============================
# 快手商品卡片解析
# ==============================

def extract_kuaishou_products(xml: str, keyword: str) -> list[dict]:
    """从快手商城/快手的 UI XML 中提取商品信息"""
    root = ElementTree.fromstring(xml)
    texts = _visible_texts(xml)
    rows: list[dict] = []
    seen_titles: set[str] = set()

    for index, text in enumerate(texts):
        price_match = PRICE_RE.search(text)
        if not price_match:
            continue
        title = _nearest_title(texts[:index])
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        window = texts[index : index + 5]
        rows.append(
            {
                "row_id": f"ks-ui:{title}:{price_match.group(1)}",
                "keyword": keyword,
                "product_id": "",
                "title": title,
                "original_price": "",
                "sale_price": price_match.group(1),
                "pay_count": _first_match(PAY_RE, window),
                "image_url": "",
                "shop_name": _shop_name(window),
                "source_url": "ui://kuaishou/search",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "kuaishou_ui",
            }
        )
    return rows or _fallback_visible_texts(texts, keyword, "ui://kuaishou/search")


# ==============================
# 小红书电商商品解析
# ==============================

XHS_PRICE_RE = re.compile(r"(?:¥|￥)\s*([0-9]+(?:\.[0-9]+)?)")
XHS_NOISE = {"搜索", "综合", "商品", "商城", "首页", "发现", "消息", "我", "购物车"}


def extract_xhs_products(xml: str, keyword: str) -> list[dict]:
    """从小红书 UI XML 中提取电商商品信息"""
    texts = _visible_texts(xml)
    rows: list[dict] = []
    seen_titles: set[str] = set()

    for index, text in enumerate(texts):
        price_match = XHS_PRICE_RE.search(text)
        if not price_match:
            continue
        title = _xhs_nearest_title(texts[:index])
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        window = texts[index : index + 5]
        rows.append(
            {
                "row_id": f"xhs-ui:{title}:{price_match.group(1)}",
                "keyword": keyword,
                "product_id": "",
                "title": title,
                "original_price": "",
                "sale_price": price_match.group(1),
                "pay_count": _first_match(PAY_RE, window),
                "image_url": "",
                "shop_name": _shop_name(window),
                "source_url": "ui://xiaohongshu/search",
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "xhs_ui",
            }
        )
    return rows or _fallback_visible_texts(texts, keyword, "ui://xiaohongshu/search")


def _xhs_nearest_title(previous: list[str]) -> str:
    for value in reversed(previous[-10:]):
        clean = value.strip()
        if not clean or clean in XHS_NOISE:
            continue
        if XHS_PRICE_RE.search(clean):
            continue
        if len(clean) < 3:
            continue
        return clean
    return ""


def _fallback_visible_texts(texts: list[str], keyword: str, source_url: str) -> list[dict]:
    """通用的 fallback 文本提取"""
    rows: list[dict] = []
    seen: set[str] = set()
    for value in texts:
        clean = value.strip()
        if clean in seen or clean in NOISE:
            continue
        if len(clean) < 6 or re.fullmatch(r"[0-9:.]+", clean):
            continue
        if "按钮" in clean or "notification" in clean.lower():
            continue
        seen.add(clean)
        rows.append(
            {
                "row_id": f"ui-visible:{clean}",
                "keyword": keyword,
                "product_id": "",
                "title": clean,
                "original_price": "",
                "sale_price": "",
                "pay_count": "",
                "image_url": "",
                "shop_name": "",
                "source_url": source_url,
                "captured_at": datetime.now().isoformat(timespec="seconds"),
                "capture_method": "ui_visible_text",
            }
        )
    return rows


# ==============================
# 平台分发
# ==============================

def extract_products_for_platform(platform_key: str, xml: str, keyword: str) -> list[dict]:
    """根据平台选择对应的 UI 解析函数"""
    extractors = {
        'douyin': extract_visible_products,
        'kuaishou': extract_kuaishou_products,
        'xhs': extract_xhs_products,
    }
    extractor = extractors.get(platform_key)
    if extractor:
        return extractor(xml, keyword)
    # 未知平台尝试通用提取
    return _fallback_visible_texts(_visible_texts(xml), keyword, f"ui://{platform_key}/search")
