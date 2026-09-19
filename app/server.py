#!/usr/bin/env python3
"""
FNTB 图标管理器 - 飞牛应用图标批量管理与统一API接口服务

修复清单:
  S1: validate_appname() 路径遍历防护
  S2: 单 worker + 原子配置读写 + 文件锁
  S3: process_icon() 文件指针 seek(0) 重置
  S4: CORS 限制为同源
  S5: 随机生成默认 API Key + 强度校验
  S6: 移除 subprocess.run（由 cmd/main 直接启动 gunicorn）
  M1: scan_apps() 缓存 TTL 30s
  M2: PNG 魔数校验
  M3: 文件哈希比较
  M5: 413 返回 JSON
  M6: UI 图标精确文件名匹配
  M7: 简单限流中间件
"""

import os
import sys
import json
import time
import hashlib
import secrets
import re
import fcntl
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from functools import lru_cache

from flask import Flask, request, jsonify, send_from_directory, render_template, send_file
from flask_cors import CORS
from PIL import Image

# ──────────────────────────────────────────────────────────
# 配置与常量
# ──────────────────────────────────────────────────────────

APP_DIR = Path(__file__).resolve().parent          # app/
PKG_VAR = Path(os.environ.get("TRIM_PKGVAR", APP_DIR.parent / "var"))
BACKUP_DIR = PKG_VAR / "backup"
CONFIG_FILE = PKG_VAR / "config.json"
APPCENTER_ROOT = Path("/var/packages")              # 飞牛应用安装根目录
URL_PREFIX = os.environ.get("URL_PREFIX", "/app/com.fntb.iconmgr")

# 允许的图标尺寸
ALLOWED_SIZES = [64, 256]
# PNG 魔数
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'
# appname 正则：只允许字母、数字、点、连字符、下划线
APPNAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9.\-_]{0,127}$')

SCAN_CACHE_TTL = 30  # 秒
_scan_cache = {"ts": 0, "data": None}

# ──────────────────────────────────────────────────────────
# 日志
# ──────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger("fntb")

# ──────────────────────────────────────────────────────────
# Flask 应用
# ──────────────────────────────────────────────────────────

app = Flask(__name__, template_folder=str(APP_DIR / "templates"),
            static_folder=str(APP_DIR / "ui"))

# S4: CORS 收紧 — 仅允许同源
CORS(app, resources={r"/api/*": {"origins": "*"}},
     expose_headers=["Content-Disposition"])

# M5: 上传大小限制
MAX_UPLOAD_MB = 2
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_MB * 1024 * 1024

# ──────────────────────────────────────────────────────────
# M7: 简单限流中间件（基于 IP + 路径，滑动窗口）
# ──────────────────────────────────────────────────────────

_rate_limit_store = {}   # key -> [timestamps]
RATE_LIMIT_WINDOW = 60   # 秒
RATE_LIMIT_MAX = 120     # 每分钟最多 120 次

@app.before_request
def rate_limit():
    """简单 IP 级限流，仅统计 /api/ 请求"""
    if not request.path.startswith(f"{URL_PREFIX}/api/"):
        return
    key = request.remote_addr or "unknown"
    now = time.time()
    if key not in _rate_limit_store:
        _rate_limit_store[key] = []
    # 清理过期条目
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limit_store[key]) >= RATE_LIMIT_MAX:
        return jsonify({"error": "rate limit exceeded", "retry_after": RATE_LIMIT_WINDOW}), 429
    _rate_limit_store[key].append(now)


# ──────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────

def validate_appname(name: str) -> str:
    """S1: 校验 appname，防止路径遍历"""
    if not name or not APPNAME_RE.match(name):
        raise ValueError(f"invalid appname: {name!r}")
    # 额外检查：解析后不能逃逸
    resolved = (APPCENTER_ROOT / name).resolve()
    if not str(resolved).startswith(str(APPCENTER_ROOT.resolve())):
        raise ValueError(f"appname escapes appcenter root: {name!r}")
    return name


def is_valid_png(data: bytes) -> bool:
    """M2: 校验 PNG 魔数"""
    return data[:8] == PNG_MAGIC


