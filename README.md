# OCR 微服务

通用 OCR 能力（FastAPI + RapidOCR），独立部署，供多个业务复用。

## 功能

- **业务接口** `POST /recognize`：传图片，返回识别文本 + 每行文字（含置信度和坐标 box）。API key 鉴权（`X-API-Key` 头），key 存于 SQLite。
- **管理 Dashboard** `GET /dashboard`：浏览器界面，API key 管理（创建/停用/删除）+ 用量统计（总调用/图片量/近 7 天趋势）。管理员 key（`X-Admin-Key`）鉴权。
- **管理 API** `/admin/keys`（增删查）、`/admin/usage`：供脚本/其他管理端调用。
- **健康检查** `GET /health`。

## 快速开始

```bash
cp .env.example .env
# 编辑 .env：
#   OCR_ADMIN_KEY=<openssl rand -hex 32>

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后：
1. 打开 `http://localhost:8000/dashboard`，输入管理员 key 登录
2. 创建一个业务 key（如"体彩对账系统"），复制生成的 key
3. 用该 key 调 `/recognize`：

```bash
curl -X POST http://localhost:8000/recognize \
  -H "X-API-Key: <业务key>" \
  -F "file=@screenshot.png"
```

## 调用示例

```json
{
  "success": true,
  "text": "今日收款65笔，合计\n3657.00",
  "lines": [
    { "text": "今日收款65笔，合计", "confidence": 0.90, "box": [[248,237],[582,237],[582,273],[248,273]] },
    { "text": "3657.00", "confidence": 0.81, "box": [[221,324],[667,324],[667,418],[221,418]] }
  ],
  "duration_ms": 236
}
```

## 配置项

| 环境变量 | 说明 |
|---------|------|
| `OCR_SERVICE_PORT` | 服务端口（默认 8000） |
| `OCR_ADMIN_KEY` | 管理员 key，管理接口/dashboard 用 |
| `OCR_DB_PATH` | SQLite 文件路径（默认 ocr.db，含 api_keys 与 usage_log 表） |
| `OCR_MAX_SIDE` | 图片最长边上限，超限等比缩放（默认 2048） |

## 部署

- 单进程即可（RapidOCR 模型常驻内存，线程安全懒加载）。
- 需要 Python 3.9+。公司服务器部署时按目标 OS 装依赖即可（Linux/Windows 均支持）。
- 建议配 Nginx 反代 HTTPS，`.env` 与 `ocr.db` 不入库。
