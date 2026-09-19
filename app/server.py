#!/usr/bin/env python3
"""
FNTB 图标管理器 v2.0 - fnOS 应用图标统一管理
- 扫描 /var/apps/ 下所有应用
- 读取每个应用的 manifest 和 ICON.PNG / ICON_256.PNG
- 支持自定义图标替换和还原
- 提供统一图标 API: /api/icons/{appname}/{size}
"""
import os
import sys
import json
import shutil
import hashlib
import tempfile
import logging
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
from PIL import Image
from gunicorn.app.base import BaseApplication

# ── 配置 ──────────────────────────────────────────────
APP_NAME = "com.fntb.iconmgr"
APP_DIR = os.environ.get("TRIM_APPDEST", os.path.dirname(os.path.abspath(__file__)))
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

os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
os.makedirs(VAR_DIR, exist_ok=True)

os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
os.makedirs(VAR_DIR, exist_ok=True)
os.makedirs(APP_DIR, exist_ok=True)

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

# ── Flask 应用 ────────────────────────────────────────
app = Flask(__name__, template_folder=os.path.join(APP_DIR, "templates"))
CORS(app)

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


def resolve_display_name(manifest, app_dir, appname):
    """解析应用显示名称，优先级：ui/config title > manifest display_name > PO文件 > 已知映射 > appname"""
    applaunchname = manifest.get("desktop_applaunchname", "")
    raw = manifest.get("display_name", "")

    # 已知应用中文名映射（当 PO 文件不可用时的兜底）
    KNOWN_NAMES = {
        "trim.media": "媒体",
        "trim.music": "音乐",
        "trim.preview": "预览",
        "trim.snapshots": "快照",
        "trim.text-editor": "文本编辑器",
        "trim.docs": "Office文档",
        "trim.browser": "浏览器",
        "leelaa.pdfload": "PDF阅读器",
        "qBittorrent": "qBittorrent",
        "python312": "Python 3.12",
        "nodejs_v22": "Node.js v22",
        "nodejs_v24": "Node.js v24",
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

    # 3. 模板变量，尝试 PO 文件
    if raw and "${" in raw:
        template_key = raw.replace("${", "").replace("}", "").strip()
        search_dirs = [
            os.path.join(app_dir, "resource", "locale"),
            os.path.join(app_dir, "locale"),
            os.path.join(app_dir, "lang"),
        ]
        po_files = ["zh_CN.po", "zh.po", "common.po", "messages.po"]
        for search_dir in search_dirs:
            if not os.path.isdir(search_dir):
                continue
            for po_file in po_files:
                po_path = os.path.join(search_dir, po_file)
                if os.path.isfile(po_path):
                    try:
                        with open(po_path, "r", encoding="utf-8") as f:
                            content = f.read()
                        import re
                        pattern = rf'msgid\s+"{re.escape(template_key)}"\s*\n\s*msgstr\s+"([^"]*)"'
                        m = re.search(pattern, content)
                        if m and m.group(1).strip():
                            return m.group(1).strip()
                    except Exception:
                        pass

    # 4. 已知应用映射
    if appname in KNOWN_NAMES:
        return KNOWN_NAMES[appname]

    # 5. 最终回退
    readable = appname.split(".")[-1] if "." in appname else appname
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
                    result[key.strip()] = value.strip()
    except Exception as e:
        logger.warning(f"读取 manifest 失败 {app_dir}: {e}")
    return result


def get_app_icon_path(app_dir, size=256):
    """获取应用默认图标路径"""
    if size == 64:
        return os.path.join(app_dir, "ICON.PNG")
    else:
        path = os.path.join(app_dir, "ICON_256.PNG")
        if not os.path.exists(path):
            path = os.path.join(app_dir, "ICON.PNG")
        return path


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
            if not manifest.get("appname"):
                continue

            appname = manifest["appname"]
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

            # 检查图标
            icon_64 = get_app_icon_path(app_dir, 64)
            icon_256 = get_app_icon_path(app_dir, 256)
            has_icon_64 = os.path.isfile(icon_64)
            has_icon_256 = os.path.isfile(icon_256)

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
    return jsonify({"status": "ok", "version": "2.5.0"})


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
    apps = refresh_cache()
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
    if size not in (64, 256):
        size = 256

    # 优先自定义图标
    custom_path = get_custom_icon_path(appname, size)
    if os.path.isfile(custom_path):
        return send_file(custom_path, mimetype="image/png")

    # 默认图标
    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], size)
            if os.path.isfile(icon_path):
                return send_file(icon_path, mimetype="image/png")

    return jsonify({"error": "图标未找到"}), 404


@app.route("/api/apps/<appname>/icon/default/<int:size>")
@app.route("/app/com.fntb.iconmgr/api/apps/<appname>/icon/default/<int:size>")
def get_default_icon(appname, size):
    """获取默认图标（忽略自定义）"""
    if size not in (64, 256):
        size = 256

    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], size)
            if os.path.isfile(icon_path):
                return send_file(icon_path, mimetype="image/png")

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


# ── 客户端图标 API（供飞牛客户端调用）────────────────

@app.route("/api/icons/<appname>/<int:size>")
@app.route("/app/com.fntb.iconmgr/api/icons/<appname>/<int:size>")
def client_icon(appname, size):
    """
    统一图标 API - 供飞牛客户端调用
    返回应用图标（优先自定义，其次默认）
    支持 size: 64, 256, 512
    """
    if size not in (64, 128, 256, 512):
        size = 256

    # 优先自定义图标
    custom_path = get_custom_icon_path(appname, 256)
    if os.path.isfile(custom_path):
        if size == 256:
            return send_file(custom_path, mimetype="image/png")
        try:
            img = Image.open(custom_path)
            img = img.resize((size, size), Image.LANCZOS)
            buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(buf.name, "PNG")
            buf.close()
            return send_file(buf.name, mimetype="image/png")
        except Exception:
            return send_file(custom_path, mimetype="image/png")

    # 默认图标
    apps = get_apps_cache()
    for a in apps:
        if a["name"] == appname:
            icon_path = get_app_icon_path(a["app_dir"], 256 if size >= 256 else 64)
            if os.path.isfile(icon_path):
                if size == 256 or size == 64:
                    return send_file(icon_path, mimetype="image/png")
                try:
                    img = Image.open(icon_path)
                    img = img.resize((size, size), Image.LANCZOS)
                    buf = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    img.save(buf.name, "PNG")
                    buf.close()
                    return send_file(buf.name, mimetype="image/png")
                except Exception:
                    return send_file(icon_path, mimetype="image/png")

    return jsonify({"error": "图标未找到"}), 404


@app.route("/api/icons")
@app.route("/app/com.fntb.iconmgr/api/icons")
def client_icons_list():
    """
    获取所有应用图标列表 - 供飞牛客户端调用
    返回格式兼容 fnOS PC 客户端快捷方式创建列表
    字段说明:
      - name: 应用包名 (如 trim.media)
      - display_name: 解析后的显示名称 (中文)
      - title: ui/config 中的标题 (与 display_name 相同或互补)
      - icon: 图标 URL (64px，供任务栏/快捷方式使用)
      - icon_256: 图标 URL (256px)
      - protocol: 协议 (http/https)
      - port: 端口号
      - path: 路径
      - tcp: 协议别名 (兼容旧客户端)
      - has_custom_icon: 是否有自定义图标
    """
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
    return jsonify({"total": len(icons), "icons": icons})


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
    logger.info(f"启动 FNTB 图标管理器 v2.0 on port {port}")
    GunicornApp(app, options).run()


if __name__ == "__main__":
    run_server()