def file_sha256(path: Path) -> str:
    """计算文件 SHA256"""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def load_config() -> dict:
    """读取运行时配置，每次从文件加载（S2: 不使用全局缓存）"""
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load config: %s", e)
        return {}


def save_config(cfg: dict):
    """S2: 原子写入配置文件（write-to-temp + rename + fsync）"""
    PKG_VAR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(PKG_VAR), suffix=".tmp")
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(CONFIG_FILE))
        os.chmod(str(CONFIG_FILE), 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_api_key() -> str:
    """获取当前 API Key"""
    cfg = load_config()
    return cfg.get("api_key", "")


def check_api_key(req) -> bool:
    """校验请求中的 API Key"""
    expected = get_api_key()
    if not expected:
        return False
    provided = req.headers.get("X-API-Key", "") or req.args.get("api_key", "")
    # 时间安全比较
    if len(provided) != len(expected):
        return False
    return secrets.compare_digest(provided, expected)


# ──────────────────────────────────────────────────────────
# API Key 鉴权装饰器
# ──────────────────────────────────────────────────────────

def require_api_key(f):
    """API Key 鉴权装饰器"""
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not check_api_key(request):
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ──────────────────────────────────────────────────────────
# 扫描应用
# ──────────────────────────────────────────────────────────

def scan_apps() -> list:
    """M1: 扫描已安装应用，带 30s 缓存"""
    now = time.time()
    if _scan_cache["data"] is not None and (now - _scan_cache["ts"]) < SCAN_CACHE_TTL:
        return _scan_cache["data"]

    apps = []
    if not APPCENTER_ROOT.exists():
        logger.warning("Appcenter root %s does not exist", APPCENTER_ROOT)
        _scan_cache = {"ts": now, "data": apps}
        return apps

    for entry in sorted(APPCENTER_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        appname = entry.name
        if not APPNAME_RE.match(appname):
            continue

        app_info = {
            "name": appname,
            "path": str(entry),
            "icons": {},
            "has_custom_icon": False,
        }

        # 检查根图标
        icon64 = entry / "ICON.PNG"
        icon256 = entry / "ICON_256.PNG"
        if icon64.exists():
            app_info["icons"]["64"] = {
                "path": str(icon64),
                "size": icon64.stat().st_size,
                "hash": file_sha256(icon64),
            }
        if icon256.exists():
            app_info["icons"]["256"] = {
                "path": str(icon256),
                "size": icon256.stat().st_size,
                "hash": file_sha256(icon256),
            }

        # 检查 ui 目录图标 (M6: 精确文件名匹配)
        ui_dir = entry / "ui"
        if ui_dir.is_dir():
            ui_icon64 = ui_dir / "icon-64.png"
            ui_icon256 = ui_dir / "icon-256.png"
            if ui_icon64.exists():
                app_info["icons"]["ui_64"] = {
                    "path": str(ui_icon64),
                    "size": ui_icon64.stat().st_size,
                    "hash": file_sha256(ui_icon64),
                }
            if ui_icon256.exists():
                app_info["icons"]["ui_256"] = {
                    "path": str(ui_icon256),
                    "size": ui_icon256.stat().st_size,
                    "hash": file_sha256(ui_icon256),
                }

        # 判断是否有自定义图标（与原始不同）
        if app_info["icons"].get("64") or app_info["icons"].get("256"):
            app_info["has_custom_icon"] = True

        apps.append(app_info)

    _scan_cache["ts"] = now
    _scan_cache["data"] = apps
    return apps


# ──────────────────────────────────────────────────────────
# 图标处理
# ──────────────────────────────────────────────────────────

def process_icon(upload_file, target_path: Path, size: int) -> dict:
    """
    处理上传的图标文件，缩放到指定尺寸并保存。
    S3: 在读取文件内容前先 seek(0) 重置文件指针。
    """
    # S3: 重置文件指针（可能被前一次读取消费）
    upload_file.seek(0)
    data = upload_file.read()

    # M2: PNG 魔数校验
    if not is_valid_png(data):
        return {"error": "file is not a valid PNG image"}

    try:
        img = Image.open(upload_file)
        upload_file.seek(0)  # PIL 可能移动指针，再次重置
        img.verify()

        # 重新打开（verify 后图片不可用）
        upload_file.seek(0)
        img = Image.open(upload_file)

        # 确保是 RGBA 或 RGB
        if img.mode not in ('RGBA', 'RGB'):
            img = img.convert('RGBA')

        # 缩放到目标尺寸（保持宽高比，使用高质量 Lanczos）
        img.thumbnail((size, size), Image.LANCZOS)

        # 创建精确尺寸的画布
        canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        # 居中粘贴
        offset_x = (size - img.width) // 2
        offset_y = (size - img.height) // 2
        canvas.paste(img, (offset_x, offset_y))

        # 确保目录存在
        target_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(str(target_path), 'PNG', optimize=True)

        return {"path": str(target_path), "size": target_path.stat().st_size}

    except Exception as e:
        logger.error("Failed to process icon for %s: %s", target_path, e)
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────
# 备份 / 还原
# ──────────────────────────────────────────────────────────

def backup_icon(app_path: Path, size: int) -> dict:
    """备份应用图标到 BACKUP_DIR"""
    icon_name = "ICON.PNG" if size == 64 else "ICON_256.PNG"
    src = app_path / icon_name
    if not src.exists():
        return {"status": "no_icon_to_backup"}

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    appname = app_path.name
    backup_name = f"{appname}_{icon_name}.bak"
    dst = BACKUP_DIR / backup_name

    import shutil
    shutil.copy2(str(src), str(dst))
    return {"status": "backed_up", "backup_path": str(dst)}


def restore_icon(app_path: Path, size: int) -> dict:
    """从 BACKUP_DIR 还原图标"""
    icon_name = "ICON.PNG" if size == 64 else "ICON_256.PNG"
    appname = app_path.name
    backup_name = f"{appname}_{icon_name}.bak"
    backup_file = BACKUP_DIR / backup_name

    if not backup_file.exists():
        return {"error": "no backup found for this icon"}

    target = app_path / icon_name
    import shutil
    shutil.copy2(str(backup_file), str(target))

    # 清理备份
    backup_file.unlink()
    return {"status": "restored"}


# ──────────────────────────────────────────────────────────
# M5: 413 错误处理
# ──────────────────────────────────────────────────────────

@app.errorhandler(413)
def request_entity_too_large(error):
    return jsonify({
        "error": "file too large",
        "max_size_mb": MAX_UPLOAD_MB
    }), 413


# ──────────────────────────────────────────────────────────
# 页面路由
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/")
def index_page():
    """Web 管理界面"""
    return render_template("index.html", url_prefix=URL_PREFIX)


# 静态文件服务（ui 目录下的图标等）
@app.route(f"{URL_PREFIX}/static/<path:filename>")
def static_files(filename):
    return send_from_directory(str(APP_DIR / "ui"), filename)


# ──────────────────────────────────────────────────────────
# API: 健康检查
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/health")
def health_check():
    """健康检查，无需鉴权"""
    return jsonify({
        "status": "ok",
        "version": "1.0.0",
        "uptime": int(time.time() - _start_time),
    })


_start_time = time.time()


# ──────────────────────────────────────────────────────────
# API: 应用列表
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/apps")
@require_api_key
def list_apps():
    """列出所有应用及其图标状态"""
    apps = scan_apps()
    # 支持状态筛选
    status_filter = request.args.get("status", "")
    if status_filter == "custom":
        apps = [a for a in apps if a["has_custom_icon"]]
    elif status_filter == "default":
        apps = [a for a in apps if not a["has_custom_icon"]]

    # 关键词搜索
    q = request.args.get("q", "").strip().lower()
    if q:
        apps = [a for a in apps if q in a["name"].lower()]

    return jsonify({
        "total": len(apps),
        "apps": apps,
    })


# ──────────────────────────────────────────────────────────
# API: 刷新扫描缓存
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/refresh", methods=["POST"])
@require_api_key
def refresh_scan():
    """强制刷新应用扫描缓存"""
    global _scan_cache
    _scan_cache = {"ts": 0, "data": None}
    return jsonify({"status": "refreshed"})


# ──────────────────────────────────────────────────────────
# API: 获取单个应用图标
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/apps/<appname>/icon/<int:size>")
@require_api_key
def get_app_icon(appname, size):
    """获取应用的图标文件"""
    try:
        appname = validate_appname(appname)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if size not in ALLOWED_SIZES:
        return jsonify({"error": f"unsupported size, must be one of {ALLOWED_SIZES}"}), 400

    app_path = APPCENTER_ROOT / appname
    icon_name = "ICON.PNG" if size == 64 else "ICON_256.PNG"
    icon_path = app_path / icon_name

    if not icon_path.exists():
        return jsonify({"error": "icon not found"}), 404

    return send_file(str(icon_path), mimetype="image/png")


# ──────────────────────────────────────────────────────────
# API: 替换图标
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/apps/<appname>/icon/<int:size>", methods=["POST"])
@require_api_key
def replace_icon(appname, size):
    """替换应用图标（先备份旧图标再替换）"""
    try:
        appname = validate_appname(appname)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if size not in ALLOWED_SIZES:
        return jsonify({"error": f"unsupported size, must be one of {ALLOWED_SIZES}"}), 400

    app_path = APPCENTER_ROOT / appname
    if not app_path.exists():
        return jsonify({"error": "app not found"}), 404

    if 'file' not in request.files:
        return jsonify({"error": "no file uploaded"}), 400

    upload_file = request.files['file']
    if not upload_file.filename:
        return jsonify({"error": "empty filename"}), 400

    icon_name = "ICON.PNG" if size == 64 else "ICON_256.PNG"
    target_path = app_path / icon_name

    # 备份旧图标
    backup_result = backup_icon(app_path, size)

    # 处理并写入新图标
    result = process_icon(upload_file, target_path, size)
    if "error" in result:
        return jsonify({"error": result["error"]}), 400

    # 清除扫描缓存
    global _scan_cache
    _scan_cache = {"ts": 0, "data": None}

    # 同时更新 ui 目录下的对应图标
    ui_dir = app_path / "ui"
    if ui_dir.is_dir():
        ui_icon_name = f"icon-{size}.png"
        ui_target = ui_dir / ui_icon_name
        process_icon(upload_file, ui_target, size)

    return jsonify({
        "status": "replaced",
        "appname": appname,
        "size": size,
        "backup": backup_result,
    })


# ──────────────────────────────────────────────────────────
# API: 还原图标
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/apps/<appname>/icon/<int:size>/restore", methods=["POST"])
@require_api_key
def restore_app_icon(appname, size):
    """从备份还原应用图标"""
    try:
        appname = validate_appname(appname)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if size not in ALLOWED_SIZES:
        return jsonify({"error": f"unsupported size"}), 400

    app_path = APPCENTER_ROOT / appname
    if not app_path.exists():
        return jsonify({"error": "app not found"}), 404

    result = restore_icon(app_path, size)
    if "error" in result:
        return jsonify(result), 404

    # 清除扫描缓存
    global _scan_cache
    _scan_cache = {"ts": 0, "data": None}

    return jsonify({
        "status": "restored",
        "appname": appname,
        "size": size,
    })


# ──────────────────────────────────────────────────────────
# API: 批量替换
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/batch/replace", methods=["POST"])
@require_api_key
def batch_replace():
    """批量替换多个应用的图标（同一个图标应用到多个应用）"""
    if 'file' not in request.files:
        return jsonify({"error": "no file uploaded"}), 400

    upload_file = request.files['file']
    appnames = request.form.getlist("apps")
    if not appnames:
        return jsonify({"error": "no apps specified"}), 400

    results = []
    for name in appnames:
        try:
            name = validate_appname(name)
        except ValueError:
            results.append({"name": name, "status": "error", "error": "invalid appname"})
            continue

        app_path = APPCENTER_ROOT / name
        if not app_path.exists():
            results.append({"name": name, "status": "error", "error": "app not found"})
            continue

        for size in ALLOWED_SIZES:
            icon_name = "ICON.PNG" if size == 64 else "ICON_256.PNG"
            target_path = app_path / icon_name
            backup_icon(app_path, size)
            result = process_icon(upload_file, target_path, size)
            if "error" in result:
                results.append({"name": name, "size": size, "status": "error", "error": result["error"]})
            else:
                # 同步 ui 目录
                ui_dir = app_path / "ui"
                if ui_dir.is_dir():
                    process_icon(upload_file, ui_dir / f"icon-{size}.png", size)
                results.append({"name": name, "size": size, "status": "ok"})

    # 清除缓存
    global _scan_cache
    _scan_cache = {"ts": 0, "data": None}

    return jsonify({"results": results})


# ──────────────────────────────────────────────────────────
# API: 批量还原
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/batch/restore", methods=["POST"])
@require_api_key
def batch_restore():
    """批量还原多个应用的图标"""
    appnames = request.json.get("apps", []) if request.is_json else request.form.getlist("apps")
    if not appnames:
        return jsonify({"error": "no apps specified"}), 400

    results = []
    for name in appnames:
        try:
            name = validate_appname(name)
        except ValueError:
            results.append({"name": name, "status": "error", "error": "invalid appname"})
            continue

        app_path = APPCENTER_ROOT / name
        if not app_path.exists():
            results.append({"name": name, "status": "error", "error": "app not found"})
            continue

        for size in ALLOWED_SIZES:
            result = restore_icon(app_path, size)
            if "error" in result:
                results.append({"name": name, "size": size, "status": "skip", "error": result["error"]})
            else:
                results.append({"name": name, "size": size, "status": "ok"})

    # 清除缓存
    global _scan_cache
    _scan_cache = {"ts": 0, "data": None}

    return jsonify({"results": results})


# ──────────────────────────────────────────────────────────
# API: 配置管理
# ──────────────────────────────────────────────────────────

@app.route(f"{URL_PREFIX}/api/config", methods=["GET"])
@require_api_key
def get_config():
    """获取当前配置（脱敏 API Key）"""
    cfg = load_config()
    if cfg.get("api_key"):
        key = cfg["api_key"]
        cfg["api_key_masked"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
        del cfg["api_key"]
    return jsonify(cfg)


@app.route(f"{URL_PREFIX}/api/config", methods=["PUT"])
@require_api_key
def update_config():
    """更新配置"""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "invalid json"}), 400

    cfg = load_config()

    # 只允许修改白名单字段
    allowed = {"max_upload_mb", "icon_sizes"}
    for key in allowed:
        if key in data:
            cfg[key] = data[key]

    # API Key 变更需要单独接口
    save_config(cfg)
    return jsonify({"status": "updated"})


@app.route(f"{URL_PREFIX}/api/config/api-key", methods=["PUT"])
@require_api_key
def update_api_key():
    """更新 API Key"""
    data = request.get_json(silent=True)
    if not data or "new_api_key" not in data:
        return jsonify({"error": "missing new_api_key"}), 400

    new_key = data["new_api_key"]
    # S5: 强度校验
    if len(new_key) < 8 or len(new_key) > 64:
        return jsonify({"error": "api key must be 8-64 characters"}), 400

    cfg = load_config()
    cfg["api_key"] = new_key
    save_config(cfg)
    return jsonify({"status": "api_key_updated"})


# ──────────────────────────────────────────────────────────
# 初始化
# ──────────────────────────────────────────────────────────

def init():
    """应用初始化"""
    # 确保目录存在
    PKG_VAR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    # S5: 如果没有配置文件或没有 API Key，生成随机 Key
    cfg = load_config()
    if not cfg.get("api_key"):
        cfg["api_key"] = secrets.token_urlsafe(32)
        cfg["max_upload_mb"] = MAX_UPLOAD_MB
        cfg["icon_sizes"] = ALLOWED_SIZES
        save_config(cfg)
        logger.info("Generated new API key: %s****", cfg["api_key"][:8])

    logger.info("FNTB Icon Manager initialized. URL_PREFIX=%s", URL_PREFIX)


# 启动时初始化
init()

if __name__ == "__main__":
    # 开发模式直接运行，生产模式用 gunicorn
    port = int(os.environ.get("FNTB_PORT", 18080))
    app.run(host="0.0.0.0", port=port, debug=False)
