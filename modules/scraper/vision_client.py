"""
视觉识别客户端 — 供 scraper-ui 爬虫模块调用
通过 Tailscale 访问小屋 RTX 4060 上的 Qwen2.5-VL 识别服务

小屋视觉API地址: http://100.72.89.56:8766

用法:
    from vision_client import VisionClient
    client = VisionClient()
    result = client.extract_sku(image_path="screenshot.png")
    # result: {"success": True, "title": "...", "price": "...", "spec": "...", ...}
"""
import base64
import json
import logging
from pathlib import Path
from typing import Optional, Union
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger("vision-client")

# 小屋视觉API地址 (Tailscale内网)
DEFAULT_VISION_API = "http://100.72.89.56:8766"


class VisionClient:
    """视觉识别客户端 — 替换 easyocr，使用小屋 RTX 4060 的视觉模型"""

    def __init__(self, api_base: str = DEFAULT_VISION_API, timeout: int = 30):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def health(self) -> dict:
        """检查小屋视觉服务是否在线"""
        try:
            req = Request(f"{self.api_base}/api/vision/health")
            resp = urlopen(req, timeout=5)
            return json.loads(resp.read())
        except Exception as e:
            return {"status": "error", "error": str(e)}

    def extract_sku(
        self,
        image_path: Optional[str] = None,
        image_bytes: Optional[bytes] = None,
        image_base64: Optional[str] = None,
        platform: str = "unknown"
    ) -> dict:
        """
        从商品图片中提取SKU信息（标题、价格、规格）
        
        Args:
            image_path: 本地图片路径
            image_bytes: 图片字节数据
            image_base64: base64编码的图片
            platform: 平台标识 (taobao, jd, pdd, tmall, etc.)
        
        Returns:
            {"success": True/False, "title": "", "price": "", "spec": "", ...}
        """
        # 获取图片 base64
        if image_base64:
            b64 = image_base64
        elif image_path:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
        elif image_bytes:
            b64 = base64.b64encode(image_bytes).decode()
        else:
            return {"success": False, "error": "No image provided"}

        payload = json.dumps({
            "image_base64": b64,
            "platform": platform
        }).encode("utf-8")

        try:
            req = Request(
                f"{self.api_base}/api/vision/extract-sku",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            resp = urlopen(req, timeout=self.timeout)
            return json.loads(resp.read())
        except HTTPError as e:
            return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
        except URLError as e:
            return {"success": False, "error": f"Connection failed: {e.reason}"}
        except Exception as e:
            logger.error(f"Vision extraction failed: {e}")
            return {"success": False, "error": str(e)}

    def extract_sku_file(self, file_path: str, platform: str = "unknown") -> dict:
        """通过文件上传提取SKU（备选方式）"""
        import uuid

        with open(file_path, "rb") as f:
            image_bytes = f.read()

        boundary = f"----{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="image.jpg"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n"
        ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()

        try:
            req = Request(
                f"{self.api_base}/api/vision/extract-sku-file?platform={platform}",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
            )
            resp = urlopen(req, timeout=self.timeout)
            return json.loads(resp.read())
        except Exception as e:
            return {"success": False, "error": str(e)}

    def batch_extract(self, images: list, platform: str = "unknown") -> dict:
        """批量提取多张图片的SKU信息"""
        requests_payload = []
        for img in images:
            if isinstance(img, str):
                with open(img, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
            elif isinstance(img, bytes):
                b64 = base64.b64encode(img).decode()
            else:
                b64 = img  # assume already base64
            requests_payload.append({"image_base64": b64, "platform": platform})

        try:
            req = Request(
                f"{self.api_base}/api/vision/batch",
                data=json.dumps(requests_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            resp = urlopen(req, timeout=self.timeout * 2)
            return json.loads(resp.read())
        except Exception as e:
            return {"success": False, "error": str(e), "results": []}


# ==============================
# 便捷函数（兼容 easyocr 调用习惯）
# ==============================

_default_client = None

def get_client() -> VisionClient:
    global _default_client
    if _default_client is None:
        _default_client = VisionClient()
    return _default_client


def extract_sku_from_image(image_path: str, platform: str = "unknown") -> dict:
    """便捷函数：从图片文件提取SKU，返回 dict"""
    return get_client().extract_sku(image_path=image_path, platform=platform)


def extract_sku_from_bytes(image_bytes: bytes, platform: str = "unknown") -> dict:
    """便捷函数：从bytes提取SKU"""
    return get_client().extract_sku(image_bytes=image_bytes, platform=platform)


# ==============================
# 统一入口：VisionRouter（供 scraper 各模块调用）
# ==============================

class VisionRouter:
    """
    视觉识别统一入口 — 所有图片识别请求经此路由
    
    支持自动 fallback：小屋GPU → 本地CPU
    """
    def __init__(self, remote_api: str = DEFAULT_VISION_API):
        self.remote = VisionClient(api_base=remote_api)
        self._fallback = None  # lazy: easyocr fallback
    
    def extract(self, image_path: str, platform: str = "unknown") -> dict:
        """提取SKU信息，自动 fallback"""
        # 优先使用小屋GPU
        result = self.remote.extract_sku(image_path=image_path, platform=platform)
        if result.get("success"):
            result["backend"] = "gpu-remote"
            return result
        
        # Fallback: 如果 easyocr 可用则用它
        try:
            if self._fallback is None:
                import easyocr
                self._fallback = easyocr.Reader(['ch_sim', 'en'], gpu=False)
            ocr_result = self._fallback.readtext(image_path)
            text = " ".join([r[1] for r in ocr_result])
            return {
                "success": True,
                "title": "",
                "price": "",
                "spec": "",
                "raw_text": text,
                "platform": platform,
                "backend": "cpu-fallback"
            }
        except ImportError:
            pass
        except Exception as e:
            logger.warning(f"Fallback OCR failed: {e}")
        
        result["backend"] = "failed"
        return result


# ==============================
# 测试
# ==============================
if __name__ == "__main__":
    client = VisionClient()
    
    # 健康检查
    print("=== Health Check ===")
    print(client.health())
    
    # 测试图片识别
    import sys
    if len(sys.argv) > 1:
        img_path = sys.argv[1]
        print(f"\n=== Extract SKU from: {img_path} ===")
        result = client.extract_sku(image_path=img_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("\nUsage: python vision_client.py <image_path>")
