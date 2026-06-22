import os
import sys
import json
import re
import socket
import ipaddress
import hmac
import hashlib
import secrets
import urllib.request
import urllib.parse
import threading
import time
import webbrowser
import base64
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

# Add current script directory to sys.path for safety
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

def sanitize_filename(name, fallback="download"):
    """Strip path components and illegal characters so user-supplied names
    can never escape the save directory (path traversal protection)."""
    if not name:
        return fallback
    name = os.path.basename(str(name))
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', name).strip().strip('.')
    return name or fallback

def sanitize_subfolder(sub):
    """Sanitize a relative subfolder path (e.g. 'Series/Season 1'); each component
    is cleaned so the result always stays inside the save directory."""
    if not sub:
        return ""
    parts = [sanitize_filename(p, "") for p in str(sub).replace("\\", "/").split("/")]
    return "/".join(p for p in parts if p)

_safe_host_cache = {}

def _open_upstream(url, timeout=15, retries=3):
    """Open an upstream request, retrying on connection resets/timeouts. This
    absorbs ISP interference (RST injection / throttled drops) on a per-request
    basis so the player doesn't notice the glitch. Free and fully local."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            return urllib.request.urlopen(req, timeout=timeout)
        except Exception as e:
            last = e
            time.sleep(min(0.25 * (i + 1), 1.0))
    raise last

def is_safe_remote_url(raw_url):
    """Return True only for public http(s) hosts. Blocks the proxy from being
    used to reach loopback/private/link-local addresses (SSRF protection).
    Results are cached per host so streaming doesn't pay a DNS lookup per segment."""
    try:
        parsed = urllib.parse.urlparse(raw_url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    cached = _safe_host_cache.get(host)
    if cached is not None:
        return cached
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        _safe_host_cache[host] = False
        return False
    safe = True
    for info in infos:
        try:
            ip_obj = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local
                or ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_unspecified):
            safe = False
            break
    _safe_host_cache[host] = safe
    return safe

def write_self_healing_launchers(root_dir):
    """Write the root launchers (.bat/.vbs). They:
      1) relocate a root python_env into app\\ on a clean start (when it isn't locked),
      2) find pythonw.exe across EVERY layout clients have in the wild — embeddable
         (python_env\\pythonw.exe) or venv (python_env\\Scripts\\pythonw.exe), in app\\
         or in the root — and run app\\backend.py with whichever exists.
    This is why some machines worked and others didn't: the pythonw path differs."""
    bat = (
        "@echo off\n"
        "if exist \"%~dp0python_env\\\" if not exist \"%~dp0app\\python_env\\\" "
        "robocopy \"%~dp0python_env\" \"%~dp0app\\python_env\" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul 2>&1\n"
        "set \"PYW=\"\n"
        "if exist \"%~dp0app\\python_env\\pythonw.exe\" set \"PYW=%~dp0app\\python_env\\pythonw.exe\"\n"
        "if not defined PYW if exist \"%~dp0app\\python_env\\Scripts\\pythonw.exe\" set \"PYW=%~dp0app\\python_env\\Scripts\\pythonw.exe\"\n"
        "if not defined PYW if exist \"%~dp0python_env\\pythonw.exe\" set \"PYW=%~dp0python_env\\pythonw.exe\"\n"
        "if not defined PYW if exist \"%~dp0python_env\\Scripts\\pythonw.exe\" set \"PYW=%~dp0python_env\\Scripts\\pythonw.exe\"\n"
        "set \"BE=%~dp0app\\backend.py\"\n"
        "if not exist \"%BE%\" set \"BE=%~dp0backend.py\"\n"
        "if defined PYW ( start \"\" \"%PYW%\" \"%BE%\" ) else ( start \"\" pythonw \"%BE%\" )\n"
    )
    vbs = (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'q = Chr(34)\n'
        'sd = fso.GetParentFolderName(WScript.ScriptFullName)\n'
        'If fso.FolderExists(sd & "\\python_env") And Not fso.FolderExists(sd & "\\app\\python_env") Then\n'
        '    WshShell.Run "robocopy " & q & sd & "\\python_env" & q & " " & q & sd & "\\app\\python_env" & q & " /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np", 0, True\n'
        'End If\n'
        'pyw = ""\n'
        'If fso.FileExists(sd & "\\app\\python_env\\pythonw.exe") Then\n'
        '    pyw = sd & "\\app\\python_env\\pythonw.exe"\n'
        'ElseIf fso.FileExists(sd & "\\app\\python_env\\Scripts\\pythonw.exe") Then\n'
        '    pyw = sd & "\\app\\python_env\\Scripts\\pythonw.exe"\n'
        'ElseIf fso.FileExists(sd & "\\python_env\\pythonw.exe") Then\n'
        '    pyw = sd & "\\python_env\\pythonw.exe"\n'
        'ElseIf fso.FileExists(sd & "\\python_env\\Scripts\\pythonw.exe") Then\n'
        '    pyw = sd & "\\python_env\\Scripts\\pythonw.exe"\n'
        'End If\n'
        'be = sd & "\\app\\backend.py"\n'
        'If Not fso.FileExists(be) Then be = sd & "\\backend.py"\n'
        'If pyw <> "" Then\n'
        '    WshShell.Run q & pyw & q & " " & q & be & q, 0, False\n'
        'Else\n'
        '    WshShell.Run "pythonw " & q & be & q, 0, False\n'
        'End If\n'
    )
    try:
        with open(os.path.join(root_dir, "Elamir-Media-Hub.bat"), 'w', encoding='utf-8') as f:
            f.write(bat)
    except Exception:
        pass
    try:
        with open(os.path.join(root_dir, "Elamir-Media-Hub.vbs"), 'w', encoding='utf-8') as f:
            f.write(vbs)
    except Exception:
        pass

def check_and_run_migration():
    import subprocess
    import shutil
    import glob
    script_dir = os.path.dirname(os.path.abspath(__file__))
    is_old_layout = os.path.exists(os.path.join(script_dir, "python_env")) and os.path.basename(script_dir) != "app"
    is_dev = os.path.exists(os.path.join(script_dir, ".git")) or os.path.exists(os.path.join(os.path.dirname(script_dir), ".git"))

    if is_old_layout and not is_dev:
        pid = os.getpid()
        app_dir = os.path.join(script_dir, "app")
        os.makedirs(app_dir, exist_ok=True)

        # PHASE 1 (Python shutil - no lock issues): Move all files except python_env
        # backend.py is copied (not moved) because Python reads it from RAM - but we copy to app/
        try:
            dst_backend = os.path.join(app_dir, "backend.py")
            if os.path.exists(dst_backend):
                os.remove(dst_backend)
            shutil.copy2(__file__, dst_backend)
        except Exception:
            pass

        cache_dir = os.path.join(app_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)

        for fname in ["index.html", "config.json", "push_to_github.bat", ".gitignore"]:
            src = os.path.join(script_dir, fname)
            if os.path.exists(src):
                try:
                    dst = os.path.join(app_dir, fname)
                    if os.path.exists(dst):
                        os.remove(dst)
                    shutil.move(src, dst)
                except Exception:
                    pass

        error_log_src = os.path.join(script_dir, "error_log.txt")
        if os.path.exists(error_log_src):
            try:
                dst = os.path.join(cache_dir, "error_log.txt")
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(error_log_src, dst)
            except Exception:
                pass

        for src in glob.glob(os.path.join(script_dir, ".cache_*.dat")):
            try:
                dst = os.path.join(cache_dir, os.path.basename(src))
                if os.path.exists(dst):
                    os.remove(dst)
                shutil.move(src, dst)
            except Exception:
                pass

        # Write the FINAL self-healing launchers (in Python — no fragile echo
        # escaping). On every clean start they relocate python_env into app\ when
        # it isn't locked, so the migration always finishes even if moving it right
        # now fails. They run app\backend.py either way.
        write_self_healing_launchers(script_dir)

        migrate_finish = os.path.join(script_dir, "migrate_finish.bat")
        finish_content = (
            "@echo off\n"
            f"taskkill /f /pid {pid} >nul 2>&1\n"
            ":waitloop\n"
            "timeout /t 1 /nobreak >nul\n"
            f'tasklist /fi "PID eq {pid}" 2>nul | find "{pid}" >nul && goto waitloop\n'
            "timeout /t 2 /nobreak >nul\n"
            # python is gone now, so python_env is unlocked — move it into app\
            f'if exist "{script_dir}\\python_env" if not exist "{script_dir}\\app\\python_env" '
            f'robocopy "{script_dir}\\python_env" "{script_dir}\\app\\python_env" /E /MOVE /NFL /NDL /NJH /NJS /nc /ns /np >nul 2>&1\n'
            # remove leftover root files
            f'if exist "{script_dir}\\backend.py" del /f /q "{script_dir}\\backend.py" >nul 2>&1\n'
            f'if exist "{script_dir}\\index.html" del /f /q "{script_dir}\\index.html" >nul 2>&1\n'
            f'if exist "{script_dir}\\push_to_github.bat" del /f /q "{script_dir}\\push_to_github.bat" >nul 2>&1\n'
            f'if exist "{script_dir}\\__pycache__" rmdir /s /q "{script_dir}\\__pycache__" >nul 2>&1\n'
            # relaunch through the self-healing VBS (it also relocates python_env if needed)
            f'start "" wscript.exe "{script_dir}\\Elamir-Media-Hub.vbs"\n'
            "(goto) 2>nul & del \"%~f0\"\n"
        )
        try:
            with open(migrate_finish, 'w', encoding='utf-8') as f:
                f.write(finish_content)
        except Exception:
            pass

        try:
            subprocess.Popen(
                ["cmd.exe", "/c", migrate_finish],
                creationflags=0x08000000 if sys.platform == 'win32' else 0,
                close_fds=True
            )
            time.sleep(2)
            sys.exit(0)
        except Exception:
            pass

    # Once we're in the new layout, ALWAYS refresh the root launchers to the
    # universal self-healing version, so every client converges no matter how its
    # launcher/python_env (embeddable vs venv) ended up. Then tidy leftovers —
    # but NEVER delete the python_env we're actually running from.
    if not is_dev and os.path.basename(script_dir) == "app":
        parent_dir = os.path.dirname(script_dir)
        write_self_healing_launchers(parent_dir)

        root_pyenv = os.path.join(parent_dir, "python_env")
        root_pycache = os.path.join(parent_dir, "__pycache__")
        try:
            exe_dir = os.path.abspath(os.path.dirname(sys.executable)).lower()
        except Exception:
            exe_dir = ""
        running_from_root = exe_dir.startswith(os.path.abspath(root_pyenv).lower())

        # Only remove a root python_env that's truly orphaned (we're not using it,
        # and it has been successfully relocated into app\).
        app_pyenv = os.path.join(script_dir, "python_env")
        if os.path.exists(root_pyenv) and not running_from_root and os.path.exists(app_pyenv):
            shutil.rmtree(root_pyenv, ignore_errors=True)
        if os.path.exists(root_pycache):
            shutil.rmtree(root_pycache, ignore_errors=True)

check_and_run_migration()

# Redirect stdout/stderr if frozen (PyInstaller executable) or running headlessly (sys.stdout is None)
if sys.stdout is None or getattr(sys, 'frozen', False):
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__)) if not getattr(sys, 'frozen', False) else "c:\\Users\\Public"
        cache_dir = os.path.join(base_dir, "cache")
        os.makedirs(cache_dir, exist_ok=True)
        log_file = open(os.path.join(cache_dir, "error_log.txt"), "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    except:
        class DummyWriter:
            def write(self, data): return len(data)
            def flush(self): pass
            def reconfigure(self, *args, **kwargs): pass
            encoding = 'utf-8'
            errors = 'strict'
            def isatty(self): return False
        sys.stdout = DummyWriter()
        sys.stderr = DummyWriter()

# Ensure Windows console supports Unicode/Arabic characters
if sys.platform.startswith('win'):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

def get_base_dir():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_assets_dir():
    if getattr(sys, 'frozen', False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))


def get_cache_dir():
    cache_dir = os.path.join(get_base_dir(), "cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

VERSION = "2.0"

# Global Configurations & Cache
CONFIG_FILE = os.path.join(get_base_dir(), "config.json")
DEFAULT_SAVE_DIR = os.path.join(os.path.expanduser('~'), 'Downloads')

# Default credentials fallback
config = {
    "domain": "http://fd.otbnver.club",
    "username": "",
    "password": "",
    "save_dir": DEFAULT_SAVE_DIR,
    "favorites": [],
    "recents": [],
    "playback_positions": {}
}


# Cache containers
CACHED_MOVIES = None
CACHED_SERIES = None
CACHED_SERIES_INFO = {}
CACHED_LIVE = None
CACHED_VOD_CATS = None
CACHED_SERIES_CATS = None
CACHED_LIVE_CATS = None

# Active downloads state
downloads_db = {}
downloads_lock = threading.Lock()

def apply_active_profile_creds():
    global config
    profiles = config.get("profiles", [])
    active_id = config.get("active_profile_id")
    if active_id:
        active_prof = None
        for p in profiles:
            if p.get("id") == active_id:
                active_prof = p
                break
        if active_prof:
            ptype = active_prof.get("type", "xtream")
            if ptype == "xtream":
                config["domain"] = active_prof.get("domain", "")
                config["username"] = active_prof.get("username", "")
                config["password"] = active_prof.get("password", "")
            else:
                config["domain"] = "http://m3u.local"
                config["username"] = active_id
                config["password"] = "m3u"

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                config.update(loaded)
        except Exception as e:
            print(f"Error loading config: {e}")
            
    if "profiles" not in config:
        config["profiles"] = []
    if "active_profile_id" not in config:
        config["active_profile_id"] = None
    if "favorites" not in config:
        config["favorites"] = []
    if "recents" not in config:
        config["recents"] = []
    if "library_items" not in config:
        config["library_items"] = []
    if "playback_positions" not in config:
        config["playback_positions"] = {}

    apply_active_profile_creds()

    # Normalize domain to remove trailing slashes
    if config.get("domain"):
        config["domain"] = config["domain"].rstrip('/')

def save_config():
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)
    except Exception as e:
        print(f"Error saving config: {e}")

