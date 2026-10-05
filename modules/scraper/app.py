"""
Scraper-UI — 带前端的网页抓取工具
内容平台: 抖音 / 快手 / 视频号 / 小红书 / B站 / 微博 / 贴吧 / 知乎
电商平台: 淘宝 / 天猫 / 京东 / 拼多多 / 1688 / 抖音商城 / 快手商城 / 小红书商城
模拟器采集: 抖音商品 / 快手商品 / 小红书商品 (Android)
"""
import json
import os
import re
import sys
import random
import time
import sqlite3
import asyncio
import subprocess
import traceback
import threading
import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

BASE_DIR = Path(__file__).resolve().parent

# 导入共享平台配置（统一管理所有平台）
SHARED_DIR = BASE_DIR / 'shared'
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from platform_configs import (
    PLATFORM_PRESETS, auto_detect_platform,
    get_js_extract, make_extractor_js,
    SEARCH_CONFIGS,
)

# ==============================
# SQLite 数据持久化
# ==============================
DB_PATH = BASE_DIR / 'data.db'

def _now_iso():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _ensure_columns(conn, table, columns):
    existing = {row[1] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')


def _init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute('''CREATE TABLE IF NOT EXISTS scrape_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        platform TEXT, url TEXT, keyword TEXT, mode TEXT,
        shop_name TEXT, shop_id TEXT,
        item_count INTEGER DEFAULT 0, status TEXT DEFAULT 'success',
        error TEXT, data_json TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS crawler_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        updated_at TEXT, task_type TEXT DEFAULT 'crawler',
        platform TEXT, url TEXT, keyword TEXT, pages INTEGER, mode TEXT,
        serial TEXT, shop_name TEXT, shop_id TEXT,
        status TEXT DEFAULT 'running', item_count INTEGER DEFAULT 0,
        output_file TEXT, error TEXT, log_json TEXT,
        started_at TEXT, finished_at TEXT)''')
    _ensure_columns(conn, 'scrape_history', {
        'shop_name': 'TEXT',
        'shop_id': 'TEXT',
    })
    _ensure_columns(conn, 'crawler_jobs', {
        'updated_at': 'TEXT',
        'task_type': "TEXT DEFAULT 'crawler'",
        'platform': 'TEXT',
        'url': 'TEXT',
        'serial': 'TEXT',
        'shop_name': 'TEXT',
        'shop_id': 'TEXT',
        'error': 'TEXT',
        'log_json': 'TEXT',
        'started_at': 'TEXT',
        'finished_at': 'TEXT',
    })
    conn.commit(); conn.close()

def _save_scrape(platform, url, keyword, mode, results, error=None, shop_name='', shop_id=''):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute('''INSERT INTO scrape_history
        (created_at,platform,url,keyword,mode,shop_name,shop_id,item_count,status,error,data_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (datetime.datetime.now().isoformat(), platform, url or '', keyword or '',
         mode, shop_name or '', shop_id or '', len(results) if results else 0,
         'error' if error else 'success', error or '',
         json.dumps(results, ensure_ascii=False) if results else '[]'))
    conn.commit(); conn.close()

def _get_history(limit=50, offset=0):
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM scrape_history ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) FROM scrape_history').fetchone()[0]
    conn.close(); return [dict(r) for r in rows], total


def _create_job(task_type, platform='', url='', keyword='', pages=0, mode='',
                output_file='', serial='', status='running', shop_name='', shop_id=''):
    now = _now_iso()
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute('''INSERT INTO crawler_jobs
        (created_at, updated_at, started_at, task_type, platform, url, keyword,
         pages, mode, serial, shop_name, shop_id, status, item_count, output_file, error, log_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (now, now, now, task_type, platform or '', url or '', keyword or '',
         pages or 0, mode or '', serial or '', shop_name or '', shop_id or '',
         status, 0, output_file or '', '', '[]'))
    conn.commit()
    job_id = cur.lastrowid
    conn.close()
    return job_id


def _update_job(job_id, **fields):
    if not job_id:
        return
    allowed = {
        'updated_at', 'started_at', 'finished_at', 'task_type', 'platform',
        'url', 'keyword', 'pages', 'mode', 'serial', 'status', 'item_count',
        'output_file', 'error', 'log_json', 'shop_name', 'shop_id'
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates['updated_at'] = _now_iso()
    parts = ', '.join(f'{key}=?' for key in updates)
    values = list(updates.values()) + [job_id]
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(f'UPDATE crawler_jobs SET {parts} WHERE id=?', values)
    conn.commit(); conn.close()


def _get_jobs(limit=80, offset=0):
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM crawler_jobs ORDER BY id DESC LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) FROM crawler_jobs').fetchone()[0]
    conn.close()
    return [dict(r) for r in rows], total


def _clean_meta_text(value, limit=120):
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    return text[:limit]


def _guess_shop_name_from_text(text):
    text = _clean_meta_text(text, 3000)
    if not text:
        return ''
    patterns = [
        r'([\u4e00-\u9fa5A-Za-z0-9（）()·\-_]{2,60}(?:旗舰店|专卖店|专营店|企业店|个人店|官方店|店铺|小店))',
        r'店铺[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-_]{2,60})',
        r'店名[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·\-_]{2,60})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return _clean_meta_text(match.group(1))
    return ''


def _merchant_shop_meta(data, records=None):
    shop_name = _clean_meta_text(data.get('shop_name') or data.get('shop') or data.get('store_name'))
    shop_id = _clean_meta_text(data.get('shop_id') or data.get('store_id'), 80)
    platform_names = {
        '\u6296\u5e97', '\u5343\u725b', '\u6dd8\u5b9d', '\u6dd8\u5b9d\u5356\u5bb6\u4e2d\u5fc3',
        '\u6296\u5e97\u540e\u53f0', '\u5343\u725b\u5de5\u4f5c\u53f0',
    }
    strong_markers = (
        '\u516c\u53f8', '\u4f01\u4e1a\u5e97', '\u65d7\u8230\u5e97',
        '\u4e13\u5356\u5e97', '\u4e13\u8425\u5e97', '\u5b98\u65b9\u5e97',
    )
    candidates = []
    for candidate in data.get('shop_candidates') or []:
        candidate = _clean_meta_text(candidate)
        if candidate and len(candidate) <= 80 and candidate not in platform_names:
            candidates.append(candidate)
    if not shop_name or shop_name in platform_names:
        shop_name = next((item for item in candidates if any(marker in item for marker in strong_markers)), '')
    if not shop_name:
        shop_name = next((item for item in candidates if '\u5e97' in item), '')
    if not shop_name:
        shop_name = candidates[0] if candidates else ''
    if not shop_name:
        for record in records or []:
            if isinstance(record, dict):
                shop_name = _guess_shop_name_from_text(' '.join(str(v) for v in record.values()))
                if shop_name:
                    break
    if not shop_name:
        shop_name = _guess_shop_name_from_text(data.get('body_text') or data.get('title') or '')
    return shop_name, shop_id


def _tag_records_with_shop(records, shop_name='', shop_id=''):
    if not shop_name and not shop_id:
        return records
    tagged = []
    for item in records or []:
        if isinstance(item, dict):
            row = dict(item)
            if shop_name:
                row.setdefault('shop_name', shop_name)
            if shop_id:
                row.setdefault('shop_id', shop_id)
            tagged.append(row)
        else:
            tagged.append(item)
    return tagged


def _read_excel_row_count(path):
    try:
        if not path or not Path(path).exists():
            return 0
        import pandas as pd
        return len(pd.read_excel(path))
    except Exception:
        return 0


def _latest_excel_result(root):
    try:
        root = Path(root)
        if not root.exists():
            return '', 0
        files = sorted(root.rglob('*.xlsx'), key=os.path.getmtime, reverse=True)
        if not files:
            return '', 0
        return str(files[0]), _read_excel_row_count(files[0])
    except Exception:
        return '', 0

_init_db()

COOKIES_DIR = BASE_DIR / 'cookies'
COOKIES_DIR.mkdir(exist_ok=True)

MERCHANT_PLATFORM_CONFIGS = {
    'taobao_seller': {
        'name': '淘宝卖家中心',
        'short_name': '淘宝',
        'icon': '🛒',
        'homepage': 'https://myseller.taobao.com/home.htm',
        'login_url': 'https://myseller.taobao.com/home.htm',
        'note': '适合采集淘宝店铺后台的商品、订单、评价、经营数据页面。',
        'data_types': {
            'products': '商品/宝贝',
            'orders': '订单',
            'aftersales': '售后',
            'reviews': '评价',
            'analytics': '经营数据',
            'custom': '自定义页面',
        },
    },
    'qianniu': {
        'name': '千牛工作台',
        'short_name': '千牛',
        'icon': '🐮',
        'homepage': 'https://work.taobao.com/',
        'login_url': 'https://work.taobao.com/',
        'note': '适合采集千牛工作台内可见的店铺、客服、商品和经营数据。',
        'data_types': {
            'products': '商品',
            'orders': '订单',
            'customer_service': '客服',
            'analytics': '经营数据',
            'custom': '自定义页面',
        },
    },
    'doudian': {
        'name': '抖店后台',
        'short_name': '抖店',
        'icon': '🎵',
        'homepage': 'https://fxg.jinritemai.com/index.html',
        'login_url': 'https://fxg.jinritemai.com/index.html',
        'note': '适合采集抖店后台的商品、订单、售后、直播/经营数据页面。',
        'data_types': {
            'products': '商品',
            'orders': '订单',
            'aftersales': '售后',
            'analytics': '经营数据',
            'custom': '自定义页面',
        },
    },
}

# 平台预设已移至 shared/platform_configs.py
# PLATFORM_PRESETS, auto_detect_platform, get_js_extract, make_extractor_js
# 均在文件顶部 import 完成



# ==============================
# 辅助函数
# ==============================

def _load_cookies(platform_key):
    """加载已保存的 cookies"""
    cookie_file = COOKIES_DIR / f'{platform_key}.json'
    state_file = COOKIES_DIR / f'{platform_key}_state.json'
    if state_file.exists():
        return str(state_file)
    if cookie_file.exists():
        return str(cookie_file)
    return None


def _save_cookies(platform_key, storage_state):
    """保存 cookies 到文件"""
    state_file = COOKIES_DIR / f'{platform_key}_state.json'
    with open(state_file, 'w', encoding='utf-8') as f:
        f.write(json.dumps(storage_state, ensure_ascii=False))
    return str(state_file)


def _list_sessions():
    """列出所有已保存的会话"""
    sessions = []
    for f in COOKIES_DIR.glob('*_state.json'):
        platform = f.stem.replace('_state', '')
        preset = PLATFORM_PRESETS.get(platform) or MERCHANT_PLATFORM_CONFIGS.get(platform, {})
        sessions.append({
            'platform': platform,
            'name': preset.get('name', platform),
            'file': str(f),
            'size': f.stat().st_size,
            'mtime': f.stat().st_mtime
        })
    return sessions


# ==============================
# 仿真模式 — 模拟真人操作
# ==============================

VIEWPORT_PRESETS = [
    {'width': 1366, 'height': 768},
    {'width': 1440, 'height': 900},
    {'width': 1536, 'height': 864},
    {'width': 1280, 'height': 800},
    {'width': 1920, 'height': 1080},
]

COMMON_UAS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
]

# 移动端设备模拟预设
DEVICE_PRESETS = {
    'pc': {
        'name': 'PC 桌面',
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'viewport': {'width': 1280, 'height': 720},
        'device_scale_factor': 1,
        'has_touch': False,
        'is_mobile': False,
    },
    'iphone15': {
        'name': 'iPhone 15 Pro',
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
        'viewport': {'width': 390, 'height': 844},
        'device_scale_factor': 3,
        'has_touch': True,
        'is_mobile': True,
    },
    'iphone14': {
        'name': 'iPhone 14',
        'user_agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
        'viewport': {'width': 390, 'height': 844},
        'device_scale_factor': 3,
        'has_touch': True,
        'is_mobile': True,
    },
    'android_s24': {
        'name': 'Samsung S24',
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; SM-S921B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36',
        'viewport': {'width': 412, 'height': 915},
        'device_scale_factor': 2.75,
        'has_touch': True,
        'is_mobile': True,
    },
    'android_pixel': {
        'name': 'Google Pixel 8',
        'user_agent': 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.135 Mobile Safari/537.36',
        'viewport': {'width': 412, 'height': 915},
        'device_scale_factor': 2.625,
        'has_touch': True,
        'is_mobile': True,
    },
}

BROWSER_PROFILE_DIR = BASE_DIR / 'browser_profile'


def _human_delay(min_ms=500, max_ms=2000):
    """随机等待，模拟人的反应时间"""
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _random_viewport():
    """随机选一个实际屏幕分辨率"""
    return random.choice(VIEWPORT_PRESETS)


def _random_ua():
    """随机选一个 User-Agent"""
    return random.choice(COMMON_UAS)


def _human_scroll(page):
    """模拟真人滚动页面：分段、变速、随机停顿"""
    try:
        max_height = page.evaluate('document.body.scrollHeight')
        if max_height < 500:
            return
        # 分 2-4 段滚动
        steps = random.randint(2, 4)
        for i in range(steps):
            target = int(max_height * (i + 1) / steps * random.uniform(0.6, 0.95))
            page.evaluate(f'window.scrollTo({{top: {target}, behavior: "smooth"}})')
            time.sleep(random.uniform(0.8, 2.5))
            # 偶尔停顿看内容
            if random.random() < 0.3:
                time.sleep(random.uniform(0.5, 1.5))
        # 滚回顶部
        page.evaluate('window.scrollTo({top: 0, behavior: "smooth"})')
        time.sleep(random.uniform(0.3, 0.8))
    except Exception:
        pass


def _human_mouse_move(page):
    """模拟鼠标在页面上随机移动"""
    try:
        viewport = page.viewport_size
        w, h = viewport['width'], viewport['height']
        # 移动几次鼠标到随机位置
        for _ in range(random.randint(2, 5)):
            x = random.randint(50, w - 50)
            y = random.randint(50, h - 50)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.1, 0.4))
    except Exception:
        pass


def _inject_stealth(page):
    """注入反检测脚本，隐藏自动化痕迹"""
    page.add_init_script("""
        // 隐藏 webdriver 标志
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // 伪造 chrome 属性
        window.chrome = {
            runtime: {}, loadTimes: function(){},
            csi: function(){}, app: {}
        };

        // 覆盖 plugins
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });

        // 覆盖 languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['zh-CN', 'zh', 'en']
        });

        // 覆盖 permissions
        const originalQuery = navigator.permissions.query;
        navigator.permissions.query = (params) => (
            params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(params)
        );
    """)


