# FNTB 图标管理器

飞牛 fnOS 应用图标批量管理与统一 API 接口服务。

## 功能

- **应用扫描**：自动扫描所有已安装飞牛应用，展示图标状态
- **图标替换**：支持替换 64×64 和 256×256 两种尺寸图标（PNG 格式）
- **图标还原**：自动备份原始图标，支持一键还原
- **批量操作**：支持批量替换和批量还原图标
- **Web 管理界面**：现代化的暗色主题管理面板，支持搜索和筛选
- **HTTP API**：完整的 RESTful API 接口，支持 API Key 鉴权
- **安全防护**：路径遍历防护、PNG 格式校验、限流保护、原子配置写入

## 目录结构

```
fntb-icon-manager/
├── manifest              # FPK 应用元信息
├── wizard/
│   └── install           # 安装向导配置
├── cmd/
│   ├── main              # 启停控制脚本（start/stop/restart/status）
│   ├── install_init      # 安装初始化
│   ├── install_callback  # 安装回调
│   ├── uninstall_init    # 卸载初始化
│   └── uninstall_callback# 卸载回调
├── config/
│   ├── privilege         # 权限配置
│   └── resource          # 资源配置
├── icons/                # 应用图标资源
├── app/
│   ├── server.py         # Flask 后端服务
│   ├── requirements.txt  # Python 依赖
│   ├── templates/
│   │   └── index.html    # Web 管理界面
│   └── ui/
│       ├── config        # fnOS 桌面图标配置
│       └── images/       # 桌面图标文件
└── README.md
```

## 安装

### 方式一：打包为 FPK

```bash
cd fntb-icon-manager
fpk pack fntb-icon-manager
```

然后通过飞牛应用中心上传安装。

### 方式二：手动部署

```bash
# 将项目目录复制到飞牛应用目录
scp -r fntb-icon-manager/ user@fnos:/var/packages/fntb-icon-manager/

# 安装依赖
cd /var/packages/fntb-icon-manager/app
pip install -r requirements.txt

# 启动服务
python server.py
```

## API 接口

所有 API 接口需要 `X-API-Key` 请求头进行鉴权。

### 健康检查

```
GET /api/health
```

无需鉴权，返回服务状态。

### 应用列表

```
GET /api/apps?status=custom&q=xxx
```

| 参数 | 类型 | 说明 |
|------|------|------|
| status | string | 筛选状态：`custom`（已自定义）/ `default`（原始图标） |
| q | string | 搜索关键词 |

### 获取应用图标

```
GET /api/apps/{appname}/icon/{size}
```

- `size`：64 或 256

### 替换应用图标

```
POST /api/apps/{appname}/icon/{size}
Content-Type: multipart/form-data

file: <PNG file>
```

自动备份旧图标后替换。

### 还原应用图标

```
POST /api/apps/{appname}/icon/{size}/restore
```

从备份目录还原原始图标。

### 刷新缓存

```
POST /api/refresh
```

强制刷新应用扫描缓存。

### 桌面清单同步（v2.21.3）

```bash
# 客户端推送（覆盖式）
curl -X POST http://<nas>:18080/api/desktop-sync \
  -H 'Content-Type: application/json' \
  -d '{"apps":[{"name":"trim.setting","title":"系统设置"},{"name":"trim.music","title":"音乐"}],
       "client":{"name":"fnos-desktop","version":"5.3.8"}}'
# => {"ok":true,"synced":true,"total":2,"at":1763000000,"mode":"desktop-sync"}

# 查看状态
curl http://<nas>:18080/api/desktop-sync
# => {"ok":true,"synced":true,"stale":false,"at":...,"age":12,"ttl":2592000,
#     "client":{"name":"fnos-desktop","version":"5.3.8"},"total":2,"apps":[...]}

# 清除同步（回到本地规则）
curl -X DELETE http://<nas>:18080/api/desktop-sync
```

### 应用列表版本指纹

```
GET /api/apps/version
```

返回 `{"ok": true, "version": "dfb44f39091ba1e5", "total": 32}`。

- 管理页面每 3 秒轮询本接口：应用新增 / 卸载后版本指纹变化，页面自动重载应用列表与图标，无需手动点「刷新」。
- 应用列表缓存有效期 20 秒，并带目录指纹检测（应用目录结构变化即触发重扫），因此刷新一次就能拿到正确结果。

### 批量替换

```
POST /api/batch/replace
Content-Type: multipart/form-data

file: <PNG file>
apps: app1&apps=app2&apps=app3
```

### 批量还原

```
POST /api/batch/restore
Content-Type: application/json

{ "apps": ["app1", "app2", "app3"] }
```

### 获取配置

```
GET /api/config
```

### 更新配置

```
PUT /api/config
Content-Type: application/json

{ "max_upload_mb": 5 }
```

### 修改 API Key

```
PUT /api/config/api-key
Content-Type: application/json

{ "new_api_key": "your-new-key" }
```

## 配置

### 安装时配置

安装向导会要求输入：
- **API 访问密钥**：8-64 位字符串，用于 API 鉴权
- **服务端口**：默认 18080

### 运行时配置

通过 Web 界面「设置」或 API 修改：
- 最大上传大小（默认 2MB）
- 支持的图标尺寸（默认 64, 256）
- API Key 修改

## 安全特性

