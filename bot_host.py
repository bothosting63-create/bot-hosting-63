import os
import re
import sys
import time
import json
import ast
import signal
import shutil
import sqlite3
import threading
import subprocess
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI-CLIENT TELEGRAM PYTHON HOST
# Render Web Service compatible
# ============================================================

APP_NAME = "KRUTIK CYBER EXPERT"

BASE_DIR = Path(__file__).resolve().parent

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()

DATA_DIR = Path(
    os.getenv(
        "DATA_DIR",
        str(BASE_DIR / "host_data")
    )
).resolve()

CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "host.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

session = requests.Session()

# ------------------------------------------------------------
# Runtime
# ------------------------------------------------------------

processes = {}
process_lock = threading.RLock()

intentional_stops = set()
user_states = {}

START_TIME = time.time()

MAX_UPLOAD_SIZE = 2 * 1024 * 1024
MAX_FILENAME_LENGTH = 100
MAX_LOG_SIZE = 8000

RATE_WINDOW = 60
RATE_MAX = 30

rate_data = {}
rate_lock = threading.Lock()


# ============================================================
# DEPENDENCY MAP
# ============================================================

IMPORT_TO_PACKAGE = {
    "telegram": "python-telegram-bot>=22,<23",
    "telegram.ext": "python-telegram-bot>=22,<23",
    "aiogram": "aiogram>=3,<4",
    "telebot": "pyTelegramBotAPI>=4,<5",
    "openai": "openai>=1.50,<2",
    "requests": "requests>=2.32,<3",
    "httpx": "httpx>=0.27,<1",
    "aiohttp": "aiohttp>=3,<4",
    "flask": "Flask>=3,<4",
    "fastapi": "fastapi>=0.115,<1",
    "uvicorn": "uvicorn>=0.30,<1",
    "bs4": "beautifulsoup4>=4,<5",
    "PIL": "Pillow>=10,<12",
    "cv2": "opencv-python-headless>=4,<5",
    "dotenv": "python-dotenv>=1,<2",
    "yaml": "PyYAML>=6,<7",
    "Crypto": "pycryptodome>=3,<4",
    "numpy": "numpy>=1.26,<3",
    "pandas": "pandas>=2,<3",
    "qrcode": "qrcode>=7,<9",
    "schedule": "schedule>=1,<2",
    "rich": "rich>=13,<15",
    "colorama": "colorama>=0.4,<1",
    "selenium": "selenium>=4,<5",
    "jwt": "PyJWT>=2,<3",
    "google": "google-api-python-client>=2,<3",
    "discord": "discord.py>=2,<3",
    "psutil": "psutil>=6,<8",
}

STDLIB = set(getattr(sys, "stdlib_module_names", set())) | {
    "os", "sys", "time", "json", "re", "math", "random",
    "datetime", "sqlite3", "subprocess", "threading",
    "asyncio", "logging", "pathlib", "typing", "collections",
    "itertools", "functools", "hashlib", "hmac", "base64",
    "secrets", "signal", "socket", "http", "urllib", "email",
    "io", "traceback", "shutil", "ast", "statistics",
    "decimal", "csv", "string", "textwrap", "copy",
    "dataclasses", "enum", "uuid", "platform",
}


# ============================================================
# DATABASE
# ============================================================

db_lock = threading.RLock()

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=10000")

db.execute("""
CREATE TABLE IF NOT EXISTS clients (
    chat_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    status TEXT DEFAULT 'enabled',
    created_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    filename TEXT NOT NULL,
    folder TEXT NOT NULL,
    status TEXT DEFAULT 'stopped',
    auto_restart INTEGER DEFAULT 1,
    created_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    created_at REAL NOT NULL
)
""")

db.commit()


def db_one(sql, params=()):
    with db_lock:
        return db.execute(sql, params).fetchone()


def db_all(sql, params=()):
    with db_lock:
        return db.execute(sql, params).fetchall()


def db_exec(sql, params=()):
    with db_lock:
        cur = db.execute(sql, params)
        db.commit()
        return cur


# ============================================================
# AUTH / SECURITY
# ============================================================

def is_owner(chat_id):
    try:
        return int(chat_id) == int(OWNER_CHAT_ID)
    except Exception:
        return False


def rate_ok(chat_id, action="general"):
    if is_owner(chat_id):
        return True

    now = time.time()
    key = f"{chat_id}:{action}"

    with rate_lock:
        values = rate_data.get(key, [])

        values = [
            x for x in values
            if now - x < RATE_WINDOW
        ]

        if len(values) >= RATE_MAX:
            rate_data[key] = values
            return False

        values.append(now)
        rate_data[key] = values

    return True


def get_global_lock():
    row = db_one(
        "SELECT value FROM settings WHERE key='global_lock'"
    )

    return bool(row and row[0] == "1")


def set_global_lock(value):
    db_exec("""
        INSERT INTO settings(key,value)
        VALUES('global_lock',?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
    """, ("1" if value else "0",))


def audit(actor, action, target=""):
    try:
        db_exec("""
            INSERT INTO audit(
                actor_id,
                action,
                target,
                created_at
            )
            VALUES(?,?,?,?)
        """, (
            int(actor),
            action,
            str(target),
            time.time()
        ))
    except Exception:
        pass


# ============================================================
# TELEGRAM API
# ============================================================

def telegram(method, data=None, timeout=30):
    try:
        response = session.post(
            f"{TELEGRAM_API}/{method}",
            data=data or {},
            timeout=timeout
        )

        return response.json()

    except Exception as e:
        print("Telegram API error:", repr(e))
        return {
            "ok": False,
            "error": str(e)
        }


