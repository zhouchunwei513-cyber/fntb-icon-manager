#!/usr/bin/env python3
"""
FNTB 图标管理器 v2.11.0 - fnOS 应用图标统一管理
- 扫描 /var/apps/ 下所有应用
- 读取每个应用的 manifest 和 ICON.PNG / ICON_256.PNG
- 支持自定义图标替换和还原
- 提供统一图标 API: /api/icons/{appname}/{size}
- 客户端兼容 API: /api/client/apps
- v2.10.0: 增强诊断日志（图标解析过程/应用扫描详情/错误上下文）+ appname 去引号归一化
- v2.11.0: 卸载时自动清除旧数据（数据目录/配置目录残留清理）
"""
import os
import sys
import json
import shutil
import hashlib
import tempfile
import logging
import time
from datetime import datetime
from pathlib import Path
from collections import deque
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from PIL import Image
from gunicorn.app.base import BaseApplication

# ── 配置 ──────────────────────────────────────────────
APP_NAME = "com.fntb.iconmgr"
APP_DIR = os.environ.get("TRIM_APPDEST", os.path.dirname(os.path.abspath(__file__)))

# 自身版本号：优先从 manifest 读取，与 fnpack 打包的 manifest 保持一致
def _load_self_version():
    candidates = [
        os.path.join(os.path.dirname(APP_DIR), "manifest"),  # 部署后 @appcenter/xxx/manifest
        os.path.join(APP_DIR, "manifest"),                   # 开发目录兜底
    ]
    for _mp in candidates:
        try:
            if os.path.isfile(_mp):
                with open(_mp, "r", encoding="utf-8") as _f:
                    for _line in _f:
                        _line = _line.strip()
                        if _line.startswith("version="):
                            _v = _line.split("=", 1)[1].strip()
                            if _v:
                                return _v
        except Exception:
            continue
    return "2.12.0"

VERSION = _load_self_version()
# var 目录: TRIM_PKGVAR 优先，否则基于 APP_DIR 创建
_pkgvar = os.environ.get("TRIM_PKGVAR", "").strip()
if _pkgvar and os.path.isdir(_pkgvar):
    VAR_DIR = _pkgvar
else:
    VAR_DIR = os.path.join(APP_DIR, "var")
# 所有 fnOS 应用安装根目录
FNOS_APPS_ROOTS = [
    "/var/apps",                        # 标准安装路径
    "/usr/local/apps/@appcenter",       # 系统应用 (install_type=root)
]
# 额外扫描 /vol*/@appcenter (第三方应用)
import glob
for vol_path in sorted(glob.glob("/vol*/@appcenter")):
    if vol_path not in FNOS_APPS_ROOTS:
        FNOS_APPS_ROOTS.append(vol_path)

# 自定义图标存储目录
CUSTOM_ICONS_DIR = os.path.join(VAR_DIR, "custom_icons")
# 应用元数据缓存文件
APPS_CACHE_FILE = os.path.join(VAR_DIR, "apps_cache.json")
# 应用配置（NAS 地址等），存服务端以便卸载时一并清除
CONFIG_FILE = os.path.join(VAR_DIR, "config.json")

os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
os.makedirs(VAR_DIR, exist_ok=True)

# ── 日志 ──────────────────────────────────────────────
logger = logging.getLogger("fntb-iconmgr")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
_fh = logging.FileHandler(os.path.join(VAR_DIR, "server.log"))
_fh.setFormatter(_formatter)
logger.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_formatter)
logger.addHandler(_sh)
logger.info(f"VAR_DIR={VAR_DIR}, APP_DIR={APP_DIR}, CUSTOM_ICONS_DIR={CUSTOM_ICONS_DIR}")

# ── 请求追踪（客户端连接状态） ─────────────────────────
_start_time = time.time()
_recent_requests = deque(maxlen=20)  # 最近 20 条请求记录


# ── Flask 应用 ────────────────────────────────────────
app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"))
CORS(app)


@app.before_request
def _track_request():
    """记录每次 API 请求的来源和时间"""
    # 只追踪 API 请求，忽略静态资源和页面
    path = request.path
    if path.startswith("/api/") or path.startswith("/app/com.fntb.iconmgr/api/"):
        # 跳过健康检查自身的请求
        if "health" not in path and "client/status" not in path:
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr)
            ua = request.headers.get("User-Agent", "") or ""
            client_mark = (request.headers.get("X-FNOS-Client", "") or "").strip()
            # 飞牛 PC 客户端标识：显式 X-FNOS-Client 头，或 UA 含 Electron/FNOS-Desktop
            is_fnos_client = bool(client_mark) or ("electron" in ua.lower() or "fnos-desktop" in ua.lower())
            _recent_requests.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "ts": time.time(),
                "ip": client_ip,
                "method": request.method,
                "path": path,
                "user_agent": ua[:80],
                "is_client": is_fnos_client,
            })

