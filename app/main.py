"""OCR 微服务入口（FastAPI + RapidOCR）。

通用 OCR 能力，供多个业务复用。
- 业务调用：POST /recognize（X-API-Key 鉴权，key 存于 DB）
- 管理：POST/GET/DELETE /admin/keys（OCR_ADMIN_KEY 鉴权）+ /dashboard 页面
"""
import os
import secrets
import time
from contextlib import asynccontextmanager

import cv2
import numpy as np
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db
from .ocr import get_engine

load_dotenv()

ADMIN_KEY = os.getenv("OCR_ADMIN_KEY", "")
MAX_SIDE = int(os.getenv("OCR_MAX_SIDE", "2048"))

if not ADMIN_KEY:
    print("[WARN] OCR_ADMIN_KEY 未配置，/admin 管理接口与 dashboard 将不可用")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 启动：建表 + 预热模型
    db.init_db()
    get_engine()
    yield


app = FastAPI(title="OCR 微服务", version="0.2.0", lifespan=lifespan)


class LineResult(BaseModel):
    text: str
    confidence: float
    box: list[list[float]]


class RecognizeResponse(BaseModel):
    success: bool
    text: str
    lines: list[LineResult]
    duration_ms: int


class KeyCreate(BaseModel):
    name: str  # 用途说明


class KeyOut(BaseModel):
    id: str
    name: str
    key: str
    created_at: int
    active: bool
    calls: int = 0
    avg_ms: float = 0
    total_bytes: int = 0
    last_used_at: int | None = None


# ---------- 工具 ----------

def _check_business_key(x_api_key: str | None) -> str:
    """校验业务调用 key，返回 key_id。"""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key")
    key_id = db.validate_key(x_api_key)
    if not key_id:
        raise HTTPException(status_code=401, detail="无效或已停用的 API Key")
    return key_id


def _check_admin_key(x_admin_key: str | None) -> None:
    if not ADMIN_KEY:
        raise HTTPException(status_code=503, detail="管理员 key 未配置")
    if not x_admin_key or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="无效的管理员 Key")


def _preprocess(image: np.ndarray) -> np.ndarray:
    h, w = image.shape[:2]
    longest = max(h, w)
    if longest > MAX_SIDE:
        scale = MAX_SIDE / longest
        image = cv2.resize(image, (int(w * scale), int(h * scale)))
    return image


# ---------- 业务接口 ----------

@app.post("/recognize")
async def recognize(
    file: UploadFile = File(...),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> RecognizeResponse:
    key_id = _check_business_key(x_api_key)

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="空文件")

    np_arr = np.frombuffer(data, np.uint8)
    image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=415, detail="无法解析为图片（支持 jpg/png/webp/bmp）")

    image = _preprocess(image)

    t0 = time.perf_counter()
    engine = get_engine()
    result, _elapse = engine(image)
    duration_ms = int((time.perf_counter() - t0) * 1000)

    # 记录用量（失败也记录，便于排查）
    db.log_usage(key_id, duration_ms, len(data))

    lines: list[LineResult] = []
    text_parts: list[str] = []
    if result:
        for line in result:
            box, text, confidence = line
            lines.append(
                LineResult(
                    text=text,
                    confidence=float(confidence),
                    box=[[float(p[0]), float(p[1])] for p in box],
                )
            )
            text_parts.append(text)

    return RecognizeResponse(
        success=True,
        text="\n".join(text_parts),
        lines=lines,
        duration_ms=duration_ms,
    )


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# ---------- 管理接口（OCR_ADMIN_KEY 鉴权） ----------

