import os
import re
import time
import json
import sqlite3
import signal
import subprocess
import threading
import shutil
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI-CLIENT TELEGRAM PYTHON HOST
# ============================================================

BRAND = "KRUTIK CYBER EXPERT"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()

BASE_DIR = Path(__file__).resolve().parent

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

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

session = requests.Session()

START_TIME = time.time()

# ============================================================
# LIMITS
# ============================================================

MAX_UPLOAD_SIZE = 2 * 1024 * 1024
MAX_FILENAME_LENGTH = 100

RATE_WINDOW = 60
RATE_MAX = 30

LOG_MAX_SIZE = 5 * 1024 * 1024

# ============================================================
# PROCESS STORAGE
# ============================================================

processes = {}
process_lock = threading.RLock()

# Bot IDs intentionally stopped by user/admin.
# This prevents watcher from auto-restarting them.
intentional_stops = set()

# Prevent multiple watcher threads from restarting same bot.
watchers = set()

user_states = {}

_rate_events = {}
_rate_lock = threading.Lock()

db_lock = threading.RLock()


# ============================================================
# DATABASE
# ============================================================

db = sqlite3.connect(
    DB_FILE,
    check_same_thread=False
)

db.execute("""
CREATE TABLE IF NOT EXISTS clients (
    chat_id INTEGER PRIMARY KEY,
    username TEXT DEFAULT '',
    first_name TEXT DEFAULT '',
    last_name TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'enabled',
    added_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    filename TEXT NOT NULL,
    folder TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped',
    auto_restart INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
""")

db.commit()


# ============================================================
# DATABASE HELPERS
# ============================================================

def db_execute(query, params=(), fetch=False, many=False):
    with db_lock:
        cur = db.cursor()

        if many:
            cur.executemany(query, params)
        else:
            cur.execute(query, params)

        rows = cur.fetchall() if fetch else None
        db.commit()

        return rows


def get_setting(key, default=None):
    with db_lock:
        row = db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,)
        ).fetchone()

    return row[0] if row else default