# ── 工具函数 ──────────────────────────────────────────

def read_ui_config(app_dir):
    """读取 app/ui/config 获取桌面入口配置"""
    config_path = os.path.join(app_dir, "ui", "config")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def get_app_title_from_ui_config(ui_config, applaunchname):
    """从 ui/config 中获取指定入口的 title"""
    url_entries = ui_config.get(".url", {})
    entry = url_entries.get(applaunchname, {})
    title = entry.get("title", "")
    return title


def _find_po_translation(template_key, app_dir, appname):
    """在多个位置搜索 PO 文件获取翻译"""
    import re

    # 搜索 PO 文件的目录列表（包含 fnOS 集中式 locale 目录）
    search_dirs = [
        # 应用内目录
        os.path.join(app_dir, "resource", "locale"),
        os.path.join(app_dir, "locale"),
        os.path.join(app_dir, "lang"),
        os.path.join(app_dir, "resource", "lang"),
        # fnOS 系统集中式 locale（翻译存在这里）
        "/usr/trim/locale",
        "/usr/trim/resource/locale",
        "/var/apps/trim-base/resource/locale",
        "/var/apps/trim-base/locale",
        "/usr/local/share/locale",
    ]
    # 也搜索 /vol*/@appcenter/trim-base 路径
    for vol in ["/vol1", "/vol2", "/vol3", "/vol4"]:
        search_dirs.append(f"{vol}/@appcenter/trim-base/resource/locale")
        search_dirs.append(f"{vol}/@appcenter/trim-base/locale")
    search_dirs.append("/usr/share/trim/locale")

    po_files = ["zh_CN.po", "zh.po", "common.po", "messages.po", "zh_Hans.po", "zh-Hans.po"]

    for search_dir in search_dirs:
        if not os.path.isdir(search_dir):
            continue
        for po_file in po_files:
            po_path = os.path.join(search_dir, po_file)
            if os.path.isfile(po_path):
                try:
                    with open(po_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    pattern = rf'msgid\s+"{re.escape(template_key)}"\s*\n\s*msgstr\s+"([^"]*)"'
                    m = re.search(pattern, content)
                    if m and m.group(1).strip():
                        return m.group(1).strip()
                except Exception:
                    pass
    return None


def _try_trim_service_appname(appname):
    """尝试通过 trim 内部命令获取应用显示名"""
    try:
        import subprocess
        # 尝试 trim app_info 命令
        result = subprocess.run(
            ["/usr/trim/bin/trim", "app", "info", appname],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout:
            data = json.loads(result.stdout)
            name = data.get("display_name", "") or data.get("title", "")
            if name and "${" not in name:
                return name
    except Exception:
        pass
    return None


def resolve_display_name(manifest, app_dir, appname):
    """
    解析应用显示名称
    优先级:
      1. ui/config 中的 title（如果是直接文本而非模板变量）
      2. manifest display_name（如果是直接文本）
      3. PO 文件翻译（搜索应用内 + fnOS 系统集中式 locale 目录）
      4. trim 内部服务获取
      5. KNOWN_NAMES 硬编码映射（兜底）
      6. 目录名可读化（最终回退）
    """
    applaunchname = manifest.get("desktop_applaunchname", "")
    raw = manifest.get("display_name", "")
    # 目录名作为备用 appname（有时 manifest appname 和目录名不同）
    dir_name = os.path.basename(app_dir)

    # 已知应用中文名映射（兜底方案，覆盖 fnOS 官方应用 + 常见第三方）
    KNOWN_NAMES = {
        # fnOS 官方应用（按 trim-* 目录名）
        "trim.media": "影视",
        "trim.music": "音乐",
        "trim.preview": "预览",
        "trim.snapshots": "快照",
        "trim.text-editor": "文本编辑器",
        "trim.docs": "Office文档",
        "trim.browser": "浏览器",
        "trim.photos": "相册",
        "trim.backup": "备份",
        "trim.download": "下载",
        "trim.docker": "Docker",
        "trim.vm": "虚拟机",
        "trim.monitor": "资源监控",
        "trim.baidunetdisk": "百度网盘",
        # 按 manifest appname 的变体
        "trim-media": "影视",
        "trim-music": "音乐",
        "trim-preview": "预览",
        "trim-snapshots": "快照",
        "trim-text-editor": "文本编辑器",
        "trim-photos": "相册",
        # 第三方应用
        "leelaa.pdfload": "PDF阅读器",
        "com.fntb.iconmgr": "FNTB图标管理器",
        # Docker / 系统
        "python312": "Python 3.12",
        "python3.12": "Python 3.12",
        "nodejs_v22": "Node.js v22",
        "nodejs_v24": "Node.js v24",
        "qBittorrent": "qBittorrent",
    }

    # 1. 优先从 ui/config 获取 title
    if applaunchname:
        ui_config = read_ui_config(app_dir)
        title = get_app_title_from_ui_config(ui_config, applaunchname)
        if title and "${" not in title:
            return title

    # 2. manifest display_name 如果是直接文本
    if raw and "${" not in raw:
        return raw

    # 3. 模板变量，搜索 PO 文件（包括 fnOS 系统 locale 目录）
    if raw and "${" in raw:
        template_key = raw.replace("${", "").replace("}", "").strip()
        po_result = _find_po_translation(template_key, app_dir, appname)
        if po_result:
            return po_result

    # 4. 尝试 trim 内部服务
    trim_result = _try_trim_service_appname(appname)
    if trim_result:
        return trim_result

    # 5. KNOWN_NAMES 映射（同时检查 appname 和目录名）
    if appname in KNOWN_NAMES:
        return KNOWN_NAMES[appname]
    if dir_name in KNOWN_NAMES:
        return KNOWN_NAMES[dir_name]
    # 也检查去掉 trim- 前缀的变体
    if dir_name.startswith("trim-") or dir_name.startswith("trim."):
        for key, name in KNOWN_NAMES.items():
            key_base = key.replace("trim-", "").replace("trim.", "")
            dir_base = dir_name.replace("trim-", "").replace("trim.", "")
            if key_base == dir_base:
                return name

    # 6. 最终回退：目录名可读化
    readable = dir_name if dir_name else appname
    readable = readable.split(".")[-1] if "." in readable and len(readable.split(".")) > 2 else readable
    readable = readable.replace("-", " ").replace("_", " ").title()
    return readable


def read_manifest(app_dir):
    """读取应用 manifest 文件，返回 dict"""
    manifest_path = os.path.join(app_dir, "manifest")
    result = {}
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, value = line.split("=", 1)
                    # v2.9.0: fnOS 部分 manifest 的值为带引号字符串（如 appname="trim.music"），
                    # 解析时去除引号，否则应用名会带引号导致客户端图标查询 404
                    result[key.strip()] = value.strip().strip('"').strip("'")
    except FileNotFoundError:
        # v2.10.0: manifest 缺失是 fnOS 常见情况（trim.* 系统应用/部分第三方），记录目录内容帮助诊断
        try:
            entries = sorted(os.listdir(app_dir))[:20]
        except Exception:
            entries = []
        logger.info(f"manifest 缺失 {app_dir} (目录内容: {entries})")
    except Exception as e:
        logger.warning(f"读取 manifest 失败 {app_dir}: {e}")
    return result


def get_app_icon_path(app_dir, size=256):
    """获取应用默认图标路径（ICON.PNG/ICON_256.PNG 优先，其次 ui/images/ 桌面图标）"""
    if size == 64:
        candidates = [
            os.path.join(app_dir, "ICON.PNG"),
            os.path.join(app_dir, "ui", "images", "icon-64.png"),
            os.path.join(app_dir, "ui", "images", "icon.png"),
        ]
    else:
        candidates = [
            os.path.join(app_dir, "ICON_256.PNG"),
            os.path.join(app_dir, "ICON.PNG"),
            os.path.join(app_dir, "ui", "images", "icon-256.png"),
            os.path.join(app_dir, "ui", "images", "icon.png"),
        ]
    # v2.9.0: 优先返回第一个存在且非空的图标文件
    for p in candidates:
        if _is_valid_icon_file(p):
            return p
    return candidates[0]


def get_custom_icon_path(appname, size=256):
    """获取自定义图标路径"""
    return os.path.join(CUSTOM_ICONS_DIR, appname, f"icon_{size}.png")


def has_custom_icon(appname):
    """检查是否有自定义图标"""
    custom_dir = os.path.join(CUSTOM_ICONS_DIR, appname)
    return os.path.isdir(custom_dir) and len(os.listdir(custom_dir)) > 0


def scan_all_apps():
    """扫描所有已安装的 fnOS 应用"""
    apps = []
    seen_names = set()

    for root_dir in FNOS_APPS_ROOTS:
        if not os.path.isdir(root_dir):
            continue
        try:
            entries = os.listdir(root_dir)
        except PermissionError:
            logger.warning(f"无权限访问 {root_dir}")
            continue

        for name in entries:
            if name in seen_names:
                continue
            app_dir = os.path.join(root_dir, name)
            if not os.path.isdir(app_dir):
                continue

            manifest = read_manifest(app_dir)
            appname = manifest.get("appname", "")
            if not appname:
                # manifest 缺失或无 appname（fnOS 系统应用 trim.* 常见）：
                # 用目录名兜底，仍可提供 ICON.PNG 图标 + KNOWN_NAMES 显示名
                appname = name
            if not appname:
                continue

            if appname in seen_names:
                continue
            seen_names.add(appname)

            # 读取 ui/config 获取启动信息和显示名称
            ui_config = read_ui_config(app_dir)
            launch_info = {}
            applaunchname = manifest.get("desktop_applaunchname", "")
            if applaunchname and ui_config:
                entry = ui_config.get(".url", {}).get(applaunchname, {})
                if entry:
                    launch_info = {
                        "title": entry.get("title", ""),
                        "protocol": entry.get("protocol", "http"),
                        "port": entry.get("port", ""),
                        "url": entry.get("url", "/"),
                    }

            # 检查图标（0 字节视为无图标）
            icon_64 = get_app_icon_path(app_dir, 64)
            icon_256 = get_app_icon_path(app_dir, 256)
            has_icon_64 = _is_valid_icon_file(icon_64)
            has_icon_256 = _is_valid_icon_file(icon_256)

            # v2.12.0: 过滤已卸载残留——既无 manifest appname 声明、又无有效图标、也无 ui 配置的目录跳过
            _manifest_declared = bool(manifest.get("appname", ""))
            if not _manifest_declared and not has_icon_64 and not has_icon_256 and not ui_config:
                logger.info(f"scan_all_apps 跳过疑似已卸载残留（无 manifest/图标/ui）: {app_dir}")
                continue

            apps.append({
                "name": appname,
                "display_name": resolve_display_name(manifest, app_dir, appname),
                "version": manifest.get("version", ""),
                "desc": manifest.get("desc", ""),
                "source": manifest.get("source", ""),
                "platform": manifest.get("platform", ""),
                "install_type": manifest.get("install_type", ""),
                "has_default_icon_64": has_icon_64,
                "has_default_icon_256": has_icon_256,
                "has_custom_icon": has_custom_icon(appname),
                "app_dir": app_dir,
                "root_dir": root_dir,
                # PC 客户端兼容字段
                "title": launch_info.get("title", ""),
                "protocol": launch_info.get("protocol", "http"),
                "port": launch_info.get("port", ""),
                "path": launch_info.get("url", "/"),
                "applaunchname": applaunchname,
            })

    # 按显示名称排序
    apps.sort(key=lambda x: x.get("display_name", x["name"]))
    # v2.10.0: 扫描统计日志，便于确认应用列表是否完整
    no_icon = [a["name"] for a in apps if not (a["has_default_icon_64"] or a["has_default_icon_256"])]
    logger.info(
        f"scan_all_apps 完成: roots={FNOS_APPS_ROOTS}, 应用数={len(apps)}, "
        f"无图标应用={len(no_icon)} ({no_icon[:20]})"
    )
    logger.info(f"扫描到应用: {[a['name'] for a in apps]}")
    return apps


def refresh_cache():
    """刷新应用缓存"""
    global _apps_cache
    _apps_cache = scan_all_apps()
    # 写入缓存文件
    try:
        cache_data = []
        for a in _apps_cache:
            cache_data.append({
                "name": a["name"],
                "display_name": a["display_name"],
                "version": a["version"],
                "desc": a["desc"],
                "source": a["source"],
                "has_custom_icon": a["has_custom_icon"],
            })
        with open(APPS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"写入缓存失败: {e}")
    return _apps_cache


# 全局缓存
_apps_cache = None


def get_apps_cache():
    global _apps_cache
    if _apps_cache is None:
        # 尝试从缓存文件加载
        if os.path.isfile(APPS_CACHE_FILE):
            try:
                with open(APPS_CACHE_FILE, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                # 缓存只存基本信息，需要重新扫描获取完整信息
            except Exception:
                pass
        _apps_cache = scan_all_apps()
    return _apps_cache


# ── API 路由 ──────────────────────────────────────────

@app.route("/")
@app.route("/app/com.fntb.iconmgr")
@app.route("/app/com.fntb.iconmgr/")
def index():
    """Web 管理界面"""
    return render_template("index.html", url_prefix="")


@app.route("/static/<path:filename>")
@app.route("/app/com.fntb.iconmgr/static/<path:filename>")
def static_files(filename):
    """静态文件"""
    return send_file(os.path.join(APP_DIR, "templates", filename))


@app.route("/api/health")
@app.route("/app/com.fntb.iconmgr/api/health")
def health():
    return jsonify({"status": "ok", "version": VERSION})


def _detect_client_connection(window_seconds=300):
    """
    基于最近请求记录，判断是否有飞牛 PC 客户端在活跃连接。
    客户端请求会带 X-FNOS-Client 头（或 UA 含 electron/fnos-desktop），
    在 _track_request 中标记 is_client=True。
    """
    cutoff = time.time() - window_seconds
    last_client_at = None
    for r in reversed(list(_recent_requests)):
        if r.get("is_client") and r.get("ts", 0) >= cutoff:
            last_client_at = r.get("time")
            break
    return bool(last_client_at), last_client_at


@app.route("/api/client/status")
@app.route("/app/com.fntb.iconmgr/api/client/status")
def client_status():
    """
    客户端连接状态 API
    返回服务运行状态、端口、版本、CORS 配置、最近请求记录等
    """
    uptime_seconds = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s" if hours else f"{minutes}m {seconds}s"

    port = int(os.environ.get("TRIM_SERVICE_PORT", 18080))

    # 检测 CORS 配置状态
    cors_enabled = True  # CORS(app) 已在初始化时启用

    # 最近请求记录
    recent = list(_recent_requests)[-10:]  # 返回最近 10 条

    # v2.12.0: 客户端连接真实检测（区分浏览器访问与飞牛 PC 客户端连接）
    client_connected, last_client_at = _detect_client_connection()
    total_client_requests = sum(1 for r in _recent_requests if r.get("is_client"))

    return jsonify({
        "status": "running",
        "version": VERSION,
        "port": port,
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "cors_enabled": cors_enabled,
        "cors_allow_origin": "*",
        "total_requests_tracked": len(list(_recent_requests)),
        "recent_requests": recent,
        "last_request": recent[-1] if recent else None,
        "api_accessible": True,
        "app_name": APP_NAME,
        "var_dir": VAR_DIR,
        "client_connected": client_connected,
        "last_client_seen": last_client_at,
        "total_client_requests": total_client_requests,
    })


@app.route("/api/logs")
@app.route("/app/com.fntb.iconmgr/api/logs")
def get_logs():
    """查看应用日志（供前端调试使用）"""
    log_file = request.args.get("file", "fntb.log")
    # 安全防护：只允许读取日志文件
    allowed_files = {"fntb.log", "server.log", "error.log"}
    if log_file not in allowed_files:
        return jsonify({"error": "不允许访问该文件"}), 403
    log_path = os.path.join(VAR_DIR, log_file)
    if not os.path.isfile(log_path):
        return jsonify({"error": "日志文件不存在", "var_dir": VAR_DIR}), 404
    try:
        lines = request.args.get("lines", "200")
        max_lines = min(int(lines), 1000)
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        recent = all_lines[-max_lines:]
        return jsonify({
            "file": log_file,
            "var_dir": VAR_DIR,
            "total_lines": len(all_lines),
            "showing": len(recent),
            "lines": [l.rstrip("\n") for l in recent],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apps")
@app.route("/app/com.fntb.iconmgr/api/apps")
def list_apps():
    """获取所有应用列表"""
    apps = get_apps_cache()
    status_filter = request.args.get("status", "")
    search_q = request.args.get("q", "").lower()

    result = []
    for a in apps:
        # 状态筛选
        if status_filter == "custom" and not a["has_custom_icon"]:
            continue
        if status_filter == "default" and a["has_custom_icon"]:
            continue

        # 搜索
        if search_q:
            if search_q not in a["name"].lower() and search_q not in a["display_name"].lower():
                continue

        result.append({
            "name": a["name"],
            "display_name": a["display_name"],
            "title": a.get("title", "") or a["display_name"],
            "version": a["version"],
            "desc": a["desc"][:100] if a["desc"] else "",
            "source": a["source"],
            "has_default_icon": a["has_default_icon_256"] or a["has_default_icon_64"],
            "has_custom_icon": a["has_custom_icon"],
            "protocol": a.get("protocol", "http"),
            "tcp": a.get("protocol", "http"),
            "port": a.get("port", ""),
            "path": a.get("path", "/"),
            "applaunchname": a.get("applaunchname", ""),
            "icons": {
                "64": a["has_default_icon_64"],
                "256": a["has_default_icon_256"],
            }
        })

    return jsonify({"total": len(result), "apps": result})


@app.route("/api/refresh")
@app.route("/app/com.fntb.iconmgr/api/refresh")
def refresh():
    """刷新应用列表缓存"""
    logger.info("refresh 手动刷新触发（来源 UA=%s）", request.headers.get("User-Agent", "")[:60])
    apps = refresh_cache()
    logger.info(f"refresh 完成: 共扫描到 {len(apps)} 个应用")
    return jsonify({"total": len(apps)})


@app.route("/api/apps/<appname>")
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>")
def get_app(appname):
    """获取单个应用详情"""
    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            return jsonify({
                "name": a["name"],
                "display_name": a["display_name"],
                "version": a["version"],
                "desc": a["desc"],
                "source": a["source"],
                "platform": a["platform"],
                "install_type": a["install_type"],
                "has_default_icon": a["has_default_icon_256"] or a["has_default_icon_64"],
                "has_custom_icon": a["has_custom_icon"],
                "app_dir": a["app_dir"],
            })
    return jsonify({"error": "应用未找到"}), 404


@app.route("/api/apps/<appname>/icon/<int:size>")
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>/icon/<int:size>")
def get_icon(appname, size):
    """获取应用图标（优先自定义，其次默认）"""
    # v2.10.0: 归一化 appname + 日志（Web UI 会传带引号的 appname，如 %22trim.docs%22）
    raw_appname = appname
    appname = appname.strip().strip('"').strip("'")
    if size not in (64, 256):
        size = 256
    logger.info(f"get_icon 请求: raw_appname={raw_appname!r} -> appname={appname!r} size={size}")

    # 优先自定义图标
    custom_path = get_custom_icon_path(appname, size)
    if _is_valid_icon_file(custom_path):
        logger.info(f"get_icon 命中自定义图标: {appname} size={size}")
        return send_file(custom_path, mimetype="image/png")

    # 默认图标
    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], size)
            if _is_valid_icon_file(icon_path):
                logger.info(f"get_icon 命中默认图标: {appname} size={size} path={icon_path}")
                return send_file(icon_path, mimetype="image/png")
            else:
                logger.warning(f"get_icon 应用存在但图标无效: {appname} app_dir={a['app_dir']} icon_path={icon_path}")

    logger.warning(f"get_icon 未找到: raw_appname={raw_appname!r} appname={appname!r} size={size} "
                   f"(已扫描应用数={len(apps)}, 可用appname={[a['name'] for a in apps][:30]})")
    return jsonify({"error": "图标未找到"}), 404


@app.route("/api/apps/<appname>/icon/default/<int:size>")
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>/icon/default/<int:size>")
def get_default_icon(appname, size):
    """获取默认图标（忽略自定义）"""
    raw_appname = appname
    appname = appname.strip().strip('"').strip("'")
    if size not in (64, 256):
        size = 256

    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], size)
            if _is_valid_icon_file(icon_path):
                return send_file(icon_path, mimetype="image/png")

    logger.warning(f"get_default_icon 未找到: raw_appname={raw_appname!r} appname={appname!r} size={size}")
    return jsonify({"error": "默认图标未找到"}), 404


@app.route("/api/apps/<appname>/icon", methods=["POST"])
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>/icon", methods=["POST"])
def upload_icon(appname):
    """上传自定义图标"""
    if "file" not in request.files:
        return jsonify({"error": "未找到文件"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "文件名为空"}), 400

    if not file.filename.lower().endswith(".png"):
        return jsonify({"error": "仅支持 PNG 格式"}), 400

    if file.content_length and file.content_length > 2 * 1024 * 1024:
        return jsonify({"error": "文件超过 2MB"}), 400

    try:
        # 读取并验证 PNG
        img = Image.open(file)
        if img.format != "PNG":
            return jsonify({"error": "文件不是有效的 PNG 图片"}), 400

        # 保存 256 和 64 两个尺寸
        custom_dir = os.path.join(CUSTOM_ICONS_DIR, appname)
        os.makedirs(custom_dir, exist_ok=True)

        # 保存原始尺寸（作为 256）
        img_256 = img.copy()
        if img_256.size != (256, 256):
            img_256 = img_256.resize((256, 256), Image.LANCZOS)
        img_256.save(os.path.join(custom_dir, "icon_256.png"), "PNG")

        # 生成 64 尺寸
        img_64 = img.resize((64, 64), Image.LANCZOS)
        img_64.save(os.path.join(custom_dir, "icon_64.png"), "PNG")

        # 更新缓存
        get_apps_cache()
        for a in _apps_cache:
            if a["name"] == appname:
                a["has_custom_icon"] = True
                break

        logger.info(f"自定义图标已保存: {appname}")
        return jsonify({"message": "图标上传成功", "appname": appname})

    except Exception as e:
        logger.error(f"图标上传失败 {appname}: {e}")
        return jsonify({"error": f"处理失败: {str(e)}"}), 500


@app.route("/api/apps/<appname>/icon/restore", methods=["POST"])
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>/icon/restore", methods=["POST"])
def restore_icon(appname):
    """还原为默认图标"""
    custom_dir = os.path.join(CUSTOM_ICONS_DIR, appname)
    if os.path.isdir(custom_dir):
        shutil.rmtree(custom_dir)

    # 更新缓存
    get_apps_cache()
    for a in _apps_cache:
        if a["name"] == appname:
            a["has_custom_icon"] = False
            break

    logger.info(f"图标已还原: {appname}")
    return jsonify({"message": "图标已还原为默认"})


@app.route("/api/apps/batch/replace", methods=["POST"])
@app.route("/app/com.fntb.iconmgr/api/apps/batch/replace", methods=["POST"])
def batch_replace():
    """批量替换图标"""
    if "file" not in request.files:
        return jsonify({"error": "未找到文件"}), 400

    apps_list = request.form.getlist("apps")
    if not apps_list:
        return jsonify({"error": "未选择应用"}), 400

    file = request.files["file"]
    try:
        img = Image.open(file)
        if img.format != "PNG":
            return jsonify({"error": "仅支持 PNG 格式"}), 400

        success = []
        failed = []
        for appname in apps_list:
            try:
                custom_dir = os.path.join(CUSTOM_ICONS_DIR, appname)
                os.makedirs(custom_dir, exist_ok=True)

                img_256 = img.copy()
                if img_256.size != (256, 256):
                    img_256 = img_256.resize((256, 256), Image.LANCZOS)
                img_256.save(os.path.join(custom_dir, "icon_256.png"), "PNG")

                img_64 = img.resize((64, 64), Image.LANCZOS)
                img_64.save(os.path.join(custom_dir, "icon_64.png"), "PNG")

                success.append(appname)
            except Exception as e:
                failed.append({"appname": appname, "error": str(e)})

        return jsonify({
            "message": f"批量替换完成",
            "success": len(success),
            "failed": len(failed),
            "failed_list": failed,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apps/batch/restore", methods=["POST"])
@app.route("/app/com.fntb.iconmgr/api/apps/batch/restore", methods=["POST"])
def batch_restore():
    """批量还原图标"""
    data = request.get_json()
    apps_list = data.get("apps", []) if data else []
    if not apps_list:
        return jsonify({"error": "未选择应用"}), 400

    success = []
    for appname in apps_list:
        custom_dir = os.path.join(CUSTOM_ICONS_DIR, appname)
        if os.path.isdir(custom_dir):
            shutil.rmtree(custom_dir)
        success.append(appname)

    return jsonify({"message": "批量还原完成", "count": len(success)})


# ── 客户端兼容 API ────────────────────────────────────

def _add_cache_headers(response, max_age=3600):
    """为响应添加缓存头；max_age<=0 时强制 no-cache 让客户端立即刷新"""
    if max_age and max_age > 0:
        response.headers["Cache-Control"] = f"public, max-age={max_age}"
    else:
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _is_valid_icon_file(path):
    """图标文件必须存在且非空（0 字节文件视为无图标，避免 send_file 返回 200 空 body）"""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except Exception:
        return False


def _serve_icon_with_cache(icon_path, size, target_size=256):
    """带缓存头发送图标文件，支持自动缩放"""
    if not _is_valid_icon_file(icon_path):
        return None
    if size == target_size or target_size in (256, 64):
        resp = send_file(icon_path, mimetype="image/png", max_age=3600)
        return _add_cache_headers(resp, 3600)
    try:
        img = Image.open(icon_path)
        img = img.resize((size, size), Image.LANCZOS)
        buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(buf.name, "PNG")
        buf.close()
        resp = send_file(buf.name, mimetype="image/png", max_age=3600)
        return _add_cache_headers(resp, 3600)
    except Exception:
        resp = send_file(icon_path, mimetype="image/png", max_age=3600)
        return _add_cache_headers(resp, 3600)


@app.route("/api/icons/<appname>/<int:size>")
@app.route("/app/com.fntb.iconmgr/api/icons/<appname>/<int:size>")
def client_icon(appname, size):
    """
    统一图标 API - 供飞牛客户端调用
    返回应用图标（优先自定义，其次默认）
    支持 size: 64, 128, 256, 512
    """
    # v2.10.0: 归一化 appname（去掉引号/空白，处理 Web UI 传 %22 编码的情况）
    raw_appname = appname
    appname = appname.strip().strip('"').strip("'")
    if size not in (64, 128, 256, 512):
        size = 256

    # v2.10.0: 解析过程日志，便于定位 404/空图标问题
    logger.info(f"client_icon 请求: raw_appname={raw_appname!r} -> appname={appname!r} size={size}")

    # 优先自定义图标
    custom_path = get_custom_icon_path(appname, 256)
    if _is_valid_icon_file(custom_path):
        resp = _serve_icon_with_cache(custom_path, size, 256)
        if resp:
            logger.info(f"client_icon 命中自定义图标: {appname} size={size} path={custom_path}")
            return resp

    # 默认图标
    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], 256 if size >= 256 else 64)
            if _is_valid_icon_file(icon_path):
                resp = _serve_icon_with_cache(icon_path, size, 256 if size >= 256 else 64)
                if resp:
                    logger.info(f"client_icon 命中默认图标: {appname} size={size} path={icon_path}")
                    return resp
            else:
                logger.warning(f"client_icon 应用存在但图标无效: {appname} app_dir={a['app_dir']} icon_path={icon_path}")

    logger.warning(f"client_icon 未找到: raw_appname={raw_appname!r} appname={appname!r} size={size} "
                   f"(已扫描应用数={len(apps)}, 可用appname={[a['name'] for a in apps][:30]})")
    return jsonify({"error": "图标未找到"}), 404


@app.route("/api/icons")
@app.route("/app/com.fntb.iconmgr/api/icons")
def client_icons_list():
    """获取所有应用图标列表"""
    apps = get_apps_cache()
    base_url = request.url_root.rstrip("/")
    icons = []
    for a in apps:
        icons.append({
            "name": a["name"],
            "display_name": a["display_name"],
            "title": a.get("title", "") or a["display_name"],
            "icon": f"{base_url}/api/icons/{a['name']}/64",
            "icon_256": f"{base_url}/api/icons/{a['name']}/256",
            "protocol": a.get("protocol", "http"),
            "tcp": a.get("protocol", "http"),
            "port": a.get("port", ""),
            "path": a.get("path", "/"),
            "url": f"{a.get('protocol', 'http')}://{a.get('port', '')}{a.get('path', '/')}",
            "has_custom_icon": a["has_custom_icon"],
        })
    return _add_cache_headers(jsonify({"total": len(icons), "icons": icons}), 300)


@app.route("/api/client/apps")
@app.route("/app/com.fntb.iconmgr/api/client/apps")
def client_apps():
    """
    飞牛 PC 客户端兼容应用列表 API
    返回格式与 fnOS appcenter 一致，供 PC 客户端设置页面/快捷方式面板调用
    字段: name, title, icon, protocol, port, path, url
    """
    apps = get_apps_cache()
    # 使用客户端请求的 host 构造 URL（支持代理/NAS 地址）
    nas_host = request.args.get("nas_host", "")
    base_url = request.url_root.rstrip("/")
    # v2.10.0: 客户端列表请求日志（确认客户端确实调用了此 API）
    logger.info(f"client_apps 请求: nas_host={nas_host!r} base_url={base_url} "
                f"UA={request.headers.get('User-Agent', '')[:60]} 已扫描应用数={len(apps)}")

    result = []
    for a in apps:
        protocol = a.get("protocol", "http")
        port = a.get("port", "")
        path = a.get("path", "/")

        # 构造完整 URL
        if nas_host:
            url = f"{protocol}://{nas_host}:{port}{path}"
            icon_base = f"http://{nas_host}:18080"
        else:
            url = f"{protocol}://{nas_host or request.host}:{port}{path}"
            icon_base = base_url

        result.append({
            "name": a["name"],
            "title": a["display_name"],
            "icon": f"{icon_base}/api/icons/{a['name']}/64",
            "icon_256": f"{icon_base}/api/icons/{a['name']}/256",
            "protocol": protocol,
            "tcp": protocol,  # 兼容旧版
            "port": port,
            "path": path,
            "url": url,
            "has_custom_icon": a["has_custom_icon"],
        })
    resp = jsonify({"total": len(result), "list": result})
    logger.info(f"client_apps 返回 {len(result)} 个应用 (name列表: {[a['name'] for a in apps][:40]})")
    return _add_cache_headers(resp, 300)  # 缓存5分钟


# ── NAS 地址配置（服务端存储，卸载时随数据目录一并清除） ──

def _load_config():
    """读取服务端配置文件（config.json，存于 VAR_DIR）"""
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning(f"读取 config.json 失败: {e}")
    return {}


def _save_config(data):
    """保存服务端配置到 config.json"""
    try:
        os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"保存 config.json 失败: {e}")
        return False