@app.get("/admin/keys", response_model=list[KeyOut])
async def admin_list_keys(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")):
    _check_admin_key(x_admin_key)
    stats = db.usage_all()
    return [
        KeyOut(
            id=s["key_id"],
            name=s["name"],
            key=s["key"],
            created_at=s["key_created_at"],
            active=bool(s["active"]),
            calls=s["calls"],
            avg_ms=round(s["avg_ms"], 1),
            total_bytes=s["total_bytes"],
            last_used_at=s["last_used_at"],
        )
        for s in stats
    ]


@app.post("/admin/keys", status_code=201)
async def admin_create_key(
    body: KeyCreate,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _check_admin_key(x_admin_key)
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name 不能为空")
    raw_key = f"ocr_{secrets.token_urlsafe(32)}"
    created = db.create_key(body.name.strip(), raw_key)
    return {"id": created["id"], "name": created["name"], "key": raw_key, "created_at": created["created_at"]}


@app.delete("/admin/keys/{key_id}")
async def admin_delete_key(
    key_id: str,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _check_admin_key(x_admin_key)
    if not db.delete_key(key_id):
        raise HTTPException(status_code=404, detail="key 不存在")
    return {"success": True}


class KeyToggle(BaseModel):
    active: bool


@app.post("/admin/keys/{key_id}/toggle")
async def admin_toggle_key(
    key_id: str,
    body: KeyToggle,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    _check_admin_key(x_admin_key)
    if not db.set_key_active(key_id, body.active):
        raise HTTPException(status_code=404, detail="key 不存在")
    return {"success": True}


@app.get("/admin/usage")
async def admin_usage(
    days: int = 7,
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
):
    """按天返回各 key 的调用量（dashboard 图表用）。"""
    _check_admin_key(x_admin_key)
    since = int(time.time()) - days * 86400
    conn = db._connect()
    try:
        rows = conn.execute(
            """
            SELECT k.name, date(u.created_at, 'unixepoch', 'localtime') AS day,
                   COUNT(u.id) AS calls
            FROM api_keys k
            LEFT JOIN usage_log u ON u.key_id = k.id AND u.created_at >= ?
            WHERE k.active = 1
            GROUP BY k.id, day
            ORDER BY day
            """,
            (since,),
        ).fetchall()
        return {"days": days, "rows": [dict(r) for r in rows]}
    finally:
        conn.close()


# ---------- Dashboard 页面 ----------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OCR 服务管理</title>
<style>
  :root { --blue:#0066cc; --bg:#f5f5f7; --card:#fff; --ink:#1d1d1f; --muted:#86868b; --red:#d70015; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:-apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif; background:var(--bg); color:var(--ink); }
  .top { background:#000; color:#fff; height:44px; display:flex; align-items:center; padding:0 24px; font-size:13px; font-weight:600; }
  .wrap { max-width:960px; margin:0 auto; padding:24px; }
  h1 { font-size:32px; letter-spacing:-0.02em; margin:8px 0 4px; }
  .sub { color:var(--muted); font-size:15px; margin-bottom:24px; }
  .card { background:var(--card); border:1px solid rgba(0,0,0,.08); border-radius:18px; padding:24px; margin-bottom:20px; }
  .card h2 { font-size:20px; margin:0 0 16px; }
  .row { display:flex; gap:16px; flex-wrap:wrap; }
  .stat { flex:1; min-width:140px; }
  .stat .n { font-size:28px; font-weight:700; font-variant-numeric:tabular-nums; }
  .stat .l { color:var(--muted); font-size:13px; margin-top:2px; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th { text-align:left; color:var(--muted); font-weight:500; padding:8px 10px; border-bottom:1px solid rgba(0,0,0,.06); font-size:12px; }
  td { padding:10px; border-bottom:1px solid rgba(0,0,0,.04); }
  .mono { font-family:ui-monospace,Menlo,monospace; font-size:12px; }
  .badge { display:inline-block; padding:1px 8px; border-radius:999px; font-size:12px; }
  .badge.on { background:rgba(0,102,204,.12); color:var(--blue); }
  .badge.off { background:rgba(215,0,21,.1); color:var(--red); }
  input[type=text], select { height:36px; border:1px solid rgba(0,0,0,.15); border-radius:11px; padding:0 12px; font-size:14px; background:#fff; }
  button { height:36px; border:none; border-radius:999px; padding:0 18px; font-size:14px; cursor:pointer; }
  .btn-primary { background:var(--blue); color:#fff; }
  .btn-danger { background:var(--red); color:#fff; }
  .btn-ghost { background:rgba(0,0,0,.05); color:var(--ink); }
  .toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#1d1d1f; color:#fff; padding:10px 20px; border-radius:999px; font-size:14px; display:none; z-index:10; }
  .bar { display:flex; align-items:flex-end; gap:4px; height:120px; }
  .bar div { background:var(--blue); border-radius:4px 4px 0 0; min-width:12px; }
  .day { display:flex; flex-direction:column; align-items:center; gap:4px; }
</style>
</head>
<body>
<div class="top">OCR 服务管理</div>
<div class="wrap">
  <h1>OCR 微服务</h1>
  <p class="sub">API key 管理与用量统计 · 服务地址 <span class="mono" id="svc-url"></span></p>

  <div class="card" id="login-card">
    <h2>管理员登录</h2>
    <div class="row">
      <input type="password" id="adminkey" placeholder="输入 OCR_ADMIN_KEY" style="flex:1;min-width:240px">
      <button class="btn-primary" onclick="login()">进入</button>
    </div>
  </div>

  <div id="main" style="display:none">
    <div class="card">
      <h2>总览</h2>
      <div class="row" id="stats"></div>
    </div>

    <div class="card">
      <h2>新建 API Key</h2>
      <div class="row">
        <input type="text" id="newkey-name" placeholder="用途说明，如：体彩对账系统">
        <button class="btn-primary" onclick="createKey()">生成</button>
      </div>
      <div id="newkey-result" style="margin-top:12px;display:none" class="card">
        <b>新 key（仅显示一次，请保存）：</b>
        <div class="mono" id="newkey-val" style="background:rgba(0,0,0,.04);padding:10px;border-radius:10px;margin-top:8px;word-break:break-all"></div>
      </div>
    </div>

    <div class="card">
      <h2>API Keys</h2>
      <table>
        <thead><tr><th>用途</th><th>Key</th><th>状态</th><th>总调用</th><th>平均耗时</th><th>图片量</th><th>最近使用</th><th></th></tr></thead>
        <tbody id="keys-tbody"></tbody>
      </table>
    </div>

    <div class="card">
      <h2>近 7 天调用趋势</h2>
      <div class="row" id="trend"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
let adminKey = localStorage.getItem('ocr_admin_key') || '';
const H = { 'X-Admin-Key': adminKey, 'Content-Type': 'application/json' };

function toast(msg){ const t=document.getElementById('toast'); t.textContent=msg; t.style.display='block'; setTimeout(()=>t.style.display='none',2500); }
function login(){
  adminKey = document.getElementById('adminkey').value;
  localStorage.setItem('ocr_admin_key', adminKey);
  load();
}
async function api(url, opts={}){
  const res = await fetch(url, { ...opts, headers: { ...H, ...(opts.headers||{}) } });
  if (res.status === 401) { document.getElementById('main').style.display='none'; document.getElementById('login-card').style.display='block'; throw new Error('未授权'); }
  if (!res.ok) throw new Error((await res.json().catch(()=>({}))).detail || res.statusText);
  return res.json();
}
function fmtBytes(b){ if(!b) return '0 B'; const u=['B','KB','MB','GB']; let i=0; let v=b; while(v>1024 && i<3){v/=1024;i++;} return v.toFixed(v<10?1:0)+' '+u[i]; }
function fmtTime(ts){ if(!ts) return '—'; const d=new Date(ts*1000); return d.toLocaleDateString('zh-CN')+' '+d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}); }

async function load(){
  document.getElementById('login-card').style.display = adminKey ? 'none' : 'block';
  document.getElementById('main').style.display = adminKey ? 'block' : 'none';
  if (!adminKey) return;
  try {
    const keys = await api('/admin/keys');
    renderKeys(keys);
    const total = keys.reduce((a,k)=>a+k.calls,0);
    const totalBytes = keys.reduce((a,k)=>a+k.total_bytes,0);
    document.getElementById('stats').innerHTML = `
      <div class="stat"><div class="n">${keys.length}</div><div class="l">API Keys</div></div>
      <div class="stat"><div class="n">${total}</div><div class="l">总调用次数</div></div>
      <div class="stat"><div class="n">${fmtBytes(totalBytes)}</div><div class="l">总识别图片量</div></div>`;
    const usage = await api('/admin/usage?days=7');
    renderTrend(usage.rows);
  } catch(e) { toast(e.message); }
}

function renderKeys(keys){
  document.getElementById('keys-tbody').innerHTML = keys.map(k => `
    <tr>
      <td><b>${k.name}</b></td>
      <td class="mono">${k.key.slice(0,16)}…</td>
      <td><span class="badge ${k.active?'on':'off'}">${k.active?'启用':'停用'}</span></td>
      <td class="mono">${k.calls}</td>
      <td class="mono">${k.avg_ms} ms</td>
      <td class="mono">${fmtBytes(k.total_bytes)}</td>
      <td>${fmtTime(k.last_used_at)}</td>
      <td><button class="btn-ghost" onclick="toggleKey('${k.id}',${!k.active})">${k.active?'停用':'启用'}</button>
          <button class="btn-danger" onclick="delKey('${k.id}')">删除</button></td>
    </tr>`).join('');
}

async function createKey(){
  const name = document.getElementById('newkey-name').value.trim();
  if(!name) return toast('请填用途说明');
  try {
    const r = await api('/admin/keys', { method:'POST', body: JSON.stringify({name}) });
    document.getElementById('newkey-val').textContent = r.key;
    document.getElementById('newkey-result').style.display='block';
    document.getElementById('newkey-name').value='';
    load();
  } catch(e){ toast(e.message); }
}
async function toggleKey(id, active){
  await api('/admin/keys/'+id+'/toggle', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({active}) });
  load();
}
async function delKey(id){
  if(!confirm('确认删除该 key？其用量记录将保留。')) return;
  await api('/admin/keys/'+id, { method:'DELETE' });
  load();
}

function renderTrend(rows){
  // 汇总每天各 key 的调用量
  const dayMap = {};
  rows.forEach(r => { if(r.day) dayMap[r.day] = (dayMap[r.day]||0) + r.calls; });
  const days = Object.keys(dayMap).sort();
  if(!days.length){ document.getElementById('trend').innerHTML='<p class="sub">暂无数据</p>'; return; }
  const max = Math.max(...days.map(d=>dayMap[d]));
  document.getElementById('trend').innerHTML = days.map(d => `
    <div class="day">
      <div style="font-size:12px;color:var(--muted)">${dayMap[d]}</div>
      <div class="bar"><div style="height:${max?Math.max(4,dayMap[d]/max*120):4}px"></div></div>
      <div style="font-size:11px;color:var(--muted)">${d.slice(5)}</div>
    </div>`).join('');
}

document.getElementById('svc-url').textContent = location.origin;
load();
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return HTMLResponse(_DASHBOARD_HTML)