def _create_context(p, platform_key=None, headless=True, human_mode=False,
                    use_system_chrome=False, use_profile=False,
                    device='pc', proxy_url=None, use_cdp=False):
    """创建浏览器上下文，支持多种防检测模式"""
    device_cfg = DEVICE_PRESETS.get(device, DEVICE_PRESETS['pc'])
    state_file = _load_cookies(platform_key) if platform_key else None

    # === CDP 模式：连接用户正在用的 Chrome ===
    if use_cdp:
        try:
            browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
            # 用已有浏览器上下文（复用登录态、cookie）
            contexts = browser.contexts
            if contexts:
                context = contexts[0]
            else:
                context = browser.new_context()
            return browser, context
        except Exception as e:
            raise RuntimeError(
                f'CDP 连接失败: {e}\n'
                '请先启动 Chrome 远程调试:\n'
                '"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222'
            )

    # === 常规模式 ===
    args = ['--disable-blink-features=AutomationControlled', '--no-sandbox']
    if not human_mode:
        args.append('--disable-web-security')
    if use_system_chrome and not use_profile:
        args.extend(['--disable-sync', '--no-default-browser-check'])

    # 确定 viewport 和 UA
    if human_mode and device == 'pc':
        viewport = _random_viewport()
        ua = _random_ua()
        dsf = random.choice([1, 1.25, 1.5, 2])
        touch = random.choice([True, False])
    else:
        viewport = device_cfg['viewport']
        ua = device_cfg['user_agent']
        dsf = device_cfg['device_scale_factor']
        touch = device_cfg['has_touch']

    proxy = {'server': proxy_url} if proxy_url else None

    launch_kwargs = {'headless': headless, 'args': args}
    if use_system_chrome:
        launch_kwargs['channel'] = 'chrome'

    browser = p.chromium.launch(**launch_kwargs)

    ctx_kwargs = {
        'user_agent': ua,
        'viewport': viewport,
        'locale': 'zh-CN',
        'timezone_id': 'Asia/Shanghai',
        'geolocation': {'latitude': 23.1291, 'longitude': 113.2644},
        'permissions': ['geolocation'],
        'device_scale_factor': dsf,
        'has_touch': touch,
        'is_mobile': device_cfg.get('is_mobile', False),
    }
    if state_file:
        ctx_kwargs['storage_state'] = state_file
    if proxy:
        ctx_kwargs['proxy'] = proxy

    if use_profile:
        BROWSER_PROFILE_DIR.mkdir(exist_ok=True)
        context = browser.new_context(**ctx_kwargs)
        _seed_browser_profile(page=None, context=context)
    else:
        context = browser.new_context(**ctx_kwargs)

    return browser, context


def _seed_browser_profile(page, context):
    """给浏览器档案注入「长期使用」的痕迹"""
    try:
        if not page:
            page = context.new_page()
        page.goto('about:blank')
        # 模拟访问过一些常见网站
        visited_sites = [
            'https://www.baidu.com',
            'https://www.zhihu.com',
            'https://www.bilibili.com',
        ]
        for site in visited_sites:
            try:
                page.goto(site, wait_until='domcontentloaded', timeout=5000)
                page.wait_for_timeout(random.randint(500, 1500))
            except Exception:
                pass
        # 设置一些 localStorage 值
        page.evaluate("""() => {
            try {
                localStorage.setItem('visited', Date.now().toString());
                localStorage.setItem('theme', 'light');
            } catch(e) {}
        }""")
        page.wait_for_timeout(500)
    except Exception:
        pass


def _safe_goto(page, url, timeout=60000, human_mode=False):
    """安全导航：避开 networkidle 陷阱，增加容错"""
    if human_mode:
        return _human_goto(page, url, timeout)

    try:
        page.goto(url, wait_until='domcontentloaded', timeout=timeout)
        page.wait_for_selector('body', timeout=10000)
        try:
            page.wait_for_load_state('load', timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state('networkidle', timeout=15000)
        except Exception:
            pass
    except Exception as e:
        if 'Timeout' in str(e):
            page.goto(url, wait_until='load', timeout=timeout+30000)
            page.wait_for_timeout(5000)
        else:
            raise


def _human_goto(page, url, timeout=60000):
    """仿真导航：完全模拟真人访问网页的流程"""
    # 1. 注入反检测脚本
    _inject_stealth(page)

    # 2. 先「思考」一下再开始
    _human_delay(300, 1000)

    # 3. 加载页面
    page.goto(url, wait_until='domcontentloaded', timeout=timeout)
    page.wait_for_selector('body', timeout=10000)
    try:
        page.wait_for_load_state('load', timeout=20000)
    except Exception:
        pass

    # 4. 页面加载后停顿，模拟阅读
    _human_delay(1000, 3000)

    # 5. 随机鼠标移动
    _human_mouse_move(page)

    # 6. 滚动浏览内容
    _human_scroll(page)

    # 7. 再停留一会儿
    _human_delay(500, 1500)

    # 8. 等网络空闲（不强求）
    try:
        page.wait_for_load_state('networkidle', timeout=10000)
    except Exception:
        pass


def _intercept_api(page, api_patterns, timeout=15000):
    """拦截指定 URL 模式的 API 响应，返回捕获的数据"""
    captured = []
    def handle_response(response):
        url = response.url
        for pattern in api_patterns:
            if pattern in url and response.status == 200:
                try:
                    body = response.json()
                    captured.append({'url': url, 'data': body})
                except Exception:
                    pass
    page.on('response', handle_response)
    page.wait_for_timeout(timeout)
    return captured


def _extract_douyin_from_api(captured):
    """从截获的抖音 API 响应中提取视频数据"""
    data = {}
    for item in captured:
        body = item['data']
        aweme_list = (
            body.get('aweme_detail')
            or (body.get('item_list') or [None])[0]
            or (body.get('aweme_list') or [None])[0]
            or body
        )
        if isinstance(aweme_list, dict):
            desc = aweme_list.get('desc') or ''
            author = (aweme_list.get('author') or {}).get('nickname', '') or ''
            stats = aweme_list.get('statistics') or {}
            if desc: data['title'] = desc
            if author: data['author'] = author
            if stats.get('digg_count'): data['likes'] = str(stats['digg_count'])
            if stats.get('comment_count'): data['comments'] = str(stats['comment_count'])
            if stats.get('share_count'): data['shares'] = str(stats['share_count'])
            duration = aweme_list.get('duration') or aweme_list.get('video', {}).get('duration', 0)
            if duration: data['duration'] = f'{duration//1000}秒'
    return data


def _extract_by_css(page, selectors):
    """通过 CSS 选择器提取数据"""
    results = []
    for sel in selectors:
        name = sel.get('name', sel.get('key', '未命名'))
        css = sel.get('css', '')
        attr = sel.get('attr', 'text')

        if not css:
            results.append({'key': sel.get('key', name), 'name': name, 'count': 0, 'items': [], 'skipped': True})
            continue

        try:
            elements = page.query_selector_all(css)
        except Exception:
            elements = []

        items = []
        for el in elements:
            try:
                if attr == 'text':
                    items.append(el.inner_text().strip())
                elif attr == 'html':
                    items.append(el.inner_html())
                elif attr == 'href':
                    items.append(el.get_attribute('href') or '')
                elif attr == 'src':
                    items.append(el.get_attribute('src') or '')
                else:
                    items.append(el.get_attribute(attr) or el.inner_text().strip())
            except Exception:
                items.append('')

        results.append({
            'key': sel.get('key', name),
            'name': name,
            'css': css,
            'count': len(items),
            'items': items
        })
    return results


def _js_extract(page, js_code):
    """注入 JS 提取结构化数据"""
    try:
        result = page.evaluate(js_code)
        if result:
            parsed = json.loads(result)
            return {k: v for k, v in parsed.items() if v}
    except Exception:
        pass
    return {}


def _smart_extract(page):
    """智能提取: 尝试多种方式获取页面数据"""
    result = {}

    # 1. 标题
    for sel in ['h1', 'title', 'meta[property="og:title"]', 'meta[name="title"]']:
        try:
            if sel.startswith('meta'):
                val = page.get_attribute(sel, 'content')
            else:
                val = page.inner_text(sel)
            if val:
                result['page_title'] = val.strip()
                break
        except:
            continue

    # 2. 描述
    for sel in ['meta[name="description"]', 'meta[property="og:description"]']:
        try:
            val = page.get_attribute(sel, 'content')
            if val:
                result['description'] = val.strip()
                break
        except:
            continue

    # 3. 所有文本段落
    try:
        texts = [el.inner_text().strip() for el in page.query_selector_all('p, h2, h3, li, .content, .desc') if el.inner_text().strip()]
        if texts:
            result['text_blocks'] = texts[:50]
    except:
        pass

    # 4. 所有链接
    try:
        links = []
        for a in page.query_selector_all('a[href]'):
            href = a.get_attribute('href')
            text = a.inner_text().strip()
            if href and href.startswith('http'):
                links.append({'text': text, 'href': href})
        if links:
            result['links'] = links[:30]
    except:
        pass

    # 5. 所有图片
    try:
        imgs = []
        for img in page.query_selector_all('img[src]'):
            src = img.get_attribute('src')
            alt = img.get_attribute('alt') or ''
            if src and (src.startswith('http') or src.startswith('//')):
                imgs.append({'src': src, 'alt': alt})
        if imgs:
            result['images'] = imgs[:20]
    except:
        pass

    # 6. JSON-LD 结构化数据
    try:
        for script in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                data = json.loads(script.inner_text())
                if isinstance(data, dict):
                    result['json_ld'] = data
                elif isinstance(data, list) and len(data) > 0:
                    result['json_ld'] = data[0]
            except:
                pass
            if result.get('json_ld'):
                break
    except:
        pass

    return result


# ==============================
# API 路由
# ==============================

@app.route('/')
def index():
    return render_template('index.html')


# ========== 环境自检 ==========

@app.route('/api/health')
def health_check():
    """环境自检"""
    checks = {}

    # Python
    checks['python'] = {'ok': True, 'version': sys.version.split()[0]}

    # Node.js
    try:
        r = subprocess.run(['node', '--version'], capture_output=True, text=True, timeout=5)
        checks['node'] = {'ok': r.returncode == 0, 'version': r.stdout.strip()}
    except: checks['node'] = {'ok': False, 'error': '未安装'}

    # Playwright 浏览器
    pw_dir = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH', str(Path.home() / 'AppData' / 'Local' / 'ms-playwright')))
    browsers = [d.name for d in pw_dir.iterdir() if d.is_dir() and 'chromium' in d.name] if pw_dir.exists() else []
    checks['playwright'] = {'ok': len(browsers) > 0, 'browsers': browsers}

    # ADB（搜索多个可能的位置）
    ADB_PATHS = [
        'adb',
        str(BASE_DIR.parent / 'douyin_product_crawler' / 'platform-tools' / 'adb.exe'),
        str(BASE_DIR.parent / 'douyin_product_crawler' / 'android-sdk' / 'platform-tools' / 'adb.exe'),
        str(Path.home() / 'AppData' / 'Local' / 'Android' / 'Sdk' / 'platform-tools' / 'adb.exe'),
    ]
    adb_found = False
    adb_version = ''
    for adb_path in ADB_PATHS:
        try:
            r = subprocess.run([adb_path, 'version'], capture_output=True, text=True, timeout=5)
            if r.returncode == 0:
                adb_found = True
                adb_version = r.stdout.split('\n')[0] if r.stdout else ''
                # 如果找到的不是系统PATH里的，就加到PATH里
                if adb_path != 'adb':
                    os.environ['PATH'] = str(Path(adb_path).parent) + os.pathsep + os.environ.get('PATH', '')
                break
        except:
            continue
    checks['adb'] = {'ok': adb_found, 'version': adb_version if adb_version else '未安装',
                     'path': [p for p in ADB_PATHS[1:] if Path(p).exists()] if not adb_found else []}

    # CDP 端口
    import socket
    cdp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cdp_ok = cdp_sock.connect_ex(('127.0.0.1', 9222)) == 0
    cdp_sock.close()
    chrome_available = any(Path(p).exists() for p in CHROME_PATHS)
    checks['cdp'] = {'ok': cdp_ok, 'port': 9222,
                     'chrome_available': chrome_available,
                     'hint': '已就绪' if cdp_ok else ('Chrome 未以调试模式启动，点击底部「CDP 帮助」可一键启动' if chrome_available else '未找到 Chrome 浏览器')}

    # 磁盘
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(BASE_DIR))
        checks['disk'] = {'ok': free > 500 * 1024 * 1024, 'free_gb': round(free / (1024**3), 1)}
    except: checks['disk'] = {'ok': True, 'free_gb': '?'}

    all_ok = all(v.get('ok', False) for v in checks.values())
    return jsonify({'ok': all_ok, 'checks': checks})


# ========== CDP 模式 ==========

CHROME_PATHS = [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    os.path.expanduser('~/AppData/Local/Google/Chrome/Application/chrome.exe'),
]