@app.route("/api/config", methods=["GET"])
@app.route("/app/com.fntb.iconmgr/api/config", methods=["GET"])
def get_config():
    """读取 NAS 地址等客户端配置（服务端存储，避免浏览器 localStorage 残留）"""
    cfg = _load_config()
    logger.info(f"GET /api/config -> nas_host={cfg.get('nas_host', '')!r}")
    return jsonify(cfg)


@app.route("/api/config", methods=["POST"])
@app.route("/app/com.fntb.iconmgr/api/config", methods=["POST"])
def post_config():
    """保存 NAS 地址等客户端配置到服务端 config.json（卸载时随数据目录清除）"""
    try:
        data = request.get_json(silent=True) or {}
        nas_host = str(data.get("nas_host", "") or "").strip()
        cfg = _load_config()
        if nas_host:
            cfg["nas_host"] = nas_host
        else:
            cfg.pop("nas_host", None)
        if _save_config(cfg):
            logger.info(f"POST /api/config 保存成功: nas_host={nas_host!r}")
            return jsonify({"success": True, "msg": "已保存", "nas_host": nas_host})
        return jsonify({"success": False, "msg": "保存失败"}), 500
    except Exception as e:
        logger.error(f"POST /api/config 异常: {e}")
        return jsonify({"success": False, "msg": f"异常: {e}"}), 500


# ── Gunicorn 启动 ─────────────────────────────────────

class GunicornApp(BaseApplication):
    def __init__(self, flask_app, options=None):
        self.options = options or {}
        self.application = flask_app
        super().__init__()

    def load_config(self):
        for key, value in self.options.items():
            self.cfg.set(key, value)

    def load(self):
        return self.application


def run_server():
    port = int(os.environ.get("TRIM_SERVICE_PORT", 18080))
    options = {
        "bind": f"0.0.0.0:{port}",
        "workers": 2,
        "timeout": 120,
        "accesslog": "-",
        "errorlog": "-",
        "loglevel": "info",
    }
    logger.info(f"启动 FNTB 图标管理器 v{VERSION} on port {port}")
    GunicornApp(app, options).run()


if __name__ == "__main__":
    run_server()