def send_message(chat_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "text": str(text)
    }

    if keyboard:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    return telegram("sendMessage", data)


def edit_message(chat_id, message_id, text, keyboard=None):
    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": str(text)
    }

    if keyboard:
        data["reply_markup"] = json.dumps(
            keyboard,
            ensure_ascii=False
        )

    return telegram("editMessageText", data)


def answer_callback(callback_id, text=""):
    data = {
        "callback_query_id": callback_id
    }

    if text:
        data["text"] = text

    return telegram(
        "answerCallbackQuery",
        data
    )


def owner_message(text, keyboard=None):
    if OWNER_CHAT_ID:
        return send_message(
            OWNER_CHAT_ID,
            text,
            keyboard
        )


# ============================================================
# CLIENTS
# ============================================================

def get_client(chat_id):
    return db_one("""
        SELECT
            chat_id,
            username,
            first_name,
            last_name,
            status,
            created_at
        FROM clients
        WHERE chat_id=?
    """, (int(chat_id),))


def get_clients():
    return db_all("""
        SELECT
            chat_id,
            username,
            first_name,
            last_name,
            status,
            created_at
        FROM clients
        ORDER BY created_at ASC
    """)


def client_folder(chat_id):
    folder = (
        CLIENTS_DIR / str(int(chat_id))
    ).resolve()

    if CLIENTS_DIR not in folder.parents:
        raise ValueError("Unsafe client path")

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def client_display(client):
    cid, username, first, last, status, created = client

    if username:
        name = f"@{username}"
    else:
        name = " ".join(
            x for x in [first, last]
            if x
        ).strip()

        if not name:
            name = "No username"

    return f"{name} | ID: {cid}"


def register_client(user):
    chat_id = int(user["id"])

    username = user.get("username", "")
    first = user.get("first_name", "")
    last = user.get("last_name", "")

    existing = get_client(chat_id)

    if existing:
        db_exec("""
            UPDATE clients
            SET username=?,
                first_name=?,
                last_name=?
            WHERE chat_id=?
        """, (
            username,
            first,
            last,
            chat_id
        ))

        client_folder(chat_id)

        return False

    db_exec("""
        INSERT INTO clients(
            chat_id,
            username,
            first_name,
            last_name,
            status,
            created_at
        )
        VALUES(?,?,?,?,?,?)
    """, (
        chat_id,
        username,
        first,
        last,
        "enabled",
        time.time()
    ))

    client_folder(chat_id)

    audit(
        OWNER_CHAT_ID or 0,
        "new_client",
        chat_id
    )

    if not is_owner(chat_id):
        display = (
            f"@{username}"
            if username
            else first or "Unknown"
        )

        owner_message(
            "🆕 NEW CLIENT\n\n"
            f"👤 Username: {display}\n"
            f"📝 Name: {(first + ' ' + last).strip() or 'N/A'}\n"
            f"🆔 ID: {chat_id}\n"
            f"⏰ {time.strftime('%Y-%m-%d %H:%M:%S')}",
            {
                "inline_keyboard": [[
                    {
                        "text": "👤 Open Client",
                        "callback_data":
                            f"client:{chat_id}"
                    }
                ]]
            }
        )

    return True


def client_enabled(chat_id):
    if is_owner(chat_id):
        return True

    row = db_one("""
        SELECT status
        FROM clients
        WHERE chat_id=?
    """, (int(chat_id),))

    return bool(
        row and row[0] == "enabled"
    )


def authorized(chat_id):
    if is_owner(chat_id):
        return True

    if get_global_lock():
        return False

    return client_enabled(chat_id)


def set_client_status(chat_id, status):
    db_exec("""
        UPDATE clients
        SET status=?
        WHERE chat_id=?
    """, (
        status,
        int(chat_id)
    ))

    audit(
        OWNER_CHAT_ID or 0,
        f"client_{status}",
        chat_id
    )


# ============================================================
# BOT DATABASE
# ============================================================

def get_bot(bot_id):
    return db_one("""
        SELECT
            id,
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart,
            created_at
        FROM bots
        WHERE id=?
    """, (int(bot_id),))


def get_client_bots(owner_id):
    return db_all("""
        SELECT
            id,
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart,
            created_at
        FROM bots
        WHERE owner_id=?
        ORDER BY id
    """, (int(owner_id),))


def get_all_bots():
    return db_all("""
        SELECT
            id,
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart,
            created_at
        FROM bots
        ORDER BY id
    """)


def safe_filename(filename):
    if not filename:
        return None

    filename = Path(filename).name

    if len(filename) > MAX_FILENAME_LENGTH:
        return None

    if "\x00" in filename:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        filename
    ):
        return None

    if not filename.lower().endswith(".py"):
        return None

    return filename


def bot_path(bot):
    folder = Path(bot[4]).resolve()

    if CLIENTS_DIR not in folder.parents:
        raise ValueError("Unsafe bot folder")

    return folder


def bot_script(bot):
    path = bot_path(bot) / bot[3]

    if path.parent != bot_path(bot):
        raise ValueError("Unsafe script path")

    return path


def bot_python(bot):
    return bot_path(bot) / ".venv" / "bin" / "python"


# ============================================================
# IMPORT DETECTION
# ============================================================

def detect_imports(source):
    imports = set()

    try:
        tree = ast.parse(source)

    except SyntaxError:
        return imports

    for node in ast.walk(tree):

        if isinstance(node, ast.Import):
            for item in node.names:
                imports.add(
                    item.name.split(".")[0]
                )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(
                    node.module.split(".")[0]
                )

    return imports