@app.route('/api/cdp/start', methods=['POST'])
def cdp_start():
    """启动 Chrome CDP 模式"""
    chrome_path = None
    for p in CHROME_PATHS:
        if os.path.exists(p):
            chrome_path = p; break
    if not chrome_path:
        return jsonify({'success': False, 'error': '未找到 Chrome 浏览器'})

    try:
        subprocess.Popen(
            [chrome_path, '--remote-debugging-port=9222'],
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        return jsonify({'success': True, 'message': 'Chrome 已启动（端口 9222）'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/cdp/check')
def cdp_check():
    """检查 CDP 连接是否可用"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222')
            contexts = browser.contexts
            pages = sum(len(ctx.pages) for ctx in contexts)
            browser.close()
        return jsonify({'connected': True, 'pages': pages})
    except Exception:
        return jsonify({'connected': False})


# ========== 历史记录 ==========

@app.route('/api/history')
def get_history():
    """查询抓取历史"""
    limit = request.args.get('limit', 50, type=int)
    offset = request.args.get('offset', 0, type=int)
    rows, total = _get_history(limit, offset)
    return jsonify({'data': rows, 'total': total, 'limit': limit, 'offset': offset})


@app.route('/api/history/<int:hid>')
def get_history_detail(hid):
    """查看单条历史详情"""
    conn = sqlite3.connect(str(DB_PATH)); conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM scrape_history WHERE id=?', (hid,)).fetchone()
    conn.close()
    if not row: return jsonify({'success': False, 'error': '不存在'})
    d = dict(row)
    d['data'] = json.loads(d.get('data_json', '[]'))
    return jsonify(d)


@app.route('/api/history/<int:hid>', methods=['DELETE'])
def delete_history(hid):
    """删除历史记录"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute('DELETE FROM scrape_history WHERE id=?', (hid,))
    conn.commit(); conn.close()
    return jsonify({'success': True})


def _history_task_type(row):
    mode = row.get('mode') or ''
    if mode.startswith('video'):
        return 'video_download'
    if mode.startswith('competitor'):
        return 'competitor'
    if mode.startswith('merchant'):
        return 'merchant'
    if mode == 'search':
        return 'web_search'
    if mode == 'batch':
        return 'quick_batch'
    if (row.get('url') or '').startswith('shop:'):
        return 'shop_crawl'
    return 'quick_scrape'


@app.route('/api/tasks')
def list_tasks():
    """统一任务中心：合并长任务和网页抓取历史。"""
    limit = min(request.args.get('limit', 80, type=int), 200)
    jobs, jobs_total = _get_jobs(limit=limit, offset=0)
    history, history_total = _get_history(limit=limit, offset=0)

    items = []
    job_fingerprints = set()
    for job in jobs:
        job_fingerprints.add((
            job.get('platform') or '',
            job.get('shop_name') or '',
            job.get('shop_id') or '',
            job.get('url') or '',
            job.get('keyword') or '',
            job.get('mode') or '',
            job.get('status') or 'unknown',
            job.get('item_count') or 0,
        ))
        items.append({
            'id': f'job-{job["id"]}',
            'source': 'crawler_jobs',
            'raw_id': job['id'],
            'created_at': job.get('created_at') or '',
            'updated_at': job.get('updated_at') or '',
            'started_at': job.get('started_at') or '',
            'finished_at': job.get('finished_at') or '',
            'task_type': job.get('task_type') or 'crawler',
            'platform': job.get('platform') or '',
            'shop_name': job.get('shop_name') or '',
            'shop_id': job.get('shop_id') or '',
            'url': job.get('url') or '',
            'keyword': job.get('keyword') or '',
            'pages': job.get('pages') or 0,
            'mode': job.get('mode') or '',
            'serial': job.get('serial') or '',
            'status': job.get('status') or 'unknown',
            'item_count': job.get('item_count') or 0,
            'output_file': job.get('output_file') or '',
            'error': job.get('error') or '',
        })

    for row in history:
        history_fingerprint = (
            row.get('platform') or '',
            row.get('shop_name') or '',
            row.get('shop_id') or '',
            row.get('url') or '',
            row.get('keyword') or '',
            row.get('mode') or '',
            row.get('status') or 'success',
            row.get('item_count') or 0,
        )
        if history_fingerprint in job_fingerprints:
            continue
        items.append({
            'id': f'history-{row["id"]}',
            'source': 'scrape_history',
            'raw_id': row['id'],
            'created_at': row.get('created_at') or '',
            'updated_at': row.get('created_at') or '',
            'started_at': row.get('created_at') or '',
            'finished_at': row.get('created_at') or '',
            'task_type': _history_task_type(row),
            'platform': row.get('platform') or '',
            'shop_name': row.get('shop_name') or '',
            'shop_id': row.get('shop_id') or '',
            'url': row.get('url') or '',
            'keyword': row.get('keyword') or '',
            'pages': '',
            'mode': row.get('mode') or '',
            'serial': '',
            'status': row.get('status') or 'success',
            'item_count': row.get('item_count') or 0,
            'output_file': '',
            'error': row.get('error') or '',
        })

    items.sort(key=lambda item: item.get('created_at') or '', reverse=True)
    return jsonify({
        'data': items[:limit],
        'total': jobs_total + history_total,
        'limit': limit,
    })


@app.route('/api/platforms')
def get_platforms():
    """获取所有支持的平台信息"""
    platforms = {}
    for key, p in PLATFORM_PRESETS.items():
        platforms[key] = {
            'name': p['name'],
            'icon': p['icon'],
            'color': p['color'],
            'homepage': p['homepage'],
            'note': p['note'],
            'fields': [{'key': f['key'], 'label': f['label']} for f in p['fields']]
        }
    return jsonify(platforms)


def _merchant_session_exists(platform):
    return (COOKIES_DIR / f'{platform}_state.json').exists() or (COOKIES_DIR / f'{platform}.json').exists()


def _merchant_diagnostics(data):
    platform = data.get('platform') or ''
    data_type = data.get('data_type') or 'custom'
    url = (data.get('url') or '').strip()
    human_mode = bool(data.get('human_mode'))
    cfg = MERCHANT_PLATFORM_CONFIGS.get(platform)
    session_ok = _merchant_session_exists(platform) if platform else False
    target_url = url or (cfg or {}).get('homepage', '')

    checks = {
        'platform': {
            'ok': bool(cfg),
            'message': cfg['name'] if cfg else '请选择支持的商家后台平台',
        },
        'data_type': {
            'ok': bool(cfg and data_type in cfg.get('data_types', {})),
            'message': (cfg.get('data_types', {}).get(data_type) if cfg else '') or data_type,
        },
        'session': {
            'ok': session_ok or human_mode,
            'message': '已有登录会话' if session_ok else ('可见浏览器模式可手动登录' if human_mode else '尚未保存登录会话'),
        },
        'url': {
            'ok': bool(target_url),
            'message': target_url or '请输入后台页面地址',
        },
    }
    ok = all(item['ok'] for item in checks.values())
    recommendations = []
    if not checks['session']['ok']:
        recommendations.append('先点击“保存登录态”，或勾选可见浏览器模式后在弹出的浏览器里手动登录。')
    if cfg and data_type != 'custom' and not url:
        recommendations.append('默认会进入平台首页；如果要抓具体商品/订单/数据页面，建议粘贴登录后的后台页面 URL。')
    if cfg:
        recommendations.append('第一版采集页面可见表格、卡片和指标文本；复杂接口字段后续可按页面单独适配。')
    return {
        'ok': ok,
        'checks': checks,
        'recommendations': recommendations,
        'platform': platform,
        'data_type': data_type,
        'target_url': target_url,
        'session_saved': session_ok,
    }


def _extract_merchant_visible_data(page):
    return page.evaluate("""
    () => {
        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
        const clip = (value, limit = 500) => {
            const text = clean(value);
            return text.length > limit ? text.slice(0, limit) + '...' : text;
        };
        const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        };
        const records = [];
        const tables = [];

        document.querySelectorAll('table').forEach((table, tableIndex) => {
            const headers = Array.from(table.querySelectorAll('thead th, thead td'))
                .map((cell) => clean(cell.innerText))
                .filter(Boolean);
            const rows = Array.from(table.querySelectorAll('tbody tr, tr')).slice(0, 120);
            const parsedRows = [];
            rows.forEach((tr, rowIndex) => {
                const cells = Array.from(tr.querySelectorAll('td, th'))
                    .map((cell) => clip(cell.innerText, 220));
                if (!cells.some(Boolean)) return;
                const record = { source: `table_${tableIndex + 1}`, row: rowIndex + 1 };
                cells.forEach((cell, index) => {
                    const key = headers[index] || `字段${index + 1}`;
                    record[key] = cell;
                });
                parsedRows.push(record);
                records.push(record);
            });
            if (parsedRows.length) {
                tables.push({ index: tableIndex + 1, headers, rows: parsedRows.slice(0, 30) });
            }
        });

        const cardSelectors = [
            '[class*="card"]',
            '[class*="item"]',
            '[class*="goods"]',
            '[class*="product"]',
            '[class*="order"]',
            '[class*="metric"]',
            '[class*="stat"]'
        ];
        const seen = new Set();
        document.querySelectorAll(cardSelectors.join(',')).forEach((el, index) => {
            if (records.length >= 180) return;
            if (!visible(el)) return;
            const text = clip(el.innerText, 420);
            if (!text || text.length < 4 || seen.has(text)) return;
            seen.add(text);
            const record = { source: 'visible_block', row: index + 1, text };
            records.push(record);
        });

        const links = Array.from(document.querySelectorAll('a[href]')).slice(0, 80).map((a) => ({
            text: clip(a.innerText || a.getAttribute('title') || '', 160),
            href: a.href,
        })).filter((item) => item.text || item.href);
        const bodyText = clip(document.body ? document.body.innerText : '', 3000);
        const shopPatternClean = /([\\u4e00-\\u9fa5A-Za-z0-9\\uff08\\uff09()·\\-_]{2,60}(?:\\u65d7\\u8230\\u5e97|\\u4e13\\u5356\\u5e97|\\u4e13\\u8425\\u5e97|\\u4f01\\u4e1a\\u5e97|\\u4e2a\\u4eba\\u5e97|\\u5b98\\u65b9\\u5e97|\\u5e97\\u94fa|\\u5c0f\\u5e97))/;
        const shopPattern = /([\\u4e00-\\u9fa5A-Za-z0-9（）()·\\-_]{2,60}(?:旗舰店|专卖店|专营店|企业店|个人店|官方店|店铺|小店))/;
        const shopCandidates = Array.from(document.querySelectorAll([
            '[class*="shop"]',
            '[class*="Shop"]',
            '[class*="store"]',
            '[class*="Store"]',
            '[class*="seller"]',
            '[class*="Seller"]',
            '[class*="merchant"]',
            '[class*="Merchant"]',
            '[class*="name"]',
            '[class*="Name"]'
        ].join(','))).filter(visible).map((el) => clean(el.innerText || el.textContent))
            .filter((text) => text && text.length <= 80);
        const bodyShop = (bodyText.match(shopPatternClean) || [])[1] || '';
        const platformShopNames = new Set([
            '\\u6296\\u5e97',
            '\\u5343\\u725b',
            '\\u6dd8\\u5b9d',
            '\\u6dd8\\u5b9d\\u5356\\u5bb6\\u4e2d\\u5fc3',
            '\\u6296\\u5e97\\u540e\\u53f0',
            '\\u5343\\u725b\\u5de5\\u4f5c\\u53f0'
        ]);
        const strongShopPattern = /(\\u516c\\u53f8|\\u4f01\\u4e1a\\u5e97|\\u65d7\\u8230\\u5e97|\\u4e13\\u5356\\u5e97|\\u4e13\\u8425\\u5e97|\\u5b98\\u65b9\\u5e97)/;
        const normalizedShopCandidates = Array.from(new Set([bodyShop, ...(shopCandidates || [])].filter(Boolean)))
            .filter((text) => !platformShopNames.has(text));
        const bestShop = normalizedShopCandidates.find((text) => strongShopPattern.test(text))
            || normalizedShopCandidates.find((text) => text.includes('\\u5e97'))
            || normalizedShopCandidates[0]
            || '';

        return {
            title: document.title || '',
            url: location.href,
            shop_name: bestShop,
            shop_candidates: normalizedShopCandidates.slice(0, 12),
            body_text: bodyText,
            records,
            tables,
            links,
            captured_at: new Date().toISOString(),
        };
    }
    """)


@app.route('/api/merchant/platforms')
def merchant_platforms():
    """商家后台平台列表。"""
    platforms = {}
    for key, cfg in MERCHANT_PLATFORM_CONFIGS.items():
        platforms[key] = {
            'name': cfg['name'],
            'short_name': cfg.get('short_name', cfg['name']),
            'icon': cfg.get('icon', ''),
            'homepage': cfg.get('homepage', ''),
            'login_url': cfg.get('login_url', ''),
            'note': cfg.get('note', ''),
            'data_types': cfg.get('data_types', {}),
            'session_saved': _merchant_session_exists(key),
        }
    return jsonify(platforms)


@app.route('/api/merchant/diagnostics', methods=['POST'])
def merchant_diagnostics():
    """商家后台采集运行前检查。"""
    data = request.get_json(silent=True) or {}
    return jsonify(_merchant_diagnostics(data))


@app.route('/api/merchant/import', methods=['POST'])
def merchant_import():
    """导入已经登录的 Chrome 页面可见数据。"""
    data = request.get_json(silent=True) or {}
    platform = data.get('platform') or 'custom'
    if platform not in MERCHANT_PLATFORM_CONFIGS:
        return jsonify({'success': False, 'error': '不支持的平台'})

    cfg = MERCHANT_PLATFORM_CONFIGS[platform]
    data_type = data.get('data_type') or 'custom'
    data_types = cfg.get('data_types', {})
    data_type_label = data_types.get(data_type, data_type)
    url = data.get('url') or ''
    title = data.get('title') or ''
    shop_name, shop_id = _merchant_shop_meta(data)

    records = data.get('records') or []
    tables = data.get('tables') or []
    links = data.get('links') or []
    if not isinstance(records, list):
        records = []
    if not isinstance(tables, list):
        tables = []
    if not isinstance(links, list):
        links = []

    # Keep imports bounded so a very large merchant page cannot bloat the local DB.
    records = records[:500]
    tables = tables[:50]
    links = links[:200]

    results = records[:]
    if not results and tables:
        for table in tables:
            for row in (table.get('rows') or [])[:100]:
                if isinstance(row, dict):
                    results.append(row)
    if not shop_name:
        shop_name, shop_id = _merchant_shop_meta(data, results)
    if not results:
        return jsonify({'success': False, 'error': '当前页面没有提取到可保存的数据'})
    results = _tag_records_with_shop(results, shop_name, shop_id)

    mode = f'merchant_chrome:{data_type}'
    keyword = f'Chrome导入:{data_type_label}'
    job_id = _create_job(
        task_type='merchant',
        platform=platform,
        url=url,
        keyword=keyword,
        pages=1,
        mode=mode,
        status='running',
        shop_name=shop_name,
        shop_id=shop_id,
    )
    _save_scrape(platform, url, keyword, mode, results, shop_name=shop_name, shop_id=shop_id)
    _update_job(
        job_id,
        status='success',
        item_count=len(results),
        shop_name=shop_name,
        shop_id=shop_id,
        finished_at=_now_iso(),
        log_json=json.dumps([
            {'time': _now_iso(), 'message': f'从 Chrome 当前页导入 {len(results)} 条可见数据'},
            {'time': _now_iso(), 'message': title or url or '未命名页面'},
        ], ensure_ascii=False),
    )
    return jsonify({
        'success': True,
        'job_id': job_id,
        'platform': platform,
        'platform_name': cfg.get('name', platform),
        'data_type': data_type,
        'data_type_label': data_type_label,
        'shop_name': shop_name,
        'shop_id': shop_id,
        'title': title,
        'url': url,
        'count': len(results),
        'records': results[:120],
        'tables': tables,
        'links': links,
    })


@app.route('/api/merchant/run', methods=['POST'])
def merchant_run():
    """采集登录后商家后台页面的可见表格、卡片和指标。"""
    data = request.get_json(silent=True) or {}
    diagnostics = _merchant_diagnostics(data)
    if not diagnostics['ok']:
        errors = [item['message'] for item in diagnostics['checks'].values() if not item['ok']]
        return jsonify({'success': False, 'error': '; '.join(errors), 'diagnostics': diagnostics})

    platform = diagnostics['platform']
    cfg = MERCHANT_PLATFORM_CONFIGS[platform]
    data_type = diagnostics['data_type']
    target_url = diagnostics['target_url']
    shop_name, shop_id = _merchant_shop_meta(data)
    human_mode = bool(data.get('human_mode'))
    wait_seconds = max(3, min(int(data.get('wait_seconds') or 8), 300))
    job_id = _create_job(
        task_type='merchant',
        platform=platform,
        url=target_url,
        keyword=cfg.get('data_types', {}).get(data_type, data_type),
        pages=1,
        mode=data_type,
        status='running',
        shop_name=shop_name,
        shop_id=shop_id,
    )

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(
                p,
                platform_key=platform,
                headless=not human_mode,
                human_mode=human_mode,
                use_system_chrome=False,
                device='pc',
            )
            page = context.new_page()
            _safe_goto(page, target_url, timeout=90000, human_mode=human_mode)
            if human_mode:
                page.wait_for_timeout(wait_seconds * 1000)
                try:
                    _save_cookies(platform, context.storage_state())
                except Exception:
                    pass
            result = _extract_merchant_visible_data(page)
            browser.close()

        records = result.get('records') or []
        if not shop_name:
            shop_name, shop_id = _merchant_shop_meta({**data, **result}, records)
        records = _tag_records_with_shop(records, shop_name, shop_id)
        item_count = len(records)
        _save_scrape(platform, target_url, cfg.get('data_types', {}).get(data_type, data_type),
                     f'merchant:{data_type}', records, shop_name=shop_name, shop_id=shop_id)
        _update_job(
            job_id,
            status='success',
            item_count=item_count,
            shop_name=shop_name,
            shop_id=shop_id,
            output_file='页面可见数据',
            log_json=json.dumps([
                f'平台: {cfg["name"]}',
                f'页面: {target_url}',
                f'采集记录: {item_count}',
            ], ensure_ascii=False),
            finished_at=_now_iso(),
        )
        return jsonify({
            'success': True,
            'job_id': job_id,
            'platform': cfg['name'],
            'data_type': data_type,
            'shop_name': shop_name,
            'shop_id': shop_id,
            'url': result.get('url') or target_url,
            'title': result.get('title') or '',
            'count': item_count,
            'records': records[:120],
            'tables': result.get('tables', [])[:10],
            'links': result.get('links', [])[:80],
            'session_saved': _merchant_session_exists(platform),
        })
    except Exception as e:
        _update_job(
            job_id,
            status='error',
            error=str(e),
            log_json=json.dumps([traceback.format_exc()], ensure_ascii=False),
            finished_at=_now_iso(),
        )
        return jsonify({'success': False, 'error': str(e), 'job_id': job_id})


@app.route('/api/sessions')
def list_sessions():
    """列出已保存的登录会话"""
    return jsonify({'sessions': _list_sessions()})


@app.route('/api/sessions/delete', methods=['POST'])
def delete_session():
    """删除登录会话"""
    data = request.json
    platform = data.get('platform', '')
    state_file = COOKIES_DIR / f'{platform}_state.json'
    cookie_file = COOKIES_DIR / f'{platform}.json'
    if state_file.exists():
        state_file.unlink()
    if cookie_file.exists():
        cookie_file.unlink()
    return jsonify({'success': True})


@app.route('/api/login', methods=['POST'])
def login_to_platform():
    """打开浏览器让用户登录，保存会话"""
    data = request.json
    platform = data.get('platform', '')

    preset = PLATFORM_PRESETS.get(platform) or MERCHANT_PLATFORM_CONFIGS.get(platform)
    if not preset:
        return jsonify({'success': False, 'error': '不支持的平台'})

    login_url = preset.get('login_url') or preset.get('homepage')
    if not login_url:
        return jsonify({'success': False, 'error': '该平台暂无网页登录入口'})

    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, channel='chrome')  # 使用系统 Chrome
            context = browser.new_context(
                viewport={'width': 1280, 'height': 800},
                locale='zh-CN',
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
            page = context.new_page()
            _safe_goto(page, login_url, timeout=60000)

            # 等用户手动登录（最多等 5 分钟）
            page.wait_for_timeout(300000)

            # 保存会话
            state = context.storage_state()
            saved_path = _save_cookies(platform, state)

            browser.close()

        return jsonify({
            'success': True,
            'message': f'{preset["name"]} 登录会话已保存',
            'session_file': saved_path
        })

    except Exception as e:
        return jsonify({'success': False, 'error': f'登录失败: {str(e)}'})


@app.route('/api/scrape', methods=['POST'])
def scrape():
    """执行网页抓取（支持平台预设和自定义选择器）"""
    data = request.json
    url = data.get('url', '').strip()
    platform = data.get('platform', '')
    mode = data.get('mode', 'playwright')
    fields = data.get('fields', [])
    custom_selectors = data.get('selectors', [])
    use_login = data.get('use_login', True)
    human_mode = data.get('human_mode', False)
    use_system_chrome = data.get('use_system_chrome', False)
    use_profile = data.get('use_profile', False)
    device = data.get('device', 'pc')
    proxy_url = data.get('proxy_url', '')
    use_cdp = data.get('use_cdp', False)

    if not url:
        return jsonify({'success': False, 'error': '请输入链接'})

    # 智能检测平台：没传 platform 时自动识别
    if not platform:
        platform = auto_detect_platform(url) or ''

    # 构建选择器列表
    selectors = list(custom_selectors)
    preset = PLATFORM_PRESETS.get(platform) if platform else None

    if preset and fields:
        for f in preset['fields']:
            if f['key'] in fields:
                selectors.append({
                    'key': f['key'],
                    'name': f['label'],
                    'css': f.get('css', ''),
                    'attr': f.get('attr', 'text'),
                    'auto': f.get('auto', False)
                })

    if not selectors and not preset:
        return smart_extract_api(url, use_login, human_mode, device, proxy_url)
    if not selectors and preset:
        return scrape_with_js(url, preset, use_login, human_mode, device, proxy_url)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(p,
                platform_key=platform if use_login else None,
                headless=not human_mode and device == 'pc' and not use_cdp,
                human_mode=human_mode,
                use_system_chrome=use_system_chrome,
                use_profile=use_profile,
                device=device,
                proxy_url=proxy_url or None,
                use_cdp=use_cdp)
            page = context.new_page()
            if human_mode and not use_cdp:
                _inject_stealth(page)
            _safe_goto(page, url, timeout=90000 if human_mode else 60000, human_mode=human_mode)

            results = _extract_by_css(page, selectors)

            # 平台 API 拦截（抖音/快手等异步加载数据的页面）
            api_data = {}
            if preset and preset.get('api_patterns'):
                captured = _intercept_api(page, preset['api_patterns'], timeout=8000)
                api_data = _extract_douyin_from_api(captured)
                if api_data:
                    for r in results:
                        key = r.get('key', '')
                        if key in api_data and r['count'] == 0:
                            r['items'] = [str(api_data[key])]
                            r['count'] = 1
                            r['source'] = 'api_intercept'

            # 如果有 JS 提取脚本且没有 CSS 选择器匹配（使用数据驱动统一提取器）
            if preset and preset.get('data_extract'):
                js_code = make_extractor_js(preset['data_extract'])
                js_data = _js_extract(page, js_code)
                if js_data:
                    for r in results:
                        key = r.get('key', '')
                        if key in js_data and r['count'] == 0:
                            r['items'] = [js_data[key]]
                            r['count'] = 1
                            r['source'] = 'js_extract'

            # 向后兼容：存在旧版 js_extract 时也尝试
            if preset and preset.get('js_extract') and not any(r['count'] > 0 for r in results):
                js_data = _js_extract(page, preset['js_extract'])
                if js_data:
                    for r in results:
                        key = r.get('key', '')
                        if key in js_data and r['count'] == 0:
                            r['items'] = [js_data[key]]
                            r['count'] = 1
                            r['source'] = 'js_extract'

            browser.close()

        # 保存到数据库
        try:
            _save_scrape(platform, url, '', mode, results)
        except Exception:
            pass

        return jsonify({'success': True, 'results': results, 'url': url, 'platform': platform})

    except Exception as e:
        # 保存失败记录
        try: _save_scrape(platform, url, '', mode, [], error=str(e))
        except: pass
        return jsonify({
            'success': False,
            'error': str(e),
            'traceback': traceback.format_exc()
        })


# ========== 批量抓取 ==========

@app.route('/api/scrape/batch', methods=['POST'])
def scrape_batch():
    """批量抓取多个链接（换行分隔或数组）"""
    data = request.json
    raw = data.get('urls', '')
    platform = data.get('platform', '')
    max_concurrent = min(data.get('max_concurrent', 3), 5)

    # 支持字符串（换行分隔）或数组
    if isinstance(raw, str):
        urls = [u.strip() for u in raw.split('\n') if u.strip()]
    elif isinstance(raw, list):
        urls = [u.strip() for u in raw if u.strip()]
    else:
        return jsonify({'success': False, 'error': 'urls 参数格式错误'})

    if len(urls) > 20:
        return jsonify({'success': False, 'error': '一次最多抓取 20 个链接'})
    if not urls:
        return jsonify({'success': False, 'error': '请粘贴至少一个链接'})

    results = []
    errors = []
    total = len(urls)
    import concurrent.futures

    def _scrape_single(url):
        try:
            # 自动检测平台
            p = platform or auto_detect_platform(url) or ''
            preset = PLATFORM_PRESETS.get(p)
            if preset and preset.get('data_extract'):
                from playwright.sync_api import sync_playwright
                with sync_playwright() as p_ctx:
                    browser, context = _create_context(p_ctx, p if data.get('use_login', True) else None,
                        headless=True)
                    page = context.new_page()
                    _safe_goto(page, url, timeout=45000)
                    js_code = make_extractor_js(preset['data_extract'])
                    js_data = _js_extract(page, js_code)
                    browser.close()
                    return {'url': url, 'platform': p, 'data': js_data, 'success': True}
            # 无预设时用智能提取
            return _scrape_single_smart(url)
        except Exception as e:
            return {'url': url, 'platform': p, 'error': str(e), 'success': False}

    def _scrape_single_smart(url):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p_ctx:
            browser, context = _create_context(p_ctx, None, headless=True)
            page = context.new_page()
            _safe_goto(page, url, timeout=45000)
            data = _smart_extract(page)
            browser.close()
            return {'url': url, 'platform': auto_detect_platform(url) or '', 'data': data, 'success': True}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = [executor.submit(_scrape_single, url) for url in urls]
        for i, future in enumerate(concurrent.futures.as_completed(futures)):
            try:
                result = future.result()
                if result['success']:
                    results.append(result)
                else:
                    errors.append(result)
            except Exception as e:
                errors.append({'url': urls[i] if i < len(urls) else '', 'error': str(e), 'success': False})

    # 保存到数据库
    try:
        _save_scrape('batch', '\n'.join(urls), '', 'batch', results)
    except:
        pass

    return jsonify({
        'success': True,
        'total': total,
        'completed': len(results),
        'errors': len(errors),
        'results': results,
        'failed': errors
    })


def scrape_with_js(url, preset, use_login, human_mode=False, device='pc', proxy_url='', platform_key=''):
    """仅用 JS 注入提取数据（无需 CSS 选择器）"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(p, None,
                headless=not human_mode and device == 'pc',
                human_mode=human_mode,
                device=device,
                proxy_url=proxy_url or None)
            page = context.new_page()
            if human_mode:
                _inject_stealth(page)
            _safe_goto(page, url, timeout=90000 if human_mode else 60000, human_mode=human_mode)

            # 优先使用数据驱动提取器
            if preset.get('data_extract'):
                js_code = make_extractor_js(preset['data_extract'])
                js_data = _js_extract(page, js_code)
            # 回退到旧版 js_extract
            elif preset.get('js_extract'):
                js_data = _js_extract(page, preset['js_extract'])
            else:
                js_data = {}
            browser.close()

        if js_data:
            results = []
            for f in preset['fields']:
                val = js_data.get(f['key'], '')
                results.append({
                    'key': f['key'],
                    'name': f['label'],
                    'count': 1 if val else 0,
                    'items': [str(val)] if val else [],
                    'source': 'js_extract'
                })
            return jsonify({'success': True, 'results': results, 'url': url, 'platform': preset['name']})
        else:
            return smart_extract_api(url, use_login, human_mode, device, proxy_url)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


# ========== 关键词搜索采集 ==========

def _normalize_competitor_item(item, platform='', keyword='', page_num=0, source='search_result'):
    item = item or {}
    title = _clean_meta_text(item.get('title') or item.get('name') or item.get('text'), 300)
    price = _clean_meta_text(item.get('price') or item.get('price_text'), 80)
    shop = _clean_meta_text(item.get('shop') or item.get('seller') or item.get('store'), 160)
    link = item.get('link') or item.get('url') or ''
    image = item.get('image') or item.get('main_image') or ''
    images = item.get('images') if isinstance(item.get('images'), list) else []
    if image and image not in images:
        images = [image] + images
    copy_text = _clean_meta_text(item.get('copy') or item.get('description') or item.get('desc'), 1200)
    return {
        'source': source,
        'platform': platform or item.get('platform') or '',
        'keyword': keyword or item.get('keyword') or '',
        'page': page_num or item.get('page') or '',
        'title': title,
        'price': price,
        'shop': shop,
        'sales': _clean_meta_text(item.get('sales') or item.get('sold') or '', 80),
        'rating': _clean_meta_text(item.get('rating') or item.get('comment') or item.get('comments') or '', 80),
        'main_image': image or (images[0] if images else ''),
        'images': images[:12],
        'image_count': len(images),
        'copy': copy_text,
        'link': link,
    }


def _extract_competitor_detail(page):
    return page.evaluate("""
    () => {
        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
        const clip = (value, limit = 1200) => {
            const text = clean(value);
            return text.length > limit ? text.slice(0, limit) + '...' : text;
        };
        const visible = (el) => {
            const rect = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
        };
        const meta = (selector) => document.querySelector(selector)?.getAttribute('content') || '';
        const attrUrl = (value) => {
            if (!value) return '';
            try { return new URL(value, location.href).href; } catch { return value; }
        };
        const title = clip(
            meta('meta[property="og:title"]') ||
            document.querySelector('h1')?.innerText ||
            document.querySelector('[class*="title"],[class*="Title"],[class*="name"],[class*="Name"]')?.innerText ||
            document.title,
            300
        );
        const priceTexts = Array.from(document.querySelectorAll([
            '[class*="price"]',
            '[class*="Price"]',
            '[class*="amount"]',
            '[class*="Amount"]',
            '[class*="money"]',
            '[class*="Money"]'
        ].join(','))).filter(visible).map((el) => clean(el.innerText || el.textContent))
            .filter((text) => /[￥¥]\\s*\\d|\\d+\\.\\d{1,2}/.test(text));
        const bodyText = clean(document.body ? document.body.innerText : '');
        const bodyPrice = (bodyText.match(/[￥¥]\\s*\\d+(?:\\.\\d{1,2})?/) || [])[0] || '';
        const images = Array.from(document.images).filter((img) => {
            const rect = img.getBoundingClientRect();
            return rect.width >= 80 && rect.height >= 80;
        }).map((img) => attrUrl(img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-original')))
            .filter((src) => src && !/avatar|icon|logo|sprite|gif/i.test(src));
        const shopTexts = Array.from(document.querySelectorAll([
            '[class*="shop"]',
            '[class*="Shop"]',
            '[class*="store"]',
            '[class*="Store"]',
            '[class*="seller"]',
            '[class*="Seller"]',
            '[class*="merchant"]',
            '[class*="Merchant"]'
        ].join(','))).filter(visible).map((el) => clean(el.innerText || el.textContent))
            .filter((text) => text && text.length <= 120);
        const copyParts = [];
        const metaDesc = meta('meta[name="description"]') || meta('meta[property="og:description"]');
        if (metaDesc) copyParts.push(metaDesc);
        document.querySelectorAll([
            '[class*="desc"]',
            '[class*="Desc"]',
            '[class*="detail"]',
            '[class*="Detail"]',
            '[class*="content"]',
            '[class*="Content"]',
            '[class*="selling"]',
            '[class*="Selling"]'
        ].join(',')).forEach((el) => {
            if (copyParts.length >= 8 || !visible(el)) return;
            const text = clip(el.innerText || el.textContent, 500);
            if (text && text.length >= 8) copyParts.push(text);
        });
        const sales = (bodyText.match(/(?:已售|销量|付款|成交|月销|评价)\\s*[:：]?\\s*[\\d.万wW+]+/) || [])[0] || '';
        const rating = (bodyText.match(/(?:评分|评价|评论)\\s*[:：]?\\s*[\\d.万wW+]+/) || [])[0] || '';
        return {
            title,
            price: priceTexts[0] || bodyPrice || '',
            shop: shopTexts[0] || '',
            sales,
            rating,
            main_image: images[0] || meta('meta[property="og:image"]') || '',
            images: Array.from(new Set(images)).slice(0, 12),
            copy: Array.from(new Set(copyParts)).join(' | '),
            link: location.href,
            captured_at: new Date().toISOString(),
        };
    }
    """)


@app.route('/api/competitor/run', methods=['POST'])
def competitor_run():
    """买家视角采集同行公开商品信息。"""
    data = request.get_json(silent=True) or {}
    platform = data.get('platform') or ''
    keyword = (data.get('keyword') or '').strip()
    urls = data.get('urls') or []
    if isinstance(urls, str):
        urls = [u.strip() for u in re.split(r'[\r\n,]+', urls) if u.strip()]
    pages = max(1, min(int(data.get('pages') or 1), 5))
    human_mode = bool(data.get('human_mode'))
    fetch_details = bool(data.get('fetch_details'))
    if not keyword and not urls:
        return jsonify({'success': False, 'error': '请输入关键词或商品链接'})
    if keyword and platform not in SEARCH_CONFIGS:
        return jsonify({'success': False, 'error': f'平台 {platform} 不支持关键词竞品采集'})

    platform_name = SEARCH_CONFIGS.get(platform, {}).get('name') or PLATFORM_PRESETS.get(platform, {}).get('name') or platform or 'multi'
    job_id = _create_job(
        task_type='competitor',
        platform=platform or 'multi',
        url='competitor:' + (keyword or (urls[0] if urls else '')),
        keyword=keyword or f'{len(urls)} 个商品链接',
        pages=pages,
        mode='competitor:buyer',
        status='running',
    )
    results = []
    errors = []
    seen_links = set()

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(
                p,
                platform_key=platform or None,
                headless=not human_mode,
                human_mode=human_mode,
                device='pc',
            )
            page = context.new_page()

            if keyword:
                search_cfg = SEARCH_CONFIGS[platform]
                for page_num in range(1, pages + 1):
                    search_url = search_cfg['search_url'].replace('{keyword}', keyword)
                    search_url = search_url.replace('{page}', str(page_num))
                    if '{page_start}' in search_url:
                        calc = search_cfg.get('page_start_calc', lambda p: (p - 1) * 44)
                        search_url = search_url.replace('{page_start}', str(calc(page_num)))
                    try:
                        _safe_goto(page, search_url, timeout=70000, human_mode=human_mode)
                        page.wait_for_timeout(4000)
                        try:
                            page.wait_for_load_state('networkidle', timeout=12000)
                        except Exception:
                            pass
                        raw = page.evaluate(search_cfg['extract_js'])
                        items = json.loads(raw) if isinstance(raw, str) else (raw or [])
                        for item in items:
                            normalized = _normalize_competitor_item(item, platform_name, keyword, page_num)
                            link = normalized.get('link') or f'{normalized.get("title")}:{normalized.get("price")}'
                            if link in seen_links:
                                continue
                            seen_links.add(link)
                            results.append(normalized)
                    except Exception as e:
                        errors.append(f'第 {page_num} 页: {e}')

            detail_urls = urls[:20]
            if fetch_details and keyword:
                detail_urls += [item.get('link') for item in results if item.get('link')][:10]
            for detail_url in detail_urls[:25]:
                if not detail_url:
                    continue
                try:
                    _safe_goto(page, detail_url, timeout=80000, human_mode=human_mode)
                    page.wait_for_timeout(4000)
                    detail = _extract_competitor_detail(page)
                    detected_platform = platform_name
                    if not detected_platform or detected_platform == 'multi':
                        detected_key = auto_detect_platform(detail_url)
                        detected_platform = PLATFORM_PRESETS.get(detected_key, {}).get('name') or detected_key or ''
                    normalized = _normalize_competitor_item(detail, detected_platform, keyword, 0, 'product_detail')
                    link = normalized.get('link') or detail_url
                    if link in seen_links:
                        for existing in results:
                            if existing.get('link') == link:
                                for key, value in normalized.items():
                                    if value and (not existing.get(key) or key in {'copy', 'images', 'image_count', 'main_image'}):
                                        existing[key] = value
                                existing['source'] = 'search_result+detail'
                                break
                        continue
                    seen_links.add(link)
                    results.append(normalized)
                except Exception as e:
                    errors.append(f'详情页 {detail_url}: {e}')

            browser.close()

        _save_scrape(platform or 'multi', 'competitor:' + (keyword or '\n'.join(urls[:5])),
                     keyword or f'{len(urls)} 个商品链接', 'competitor:buyer', results)
        _update_job(
            job_id,
            status='success',
            item_count=len(results),
            finished_at=_now_iso(),
            log_json=json.dumps(errors[-20:], ensure_ascii=False),
        )
        return jsonify({
            'success': True,
            'job_id': job_id,
            'platform': platform_name,
            'keyword': keyword,
            'count': len(results),
            'items': results[:160],
            'errors': errors,
        })
    except Exception as e:
        _update_job(job_id, status='error', error=str(e), finished_at=_now_iso(),
                    log_json=json.dumps([traceback.format_exc()], ensure_ascii=False))
        return jsonify({'success': False, 'error': str(e), 'job_id': job_id})


VIDEO_DOWNLOAD_DIR = BASE_DIR / 'crawler_output' / 'videos'


def _video_download_dir():
    VIDEO_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return VIDEO_DOWNLOAD_DIR


def _find_ytdlp_command():
    import shutil
    if shutil.which('yt-dlp'):
        return ['yt-dlp']
    try:
        import importlib.util
        if importlib.util.find_spec('yt_dlp'):
            return [sys.executable, '-m', 'yt_dlp']
    except Exception:
        pass
    return []


def _extract_video_metadata_with_playwright(page):
    return page.evaluate("""
    () => {
        const clean = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
        const meta = (selector) => document.querySelector(selector)?.getAttribute('content') || '';
        const attrUrl = (value) => {
            if (!value) return '';
            try { return new URL(value, location.href).href; } catch { return value; }
        };
        const videos = Array.from(document.querySelectorAll('video')).map((video) => ({
            src: attrUrl(video.currentSrc || video.src || ''),
            poster: attrUrl(video.poster || ''),
        })).filter((item) => item.src || item.poster);
        const title = clean(
            meta('meta[property="og:title"]') ||
            meta('meta[name="twitter:title"]') ||
            document.querySelector('h1')?.innerText ||
            document.title
        );
        const description = clean(
            meta('meta[name="description"]') ||
            meta('meta[property="og:description"]') ||
            meta('meta[name="twitter:description"]') ||
            ''
        );
        const cover = attrUrl(
            meta('meta[property="og:image"]') ||
            meta('meta[name="twitter:image"]') ||
            videos.find((item) => item.poster)?.poster ||
            ''
        );
        const videoUrl = attrUrl(
            meta('meta[property="og:video"]') ||
            meta('meta[property="og:video:url"]') ||
            videos.find((item) => item.src)?.src ||
            ''
        );
        return {
            title,
            description,
            cover,
            video_url: videoUrl,
            page_url: location.href,
            source: 'page_visible_metadata',
            captured_at: new Date().toISOString(),
        };
    }
    """)


def _video_metadata_with_ytdlp(url, command):
    if not command:
        return {}, 'yt-dlp not installed'
    cmd = command + ['--dump-json', '--skip-download', '--no-playlist', '--socket-timeout', '30', url]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(_video_download_dir()))
    if proc.returncode != 0:
        return {}, (proc.stderr or proc.stdout or 'yt-dlp metadata failed')[-1000:]
    lines = [line for line in (proc.stdout or '').splitlines() if line.strip()]
    if not lines:
        return {}, 'yt-dlp returned empty metadata'
    info = json.loads(lines[-1])
    thumbnails = info.get('thumbnails') or []
    cover = info.get('thumbnail') or (thumbnails[-1].get('url') if thumbnails else '')
    return {
        'title': info.get('title') or '',
        'author': info.get('uploader') or info.get('creator') or '',
        'description': info.get('description') or '',
        'duration': info.get('duration') or '',
        'cover': cover,
        'video_id': info.get('id') or '',
        'page_url': info.get('webpage_url') or url,
        'extractor': info.get('extractor') or '',
        'source': 'yt-dlp_metadata',
    }, ''


def _download_video_with_ytdlp(url, command):
    if not command:
        return [], 'yt-dlp not installed'
    out_dir = _video_download_dir()
    before = {str(path) for path in out_dir.glob('*')}
    template = str(out_dir / '%(extractor)s_%(id)s_%(title).80s.%(ext)s')
    cmd = command + [
        '--no-playlist',
        '--restrict-filenames',
        '--write-thumbnail',
        '--write-info-json',
        '--socket-timeout', '30',
        '--retries', '1',
        '-o', template,
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=360, cwd=str(out_dir))
    after = [path for path in out_dir.glob('*') if str(path) not in before]
    files = [str(path) for path in after if path.is_file()]
    if proc.returncode != 0:
        return files, (proc.stderr or proc.stdout or 'yt-dlp download failed')[-1000:]
    return files, ''


@app.route('/api/video-download/run', methods=['POST'])
def video_download_run():
    """下载或采集公开视频信息。仅用于本人/授权/公开可访问视频。"""
    data = request.get_json(silent=True) or {}
    raw_urls = data.get('urls') or data.get('url') or ''
    urls = [u.strip() for u in re.split(r'[\r\n,]+', raw_urls) if u.strip()]
    urls = urls[:20]
    download_file = bool(data.get('download_file'))
    confirm_rights = bool(data.get('confirm_rights'))
    human_mode = bool(data.get('human_mode'))
    platform = data.get('platform') or 'douyin'

    if not urls:
        return jsonify({'success': False, 'error': '请粘贴抖音视频链接'})
    if download_file and not confirm_rights:
        return jsonify({'success': False, 'error': '下载前请确认这些视频属于本人、已获授权或允许保存的公开内容'})

    job_id = _create_job(
        task_type='video_download',
        platform=platform,
        url='\n'.join(urls[:5]),
        keyword=f'{len(urls)} 个视频链接',
        pages=len(urls),
        mode='video:download' if download_file else 'video:metadata',
        status='running',
    )
    ytdlp_cmd = _find_ytdlp_command()
    results = []
    errors = []

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = None
            context = None
            if not ytdlp_cmd:
                browser, context = _create_context(
                    p,
                    platform_key='douyin',
                    headless=not human_mode,
                    human_mode=human_mode,
                    device='pc',
                )
            for index, url in enumerate(urls, start=1):
                item = {
                    'source_url': url,
                    'platform': '抖音',
                    'status': 'pending',
                    'downloaded_files': [],
                }
                meta, meta_error = _video_metadata_with_ytdlp(url, ytdlp_cmd)
                if meta:
                    item.update(meta)
                elif context:
                    try:
                        page = context.new_page()
                        _safe_goto(page, url, timeout=80000, human_mode=human_mode)
                        page.wait_for_timeout(3000)
                        item.update(_extract_video_metadata_with_playwright(page))
                        page.close()
                    except Exception as e:
                        meta_error = str(e)
                if meta_error:
                    item['metadata_error'] = meta_error
                    errors.append(f'{index}. 元数据失败: {meta_error}')

                if download_file:
                    files, download_error = _download_video_with_ytdlp(url, ytdlp_cmd)
                    item['downloaded_files'] = files
                    item['download_dir'] = str(_video_download_dir())
                    if download_error:
                        item['download_error'] = download_error
                        errors.append(f'{index}. 下载失败: {download_error}')
                    item['status'] = 'success' if files else 'error'
                else:
                    item['status'] = 'success' if (item.get('title') or item.get('video_url') or item.get('cover')) else 'error'
                results.append(item)
            if browser:
                browser.close()

        success_count = sum(1 for item in results if item.get('status') == 'success')
        status = 'success' if success_count else 'error'
        _save_scrape(platform, '\n'.join(urls[:5]), f'{len(urls)} 个视频链接',
                     'video:download' if download_file else 'video:metadata', results,
                     error='' if status == 'success' else '; '.join(errors[:3]))
        _update_job(
            job_id,
            status=status,
            item_count=success_count,
            output_file=str(_video_download_dir()) if download_file else '',
            error='' if status == 'success' else '; '.join(errors[:3]),
            log_json=json.dumps(errors[-30:], ensure_ascii=False),
            finished_at=_now_iso(),
        )
        return jsonify({
            'success': status == 'success',
            'job_id': job_id,
            'count': success_count,
            'items': results,
            'errors': errors,
            'download_dir': str(_video_download_dir()),
            'ytdlp_available': bool(ytdlp_cmd),
        })
    except Exception as e:
        _update_job(job_id, status='error', error=str(e), finished_at=_now_iso(),
                    log_json=json.dumps([traceback.format_exc()], ensure_ascii=False))
        return jsonify({'success': False, 'error': str(e), 'job_id': job_id})

@app.route('/api/video-download/reverse', methods=['POST'])
def video_download_reverse():
    """逆向API无水印下载 —— 无浏览器, 最快的方式"""
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    if not url:
        return jsonify({'success': False, 'error': '请提供抖音视频链接'})

    cmd = data.get('cmd', 'download')
    pages = _safe_int(data.get('pages', 1), default=1, min_value=1, max_value=20)
    video_root = BASE_DIR.parent / 'video-parser'
    hermes_cli = str(video_root / 'hermes_douyin.py')
    job_id = _create_job(
        task_type='video_reverse', platform='douyin', url=url,
        keyword=f'{cmd}:{url[:50]}' if cmd == 'download' else f'batch:{pages}p',
        pages=pages, mode=f'reverse:{cmd}',
    )

    def run_reverse():
        try:
            cli_cmd = [os.environ.get('VIDEO_PARSER_PYTHON', str(video_root / 'venv/Scripts/python.exe')), hermes_cli, cmd, url]
            if cmd == 'batch':
                cli_cmd += ['--pages', str(pages), '--concurrent', '3']
            result = subprocess.run(
                cli_cmd, capture_output=True, text=True, timeout=300,
                cwd=video_root,
            )
            output = result.stdout.strip()
            if result.returncode != 0:
                _update_job(job_id, status='error',
                    error=result.stderr.strip() or output or f'CLI exit {result.returncode}',
                    finished_at=_now_iso())
                return
            lines = output.splitlines()
            results = [json.loads(l) for l in lines if l.strip().startswith('{')]
            if not results:
                _update_job(job_id, status='error', error='CLI 无有效输出',
                    log_json=output[:1000], finished_at=_now_iso())
                return
            if cmd == 'batch' and len(results) >= 2:
                batch_done = [r for r in results if r.get('type') == 'batch_done']
                items = [r for r in results if r.get('type') == 'progress']
                success = batch_done[-1]['success'] if batch_done else 0
                total = batch_done[-1]['total'] if batch_done else len(items)
                save_root = batch_done[-1].get('save_root', '') if batch_done else ''
                _update_job(job_id,
                    status='success' if success > 0 else 'error',
                    item_count=success, output_file=save_root,
                    log_json=json.dumps({
                        'total': total, 'success': success,
                        'items': [i.get('item', {}) for i in items]
                    }, ensure_ascii=False)[:8000],
                    finished_at=_now_iso())
            else:
                item = results[0]
                ok = item.get('status') == 'downloaded'
                _update_job(job_id, status='success' if ok else 'error',
                    output_file=item.get('path', '') or item.get('save_root', ''),
                    item_count=1 if ok else 0,
                    log_json=json.dumps(item, ensure_ascii=False)[:8000],
                    error=item.get('error', ''), finished_at=_now_iso())
        except subprocess.TimeoutExpired:
            _update_job(job_id, status='timeout', error='下载超时 (5min)',
                finished_at=_now_iso())
        except Exception as e:
            _update_job(job_id, status='error', error=str(e),
                finished_at=_now_iso(),
                log_json=traceback.format_exc()[:2000])

    thread = threading.Thread(target=run_reverse, daemon=True)
    thread.start()
    return jsonify({
        'success': True, 'message': '逆向下载已启动', 'job_id': job_id,
        'backend': 'reverse-engineered API'
    })


@app.route('/api/search', methods=['POST'])
def search_products():
    """按关键词搜索采集商品列表"""
    data = request.json
    platform = data.get('platform', '')
    keyword = data.get('keyword', '').strip()
    pages = min(data.get('pages', 1), 5)
    human_mode = data.get('human_mode', False)

    if not keyword:
        return jsonify({'success': False, 'error': '请输入搜索关键词'})

    search_cfg = SEARCH_CONFIGS.get(platform)
    if not search_cfg:
        return jsonify({'success': False, 'error': f'平台 {platform} 不支持关键词搜索'})

    from playwright.sync_api import sync_playwright
    all_items = []
    errors = []

    try:
        with sync_playwright() as p:
            for page_num in range(1, pages + 1):
                # 构建搜索URL
                url = search_cfg['search_url'].replace('{keyword}', keyword)
                url = url.replace('{page}', str(page_num))
                if '{page_start}' in url:
                    calc = search_cfg.get('page_start_calc', lambda p: (p-1)*44)
                    url = url.replace('{page_start}', str(calc(page_num)))

                browser, context = _create_context(p, None,
                    headless=not human_mode,
                    human_mode=human_mode)

                page = context.new_page()
                try:
                    _safe_goto(page, url, timeout=60000, human_mode=human_mode)
                    # 等待结果加载
                    try:
                        page.wait_for_timeout(5000)
                        page.wait_for_load_state('networkidle', timeout=15000)
                    except:
                        pass

                    # 提取搜索结果
                    js_code = search_cfg['extract_js']
                    raw = page.evaluate(js_code)
                    items = json.loads(raw) if isinstance(raw, str) else (raw or [])
                    # 去重
                    seen_links = set()
                    for item in items:
                        link = item.get('link', '')
                        if link and link not in seen_links:
                            seen_links.add(link)
                            item['platform'] = search_cfg['name']
                            item['keyword'] = keyword
                            item['page'] = page_num
                            all_items.append(item)

                    # 翻页
                    if page_num < pages and search_cfg.get('next_page_click'):
                        try:
                            has_next = page.evaluate(search_cfg['has_next_page'])
                            if has_next:
                                page.evaluate(search_cfg['next_page_click'])
                                page.wait_for_timeout(3000)
                        except:
                            pass

                except Exception as e:
                    errors.append(f'第{page_num}页: {str(e)}')
                finally:
                    browser.close()

        # 保存到数据库
        try:
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute('INSERT INTO scrape_history (created_at,platform,url,keyword,mode,item_count,status,data_json) VALUES (?,?,?,?,?,?,?,?)',
                (datetime.datetime.now().isoformat(), platform, f'search:{keyword}', keyword,
                 'search', len(all_items), 'success',
                 json.dumps(all_items[:50], ensure_ascii=False)))
            conn.commit(); conn.close()
        except:
            pass

        return jsonify({
            'success': True,
            'platform': search_cfg['name'],
            'keyword': keyword,
            'pages': pages,
            'total': len(all_items),
            'items': all_items[:100],
            'errors': errors,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()})


# ========== 数据看板 ==========

@app.route('/api/dashboard')
def dashboard():
    """统计数据：总抓取数、平台分布、最近趋势"""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # 总抓取数
    total = conn.execute('SELECT COUNT(*) as c FROM scrape_history').fetchone()['c']
    total_items = conn.execute('SELECT COALESCE(SUM(item_count),0) as c FROM scrape_history').fetchone()['c']

    # 各平台分布
    rows = conn.execute('SELECT platform, COUNT(*) as c, SUM(item_count) as items FROM scrape_history GROUP BY platform ORDER BY c DESC LIMIT 15').fetchall()
    platform_dist = [dict(r) for r in rows]

    # 最近7天趋势
    week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
    trend = conn.execute("""
        SELECT date(created_at) as day, COUNT(*) as c, SUM(item_count) as items
        FROM scrape_history WHERE created_at >= ? GROUP BY date(created_at) ORDER BY day
    """, (week_ago,)).fetchall()
    trend_data = [dict(r) for r in trend]

    conn.close()
    return jsonify({
        'total_scrapes': total,
        'total_items': total_items,
        'platform_distribution': platform_dist,
        'trend': trend_data,
    })


# ========== 店铺全商品采集 ==========

@app.route('/api/shop-crawl', methods=['POST'])
def shop_crawl():
    """店铺全商品采集"""
    data = request.json
    store_url = data.get('store_url', '').strip()
    pages = min(data.get('pages', 3), 10)

    if not store_url:
        return jsonify({'success': False, 'error': '请输入店铺链接'})

    platform = auto_detect_platform(store_url) or ''
    from playwright.sync_api import sync_playwright
    all_items = []

    try:
        with sync_playwright() as p:
            browser, context = _create_context(p, None, headless=True)
            page = context.new_page()
            _safe_goto(page, store_url, timeout=60000)
            page.wait_for_timeout(5000)

            for pg in range(pages):
                # 尝试找"所有商品"/"全部商品"入口
                try:
                    all_goods = page.query_selector('a[href*="search"], a:has-text("所有商品"), a:has-text("全部商品"), [class*="allProduct"], [class*="all-goods"]')
                    if all_goods:
                        all_goods.click()
                        page.wait_for_timeout(3000)
                except: pass

                # 提取当前页商品
                js = '''
                () => {
                    const items = document.querySelectorAll('[class*="item"], [class*="product"], [class*="goods"], li');
                    return JSON.stringify(Array.from(items).slice(0, 60).filter(el => {
                        const t = el.querySelector('[class*="title"], [class*="name"], a')?.textContent?.trim();
                        return t && t.length > 3;
                    }).map(el => ({
                        title: el.querySelector('[class*="title"], [class*="name"]')?.textContent?.trim() || el.querySelector('a')?.textContent?.trim() || '',
                        price: el.querySelector('[class*="price"]')?.textContent?.trim() || '',
                        link: el.querySelector('a')?.href || '',
                        image: el.querySelector('img')?.src || '',
                    })));
                }
                '''
                raw = page.evaluate(js)
                items = json.loads(raw) if isinstance(raw, str) else (raw or [])
                for item in items:
                    if item.get('title') and not any(x['title'] == item['title'] for x in all_items):
                        item['platform'] = platform
                        item['store_url'] = store_url
                        all_items.append(item)

                # 翻页
                try:
                    next_btn = page.query_selector('.next, a[class*="next"], .page-next, [class*="pagination"] .active + *')
                    if next_btn:
                        next_btn.click()
                        page.wait_for_timeout(3000)
                    else:
                        break
                except:
                    break

            browser.close()

        return jsonify({'success': True, 'store_url': store_url, 'platform': platform, 'total': len(all_items), 'items': all_items[:100]})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()})


# ========== 跨平台比价 ==========

@app.route('/api/compare', methods=['POST'])
def compare_prices():
    """跨平台比价：搜索同关键词，合并对比"""
    data = request.json
    keyword = data.get('keyword', '').strip()
    platforms = data.get('platforms', ['taobao', 'jd', 'pdd', 'ali1688'])

    if not keyword:
        return jsonify({'success': False, 'error': '请输入搜索关键词'})

    results = {}
    errors = []
    import concurrent.futures

    def _search_platform(pf):
        try:
            url = SEARCH_CONFIGS[pf]['search_url'].replace('{keyword}', keyword).replace('{page}', '1').replace('{page_start}', '0')
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p_ctx:
                browser, context = _create_context(p_ctx, None, headless=True)
                page = context.new_page()
                _safe_goto(page, url, timeout=45000)
                page.wait_for_timeout(5000)
                try: page.wait_for_load_state('networkidle', timeout=10000)
                except: pass
                raw = page.evaluate(SEARCH_CONFIGS[pf]['extract_js'])
                items = json.loads(raw) if isinstance(raw, str) else (raw or [])
                browser.close()
                return pf, [{'title': i.get('title',''), 'price': i.get('price',''), 'sales': i.get('sales',''), 'shop': i.get('shop',''), 'link': i.get('link',''), 'image': i.get('image',''), 'platform': SEARCH_CONFIGS[pf]['name']} for i in items[:10]]
        except Exception as e:
            return pf, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_search_platform, pf): pf for pf in platforms if pf in SEARCH_CONFIGS}
        for future in concurrent.futures.as_completed(futures):
            pf, items = future.result()
            results[pf] = items
            if not items: errors.append(f'{pf} 无结果')

    # 简单合并：title 相似度匹配
    merged = []
    seen = set()
    for pf, items in results.items():
        for item in items:
            t = item['title'][:20]
            if t and t not in seen:
                seen.add(t)
                row = {'keyword': keyword, 'match_key': t}
                row[f'{pf}_price'] = item['price']
                row[f'{pf}_sales'] = item['sales']
                row[f'{pf}_shop'] = item['shop']
                row[f'{pf}_link'] = item['link']
                row[f'{pf}_platform'] = SEARCH_CONFIGS[pf]['name']
                merged.append(row)
            elif t:
                for row in merged:
                    if row['match_key'] == t:
                        row[f'{pf}_price'] = item['price']
                        row[f'{pf}_sales'] = item['sales']
                        row[f'{pf}_shop'] = item['shop']
                        row[f'{pf}_link'] = item['link']
                        row[f'{pf}_platform'] = SEARCH_CONFIGS[pf]['name']

    return jsonify({'success': True, 'keyword': keyword, 'platforms': list(results.keys()), 'total': len(merged), 'comparison': merged[:50], 'errors': errors})


# ========== 评价分析 ==========

@app.route('/api/review', methods=['POST'])
def review_analysis():
    """提取商品评价 + 简单情感分析"""
    data = request.json
    url = data.get('url', '').strip()
    if not url: return jsonify({'success': False, 'error': '请输入商品链接'})

    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as p:
            browser, context = _create_context(p, None, headless=True)
            page = context.new_page()
            _safe_goto(page, url, timeout=60000)
            page.wait_for_timeout(5000)

            # 点击评价tab
            try:
                tab = page.query_selector('a:has-text("评价"), span:has-text("评价"), [class*="review"], [class*="comment"]')
                if tab: tab.click(); page.wait_for_timeout(3000)
            except: pass

            # 提取评价
            reviews_js = '''
            () => {
                const items = document.querySelectorAll('[class*="review"], [class*="comment"], .rate-item, .comment-item, [class*="feedback"]');
                return JSON.stringify(Array.from(items).slice(0, 50).map(el => ({
                    content: el.textContent?.trim()?.slice(0, 200) || '',
                    author: el.querySelector('[class*="user"], [class*="name"]')?.textContent?.trim() || '',
                    rating: el.querySelector('[class*="star"], [class*="rate"], [class*="score"]')?.textContent?.trim() || '',
                })).filter(r => r.content && r.content.length > 5));
            }
            '''
            raw = page.evaluate(reviews_js)
            reviews = json.loads(raw) if isinstance(raw, str) else (raw or [])
            browser.close()

        # 简单情感分析
        GOOD_KW = ['好', '棒', '满意', '推荐', '不错', '喜欢', '赞', '值得', '超值', '性价比', '好看', '实用', '舒服']
        BAD_KW = ['差', '垃圾', '失望', '退货', '退款', '差评', '不好', '不行', '坏', '破', '烂', '假', '骗']
        for r in reviews:
            c = r.get('content', '')
            good = sum(1 for kw in GOOD_KW if kw in c)
            bad = sum(1 for kw in BAD_KW if kw in c)
            r['sentiment'] = '好评' if good > bad else ('差评' if bad > good else '中性')

        good_count = sum(1 for r in reviews if r.get('sentiment') == '好评')
        bad_count = sum(1 for r in reviews if r.get('sentiment') == '差评')
        neutral_count = len(reviews) - good_count - bad_count

        return jsonify({
            'success': True, 'url': url,
            'total': len(reviews),
            'good': good_count, 'bad': bad_count, 'neutral': neutral_count,
            'reviews': reviews[:50],
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def smart_extract_api(url, use_login=True, human_mode=False, device='pc', proxy_url=''):
    """智能提取：自动分析页面内容"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(p, None,
                headless=not human_mode and device == 'pc',
                human_mode=human_mode,
                device=device,
                proxy_url=proxy_url or None)
            page = context.new_page()
            if human_mode:
                _inject_stealth(page)
            _safe_goto(page, url, timeout=90000 if human_mode else 60000, human_mode=human_mode)

            data = _smart_extract(page)
            browser.close()

        if data:
            results = []
            for key, val in data.items():
                if isinstance(val, list):
                    items = [json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else str(v) for v in val]
                elif isinstance(val, dict):
                    items = [json.dumps(val, ensure_ascii=False)]
                else:
                    items = [str(val)]
                results.append({
                    'key': key,
                    'name': {'page_title': '页面标题', 'description': '页面描述',
                             'text_blocks': '文本内容', 'links': '链接列表',
                             'images': '图片列表', 'json_ld': '结构化数据'}.get(key, key),
                    'count': len(items),
                    'items': items,
                    'source': 'smart_extract'
                })
            return jsonify({'success': True, 'results': results, 'url': url, 'smart': True})
        else:
            return jsonify({'success': False, 'error': '无法从页面提取到数据，请检查链接或尝试登录后抓取'})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/preview', methods=['POST'])
def preview():
    """预览页面"""
    data = request.json
    url = data.get('url', '').strip()
    platform = data.get('platform', '')
    use_login = data.get('use_login', False)
    device = data.get('device', 'pc')

    if not url:
        return jsonify({'success': False, 'error': '请输入 URL'})

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(p, platform if use_login else None,
                headless=True, device=device)
            page = context.new_page()
            _safe_goto(page, url, timeout=60000)
            title = page.title()
            body_text = page.inner_text('body')[:2000]
            browser.close()

        return jsonify({'success': True, 'title': title, 'preview': body_text})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/screenshot', methods=['POST'])
def screenshot():
    """截取页面截图（用已登录浏览器）"""
    data = request.json
    url = data.get('url', '').strip()
    platform = data.get('platform', '')
    use_login = data.get('use_login', False)

    if not url:
        return jsonify({'success': False, 'error': '请输入 URL'})

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser, context = _create_context(p, platform if use_login else None, headless=True)
            page = context.new_page()
            _safe_goto(page, url, timeout=60000)
            import base64, io
            screenshot_bytes = page.screenshot(full_page=False)
            img_b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
            browser.close()

        return jsonify({'success': True, 'image': img_b64})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/export', methods=['POST'])
def export_data():
    """导出数据"""
    data = request.json
    fmt = data.get('format', 'json')
    results = data.get('results', [])

    if fmt == 'json':
        export = {}
        for r in results:
            name = r.get('name', r.get('key', '数据'))
            items = r.get('items', [])
            if r.get('count', 0) == 1:
                export[name] = items[0] if items else ''
            else:
                export[name] = items
        return app.response_class(
            response=json.dumps(export, ensure_ascii=False, indent=2),
            mimetype='application/json',
            headers={'Content-Disposition': 'attachment; filename=scraped_data.json'}
        )
    elif fmt == 'csv':
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        headers = []
        cols = []
        max_rows = 0
        for r in results:
            name = r.get('name', r.get('key', '数据'))
            headers.append(name)
            cols.append(r.get('items', []))
            max_rows = max(max_rows, r.get('count', 0))
        writer.writerow(headers)
        for i in range(max_rows):
            row = [col[i] if i < len(col) else '' for col in cols]
            writer.writerow(row)
        return app.response_class(
            response=output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=scraped_data.csv'}
        )
    return jsonify({'success': False, 'error': '不支持的格式'})


# ==============================
# 抖音商品采集器集成
# ==============================

CRAWLER_DIR = BASE_DIR
CRAWLER_VENV_PYTHON = Path(os.environ.get('COLLECTOR_PYTHON', str(BASE_DIR.parents[1] / '.venv/Scripts/python.exe')))
CRAWLER_SCRIPTS = BASE_DIR / 'crawler_scripts'
CRAWLER_SRC = BASE_DIR
CRAWLER_OUTPUT = BASE_DIR / 'crawler_output'

# 确保 ADB 在 PATH 中（商品采集器需要）
_ADB_DIRS = [
    BASE_DIR.parents[1] / 'platform-tools',
    BASE_DIR / 'platform-tools',
    BASE_DIR.parent / 'douyin_product_crawler' / 'platform-tools',
    BASE_DIR.parent / 'douyin_product_crawler' / 'android-sdk' / 'platform-tools',
    Path('D:/douyin_product_crawler/platform-tools'),
    Path('D:/douyin_product_crawler/android-sdk/platform-tools'),
    Path.home() / 'AppData' / 'Local' / 'Android' / 'Sdk' / 'platform-tools',
]
for _adb_dir in _ADB_DIRS:
    if _adb_dir.exists() and str(_adb_dir) not in os.environ.get('PATH', ''):
        os.environ['PATH'] = str(_adb_dir) + os.pathsep + os.environ.get('PATH', '')
        break

def _adb_executable():
    env_adb = os.environ.get('ADB_EXE', '').strip()
    candidates = []
    if env_adb:
        candidates.append(Path(env_adb))
    candidates.extend(_adb_dir / 'adb.exe' for _adb_dir in _ADB_DIRS)
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return 'adb'

def _run_adb(*args, timeout=5):
    return subprocess.run(
        [_adb_executable(), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

def _list_android_devices():
    info = {'ok': False, 'adb_path': _adb_executable(), 'devices': [], 'error': ''}
    try:
        result = _run_adb('devices', '-l', timeout=5)
    except FileNotFoundError:
        info['error'] = 'ADB not found. Set ADB_EXE or install Android platform-tools.'
        return info
    except subprocess.TimeoutExpired:
        info['error'] = 'ADB devices timed out.'
        return info
    except Exception as exc:
        info['error'] = str(exc)
        return info

    if result.returncode != 0:
        info['error'] = (result.stderr or result.stdout or 'adb devices failed').strip()
        return info

    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('List of devices'):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        meta = {}
        for token in parts[2:]:
            if ':' in token:
                key, value = token.split(':', 1)
                meta[key] = value
        model = meta.get('model') or meta.get('device') or ''
        product = meta.get('product') or ''
        is_emulator = serial.startswith('emulator-')
        info['devices'].append({
            'serial': serial,
            'state': state,
            'kind': 'emulator' if is_emulator else 'phone',
            'is_emulator': is_emulator,
            'model': model,
            'product': product,
            'label': model or product or serial,
            'raw': line,
        })

    info['ok'] = True
    return info

def _select_android_device(serial=''):
    serial = str(serial or '').strip()
    device_info = _list_android_devices()
    if not device_info['ok']:
        return None, device_info.get('error') or 'ADB unavailable'

    devices = device_info['devices']
    if serial:
        selected = next((item for item in devices if item['serial'] == serial), None)
        if not selected:
            return None, f'Android device not found: {serial}'
        if selected['state'] != 'device':
            return None, f'Android device {serial} is {selected["state"]}'
        return selected, ''

    ready = [item for item in devices if item['state'] == 'device']
    if not ready:
        if devices:
            states = ', '.join(f'{item["serial"]}:{item["state"]}' for item in devices)
            return None, f'No authorized Android device. Current states: {states}'
        return None, 'No Android device detected by ADB.'

    phone = next((item for item in ready if not item['is_emulator']), None)
    return phone or ready[0], ''

def _android_app_installed(serial='', package_name=''):
    serial = str(serial or '').strip()
    package_name = str(package_name or '').strip()
    if not serial:
        return {'ok': False, 'message': '等待选择 Android 设备', 'package_name': package_name}
    if not package_name:
        return {'ok': False, 'message': '平台未配置 App 包名', 'package_name': package_name}
    try:
        result = _run_adb(
            '-s', serial,
            'shell', 'pm', 'list', 'packages', package_name,
            timeout=8,
        )
    except subprocess.TimeoutExpired:
        return {'ok': False, 'message': f'检查 {package_name} 安装状态超时', 'package_name': package_name}
    except Exception as exc:
        return {'ok': False, 'message': str(exc), 'package_name': package_name}

    output = result.stdout or ''
    if result.returncode != 0:
        message = (result.stderr or output or f'无法检查 {package_name}').strip()
        return {'ok': False, 'message': message, 'package_name': package_name}

    installed_packages = [
        line.strip().removeprefix('package:')
        for line in output.splitlines()
        if line.strip().startswith('package:')
    ]
    if package_name in installed_packages:
        return {'ok': True, 'message': f'已安装 {package_name}', 'package_name': package_name}
    return {'ok': False, 'message': f'未安装目标 App: {package_name}', 'package_name': package_name}

def _safe_int(value, default=1, min_value=1, max_value=20):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))

def _safe_filename_part(value, fallback='crawler'):
    text = str(value or '').strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', '-', text)
    text = re.sub(r'\s+', '-', text)
    text = text.strip('.-_ ')
    return (text or fallback)[:80]

def _crawler_output_path(keyword, platform, excel_name=''):
    base = _safe_filename_part(excel_name or f'{keyword}_{platform}_result', 'crawler_result')
    if not base.lower().endswith('.xlsx'):
        base += '.xlsx'
    path = (CRAWLER_OUTPUT / base).resolve()
    output_root = CRAWLER_OUTPUT.resolve()
    if path.parent != output_root:
        path = output_root / path.name
    return path

def _crawler_preflight(data=None, require_device=False):
    data = data or {}
    keyword = str(data.get('keyword') or '').strip()
    mode = str(data.get('mode') or 'ui').strip().lower()
    platform = str(data.get('platform') or 'douyin').strip().lower()
    pages = _safe_int(data.get('pages', 3), default=3, min_value=1, max_value=20)
    excel_name = str(data.get('excel_name') or f'{keyword}_result').strip()
    serial = str(data.get('serial') or data.get('device_serial') or '').strip()

    from shared.platform_configs import PLATFORM_EMULATOR_CONFIGS
    platform_config = PLATFORM_EMULATOR_CONFIGS.get(platform, {})
    package_name = str(platform_config.get('package_name') or '').strip()
    script = CRAWLER_SCRIPTS / ('run_api_crawler.ps1' if mode == 'api' else 'run_ui_crawler.ps1')
    output_file = _crawler_output_path(keyword or 'crawler', platform, excel_name)
    device_info = _list_android_devices()
    selected_device, device_error = _select_android_device(serial)
    if selected_device:
        app_check = _android_app_installed(selected_device['serial'], package_name)
    elif platform_config:
        app_check = {'ok': False, 'message': f'等待设备连接后检查 {package_name}', 'package_name': package_name}
    else:
        app_check = {'ok': False, 'message': f'平台配置不存在: {platform}', 'package_name': package_name}

    checks = {
        'keyword': {'ok': bool(keyword), 'message': '关键词已填写' if keyword else '请输入搜索关键词'},
        'mode': {'ok': mode in {'ui', 'api'}, 'message': f'采集模式: {mode}'},
        'platform': {'ok': platform in PLATFORM_EMULATOR_CONFIGS, 'message': f'平台: {platform}'},
        'venv': {'ok': CRAWLER_VENV_PYTHON.exists(), 'message': str(CRAWLER_VENV_PYTHON)},
        'source': {'ok': (CRAWLER_SRC / 'douyin_crawler').exists(), 'message': str(CRAWLER_SRC / 'douyin_crawler')},
        'script': {'ok': script.exists(), 'message': str(script)},
        'adb': {'ok': device_info.get('ok', False), 'message': device_info.get('error') or device_info.get('adb_path', 'adb')},
        'device': {
            'ok': selected_device is not None,
            'message': selected_device['serial'] if selected_device else (device_error or 'No Android device detected by ADB.'),
        },
        'app': app_check,
        'output': {'ok': True, 'message': str(output_file)},
    }

    try:
        CRAWLER_OUTPUT.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        checks['output'] = {'ok': False, 'message': str(exc)}

    required = ['keyword', 'mode', 'platform', 'venv', 'source', 'script', 'adb', 'output']
    if require_device:
        required.append('device')
        required.append('app')
    ok = all(checks[key]['ok'] for key in required)
    errors = [checks[key]['message'] for key in required if not checks[key]['ok']]

    return {
        'ok': ok,
        'errors': errors,
        'checks': checks,
        'keyword': keyword,
        'pages': pages,
        'mode': mode,
        'platform': platform,
        'package_name': package_name,
        'script': str(script),
        'output_file': str(output_file),
        'selected_device': selected_device,
        'devices': device_info.get('devices', []),
        'adb_path': device_info.get('adb_path', ''),
    }

_crawler_process = None
_crawler_status = {
    'running': False, 'mode': '', 'keyword': '', 'pages': 0, 'platform': '',
    'output': '', 'serial': '', 'device': None, 'started_at': '', 'finished_at': '',
    'returncode': None, 'item_count': 0, 'job_id': None, 'stop_requested': False,
    'log': [], 'progress': None
}


@app.route('/api/crawler/info')
def crawler_info():
    """返回采集器配置信息和状态"""
    device_info = _list_android_devices()
    selected_device, device_error = _select_android_device()
    emulator_running = any(
        item['is_emulator'] and item['state'] == 'device'
        for item in device_info.get('devices', [])
    )
    from shared.platform_configs import PLATFORM_EMULATOR_CONFIGS
    platforms = {}
    for k, v in PLATFORM_EMULATOR_CONFIGS.items():
        platforms[k] = {'name': v['name'], 'icon': v.get('icon', '📱')}
    return jsonify({
        'project_dir': str(CRAWLER_DIR),
        'emulator_running': emulator_running,
        'device_running': selected_device is not None,
        'selected_device': selected_device,
        'devices': device_info.get('devices', []),
        'adb_path': device_info.get('adb_path', ''),
        'adb_ok': device_info.get('ok', False),
        'adb_error': device_info.get('error') or device_error,
        'venv_exists': CRAWLER_VENV_PYTHON.exists(),
        'platforms': platforms,
        'scripts': {
            'run_ui': str(CRAWLER_SCRIPTS / 'run_ui_crawler.ps1'),
            'run_api': str(CRAWLER_SCRIPTS / 'run_api_crawler.ps1'),
            'start_emu': str(CRAWLER_SCRIPTS / 'start_pixel_emulator.ps1'),
        },
        'status': _crawler_status,
    })


@app.route('/api/crawler/devices')
def crawler_devices():
    """Return connected Android devices from ADB."""
    return jsonify(_list_android_devices())


@app.route('/api/crawler/preflight', methods=['GET', 'POST'])
def crawler_preflight():
    """Validate crawler settings before starting a run."""
    data = request.get_json(silent=True) if request.method == 'POST' else request.args.to_dict()
    data = data or {}
    require_device = str(data.get('require_device') or '').lower() in {'1', 'true', 'yes', 'on'}
    return jsonify(_crawler_preflight(data, require_device=require_device))


@app.route('/api/crawler/diagnostics', methods=['GET', 'POST'])
def crawler_diagnostics():
    """Return actionable diagnostics for Android App crawling."""
    data = request.get_json(silent=True) if request.method == 'POST' else request.args.to_dict()
    data = data or {}
    preflight = _crawler_preflight(data, require_device=True)
    device_info = _list_android_devices()
    checks = preflight.get('checks', {})
    recommendations = []

    if not checks.get('adb', {}).get('ok'):
        recommendations.append('先确认 ADB 路径可用，或把 platform-tools 加到 PATH。')
    elif not checks.get('device', {}).get('ok'):
        recommendations.append('手机开启开发者选项和 USB 调试，插线后在手机弹窗里允许本机调试。')
        recommendations.append('如果设备显示 unauthorized，拔插 USB 后重新授权，或执行 adb kill-server 后再连接。')
    elif not checks.get('app', {}).get('ok'):
        recommendations.append('目标 App 未安装或包名不匹配；请在这台手机安装对应平台 App 后重新预检。')
    if not checks.get('keyword', {}).get('ok'):
        recommendations.append('填写关键词后再运行，任务中心会用关键词生成输出文件名。')
    if not checks.get('script', {}).get('ok') or not checks.get('source', {}).get('ok'):
        recommendations.append('检查 douyin_crawler 子项目和 crawler_scripts 是否完整。')
    if not checks.get('venv', {}).get('ok'):
        recommendations.append('先安装 scraper-ui 使用的 Python 虚拟环境依赖。')
    if preflight.get('mode') == 'api':
        recommendations.append('API 抓包模式需要代理和证书环境；优先用 UI 可见文本模式确认真机流程。')
    if preflight.get('ok'):
        recommendations.append('运行条件已满足，可以开始采集。')

    return jsonify({
        'ok': preflight.get('ok', False),
        'checks': checks,
        'errors': preflight.get('errors', []),
        'recommendations': recommendations,
        'devices': device_info.get('devices', []),
        'adb_path': device_info.get('adb_path', ''),
        'selected_device': preflight.get('selected_device'),
    })


@app.route('/api/crawler/emulator', methods=['POST'])
def crawler_start_emulator():
    """启动 Android 模拟器"""
    try:
        script = CRAWLER_SCRIPTS / 'start_pixel_emulator.ps1'
        if not script.exists():
            return jsonify({'success': False, 'error': '启动脚本不存在'})

        subprocess.Popen(['powershell', '-File', str(script)],
                         cwd=str(CRAWLER_DIR),
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return jsonify({'success': True, 'message': '模拟器正在启动（需要 2-3 分钟）'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/crawler/run', methods=['POST'])
def crawler_run():
    """运行采集器"""
    global _crawler_process, _crawler_status

    data = request.get_json(silent=True) or {}
    preflight = _crawler_preflight(data, require_device=True)
    if not preflight['ok']:
        return jsonify({'success': False, 'error': '; '.join(preflight['errors']), 'preflight': preflight})

    if _crawler_status['running']:
        return jsonify({'success': False, 'error': '采集器正在运行中'})

    keyword = preflight['keyword']
    pages = preflight['pages']
    mode = preflight['mode']
    platform = preflight['platform']
    output_file = preflight['output_file']
    selected_device = preflight['selected_device']
    selected_serial = selected_device['serial']
    job_id = _create_job(
        task_type='app_product',
        platform=platform,
        keyword=keyword,
        pages=pages,
        mode=mode,
        output_file=output_file,
        serial=selected_serial,
    )

    # 更新状态
    _crawler_status = {
        'running': True,
        'mode': mode,
        'keyword': keyword,
        'pages': pages,
        'platform': platform,
        'output': output_file,
        'serial': selected_serial,
        'device': selected_device,
        'started_at': _now_iso(),
        'finished_at': '',
        'returncode': None,
        'item_count': 0,
        'job_id': job_id,
        'stop_requested': False,
        'log': []
    }

    # 在后台线程运行
    def run_crawler():
        global _crawler_status
        final_status = 'error'
        error_text = ''
        item_count = 0
        try:
            env = os.environ.copy()
            env['PYTHONPATH'] = str(CRAWLER_SRC)

            # 构造命令行
            cmd = [
                str(CRAWLER_VENV_PYTHON), '-m', 'douyin_crawler.cli',
                'run-ui' if mode == 'ui' else 'run',
                '--keyword', keyword,
                '--pages', str(pages),
                '--excel', output_file,
                '--serial', selected_serial,
                '--platform', platform,
            ]

            _crawler_status['log'].append(
                f'Android device: {selected_serial} ({selected_device.get("label", "")})'
            )
            _crawler_status['log'].append(f'启动: {" ".join(cmd)}')

            process = subprocess.Popen(
                cmd,
                cwd=str(CRAWLER_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            global _crawler_process
            _crawler_process = process

            # 读取输出 + 进度解析 + 超时控制
            import threading as _thr
            crawl_timeout = 600  # 10分钟超时
            timed_out = False

            def _kill_on_timeout():
                nonlocal timed_out
                try:
                    process.wait(timeout=crawl_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    process.kill()

            timer = _thr.Thread(target=_kill_on_timeout, daemon=True)
            timer.start()

            for line in process.stdout:
                line = line.strip()
                if line:
                    _crawler_status['log'].append(line)
                    # 解析进度: "Triggering page X/Y" 或 "Reading visible page X/Y"
                    m = re.search(r'(Triggering|Reading visible) page (\d+)/(\d+)', line)
                    if m:
                        _crawler_status['progress'] = {'current': int(m.group(2)), 'total': int(m.group(3))}

            timer.join(timeout=2)
            _crawler_status['progress'] = None

            if timed_out:
                _crawler_status['log'].append(f'⏰ 采集超时 (>{crawl_timeout}s)，已终止')
                final_status = 'timeout'
                error_text = f'采集超时 (>{crawl_timeout}s)'
            elif process.returncode == 0:
                item_count = _read_excel_row_count(output_file)
                _crawler_status['item_count'] = item_count
                _crawler_status['log'].append('✅ 采集完成')
                final_status = 'success'
            else:
                if _crawler_status.get('stop_requested'):
                    _crawler_status['log'].append('⏹ 采集已停止')
                    final_status = 'stopped'
                    error_text = '用户手动停止'
                else:
                    error_text = f'exit code: {process.returncode}'
                    _crawler_status['log'].append(f'❌ 采集失败 ({error_text})')
            _crawler_status['returncode'] = process.returncode

        except Exception as e:
            error_text = str(e)
            _crawler_status['log'].append(f'❌ 错误: {str(e)}')
            # API模式证书失败 → 自动回退 UI模式提示
            if mode == 'api' and not _crawler_status.get('stop_requested'):
                log_text = '\n'.join(_crawler_status.get('log', []))
                if 'certificate' in log_text.lower() or 'does not trust the proxy' in log_text.lower():
                    _crawler_status['log'].append(
                        '💡 证书验证失败，建议切换到 UI可见文本模式 (mode=ui) 重新采集'
                    )
        finally:
            _crawler_status['running'] = False
            _crawler_status['finished_at'] = _now_iso()
            _crawler_process = None
            _update_job(
                job_id,
                status=final_status,
                item_count=item_count,
                output_file=output_file,
                error=error_text,
                log_json=json.dumps(_crawler_status.get('log', [])[-300:], ensure_ascii=False),
                finished_at=_crawler_status['finished_at'],
            )

    # 无设备时自动启动模拟器
    if not selected_device and any(
        item['is_emulator'] for item in preflight.get('devices', [])
    ):
        _crawler_status['log'].append('⚠ 未检测到在线设备，尝试启动模拟器...')
        emu_script = CRAWLER_SCRIPTS / 'start_pixel_emulator.ps1'
        if emu_script.exists():
            subprocess.Popen(
                ['powershell', '-File', str(emu_script)],
                cwd=str(CRAWLER_DIR),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _crawler_status['log'].append('🔄 模拟器正在启动（约2-3分钟），请等待...')
            # 等30秒让模拟器有机会上线
            time.sleep(5)
            retry_device, _ = _select_android_device()
            if retry_device:
                _crawler_status['log'].append(f'✅ 设备已上线: {retry_device["serial"]}')
            else:
                _crawler_status['log'].append('⚠ 模拟器启动中，稍后可手动重试采集')
        else:
            _crawler_status['log'].append(f'❌ 启动脚本不存在: {emu_script}')

    thread = threading.Thread(target=run_crawler, daemon=True)
    thread.start()

    return jsonify({'success': True, 'message': '采集已启动', 'job_id': job_id})


@app.route('/api/crawler/status')
def crawler_get_status():
    """获取采集器运行状态"""
    return jsonify(_crawler_status)


@app.route('/api/crawler/stop', methods=['POST'])
def crawler_stop():
    """停止采集"""
    global _crawler_process, _crawler_status
    _crawler_status['stop_requested'] = True
    if _crawler_process and _crawler_process.poll() is None:
        _crawler_process.terminate()
        try:
            _crawler_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            _crawler_process.kill()
        _crawler_status['log'].append('⏹ 已手动停止')
    _crawler_status['running'] = False
    _crawler_status['finished_at'] = _now_iso()
    _update_job(
        _crawler_status.get('job_id'),
        status='stopped',
        error='用户手动停止',
        log_json=json.dumps(_crawler_status.get('log', [])[-300:], ensure_ascii=False),
        finished_at=_crawler_status['finished_at'],
    )
    _crawler_process = None
    return jsonify({'success': True})


@app.route('/api/crawler/results')
def crawler_results():
    """获取最新采集结果（读取 Excel 数据）"""
    try:
        output_dir = CRAWLER_OUTPUT
        xlsx_files = sorted(output_dir.glob('*.xlsx'), key=os.path.getmtime, reverse=True)
        if not xlsx_files:
            return jsonify({'success': False, 'error': '暂无结果文件', 'files': []})

        latest = xlsx_files[0]
        import pandas as pd
        df = pd.read_excel(latest)
        data = df.fillna('').to_dict(orient='records')

        files_info = []
        for f in xlsx_files[:10]:
            size = f.stat().st_size
            mtime = f.stat().st_mtime
            rows = None
            if f == latest:
                rows = len(data)
            files_info.append({
                'name': f.name,
                'path': str(f),
                'size': size,
                'mtime': mtime,
                'rows': rows
            })

        return jsonify({
            'success': True,
            'file': latest.name,
            'columns': list(df.columns),
            'count': len(data),
            'data': data[:100],  # 最多返回 100 条
            'files': files_info,
        })
    except ImportError:
        return jsonify({'success': False, 'error': '缺少 pandas，请安装'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


def _check_emulator():
    """检查模拟器是否正在运行"""
    device_info = _list_android_devices()
    if not device_info.get('ok'):
        return False
    return any(
        item.get('is_emulator') and item.get('state') == 'device'
        for item in device_info.get('devices', [])
    )


# ==============================
# MediaCrawler 集成
# ==============================

MEDIA_CRAWLER_DIR = BASE_DIR / 'media_crawler'
MEDIA_CRAWLER_VENV = Path(os.environ.get('MEDIA_CRAWLER_PYTHON', str(MEDIA_CRAWLER_DIR / '.venv' / 'Scripts' / 'python.exe')))

MEDIA_CRAWLER_PLATFORMS = {
    'xhs':    {'name': '小红书', 'icon': '📕', 'color': '#FF2442'},
    'dy':     {'name': '抖音',   'icon': '🎵', 'color': '#161823'},
    'ks':     {'name': '快手',   'icon': '📷', 'color': '#FF4906'},
    'bili':   {'name': 'B站',    'icon': '📺', 'color': '#FB7299'},
    'wb':     {'name': '微博',   'icon': '🐦', 'color': '#E6162D'},
    'tieba':  {'name': '贴吧',   'icon': '📋', 'color': '#4B6EFF'},
    'zhihu':  {'name': '知乎',   'icon': '💡', 'color': '#056DE8'},
}

_mc_process = None
_mc_status = {
    'running': False, 'platform': '', 'type': '', 'keyword': '',
    'started_at': '', 'finished_at': '', 'returncode': None,
    'item_count': 0, 'job_id': None, 'stop_requested': False, 'log': []
}


@app.route('/api/media-crawler/platforms')
def mc_platforms():
    """返回 MediaCrawler 支持的平台列表"""
    return jsonify(MEDIA_CRAWLER_PLATFORMS)


@app.route('/api/media-crawler/run', methods=['POST'])
def mc_run():
    """启动 MediaCrawler 采集"""
    global _mc_process, _mc_status

    data = request.json
    platform = data.get('platform', '')
    crawl_type = data.get('type', 'search')  # search | detail
    keyword = data.get('keyword', '')
    login_type = data.get('login_type', 'qrcode')
    pages = data.get('pages', 5)

    if not platform or platform not in MEDIA_CRAWLER_PLATFORMS:
        return jsonify({'success': False, 'error': '请选择平台'})

    if _mc_status['running']:
        return jsonify({'success': False, 'error': 'MediaCrawler 正在运行中'})

    job_id = _create_job(
        task_type='media',
        platform=platform,
        keyword=keyword,
        pages=pages,
        mode=crawl_type,
    )
    _mc_status = {'running': True, 'platform': platform, 'type': crawl_type,
                  'keyword': keyword, 'started_at': _now_iso(), 'finished_at': '',
                  'returncode': None, 'item_count': 0, 'job_id': job_id,
                  'stop_requested': False, 'log': []}

    def run_mc():
        global _mc_status
        final_status = 'error'
        error_text = ''
        output_file = ''
        item_count = 0
        try:
            env = os.environ.copy()
            env['PYTHONPATH'] = str(MEDIA_CRAWLER_DIR)
            env['NODE_OPTIONS'] = ''  # 避免 node 警告

            # 设置环境变量（MediaCrawler 从 config 模块读取配置）
            env['PLATFORM'] = platform
            env['KEYWORDS'] = keyword
            env['LOGIN_TYPE'] = login_type
            env['CRAWLER_TYPE'] = crawl_type
            env['START_PAGE'] = '1'
            env['CRAWL_MAX_COUNT'] = str(pages * 10)
            env['SAVE_DATA_OPTION'] = 'excel'

            cmd = [str(MEDIA_CRAWLER_VENV), 'main.py']
            _mc_status['log'].append(f'启动: {" ".join(cmd)}')
            _mc_status['log'].append(f'平台: {MEDIA_CRAWLER_PLATFORMS[platform]["name"]}  关键词: {keyword}')

            process = subprocess.Popen(
                cmd,
                cwd=str(MEDIA_CRAWLER_DIR),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            global _mc_process
            _mc_process = process

            for line in process.stdout:
                line = line.strip()
                if line:
                    _mc_status['log'].append(line)

            process.wait()
            if process.returncode == 0:
                output_file, item_count = _latest_excel_result(MEDIA_CRAWLER_DIR / 'data')
                _mc_status['item_count'] = item_count
                _mc_status['log'].append('✅ MediaCrawler 采集完成')
                final_status = 'success'
            else:
                if _mc_status.get('stop_requested'):
                    _mc_status['log'].append('⏹ MediaCrawler 已停止')
                    final_status = 'stopped'
                    error_text = '用户手动停止'
                else:
                    error_text = f'exit: {process.returncode}'
                    _mc_status['log'].append(f'❌ 采集失败 ({error_text})')
            _mc_status['returncode'] = process.returncode

        except Exception as e:
            error_text = str(e)
            _mc_status['log'].append(f'❌ 错误: {str(e)}')
        finally:
            _mc_status['running'] = False
            _mc_status['finished_at'] = _now_iso()
            _mc_process = None
            _update_job(
                job_id,
                status=final_status,
                item_count=item_count,
                output_file=output_file,
                error=error_text,
                log_json=json.dumps(_mc_status.get('log', [])[-300:], ensure_ascii=False),
                finished_at=_mc_status['finished_at'],
            )

    thread = threading.Thread(target=run_mc, daemon=True)
    thread.start()
    return jsonify({'success': True, 'message': 'MediaCrawler 已启动', 'job_id': job_id})


@app.route('/api/media-crawler/status')
def mc_status():
    """MediaCrawler 运行状态"""
    return jsonify(_mc_status)


@app.route('/api/media-crawler/stop', methods=['POST'])
def mc_stop():
    """停止 MediaCrawler"""
    global _mc_process, _mc_status
    _mc_status['stop_requested'] = True
    if _mc_process and _mc_process.poll() is None:
        _mc_process.terminate()
        _mc_status['log'].append('⏹ 已手动停止')
    _mc_status['running'] = False
    _mc_status['finished_at'] = _now_iso()
    _update_job(
        _mc_status.get('job_id'),
        status='stopped',
        error='用户手动停止',
        log_json=json.dumps(_mc_status.get('log', [])[-300:], ensure_ascii=False),
        finished_at=_mc_status['finished_at'],
    )
    _mc_process = None
    return jsonify({'success': True})


@app.route('/api/media-crawler/results')
def mc_results():
    """读取 MediaCrawler 采集结果"""
    import glob
    output_dir = MEDIA_CRAWLER_DIR / 'data'
    if not output_dir.exists():
        return jsonify({'success': False, 'error': '暂无结果', 'files': []})

    files_info = []
    for f in sorted(output_dir.rglob('*.xlsx'), key=os.path.getmtime, reverse=True)[:10]:
        files_info.append({
            'name': f.name,
            'path': str(f),
            'size': f.stat().st_size,
            'mtime': f.stat().st_mtime,
        })

    # 读取最新 Excel
    if files_info:
        try:
            import pandas as pd
            latest = output_dir / files_info[0]['name']
            df = pd.read_excel(latest)
            data = df.fillna('').to_dict(orient='records')
            return jsonify({
                'success': True,
                'file': files_info[0]['name'],
                'columns': list(df.columns),
                'count': len(data),
                'data': data[:100],
                'files': files_info,
            })
        except Exception as e:
            return jsonify({'success': True, 'files': files_info, 'error': str(e)})

    return jsonify({'success': False, 'error': '暂无结果文件'})


# 京东 / 拼多多 SKU 详情采集 (专用路由)
import sys, json as _json
SKU_SCRAPER_PATH = str(BASE_DIR / 'crawler_scripts')
if SKU_SCRAPER_PATH not in sys.path:
    sys.path.insert(0, SKU_SCRAPER_PATH)
try:
    from jd_pdd_sku_scraper import scrape_sku as _scrape_jd_pdd_sku
except ImportError:
    _scrape_jd_pdd_sku = None

@app.route('/api/jd-pdd/scrape', methods=['POST'])
def jd_pdd_scrape():
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip()
    platform = data.get('platform', '')
    if not url:
        return jsonify({'success': False, 'error': '请输入商品链接'})
    if not platform:
        platform = auto_detect_platform(url) or ''
    if platform not in ('jd', 'pdd'):
        return jsonify({'success': False, 'error': f'平台 {platform} 不受支持'})
    job_id = _create_job(task_type='jd_pdd_sku', platform=platform, url=url, mode='sku_detail', status='running')
    try:
        if _scrape_jd_pdd_sku:
            result = _scrape_jd_pdd_sku(url, platform, headless=True)
        else:
            preset = PLATFORM_PRESETS.get(platform)
            if not preset:
                return jsonify({'success': False, 'error': f'平台 {platform} 无配置'})
            return scrape_with_js(url, preset, True)
        if result.get('ok'):
            _update_job(job_id, status='success', item_count=1, finished_at=_now_iso())
            return jsonify({'success': True, 'result': result, 'job_id': job_id})
        else:
            _update_job(job_id, status='error', error=result.get('error','unknown'), finished_at=_now_iso())
            return jsonify({'success': False, 'result': result, 'job_id': job_id,
                          'hint': result.get('hint',''), 'login_required': result.get('login_required',False)})
    except Exception as e:
        _update_job(job_id, status='error', error=str(e), finished_at=_now_iso())
        return jsonify({'success': False, 'error': str(e), 'job_id': job_id})

@app.route('/api/jd-pdd/login-guide')
def jd_pdd_login_guide():
    return jsonify({
        'jd': {'login_url': 'https://passport.jd.com/new/login.aspx',
               'how_to': '通过 /api/login 接口打开京东登录页扫码登录后 cookies 自动保存'},
        'pdd': {'login_url': 'https://mobile.yangkeduo.com/login.html',
                'how_to': '拼多多移动端优先，建议用移动端设备模拟登录'},
        'common': {'login_api': 'POST /api/login', 'scraper_ui': 'http://127.0.0.1:5566'}
    })

print("JD/PDD routes loaded successfully")


if __name__ == '__main__':
    port = int(os.environ.get('SCRAPER_UI_PORT', '5566'))
    print('🚀 Scraper-UI 启动!')
    print(f'   本地访问: http://127.0.0.1:{port}')
    print('   按 Ctrl+C 停止')
    app.run(host='127.0.0.1', port=port, debug=False)