# ── App lock (password gate for remote/network devices) ──
def hash_password(pw):
    return hashlib.sha256(("emh_lock_salt_" + (pw or "")).encode("utf-8")).hexdigest()

def app_lock_enabled():
    return bool(config.get("app_password"))

def _get_app_secret():
    sec = config.get("app_secret")
    if not sec:
        sec = secrets.token_hex(32)
        config["app_secret"] = sec
        save_config()
    return sec

def make_auth_token():
    # Stateless, HMAC-signed token bound to the current password — survives restarts,
    # and is invalidated automatically when the password (or secret) changes.
    expiry = int(time.time()) + 30 * 24 * 3600
    msg = f"{expiry}.{config.get('app_password','')}"
    sig = hmac.new(_get_app_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{expiry}.{sig}"

def verify_auth_token(token):
    try:
        expiry_s, sig = token.split(".", 1)
        if int(expiry_s) < time.time():
            return False
        msg = f"{expiry_s}.{config.get('app_password','')}"
        expected = hmac.new(_get_app_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

def add_to_library_config(filename, size_bytes):
    try:
        load_config()
        if "library_items" not in config:
            config["library_items"] = []
        
        # Check if already exists, update size if exists
        exists = False
        for item in config["library_items"]:
            if item.get("name") == filename:
                item["size_mb"] = round(size_bytes / (1024 * 1024), 2)
                exists = True
                break
        
        if not exists:
            config["library_items"].append({
                "name": filename,
                "size_mb": round(size_bytes / (1024 * 1024), 2),
                "created_at": int(time.time())
            })
        save_config()
    except Exception as e:
        print(f"Error adding to library config: {e}")

def restart_application():
    time.sleep(1.0)
    base_dir = get_base_dir()
    pid = os.getpid()
    
    # Check parent dir too, in case we are in an 'app' subdirectory
    launcher_dir = base_dir
    parent_dir = os.path.dirname(base_dir)
    if os.path.exists(os.path.join(parent_dir, "Elamir-Media-Hub.vbs")):
        launcher_dir = parent_dir
        launcher = "wscript.exe Elamir-Media-Hub.vbs"
    elif os.path.exists(os.path.join(parent_dir, "Elamir-Media-Hub.bat")):
        launcher_dir = parent_dir
        launcher = "Elamir-Media-Hub.bat"
    elif os.path.exists(os.path.join(base_dir, "Elamir-Media-Hub.vbs")):
        launcher = "wscript.exe Elamir-Media-Hub.vbs"
    elif os.path.exists(os.path.join(base_dir, "Elamir-Media-Hub.bat")):
        launcher = "Elamir-Media-Hub.bat"
    else:
        py_exe = sys.executable
        launcher = f'"{py_exe}" "{os.path.join(base_dir, "backend.py")}"'
        
    restart_script = os.path.join(launcher_dir, "restart.bat")
    bat_content = (
        "@echo off\n"
        "cd /d \"%~dp0\"\n"
        "timeout /t 1 /nobreak >nul\n"
        f"taskkill /f /pid {pid} >nul 2>&1\n"
        "timeout /t 1 /nobreak >nul\n"
        f"start \"\" {launcher}\n"
        "(goto) 2>nul & del \"%~f0\"\n"
    )
    
    try:
        with open(restart_script, 'w', encoding='utf-8') as f:
            f.write(bat_content)
        
        import subprocess
        subprocess.Popen(["cmd.exe", "/c", restart_script], 
                         creationflags=0x08000000 if sys.platform == 'win32' else 0,
                         close_fds=True)
    except Exception as e:
        print(f"Error creating restart script: {e}")

# Load configuration on startup
load_config()

# Helper for calling IPTV server API
def fetch_json_from_iptv(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Error fetching from IPTV: {e}")
        return None

def natural_sort_key(s):
    import re
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

def live_channel_sort_key(channel):
    import re
    name = channel.get('name', '')
    quality_pattern = re.compile(
        r'\b(4k|uhd|fhd|1080p|1080|hd|720p|720|sd|576p|480p|480|mini|low|hevc|h265|h\.265|h264|h\.264)\b',
        re.IGNORECASE
    )
    tags = quality_pattern.findall(name)
    tags_lower = [t.lower() for t in tags]
    
    base_name = quality_pattern.sub('', name)
    base_name = re.sub(r'\s+', ' ', base_name).strip(" -_")
    
    score = 2
    if any(t in tags_lower for t in ['4k', 'uhd']):
        score = 5
    elif any(t in tags_lower for t in ['fhd', '1080p', '1080']):
        score = 4
    elif any(t in tags_lower for t in ['hd', '720p', '720']):
        score = 3
    elif any(t in tags_lower for t in ['sd', '576p', '480p', '480']):
        score = 1
    elif any(t in tags_lower for t in ['mini', 'low']):
        score = 0
        
    return (natural_sort_key(base_name), -score, natural_sort_key(name))

def get_iptv_expiry_date():
    domain = config.get("domain", "")
    username = config.get("username", "")
    password = config.get("password", "")
    
    if not (domain and username and password):
        return None
    if domain == "http://m3u.local":
        return "Unlimited"
        
    url = f"{domain.rstrip('/')}/player_api.php?username={username}&password={password}"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=4) as response:
            data = json.loads(response.read().decode('utf-8'))
            user_info = data.get("user_info", {})
            exp_date = user_info.get("exp_date")
            if exp_date:
                if str(exp_date).lower() in ["null", "0", "none"] or not exp_date:
                    return "Unlimited"
                try:
                    ts = int(exp_date)
                    import datetime
                    dt = datetime.datetime.fromtimestamp(ts)
                    return dt.strftime("%Y-%m-%d")
                except ValueError:
                    return str(exp_date)
            else:
                return "Unlimited"
    except Exception as e:
        print(f"Error fetching expiry date: {e}")
    return None



# Clipboard helper
def copy_to_clipboard(text):
    try:
        import subprocess
        subprocess.run("clip", input=text, text=True, check=True, shell=True,
                       creationflags=0x08000000 if sys.platform == 'win32' else 0)
        return True
    except Exception:
        try:
            import ctypes
            cf_unicodetext = 13
            h_mem = ctypes.windll.kernel32.GlobalAlloc(2, (len(text) + 1) * 2)
            p_mem = ctypes.windll.kernel32.GlobalLock(h_mem)
            ctypes.cdll.msvcrt.wcscpy(ctypes.c_wchar_p(p_mem), text)
            ctypes.windll.kernel32.GlobalUnlock(h_mem)
            if ctypes.windll.user32.OpenClipboard(0):
                ctypes.windll.user32.EmptyClipboard()
                ctypes.windll.user32.SetClipboardData(cf_unicodetext, h_mem)
                ctypes.windll.user32.CloseClipboard()
                return True
        except:
            pass
    return False

# ── Persistent Local Cache Utilities ──
def get_cache_filepath(name):
    import hashlib
    # Unique cache per user credentials to avoid cross-pollution
    user_str = f"{config['domain']}_{config['username']}"
    h = hashlib.md5(user_str.encode('utf-8')).hexdigest()[:10]
    cache_dir = get_cache_dir()
    return os.path.join(cache_dir, f".cache_{name}_{h}.dat")

def save_to_persistent_cache(name, data):
    try:
        filepath = get_cache_filepath(name)
        cache_data = {
            "timestamp": int(time.time()),
            "data": data
        }
        json_str = json.dumps(cache_data)
        encoded = base64.b64encode(json_str.encode('utf-8'))
        with open(filepath, 'wb') as f:
            f.write(encoded)
    except Exception as e:
        print(f"Error saving cache {name}: {e}")

def load_from_persistent_cache(name, expiry_seconds=86400):
    try:
        filepath = get_cache_filepath(name)
        if not os.path.exists(filepath):
            return None
        with open(filepath, 'rb') as f:
            encoded = f.read()
        json_str = base64.b64decode(encoded).decode('utf-8')
        cache_data = json.loads(json_str)
        if time.time() - cache_data["timestamp"] < expiry_seconds:
            return cache_data["data"]
    except Exception as e:
        print(f"Error loading cache {name}: {e}")
    return None

def clear_persistent_caches():
    cache_dir = get_cache_dir()
    for file in os.listdir(cache_dir):
        if file.startswith(".cache_") and file.endswith(".dat"):
            try:
                os.remove(os.path.join(cache_dir, file))
            except:
                pass

# ── M3U Parser & Profiles Resolving Module ──
M3U_STREAMS_MAP = {}

def resolve_m3u_url(url):
    if not url:
        return ""
    if "m3u.local" in url:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        filename = os.path.basename(path)
        stream_id, _ = os.path.splitext(filename)
        real_url = M3U_STREAMS_MAP.get(stream_id)
        if real_url:
            return real_url
    return url

def build_stream_url(media_type, stream_id):
    """Build the authenticated provider stream URL server-side, so credentials
    never have to be sent to the browser (see /api/stream and /api/direct-url)."""
    if stream_id is None or stream_id == "":
        return ""
    domain = config.get("domain", "")
    username = config.get("username", "")
    password = config.get("password", "")
    if domain == "http://m3u.local":
        return M3U_STREAMS_MAP.get(stream_id) or M3U_STREAMS_MAP.get(str(stream_id), "")
    seg = "movie" if media_type == "movie" else "series" if media_type == "series" else "live"
    return f"{domain}/{seg}/{username}/{password}/{stream_id}.m3u8"

def parse_series_name(title):
    import re
    match = re.search(r'^(.*?)\s+S(\d+)\s*E(\d+)', title, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3)), f"S{match.group(2)}E{match.group(3)}"
    
    match = re.search(r'^(.*?)\s+(\d+)x(\d+)', title, re.IGNORECASE)
    if match:
        return match.group(1).strip(), int(match.group(2)), int(match.group(3)), f"S{match.group(2)}E{match.group(3)}"

    return title.strip(), 1, 1, "S01E01"

def parse_extinf_line(line):
    import re
    attribs = {}
    comma_idx = line.rfind(",")
    if comma_idx != -1:
        name = line[comma_idx+1:].strip()
        attribs["name"] = name
        attr_part = line[8:comma_idx].strip()
    else:
        attribs["name"] = ""
        attr_part = line[8:].strip()
        
    pattern = re.compile(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'=,]+))')
    for match in pattern.finditer(attr_part):
        key = match.group(1).lower()
        val = match.group(2) or match.group(3) or match.group(4) or ""
        if key in ["tvg-logo", "logo"]:
            attribs["logo"] = val
        elif key in ["group-title", "group"]:
            attribs["group"] = val
        elif key == "tvg-name":
            attribs["tvg_name"] = val
            
    if not attribs["name"] and attribs.get("tvg_name"):
        attribs["name"] = attribs["tvg_name"]
        
    return attribs

def group_series_from_m3u(series_episodes):
    grouped_series = {}
    episodes_map = {}
    import hashlib
    
    for ep in series_episodes:
        title = ep["name"]
        group = ep["category_id"]
        logo = ep["stream_icon"]
        
        series_name, season, episode_num, code = parse_series_name(title)
        series_id = "ser_" + hashlib.md5(f"{series_name}_{group}".encode('utf-8')).hexdigest()[:10]
        
        if series_id not in grouped_series:
            grouped_series[series_id] = {
                "series_id": series_id,
                "name": series_name,
                "stream_icon": logo,
                "category_id": group,
                "type": "series"
            }
            episodes_map[series_id] = {}
            
        if season not in episodes_map[series_id]:
            episodes_map[series_id][season] = []
            
        episodes_map[series_id][season].append({
            "id": ep["stream_id"],
            "title": title,
            "episode_num": episode_num,
            "container_extension": "mp4",
            "url": ep["url"],
            "stream_icon": logo
        })
        
    for ser_id in episodes_map:
        for seas in episodes_map[ser_id]:
            episodes_map[ser_id][seas] = sorted(episodes_map[ser_id][seas], key=lambda x: x["episode_num"])
            
    return grouped_series, episodes_map

def parse_m3u_content(content_or_filepath):
    import io
    import re
    categories = {"live": set(), "movie": set(), "series": set()}
    live_streams = []
    movies = []
    series_episodes = []

    if os.path.exists(content_or_filepath):
        f = open(content_or_filepath, 'r', encoding='utf-8', errors='ignore')
    else:
        f = io.StringIO(content_or_filepath)

    current_inf = None
    stream_counter = 0

    for line in f:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTM3U"):
            continue
        elif line.startswith("#EXTINF:"):
            current_inf = parse_extinf_line(line)
        elif line.startswith("#") and not line.startswith("#EXTINF"):
            continue
        else:
            if current_inf is not None:
                url = line
                name = current_inf.get("name", f"Channel {stream_counter}")
                logo = current_inf.get("logo", "")
                group = current_inf.get("group", "Uncategorized")
                
                group_lower = group.lower()
                url_lower = url.lower()
                
                is_movie_ext = any(ext in url_lower for ext in [".mp4", ".mkv", ".avi", ".divx", ".flv", ".mov", ".wmv"])
                is_series_pattern = any(p in name.lower() for p in ["s0", "s1", "s2", "season", "episode", "ep0", "ep1", "ep2"]) or ("x" in name.lower() and re.search(r'\d+x\d+', name.lower()))
                
                media_type = "live"
                if "movie" in group_lower or "vod" in group_lower or "cinema" in group_lower or "أفلام" in group_lower or "سينما" in group_lower or "فيلم" in group_lower or (is_movie_ext and "series" not in group_lower):
                    media_type = "movie"
                elif "series" in group_lower or "show" in group_lower or "tv shows" in group_lower or "tv series" in group_lower or "مسلسل" in group_lower or "مسلسلات" in group_lower or (is_movie_ext and "series" in group_lower) or is_series_pattern:
                    media_type = "series"
                
                stream_id = f"m3u_{stream_counter}"
                stream_counter += 1
                
                item = {
                    "stream_id": stream_id,
                    "name": name,
                    "stream_icon": logo,
                    "category_id": group,
                    "url": url,
                    "type": media_type
                }
                
                if media_type == "movie":
                    movies.append(item)
                    categories["movie"].add(group)
                elif media_type == "series":
                    series_episodes.append(item)
                    categories["series"].add(group)
                else:
                    categories["live"].add(group)
                    item["num"] = len(live_streams) + 1
                    live_streams.append(item)
                    
                current_inf = None

    f.close()
    
    grouped_series, episodes_map = group_series_from_m3u(series_episodes)
    
    return {
        "live": live_streams,
        "movies": movies,
        "series": list(grouped_series.values()),
        "episodes": episodes_map,
        "categories": {
            "live": [{"category_id": g, "category_name": g} for g in sorted(categories["live"])],
            "movie": [{"category_id": g, "category_name": g} for g in sorted(categories["movie"])],
            "series": [{"category_id": g, "category_name": g} for g in sorted(categories["series"])]
        }
    }

def rebuild_m3u_streams_map():
    global M3U_STREAMS_MAP
    M3U_STREAMS_MAP = {}
    try:
        live_data = load_from_persistent_cache("live")
        if live_data:
            for item in live_data:
                if "stream_id" in item and "url" in item:
                    M3U_STREAMS_MAP[item["stream_id"]] = item["url"]
        movies_data = load_from_persistent_cache("movies")
        if movies_data:
            for item in movies_data:
                if "stream_id" in item and "url" in item:
                    M3U_STREAMS_MAP[item["stream_id"]] = item["url"]
        series_data = load_from_persistent_cache("series")
        if series_data:
            for s in series_data:
                s_id = s.get("series_id")
                if s_id:
                    s_info = load_from_persistent_cache(f"series_info_{s_id}")
                    if s_info and "episodes" in s_info:
                        for season in s_info["episodes"].values():
                            for ep in season:
                                if "id" in ep and "url" in ep:
                                    M3U_STREAMS_MAP[ep["id"]] = ep["url"]
    except Exception as e:
        print(f"Error rebuilding M3U streams map: {e}")

def load_profile_data(force=False):
    profiles = config.get("profiles", [])
    active_id = config.get("active_profile_id")
    if not active_id:
        return
        
    active_prof = None
    for p in profiles:
        if p.get("id") == active_id:
            active_prof = p
            break
            
    if not active_prof:
        return
        
    ptype = active_prof.get("type")
    if ptype == "xtream":
        return
        
    # Check if cache exists
    movies_cache = load_from_persistent_cache("movies")
    if not force and movies_cache is not None:
        rebuild_m3u_streams_map()
        return
        
    print(f"[~] Parsing M3U content for profile: {active_prof.get('name')}...")
    source = ""
    if ptype == "m3u_url":
        url = active_prof.get("m3u_url")
        if url:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    source = resp.read().decode('utf-8', errors='ignore')
            except Exception as e:
                print(f"Error downloading M3U URL: {e}")
                return
    elif ptype == "m3u_file":
        filepath = active_prof.get("m3u_file_path")
        if filepath and os.path.exists(filepath):
            source = filepath
            
    if not source:
        return
        
    parsed = parse_m3u_content(source)
    if parsed:
        save_to_persistent_cache("live", parsed["live"])
        save_to_persistent_cache("movies", parsed["movies"])
        save_to_persistent_cache("series", parsed["series"])
        save_to_persistent_cache("live_cats", parsed["categories"]["live"])
        save_to_persistent_cache("vod_cats", parsed["categories"]["movie"])
        save_to_persistent_cache("series_cats", parsed["categories"]["series"])
        
        for ser_id, seasons_data in parsed["episodes"].items():
            save_to_persistent_cache(f"series_info_{ser_id}", {"episodes": seasons_data})
            
        rebuild_m3u_streams_map()
        print(f"[+] M3U Parsing complete. Cached channels, movies, and series.")

def load_cache_stale_check(name):
    try:
        filepath = get_cache_filepath(name)
        if not os.path.exists(filepath):
            return None, True
        with open(filepath, 'rb') as f:
            encoded = f.read()
        json_str = base64.b64decode(encoded).decode('utf-8')
        cache_data = json.loads(json_str)
        age = time.time() - cache_data["timestamp"]
        # If older than 10 minutes (600s), it is stale
        return cache_data["data"], (age > 600)
    except:
        return None, True

def revalidate_cache_in_background(name, action):
    def task():
        print(f"[~] Background revalidation started for {name}...")
        url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action={action}"
        fresh_data = fetch_json_from_iptv(url)
        if fresh_data:
            save_to_persistent_cache(name, fresh_data)
            global CACHED_MOVIES, CACHED_SERIES, CACHED_LIVE, CACHED_VOD_CATS, CACHED_SERIES_CATS, CACHED_LIVE_CATS
            if name == "movies": CACHED_MOVIES = fresh_data
            elif name == "series": CACHED_SERIES = fresh_data
            elif name == "live": CACHED_LIVE = fresh_data
            elif name == "vod_cats": CACHED_VOD_CATS = fresh_data
            elif name == "series_cats": CACHED_SERIES_CATS = fresh_data
            elif name == "live_cats": CACHED_LIVE_CATS = fresh_data
            print(f"[+] Background revalidation complete for {name}.")
    threading.Thread(target=task, daemon=True).start()

# HLS Segment merger and downloader thread with Resume support
class DownloadThread(threading.Thread):
    def __init__(self, dl_id, media_type, stream_id, filename, limit_bytes=None, subfolder=""):
        super().__init__()
        self.dl_id = dl_id
        self.media_type = media_type
        self.stream_id = stream_id
        self.filename = sanitize_filename(filename, f"download_{stream_id}")
        self.limit_bytes = limit_bytes
        self.subfolder = sanitize_subfolder(subfolder)
        self.cancel_requested = False

    def _supports_range(self, url):
        """True if the server honours HTTP Range (needed for multi-connection)."""
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Range': 'bytes=0-0'})
            with urllib.request.urlopen(req, timeout=10) as r:
                return (getattr(r, 'status', r.getcode()) == 206)
        except Exception:
            return False

    def _parallel_range_download(self, url, filepath, total_size, num_conns=8):
        """Download a single file over several connections at once. IPTV providers
        usually throttle each connection, so N connections ≈ N× the speed and fill
        the user's real bandwidth."""
        import concurrent.futures
        if total_size <= 0 or not self._supports_range(url):
            return False
        part = max(1, total_size // num_conns)
        ranges = []
        s = 0
        while s < total_size:
            e = min(s + part - 1, total_size - 1)
            ranges.append((s, e))
            s = e + 1
        wf = None
        try:
            wf = open(filepath, 'wb')
            wf.truncate(total_size)
            wf.close()
            wf = open(filepath, 'r+b')   # single shared handle; writes are seek+locked
            done = {'b': 0}
            lock = threading.Lock()
            start_time = time.time()

            def dl(s, e):
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Range': f'bytes={s}-{e}'})
                pos = s
                with urllib.request.urlopen(req, timeout=20) as r:
                    while True:
                        if self.cancel_requested:
                            raise Exception("Cancelled by user")
                        chunk = r.read(262144)
                        if not chunk:
                            break
                        with lock:
                            wf.seek(pos)
                            wf.write(chunk)
                            done['b'] += len(chunk)
                            total_done = done['b']
                        pos += len(chunk)
                        el = time.time() - start_time
                        with downloads_lock:
                            downloads_db[self.dl_id].update({
                                "progress": int(total_done * 100 / total_size),
                                "downloaded_mb": total_done // (1024 * 1024),
                                "total_size_mb": total_size // (1024 * 1024),
                                "speed_mb_s": (total_done / (1024 * 1024)) / el if el > 0 else 0
                            })

            with concurrent.futures.ThreadPoolExecutor(max_workers=num_conns) as ex:
                futs = [ex.submit(dl, a, b) for (a, b) in ranges]
                for fu in concurrent.futures.as_completed(futs):
                    fu.result()  # propagate errors / cancellation
            wf.close(); wf = None
            return True
        except Exception as e:
            try:
                if wf: wf.close()
            except Exception:
                pass
            if "Cancelled" in str(e):
                raise
            print(f"[parallel download] failed, falling back to single connection: {e}")
            return False

    def run(self):
        domain = config["domain"]
        username = config["username"]
        password = config["password"]
        save_dir = config["save_dir"]

        if domain == "http://m3u.local":
            url = M3U_STREAMS_MAP.get(self.stream_id, "")
        else:
            url_path = "movie" if self.media_type == "movie" else "series"
            url = f"{domain}/{url_path}/{username}/{password}/{self.stream_id}.m3u8"

        # Setup file path (append _sample for sample download); season/series
        # downloads go into their own subfolder.
        filename_ext = f"{self.filename}_sample.ts" if self.limit_bytes else f"{self.filename}.ts"
        target_dir = os.path.join(save_dir, *self.subfolder.split("/")) if self.subfolder else save_dir
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception:
            target_dir = save_dir
        filepath = os.path.join(target_dir, filename_ext)
        # Relative path stored in the library so it can be found/streamed later.
        self.rel_path = os.path.join(self.subfolder.replace("/", os.sep), filename_ext) if self.subfolder else filename_ext
        
        resume_path = filepath + ".resume"
        # We only support HLS resume for full downloads (not samples)
        has_hls_resume = os.path.exists(filepath) and os.path.exists(resume_path) and not self.limit_bytes
        
        current_size = 0
        is_partial = False
        
        # We only support direct resume for full downloads (not samples)
        if not has_hls_resume and not self.limit_bytes:
            current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            
        initial_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            
        headers = {'User-Agent': 'Mozilla/5.0'}
        if current_size > 0:
            headers['Range'] = f"bytes={current_size}-"
            
        with downloads_lock:
            downloads_db[self.dl_id] = {
                "id": self.dl_id,
                "filename": os.path.basename(filepath),
                "progress": 0,
                "downloaded_mb": initial_size // (1024 * 1024),
                "total_size_mb": 0,
                "speed_mb_s": 0,
                "status": "downloading",
                "media_type": self.media_type,
                "stream_id": self.stream_id,
                "limit_bytes": self.limit_bytes
            }
            
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                final_url = response.geturl()
                
                code = response.status if hasattr(response, 'status') else response.getcode()
                is_partial = (code == 206)
                
                if current_size > 0 and not is_partial:
                    print("[-] Server doesn't support resuming for direct download. Truncating file.")
                    current_size = 0
                    with downloads_lock:
                        downloads_db[self.dl_id]["downloaded_mb"] = 0
                        
                first_block = b""
                if not is_partial:
                    first_block = response.read(1024)
                    
                if (first_block.startswith(b'#EXTM3U') or has_hls_resume):
                    # ── HLS Segmented Download ──
                    # Always read the fresh manifest to get updated session tokens
                    playlist_data = first_block + response.read()
                    playlist_text = playlist_data.decode('utf-8', errors='ignore')
                    
                    segments = []
                    lines = playlist_text.splitlines()
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            segments.append(urllib.parse.urljoin(final_url, line))
                            
                    if not segments:
                        raise Exception("No segments found in the playlist.")
                        
                    next_index = 0
                    if has_hls_resume:
                        try:
                            with open(resume_path, 'r', encoding='utf-8') as rf:
                                resume_data = json.load(rf)
                                next_index = resume_data.get("next_index", 0)
                                print(f"[+] Loaded HLS resume next_index: {next_index}")
                        except Exception as ree:
                            print(f"[-] Error loading resume metadata: {ree}")
                            
                    # Only write/update resume file if NOT a sample download
                    if not self.limit_bytes:
                        resume_data = {
                            "segments": segments,
                            "next_index": next_index
                        }
                        try:
                            with open(resume_path, 'w', encoding='utf-8') as rf:
                                json.dump(resume_data, rf)
                        except:
                            pass
                                
                    total_segments = len(segments)
                    downloaded_bytes = os.path.getsize(filepath) if (next_index > 0 and os.path.exists(filepath)) else 0
                    start_time = time.time()
                    
                    import concurrent.futures

                    write_mode = 'ab' if next_index > 0 else 'wb'
                    MAX_WORKERS = 30
                    LOOK_AHEAD = 60

                    def download_segment_task(idx, url):
                        if self.cancel_requested:
                            raise Exception("Cancelled by user")
                        retries = 5
                        for attempt in range(retries):
                            if self.cancel_requested:
                                raise Exception("Cancelled by user")
                            try:
                                s_req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                                with urllib.request.urlopen(s_req, timeout=15) as s_res:
                                    if self.cancel_requested:
                                        raise Exception("Cancelled by user")
                                    return idx, s_res.read()
                            except Exception as e:
                                if "Cancelled by user" in str(e):
                                    raise e
                                if attempt == retries - 1:
                                    raise Exception(f"Segment {idx} download failed after {retries} attempts: {e}")
                                time.sleep(1.0 + attempt * 1.0)

                    completed_segments = {}
                    write_idx = next_index
                    sub_idx = next_index
                    orig_file_size = os.path.getsize(filepath) if (next_index > 0 and os.path.exists(filepath)) else 0
                    
                    limit_reached = False
                    with open(filepath, write_mode) as f:
                        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                            active_futures = {}
                            
                            while write_idx < total_segments:
                                if self.cancel_requested:
                                    raise Exception("Cancelled by user")
                                    
                                if limit_reached:
                                    break
                                    
                                # Submit tasks up to LOOK_AHEAD limit
                                while sub_idx < total_segments and (sub_idx - write_idx) < LOOK_AHEAD:
                                    if self.cancel_requested:
                                        break
                                    s_url = segments[sub_idx]
                                    future = executor.submit(download_segment_task, sub_idx, s_url)
                                    active_futures[future] = sub_idx
                                    sub_idx += 1
                                    
                                # Wait for at least one future to complete
                                if active_futures:
                                    done, _ = concurrent.futures.wait(
                                        active_futures.keys(),
                                        timeout=0.2,
                                        return_when=concurrent.futures.FIRST_COMPLETED
                                    )
                                    
                                    for future in done:
                                        f_idx = active_futures.pop(future)
                                        try:
                                            res_idx, s_data = future.result()
                                            completed_segments[res_idx] = s_data
                                        except Exception as e:
                                            # Cancel pending futures in the executor
                                            for active_fut in active_futures:
                                                active_fut.cancel()
                                            raise e
                                else:
                                    if write_idx < total_segments:
                                        time.sleep(0.1)
                                        
                                # Write completed segments in order
                                while write_idx in completed_segments:
                                    s_data = completed_segments.pop(write_idx)
                                    f.write(s_data)
                                    downloaded_bytes += len(s_data)
                                    write_idx += 1
                                    
                                    if not self.limit_bytes:
                                        try:
                                            resume_data["next_index"] = write_idx
                                            with open(resume_path, 'w', encoding='utf-8') as rf:
                                                json.dump(resume_data, rf)
                                        except:
                                            pass
                                            
                                    elapsed = time.time() - start_time
                                    speed = ((downloaded_bytes - orig_file_size) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                                    
                                    progress_percent = int(write_idx * 100 / total_segments)
                                    if self.limit_bytes:
                                        progress_percent = min(100, int(downloaded_bytes * 100 / self.limit_bytes))
                                        
                                    with downloads_lock:
                                        downloads_db[self.dl_id].update({
                                            "progress": progress_percent,
                                            "downloaded_mb": downloaded_bytes // (1024 * 1024),
                                            "speed_mb_s": speed
                                        })
                                        
                                    if self.limit_bytes and downloaded_bytes >= self.limit_bytes:
                                        for active_fut in active_futures:
                                            active_fut.cancel()
                                        limit_reached = True
                                        break
                                        
                    if not self.limit_bytes and os.path.exists(resume_path):
                        try: os.remove(resume_path)
                        except: pass

                        
                else:
                    # ── Direct Binary Download ──
                    content_length = int(response.info().get('Content-Length', 0))
                    total_size = content_length + current_size

                    # Multi-connection accelerator: for a fresh full download of a
                    # reasonably large file, split it across several connections to
                    # beat the provider's per-connection speed cap.
                    if (content_length > 8 * 1024 * 1024 and current_size == 0 and not self.limit_bytes
                            and self._parallel_range_download(final_url, filepath, content_length)):
                        downloaded_bytes = content_length
                    else:
                      downloaded_bytes = current_size if is_partial else len(first_block)
                      start_time = time.time()

                      effective_total = min(total_size, self.limit_bytes) if (self.limit_bytes and total_size > 0) else total_size

                      if effective_total > 0:
                        with downloads_lock:
                            downloads_db[self.dl_id]["total_size_mb"] = effective_total // (1024 * 1024)

                      write_mode = 'ab' if is_partial else 'wb'
                      with open(filepath, write_mode) as f:
                        if not is_partial:
                            f.write(first_block)
                            
                        block_size = 1024 * 256
                        while True:
                            if self.cancel_requested:
                                raise Exception("Cancelled by user")
                                
                            to_read = block_size
                            if self.limit_bytes:
                                to_read = min(block_size, self.limit_bytes - downloaded_bytes)
                                
                            buffer = response.read(to_read)
                            if not buffer:
                                break
                            f.write(buffer)
                            downloaded_bytes += len(buffer)
                            
                            elapsed = time.time() - start_time
                            speed = ((downloaded_bytes - current_size) / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                            
                            progress_percent = 0
                            if effective_total > 0:
                                progress_percent = int(downloaded_bytes * 100 / effective_total)
                                
                            with downloads_lock:
                                downloads_db[self.dl_id].update({
                                    "progress": progress_percent,
                                    "downloaded_mb": downloaded_bytes // (1024 * 1024),
                                    "speed_mb_s": speed
                                })
                                
                            if self.limit_bytes and downloaded_bytes >= self.limit_bytes:
                                break
                                
            # Success
            with downloads_lock:
                downloads_db[self.dl_id].update({
                    "progress": 100,
                    "status": "completed",
                    "speed_mb_s": 0
                })
            try:
                if os.path.exists(filepath):
                    add_to_library_config(self.rel_path, os.path.getsize(filepath))
            except Exception as le:
                print(f"Error adding completed download to library: {le}")
        except Exception as e:
            print(f"Download thread error: {e}")
            if self.limit_bytes and os.path.exists(filepath):
                try: os.remove(filepath)
                except: pass
            with downloads_lock:
                downloads_db[self.dl_id].update({
                    "status": "cancelled" if "Cancelled" in str(e) else "failed",
                    "speed_mb_s": 0
                })

# Shared PowerShell that creates a hidden, top-most owner window and forces it to
# the foreground, so the dialog it owns always opens ABOVE the browser instead of
# behind it (a background process otherwise can't steal focus on Windows).
_PS_DIALOG_PREFIX = (
    "Add-Type -AssemblyName System.Windows.Forms\n"
    "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
    "public class Fg{[DllImport(\"user32.dll\")]public static extern bool SetForegroundWindow(IntPtr h);}'\n"
    "$tp = New-Object System.Windows.Forms.Form\n"
    "$tp.TopMost = $true\n"
    "$tp.ShowInTaskbar = $false\n"
    "$tp.Width = 1\n"
    "$tp.Height = 1\n"
    "$tp.StartPosition = 'CenterScreen'\n"
    "$tp.Opacity = 0\n"
    "$tp.Show()\n"
    "$tp.Activate()\n"
    "[Fg]::SetForegroundWindow($tp.Handle) | Out-Null\n"
)

def _run_native_dialog(dialog_lines):
    try:
        import subprocess
        ps_code = _PS_DIALOG_PREFIX + dialog_lines + "$tp.Close()\n"
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-Command", ps_code]
        output = subprocess.check_output(
            cmd,
            text=True,
            creationflags=0x08000000 if sys.platform == 'win32' else 0
        ).strip()
        return output if output else None
    except Exception as e:
        print(f"Native dialog error: {e}")
        return None

# Native Windows folder selector using PowerShell Forms (avoids Tkinter extraction errors in PyInstaller)
def show_native_folder_picker():
    return _run_native_dialog(
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog\n"
        "$dialog.Description = 'Select Download Folder'\n"
        "$dialog.ShowNewFolderButton = $true\n"
        "$res = $dialog.ShowDialog($tp)\n"
        "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.SelectedPath }\n"
    )

def show_native_file_picker():
    return _run_native_dialog(
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog\n"
        "$dialog.Title = 'Select Video to Import'\n"
        "$dialog.Filter = 'Video Files (*.mp4;*.mkv;*.ts;*.avi;*.mov;*.m4v)|*.mp4;*.mkv;*.ts;*.avi;*.mov;*.m4v|All Files (*.*)|*.*'\n"
        "$res = $dialog.ShowDialog($tp)\n"
        "if ($res -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.FileName }\n"
    )

# ── Custom DNS (two modes) ──────────────────────────────────────────────
#  • System DNS  : changes the whole PC's resolver (needs Admin / UAC).
#  • App DNS     : resolves the provider's host via DoH using the chosen DNS,
#                  for this app's traffic only — no Admin, no system changes.
_DNS_PRESETS = {
    "cloudflare": ["1.1.1.1", "1.0.0.1"],
    "google":     ["8.8.8.8", "8.8.4.4"],
    "adguard":    ["94.140.14.14", "94.140.15.15"],
    "quad9":      ["9.9.9.9", "149.112.112.112"],
}
# DoH endpoints are IP-based so resolving them never needs DNS itself (no loop).
_DOH_ENDPOINTS = {
    "cloudflare": "https://1.1.1.1/dns-query",
    "google":     "https://8.8.8.8/resolve",
    "adguard":    "https://94.140.14.14/dns-query",
    "quad9":      "https://9.9.9.9:5053/dns-query",
}
_app_dns_provider = None   # None/"off" = disabled; otherwise a preset key
_doh_cache = {}
_orig_getaddrinfo = socket.getaddrinfo

def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

def _doh_resolve(host):
    endpoint = _DOH_ENDPOINTS.get(_app_dns_provider)
    if not endpoint:
        return None
    if host in _doh_cache:
        return _doh_cache[host]
    try:
        url = f"{endpoint}?name={urllib.parse.quote(host)}&type=A"
        req = urllib.request.Request(url, headers={"accept": "application/dns-json", "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        for ans in data.get("Answer", []):
            if ans.get("type") == 1 and ans.get("data"):
                _doh_cache[host] = ans["data"]
                return ans["data"]
    except Exception as e:
        print(f"[DoH] resolve failed for {host}: {e}")
    return None

def _patched_getaddrinfo(host, *args, **kwargs):
    # When App DNS is on, resolve real hostnames via DoH and connect to that IP.
    # The Host header / TLS SNI still use the original hostname, so https works.
    if _app_dns_provider and host and host != "localhost" and not _is_ip_literal(host):
        ip = _doh_resolve(host)
        if ip:
            try:
                return _orig_getaddrinfo(ip, *args, **kwargs)
            except Exception:
                pass
    return _orig_getaddrinfo(host, *args, **kwargs)

socket.getaddrinfo = _patched_getaddrinfo

def apply_app_dns_from_config():
    global _app_dns_provider
    prov = config.get("dns_app_provider", "off")
    _app_dns_provider = prov if prov in _DOH_ENDPOINTS else None

def set_app_dns(provider):
    global _app_dns_provider
    _doh_cache.clear()
    _safe_host_cache.clear()
    _app_dns_provider = provider if provider in _DOH_ENDPOINTS else None
    config["dns_app_provider"] = _app_dns_provider or "off"
    save_config()
    return {"status": "success", "provider": _app_dns_provider or "off"}

def set_system_dns(provider):
    """Change the whole system's DNS for the active adapter (requires Admin)."""
    import subprocess, tempfile
    if provider == "auto":
        action = "Set-DnsClientServerAddress -InterfaceIndex $ifc -ResetServerAddresses"
    else:
        servers = _DNS_PRESETS.get(provider)
        if not servers:
            return {"status": "error", "message": "Unknown DNS provider"}
        action = "Set-DnsClientServerAddress -InterfaceIndex $ifc -ServerAddresses " + ",".join(servers)
    script = (
        "$ErrorActionPreference='Stop'\n"
        "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1\n"
        "$ifc = $r.InterfaceIndex\n"
        + action + "\n"
        "Clear-DnsClientCache\n"
    )
    try:
        tmp = os.path.join(tempfile.gettempdir(), "emh_setdns.ps1")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        elevate = (
            "try { $p = Start-Process powershell -Verb RunAs -PassThru -Wait "
            "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','" + tmp + "'; "
            "exit $p.ExitCode } catch { Write-Error $_.Exception.Message; exit 5 }"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", elevate],
            capture_output=True, text=True, timeout=120,
            creationflags=0x08000000 if sys.platform == 'win32' else 0
        )
        if proc.returncode == 0:
            return {"status": "success", "provider": provider}
        msg = (proc.stderr or "").strip() or "Admin permission was declined."
        return {"status": "error", "message": msg[:300]}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def get_current_dns():
    import subprocess
    info = {"system": [], "systemProvider": "unknown", "app": (_app_dns_provider or "off")}
    try:
        script = (
            "$r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric | Select-Object -First 1\n"
            "(Get-DnsClientServerAddress -InterfaceIndex $r.InterfaceIndex -AddressFamily IPv4).ServerAddresses -join ','\n"
        )
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=15,
            creationflags=0x08000000 if sys.platform == 'win32' else 0
        )
        servers = [x.strip() for x in (proc.stdout or "").strip().split(",") if x.strip()]
        info["system"] = servers
        if not servers:
            info["systemProvider"] = "auto"
        else:
            info["systemProvider"] = "custom"
            for name, ips in _DNS_PRESETS.items():
                if servers[0] == ips[0]:
                    info["systemProvider"] = name
                    break
    except Exception:
        pass
    return info

# ── Upstream proxy / tunnel (route all provider traffic through a proxy so the
#    ISP only sees an encrypted connection — defeats DPI throttling, RST injection
#    and IP blocking of the IPTV provider) ─────────────────────────────────────
_proxy_url = ""

def apply_upstream_proxy():
    """Install a global opener that sends all provider requests through the proxy."""
    global _proxy_url
    _proxy_url = (config.get("proxy_url") or "").strip()
    if _proxy_url:
        handler = urllib.request.ProxyHandler({"http": _proxy_url, "https": _proxy_url})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()  # direct, no proxy
    urllib.request.install_opener(opener)

def set_upstream_proxy(url):
    config["proxy_url"] = (url or "").strip()
    save_config()
    apply_upstream_proxy()
    return {"status": "success", "proxy": config["proxy_url"]}

def test_upstream_proxy():
    """Check the current route and report the exit IP the provider would see."""
    try:
        req = urllib.request.Request("https://1.1.1.1/cdn-cgi/trace", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            txt = r.read().decode("utf-8", "ignore")
        ip = ""
        for line in txt.splitlines():
            if line.startswith("ip="):
                ip = line[3:].strip()
        return {"status": "success", "ip": ip, "via_proxy": bool(_proxy_url)}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}

# EPG helpers removed

# HTTP Request Handler for Local Server
class LocalAppAPIHandler(SimpleHTTPRequestHandler):
    
    # Disable log output in console for clean terminal
    def log_message(self, format, *args):
        pass

    def end_headers(self):
        if self.path.endswith(".html") or self.path == "/" or "/api/" in self.path:
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
        super().end_headers()

    def translate_path(self, path):
        path = path.split('?', 1)[0].split('#', 1)[0]
        path = os.path.normpath(urllib.parse.unquote(path))
        words = path.split(os.sep)
        words = filter(None, words)
        result = get_assets_dir()
        for word in words:
            if os.path.dirname(word) or word in (os.curdir, os.pardir):
                continue
            result = os.path.join(result, word)
        return result

    def _client_is_local(self):
        ip = self.client_address[0] if self.client_address else ""
        return ip in ("127.0.0.1", "::1", "localhost") or ip.startswith("127.")

    def _get_cookie(self, name):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _is_authorized(self):
        # The local machine always has access; the lock only guards network devices.
        if self._client_is_local():
            return True
        if not app_lock_enabled():
            return True
        tok = self._get_cookie("auth_token")
        return bool(tok and verify_auth_token(tok))

    def _auth_gate(self, path):
        """Return True if the request may proceed. Blocks protected /api routes for
        unauthorized network devices."""
        if not path.startswith("/api/"):
            return True
        if path in ("/api/login", "/api/auth-status"):
            return True
        if self._is_authorized():
            return True
        self.send_error(401, "Unauthorized")
        return False

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        query = urllib.parse.parse_qs(parsed_path.query)

        # Serve static files from assets directory
        root_dir = get_assets_dir()

        if not self._auth_gate(path):
            return

        # API endpoints
        if path == "/api/auth-status":
            self.send_json({
                "locked": app_lock_enabled(),
                "authorized": self._is_authorized(),
                "local": self._client_is_local()
            })

        elif path == "/api/creds":
            # Never expose passwords to the browser; the backend builds stream URLs.
            res_data = config.copy()
            res_data.pop("password", None)
            res_data["profiles"] = [
                {k: v for k, v in p.items() if k != "password"}
                for p in config.get("profiles", [])
            ]
            res_data["version"] = VERSION
            res_data["exp_date"] = get_iptv_expiry_date()
            self.send_json(res_data)

        elif path == "/api/live-bynum":
            # Resolve a live channel by its displayed sequential number (gnum = the
            # 1-based position in the full live list, which is what the card shows).
            chans = CACHED_LIVE
            if not chans:
                data, _ = load_cache_stale_check("live")
                chans = data or []
            try:
                n = int(query.get("num", [""])[0])
            except (ValueError, TypeError):
                n = 0
            ch = None
            if chans and 1 <= n <= len(chans):
                ch = dict(chans[n - 1])
                ch["gnum"] = n
            self.send_json({"channel": ch})

        elif path == "/api/dns":
            self.send_json(get_current_dns())

        elif path == "/api/upstream-proxy":
            self.send_json({"proxy": config.get("proxy_url", "")})

        elif path == "/api/direct-url":
            # Returns the authenticated provider URL for a stream. Used only when
            # the client genuinely needs the raw URL (e.g. casting to a device that
            # cannot reach the local proxy).
            media_type = query.get("type", [None])[0]
            stream_id = query.get("id", [None])[0]
            direct = build_stream_url(media_type, stream_id) if (media_type and stream_id) else ""
            self.send_json({"url": direct})


        elif path == "/api/movies":
            self.handle_get_movies(query)
            
        elif path == "/api/series":
            self.handle_get_series(query)
            
        elif path == "/api/series-info":
            self.handle_get_series_info(query)
            
        elif path == "/api/live":
            self.handle_get_live(query)
            
        elif path == "/api/playback-position":
            stream_id = query.get("id", [None])[0]
            if stream_id:
                pos_data = config.get("playback_positions", {}).get(stream_id, {})
                self.send_json(pos_data)
            else:
                pos_dict = config.get("playback_positions", {})
                sorted_pos = sorted(
                    [{"id": k, **v} for k, v in pos_dict.items()],
                    key=lambda x: x.get("timestamp", 0),
                    reverse=True
                )
                self.send_json({"items": sorted_pos})

        elif path == "/api/search":
            search_query = query.get("q", [""])[0].strip().lower()
            if not search_query:
                self.send_json({"live": [], "movies": [], "series": []})
            else:
                self.handle_global_search(search_query)

        elif path == "/api/check-stream":
            stream_id = query.get("id", [None])[0]
            if not stream_id:
                self.send_error(400, "Missing stream id")
            else:
                self.handle_check_stream(stream_id)

        elif path == "/api/vod-categories":
            self.handle_get_vod_cats()
            
        elif path == "/api/series-categories":
            self.handle_get_series_cats()

        elif path == "/api/live-categories":
            self.handle_get_live_cats()

        elif path == "/api/stream":
            self.handle_stream_proxy(query)

        elif path == "/api/proxy":
            self.handle_segment_proxy(query)
            
        elif path == "/api/select-dir":
            # Select folder natively
            folder = show_native_folder_picker()
            if folder:
                config["save_dir"] = folder
                save_config()
                self.send_json({"path": folder})
            else:
                self.send_json({"path": None})
                
        elif path == "/api/downloads":
            with downloads_lock:
                dls = list(downloads_db.values())
            self.send_json({"downloads": dls})
            
        elif path == "/api/library":
            self.handle_get_library()
            
        elif path == "/api/library/stream":
            self.handle_stream_library(query)
            
        elif path == "/api/library/raw":
            self.handle_raw_library(query)
            
        elif path == "/api/cancel-download":
            dl_id = query.get("id", [None])[0]
            if dl_id in downloads_db:
                # Signal thread to stop
                for t in threading.enumerate():
                    if hasattr(t, 'dl_id') and t.dl_id == dl_id:
                        t.cancel_requested = True
                        break
                self.send_json({"status": "cancelling"})
            else:
                self.send_error(404, "Download not found")
                
        elif path == "/api/clear-download":
            dl_id = query.get("id", [None])[0]
            with downloads_lock:
                if dl_id in downloads_db:
                    del downloads_db[dl_id]
            self.send_json({"status": "cleared"})
                
        elif path == "/api/favorites":
            self.send_json({"items": config.get("favorites", [])})
            
        elif path == "/api/recents":
            self.send_json({"items": config.get("recents", [])})
            
        elif path == "/api/profiles":
            self.send_json({
                "profiles": config.get("profiles", []),
                "active_profile_id": config.get("active_profile_id")
            })
                
        else:
            # Serve Static Files
            if path == "/" or not path:
                path = "/index.html"
                
            local_file = os.path.join(root_dir, path.lstrip('/'))
            if os.path.exists(local_file) and os.path.isfile(local_file):
                # Standard SimpleHTTPRequestHandler serve
                self.path = path
                return super().do_GET()
            else:
                # Fallback to index.html for Single Page Routing
                self.path = "/index.html"
                return super().do_GET()

    def do_POST(self):
        global CACHED_MOVIES, CACHED_SERIES, CACHED_SERIES_INFO, CACHED_LIVE, CACHED_VOD_CATS, CACHED_SERIES_CATS, CACHED_LIVE_CATS
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        if not self._auth_gate(path):
            return

        if path == "/api/login":
            try:
                data = json.loads(body)
                pw = data.get("password", "")
                if app_lock_enabled() and hash_password(pw) == config.get("app_password"):
                    token = make_auth_token()
                    content = json.dumps({"status": "success"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", len(content))
                    self.send_header("Set-Cookie", f"auth_token={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    self.send_error(401, "Invalid password")
            except Exception as e:
                self.send_error(400, f"Login error: {e}")

        elif path == "/api/app-password":
            # Reachable only by the local machine or an already-authorized device
            # (enforced by the auth gate above).
            try:
                data = json.loads(body)
                new_pw = data.get("password", "")
                if new_pw:
                    config["app_password"] = hash_password(new_pw)
                    config["app_secret"] = secrets.token_hex(32)  # invalidate existing sessions
                else:
                    config.pop("app_password", None)
                save_config()
                self.send_json({"status": "success", "locked": app_lock_enabled()})
            except Exception as e:
                self.send_error(400, f"Error: {e}")

        elif path == "/api/dns":
            try:
                data = json.loads(body)
                scope = data.get("scope", "app")
                provider = data.get("provider", "auto")
                if scope == "system":
                    self.send_json(set_system_dns(provider))     # whole PC (Admin)
                else:
                    self.send_json(set_app_dns(provider))        # streaming only (no Admin)
            except Exception as e:
                self.send_error(400, f"DNS error: {e}")

        elif path == "/api/upstream-proxy":
            try:
                data = json.loads(body)
                if data.get("action") == "test":
                    self.send_json(test_upstream_proxy())
                else:
                    self.send_json(set_upstream_proxy(data.get("url", "")))
            except Exception as e:
                self.send_error(400, f"Proxy error: {e}")

        elif path == "/api/creds":
            try:
                new_creds = json.loads(body)
                # Blank password means "keep the existing one" (the UI no longer
                # receives the password, so it can't echo it back).
                incoming_pw = new_creds.get("password", "")
                config.update({
                    "domain": new_creds.get("domain", config["domain"]),
                    "username": new_creds.get("username", config["username"]),
                    "password": incoming_pw if incoming_pw else config["password"],
                })
                save_config()
                # Invalidate cache on disk and memory
                clear_persistent_caches()
                CACHED_MOVIES = None
                CACHED_SERIES = None
                CACHED_SERIES_INFO = {}
                CACHED_LIVE = None
                CACHED_VOD_CATS = None
                CACHED_SERIES_CATS = None
                CACHED_LIVE_CATS = None
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Invalid body: {e}")

        elif path == "/api/playback-position":
            try:
                data = json.loads(body)
                stream_id = str(data.get("stream_id"))
                position = float(data.get("position", 0))
                duration = float(data.get("duration", 0))
                title = data.get("title", "")
                media_type = data.get("type", "movie")
                stream_icon = data.get("stream_icon", "")
                
                if "playback_positions" not in config:
                    config["playback_positions"] = {}
                
                if duration > 0 and (position / duration > 0.95 or (duration - position) < 15):
                    if stream_id in config["playback_positions"]:
                        del config["playback_positions"][stream_id]
                else:
                    config["playback_positions"][stream_id] = {
                        "position": position,
                        "duration": duration,
                        "title": title,
                        "type": media_type,
                        "stream_icon": stream_icon,
                        "timestamp": int(time.time())
                    }
                save_config()
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error saving playback position: {e}")

        elif path == "/api/playback-position/delete":
            try:
                data = json.loads(body)
                stream_id = str(data.get("stream_id"))
                if "playback_positions" in config and stream_id in config["playback_positions"]:
                    del config["playback_positions"][stream_id]
                    save_config()
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error: {e}")
                
        elif path == "/api/record":
            # Record a live stream until cancelled
            try:
                data       = json.loads(body)
                stream_id  = data.get("id")
                filename   = data.get("filename", f"recording_{stream_id}")
                clean_name = sanitize_filename(filename, f"recording_{stream_id}")
                dl_id      = f"rec_{int(time.time() * 1000)}"
                
                domain    = config["domain"]
                username  = config["username"]
                password  = config["password"]
                save_dir  = config["save_dir"]
                if domain == "http://m3u.local":
                    live_url = M3U_STREAMS_MAP.get(stream_id, "")
                else:
                    live_url  = f"{domain}/live/{username}/{password}/{stream_id}.ts"
                filepath  = os.path.join(save_dir, f"{clean_name}.ts")
                
                t = threading.Thread(daemon=True)
                t.dl_id            = dl_id
                t.cancel_requested = False
                
                with downloads_lock:
                    downloads_db[dl_id] = {
                        "id": dl_id, "filename": os.path.basename(filepath),
                        "progress": -1, "downloaded_mb": 0,
                        "total_size_mb": 0, "speed_mb_s": 0, "status": "downloading"
                    }
                
                def record_run():
                    import time as _t
                    downloaded_bytes = 0
                    start_time = _t.time()
                    downloaded_segments = set()
                    
                    try:
                        # Check first response
                        req = urllib.request.Request(live_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=15) as resp:
                            final_url = resp.geturl()
                            first_chunk = resp.read(65536)
                            
                        if first_chunk.startswith(b"#EXTM3U"):
                            # It is an HLS live stream
                            with open(filepath, 'wb') as f:
                                # Write the first chunk if it contained any TS segment data (unlikely for manifest, but good practice)
                                while True:
                                    if t.cancel_requested:
                                        raise Exception("Cancelled by user")
                                        
                                    # Fetch manifest
                                    m_req = urllib.request.Request(live_url, headers={"User-Agent": "Mozilla/5.0"})
                                    try:
                                        with urllib.request.urlopen(m_req, timeout=10) as m_resp:
                                            m_url = m_resp.geturl()
                                            m_content = m_resp.read().decode('utf-8', errors='ignore')
                                    except Exception as e:
                                        print(f"Error fetching live manifest: {e}")
                                        _t.sleep(2)
                                        continue
                                        
                                    lines = m_content.splitlines()
                                    segments = []
                                    for line in lines:
                                        line = line.strip()
                                        if line and not line.startswith('#'):
                                            seg_url = urllib.parse.urljoin(m_url, line)
                                            segments.append(seg_url)
                                            
                                    new_segments = [s for s in segments if s not in downloaded_segments]
                                    for s_url in new_segments:
                                        if t.cancel_requested:
                                            raise Exception("Cancelled by user")
                                        try:
                                            s_req = urllib.request.Request(s_url, headers={"User-Agent": "Mozilla/5.0"})
                                            with urllib.request.urlopen(s_req, timeout=10) as s_resp:
                                                data = s_resp.read()
                                                f.write(data)
                                                downloaded_bytes += len(data)
                                                downloaded_segments.add(s_url)
                                        except Exception as e:
                                            print(f"Error downloading segment {s_url}: {e}")
                                            
                                    elapsed = _t.time() - start_time
                                    speed = (downloaded_bytes / (1024*1024)) / elapsed if elapsed > 0 else 0
                                    with downloads_lock:
                                        downloads_db[t.dl_id].update({
                                            "downloaded_mb": downloaded_bytes // (1024*1024),
                                            "speed_mb_s": speed
                                        })
                                        
                                    # Sleep for 3 seconds before reloading manifest
                                    _t.sleep(3)
                        else:
                            # Direct TS stream
                            with open(filepath, 'wb') as f:
                                f.write(first_chunk)
                                downloaded_bytes += len(first_chunk)
                                # Continue reading from same stream
                                while True:
                                    if t.cancel_requested:
                                        raise Exception("Cancelled by user")
                                    # Read more from first connection if possible, or reconnect
                                    # Since first connection is already open, we should read it first
                                    # Let's write the direct streaming loop:
                                    req = urllib.request.Request(live_url, headers={"User-Agent": "Mozilla/5.0"})
                                    with urllib.request.urlopen(req, timeout=15) as resp:
                                        while True:
                                            if t.cancel_requested:
                                                raise Exception("Cancelled by user")
                                            chunk = resp.read(65536)
                                            if not chunk:
                                                break
                                            f.write(chunk)
                                            downloaded_bytes += len(chunk)
                                            elapsed = _t.time() - start_time
                                            speed = (downloaded_bytes / (1024*1024)) / elapsed if elapsed > 0 else 0
                                            with downloads_lock:
                                                downloads_db[t.dl_id].update({
                                                    "downloaded_mb": downloaded_bytes // (1024*1024),
                                                    "speed_mb_s": speed
                                                })
                                        break
                                            
                        with downloads_lock:
                            downloads_db[t.dl_id].update({"status": "completed", "speed_mb_s": 0})
                        try:
                            if os.path.exists(filepath):
                                add_to_library_config(os.path.basename(filepath), os.path.getsize(filepath))
                        except Exception as le:
                            print(f"Error adding completed recording to library: {le}")
                    except Exception as err:
                        print(f"Record exception: {err}")
                        was_stopped = "Cancelled" in str(err)
                        # Stopping a live recording is the normal way to finish it,
                        # so the partial file is a valid recording the user wants kept.
                        has_data = False
                        try:
                            has_data = os.path.exists(filepath) and os.path.getsize(filepath) > 0
                        except Exception:
                            pass
                        with downloads_lock:
                            downloads_db[t.dl_id].update({
                                "status": "completed" if (was_stopped and has_data) else "cancelled" if was_stopped else "failed",
                                "speed_mb_s": 0
                            })
                        if was_stopped and has_data:
                            try:
                                add_to_library_config(os.path.basename(filepath), os.path.getsize(filepath))
                            except Exception as le:
                                print(f"Error adding stopped recording to library: {le}")
                
                t.run = record_run
                t.start()
                self.send_json({"status": "recording", "id": dl_id})
            except Exception as e:
                self.send_error(400, f"Record error: {e}")

        elif path == "/api/download":
            try:
                data = json.loads(body)
                media_type = data.get("type")
                stream_id = data.get("id")
                filename = data.get("filename")
                limit_bytes = data.get("limit_bytes") # optional
                
                # Start background thread
                dl_id = f"dl_{int(time.time() * 1000)}"
                t = DownloadThread(dl_id, media_type, stream_id, filename, limit_bytes)
                t.daemon = True
                t.start()
                self.send_json({"status": "started", "id": dl_id})
            except Exception as e:
                self.send_error(400, f"Error starting download: {e}")

        elif path == "/api/download-series":
            # Queue every episode and download them one-by-one (sequentially) so we
            # don't hammer the provider with dozens of parallel connections.
            try:
                data = json.loads(body)
                episodes = data.get("episodes", [])
                series_name = sanitize_filename(data.get("series_name", "Series"), "Series")
                if not episodes:
                    self.send_error(400, "No episodes provided")
                    return
                group_id = f"ser_{int(time.time() * 1000)}"
                total = len(episodes)
                t = threading.Thread(daemon=True)
                t.dl_id = group_id
                t.cancel_requested = False

                def run_series():
                    for i, ep in enumerate(episodes):
                        if t.cancel_requested:
                            break
                        with downloads_lock:
                            downloads_db[group_id] = {
                                "id": group_id,
                                "filename": f"{series_name} — episode {i+1}/{total}",
                                "progress": -1, "downloaded_mb": 0, "total_size_mb": 0,
                                "speed_mb_s": 0, "status": "downloading"
                            }
                        ep_thread = DownloadThread(f"{group_id}_{i}", "series",
                                                   ep.get("id"),
                                                   ep.get("filename") or f"{series_name}_{i+1}",
                                                   subfolder=ep.get("subfolder", ""))
                        ep_thread.start()
                        while ep_thread.is_alive():
                            if t.cancel_requested:
                                ep_thread.cancel_requested = True
                            ep_thread.join(timeout=0.5)
                    with downloads_lock:
                        if group_id in downloads_db:
                            downloads_db[group_id].update({
                                "status": "cancelled" if t.cancel_requested else "completed",
                                "filename": f"{series_name} — {'cancelled' if t.cancel_requested else 'all episodes done'} ({total})",
                                "speed_mb_s": 0
                            })

                t.run = run_series
                t.start()
                self.send_json({"status": "started", "id": group_id})
            except Exception as e:
                self.send_error(400, f"Series download error: {e}")


        elif path == "/api/copy":
            try:
                data = json.loads(body)
                text = data.get("text", "")
                if copy_to_clipboard(text):
                    self.send_json({"status": "success"})
                else:
                    self.send_error(500, "Clipboard write failed")
            except Exception as e:
                self.send_error(400, f"Error: {e}")
                
        elif path == "/api/favorites":
            try:
                data = json.loads(body)
                action = data.get("action", "add")
                item_id = str(data.get("id"))
                item_type = data.get("type")
                
                favorites = config.get("favorites", [])
                
                if action == "remove":
                    favorites = [f for f in favorites if not (str(f.get("id")) == item_id and f.get("type") == item_type)]
                else:
                    # check if already exists
                    exists = any(str(f.get("id")) == item_id and f.get("type") == item_type for f in favorites)
                    if not exists:
                        new_item = {
                            "id": item_id,
                            "type": item_type,
                            "name": data.get("name"),
                            "stream_icon": data.get("stream_icon"),
                            "category_id": data.get("category_id")
                        }
                        favorites.append(new_item)
                
                config["favorites"] = favorites
                save_config()
                self.send_json({"status": "success", "favorites": favorites})
            except Exception as e:
                self.send_error(400, f"Error saving favorite: {e}")
                
        elif path == "/api/recents":
            try:
                data = json.loads(body)
                item_id = str(data.get("id"))
                item_type = data.get("type")
                
                recents = config.get("recents", [])
                
                # Remove duplicate of the same item if it exists
                recents = [r for r in recents if not (str(r.get("id")) == item_id and r.get("type") == item_type)]
                
                # Add to the beginning of the list
                new_item = {
                    "id": item_id,
                    "type": item_type,
                    "name": data.get("name"),
                    "stream_icon": data.get("stream_icon"),
                    "category_id": data.get("category_id"),
                    "timestamp": int(time.time())
                }
                # Series: remember which series + the last episode watched so we can
                # reopen the right show and resume where the user left off.
                for opt in ("series_id", "episode_id", "ep_label", "season"):
                    if data.get(opt) is not None:
                        new_item[opt] = data.get(opt)
                recents.insert(0, new_item)
                
                # Limit to 50 items
                recents = recents[:50]
                
                config["recents"] = recents
                save_config()
                self.send_json({"status": "success", "recents": recents})
            except Exception as e:
                self.send_error(400, f"Error saving recent: {e}")
        elif path == "/api/recents/delete":
            try:
                data = json.loads(body)
                item_id = str(data.get("id"))
                item_type = data.get("type")
                
                recents = config.get("recents", [])
                recents = [r for r in recents if not (str(r.get("id")) == item_id and r.get("type") == item_type)]
                
                config["recents"] = recents
                save_config()
                self.send_json({"status": "success", "recents": recents})
            except Exception as e:
                self.send_error(400, f"Error deleting recent: {e}")
        elif path == "/api/recents/clear":
            try:
                config["recents"] = []
                save_config()
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error clearing recents: {e}")
        elif path == "/api/library/delete":
            try:
                data = json.loads(body)
                file_name = data.get("file")
                if not file_name:
                    self.send_error(400, "Missing file parameter")
                    return
                
                save_dir = config.get("save_dir", DEFAULT_SAVE_DIR)
                file_path = os.path.normpath(os.path.join(save_dir, file_name))

                abs_save_dir = os.path.abspath(save_dir)
                abs_file_path = os.path.abspath(file_path)
                if os.path.commonpath([abs_save_dir, abs_file_path]) != abs_save_dir:
                    self.send_error(403, "Access denied")
                    return

                # Always remove from config library_items to keep config/UI in sync
                load_config()
                config["library_items"] = [item for item in config.get("library_items", []) if item.get("name") != file_name]
                save_config()
                
                if os.path.exists(file_path) and os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as re_err:
                        print(f"Error removing physical file: {re_err}")
                
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error deleting file: {e}")
                
        elif path == "/api/library/clear":
            try:
                data = json.loads(body)
                file_name = data.get("file")
                if not file_name:
                    self.send_error(400, "Missing file parameter")
                    return
                
                # Only remove from config library_items, leaving physical file intact
                load_config()
                config["library_items"] = [item for item in config.get("library_items", []) if item.get("name") != file_name]
                save_config()
                
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error clearing file from list: {e}")
                
        elif path == "/api/library/import":
            try:
                file_path = show_native_file_picker()
                if file_path and os.path.exists(file_path):
                    filename = os.path.basename(file_path)
                    save_dir = config.get("save_dir", DEFAULT_SAVE_DIR)
                    if not os.path.exists(save_dir):
                        os.makedirs(save_dir, exist_ok=True)
                    
                    dest_path = os.path.join(save_dir, filename)
                    if os.path.exists(dest_path):
                        name_part, ext_part = os.path.splitext(filename)
                        filename = f"{name_part}_{int(time.time())}{ext_part}"
                        dest_path = os.path.join(save_dir, filename)
                    
                    # Add to library config immediately with its size
                    file_size = os.path.getsize(file_path)
                    add_to_library_config(filename, file_size)
                    
                    # Copy in a background thread
                    import shutil
                    def copy_task():
                        try:
                            shutil.copy2(file_path, dest_path)
                        except Exception as e:
                            print(f"Error copying imported file: {e}")
                            # Remove from config if copy failed
                            try:
                                load_config()
                                config["library_items"] = [item for item in config.get("library_items", []) if item.get("name") != filename]
                                save_config()
                            except:
                                pass
                                
                    threading.Thread(target=copy_task, daemon=True).start()
                    self.send_json({"status": "success", "filename": filename})
                else:
                    self.send_json({"status": "cancelled"})
            except Exception as e:
                self.send_error(500, f"Import error: {e}")
        elif path == "/api/profiles/add":
            try:
                data = json.loads(body)
                prof_name = data.get("name")
                prof_type = data.get("type", "xtream")
                
                if not prof_name:
                    self.send_error(400, "Missing profile name")
                    return
                
                prof_id = f"prof_{int(time.time() * 1000)}"
                new_profile = {
                    "id": prof_id,
                    "name": prof_name,
                    "type": prof_type
                }
                
                if prof_type == "xtream":
                    new_profile["domain"] = data.get("domain", "")
                    new_profile["username"] = data.get("username", "")
                    new_profile["password"] = data.get("password", "")
                elif prof_type == "m3u_url":
                    new_profile["m3u_url"] = data.get("m3u_url", "")
                    
                if "profiles" not in config:
                    config["profiles"] = []
                config["profiles"].append(new_profile)
                save_config()
                self.send_json({"status": "success", "profile": new_profile})
            except Exception as e:
                self.send_error(400, f"Error adding profile: {e}")

        elif path == "/api/profiles/select":
            try:
                data = json.loads(body)
                prof_id = data.get("id")
                
                profiles = config.get("profiles", [])
                matched = any(p.get("id") == prof_id for p in profiles)
                if prof_id and not matched:
                    self.send_error(404, "Profile not found")
                    return
                
                config["active_profile_id"] = prof_id
                apply_active_profile_creds()
                save_config()
                
                CACHED_MOVIES = None
                CACHED_SERIES = None
                CACHED_SERIES_INFO = {}
                CACHED_LIVE = None
                CACHED_VOD_CATS = None
                CACHED_SERIES_CATS = None
                CACHED_LIVE_CATS = None
                
                if prof_id:
                    load_profile_data(force=True)
                
                res_data = config.copy()
                res_data["version"] = VERSION
                res_data["exp_date"] = get_iptv_expiry_date()
                self.send_json({"status": "success", "creds": res_data})
            except Exception as e:
                self.send_error(400, f"Error selecting profile: {e}")

        elif path == "/api/profiles/delete":
            try:
                data = json.loads(body)
                prof_id = data.get("id")
                if not prof_id:
                    self.send_error(400, "Missing profile id")
                    return
                
                profiles = config.get("profiles", [])
                target_p = None
                for p in profiles:
                    if p.get("id") == prof_id:
                        target_p = p
                        break
                
                if not target_p:
                    self.send_error(404, "Profile not found")
                    return
                
                if target_p.get("type") == "m3u_file":
                    filepath = target_p.get("m3u_file_path")
                    if filepath and os.path.exists(filepath):
                        try:
                            os.remove(filepath)
                        except Exception as fe:
                            print(f"Error deleting M3U file: {fe}")
                
                config["profiles"] = [p for p in profiles if p.get("id") != prof_id]
                
                if config.get("active_profile_id") == prof_id:
                    config["active_profile_id"] = None
                    config["domain"] = "http://fd.otbnver.club"
                    config["username"] = ""
                    config["password"] = ""
                    
                save_config()
                
                CACHED_MOVIES = None
                CACHED_SERIES = None
                CACHED_SERIES_INFO = {}
                CACHED_LIVE = None
                CACHED_VOD_CATS = None
                CACHED_SERIES_CATS = None
                CACHED_LIVE_CATS = None
                
                self.send_json({"status": "success"})
            except Exception as e:
                self.send_error(400, f"Error deleting profile: {e}")

        elif path == "/api/profiles/upload-m3u":
            try:
                ctype = self.headers.get('Content-Type')
                if not ctype or 'multipart/form-data' not in ctype:
                    self.send_error(400, "Content-Type must be multipart/form-data")
                    return
                
                parts = ctype.split('boundary=')
                if len(parts) < 2:
                    self.send_error(400, "Missing boundary in Content-Type")
                    return
                boundary = ('--' + parts[1]).encode('utf-8')
                
                content_length = int(self.headers.get('Content-Length', 0))
                raw_body = self.rfile.read(content_length)
                
                parts = raw_body.split(boundary)
                profile_name = "Uploaded Profile"
                file_content = b""
                
                for part in parts:
                    if not part or part.strip() == b"--":
                        continue
                    if b"\r\n\r\n" in part:
                        headers, part_body = part.split(b"\r\n\r\n", 1)
                    elif b"\n\n" in part:
                        headers, part_body = part.split(b"\n\n", 1)
                    else:
                        continue
                        
                    headers_str = headers.decode('utf-8', errors='ignore')
                    if part_body.endswith(b"\r\n"):
                        part_body = part_body[:-2]
                    elif part_body.endswith(b"\n"):
                        part_body = part_body[:-1]
                        
                    if 'name="name"' in headers_str:
                        profile_name = part_body.decode('utf-8', errors='ignore').strip()
                    elif 'name="file"' in headers_str:
                        file_content = part_body
                
                if not file_content:
                    self.send_error(400, "No file uploaded")
                    return
                
                uploads_dir = os.path.join(get_cache_dir(), "uploads")
                os.makedirs(uploads_dir, exist_ok=True)
                
                prof_id = f"prof_{int(time.time() * 1000)}"
                filename = f"{prof_id}.m3u"
                filepath = os.path.join(uploads_dir, filename)
                
                with open(filepath, "wb") as f_out:
                    f_out.write(file_content)
                
                new_profile = {
                    "id": prof_id,
                    "name": profile_name,
                    "type": "m3u_file",
                    "m3u_file_path": filepath
                }
                
                if "profiles" not in config:
                    config["profiles"] = []
                config["profiles"].append(new_profile)
                save_config()
                self.send_json({"status": "success", "profile": new_profile})
            except Exception as e:
                self.send_error(500, f"Error processing upload: {e}")

        elif path == "/api/update":
            try:
                import py_compile
                
                # Fetch files from GitHub (check main branch first, fallback to master if needed)
                base_url = "https://raw.githubusercontent.com/moalamir52/Elamir-Media-Hub"
                
                app_dir = os.path.dirname(os.path.abspath(__file__))
                backend_local = os.path.join(app_dir, "backend.py")
                index_local = os.path.join(app_dir, "index.html")
                
                backend_tmp = backend_local + ".tmp"
                index_tmp = index_local + ".tmp"
                
                # Download function with branch fallback
                def download_file(file_name, local_tmp):
                    headers = {'User-Agent': 'Mozilla/5.0'}
                    url = f"{base_url}/main/{file_name}"
                    req = urllib.request.Request(url, headers=headers)
                    try:
                        with urllib.request.urlopen(req, timeout=10) as response:
                            with open(local_tmp, 'wb') as f:
                                f.write(response.read())
                        return True
                    except Exception as e_main:
                        url_master = f"{base_url}/master/{file_name}"
                        req_master = urllib.request.Request(url_master, headers=headers)
                        try:
                            with urllib.request.urlopen(req_master, timeout=10) as response_master:
                                with open(local_tmp, 'wb') as f:
                                    f.write(response_master.read())
                            return True
                        except Exception as e_master:
                            raise Exception(f"Failed to download {file_name} from main ({e_main}) and master ({e_master})")
                
                # Download backend.py and index.html
                download_file("backend.py", backend_tmp)
                download_file("index.html", index_tmp)
                
                # Verify backend.py syntax
                try:
                    py_compile.compile(backend_tmp, doraise=True)
                except Exception as syntax_error:
                    if os.path.exists(backend_tmp): os.remove(backend_tmp)
                    if os.path.exists(index_tmp): os.remove(index_tmp)
                    self.send_json({"status": "error", "message": f"Downloaded code is corrupt (Syntax Error): {syntax_error}"})
                    return
                
                # Check version of downloaded code
                remote_version = None
                try:
                    with open(backend_tmp, 'r', encoding='utf-8') as f:
                        content = f.read()
                        import re
                        match = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', content)
                        if match:
                            remote_version = match.group(1)
                except Exception as ver_err:
                    print(f"Error parsing remote version: {ver_err}")
                
                if remote_version and remote_version == VERSION:
                    if os.path.exists(backend_tmp): os.remove(backend_tmp)
                    if os.path.exists(index_tmp): os.remove(index_tmp)
                    self.send_json({
                        "status": "up_to_date", 
                        "message": f"Your application is already up to date (v{VERSION})."
                    })
                    return
                
                # Swap files (use overwrite logic)
                if os.path.exists(backend_local):
                    try: os.remove(backend_local)
                    except: pass
                os.rename(backend_tmp, backend_local)
                
                if os.path.exists(index_local):
                    try: os.remove(index_local)
                    except: pass
                os.rename(index_tmp, index_local)
                
                self.send_json({
                    "status": "success", 
                    "message": "Update downloaded successfully! The application will restart automatically in a few seconds."
                })
                threading.Thread(target=restart_application, daemon=True).start()
            except Exception as e:
                try:
                    if os.path.exists(backend_tmp): os.remove(backend_tmp)
                    if os.path.exists(index_tmp): os.remove(index_tmp)
                except:
                    pass
                self.send_json({"status": "error", "message": f"Update failed: {e}"})
                

        else:
            self.send_error(404, "API endpoint not found")

    # API Logic - Global search across categories
    def handle_global_search(self, q):
        global CACHED_LIVE, CACHED_MOVIES, CACHED_SERIES
        if not CACHED_LIVE:
            data, _ = load_cache_stale_check("live")
            CACHED_LIVE = data or []
        if not CACHED_MOVIES:
            data, _ = load_cache_stale_check("movies")
            CACHED_MOVIES = data or []
        if not CACHED_SERIES:
            data, _ = load_cache_stale_check("series")
            CACHED_SERIES = data or []

        live_matches = []
        movie_matches = []
        series_matches = []
        limit = 50

        if CACHED_LIVE:
            for item in CACHED_LIVE:
                if q in item.get("name", "").lower():
                    live_matches.append(item)
                    if len(live_matches) >= limit:
                        break
        if CACHED_MOVIES:
            for item in CACHED_MOVIES:
                if q in item.get("name", "").lower():
                    movie_matches.append(item)
                    if len(movie_matches) >= limit:
                        break
        if CACHED_SERIES:
            for item in CACHED_SERIES:
                if q in item.get("name", "").lower():
                    series_matches.append(item)
                    if len(series_matches) >= limit:
                        break

        self.send_json({
            "live": live_matches,
            "movies": movie_matches,
            "series": series_matches
        })

    # API Logic - Live Stream Status Checker
    def handle_check_stream(self, stream_id):
        domain = config.get("domain", "")
        username = config.get("username", "")
        password = config.get("password", "")
        if not (domain and username and password):
            self.send_json({"online": False})
            return
        if domain == "http://m3u.local":
            stream_url = M3U_STREAMS_MAP.get(stream_id, "")
        else:
            stream_url = f"{domain.rstrip('/')}/live/{username}/{password}/{stream_id}.ts"
        req = urllib.request.Request(stream_url, method='GET', headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req, timeout=1.8) as response:
                code = response.getcode()
                self.send_json({"online": code == 200})
        except Exception as e:
            print(f"Stream check error for ID {stream_id}: {e}")
            self.send_json({"online": False})

    # API Logic - Movies list fetch, cache and search
    def handle_get_movies(self, query):
        global CACHED_MOVIES
        if not CACHED_MOVIES:
            data, is_stale = load_cache_stale_check("movies")
            if data:
                CACHED_MOVIES = data
                if is_stale:
                    revalidate_cache_in_background("movies", "get_vod_streams")
            else:
                print("[~] Fetching movies list from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_vod_streams"
                CACHED_MOVIES = fetch_json_from_iptv(url)
                if CACHED_MOVIES:
                    save_to_persistent_cache("movies", CACHED_MOVIES)
            
        if not CACHED_MOVIES:
            self.send_json({"movies": [], "total": 0, "page": 1, "pages": 1})
            return
            
        search    = query.get("query",       [""])[0].strip().lower()
        page      = int(query.get("page",     [1])[0])
        limit     = int(query.get("limit",    [24])[0])
        cat_id    = query.get("category_id",  [""])[0].strip()
        sort_by   = query.get("sort_by",      [""])[0].strip()
        
        # Filter movies
        filtered = CACHED_MOVIES
        if search:
            filtered = [m for m in filtered if search in m.get('name', '').lower()]
        if cat_id:
            filtered = [m for m in filtered if str(m.get('category_id', '')) == cat_id]
            
        # Sort movies
        if sort_by == "a-z":
            filtered = sorted(filtered, key=lambda x: x.get("name", "").lower())
        elif sort_by == "z-a":
            filtered = sorted(filtered, key=lambda x: x.get("name", "").lower(), reverse=True)
        elif sort_by == "rating":
            filtered = sorted(filtered, key=lambda x: float(x.get("rating", 0) or 0), reverse=True)
        elif sort_by == "recent":
            filtered = sorted(filtered, key=lambda x: int(x.get("added", 0) or 0), reverse=True)
            
        # Paginate
        total = len(filtered)
        pages = max(1, (total + limit - 1) // limit)
        start_idx = (page - 1) * limit
        
        self.send_json({
            "movies": filtered[start_idx:start_idx+limit],
            "total": total, "page": page, "pages": pages
        })

    # API Logic - Series list fetch, cache and search
    def handle_get_series(self, query):
        global CACHED_SERIES
        if not CACHED_SERIES:
            data, is_stale = load_cache_stale_check("series")
            if data:
                CACHED_SERIES = data
                if is_stale:
                    revalidate_cache_in_background("series", "get_series")
            else:
                print("[~] Fetching series list from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_series"
                CACHED_SERIES = fetch_json_from_iptv(url)
                if CACHED_SERIES:
                    save_to_persistent_cache("series", CACHED_SERIES)
            
        if not CACHED_SERIES:
            self.send_json({"series": [], "total": 0, "page": 1, "pages": 1})
            return
            
        search    = query.get("query",      [""])[0].strip().lower()
        page      = int(query.get("page",    [1])[0])
        limit     = int(query.get("limit",   [24])[0])
        cat_id    = query.get("category_id", [""])[0].strip()
        sort_by   = query.get("sort_by",     [""])[0].strip()
        
        # Filter series
        filtered = CACHED_SERIES
        if search:
            filtered = [s for s in filtered if search in s.get('name', '').lower()]
        if cat_id:
            filtered = [s for s in filtered if str(s.get('category_id', '')) == cat_id]
            
        # Sort series
        if sort_by == "a-z":
            filtered = sorted(filtered, key=lambda x: x.get("name", "").lower())
        elif sort_by == "z-a":
            filtered = sorted(filtered, key=lambda x: x.get("name", "").lower(), reverse=True)
        elif sort_by == "rating":
            filtered = sorted(filtered, key=lambda x: float(x.get("rating", 0) or 0), reverse=True)
        elif sort_by == "recent":
            filtered = sorted(filtered, key=lambda x: int(x.get("last_modified", 0) or 0), reverse=True)
            
        # Paginate
        total = len(filtered)
        pages = max(1, (total + limit - 1) // limit)
        start_idx = (page - 1) * limit
        
        self.send_json({
            "series": filtered[start_idx:start_idx+limit],
            "total": total, "page": page, "pages": pages
        })

    # API Logic - Series Info details fetch (Seasons / Episodes)
    def handle_get_series_info(self, query):
        series_id = query.get("id", [None])[0]
        if not series_id:
            self.send_error(400, "Missing series id")
            return
            
        global CACHED_SERIES_INFO
        if series_id not in CACHED_SERIES_INFO:
            cache_name = f"series_info_{series_id}"
            cached_data = load_from_persistent_cache(cache_name)
            if cached_data:
                CACHED_SERIES_INFO[series_id] = cached_data
            else:
                print(f"[~] Fetching series info for ID {series_id}...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_series_info&series_id={series_id}"
                details = fetch_json_from_iptv(url)
                if details:
                    CACHED_SERIES_INFO[series_id] = details
                    save_to_persistent_cache(cache_name, details)
                
        info = CACHED_SERIES_INFO.get(series_id, {"episodes": {}})
        self.send_json(info)

    # API Logic - Live channels
    def handle_get_live(self, query):
        global CACHED_LIVE
        if not CACHED_LIVE:
            data, is_stale = load_cache_stale_check("live")
            if data:
                CACHED_LIVE = data
                if is_stale:
                    revalidate_cache_in_background("live", "get_live_streams")
            else:
                print("[~] Fetching live channels from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_live_streams"
                CACHED_LIVE = fetch_json_from_iptv(url) or []
                if CACHED_LIVE:
                    save_to_persistent_cache("live", CACHED_LIVE)
        
        # Assign a stable, unique sequential number (the provider's "num" repeats
        # across categories, so it can't be used for channel-number entry).
        for i, c in enumerate(CACHED_LIVE):
            c["gnum"] = i + 1

        search = query.get("query", [""])[0].strip().lower()
        page = int(query.get("page", [1])[0])
        limit = int(query.get("limit", [24])[0])
        cat_id = query.get("category_id", [""])[0].strip()

        filtered = CACHED_LIVE
        if search:
            filtered = [c for c in filtered if search in c.get('name', '').lower()]
        if cat_id:
            filtered = [c for c in filtered if str(c.get('category_id', '')) == cat_id]
            
        if cat_id or search:
            filtered = sorted(filtered, key=live_channel_sort_key)
        
        total = len(filtered)
        pages = max(1, (total + limit - 1) // limit)
        start = (page - 1) * limit
        self.send_json({"channels": filtered[start:start+limit], "total": total, "page": page, "pages": pages})

    def handle_get_vod_cats(self):
        global CACHED_VOD_CATS
        if not CACHED_VOD_CATS:
            data, is_stale = load_cache_stale_check("vod_cats")
            if data:
                CACHED_VOD_CATS = data
                if is_stale:
                    revalidate_cache_in_background("vod_cats", "get_vod_categories")
            else:
                print("[~] Fetching VOD categories from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_vod_categories"
                CACHED_VOD_CATS = fetch_json_from_iptv(url) or []
                if CACHED_VOD_CATS:
                    save_to_persistent_cache("vod_cats", CACHED_VOD_CATS)
        self.send_json(CACHED_VOD_CATS)

    def handle_get_series_cats(self):
        global CACHED_SERIES_CATS
        if not CACHED_SERIES_CATS:
            data, is_stale = load_cache_stale_check("series_cats")
            if data:
                CACHED_SERIES_CATS = data
                if is_stale:
                    revalidate_cache_in_background("series_cats", "get_series_categories")
            else:
                print("[~] Fetching series categories from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_series_categories"
                CACHED_SERIES_CATS = fetch_json_from_iptv(url) or []
                if CACHED_SERIES_CATS:
                    save_to_persistent_cache("series_cats", CACHED_SERIES_CATS)
        self.send_json(CACHED_SERIES_CATS)

    def handle_get_live_cats(self):
        global CACHED_LIVE_CATS
        if not CACHED_LIVE_CATS:
            data, is_stale = load_cache_stale_check("live_cats")
            if data:
                CACHED_LIVE_CATS = data
                if is_stale:
                    revalidate_cache_in_background("live_cats", "get_live_categories")
            else:
                print("[~] Fetching live categories from IPTV server...")
                url = f"{config['domain']}/player_api.php?username={config['username']}&password={config['password']}&action=get_live_categories"
                CACHED_LIVE_CATS = fetch_json_from_iptv(url) or []
                if CACHED_LIVE_CATS:
                    save_to_persistent_cache("live_cats", CACHED_LIVE_CATS)
        self.send_json(CACHED_LIVE_CATS)

    # ── Streaming Proxy (resolves CORS by piping IPTV streams through localhost) ──
    def handle_stream_proxy(self, query):
        """Fetch M3U8 manifest and rewrite segment/key URLs through our local proxy.

        Preferred form: /api/stream?type=<live|movie|series>&id=<stream_id> — the
        provider URL (with credentials) is built server-side so it never reaches
        the browser. Legacy ?url= is still accepted but restricted to safe hosts."""
        import re
        media_type = query.get("type", [None])[0]
        stream_id = query.get("id", [None])[0]
        raw_url = query.get("url", [None])[0]

        if media_type and stream_id:
            stream_url = build_stream_url(media_type, stream_id)
            if not stream_url:
                self.send_error(404, "Stream not found")
                return
        elif raw_url:
            stream_url = resolve_m3u_url(urllib.parse.unquote(raw_url))
        else:
            self.send_error(400, "Missing type/id (or url) parameter")
            return

        # Step 1: fetch the manifest from the provider
        try:
            if not is_safe_remote_url(stream_url):
                self.send_error(403, "Blocked: target host is not allowed")
                return
            with _open_upstream(stream_url, timeout=15) as resp:
                final_url = resp.geturl()
                content   = resp.read()
        except Exception as e:
            print(f"[Stream Proxy Error] {e}")
            self._safe_send_error(502, f"Upstream error: {e}")
            return

        # Step 2: rewrite segment/key URLs through our proxy
        if content.lstrip().startswith(b"#EXTM3U"):
            out = []
            for line in content.decode("utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    seg_url   = urllib.parse.urljoin(final_url, s)
                    out.append("/api/proxy?url=" + urllib.parse.quote(seg_url, safe=""))
                elif s.startswith("#EXT-X-KEY") and 'URI="' in s:
                    def fix_key(m):
                        ku = urllib.parse.urljoin(final_url, m.group(1))
                        return f'URI="/api/proxy?url={urllib.parse.quote(ku, safe="")}"'
                    out.append(re.sub(r'URI="([^"]+)"', fix_key, s))
                else:
                    out.append(line)
            new_content = "\n".join(out).encode("utf-8")
        else:
            new_content = content

        # Step 3: send to the browser. It may have already moved on (seek, quality
        # switch, closed the player) — that's normal, so ignore disconnects quietly.
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", len(new_content))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(new_content)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def handle_segment_proxy(self, query):
        """Pipe segment/key bytes from the IPTV server to the browser. If the URL
        turns out to be a nested playlist (variant of a master, for multi-bitrate /
        quality selection), rewrite its segment URLs through the proxy too."""
        import re
        raw_url = query.get("url", [None])[0]
        if not raw_url:
            self.send_error(400, "Missing url parameter")
            return
        url = urllib.parse.unquote(raw_url)
        if not is_safe_remote_url(url):
            self.send_error(403, "Blocked: target host is not allowed")
            return

        # Step 1: open the upstream connection, retrying through ISP interference
        try:
            resp = _open_upstream(url, timeout=20)
        except Exception as e:
            print(f"[Segment Proxy Error] {e}")
            self._safe_send_error(502, f"Segment proxy error: {e}")
            return

        # Step 2: stream to the browser. Both the player dropping the request and the
        # provider resetting the connection mid-segment are normal — handle quietly
        # so they don't flood the console with tracebacks.
        try:
            with resp:
                ctype     = resp.headers.get("Content-Type", "video/MP2T")
                final_url = resp.geturl()
                first     = resp.read(64)
                is_m3u8 = (first.lstrip().startswith(b"#EXTM3U")
                           or url.split("?")[0].lower().endswith(".m3u8")
                           or "mpegurl" in ctype.lower())

                if is_m3u8:
                    content = first + resp.read()
                    out = []
                    for line in content.decode("utf-8", errors="ignore").splitlines():
                        s = line.strip()
                        if s and not s.startswith("#"):
                            seg = urllib.parse.urljoin(final_url, s)
                            out.append("/api/proxy?url=" + urllib.parse.quote(seg, safe=""))
                        elif s.startswith("#EXT-X-KEY") and 'URI="' in s:
                            def fix_key(m):
                                ku = urllib.parse.urljoin(final_url, m.group(1))
                                return f'URI="/api/proxy?url={urllib.parse.quote(ku, safe="")}"'
                            out.append(re.sub(r'URI="([^"]+)"', fix_key, s))
                        else:
                            out.append(line)
                    new_content = "\n".join(out).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Content-Length", len(new_content))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(new_content)
                    return

                cl = resp.headers.get("Content-Length", "")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-cache")
                if cl:
                    self.send_header("Content-Length", cl)
                self.end_headers()
                self.wfile.write(first)
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass  # client moved on or upstream dropped mid-segment — normal
        except Exception as e:
            print(f"[Segment Proxy Error] {e}")

    def handle_get_library(self):
        try:
            save_dir = config.get("save_dir", DEFAULT_SAVE_DIR)
            library_items = config.get("library_items", [])
            
            # Filter and verify existence on disk
            items = []
            updated_library_items = []
            
            for item in library_items:
                name = item.get("name")
                file_path = os.path.join(save_dir, name)
                if os.path.exists(file_path) and os.path.isfile(file_path):
                    items.append(item)
                    updated_library_items.append(item)
            
            # If some items were deleted outside the app, sync the config
            if len(updated_library_items) != len(library_items):
                config["library_items"] = updated_library_items
                save_config()
                
            # Sort newest first
            items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
            self.send_json({"items": items})
        except Exception as e:
            print(f"Error reading local library: {e}")
            self.send_error(500, f"Error reading local library: {e}")

    def handle_stream_library(self, query):
        file_name = query.get("file", [None])[0]
        if not file_name:
            self.send_error(400, "Missing file parameter")
            return
        
        encoded_file = urllib.parse.quote(file_name)
        raw_url = f"/api/library/raw?file={encoded_file}"
        
        m3u8_content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:10800\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:10800.0,\n"
            f"{raw_url}\n"
            "#EXT-X-ENDLIST\n"
        ).encode('utf-8')
        
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", len(m3u8_content))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(m3u8_content)

    def handle_raw_library(self, query):
        file_name = query.get("file", [None])[0]
        if not file_name:
            self.send_error(400, "Missing file parameter")
            return
            
        save_dir = config.get("save_dir", DEFAULT_SAVE_DIR)
        # Allow subfolders (season folders) but never escape the save directory.
        file_path = os.path.normpath(os.path.join(save_dir, file_name))

        abs_save_dir = os.path.abspath(save_dir)
        abs_file_path = os.path.abspath(file_path)
        if os.path.commonpath([abs_save_dir, abs_file_path]) != abs_save_dir:
            self.send_error(403, "Access denied")
            return

        if not os.path.exists(file_path) or not os.path.isfile(file_path):
            self.send_error(404, "File not found")
            return
            
        try:
            file_size = os.path.getsize(file_path)
            
            _, ext = os.path.splitext(file_name.lower())
            ctype = "video/MP2T"
            if ext == ".mp4": ctype = "video/mp4"
            elif ext == ".mkv": ctype = "video/x-matroska"
            elif ext == ".avi": ctype = "video/x-msvideo"
            
            range_header = self.headers.get('Range', None)
            
            if range_header:
                import re
                match = re.match(r'bytes=(\d+)-(\d*)', range_header)
                if match:
                    start_byte = int(match.group(1))
                    end_byte_str = match.group(2)
                    end_byte = int(end_byte_str) if end_byte_str else file_size - 1
                    
                    if start_byte >= file_size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{file_size}")
                        self.end_headers()
                        return
                        
                    length = end_byte - start_byte + 1
                    
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Range", f"bytes {start_byte}-{end_byte}/{file_size}")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    
                    with open(file_path, 'rb') as f:
                        f.seek(start_byte)
                        remaining = length
                        chunk_size = 65536
                        while remaining > 0:
                            to_read = min(chunk_size, remaining)
                            chunk = f.read(to_read)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            remaining -= len(chunk)
                else:
                    self.send_error(400, "Invalid Range Header")
            else:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                
                with open(file_path, 'rb') as f:
                    chunk_size = 65536
                    while True:
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except Exception as e:
            print(f"Error streaming raw library: {e}")
            try:
                self.send_error(502, f"Streaming error: {e}")
            except:
                pass

    # Helper: Send JSON response
    def _safe_send_error(self, code, message=""):
        """send_error that won't crash if the client has already disconnected."""
        try:
            self.send_error(code, message)
        except (BrokenPipeError, ConnectionError, OSError):
            pass

    def send_json(self, data):
        try:
            content = json.dumps(data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(content))
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            print(f"Error sending JSON: {e}")

# Multi-threaded HTTP server class
class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A player abandoning a segment (seek / quality switch / close) closes the
        # socket mid-response. That's normal — don't print a scary traceback for it.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionError, OSError)):
            return
        super().handle_error(request, client_address)
def kill_process_on_port(port):
    import subprocess
    import os
    import time
    
    current_pid = os.getpid()
    try:
        if os.name == 'nt': # Windows
            cmd = "netstat -ano"
            output = subprocess.check_output(cmd, shell=True, text=True)
            pids = set()
            for line in output.strip().split('\n'):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            pid = int(parts[-1])
                            if pid != current_pid:
                                pids.add(pid)
                        except ValueError:
                            pass
            for pid in pids:
                print(f"[+] Found old server process with PID {pid}. Killing it...")
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], 
                               stdout=subprocess.DEVNULL, 
                               stderr=subprocess.DEVNULL,
                               creationflags=0x08000000)
            if pids:
                time.sleep(1.0)
        else: # macOS / Linux
            cmd = f"lsof -t -i:{port} -sTCP:LISTEN"
            try:
                output = subprocess.check_output(cmd, shell=True, text=True)
                pids = [int(p) for p in output.strip().split('\n') if p.strip()]
                for pid in pids:
                    if pid != current_pid:
                        os.kill(pid, 9)
                if pids:
                    time.sleep(1.0)
            except:
                pass
    except Exception as e:
        print(f"Error releasing port {port}: {e}")

def open_browser():
    time.sleep(1.2)
    webbrowser.open("http://localhost:32100")

def main():
    apply_app_dns_from_config()  # re-enable App DNS (DoH) if it was set before
    apply_upstream_proxy()       # re-enable the proxy/tunnel route if configured
    port = 32100
    kill_process_on_port(port)
    server_address = ('', port)
    httpd = ThreadingHTTPServer(server_address, LocalAppAPIHandler)
    print(f"[+] Elamir Media Hub Server running on http://localhost:{port}")
    
    # Auto-open browser in a separate thread
    threading.Thread(target=open_browser, daemon=True).start()
        
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[-] Shutting down server.")
        httpd.server_close()

if __name__ == "__main__":
    main()