def dependency_list(source):
    imports = detect_imports(source)

    packages = []

    for module in sorted(imports):

        if module in STDLIB:
            continue

        if module in IMPORT_TO_PACKAGE:
            package = IMPORT_TO_PACKAGE[module]

            if package not in packages:
                packages.append(package)

    return packages


# ============================================================
# VENV
# ============================================================

def ensure_venv(bot):
    folder = bot_path(bot)
    venv = folder / ".venv"

    python = (
        venv / "bin" / "python"
        if os.name != "nt"
        else venv / "Scripts" / "python.exe"
    )

    if not python.exists():

        print(
            f"[BOT {bot[0]}] Creating virtual environment"
        )

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                str(venv)
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            raise RuntimeError(
                "venv creation failed:\n" +
                result.stdout[-3000:]
            )

    return python


def install_dependencies(bot):
    script = bot_script(bot)

    try:
        source = script.read_text(
            encoding="utf-8",
            errors="replace"
        )
    except Exception:
        return

    packages = dependency_list(source)

    if not packages:
        return

    python = ensure_venv(bot)

    marker = bot_path(bot) / ".requirements_installed"

    desired = "\n".join(packages)

    if marker.exists():
        try:
            if marker.read_text(
                encoding="utf-8"
            ) == desired:
                return
        except Exception:
            pass

    print(
        f"[BOT {bot[0]}] Installing: {packages}"
    )

    result = subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            *packages
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=600
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Dependency installation failed:\n" +
            result.stdout[-5000:]
        )

    marker.write_text(
        desired,
        encoding="utf-8"
    )


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def process_alive(bot_id):
    with process_lock:
        p = processes.get(int(bot_id))

        if not p:
            return False

        return p.poll() is None


def update_bot_status(bot_id, status):
    db_exec("""
        UPDATE bots
        SET status=?
        WHERE id=?
    """, (
        status,
        int(bot_id)
    ))


def kill_process_group(p, force=False):
    if not p:
        return

    if p.poll() is not None:
        return

    try:

        if os.name == "posix":

            try:
                os.killpg(
                    os.getpgid(p.pid),
                    signal.SIGKILL if force
                    else signal.SIGTERM
                )

            except ProcessLookupError:
                pass

        else:

            if force:
                p.kill()
            else:
                p.terminate()

    except Exception as e:
        print(
            "Process termination error:",
            e
        )


def stop_bot(bot_id, intentional=True):
    bot = get_bot(bot_id)

    if not bot:
        return False

    bot_id = int(bot_id)

    if intentional:
        intentional_stops.add(bot_id)

    with process_lock:
        p = processes.get(bot_id)

    if p:
        kill_process_group(p, force=False)

        try:
            p.wait(timeout=8)
        except Exception:
            kill_process_group(
                p,
                force=True
            )

            try:
                p.wait(timeout=3)
            except Exception:
                pass

    with process_lock:
        processes.pop(bot_id, None)

    update_bot_status(
        bot_id,
        "stopped"
    )

    return True


def start_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    bot_id = int(bot_id)

    if process_alive(bot_id):
        return True, "Bot is already running."

    script = bot_script(bot)

    if not script.exists():
        return False, "Python file not found."

    try:
        source = script.read_text(
            encoding="utf-8",
            errors="replace"
        )

        compile(
            source,
            str(script),
            "exec"
        )

    except SyntaxError as e:
        update_bot_status(
            bot_id,
            "error"
        )

        return False, (
            "❌ Syntax error:\n"
            f"{e}"
        )

    except Exception as e:
        return False, str(e)

    try:
        install_dependencies(bot)

        python = bot_python(bot)

        if not python.exists():
            python = ensure_venv(bot)

        log_file = bot_path(bot) / "bot.log"

        log = open(
            log_file,
            "a",
            encoding="utf-8",
            buffering=1
        )

        log.write(
            "\n\n"
            + "=" * 60
            + "\n"
            + f"START {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + "=" * 60
            + "\n"
        )

        kwargs = {
            "cwd": str(bot_path(bot)),
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "stdin": subprocess.DEVNULL,
            "text": True,
        }

        if os.name == "posix":
            kwargs["start_new_session"] = True

        p = subprocess.Popen(
            [
                str(python),
                str(script)
            ],
            **kwargs
        )

        with process_lock:
            processes[bot_id] = p

        intentional_stops.discard(bot_id)

        update_bot_status(
            bot_id,
            "running"
        )

        threading.Thread(
            target=watch_process,
            args=(bot_id, p),
            daemon=True
        ).start()

        return True, "✅ Bot started."

    except Exception as e:

        update_bot_status(
            bot_id,
            "error"
        )

        return False, (
            "❌ Start failed:\n"
            + str(e)
        )


def watch_process(bot_id, p):
    try:
        code = p.wait()

    except Exception:
        code = -1

    with process_lock:
        current = processes.get(bot_id)

        if current is p:
            processes.pop(
                bot_id,
                None
            )

    if bot_id in intentional_stops:
        intentional_stops.discard(bot_id)

        update_bot_status(
            bot_id,
            "stopped"
        )

        return

    update_bot_status(
        bot_id,
        "crashed"
        if code != 0
        else "stopped"
    )

    bot = get_bot(bot_id)

    if not bot:
        return

    if (
        code != 0
        and bot[6] == 1
        and client_enabled(bot[1])
    ):
        time.sleep(2)

        if (
            bot_id not in intentional_stops
            and not process_alive(bot_id)
        ):
            start_bot(bot_id)


