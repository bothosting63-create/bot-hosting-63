import os
import re
import time
import json
import sqlite3
import signal
import subprocess
import threading
from pathlib import Path

import requests


# ============================================================
# KRUTIK CYBER EXPERT
# MULTI-CLIENT TELEGRAM PYTHON HOST
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()

BRAND = "KRUTIK CYBER EXPERT"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "host_data"))).resolve()
CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "host.db"

DATA_DIR.mkdir(exist_ok=True)
CLIENTS_DIR.mkdir(exist_ok=True)


API = f"https://api.telegram.org/bot{BOT_TOKEN}"

session = requests.Session()

processes = {}
process_lock = threading.Lock()

user_states = {}

START_TIME = time.time()


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

db.commit()

db_lock = threading.Lock()



# ============================================================
# SECURITY HARDENING
# ============================================================
MAX_UPLOAD_SIZE = 2 * 1024 * 1024
MAX_FILENAME_LENGTH = 100
RATE_WINDOW = 60
RATE_MAX = 30
_rate_events = {}
_rate_lock = threading.Lock()

def security_owner(chat_id):
    try:
        return int(chat_id) == int(OWNER_CHAT_ID)
    except (TypeError, ValueError):
        return False

def check_rate_limit(chat_id, action="general"):
    now = time.time()
    key = f"{chat_id}:{action}"
    with _rate_lock:
        events = [x for x in _rate_events.get(key, []) if now - x < RATE_WINDOW]
        if len(events) >= RATE_MAX:
            _rate_events[key] = events
            return False
        events.append(now)
        _rate_events[key] = events
        return True

def validate_filename(filename):
    if not filename or len(filename) > MAX_FILENAME_LENGTH:
        return None
    filename = str(filename).strip()
    if "\x00" in filename or Path(filename).name != filename:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
        return None
    if not filename.lower().endswith(".py"):
        return None
    return filename

def safe_bot_folder(bot):
    if not bot:
        return None
    try:
        owner_id = int(bot[1])
        bot_id = int(bot[0])
        expected = (CLIENTS_DIR / str(owner_id) / f"bot_{bot_id}").resolve()
        actual = Path(bot[4]).resolve()
        actual.relative_to(CLIENTS_DIR.resolve())
        return actual if actual == expected else None
    except Exception:
        return None

def can_control_bot(chat_id, bot):
    if not bot:
        return False
    try:
        return security_owner(chat_id) or int(chat_id) == int(bot[1])
    except (TypeError, ValueError):
        return False

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
        "text": text
    }

    if keyboard:
        data["reply_markup"] = json.dumps(keyboard)

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
# AUTHORIZATION
# ============================================================

def is_owner(chat_id):
    return security_owner(chat_id)


def client_exists(chat_id):

    with db_lock:

        row = db.execute(
            """
            SELECT status
            FROM clients
            WHERE chat_id = ?
            """,
            (int(chat_id),)
        ).fetchone()

    return row is not None


def client_enabled(chat_id):

    with db_lock:

        row = db.execute(
            """
            SELECT status
            FROM clients
            WHERE chat_id = ?
            """,
            (int(chat_id),)
        ).fetchone()

    return row is not None and row[0] == "enabled"


def authorized(chat_id):

    if is_owner(chat_id):
        return True

    return client_enabled(chat_id)


# ============================================================
# CLIENT DATABASE FUNCTIONS
# ============================================================

def add_client(chat_id):

    with db_lock:

        db.execute(
            """
            INSERT OR REPLACE INTO clients
            (chat_id, status, added_at)
            VALUES (?, 'enabled', ?)
            """,
            (
                int(chat_id),
                time.time()
            )
        )

        db.commit()

    client_folder(int(chat_id))