- ✅ 路径遍历防护（appname 白名单校验）
- ✅ 原子配置写入（防止并发写入损坏）
- ✅ PNG 魔数校验（防止恶意文件上传）
- ✅ CORS 同源策略
- ✅ API Key 强度校验（≥8 位）
- ✅ IP 级限流（120 次/分钟）
- ✅ 413 请求体过大保护

## 技术栈

- **后端**：Python 3 + Flask + Gunicorn
- **图片处理**：Pillow（Lanczos 缩放 + 居中画布）
- **前端**：原生 HTML/CSS/JS（暗色主题 SPA）
- **部署**：飞牛 FPK 包格式

## License

MIT

## 更新记录

### v2.21.3（2026-11-14）
- 新增「**桌面清单同步**」（方案 A）：飞牛前端的桌面条目走加密会话（RSA/AES）与 OAuth，
  而 `/oauthapi` 只对 `OfficialAppUsers` 组的官方应用开放，第三方 FPK 拿不到 token
  （详见 issue 说明）。因此改由**客户端**把自己在飞牛桌面上实际看到的应用列表推过来，本服务以它为真值。
  - `POST /api/desktop-sync`：`{"apps":[{"name":"trim.setting","title":"系统设置"}], "client":{"name":"fnos-desktop","version":"5.3.8"}}`
  - `GET /api/desktop-sync`：查看同步状态（`synced / stale / at / age / client / total / apps`）
  - `DELETE /api/desktop-sync`：清除同步，回落到本地规则
- 同步生效时 `/api/apps` **严格返回同步清单里的应用并按桌面顺序排列**，`/api/apps/version` 同口径；
  清单存 `var/desktop_sync.json`，**30 天有效**，过期自动回落本地规则（与 v2.21.2 行为一致）。
- 页面顶部显示同步状态（来自哪个客户端、几项、多久前），便于确认是否已同步。
- 配套客户端：FNOS 桌面客户端 5.3.8 起，主窗加载完成后自动同步一次（同一份清单 6 小时内不重推，每 15 分钟复查）。

### v2.21.2（2026-11-14）
- **默认只显示飞牛桌面上的应用**：`/api/apps` 现在返回 29 项，与飞牛桌面完全一致（此前 33 项）。
  入口全部 `noDisplay: true` 的应用（Office文档 / 文本编辑器 / 预览）与「扩展管理」类的 fnMusic 音源扩展不再出现在列表里。
- 新增「**仅显示飞牛桌面上的应用**」开关（默认勾选）。取消勾选即可看到全部已安装应用，等价于 `/api/apps?all=1`。
- 新增接口 `GET/POST /api/desktop-filter`：查看与调整桌面过滤（`hidden` 强制隐藏 / `shown` 强制显示），
  持久化到 `var/desktop_filter.json`，立即生效。
- 修复 `ui/config` 查找：系统模块应用的实际配置在 `/usr/local/apps/@appcenter/<app>/ui/config`
  （`/var/apps/<app>/` 下只有 manifest 与软链），此前读不到导致 `noDisplay` 与入口标题失效。
- 显示名继续与桌面对齐：日志 / 文件快照 / 素材库 Seek / 影视在线扩展管理。

### v2.21.1（2026-11-14）

图标准确性与列表准确性修复，均针对「服务与飞牛桌面同机」的部署场景：

- **系统应用图标改为本机静态文件优先**：直接读 `/usr/trim/www/static/app/icons/<app>/icon.png`（应用以 root 运行，与飞牛桌面渲染的图标字节完全一致），HTTP 下载降级为兜底。修掉 9 个桌面内置模块应用（Docker、下载、文件管理、回收站、备份、应用中心、日志、设置、资源监控）图标返回 8893 字节登录页 HTML 的问题。
- **图标魔数校验**：新增 `_icon_head_ok()` / `_icon_magic_ok()`，PNG/JPEG/WebP/GIF/BMP/ICO/SVG 白名单；下载与缓存读取都校验，HTML 一律丢弃、不落盘。
- **黑名单修正**：`RUNTIME_APP_BLACKLIST` 移除 `xte`、`npc`、`trim.sync_server` —— 它们是有 manifest 的正常应用，此前被误判为运行时环境而不显示。
- **垃圾条目剔除**：扫描只认带 manifest 的应用（`SYSTEM_APPS` 登记的除外），不再出现无 manifest 的壳目录 `app` 与安装残留目录 `fndesk_<时间戳>`。
- **显示名与桌面对齐**：日志中心→日志、快照→文件快照、Trim.Seek→素材库 Seek、飞牛影视在线扩展→影视在线扩展管理（仅在解析结果等于旧名时替换，不影响其他语言）。
- **接口口径统一**：`/api/apps/version` 的 `total` 改由 `_visible_apps()` 计算，与 `/api/apps` 一致。
- **图标 MIME 修正**：`_mime_of_icon()` 按魔数返回真实类型，WebP 不再谎报 `image/png`。

### v2.21.0（2026-10-30）

- 应用列表自动感知：缓存 20 秒 TTL + 目录指纹，新增/卸载应用自动重扫
- 新增 `/api/apps/version` 版本指纹接口，页面每 3 秒轮询自动更新
- 修复公网（FRP）访问时图标 URL 错误指向内网端口导致图标不更新