def restart_bot(bot_id):
    stop_bot(
        bot_id,
        intentional=True
    )

    time.sleep(1)

    return start_bot(bot_id)


def stop_all_bots(owner_id=None):
    bots = (
        get_client_bots(owner_id)
        if owner_id is not None
        else get_all_bots()
    )

    count = 0

    for bot in bots:

        if stop_bot(
            bot[0],
            intentional=True
        ):
            count += 1

    return count


# ============================================================
# BOT CREATION / DELETE
# ============================================================

def create_bot(
    owner_id,
    name,
    filename,
    content
):
    filename = safe_filename(filename)

    if not filename:
        raise ValueError(
            "Only safe .py filenames are allowed."
        )

    owner_id = int(owner_id)

    folder = (
        CLIENTS_DIR /
        str(owner_id) /
        f"bot_{int(time.time() * 1000)}"
    ).resolve()

    if CLIENTS_DIR not in folder.parents:
        raise ValueError("Unsafe path")

    folder.mkdir(
        parents=True,
        exist_ok=False
    )

    script = folder / filename

    if len(content) > MAX_UPLOAD_SIZE:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        raise ValueError(
            "File is too large. Maximum 2 MB."
        )

    script.write_bytes(content)

    try:
        source = content.decode(
            "utf-8"
        )

        compile(
            source,
            str(script),
            "exec"
        )

    except Exception:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

        raise

    cur = db_exec("""
        INSERT INTO bots(
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart,
            created_at
        )
        VALUES(?,?,?,?,?,?,?)
    """, (
        owner_id,
        name[:100],
        filename,
        str(folder),
        "stopped",
        1,
        time.time()
    ))

    bot_id = cur.lastrowid

    audit(
        owner_id,
        "create_bot",
        bot_id
    )

    return bot_id


def delete_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False

    stop_bot(
        bot_id,
        intentional=True
    )

    folder = bot_path(bot)

    db_exec(
        "DELETE FROM bots WHERE id=?",
        (int(bot_id),)
    )

    shutil.rmtree(
        folder,
        ignore_errors=True
    )

    intentional_stops.discard(
        int(bot_id)
    )

    audit(
        OWNER_CHAT_ID or 0,
        "delete_bot",
        bot_id
    )

    return True


def delete_client(client_id):
    client_id = int(client_id)

    if is_owner(client_id):
        return False

    stop_all_bots(
        owner_id=client_id
    )

    bots = get_client_bots(client_id)

    db_exec(
        "DELETE FROM bots WHERE owner_id=?",
        (client_id,)
    )

    db_exec(
        "DELETE FROM clients WHERE chat_id=?",
        (client_id,)
    )

    folder = (
        CLIENTS_DIR /
        str(client_id)
    ).resolve()

    if CLIENTS_DIR in folder.parents:
        shutil.rmtree(
            folder,
            ignore_errors=True
        )

    for bot in bots:
        intentional_stops.discard(
            int(bot[0])
        )

    audit(
        OWNER_CHAT_ID or 0,
        "delete_client",
        client_id
    )

    return True


# ============================================================
# LOGS
# ============================================================

def read_logs(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return "Bot not found."

    path = bot_path(bot) / "bot.log"

    if not path.exists():
        return "No logs yet."

    try:
        data = path.read_text(
            encoding="utf-8",
            errors="replace"
        )

        return data[-MAX_LOG_SIZE:]

    except Exception as e:
        return str(e)


# ============================================================
# KEYBOARDS
# ============================================================

def owner_panel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "👥 Clients",
                    "callback_data": "owner:clients"
                },
                {
                    "text": "🤖 All Bots",
                    "callback_data": "owner:bots"
                }
            ],
            [
                {
                    "text": "🛑 Stop All Bots",
                    "callback_data": "owner:stopall"
                }
            ],
            [
                {
                    "text": "🔒 Lock All Clients",
                    "callback_data": "owner:lock"
                },
                {
                    "text": "🔓 Unlock All Clients",
                    "callback_data": "owner:unlock"
                }
            ],
            [
                {
                    "text": "🔐 Hosting Access",
                    "callback_data": "owner:access"
                }
            ]
        ]
    }


def client_panel_keyboard():
    return {
        "inline_keyboard": [
            [
                {
                    "text": "🤖 My Bots",
                    "callback_data": "my:bots"
                }
            ],
            [
                {
                    "text": "ℹ️ Status",
                    "callback_data": "my:status"
                }
            ]
        ]
    }


def bot_keyboard(bot_id, owner=True):
    rows = [
        [
            {
                "text": "▶️ Start",
                "callback_data":
                    f"bot:start:{bot_id}"
            },
            {
                "text": "⏹ Stop",
                "callback_data":
                    f"bot:stop:{bot_id}"
            }
        ],
        [
            {
                "text": "🔄 Restart",
                "callback_data":
                    f"bot:restart:{bot_id}"
            },
            {
                "text": "📜 Logs",
                "callback_data":
                    f"bot:logs:{bot_id}"
            }
        ],
        [
            {
                "text": "🗑 Delete",
                "callback_data":
                    f"bot:delete:{bot_id}"
            }
        ],
        [
            {
                "text": "⬅️ Back",
                "callback_data":
                    "owner:bots"
                    if owner
                    else "my:bots"
            }
        ]
    ]

    return {
        "inline_keyboard": rows
    }