def set_client_status(chat_id, status):

    with db_lock:

        db.execute(
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

        db.commit()


def remove_client(chat_id):

    stop_all_bots(int(chat_id))

    with db_lock:

        db.execute(
            """
            DELETE FROM bots
            WHERE owner_id = ?
            """,
            (int(chat_id),)
        )

        db.execute(
            """
            DELETE FROM clients
            WHERE chat_id = ?
            """,
            (int(chat_id),)
        )

        db.commit()

    folder = client_folder(int(chat_id))

    if folder.exists():

        for item in folder.rglob("*"):

            if item.is_file():

                try:
                    item.unlink()
                except:
                    pass

        for item in sorted(
            folder.rglob("*"),
            reverse=True
        ):

            if item.is_dir():

                try:
                    item.rmdir()
                except:
                    pass

        try:
            folder.rmdir()
        except:
            pass


def get_clients():

    with db_lock:

        return db.execute(
            """
            SELECT chat_id, status, added_at
            FROM clients
            ORDER BY added_at
            """
        ).fetchall()


# ============================================================
# FILESYSTEM ISOLATION
# ============================================================

def client_folder(chat_id):

    folder = CLIENTS_DIR / str(int(chat_id))

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def bot_folder(owner_id, bot_id):

    folder = (client_folder(owner_id) / f"bot_{int(bot_id)}").resolve()
    folder.relative_to(CLIENTS_DIR.resolve())

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def safe_filename(filename):
    return validate_filename(filename) or "bot.py"


# ============================================================
# BOT DATABASE
# ============================================================

def create_bot(owner_id, name, filename):

    with db_lock:

        cursor = db.execute(
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
            VALUES (?, ?, ?, '', 'stopped', 1, ?)
            """,
            (
                int(owner_id),
                name,
                filename,
                time.time()
            )
        )

        bot_id = cursor.lastrowid

        folder = bot_folder(
            owner_id,
            bot_id
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

    with db_lock:

        return db.execute(
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
            (int(bot_id),)
        ).fetchone()


def get_client_bots(owner_id):

    with db_lock:

        return db.execute(
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
            (int(owner_id),)
        ).fetchall()


def set_bot_status(bot_id, status):

    with db_lock:

        db.execute(
            """
            UPDATE bots
            SET status = ?
            WHERE id = ?
            """,
            (
                status,
                int(bot_id)
            )
        )

        db.commit()


# ============================================================
# LOGGING
# ============================================================

def log_file(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return None

    folder = Path(bot[4])

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

    except:
        pass


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def process_alive(bot_id):

    with process_lock:

        p = processes.get(
            int(bot_id)
        )

    return (
        p is not None
        and p.poll() is None
    )


def start_bot(bot_id):

    bot = get_bot(bot_id)

    if not bot:

        return False, "Bot not found."

    bot_id = int(bot[0])
    owner_id = int(bot[1])
    filename = bot[3]
    folder = Path(bot[4])

    if not client_enabled(owner_id) and not is_owner(owner_id):

        return False, "Client is disabled."

    if process_alive(bot_id):

        return False, "Bot is already running."

    safe_folder = safe_bot_folder(bot)
    if safe_folder is None:
        return False, "Unsafe bot folder. Operation blocked."
    folder = safe_folder
    script = (folder / filename).resolve()
    try:
        script.relative_to(folder)
    except ValueError:
        return False, "Unsafe script path blocked."

    if not script.exists():

        return False, "Python file not found."

    log = log_file(bot_id)

    try:

        log.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        with open(
            log,
            "a",
            encoding="utf-8"
        ) as log_handle:

            log_handle.write(
                "\n\n"
                + "=" * 60
                + "\n"
                + f"STARTING BOT {bot_id}\n"
                + "=" * 60
                + "\n"
            )

            process = subprocess.Popen(
                [
                    "python",
                    "-u",
                    str(script)
                ],
                cwd=str(folder),
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

        return True, f"Bot started. PID: {process.pid}"

    except Exception as e:

        write_log(
            bot_id,
            f"START ERROR: {repr(e)}"
        )

        return False, str(e)


def stop_bot(bot_id):

    bot_id = int(bot_id)

    with process_lock:

        process = processes.get(
            bot_id
        )

    if not process:

        set_bot_status(
            bot_id,
            "stopped"
        )

        return False, "Bot is not running."

    try:

        if process.poll() is None:

            process.terminate()

            try:

                process.wait(
                    timeout=5
                )

            except subprocess.TimeoutExpired:

                process.kill()
                process.wait()

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
            "Bot stopped."
        )

        return True, "Bot stopped."

    except Exception as e:

        return False, str(e)


def restart_bot(bot_id):

    stop_bot(bot_id)

    time.sleep(0.5)

    return start_bot(bot_id)


def watch_process(bot_id):

    bot = get_bot(bot_id)

    if not bot:
        return

    auto_restart = bool(
        bot[6]
    )

    with process_lock:

        process = processes.get(
            int(bot_id)
        )

    if not process:
        return

    exit_code = process.wait()

    with process_lock:

        processes.pop(
            int(bot_id),
            None
        )

    write_log(
        bot_id,
        f"Process exited with code {exit_code}"
    )

    set_bot_status(
        bot_id,
        "stopped"
    )

    if auto_restart:

        time.sleep(2)

        current = get_bot(bot_id)

        if current and current[5] != "deleted":

            owner_id = int(
                current[1]
            )

            if client_enabled(owner_id):

                write_log(
                    bot_id,
                    "Auto-restarting..."
                )

                start_bot(
                    bot_id
                )


def stop_all_bots(owner_id):

    bots = get_client_bots(
        owner_id
    )

    for bot in bots:

        stop_bot(
            int(bot[0])
        )


def restart_all_bots(owner_id):

    bots = get_client_bots(
        owner_id
    )

    results = []

    for bot in bots:

        ok, msg = restart_bot(
            int(bot[0])
        )

        results.append(
            f"{bot[1]}: {msg}"
        )

    return results


# ============================================================
# KEYBOARDS
# ============================================================

def owner_menu():

    return {
        "inline_keyboard": [

            [
                {
                    "text": "👥 Clients",
                    "callback_data": "clients"
                },
                {
                    "text": "➕ Add Client",
                    "callback_data": "addclient"
                }
            ],

            [
                {
                    "text": "📊 All Bots",
                    "callback_data": "allbots"
                },
                {
                    "text": "📈 Host Status",
                    "callback_data": "hoststatus"
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

    send_message(
        chat_id,

        f"""👑 {BRAND}

OWNER CONTROL PANEL

🔐 Access: FULL OWNER

You can manage all clients
and all hosted bots.

Choose an option below:""",

        owner_menu()
    )


# ============================================================
# CLIENT PANEL
# ============================================================

def show_client(chat_id):

    send_message(
        chat_id,

        f"""🚀 {BRAND}

CLIENT PANEL

🟢 Access: ENABLED
🆔 Client ID: {chat_id}

You can manage only
your own hosted bots.

Choose an option:""",

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
            "👥 CLIENTS\n\nNo clients added yet."
        )

        return

    text = f"👥 {BRAND} CLIENTS\n\n"

    keyboard = []

    for client in clients:

        cid = client[0]
        status = client[1]

        icon = (
            "🟢"
            if status == "enabled"
            else "🔴"
        )

        text += (
            f"{icon} `{cid}` → "
            f"{status.upper()}\n"
        )

        keyboard.append(
            [
                {
                    "text": f"{icon} {cid}",
                    "callback_data":
                        f"client:{cid}"
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
# CLIENT MANAGEMENT PANEL
# ============================================================

def client_management(chat_id, client_id):

    clients = get_clients()

    found = None

    for c in clients:

        if int(c[0]) == int(client_id):

            found = c
            break

    if not found:

        send_message(
            chat_id,
            "❌ Client not found."
        )

        return

    status = found[1]

    keyboard = {
        "inline_keyboard": [

            [
                {
                    "text": "✅ Enable",
                    "callback_data":
                        f"enable:{client_id}"
                },
                {
                    "text": "🚫 Disable",
                    "callback_data":
                        f"disable:{client_id}"
                }
            ],

            [
                {
                    "text": "🤖 Bots",
                    "callback_data":
                        f"clientbots:{client_id}"
                }
            ],

            [
                {
                    "text": "❌ Remove",
                    "callback_data":
                        f"remove:{client_id}"
                }
            ]

        ]
    }

    send_message(
        chat_id,

        f"""👤 CLIENT

🆔 Chat ID: {client_id}
📊 Status: {status.upper()}

Select an action:""",

        keyboard
    )


# ============================================================
# BOT LIST
# ============================================================

def show_bots(chat_id, owner_id):

    bots = get_client_bots(
        owner_id
    )

    if not bots:

        send_message(
            chat_id,
            "🤖 No bots found."
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

        icon = (
            "🟢"
            if process_alive(bot_id)
            else "🔴"
        )

        text += (
            f"{icon} #{bot_id} "
            f"{name}\n"
            f"   📄 {filename}\n"
            f"   📊 {status}\n\n"
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

def bot_management(chat_id, bot_id):

    bot = get_bot(bot_id)

    if not bot:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    owner_id = int(bot[1])

    if not is_owner(chat_id):

        if int(chat_id) != owner_id:

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
                    "text": "📜 Logs",
                    "callback_data":
                        f"logs:{bot_id}"
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

👤 Owner: {owner_id}

🔁 Auto Restart:
{'ON' if bot[6] else 'OFF'}""",

        keyboard
    )


# ============================================================
# LOG VIEW
# ============================================================

def send_logs(chat_id, bot_id):

    bot = get_bot(bot_id)

    if not bot:

        send_message(
            chat_id,
            "❌ Bot not found."
        )

        return

    owner_id = int(bot[1])

    if not is_owner(chat_id):

        if int(chat_id) != owner_id:

            send_message(
                chat_id,
                "🚫 Access denied."
            )

            return

    path = log_file(bot_id)

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
# DOCUMENT UPLOAD
# ============================================================

def download_document(message, owner_id):

    document = message.get(
        "document"
    )

    if not document:

        return

    raw_filename = document.get("file_name", "bot.py")
    filename = validate_filename(raw_filename)
    file_size = document.get("file_size")

    if not filename:


        send_message(owner_id, "❌ Invalid filename. Sirf safe `.py` filename allowed hai.")
        return

    if file_size is not None and int(file_size) > MAX_UPLOAD_SIZE:
        send_message(owner_id, "❌ File too large. Maximum 2 MB allowed hai.")

        return

    file_id = document["file_id"]

    result = api(
        "getFile",
        {
            "file_id": file_id
        }
    )

    if not result.get("ok"):

        send_message(
            owner_id,
            "❌ Telegram file information nahi mil saki."
        )

        return

    telegram_path = result[
        "result"
    ]["file_path"]

    try:

        response = session.get(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{telegram_path}",
            timeout=60
        )

        if response.status_code != 200:

            send_message(
                owner_id,
                "❌ File download failed."
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

        folder = Path(
            bot[4]
        )

        safe_folder = safe_bot_folder(bot)
        if safe_folder is None:
            send_message(owner_id, "❌ Unsafe bot folder. Upload cancelled.")
            return
        target = (safe_folder / filename).resolve()
        target.relative_to(safe_folder)

        target.write_bytes(
            response.content
        )

        send_message(
            owner_id,

            f"""✅ BOT UPLOADED

🏷️ {BRAND}

🤖 Bot ID: {bot_id}
📛 Name: {name_without_ext}
📄 File: {filename}

📁 Client data:
{folder}

Now you can ▶️ Run the bot.""",

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

    except Exception as e:

        send_message(
            owner_id,
            f"❌ Upload error:\n{e}"
        )


# ============================================================
# CALLBACK HANDLER
# ============================================================

def handle_callback(callback):
    callback_id = callback.get("id")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if chat_id is not None and not check_rate_limit(chat_id, "callback"):
        answer_callback(callback_id, "⚠️ Too many requests.")
        return

    callback_id = callback["id"]

    answer_callback(
        callback_id
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

    # -----------------------------------------
    # GLOBAL AUTH CHECK
    # -----------------------------------------

    if not authorized(chat_id):

        send_message(
            chat_id,

            f"""🚫 ACCESS DENIED

{BRAND}

Your Chat ID is not authorized.

🆔 {chat_id}

Please contact the owner."""
        )

        return

    # -----------------------------------------
    # OWNER MENU
    # -----------------------------------------

    if data == "clients":

        if not is_owner(chat_id):
            return

        show_clients(
            chat_id
        )

        return


    if data == "addclient":

        if not is_owner(chat_id):
            return

        user_states[
            chat_id
        ] = "add_client"

        send_message(
            chat_id,

            """➕ ADD CLIENT

Send the client's Telegram Chat ID.

Example:

123456789

Send /cancel to cancel."""
        )

        return


    if data == "hoststatus":

        if not is_owner(chat_id):
            return

        running = 0
        total = 0

        with db_lock:

            total = db.execute(
                "SELECT COUNT(*) FROM bots"
            ).fetchone()[0]

        with process_lock:

            running = sum(
                1
                for p in processes.values()
                if p.poll() is None
            )

        uptime = int(
            time.time() - START_TIME
        )

        send_message(
            chat_id,

            f"""📊 {BRAND} HOST STATUS

🤖 Total Bots: {total}
🟢 Running: {running}
🔴 Stopped: {total - running}

👥 Clients:
{len(get_clients())}

⏱️ Host Uptime:
{uptime} seconds"""
        )

        return


    if data == "allbots":

        if not is_owner(chat_id):
            return

        bots_text = "📊 ALL BOTS\n\n"

        with db_lock:

            rows = db.execute(
                """
                SELECT id, owner_id, name, status
                FROM bots
                ORDER BY id
                """
            ).fetchall()

        if not rows:

            bots_text += "No bots."

        else:

            for row in rows:

                bots_text += (
                    f"#{row[0]} "
                    f"{row[2]}\n"
                    f"👤 Owner: {row[1]}\n"
                    f"📊 {row[3]}\n\n"
                )

        send_message(
            chat_id,
            bots_text
        )

        return


    if data == "runall":

        if not is_owner(chat_id):
            return

        with db_lock:

            rows = db.execute(
                "SELECT id FROM bots"
            ).fetchall()

        started = 0

        for row in rows:

            ok, _ = start_bot(
                row[0]
            )

            if ok:
                started += 1

        send_message(
            chat_id,
            f"▶️ Run All complete.\n\nStarted: {started}"
        )

        return


    if data == "stopall":

        if not is_owner(chat_id):
            return

        with db_lock:

            rows = db.execute(
                "SELECT id FROM bots"
            ).fetchall()

        stopped = 0

        for row in rows:

            ok, _ = stop_bot(
                row[0]
            )

            if ok:
                stopped += 1

        send_message(
            chat_id,
            f"🛑 Stop All complete.\n\nStopped: {stopped}"
        )

        return


    if data == "restartall":

        if not is_owner(chat_id):
            return

        with db_lock:

            rows = db.execute(
                "SELECT id FROM bots"
            ).fetchall()

        restarted = 0

        for row in rows:

            ok, _ = restart_bot(
                row[0]
            )

            if ok:
                restarted += 1

        send_message(
            chat_id,
            f"🔄 Restart All complete.\n\nRestarted: {restarted}"
        )

        return


    # -----------------------------------------
    # CLIENT MANAGEMENT
    # -----------------------------------------

    if data.startswith("client:"):

        if not is_owner(chat_id):
            return

        cid = int(
            data.split(":")[1]
        )

        client_management(
            chat_id,
            cid
        )

        return


    if data.startswith("enable:"):

        if not is_owner(chat_id):
            return

        cid = int(
            data.split(":")[1]
        )

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

        cid = int(
            data.split(":")[1]
        )

        set_client_status(
            cid,
            "disabled"
        )

        stop_all_bots(
            cid
        )

        send_message(
            chat_id,

            f"""🚫 Client disabled.

🆔 {cid}

All of this client's running bots
have been stopped."""
        )

        return


    if data.startswith("remove:"):

        if not is_owner(chat_id):
            return

        cid = int(
            data.split(":")[1]
        )

        remove_client(
            cid
        )

        send_message(
            chat_id,
            f"❌ Client {cid} removed."
        )

        return


    if data.startswith("clientbots:"):

        if not is_owner(chat_id):
            return

        cid = int(
            data.split(":")[1]
        )

        show_bots(
            chat_id,
            cid
        )

        return


    # -----------------------------------------
    # CLIENT BOT LIST
    # -----------------------------------------

    if data == "mybots":

        show_bots(
            chat_id,
            chat_id
        )

        return


    if data == "upload":

        user_states[
            chat_id
        ] = "upload"

        send_message(
            chat_id,

            f"""📤 {BRAND}

BOT UPLOAD

Send your Python `.py` file now.

Example:
mybot.py

The bot will be stored inside
your private client folder."""
        )

        return


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

        stop_all_bots(
            chat_id
        )

        send_message(
            chat_id,
            "🛑 All your bots stopped."
        )

        return


    if data == "myrestartall":

        results = restart_all_bots(
            chat_id
        )

        send_message(
            chat_id,
            "🔄 Restart All\n\n"
            + "\n".join(results)
        )

        return


    # -----------------------------------------
    # BOT CONTROL
    # -----------------------------------------

    if data.startswith("bot:"):

        bot_id = int(
            data.split(":")[1]
        )

        bot_management(
            chat_id,
            bot_id
        )

        return


    if data.startswith("run:"):

        bot_id = int(
            data.split(":")[1]
        )

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

        bot_id = int(
            data.split(":")[1]
        )

        bot = get_bot(
            bot_id
        )

        if not bot:
            return

        if not is_owner(chat_id):

            if int(bot[1]) != int(chat_id):
                return

        ok, msg = stop_bot(
            bot_id
        )

        send_message(
            chat_id,
            ("✅ " if ok else "❌ ")
            + msg
        )

        return


    if data.startswith("restart:"):

        bot_id = int(
            data.split(":")[1]
        )

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


    if data.startswith("logs:"):

        bot_id = int(
            data.split(":")[1]
        )

        send_logs(
            chat_id,
            bot_id
        )

        return


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_message(message):

    chat_id = int(
        message["chat"]["id"]
    )

    text = message.get(
        "text",
        ""
    )

    # -----------------------------------------
    # DOCUMENT
    # -----------------------------------------

    if "document" in message:

        if not authorized(chat_id):

            send_message(
                chat_id,
                f"🚫 {BRAND}\n\nAccess denied."
            )

            return

        download_document(
            message,
            chat_id
        )

        return


    # -----------------------------------------
    # CANCEL
    # -----------------------------------------

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


    # -----------------------------------------
    # START
    # -----------------------------------------

    if text.startswith("/start"):

        if is_owner(chat_id):

            show_owner(
                chat_id
            )

            return

        if client_enabled(chat_id):

            show_client(
                chat_id
            )

            return

        send_message(
            chat_id,

            f"""🚫 ACCESS DENIED

{BRAND}

Your Chat ID is not authorized.

🆔 {chat_id}

Contact the owner for access."""
        )

        return


    # -----------------------------------------
    # OWNER ADD CLIENT STATE
    # -----------------------------------------

    if user_states.get(chat_id) == "add_client":

        if not is_owner(chat_id):
            return

        if text.isdigit():

            client_id = int(
                text
            )

            if client_id == OWNER_CHAT_ID:

                send_message(
                    chat_id,
                    "❌ Owner ko client ke roop mein add karne ki zarurat nahi."
                )

                return

            add_client(
                client_id
            )

            user_states.pop(
                chat_id,
                None
            )

            send_message(
                chat_id,

                f"""✅ CLIENT ADDED

🆔 Chat ID: {client_id}
📊 Status: ENABLED

The client can now access
{BRAND}."""
            )

        else:

            send_message(
                chat_id,
                "❌ Invalid Chat ID.\n\nOnly numbers allowed."

            )

        return


    # -----------------------------------------
    # NORMAL AUTH
    # -----------------------------------------

    if not authorized(chat_id):

        send_message(
            chat_id,

            f"""🚫 ACCESS DENIED

{BRAND}

You are not an authorized client.

🆔 Chat ID: {chat_id}"""
        )

        return


    # -----------------------------------------
    # COMMANDS
    # -----------------------------------------

    if text == "/panel":

        if is_owner(chat_id):

            show_owner(
                chat_id
            )

        else:

            show_client(
                chat_id
            )

        return


    if text == "/clients":

        if is_owner(chat_id):

            show_clients(
                chat_id
            )

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

    bot_username = result[
        "result"
    ].get(
        "username"
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

                data[
                    "offset"
                ] = offset

            result = api(
                "getUpdates",
                data,
                timeout=12
            )

            if not result.get("ok"):

                print(
                    "⚠️ Telegram error:",
                    result
                )

                time.sleep(1)

                continue

            for update in result.get(
                "result",
                []
            ):

                offset = (
                    update[
                        "update_id"
                    ] + 1
                )

                try:

                    if "message" in update:

                        handle_message(
                            update[
                                "message"
                            ]
                        )

                    elif "callback_query" in update:

                        handle_callback(
                            update[
                                "callback_query"
                            ]
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
                "🔄 Reconnecting:",
                repr(e)
            )

            time.sleep(1)


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        print(
            "❌ BOT_TOKEN set karo."
        )

        return

    if not OWNER_CHAT_ID:

        print(
            "❌ OWNER_CHAT_ID set karo."
        )

        return

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

    polling()


if __name__ == "__main__":

    main()
