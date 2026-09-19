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