# ============================================================
# TEXT SCREENS
# ============================================================

def owner_panel_text():
    lock = (
        "🔒 LOCKED"
        if get_global_lock()
        else "🔓 UNLOCKED"
    )

    bots = get_all_bots()

    running = sum(
        1
        for b in bots
        if process_alive(b[0])
    )

    clients = len(get_clients())

    return (
        f"🛠 {APP_NAME}\n\n"
        f"👥 Clients: {clients}\n"
        f"🤖 Bots: {len(bots)}\n"
        f"🟢 Running: {running}\n"
        f"🔐 Global Client Access: {lock}\n\n"
        "Owner controls:"
    )


def client_panel_text(chat_id):
    bots = get_client_bots(chat_id)

    running = sum(
        1
        for b in bots
        if process_alive(b[0])
    )

    return (
        f"🤖 {APP_NAME}\n\n"
        f"Your bots: {len(bots)}\n"
        f"Running: {running}\n\n"
        "Upload one .py file to create a bot."
    )


def bot_text(bot):
    running = process_alive(bot[0])

    status = (
        "🟢 RUNNING"
        if running
        else f"🔴 {bot[5].upper()}"
    )

    return (
        f"🤖 {bot[2]}\n\n"
        f"🆔 Bot ID: {bot[0]}\n"
        f"👤 Owner ID: {bot[1]}\n"
        f"📄 File: {bot[3]}\n"
        f"📊 Status: {status}\n"
        f"🔄 Auto Restart: "
        f"{'ON' if bot[6] else 'OFF'}"
    )


# ============================================================
# OWNER SCREENS
# ============================================================

def show_clients(chat_id, message_id=None):
    clients = get_clients()

    rows = []

    for client in clients:

        cid = client[0]

        label = (
            f"@{client[1]}"
            if client[1]
            else client[2] or "Client"
        )

        if client[4] != "enabled":
            label = "🚫 " + label

        rows.append([
            {
                "text": label[:40],
                "callback_data":
                    f"client:{cid}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "owner:panel"
        }
    ])

    text = (
        f"👥 CLIENTS\n\n"
        f"Total: {len(clients)}\n\n"
        "Select a client:"
    )

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


def show_client(chat_id, client_id, message_id=None):
    client = get_client(client_id)

    if not client:
        text = "❌ Client not found."
        keyboard = {
            "inline_keyboard": [[
                {
                    "text": "⬅️ Back",
                    "callback_data":
                        "owner:clients"
                }
            ]]
        }

    else:
        bots = get_client_bots(client_id)

        label = client_display(client)

        status = (
            "🟢 Enabled"
            if client[4] == "enabled"
            else "🔴 Disabled"
        )

        text = (
            f"👤 CLIENT\n\n"
            f"{label}\n"
            f"📊 Access: {status}\n"
            f"🤖 Bots: {len(bots)}\n"
        )

        rows = []

        for bot in bots:
            rows.append([
                {
                    "text":
                        f"🤖 {bot[2]} "
                        f"({'🟢' if process_alive(bot[0]) else '🔴'})",
                    "callback_data":
                        f"bot:{bot[0]}"
                }
            ])

        rows += [
            [
                {
                    "text": (
                        "🚫 Disable Client"
                        if client[4] == "enabled"
                        else "🟢 Enable Client"
                    ),
                    "callback_data":
                        f"clienttoggle:{client_id}"
                }
            ],
            [
                {
                    "text": "🗑 Delete Client",
                    "callback_data":
                        f"clientdelete:{client_id}"
                }
            ],
            [
                {
                    "text": "⬅️ Clients",
                    "callback_data":
                        "owner:clients"
                }
            ]
        ]

        keyboard = {
            "inline_keyboard": rows
        }

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