def set_setting(key, value):
    db_execute(
        """
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, str(value))
    )


# ============================================================
# SECURITY
# ============================================================

def owner_id():
    try:
        return int(OWNER_CHAT_ID)
    except Exception:
        return 0


def is_owner(chat_id):
    try:
        return int(chat_id) == owner_id()
    except Exception:
        return False


def check_rate_limit(chat_id, action="general"):
    now = time.time()
    key = f"{chat_id}:{action}"

    with _rate_lock:
        events = [
            t
            for t in _rate_events.get(key, [])
            if now - t < RATE_WINDOW
        ]

        if len(events) >= RATE_MAX:
            _rate_events[key] = events
            return False

        events.append(now)
        _rate_events[key] = events

    return True


def validate_filename(filename):
    if not filename:
        return None

    filename = str(filename).strip()

    if len(filename) > MAX_FILENAME_LENGTH:
        return None

    if "\x00" in filename:
        return None

    if Path(filename).name != filename:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]+",
        filename
    ):
        return None

    if not filename.lower().endswith(".py"):
        return None

    return filename


def safe_client_folder(chat_id):
    try:
        chat_id = int(chat_id)

        root = CLIENTS_DIR.resolve()

        folder = (
            CLIENTS_DIR / str(chat_id)
        ).resolve()

        folder.relative_to(root)

        return folder

    except Exception:
        return None


def safe_bot_folder(bot):
    if not bot:
        return None

    try:
        bot_id = int(bot[0])
        owner = int(bot[1])

        expected = (
            CLIENTS_DIR
            / str(owner)
            / f"bot_{bot_id}"
        ).resolve()

        actual = Path(bot[4]).resolve()

        actual.relative_to(
            CLIENTS_DIR.resolve()
        )

        if actual != expected:
            return None

        return actual

    except Exception:
        return None


def safe_script_path(bot):
    folder = safe_bot_folder(bot)

    if folder is None:
        return None

    filename = validate_filename(bot[3])

    if not filename:
        return None

    script = (
        folder / filename
    ).resolve()

    try:
        script.relative_to(folder)
    except ValueError:
        return None

    return script


# ============================================================
# GLOBAL CLIENT LOCK
# ============================================================

def clients_locked():
    return get_setting(
        "global_client_lock",
        "0"
    ) == "1"


def lock_clients():
    set_setting(
        "global_client_lock",
        "1"
    )


def unlock_clients():
    set_setting(
        "global_client_lock",
        "0"
    )


def hosting_authorized(chat_id):
    if is_owner(chat_id):
        return True

    if clients_locked():
        return False

    return client_enabled(chat_id)


# ============================================================
# TELEGRAM API
# ============================================================

def api(method, data=None, timeout=20):
    try:
        response = session.post(
            f"{API}/{method}",
            data=data or {},
            timeout=timeout
        )

        return response.json()

    except Exception as e:
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

    return api(
        "sendMessage",
        data
    )


def answer_callback(callback_id, text=None):
    data = {
        "callback_query_id": callback_id
    }

    if text:
        data["text"] = text

    return api(
        "answerCallbackQuery",
        data
    )


# ============================================================
# CLIENT MANAGEMENT
# ============================================================

def client_exists(chat_id):
    rows = db_execute(
        """
        SELECT chat_id
        FROM clients
        WHERE chat_id = ?
        """,
        (int(chat_id),),
        fetch=True
    )

    return bool(rows)


def client_enabled(chat_id):
    rows = db_execute(
        """
        SELECT status
        FROM clients
        WHERE chat_id = ?
        """,
        (int(chat_id),),
        fetch=True
    )

    return bool(
        rows
        and rows[0][0] == "enabled"
    )


def authorized(chat_id):
    return hosting_authorized(chat_id)


def add_client(
    chat_id,
    username="",
    first_name="",
    last_name=""
):
    chat_id = int(chat_id)

    existed = client_exists(chat_id)

    db_execute(
        """
        INSERT INTO clients
        (
            chat_id,
            username,
            first_name,
            last_name,
            status,
            added_at
        )
        VALUES (?, ?, ?, ?, 'enabled', ?)

        ON CONFLICT(chat_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_name=excluded.last_name
        """,
        (
            chat_id,
            username or "",
            first_name or "",
            last_name or "",
            time.time()
        )
    )

    folder = safe_client_folder(chat_id)

    if folder:
        folder.mkdir(
            parents=True,
            exist_ok=True
        )

    return not existed


def set_client_status(chat_id, status):
    db_execute(
        """
        UPDATE clients
        SET status = ?
        WHERE chat_id = ?
        """,
        (
            status,
            int(chat_id)
        )
    )


def get_clients():
    return db_execute(
        """
        SELECT
            chat_id,
            username,
            first_name,
            last_name,
            status,
            added_at
        FROM clients
        ORDER BY added_at
        """,
        fetch=True
    )


def get_client(chat_id):
    rows = db_execute(
        """
        SELECT
            chat_id,
            username,
            first_name,
            last_name,
            status,
            added_at
        FROM clients
        WHERE chat_id = ?
        """,
        (int(chat_id),),
        fetch=True
    )

    return rows[0] if rows else None


def remove_client(chat_id):
    chat_id = int(chat_id)

    # Stop all processes first.
    stop_all_bots(chat_id)

    db_execute(
        """
        DELETE FROM bots
        WHERE owner_id = ?
        """,
        (chat_id,)
    )

    db_execute(
        """
        DELETE FROM clients
        WHERE chat_id = ?
        """,
        (chat_id,)
    )

    folder = safe_client_folder(chat_id)

    if folder and folder.exists():
        try:
            shutil.rmtree(folder)
        except Exception:
            pass


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(owner_id, name, filename):
    owner_id = int(owner_id)

    folder_placeholder = ""

    with db_lock:
        cur = db.execute(
            """
            INSERT INTO bots
            (
                owner_id,
                name,
                filename,
                folder,
                status,
                auto_restart,
                created_at
            )
            VALUES (?, ?, ?, ?, 'stopped', 1, ?)
            """,
            (
                owner_id,
                name,
                filename,
                folder_placeholder,
                time.time()
            )
        )

        bot_id = cur.lastrowid

        folder = (
            CLIENTS_DIR
            / str(owner_id)
            / f"bot_{bot_id}"
        ).resolve()

        folder.relative_to(
            CLIENTS_DIR.resolve()
        )

        folder.mkdir(
            parents=True,
            exist_ok=True
        )

        db.execute(
            """
            UPDATE bots
            SET folder = ?
            WHERE id = ?
            """,
            (
                str(folder),
                bot_id
            )
        )

        db.commit()

    return bot_id


def get_bot(bot_id):
    rows = db_execute(
        """
        SELECT
            id,
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart
        FROM bots
        WHERE id = ?
        """,
        (int(bot_id),),
        fetch=True
    )

    return rows[0] if rows else None


def get_client_bots(owner_id):
    return db_execute(
        """
        SELECT
            id,
            name,
            filename,
            status,
            auto_restart
        FROM bots
        WHERE owner_id = ?
        ORDER BY id
        """,
        (int(owner_id),),
        fetch=True
    )


def get_all_bots():
    return db_execute(
        """
        SELECT
            id,
            owner_id,
            name,
            filename,
            folder,
            status,
            auto_restart
        FROM bots
        ORDER BY id
        """,
        fetch=True
    )


def set_bot_status(bot_id, status):
    db_execute(
        """
        UPDATE bots
        SET status = ?
        WHERE id = ?
        """,
        (
            int(bot_id),
            status
        )
    )


def set_auto_restart(bot_id, enabled):
    db_execute(
        """
        UPDATE bots
        SET auto_restart = ?
        WHERE id = ?
        """,
        (
            1 if enabled else 0,
            int(bot_id)
        )
    )


# ============================================================
# LOGGING
# ============================================================

def log_file(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return None

    folder = safe_bot_folder(bot)

    if folder is None:
        return None

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder / "bot.log"


def write_log(bot_id, text):
    path = log_file(bot_id)

    if not path:
        return

    try:
        if path.exists():
            if path.stat().st_size > LOG_MAX_SIZE:
                old = path.read_text(
                    encoding="utf-8",
                    errors="replace"
                )

                old = old[-1024 * 1024:]

                path.write_text(
                    old,
                    encoding="utf-8"
                )

        with open(
            path,
            "a",
            encoding="utf-8",
            errors="replace"
        ) as f:

            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{text}\n"
            )

    except Exception:
        pass


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def process_alive(bot_id):
    bot_id = int(bot_id)

    with process_lock:
        process = processes.get(bot_id)

    return (
        process is not None
        and process.poll() is None
    )


def terminate_process(process):
    if not process:
        return

    if process.poll() is not None:
        return

    try:
        if os.name == "posix":
            os.killpg(
                os.getpgid(process.pid),
                signal.SIGTERM
            )
        else:
            process.terminate()

    except Exception:
        try:
            process.terminate()
        except Exception:
            pass

    try:
        process.wait(
            timeout=5
        )

    except subprocess.TimeoutExpired:

        try:
            if os.name == "posix":
                os.killpg(
                    os.getpgid(process.pid),
                    signal.SIGKILL
                )
            else:
                process.kill()

        except Exception:
            pass

        try:
            process.wait(
                timeout=5
            )
        except Exception:
            pass


def start_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    bot_id = int(bot[0])
    owner = int(bot[1])

    # Owner can control disabled clients.
    if not is_owner(owner):
        if not client_enabled(owner):
            return False, "Client is disabled."

    if process_alive(bot_id):
        return False, "Bot is already running."

    script = safe_script_path(bot)

    if script is None:
        return False, "Unsafe script path blocked."

    if not script.exists():
        return False, "Python file not found."

    # A fresh start means this bot is no longer intentionally stopped.
    with process_lock:
        intentional_stops.discard(bot_id)

    log = log_file(bot_id)

    if not log:
        return False, "Invalid log path."

    try:
        log.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with open(
            log,
            "a",
            encoding="utf-8",
            errors="replace"
        ) as log_handle:

            log_handle.write(
                "\n"
                + "=" * 60
                + "\n"
                + f"STARTING BOT #{bot_id}\n"
                + f"FILE: {script.name}\n"
                + "=" * 60
                + "\n"
            )

            if os.name == "posix":

                process = subprocess.Popen(
                    [
                        "python",
                        "-u",
                        str(script)
                    ],
                    cwd=str(script.parent),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True
                )

            else:

                process = subprocess.Popen(
                    [
                        "python",
                        "-u",
                        str(script)
                    ],
                    cwd=str(script.parent),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL
                )

        with process_lock:
            processes[bot_id] = process

        set_bot_status(
            bot_id,
            "running"
        )

        write_log(
            bot_id,
            f"PID: {process.pid}"
        )

        threading.Thread(
            target=watch_process,
            args=(bot_id,),
            daemon=True
        ).start()

        return True, (
            f"Bot started successfully.\n"
            f"PID: {process.pid}"
        )

    except Exception as e:

        write_log(
            bot_id,
            f"START ERROR: {repr(e)}"
        )

        set_bot_status(
            bot_id,
            "stopped"
        )

        return False, str(e)


def stop_bot(bot_id, intentional=True):
    bot_id = int(bot_id)

    with process_lock:

        process = processes.get(
            bot_id
        )

        if intentional:
            intentional_stops.add(
                bot_id
            )

    if not process:

        set_bot_status(
            bot_id,
            "stopped"
        )

        return False, "Bot is not running."

    try:

        terminate_process(
            process
        )

        with process_lock:
            processes.pop(
                bot_id,
                None
            )

        set_bot_status(
            bot_id,
            "stopped"
        )

        write_log(
            bot_id,
            "Bot stopped intentionally."
        )

        return True, "Bot stopped."

    except Exception as e:

        write_log(
            bot_id,
            f"STOP ERROR: {repr(e)}"
        )

        return False, str(e)


def restart_bot(bot_id):
    bot_id = int(bot_id)

    # Intentional stop prevents old watcher from restarting.
    stop_bot(
        bot_id,
        intentional=True
    )

    time.sleep(0.5)

    return start_bot(
        bot_id
    )


def watch_process(bot_id):
    bot_id = int(bot_id)

    with process_lock:

        if bot_id in watchers:
            return

        process = processes.get(
            bot_id
        )

        if not process:
            return

        watchers.add(
            bot_id
        )

    try:

        exit_code = process.wait()

        with process_lock:

            current = processes.get(
                bot_id
            )

            if current is process:
                processes.pop(
                    bot_id,
                    None
                )

            was_intentional = (
                bot_id in intentional_stops
            )

            intentional_stops.discard(
                bot_id
            )

        set_bot_status(
            bot_id,
            "stopped"
        )

        write_log(
            bot_id,
            f"Process exited with code {exit_code}"
        )

        # Intentional stop => NEVER auto restart.
        if was_intentional:
            write_log(
                bot_id,
                "Auto-restart skipped: intentional stop."
            )
            return

        bot = get_bot(bot_id)

        if not bot:
            return

        auto_restart = bool(
            bot[6]
        )

        if not auto_restart:
            return

        owner = int(
            bot[1]
        )

        if not client_enabled(owner):
            write_log(
                bot_id,
                "Auto-restart skipped: client disabled."
            )
            return

        time.sleep(2)

        current = get_bot(
            bot_id
        )

        if not current:
            return

        if process_alive(bot_id):
            return

        write_log(
            bot_id,
            "Bot crashed/exited unexpectedly. Auto-restarting..."
        )

        start_bot(
            bot_id
        )

    finally:

        with process_lock:
            watchers.discard(
                bot_id
            )


def stop_all_bots(owner_id=None):
    if owner_id is None:
        bots = get_all_bots()
    else:
        bots = get_client_bots(
            owner_id
        )

    stopped = 0

    for bot in bots:

        bot_id = int(
            bot[0]
        )

        ok, _ = stop_bot(
            bot_id,
            intentional=True
        )

        if ok:
            stopped += 1

    return stopped


def restart_all_bots(owner_id=None):
    if owner_id is None:
        bots = get_all_bots()
    else:
        bots = get_client_bots(
            owner_id
        )

    results = []

    for bot in bots:

        bot_id = int(
            bot[0]
        )

        ok, msg = restart_bot(
            bot_id
        )

        results.append(
            f"#{bot_id} {bot[1]}: "
            f"{'✅' if ok else '❌'} {msg}"
        )

    return results


# ============================================================
# DELETE BOT
# ============================================================

def delete_bot(bot_id):
    bot = get_bot(bot_id)

    if not bot:
        return False, "Bot not found."

    bot_id = int(
        bot[0]
    )

    # Permanently stop it first.
    stop_bot(
        bot_id,
        intentional=True
    )

    with process_lock:
        intentional_stops.discard(
            bot_id
        )

    folder = safe_bot_folder(
        bot
    )

    db_execute(
        """
        DELETE FROM bots
        WHERE id = ?
        """,
        (bot_id,)
    )

    if folder and folder.exists():
        try:
            shutil.rmtree(
                folder
            )
        except Exception as e:
            return True, (
                "Bot database record deleted, "
                f"but folder cleanup failed: {e}"
            )

    return True, "Bot permanently deleted."


# ============================================================
# KEYBOARDS
# ============================================================

def owner_menu():

    lock_text = (
        "🔓 Unlock All Clients"
        if clients_locked()
        else "🔒 Lock All Clients"
    )

    lock_callback = (
        "unlockall"
        if clients_locked()
        else "lockall"
    )

    return {
        "inline_keyboard": [

            [
                {
                    "text": "👥 Clients",
                    "callback_data": "clients"
                },
                {
                    "text": "🤖 All Bots",
                    "callback_data": "allbots"
                }
            ],

            [
                {
                    "text": "📊 Host Status",
                    "callback_data": "hoststatus"
                },
                {
                    "text": "🔐 Hosting Access",
                    "callback_data": "access"
                }
            ],

            [
                {
                    "text": "▶️ Run All",
                    "callback_data": "runall"
                },
                {
                    "text": "🛑 Stop All",
                    "callback_data": "stopall"
                }
            ],

            [
                {
                    "text": "🔄 Restart All",
                    "callback_data": "restartall"
                }
            ],

            [
                {
                    "text": lock_text,
                    "callback_data": lock_callback
                }
            ]
        ]
    }


def client_menu():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "📤 Upload Bot",
                    "callback_data": "upload"
                }
            ],

            [
                {
                    "text": "🤖 My Bots",
                    "callback_data": "mybots"
                }
            ],

            [
                {
                    "text": "▶️ Run All",
                    "callback_data": "myrunall"
                },
                {
                    "text": "🛑 Stop All",
                    "callback_data": "mystopall"
                }
            ],

            [
                {
                    "text": "🔄 Restart All",
                    "callback_data": "myrestartall"
                }
            ]
        ]
    }


# ============================================================
# OWNER PANEL
# ============================================================

def show_owner(chat_id):

    lock_status = (
        "🔒 LOCKED"
        if clients_locked()
        else "🔓 UNLOCKED"
    )

    send_message(
        chat_id,

        f"""👑 {BRAND}

OWNER CONTROL PANEL

🔐 Access: FULL OWNER
🌐 Global Client Access: {lock_status}

You have complete control over
all clients and hosted bots.""",

        owner_menu()
    )


# ============================================================
# CLIENT PANEL
# ============================================================

def show_client(chat_id):

    client = get_client(
        chat_id
    )

    username = (
        f"@{client[1]}"
        if client and client[1]
        else "Not available"
    )

    name = (
        " ".join(
            x for x in [
                client[2] if client else "",
                client[3] if client else ""
            ]
            if x
        )
        or "Unknown"
    )

    send_message(
        chat_id,

        f"""🚀 {BRAND}

CLIENT PANEL

👤 Name: {name}
🔹 Username: {username}
🆔 Client ID: {chat_id}

🟢 Hosting Access: ENABLED

You can manage only your
own hosted bots.""",

        client_menu()
    )


# ============================================================
# CLIENT LIST
# ============================================================

def show_clients(chat_id):

    clients = get_clients()

    if not clients:

        send_message(
            chat_id,
            "👥 No clients found."
        )

        return

    text = f"👥 {BRAND} CLIENTS\n\n"

    keyboard = []

    for client in clients:

        cid = client[0]
        username = client[1]
        first_name = client[2]
        last_name = client[3]
        status = client[4]

        display_name = (
            " ".join(
                x for x in [
                    first_name,
                    last_name
                ]
                if x
            )
            or "Unknown"
        )

        user_display = (
            f"@{username}"
            if username
            else "No username"
        )

        icon = (
            "🟢"
            if status == "enabled"
            else "🔴"
        )

        text += (
            f"{icon} {display_name}\n"
            f"   👤 {user_display}\n"
            f"   🆔 {cid}\n"
            f"   📊 {status.upper()}\n\n"
        )

        keyboard.append(
            [
                {
                    "text": (
                        f"{icon} "
                        f"{display_name} "
                        f"({cid})"
                    ),
                    "callback_data":
                        f"client:{cid}"
                }
            ]
        )

    keyboard.append(
        [
            {
                "text": "🔙 Owner Panel",
                "callback_data": "ownerpanel"
            }
        ]
    )

    send_message(
        chat_id,
        text,
        {
            "inline_keyboard":
                keyboard
        }
    )


# ============================================================
# CLIENT MANAGEMENT
# ============================================================

def client_management(
    chat_id,
    client_id
):

    client = get_client(
        client_id
    )

    if not client:

        send_message(
            chat_id,
            "❌ Client not found."
        )

        return

    cid = client[0]
    username = client[1]
    first_name = client[2]
    last_name = client[3]
    status = client[4]

    name = (
        " ".join(
            x for x in [
                first_name,
                last_name
            ]
            if x
        )
        or "Unknown"
    )

    bots = get_client_bots(
        cid
    )

    running = sum(
        1
        for b in bots
        if process_alive(b[0])
    )

    user_display = (
        f"@{username}"
        if username
        else "No username"
    )

    keyboard = {
        "inline_keyboard": [

            [
                {
                    "text": "🤖 Manage Bots",
                    "callback_data":
                        f"clientbots:{cid}"
                }
            ],

            [
                {
                    "text": "✅ Enable",
                    "callback_data":
                        f"enable:{cid}"
                },
                {
                    "text": "🚫 Disable",
                    "callback_data":
                        f"disable:{cid}"
                }
            ],

            [
                {
                    "text": "🗑️ Delete Client",
                    "callback_data":
                        f"removeconfirm:{cid}"
                }
            ],

            [
                {
                    "text": "🔙 Clients",
                    "callback_data": "clients"
                }
            ]
        ]
    }

    send_message(
        chat_id,

        f"""👤 CLIENT MANAGEMENT

📛 Name: {name}
👤 Username: {user_display}
🆔 Chat ID: {cid}

📊 Access: {status.upper()}

🤖 Total Bots: {len(bots)}
🟢 Running: {running}
🔴 Stopped: {len(bots) - running}

Choose an action:""",

        keyboard
    )


# ============================================================
# CLIENT BOT LIST
# ============================================================

def show_bots(chat_id, owner_id):

    bots = get_client_bots(
        owner_id
    )

    if not bots:

        send_message(
            chat_id,
            "🤖 No bots found for this client."
        )

        return

    text = (
        f"🤖 {BRAND} BOTS\n\n"
    )

    keyboard = []

    for bot in bots:

        bot_id = bot[0]
        name = bot[1]
        filename = bot[2]
        status = bot[3]
        auto_restart = bot[4]

        running = process_alive(
            bot_id
        )

        icon = (
            "🟢"
            if running
            else "🔴"
        )

        text += (
            f"{icon} #{bot_id} {name}\n"
            f"   📄 {filename}\n"
            f"   📊 {status}\n"
            f"   🔄 Auto Restart: "
            f"{'ON' if auto_restart else 'OFF'}\n\n"
        )

        keyboard.append(
            [
                {
                    "text":
                        f"🤖 #{bot_id} {name}",
                    "callback_data":
                        f"bot:{bot_id}"
                }
            ]
        )

    if is_owner(chat_id):

        keyboard.append(
            [
                {
                    "text": "🔙 Owner Panel",
                    "callback_data": "ownerpanel"
                }
            ]
        )

    else:

        keyboard.append(
            [
                {
                    "text": "🔙 My Panel",
                    "callback_data": "clientpanel"
                }
            ]
        )

    send_message(
        chat_id,
        text,
        {
            "inline_keyboard":
                keyboard
        }
    )


# ============================================================
# BOT MANAGEMENT
# ============================================================

def bot_management(
    chat_id,
    bot_id
):

    bot = get_bot(
        bot_id
    )

    if not bot:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    if not is_owner(chat_id):

        if int(bot[1]) != int(chat_id):
            send_message(
                chat_id,
                "🚫 Access denied."
            )
            return

    running = process_alive(
        bot_id
    )

    status = (
        "🟢 RUNNING"
        if running
        else "🔴 STOPPED"
    )

    keyboard = {
        "inline_keyboard": [

            [
                {
                    "text": "▶️ Run",
                    "callback_data":
                        f"run:{bot_id}"
                },
                {
                    "text": "🛑 Stop",
                    "callback_data":
                        f"stop:{bot_id}"
                }
            ],

            [
                {
                    "text": "🔄 Restart",
                    "callback_data":
                        f"restart:{bot_id}"
                }
            ],

            [
                {
                    "text": (
                        "🔕 Disable Auto Restart"
                        if bot[6]
                        else "🔔 Enable Auto Restart"
                    ),
                    "callback_data":
                        f"autorestart:{bot_id}"
                }
            ],

            [
                {
                    "text": "📜 Logs",
                    "callback_data":
                        f"logs:{bot_id}"
                }
            ],

            [
                {
                    "text": "🗑️ Delete Bot",
                    "callback_data":
                        f"deletebotconfirm:{bot_id}"
                }
            ]
        ]
    }

    send_message(
        chat_id,

        f"""🤖 BOT CONTROL

🆔 Bot ID: {bot_id}
📛 Name: {bot[2]}
📄 File: {bot[3]}

📊 Status: {status}

👤 Owner: {bot[1]}

🔄 Auto Restart:
{'ON' if bot[6] else 'OFF'}""",

        keyboard
    )


# ============================================================
# LOGS
# ============================================================

def send_logs(chat_id, bot_id):

    bot = get_bot(
        bot_id
    )

    if not bot:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    if not is_owner(chat_id):

        if int(bot[1]) != int(chat_id):
            send_message(
                chat_id,
                "🚫 Access denied."
            )
            return

    path = log_file(
        bot_id
    )

    if not path or not path.exists():

        send_message(
            chat_id,
            "📜 No logs available."
        )

        return

    try:

        content = path.read_text(
            encoding="utf-8",
            errors="replace"
        )

        if not content:
            content = "No logs."

        if len(content) > 3500:
            content = content[-3500:]

        send_message(
            chat_id,
            "📜 LOGS\n\n"
            + content
        )

    except Exception as e:

        send_message(
            chat_id,
            f"❌ Log error:\n{e}"
        )


# ============================================================
# UPLOAD
# ============================================================

def download_document(
    message,
    owner_id
):

    document = message.get(
        "document"
    )

    if not document:
        return

    raw_filename = document.get(
        "file_name",
        "bot.py"
    )

    filename = validate_filename(
        raw_filename
    )

    file_size = document.get(
        "file_size"
    )

    if not filename:

        send_message(
            owner_id,
            "❌ Invalid filename.\n\n"
            "Sirf safe `.py` filename allowed hai."
        )

        return

    try:
        if (
            file_size is not None
            and int(file_size) > MAX_UPLOAD_SIZE
        ):
            send_message(
                owner_id,
                "❌ File too large.\n\n"
                "Maximum size: 2 MB"
            )
            return
    except Exception:
        pass

    file_id = document.get(
        "file_id"
    )

    if not file_id:
        send_message(
            owner_id,
            "❌ Invalid Telegram file."
        )
        return

    result = api(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if not result.get("ok"):

        send_message(
            owner_id,
            "❌ Telegram file information nahi mili."
        )

        return

    telegram_path = (
        result
        .get("result", {})
        .get("file_path")
    )

    if not telegram_path:

        send_message(
            owner_id,
            "❌ Telegram file path missing."
        )

        return

    try:

        response = session.get(
            f"https://api.telegram.org/file/"
            f"bot{BOT_TOKEN}/{telegram_path}",
            timeout=60
        )

        if response.status_code != 200:

            send_message(
                owner_id,
                "❌ File download failed."
            )

            return

        if len(response.content) > MAX_UPLOAD_SIZE:

            send_message(
                owner_id,
                "❌ Downloaded file 2 MB se bada hai."
            )

            return

        # Basic Python syntax check.
        source = response.content.decode(
            "utf-8"
        )

        try:
            compile(
                source,
                filename,
                "exec"
            )
        except SyntaxError as e:

            send_message(
                owner_id,
                f"❌ Python syntax error:\n\n{e}"
            )

            return

        name_without_ext = Path(
            filename
        ).stem

        bot_id = create_bot(
            owner_id,
            name_without_ext,
            filename
        )

        bot = get_bot(
            bot_id
        )

        folder = safe_bot_folder(
            bot
        )

        if folder is None:

            delete_bot(
                bot_id
            )

            send_message(
                owner_id,
                "❌ Unsafe bot folder."
            )

            return

        target = (
            folder / filename
        ).resolve()

        try:
            target.relative_to(
                folder
            )
        except ValueError:

            delete_bot(
                bot_id
            )

            send_message(
                owner_id,
                "❌ Unsafe file path."
            )

            return

        target.write_bytes(
            response.content
        )

        write_log(
            bot_id,
            "Bot uploaded successfully."
        )

        user_states.pop(
            owner_id,
            None
        )

        send_message(
            owner_id,

            f"""✅ BOT UPLOADED

🏷️ {BRAND}

🤖 Bot ID: {bot_id}
📛 Name: {name_without_ext}
📄 File: {filename}

📁 Stored safely in client folder.

You can now run the bot.""",

            {
                "inline_keyboard": [
                    [
                        {
                            "text": "▶️ Run Now",
                            "callback_data":
                                f"run:{bot_id}"
                        }
                    ],
                    [
                        {
                            "text": "🤖 Bot Control",
                            "callback_data":
                                f"bot:{bot_id}"
                        }
                    ]
                ]
            }
        )

    except UnicodeDecodeError:

        send_message(
            owner_id,
            "❌ File UTF-8 Python source nahi lagti."
        )

    except Exception as e:

        send_message(
            owner_id,
            f"❌ Upload error:\n{repr(e)}"
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(callback):

    callback_id = callback.get(
        "id"
    )

    message = callback.get(
        "message",
        {}
    )

    chat = message.get(
        "chat",
        {}
    )

    chat_id = chat.get(
        "id"
    )

    data = callback.get(
        "data",
        ""
    )

    if chat_id is None:
        return

    if not check_rate_limit(
        chat_id,
        "callback"
    ):

        answer_callback(
            callback_id,
            "⚠️ Too many requests."
        )

        return

    answer_callback(
        callback_id
    )

    # Owner panel callbacks remain available to owner.
    # Client callbacks require hosting access.
    if not is_owner(chat_id):

        if not authorized(chat_id):

            send_message(
                chat_id,

                f"""🚫 ACCESS DENIED

{BRAND}

Hosting access is currently unavailable.

🆔 {chat_id}

Contact the owner."""
            )

            return

    # ========================================================
    # PANELS
    # ========================================================

    if data == "ownerpanel":

        if is_owner(chat_id):
            show_owner(chat_id)

        return

    if data == "clientpanel":

        if not is_owner(chat_id):
            show_client(chat_id)

        return

    # ========================================================
    # OWNER CLIENTS
    # ========================================================

    if data == "clients":

        if is_owner(chat_id):
            show_clients(chat_id)

        return

    if data == "access":

        if not is_owner(chat_id):
            return

        clients = get_clients()

        text = (
            "🔐 HOSTING ACCESS\n\n"
        )

        if not clients:
            text += "No clients."

        else:
            for c in clients:

                name = (
                    " ".join(
                        x for x in [
                            c[2],
                            c[3]
                        ]
                        if x
                    )
                    or "Unknown"
                )

                username = (
                    f"@{c[1]}"
                    if c[1]
                    else "No username"
                )

                text += (
                    f"👤 {name}\n"
                    f"   {username}\n"
                    f"   🆔 {c[0]}\n"
                    f"   📊 {c[4].upper()}\n\n"
                )

        send_message(
            chat_id,
            text
        )

        return

    if data == "lockall":

        if not is_owner(chat_id):
            return

        lock_clients()

        send_message(
            chat_id,
            "🔒 ALL CLIENTS LOCKED.\n\n"
            "Clients cannot use hosting until unlocked."
        )

        show_owner(
            chat_id
        )

        return

    if data == "unlockall":

        if not is_owner(chat_id):
            return

        unlock_clients()

        send_message(
            chat_id,
            "🔓 ALL CLIENTS UNLOCKED."
        )

        show_owner(
            chat_id
        )

        return

    # ========================================================
    # HOST STATUS
    # ========================================================

    if data == "hoststatus":

        if not is_owner(chat_id):
            return

        bots = get_all_bots()

        running = sum(
            1
            for bot in bots
            if process_alive(bot[0])
        )

        uptime = int(
            time.time() - START_TIME
        )

        send_message(
            chat_id,

            f"""📊 {BRAND} HOST STATUS

👥 Clients: {len(get_clients())}

🤖 Total Bots: {len(bots)}
🟢 Running: {running}
🔴 Stopped: {len(bots) - running}

🔐 Global Client Lock:
{'ON' if clients_locked() else 'OFF'}

⏱️ Host Uptime:
{uptime} seconds"""
        )

        return

    # ========================================================
    # ALL BOTS
    # ========================================================

    if data == "allbots":

        if not is_owner(chat_id):
            return

        bots = get_all_bots()

        if not bots:

            send_message(
                chat_id,
                "🤖 No bots hosted."
            )

            return

        text = "🤖 ALL HOSTED BOTS\n\n"

        keyboard = []

        for bot in bots:

            running = process_alive(
                bot[0]
            )

            icon = (
                "🟢"
                if running
                else "🔴"
            )

            client = get_client(
                bot[1]
            )

            username = (
                f"@{client[1]}"
                if client and client[1]
                else "No username"
            )

            text += (
                f"{icon} #{bot[0]} "
                f"{bot[2]}\n"
                f"   👤 {username}\n"
                f"   🆔 {bot[1]}\n"
                f"   📄 {bot[3]}\n\n"
            )

            keyboard.append(
                [
                    {
                        "text":
                            f"🤖 #{bot[0]} {bot[2]}",
                        "callback_data":
                            f"bot:{bot[0]}"
                    }
                ]
            )

        send_message(
            chat_id,
            text,
            {
                "inline_keyboard":
                    keyboard
            }
        )

        return

    # ========================================================
    # OWNER RUN/STOP/RESTART ALL
    # ========================================================

    if data == "runall":

        if not is_owner(chat_id):
            return

        bots = get_all_bots()

        started = 0

        for bot in bots:

            ok, _ = start_bot(
                bot[0]
            )

            if ok:
                started += 1

        send_message(
            chat_id,
            f"▶️ Run All complete.\n\n"
            f"Started: {started}"
        )

        return

    if data == "stopall":

        if not is_owner(chat_id):
            return

        stopped = stop_all_bots()

        send_message(
            chat_id,
            f"🛑 Stop All complete.\n\n"
            f"Stopped: {stopped}"
        )

        return

    if data == "restartall":

        if not is_owner(chat_id):
            return

        results = restart_all_bots()

        send_message(
            chat_id,

            "🔄 Restart All\n\n"
            + "\n".join(results)
        )

        return

    # ========================================================
    # CLIENT MANAGEMENT
    # ========================================================

    if data.startswith("client:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        client_management(
            chat_id,
            cid
        )

        return

    if data.startswith("enable:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        set_client_status(
            cid,
            "enabled"
        )

        send_message(
            chat_id,
            f"✅ Client {cid} ENABLED."
        )

        return

    if data.startswith("disable:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        set_client_status(
            cid,
            "disabled"
        )

        stopped = stop_all_bots(
            cid
        )

        send_message(
            chat_id,

            f"""🚫 CLIENT DISABLED

🆔 {cid}

🛑 Stopped bots: {stopped}

Client can no longer use
the hosting manager."""
        )

        return

    if data.startswith("clientbots:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        show_bots(
            chat_id,
            cid
        )

        return

    # ========================================================
    # CLIENT BOT LIST
    # ========================================================

    if data == "mybots":

        show_bots(
            chat_id,
            chat_id
        )

        return

    # ========================================================
    # UPLOAD
    # ========================================================

    if data == "upload":

        user_states[
            chat_id
        ] = "upload"

        send_message(
            chat_id,

            f"""📤 {BRAND}

BOT UPLOAD

Send exactly ONE Python `.py` file.

Example:
mybot.py

Maximum size:
2 MB

The file will be stored
inside your private client folder."""
        )

        return

    # ========================================================
    # CLIENT RUN ALL
    # ========================================================

    if data == "myrunall":

        bots = get_client_bots(
            chat_id
        )

        count = 0

        for bot in bots:

            ok, _ = start_bot(
                bot[0]
            )

            if ok:
                count += 1

        send_message(
            chat_id,
            f"▶️ Started {count} bot(s)."
        )

        return

    if data == "mystopall":

        stopped = stop_all_bots(
            chat_id
        )

        send_message(
            chat_id,
            f"🛑 Stopped {stopped} bot(s)."
        )

        return

    if data == "myrestartall":

        results = restart_all_bots(
            chat_id
        )

        send_message(
            chat_id,

            "🔄 Restart All\n\n"
            + (
                "\n".join(results)
                if results
                else "No bots."
            )
        )

        return

    # ========================================================
    # BOT CONTROL
    # ========================================================

    if data.startswith("bot:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot_management(
            chat_id,
            bot_id
        )

        return

    if data.startswith("run:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        ok, msg = start_bot(
            bot_id
        )

        send_message(
            chat_id,
            ("✅ " if ok else "❌ ")
            + msg
        )

        return

    if data.startswith("stop:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        ok, msg = stop_bot(
            bot_id,
            intentional=True
        )

        send_message(
            chat_id,
            ("✅ " if ok else "❌ ")
            + msg
        )

        return

    if data.startswith("restart:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        ok, msg = restart_bot(
            bot_id
        )

        send_message(
            chat_id,
            ("✅ " if ok else "❌ ")
            + msg
        )

        return

    # ========================================================
    # AUTO RESTART
    # ========================================================

    if data.startswith("autorestart:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        new_value = not bool(
            bot[6]
        )

        set_auto_restart(
            bot_id,
            new_value
        )

        send_message(
            chat_id,

            "🔄 Auto Restart: "
            + (
                "ON"
                if new_value
                else "OFF"
            )
        )

        return

    # ========================================================
    # LOGS
    # ========================================================

    if data.startswith("logs:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        send_logs(
            chat_id,
            bot_id
        )

        return

    # ========================================================
    # BOT DELETE CONFIRM
    # ========================================================

    if data.startswith("deletebotconfirm:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        send_message(
            chat_id,

            f"""⚠️ PERMANENT DELETE

🤖 Bot: {bot[2]}
🆔 ID: {bot_id}

This will permanently delete:

• Python file
• bot.log
• bot folder
• database record

❌ This action cannot be undone.""",

            {
                "inline_keyboard": [
                    [
                        {
                            "text":
                                "❌ YES, DELETE PERMANENTLY",
                            "callback_data":
                                f"deletebot:{bot_id}"
                        }
                    ],
                    [
                        {
                            "text":
                                "🔙 Cancel",
                            "callback_data":
                                f"bot:{bot_id}"
                        }
                    ]
                ]
            }
        )

        return

    if data.startswith("deletebot:"):

        try:
            bot_id = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):
            if int(bot[1]) != int(chat_id):
                return

        ok, msg = delete_bot(
            bot_id
        )

        send_message(
            chat_id,
            ("✅ " if ok else "❌ ")
            + msg
        )

        return

    # ========================================================
    # CLIENT DELETE CONFIRM
    # ========================================================

    if data.startswith("removeconfirm:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        client = get_client(
            cid
        )

        if not client:
            return

        send_message(
            chat_id,

            f"""⚠️ PERMANENT CLIENT DELETE

🆔 Client: {cid}

This will permanently delete:

• Client record
• ALL client bots
• ALL Python files
• ALL bot logs
• ALL bot folders

❌ THIS CANNOT BE UNDONE.""",

            {
                "inline_keyboard": [
                    [
                        {
                            "text":
                                "❌ YES, DELETE CLIENT",
                            "callback_data":
                                f"remove:{cid}"
                        }
                    ],
                    [
                        {
                            "text":
                                "🔙 Cancel",
                            "callback_data":
                                f"client:{cid}"
                        }
                    ]
                ]
            }
        )

        return

    if data.startswith("remove:"):

        if not is_owner(chat_id):
            return

        try:
            cid = int(
                data.split(
                    ":",
                    1
                )[1]
            )
        except Exception:
            return

        if cid == owner_id():
            send_message(
                chat_id,
                "❌ Owner cannot be deleted."
            )
            return

        remove_client(
            cid
        )

        send_message(
            chat_id,
            f"✅ Client {cid} permanently deleted."
        )

        return


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_message(message):

    chat = message.get(
        "chat",
        {}
    )

    chat_id = int(
        chat.get("id")
    )

    text = message.get(
        "text",
        ""
    )

    user = message.get(
        "from",
        {}
    )

    username = user.get(
        "username",
        ""
    )

    first_name = user.get(
        "first_name",
        ""
    )

    last_name = user.get(
        "last_name",
        ""
    )

    # ========================================================
    # DOCUMENT
    # ========================================================

    if "document" in message:

        if not authorized(chat_id):

            send_message(
                chat_id,
                f"🚫 {BRAND}\n\n"
                "Hosting access denied."
            )

            return

        download_document(
            message,
            chat_id
        )

        return

    # ========================================================
    # CANCEL
    # ========================================================

    if text == "/cancel":

        user_states.pop(
            chat_id,
            None
        )

        send_message(
            chat_id,
            "❌ Current operation cancelled."
        )

        return

    # ========================================================
    # START
    # ========================================================

    if text.startswith("/start"):

        if is_owner(chat_id):

            show_owner(
                chat_id
            )

            return

        # First-time client registration.
        new_client = add_client(
            chat_id,
            username,
            first_name,
            last_name
        )

        if new_client:

            send_message(
                owner_id(),

                f"""🆕 NEW CLIENT

👤 Name:
{first_name} {last_name}

👤 Username:
@{username if username else 'No username'}

🆔 Chat ID:
{chat_id}

⏱️ Time:
{time.strftime('%Y-%m-%d %H:%M:%S')}

📊 Hosting Access:
{'🔒 LOCKED' if clients_locked() else '🟢 ENABLED'}""",

                {
                    "inline_keyboard": [
                        [
                            {
                                "text":
                                    "👤 Manage Client",
                                "callback_data":
                                    f"client:{chat_id}"
                            }
                        ]
                    ]
                }
            )

        if not authorized(chat_id):

            send_message(
                chat_id,

                f"""🚫 ACCESS DENIED

{BRAND}

Your hosting access is currently disabled.

🆔 {chat_id}

Please contact the owner."""
            )

            return

        show_client(
            chat_id
        )

        return

    # ========================================================
    # ADD CLIENT STATE
    # ========================================================

    if user_states.get(chat_id) == "add_client":

        if not is_owner(chat_id):
            return

        if not text.isdigit():

            send_message(
                chat_id,
                "❌ Invalid Chat ID.\n\n"
                "Only numbers allowed."
            )

            return

        cid = int(
            text
        )

        if cid == owner_id():

            send_message(
                chat_id,
                "❌ Owner ko client add karne ki zarurat nahi."
            )

            return

        added = add_client(
            cid
        )

        user_states.pop(
            chat_id,
            None
        )

        send_message(
            chat_id,

            f"""✅ CLIENT {'ADDED' if added else 'UPDATED'}

🆔 Chat ID: {cid}
📊 Status: ENABLED"""
        )

        return

    # ========================================================
    # AUTH
    # ========================================================

    if not authorized(chat_id):

        send_message(
            chat_id,

            f"""🚫 ACCESS DENIED

{BRAND}

You are not authorized.

🆔 {chat_id}"""
        )

        return

    # ========================================================
    # COMMANDS
    # ========================================================

    if text == "/panel":

        if is_owner(chat_id):
            show_owner(chat_id)
        else:
            show_client(chat_id)

        return

    if text == "/clients":

        if is_owner(chat_id):
            show_clients(chat_id)

        return

    if text == "/bots":

        show_bots(
            chat_id,
            chat_id
        )

        return

    if text == "/status":

        bots = get_client_bots(
            chat_id
        )

        running = sum(
            1
            for bot in bots
            if process_alive(bot[0])
        )

        send_message(
            chat_id,

            f"""📊 YOUR STATUS

👤 Client ID: {chat_id}

🤖 Total Bots: {len(bots)}
🟢 Running: {running}
🔴 Stopped: {len(bots) - running}

🏷️ {BRAND}"""
        )

        return

    if text == "/help":

        send_message(
            chat_id,

            f"""ℹ️ {BRAND}

Commands:

/start - Open panel
/panel - Open control panel
/bots - My bots
/status - Status
/cancel - Cancel current action
/help - Help"""
        )

        return


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8"
        )

        self.end_headers()

        uptime = int(
            time.time() - START_TIME
        )

        response = (
            f"{BRAND} is running\n"
            f"Uptime: {uptime}s\n"
        )

        self.wfile.write(
            response.encode(
                "utf-8"
            )
        )

    def do_HEAD(self):

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/plain"
        )

        self.end_headers()

    def log_message(
        self,
        format,
        *args
    ):
        return


def start_health_server():

    try:

        port = int(
            os.getenv(
                "PORT",
                "10000"
            )
        )

        server = HTTPServer(
            (
                "0.0.0.0",
                port
            ),
            HealthHandler
        )

        print(
            f"🌐 Health server listening "
            f"on 0.0.0.0:{port}"
        )

        server.serve_forever()

    except Exception as e:

        print(
            "❌ Health server error:",
            repr(e)
        )


# ============================================================
# POLLING
# ============================================================

def polling():

    print(
        f"🚀 {BRAND} HOST STARTING..."
    )

    result = api(
        "getMe"
    )

    if not result.get("ok"):

        print(
            "❌ Telegram API error:"
        )

        print(
            result
        )

        return

    bot_username = (
        result
        .get("result", {})
        .get("username")
    )

    print(
        f"🤖 @{bot_username}"
    )

    print(
        "🟢 Telegram API: OK"
    )

    api(
        "deleteWebhook",
        {
            "drop_pending_updates":
                "true"
        }
    )

    offset = None

    while True:

        try:

            data = {
                "timeout": 8,
                "limit": 50,
                "allowed_updates":
                    json.dumps(
                        [
                            "message",
                            "callback_query"
                        ]
                    )
            }

            if offset is not None:

                data["offset"] = offset

            result = api(
                "getUpdates",
                data,
                timeout=15
            )

            if not result.get("ok"):

                print(
                    "⚠️ Telegram error:",
                    result
                )

                time.sleep(
                    2
                )

                continue

            updates = result.get(
                "result",
                []
            )

            for update in updates:

                offset = (
                    update["update_id"]
                    + 1
                )

                try:

                    if "message" in update:

                        handle_message(
                            update["message"]
                        )

                    elif "callback_query" in update:

                        handle_callback(
                            update["callback_query"]
                        )

                except Exception as e:

                    print(
                        "⚠️ Update error:",
                        repr(e)
                    )

        except KeyboardInterrupt:

            print(
                "\n🛑 Host stopped."
            )

            break

        except Exception as e:

            print(
                "🔄 Polling reconnect:",
                repr(e)
            )

            time.sleep(
                2
            )


# ============================================================
# STARTUP
# ============================================================

def validate_environment():

    if not BOT_TOKEN:

        print(
            "❌ BOT_TOKEN environment variable missing."
        )

        return False

    if not OWNER_CHAT_ID:

        print(
            "❌ OWNER_CHAT_ID environment variable missing."
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


def recover_database():

    print(
        "🔄 Checking existing bots..."
    )

    bots = get_all_bots()

    recovered = 0

    for bot in bots:

        set_bot_status(
            bot[0],
            "stopped"
        )

        recovered += 1

    print(
        f"✅ Database checked. Bots: {recovered}"
    )


def main():

    print(
        "=" * 60
    )

    print(
        f"🔥 {BRAND}"
    )

    print(
        "🚀 MULTI-CLIENT PYTHON HOST"
    )

    print(
        "=" * 60
    )

    if not validate_environment():
        return

    recover_database()

    # Render Web Service needs an open port.
    threading.Thread(
        target=start_health_server,
        daemon=True
    ).start()

    # Telegram polling.
    polling()


if __name__ == "__main__":

    main()
