"""
视觉识别 API 服务 — 部署在小屋 RTX 4060 上
基于 Qwen2.5-VL-7B (Ollama)，替代 easyocr 实现电商图片 SKU 提取

启动: uvicorn vision_api_server:app --host 0.0.0.0 --port 8766
"""
import base64
import io
import json
import logging
import traceback
from typing import Optional

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from PIL import Image
import ollama

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("vision-api")

app = FastAPI(title="SKU Vision API", version="1.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MODEL = "qwen2.5vl:7b"
OLLAMA_HOST = "http://100.72.89.56:11434"  # Tailscale IP, visible to scheduled tasks

# ---- Lazy Ollama client (avoids blocking at import time) ----
_ollama_client = None

def _get_client():
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = ollama.Client(host=OLLAMA_HOST)
        logger.info(f"Ollama client initialized, host={OLLAMA_HOST}")
    return _ollama_client


# ============================================================
# Request/Response Models
# ============================================================

class SkuRequest(BaseModel):
    """base64 图片请求"""
    image_base64: str
    platform: Optional[str] = "unknown"  # taobao, jd, pdd, tmall, etc.

class SkuResult(BaseModel):
    success: bool
    title: str = ""
    price: str = ""
    spec: str = ""          # 规格 (尺寸/颜色/型号等)
    shop_name: str = ""      # 店铺名
    raw_text: str = ""       # 原始识别文本
    platform: str = ""
    error: str = ""

# ============================================================
# Core: call Qwen2.5-VL
# ============================================================

PROMPT_TEMPLATE = """你是一个电商商品图片分析器。请仔细查看这张商品截图/图片，提取以下信息并以JSON格式返回：

{
  "title": "商品标题/名称",
  "price": "价格（含货币符号，如¥29.9）",
  "spec": "规格信息（如颜色、尺寸、容量、型号等，多个用逗号分隔）",
  "shop_name": "店铺名称（如果有的话）",
  "description": "商品描述或卖点（如果有的话）",
  "discount": "优惠信息（如果有的话，如满减、券、限时折扣）"
}

注意：
- 只返回JSON，不要有其他文字
- 如果某个字段没有找到，填空字符串 ""
- 价格要保持原始格式，包括货币符号
- 规格要提取所有可见的选项
"""

def call_vision_model(image_bytes: bytes) -> dict:
    """调用 Qwen2.5-VL 识别商品图片"""
    img = Image.open(io.BytesIO(image_bytes))
    
    # 限制图片大小避免超时 (最大 2048px 长边)
    max_dim = 2048
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        new_size = (int(img.size[0] * ratio), int(img.size[1] * ratio))
        img = img.resize(new_size, Image.LANCZOS)
        logger.info(f"Resized image from {img.size} to {new_size}")
    
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    img_b64 = base64.b64encode(buf.getvalue()).decode()

    client = _get_client()
    try:
        response = client.chat(
            model=MODEL,
            messages=[{
                "role": "user",
                "content": PROMPT_TEMPLATE,
                "images": [img_b64]
            }],
            options={"temperature": 0.1, "num_predict": 1024}
        )
        raw = response["message"]["content"]
        logger.info(f"Model raw response: {raw[:300]}")
        return {"raw": raw, "success": True}
    except Exception as e:
        logger.error(f"Ollama call failed: {e}")
        return {"raw": "", "success": False, "error": str(e)}


def parse_llm_response(raw_text: str) -> dict:
    """从LLM返回的JSON字符串中提取结构化数据"""
    try:
        data = json.loads(raw_text)
        return data
    except json.JSONDecodeError:
        pass

    import re
    match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    
    return {"title": "", "price": "", "spec": "", "shop_name": "",
            "description": "", "discount": "", "_raw": raw_text}


# ============================================================
# API Endpoints
# ============================================================

@app.get("/api/vision/health")
async def health():
    """健康检查"""
    try:
        client = _get_client()
        models = client.list()
        model_names = [m["name"] for m in models.get("models", [])]
        has_model = any(MODEL.split(":")[0] in n for n in model_names)
        return {
            "status": "ok",
            "model": MODEL,
            "model_available": has_model,
            "models_loaded": model_names
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/vision/extract-sku", response_model=SkuResult)
async def extract_sku(req: SkuRequest):
    """从base64图片提取SKU信息"""
    try:
        image_bytes = base64.b64decode(req.image_base64)
    except Exception:
        raise HTTPException(400, "Invalid base64 image")
    
    result = call_vision_model(image_bytes)
    if not result["success"]:
        return SkuResult(success=False, error=result.get("error", "Model call failed"),
                         platform=req.platform)
    
    parsed = parse_llm_response(result["raw"])
    return SkuResult(
        success=True,
        title=parsed.get("title", ""),
        price=parsed.get("price", ""),
        spec=parsed.get("spec", ""),
        shop_name=parsed.get("shop_name", ""),
        raw_text=result["raw"],
        platform=req.platform
    )


@app.post("/api/vision/extract-sku-file")
async def extract_sku_file(file: UploadFile = File(...), platform: str = "unknown"):
    """从文件上传提取SKU信息"""
    image_bytes = await file.read()
    result = call_vision_model(image_bytes)
    if not result["success"]:
        return {"success": False, "error": result.get("error", "Model call failed")}
    
    parsed = parse_llm_response(result["raw"])
    return {
        "success": True,
        "title": parsed.get("title", ""),
        "price": parsed.get("price", ""),
        "spec": parsed.get("spec", ""),
        "shop_name": parsed.get("shop_name", ""),
        "description": parsed.get("description", ""),
        "discount": parsed.get("discount", ""),
        "raw_text": result["raw"],
        "platform": platform
    }


@app.post("/api/vision/batch")
async def batch_extract(requests: list[SkuRequest]):
    """批量提取 (最多10张)"""
    results = []
    for req in requests[:10]:
        try:
            image_bytes = base64.b64decode(req.image_base64)
            result = call_vision_model(image_bytes)
            parsed = parse_llm_response(result["raw"]) if result["success"] else {}
            results.append({
                "success": result["success"],
                "title": parsed.get("title", ""),
                "price": parsed.get("price", ""),
                "spec": parsed.get("spec", ""),
                "error": result.get("error", ""),
                "platform": req.platform
            })
        except Exception as e:
            results.append({"success": False, "error": str(e), "platform": req.platform})
    return {"results": results, "total": len(results)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8766, log_level="info")