def show_all_bots(chat_id, message_id=None):
    bots = get_all_bots()

    rows = []

    for bot in bots:

        rows.append([
            {
                "text":
                    f"#{bot[0]} {bot[2]} "
                    f"({'🟢' if process_alive(bot[0]) else '🔴'})",
                "callback_data":
                    f"bot:{bot[0]}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "owner:panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    text = (
        f"🤖 ALL BOTS\n\n"
        f"Total: {len(bots)}"
    )

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


def show_my_bots(chat_id, message_id=None):
    bots = get_client_bots(chat_id)

    rows = []

    for bot in bots:

        rows.append([
            {
                "text":
                    f"{bot[2]} "
                    f"({'🟢' if process_alive(bot[0]) else '🔴'})",
                "callback_data":
                    f"mybot:{bot[0]}"
            }
        ])

    text = (
        "🤖 MY BOTS\n\n"
        f"Total: {len(bots)}"
    )

    rows.append([
        {
            "text": "⬅️ Back",
            "callback_data": "my:panel"
        }
    ])

    keyboard = {
        "inline_keyboard": rows
    }

    if message_id:
        edit_message(
            chat_id,
            message_id,
            text,
            keyboard
        )
    else:
        send_message(
            chat_id,
            text,
            keyboard
        )


def show_access(chat_id):
    clients = get_clients()

    rows = []

    for client in clients:

        cid = client[0]

        label = (
            f"@{client[1]}"
            if client[1]
            else str(cid)
        )

        rows.append([
            {
                "text":
                    f"{'🟢' if client[4] == 'enabled' else '🔴'} {label}",
                "callback_data":
                    f"clienttoggle:{cid}"
            }
        ])

    rows.append([
        {
            "text": "⬅️ Owner Panel",
            "callback_data": "owner:panel"
        }
    ])

    send_message(
        chat_id,
        "🔐 HOSTING ACCESS\n\n"
        "Tap a client to enable/disable access.",
        {
            "inline_keyboard": rows
        }
    )


# ============================================================
# UPLOAD
# ============================================================

def download_document(document):
    file_id = document.get("file_id")

    name = document.get(
        "file_name",
        "bot.py"
    )

    name = safe_filename(name)

    if not name:
        raise ValueError(
            "❌ Only safe .py files are allowed."
        )

    size = int(
        document.get(
            "file_size",
            0
        ) or 0
    )

    if size > MAX_UPLOAD_SIZE:
        raise ValueError(
            "❌ File too large. Maximum is 2 MB."
        )

    result = telegram(
        "getFile",
        {"file_id": file_id}
    )

    if not result.get("ok"):
        raise RuntimeError(
            "Unable to get Telegram file."
        )

    file_path = result["result"]["file_path"]

    url = (
        f"https://api.telegram.org/file/bot"
        f"{BOT_TOKEN}/{file_path}"
    )

    response = session.get(
        url,
        timeout=60
    )

    if response.status_code != 200:
        raise RuntimeError(
            "File download failed."
        )

    content = response.content

    if len(content) > MAX_UPLOAD_SIZE:
        raise ValueError(
            "❌ File too large. Maximum is 2 MB."
        )

    return name, content


# ============================================================
# CALLBACK HANDLING
# ============================================================

def callback_allowed(chat_id):
    if is_owner(chat_id):
        return True

    return authorized(chat_id)


def handle_callback(callback):
    callback_id = callback["id"]

    message = callback.get(
        "message",
        {}
    )

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    if not chat_id:
        return

    data = callback.get(
        "data",
        ""
    )

    message_id = message.get(
        "message_id"
    )

    if not rate_ok(chat_id, "callback"):
        answer_callback(
            callback_id,
            "Too many requests."
        )
        return

    if not callback_allowed(chat_id):
        answer_callback(
            callback_id,
            "🔒 Hosting access is locked."
        )
        return

    answer_callback(
        callback_id
    )

    # --------------------------------------------------------
    # Owner panel
    # --------------------------------------------------------

    if data == "owner:panel":

        if not is_owner(chat_id):
            return

        edit_message(
            chat_id,
            message_id,
            owner_panel_text(),
            owner_panel_keyboard()
        )

        return

    if data == "owner:clients":

        if not is_owner(chat_id):
            return

        show_clients(
            chat_id,
            message_id
        )

        return

    if data == "owner:bots":

        if not is_owner(chat_id):
            return

        show_all_bots(
            chat_id,
            message_id
        )

        return

    if data == "owner:stopall":

        if not is_owner(chat_id):
            return

        count = stop_all_bots()

        edit_message(
            chat_id,
            message_id,
            f"🛑 STOP ALL COMPLETE\n\n"
            f"Stopped bots: {count}",
            owner_panel_keyboard()
        )

        audit(
            chat_id,
            "stop_all",
            count
        )

        return

    if data == "owner:lock":

        if not is_owner(chat_id):
            return

        set_global_lock(True)

        edit_message(
            chat_id,
            message_id,
            owner_panel_text(),
            owner_panel_keyboard()
        )

        audit(
            chat_id,
            "global_lock",
            "on"
        )

        return

    if data == "owner:unlock":

        if not is_owner(chat_id):
            return

        set_global_lock(False)

        edit_message(
            chat_id,
            message_id,
            owner_panel_text(),
            owner_panel_keyboard()
        )

        audit(
            chat_id,
            "global_lock",
            "off"
        )

        return

    if data == "owner:access":

        if is_owner(chat_id):
            show_access(chat_id)

        return

    # --------------------------------------------------------
    # Client
    # --------------------------------------------------------

    if data.startswith("client:"):

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        show_client(
            chat_id,
            client_id,
            message_id
        )

        return

    if data.startswith("clienttoggle:"):

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        client = get_client(client_id)

        if not client:
            return

        new_status = (
            "disabled"
            if client[4] == "enabled"
            else "enabled"
        )

        set_client_status(
            client_id,
            new_status
        )

        show_client(
            chat_id,
            client_id,
            message_id
        )

        return

    if data.startswith("clientdelete:"):

        if not is_owner(chat_id):
            return

        try:
            client_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        if client_id == chat_id:
            return

        user_states[chat_id] = {
            "type": "confirm_client_delete",
            "client_id": client_id
        }

        edit_message(
            chat_id,
            message_id,
            "⚠️ PERMANENT DELETE\n\n"
            "This will permanently delete:\n"
            "• Client record\n"
            "• All bots\n"
            "• Python files\n"
            "• .venv folders\n"
            "• Logs\n\n"
            "This action cannot be undone.\n\n"
            "Type CONFIRM to continue.",
            {
                "inline_keyboard": [[
                    {
                        "text": "❌ Cancel",
                        "callback_data":
                            f"client:{client_id}"
                    }
                ]]
            }
        )

        return

    # --------------------------------------------------------
    # Bot screens
    # --------------------------------------------------------

    if data.startswith("bot:"):

        parts = data.split(":")

        if len(parts) == 2:

            try:
                bot_id = int(parts[1])
            except Exception:
                return

            bot = get_bot(bot_id)

            if not bot:
                return

            if not control_allowed(
                chat_id,
                bot
            ):
                return

            edit_message(
                chat_id,
                message_id,
                bot_text(bot),
                bot_keyboard(
                    bot_id,
                    owner=is_owner(chat_id)
                )
            )

            return

        action = parts[1]

        try:
            bot_id = int(parts[2])
        except Exception:
            return

        bot = get_bot(bot_id)

        if not bot:
            return

        if not control_allowed(
            chat_id,
            bot
        ):
            return

        if action == "start":
            ok, text = start_bot(bot_id)

        elif action == "stop":
            ok = stop_bot(
                bot_id,
                intentional=True
            )
            text = (
                "⏹ Bot stopped."
                if ok
                else "Bot not found."
            )

        elif action == "restart":
            ok, text = restart_bot(
                bot_id
            )

        elif action == "logs":
            logs = read_logs(bot_id)

            send_message(
                chat_id,
                f"📜 LOGS — Bot #{bot_id}\n\n"
                f"{logs[-MAX_LOG_SIZE:]}"
            )

            return

        elif action == "delete":

            user_states[chat_id] = {
                "type": "confirm_bot_delete",
                "bot_id": bot_id
            }

            edit_message(
                chat_id,
                message_id,
                "⚠️ PERMANENT BOT DELETE\n\n"
                "The bot file, .venv and logs "
                "will be permanently deleted.\n\n"
                "Type CONFIRM to continue.",
                {
                    "inline_keyboard": [[
                        {
                            "text": "❌ Cancel",
                            "callback_data":
                                f"bot:{bot_id}"
                        }
                    ]]
                }
            )

            return

        else:
            return

        bot = get_bot(bot_id)

        edit_message(
            chat_id,
            message_id,
            (
                (text + "\n\n" if text else "")
                + bot_text(bot)
            ),
            bot_keyboard(
                bot_id,
                owner=is_owner(chat_id)
            )
        )

        return

    # --------------------------------------------------------
    # My bots
    # --------------------------------------------------------

    if data == "my:panel":

        if not authorized(chat_id):
            return

        edit_message(
            chat_id,
            message_id,
            client_panel_text(chat_id),
            client_panel_keyboard()
        )

        return

    if data == "my:bots":

        show_my_bots(
            chat_id,
            message_id
        )

        return

    if data == "my:status":

        send_message(
            chat_id,
            client_panel_text(chat_id)
        )

        return

    if data.startswith("mybot:"):

        try:
            bot_id = int(
                data.split(":")[1]
            )
        except Exception:
            return

        bot = get_bot(bot_id)

        if not bot:
            return

        if int(bot[1]) != int(chat_id):
            return

        edit_message(
            chat_id,
            message_id,
            bot_text(bot),
            bot_keyboard(
                bot_id,
                owner=False
            )
        )

        return


def control_allowed(chat_id, bot):
    return (
        is_owner(chat_id)
        or int(bot[1]) == int(chat_id)
    )


# ============================================================
# MESSAGE HANDLING
# ============================================================

def handle_message(message):
    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get("id")

    if not chat_id:
        return

    user = message.get(
        "from",
        chat
    )

    register_client(user)

    text = message.get(
        "text",
        ""
    )

    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    if text.strip().upper() == "CONFIRM":

        state = user_states.pop(
            chat_id,
            None
        )

        if state:

            if state["type"] == "confirm_bot_delete":

                bot_id = state["bot_id"]

                bot = get_bot(bot_id)

                if bot and control_allowed(
                    chat_id,
                    bot
                ):
                    delete_bot(bot_id)

                    send_message(
                        chat_id,
                        "🗑 Bot permanently deleted."
                    )

                return

            if state["type"] == "confirm_client_delete":

                if not is_owner(chat_id):
                    return

                client_id = state["client_id"]

                if delete_client(client_id):

                    send_message(
                        chat_id,
                        "🗑 Client and all data "
                        "permanently deleted."
                    )

                return

    # --------------------------------------------------------
    # Commands
    # --------------------------------------------------------

    if text.startswith("/start"):

        if is_owner(chat_id):

            send_message(
                chat_id,
                owner_panel_text(),
                owner_panel_keyboard()
            )

        elif authorized(chat_id):

            send_message(
                chat_id,
                client_panel_text(chat_id),
                client_panel_keyboard()
            )

        else:

            send_message(
                chat_id,
                "🔒 Hosting access is currently locked.\n\n"
                "Please contact the owner."
            )

        return

    if text == "/panel":

        if is_owner(chat_id):

            send_message(
                chat_id,
                owner_panel_text(),
                owner_panel_keyboard()
            )

        elif authorized(chat_id):

            send_message(
                chat_id,
                client_panel_text(chat_id),
                client_panel_keyboard()
            )

        return

    if text == "/clients" and is_owner(chat_id):

        show_clients(chat_id)
        return

    if text == "/bots" and is_owner(chat_id):

        show_all_bots(chat_id)
        return

    if text == "/stopall" and is_owner(chat_id):

        count = stop_all_bots()

        send_message(
            chat_id,
            f"🛑 Stopped {count} bots."
        )

        return

    if text == "/status":

        if not authorized(chat_id):
            return

        bots = (
            get_all_bots()
            if is_owner(chat_id)
            else get_client_bots(chat_id)
        )

        running = sum(
            process_alive(b[0])
            for b in bots
        )

        send_message(
            chat_id,
            f"📊 STATUS\n\n"
            f"🤖 Bots: {len(bots)}\n"
            f"🟢 Running: {running}\n"
            f"⏱ Host uptime: "
            f"{int(time.time() - START_TIME)} sec"
        )

        return

    if text == "/help":

        send_message(
            chat_id,
            "🛠 KRUTIK CYBER EXPERT\n\n"
            "/start - Panel\n"
            "/panel - Panel\n"
            "/status - Status\n"
            "/help - Help\n\n"
            "Send one .py file to create a bot."
        )

        return

    # --------------------------------------------------------
    # Document upload
    # --------------------------------------------------------

    document = message.get(
        "document"
    )

    if document:

        if not authorized(chat_id):

            send_message(
                chat_id,
                "🔒 Hosting access is locked."
            )

            return

        if not rate_ok(
            chat_id,
            "upload"
        ):

            send_message(
                chat_id,
                "⏳ Too many uploads. Try later."
            )

            return

        filename = document.get(
            "file_name",
            ""
        )

        if not filename.lower().endswith(".py"):

            send_message(
                chat_id,
                "❌ Only one .py file is allowed."
            )

            return

        try:

            filename, content = download_document(
                document
            )

            stem = Path(
                filename
            ).stem

            bot_id = create_bot(
                chat_id,
                stem,
                filename,
                content
            )

            send_message(
                chat_id,
                f"✅ Bot created successfully.\n\n"
                f"🤖 Name: {stem}\n"
                f"🆔 Bot ID: {bot_id}\n"
                f"📄 File: {filename}\n\n"
                "Use the button below.",
                {
                    "inline_keyboard": [[
                        {
                            "text": "🤖 Open Bot",
                            "callback_data":
                                f"mybot:{bot_id}"
                        }
                    ]]
                }
            )

        except Exception as e:

            send_message(
                chat_id,
                "❌ Upload failed:\n\n"
                + str(e)
            )

        return


# ============================================================
# TELEGRAM POLLING
# ============================================================

def polling_loop():
    print("🤖 Telegram polling started")

    offset = None

    while True:

        try:

            data = {
                "timeout": 30,
                "allowed_updates": json.dumps([
                    "message",
                    "callback_query"
                ])
            }

            if offset is not None:
                data["offset"] = offset

            result = telegram(
                "getUpdates",
                data,
                timeout=40
            )

            if not result.get("ok"):

                print(
                    "getUpdates failed:",
                    result
                )

                time.sleep(3)
                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"] + 1
                )

                try:

                    if "callback_query" in update:
                        handle_callback(
                            update["callback_query"]
                        )

                    elif "message" in update:
                        handle_message(
                            update["message"]
                        )

                except Exception as e:

                    print(
                        "Update handling error:",
                        repr(e)
                    )

        except Exception as e:

            print(
                "Polling error:",
                repr(e)
            )

            time.sleep(3)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        if self.path.startswith("/health"):
            body = (
                f"{APP_NAME} OK\n"
                f"uptime={int(time.time() - START_TIME)}\n"
            ).encode()

        else:
            body = (
                f"{APP_NAME} HOST RUNNING\n"
            ).encode()

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(body)

    def do_HEAD(self):

        body = b"OK"

        self.send_response(200)

        self.send_header(
            "Content-Type",
            "text/plain"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

    def log_message(
        self,
        format,
        *args
    ):
        pass


def start_health_server():

    port = int(
        os.getenv(
            "PORT",
            "10000"
        )
    )

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        HealthHandler
    )

    print(
        f"🌐 Render health server "
        f"listening on 0.0.0.0:{port}"
    )

    server.serve_forever()


# ============================================================
# RECOVER AUTO-RESTART BOTS
# ============================================================

def recover_bots():

    bots = get_all_bots()

    for bot in bots:

        if bot[5] == "running":

            update_bot_status(
                bot[0],
                "stopped"
            )

    for bot in bots:

        if bot[6] == 1:

            try:

                if client_enabled(
                    bot[1]
                ):

                    ok, text = start_bot(
                        bot[0]
                    )

                    print(
                        f"[RECOVERY] Bot {bot[0]}:",
                        text
                    )

            except Exception as e:

                print(
                    f"[RECOVERY] Bot {bot[0]} failed:",
                    e
                )


# ============================================================
# VALIDATION
# ============================================================

def validate_environment():

    if not BOT_TOKEN:

        print(
            "❌ BOT_TOKEN environment variable is missing."
        )

        return False

    if not OWNER_CHAT_ID:

        print(
            "❌ OWNER_CHAT_ID environment variable is missing."
        )

        return False

    try:
        int(OWNER_CHAT_ID)

    except ValueError:

        print(
            "❌ OWNER_CHAT_ID must be numeric."
        )

        return False

    return True


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print(APP_NAME)
    print("Multi-client Telegram Hosting Manager")
    print("=" * 60)

    print(
        "DATA_DIR:",
        DATA_DIR
    )

    if not validate_environment():
        sys.exit(1)

    # IMPORTANT FOR RENDER:
    # Start HTTP server immediately so Render
    # detects an open port.
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    time.sleep(0.5)

    # Test Telegram token
    result = telegram(
        "getMe",
        timeout=15
    )

    if not result.get("ok"):

        print(
            "❌ Telegram BOT_TOKEN check failed."
        )

        print(result)

        # Keep process alive so Render can still see
        # the health endpoint while configuration is fixed.
    else:

        bot_info = result.get(
            "result",
            {}
        )

        print(
            "✅ Telegram bot:",
            bot_info.get("username")
        )

    # Recover bots only after Telegram/health
    # infrastructure is started.
    try:
        recover_bots()
    except Exception as e:
        print(
            "Recovery error:",
            repr(e)
        )

    polling_loop()


if __name__ == "__main__":
    main()
