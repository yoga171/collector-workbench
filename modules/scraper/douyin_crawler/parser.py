from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any


TITLE_KEYS = ("title", "name", "product_name", "goods_name", "item_title", "desc")
ORIGINAL_PRICE_KEYS = ("origin_price", "original_price", "market_price", "list_price")
SALE_PRICE_KEYS = ("price", "sale_price", "real_price", "discount_price", "min_price")
PAY_COUNT_KEYS = ("pay_count", "sales", "sales_count", "sold", "sold_count", "sales_volume")
IMAGE_KEYS = ("image", "img", "cover", "pic", "thumb", "thumbnail")
ID_KEYS = ("product_id", "goods_id", "item_id", "commodity_id", "id")
SHOP_KEYS = ("shop_name", "shop", "seller_name", "author_name")


def extract_products(payload: Any, source_url: str) -> list[dict[str, Any]]:
    candidates = []
    for node in walk_dicts(payload):
        product = normalize_product(node, source_url)
        if product:
            candidates.append(product)
    return dedupe_products(candidates)


def walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def normalize_product(node: dict[str, Any], source_url: str) -> dict[str, Any] | None:
    title = pick_text(node, TITLE_KEYS)
    sale_price = pick_price(node, SALE_PRICE_KEYS)
    original_price = pick_price(node, ORIGINAL_PRICE_KEYS)
    image_url = pick_image(node)
    pay_count = pick_text(node, PAY_COUNT_KEYS)
    product_id = pick_text(node, ID_KEYS)
    shop_name = pick_text(node, SHOP_KEYS)

    if not title or not (sale_price or image_url or product_id):
        return None

    stable_key = product_id or f"{title}|{sale_price}|{image_url}"
    row_id = hashlib.sha1(stable_key.encode("utf-8", errors="ignore")).hexdigest()[:16]

    return {
        "row_id": row_id,
        "product_id": product_id or "",
        "title": title,
        "original_price": original_price or "",
        "sale_price": sale_price or "",
        "pay_count": pay_count or "",
        "image_url": image_url or "",
        "shop_name": shop_name or "",
        "source_url": source_url,
    }


def pick_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = find_key(node, key)
        if value is None:
            continue
        if isinstance(value, (str, int, float)):
            text = str(value).strip()
            if text:
                return text
    return ""


def pick_price(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    text = pick_text(node, keys)
    if not text:
        return ""
    if text.isdigit() and len(text) >= 3:
        return f"{int(text) / 100:.2f}"
    return text


def pick_image(node: dict[str, Any]) -> str:
    for key in IMAGE_KEYS:
        value = find_key(node, key)
        url = image_from_value(value)
        if url:
            return url
    return ""


def image_from_value(value: Any) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return value
    if isinstance(value, dict):
        for key in ("url", "uri", "origin_url", "url_list", "urls"):
            url = image_from_value(value.get(key))
            if url:
                return url
    if isinstance(value, list):
        for item in value:
            url = image_from_value(item)
            if url:
                return url
    return ""


def find_key(node: dict[str, Any], wanted: str) -> Any:
    for key, value in node.items():
        if key.lower() == wanted:
            return value
    return None


def dedupe_products(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    result = []
    for product in products:
        row_id = product["row_id"]
        if row_id in seen:
            continue
        seen.add(row_id)
        result.append(product)
    return result

