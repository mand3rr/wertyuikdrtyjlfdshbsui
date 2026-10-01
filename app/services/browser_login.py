import asyncio
import json
import re
import shutil
import sys
from pathlib import Path

from app.config import settings

from loguru import logger

_active_browsers: list = []


def _profile_dir(login: str) -> Path:
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', str(login)) or "default"
    path = settings.browser_profiles_dir / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


STEAM_DOMAINS = [
    ".steamcommunity.com",
    ".steampowered.com",
    ".store.steampowered.com",
    ".help.steampowered.com",
    ".login.steampowered.com",
]

# Common Chrome/Chromium install locations on Windows
_WINDOWS_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome Beta\Application\chrome.exe",
    r"C:\Program Files\Google\Chrome Dev\Application\chrome.exe",
    r"C:\Program Files\Chromium\Application\chrome.exe",
    r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
]


def _find_chrome() -> str | None:
    """Try to locate a Chrome/Chromium binary. Returns path or None."""
    # 1. Check PATH
    for name in ("chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found

    # 2. Windows-specific fixed paths + Registry
    if sys.platform == "win32":
        import os

        local = os.environ.get("LOCALAPPDATA", "")
        user_path = Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"
        candidates = _WINDOWS_CHROME_PATHS + ([str(user_path)] if local else [])
        for p in candidates:
            if Path(p).exists():
                return p

        # Registry lookup (handles non-standard install paths)
        try:
            import winreg

            reg_keys = [
                (
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ),
                (
                    winreg.HKEY_CURRENT_USER,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                ),
            ]
            for hive, key_path in reg_keys:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        path, _ = winreg.QueryValueEx(key, "")
                        if path and Path(path).exists():
                            return path
                except FileNotFoundError:
                    pass
        except Exception:
            pass

    return None


async def _wait_connection_closed(browser) -> None:
    """Fallback: wait until CDP connection closes (with grace period)."""
    await asyncio.sleep(3)
    consecutive_closed = 0
    tick = 0
    while True:
        await asyncio.sleep(1)
        tick += 1
        try:
            closed = not browser or not browser.connection or browser.connection.closed
        except Exception as e:
            logger.debug(f"[browser] connection check error at tick={tick}: {e}")
            closed = True

        if tick % 5 == 0:
            logger.debug(
                f"[browser] fallback tick={tick} closed={closed} consecutive={consecutive_closed}"
            )

        if closed:
            consecutive_closed += 1
            logger.debug(
                f"[browser] connection closed tick={tick}, consecutive={consecutive_closed}"
            )
            if consecutive_closed >= 3:
                break
        else:
            consecutive_closed = 0


async def open_browser_and_login(account: dict) -> None:
    """Open Chrome via nodriver and log into Steam with credentials."""
    import nodriver as uc

    login = account.get("login", "")
    password = account.get("password", "")
    if not login or not password:
        raise ValueError("Account has no login or password")

    chrome_path = _find_chrome()
    if chrome_path:
        logger.debug(f"[browser] Using Chrome at: {chrome_path}")
    else:
        logger.warning("[browser] Chrome not found in standard locations — nodriver will attempt its own detection")

    # Kill any stale Chrome instances from previous sessions
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] and "chrome" in proc.info["name"].lower():
                    cmdline = proc.info.get("cmdline") or []
                    if any("remote-debugging-port" in arg for arg in cmdline):
                        logger.warning(f"[browser] Killing stale Chrome pid={proc.info['pid']}")
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception:
        pass

    max_retries = 2
    last_error = None
    profile_dir = _profile_dir(login)
    for attempt in range(max_retries):
        try:
            if chrome_path:
                browser = await uc.start(browser_executable_path=chrome_path, user_data_dir=profile_dir, sandbox=True)
            else:
                browser = await uc.start(user_data_dir=profile_dir, sandbox=True)
            break
        except Exception as exc:
            last_error = exc
            logger.warning(f"[browser] Start attempt {attempt+1}/{max_retries} failed: {exc}")
            # Kill stale Chrome processes before retry
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        if proc.info["name"] and "chrome" in proc.info["name"].lower():
                            cmdline = proc.info.get("cmdline") or []
                            if any("remote-debugging-port" in arg for arg in cmdline):
                                proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:
                pass
            await asyncio.sleep(3)
    else:
        raise RuntimeError(f"Не удалось запустить браузер после {max_retries} попыток") from last_error

    _active_browsers.append(browser)

    try:
        tab = await browser.get("https://steamcommunity.com/", timeout=30)
        await asyncio.sleep(5)
    except Exception as exc:
        logger.warning(f"[browser] CDP connection after start failed: {exc}, retrying...")
        try:
            browser.stop()
        except Exception:
            pass
        await asyncio.sleep(2)
        if chrome_path:
            browser = await uc.start(browser_executable_path=chrome_path, user_data_dir=profile_dir, sandbox=True)
        else:
            browser = await uc.start(user_data_dir=profile_dir, sandbox=True)
        _active_browsers.append(browser)
        tab = await browser.get("https://steamcommunity.com/", timeout=30)
        await asyncio.sleep(5)
    try:
        # Check if already logged in by looking for "Sign In" link
        logged_in = await tab.evaluate(
            "document.querySelector('a.global_action_link') === null"
        )

        if not logged_in:
            logger.info(f"[browser] Logging in {login} via nodriver...")
            await tab.get("https://store.steampowered.com/login/")
            await asyncio.sleep(5)

            await tab.evaluate(f"""
                (() => {{
                    const val = {json.dumps(login)};
                    const inputs = [...document.querySelectorAll('input[type="text"]')].filter(i => i.offsetParent !== null);
                    if (inputs.length) {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(inputs[0], val);
                        inputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
                        inputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.5)

            await tab.evaluate(f"""
                (() => {{
                    const val = {json.dumps(password)};
                    const inputs = [...document.querySelectorAll('input[type="password"]')].filter(i => i.offsetParent !== null);
                    if (inputs.length) {{
                        const nativeSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                        nativeSetter.call(inputs[0], val);
                        inputs[0].dispatchEvent(new Event('input', {{bubbles: true}}));
                        inputs[0].dispatchEvent(new Event('change', {{bubbles: true}}));
                    }}
                }})()
            """)
            await asyncio.sleep(0.5)

            await tab.evaluate("""
                (() => {
                    const btn = document.querySelector('button[type="submit"]');
                    if (btn) btn.click();
                })()
            """)
            logger.info(f"[browser] Login form submitted for {login}, waiting for user to handle 2FA...")
            await asyncio.sleep(5)

        # Save session cookies from the browser
        try:
            raw_cookies = await tab.send(uc.cdp.network.get_cookies())
            if raw_cookies:
                cookies_list = []
                for c in raw_cookies:
                    cookies_list.append({
                        "name": c.get("name", ""),
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ""),
                        "path": c.get("path", "/"),
                        "secure": c.get("secure", False),
                        "httpOnly": c.get("httpOnly", False),
                    })
                session_cookies_json = json.dumps(cookies_list)
                from app.database import get_db as _get_db
                db = await _get_db()
                await db.execute(
                    "UPDATE logpass_accounts SET session_cookies = ? WHERE id = ?",
                    (session_cookies_json, account.get("id")),
                )
                await db.commit()
                logger.info(f"[browser] Saved {len(cookies_list)} cookies for {login}")
        except Exception as e:
            logger.warning(f"[browser] Failed to save cookies for {login}: {e}")

        # Navigate to profile
        steam_id = account.get("steam_id") or ""
        start_url = f"https://steamcommunity.com/profiles/{steam_id}/" if steam_id else "https://steamcommunity.com/"
        await browser.get(start_url)
        logger.info(f"[browser] Opened for {login}")

        # Wait until Chrome process exits
        await asyncio.sleep(2)
        pid = getattr(browser, "_process_pid", None) or getattr(
            getattr(browser, "_process", None), "pid", None
        )
        logger.debug(f"[browser] monitoring Chrome: pid={pid}")

        if pid:
            try:
                import psutil
                proc = psutil.Process(pid)
                tick = 0
                while True:
                    await asyncio.sleep(1)
                    tick += 1
                    try:
                        running = proc.is_running()
                        status = proc.status()
                    except psutil.NoSuchProcess:
                        break
                    except Exception:
                        break
                    if not running or status == psutil.STATUS_ZOMBIE:
                        break
            except ImportError:
                await _wait_connection_closed(browser)
            except Exception:
                pass
        else:
            await _wait_connection_closed(browser)

    finally:
        if browser in _active_browsers:
            _active_browsers.remove(browser)

        loop = asyncio.get_running_loop()
        _original_handler = loop.get_exception_handler()

        def _suppress_nodriver_close_errors(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
                return
            if _original_handler:
                _original_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_suppress_nodriver_close_errors)
        try:
            browser.stop()
        except Exception:
            pass
        await asyncio.sleep(0.5)
        loop.set_exception_handler(_original_handler)
        logger.debug(f"[browser] Closed for {login}")


async def open_browser_with_cookies(account: dict) -> None:
    """Open a Chrome browser with Steam session cookies using nodriver."""
    import nodriver as uc

    cookies_json = account.get("session_cookies")
    if not cookies_json:
        raise ValueError("No session cookies saved. Validate the account first.")

    cookies = json.loads(cookies_json)

    # Try to find Chrome ourselves so we can give a better error
    chrome_path = _find_chrome()
    if chrome_path:
        logger.debug(f"[browser] Using Chrome at: {chrome_path}")
    else:
        logger.warning(
            "[browser] Chrome not found in standard locations — "
            "nodriver will attempt its own detection"
        )

    profile_dir = _profile_dir(account.get("login", ""))
    max_retries = 2
    last_error = None
    for attempt in range(max_retries):
        try:
            if chrome_path:
                browser = await uc.start(browser_executable_path=chrome_path, user_data_dir=profile_dir, sandbox=True)
            else:
                browser = await uc.start(user_data_dir=profile_dir, sandbox=True)
            break
        except Exception as exc:
            last_error = exc
            logger.warning(f"[browser] Start attempt {attempt+1}/{max_retries} failed: {exc}")
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        if proc.info["name"] and "chrome" in proc.info["name"].lower():
                            cmdline = proc.info.get("cmdline") or []
                            if any("remote-debugging-port" in arg for arg in cmdline):
                                proc.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
            except Exception:
                pass
            await asyncio.sleep(3)
    else:
        raise RuntimeError(f"Не удалось запустить браузер после {max_retries} попыток") from last_error

    _active_browsers.append(browser)

    try:
        tab = await browser.get("about:blank")

        for c in cookies:
            domain = c.get("domain") or ".steamcommunity.com"
            domains_to_set = [domain]
            if "steam" in domain:
                domains_to_set = list(set([domain] + STEAM_DOMAINS))

            for d in domains_to_set:
                try:
                    await tab.send(
                        uc.cdp.network.set_cookie(
                            name=c["name"],
                            value=c["value"],
                            domain=d,
                            path=c.get("path") or "/",
                            secure=c.get("secure", True),
                            http_only=c.get("httpOnly", False),
                        )
                    )
                except Exception:
                    pass

        steam_id = account.get("steam_id") or ""
        start_url = (
            f"https://steamcommunity.com/profiles/{steam_id}/"
            if steam_id
            else "https://steamcommunity.com/"
        )
        await browser.get(start_url)
        logger.info(f"[browser] Opened for {account.get('login', account.get('id'))}")

        # Wait until the Chrome process exits.
        # We check the process PID directly — this is immune to CDP
        # connection state fluctuations during page navigation.
        await asyncio.sleep(2)  # let Chrome fully start

        pid = getattr(browser, "_process_pid", None) or getattr(
            getattr(browser, "_process", None), "pid", None
        )
        logger.debug(f"[browser] monitoring Chrome: pid={pid}")

        if pid:
            try:
                import psutil  # type: ignore

                proc = psutil.Process(pid)
                logger.debug(
                    f"[browser] Chrome pid={pid} status={proc.status()} — waiting for user to close"
                )
                tick = 0
                while True:
                    await asyncio.sleep(1)
                    tick += 1
                    try:
                        running = proc.is_running()
                        status = proc.status()
                    except psutil.NoSuchProcess:
                        logger.debug(
                            f"[browser] Chrome pid={pid} — process no longer exists (tick={tick})"
                        )
                        break
                    except Exception as e:
                        logger.debug(
                            f"[browser] Chrome pid={pid} — psutil error (tick={tick}): {e}"
                        )
                        break
                    if tick % 5 == 0:
                        conn_closed = getattr(
                            getattr(browser, "connection", None), "closed", "?"
                        )
                        logger.debug(
                            f"[browser] Chrome pid={pid} alive tick={tick} status={status} conn.closed={conn_closed}"
                        )
                    if not running or status == psutil.STATUS_ZOMBIE:
                        logger.debug(
                            f"[browser] Chrome pid={pid} exited at tick={tick}, final status={status}"
                        )
                        break
            except ImportError:
                logger.debug(
                    "[browser] psutil not installed — falling back to connection check"
                )
                await _wait_connection_closed(browser)
            except Exception as e:
                logger.debug(f"[browser] pid monitoring error: {e}")
        else:
            logger.debug(
                "[browser] could not get Chrome PID — falling back to connection check"
            )
            await _wait_connection_closed(browser)

    finally:
        if browser in _active_browsers:
            _active_browsers.remove(browser)

        loop = asyncio.get_running_loop()
        _original_handler = loop.get_exception_handler()

        def _suppress_nodriver_close_errors(loop, context):
            exc = context.get("exception")
            if isinstance(exc, (ConnectionRefusedError, ConnectionResetError, OSError)):
                return
            if _original_handler:
                _original_handler(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(_suppress_nodriver_close_errors)
        try:
            browser.stop()
        except Exception:
            pass
        await asyncio.sleep(0.5)
        loop.set_exception_handler(_original_handler)

        logger.debug(f"[browser] Closed for {account.get('login', account.get('id'))}")
