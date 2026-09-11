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
DATA_DIR = BASE_DIR / "host_data"
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

    return int(chat_id) == int(OWNER_CHAT_ID)


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

    folder = (
        client_folder(owner_id)
        / f"bot_{bot_id}"
    )

    folder.mkdir(
        parents=True,
        exist_ok=True
    )

    return folder


def safe_filename(filename):

    filename = os.path.basename(filename)

    filename = re.sub(
        r"[^a-zA-Z0-9_.-]",
        "_",
        filename
    )

    if not filename:

        filename = "bot.py"

    return filename


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

    script = folder / filename

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

    filename = safe_filename(
        document.get(
            "file_name",
            "bot.py"
        )
    )

    if not filename.lower().endswith(
        ".py"
    ):

        send_message(
            owner_id,
            "❌ Sirf `.py` files upload karo."
        )

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

        target = folder / filename

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

    if (
        BOT_TOKEN
        == "PUT_YOUR_HOST_BOT_TOKEN_HERE"
    ):

        print(
            "❌ BOT_TOKEN set karo."
        )

        return

    if (
        not OWNER_CHAT_ID
        or OWNER_CHAT_ID == 123456789
    ):

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



# ============================================================
# FINAL ADDITIONS / COMPATIBILITY LAYER
# Existing original panel/features are preserved; these are ADDITIONS.
# ============================================================

# Render environment support
BOT_TOKEN = os.getenv("BOT_TOKEN", BOT_TOKEN).strip()
try:
    OWNER_CHAT_ID = int(os.getenv("OWNER_CHAT_ID", str(OWNER_CHAT_ID)).strip())
except Exception:
    OWNER_CHAT_ID = 0

DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR / "host_data"))).resolve()
CLIENTS_DIR = DATA_DIR / "clients"
DB_FILE = DATA_DIR / "host.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Re-open SQLite at the configured DATA_DIR (the original file may have
# been created before DATA_DIR was overridden).
try:
    db.close()
except Exception:
    pass
db = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=30)
db.row_factory = sqlite3.Row
db.execute("CREATE TABLE IF NOT EXISTS clients (chat_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'enabled', added_at REAL NOT NULL)")
db.execute("CREATE TABLE IF NOT EXISTS bots (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, name TEXT NOT NULL, filename TEXT NOT NULL, folder TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'stopped', auto_restart INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)")
db.commit()

# New persisted settings/columns. Existing database is migrated automatically.
with db_lock:
    try:
        db.execute("ALTER TABLE clients ADD COLUMN username TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE clients ADD COLUMN first_name TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE clients ADD COLUMN last_name TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE clients ADD COLUMN notified INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        db.execute("ALTER TABLE bots ADD COLUMN desired_running INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.commit()


def setting_get(key, default=""):
    with db_lock:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def setting_set(key, value):
    with db_lock:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        db.commit()


def global_locked():
    return setting_get("global_lock", "0") == "1"


def set_global_lock(value):
    setting_set("global_lock", "1" if value else "0")


def control_allowed(chat_id, action=True):
    if is_owner(chat_id):
        return True
    if global_locked():
        send_message(chat_id, f"🔒 {BRAND}\n\nHosting is temporarily locked by the owner.\nPlease try again later.")
        return False
    if not client_enabled(chat_id):
        send_message(chat_id, f"🚫 {BRAND}\n\nYour hosting access is disabled.\n\n🆔 {chat_id}")
        return False
    return True


def update_client_profile(message):
    user = message.get("from", {}) or {}
    cid = int(message["chat"]["id"])
    username = (user.get("username") or "").strip()
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    with db_lock:
        db.execute("UPDATE clients SET username=?, first_name=?, last_name=? WHERE chat_id=?", (username, first_name, last_name, cid))
        db.commit()


def register_client_if_needed(message):
    cid = int(message["chat"]["id"])
    if is_owner(cid):
        return False
    user = message.get("from", {}) or {}
    username = (user.get("username") or "").strip()
    first_name = (user.get("first_name") or "").strip()
    last_name = (user.get("last_name") or "").strip()
    now = time.time()
    is_new = False
    with db_lock:
        row = db.execute("SELECT chat_id, notified FROM clients WHERE chat_id=?", (cid,)).fetchone()
        if row is None:
            db.execute("INSERT INTO clients(chat_id,username,first_name,last_name,status,added_at,notified) VALUES(?,?,?,?,?,?,0)", (cid,username,first_name,last_name,"enabled",now))
            db.commit()
            is_new = True
        else:
            db.execute("UPDATE clients SET username=?, first_name=?, last_name=? WHERE chat_id=?", (username,first_name,last_name,cid))
            db.commit()
    client_folder(cid)
    if is_new:
        username_text = f"@{username}" if username else "(no username)"
        name_text = " ".join(x for x in [first_name,last_name] if x).strip() or "(no name)"
        send_message(OWNER_CHAT_ID, f"🆕 NEW CLIENT\n\n👤 Username: {username_text}\n📛 Name: {name_text}\n🆔 Chat ID: {cid}\n🕒 Time: {time.strftime('%Y-%m-%d %H:%M:%S')}", {"inline_keyboard":[[{"text":"👤 Open Client","callback_data":f"client:{cid}"}]]})
        with db_lock:
            db.execute("UPDATE clients SET notified=1 WHERE chat_id=?", (cid,))
            db.commit()
    return is_new


def add_client(chat_id):
    with db_lock:
        db.execute("INSERT OR IGNORE INTO clients(chat_id,status,added_at,username,first_name,last_name,notified) VALUES(?, 'enabled', ?, '', '', '', 0)", (int(chat_id), time.time()))
        db.commit()
    client_folder(int(chat_id))


def get_clients():
    with db_lock:
        return db.execute("SELECT chat_id, status, added_at, username, first_name, last_name FROM clients ORDER BY added_at").fetchall()


def set_bot_desired(bot_id, value):
    with db_lock:
        db.execute("UPDATE bots SET desired_running=? WHERE id=?", (1 if value else 0, int(bot_id)))
        db.commit()


def get_bot(bot_id):
    with db_lock:
        row = db.execute("SELECT id,owner_id,name,filename,folder,status,auto_restart,desired_running FROM bots WHERE id=?", (int(bot_id),)).fetchone()
    return row


def get_client_bots(owner_id):
    with db_lock:
        return db.execute("SELECT id,name,filename,status,auto_restart,desired_running FROM bots WHERE owner_id=? ORDER BY id", (int(owner_id),)).fetchall()


def _kill_process_tree(process):
    if not process:
        return
    try:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except Exception:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except Exception:
                    process.kill()
                try:
                    process.wait(timeout=3)
                except Exception:
                    pass
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    except Exception:
        pass


def _imports_for_script(script):
    try:
        tree = ast.parse(script.read_text(encoding="utf-8", errors="replace"), filename=str(script))
    except Exception:
        return set()
    found=set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                found.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split('.')[0])
    return found


IMPORT_TO_PACKAGE = {
    "telegram":"python-telegram-bot>=22,<23", "openai":"openai>=1.50,<2", "requests":"requests>=2.32,<3", "httpx":"httpx", "aiohttp":"aiohttp", "flask":"Flask", "fastapi":"fastapi", "uvicorn":"uvicorn", "bs4":"beautifulsoup4", "PIL":"Pillow", "cv2":"opencv-python", "dotenv":"python-dotenv", "yaml":"PyYAML", "Crypto":"pycryptodome", "numpy":"numpy", "pandas":"pandas", "qrcode":"qrcode", "schedule":"schedule", "rich":"rich", "colorama":"colorama", "selenium":"selenium", "jwt":"PyJWT", "google":"google-api-python-client", "discord":"discord.py", "psutil":"psutil"
}
try:
    STDLIB = set(sys.stdlib_module_names)
except Exception:
    STDLIB = set()


def _venv_python(folder):
    return folder / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def prepare_dependencies(bot_id):
    bot=get_bot(bot_id)
    if not bot: return False, "Bot not found."
    folder=Path(bot[4]); script=folder/bot[3]
    try:
        compile(script.read_text(encoding="utf-8",errors="replace"), str(script), "exec")
    except SyntaxError as e:
        return False, f"Syntax error: line {e.lineno}: {e.msg}"
    py=_venv_python(folder)
    if not py.exists():
        r=subprocess.run([sys.executable,"-m","venv",str(folder/".venv")],cwd=str(folder),capture_output=True,text=True,timeout=180)
        if r.returncode != 0: return False, (r.stderr or r.stdout or "venv creation failed")[-1500:]
    imports=_imports_for_script(script)
    packages=[]
    local_names={p.stem for p in folder.glob("*.py")}
    for mod in sorted(imports):
        if mod in STDLIB or mod in local_names: continue
        pkg=IMPORT_TO_PACKAGE.get(mod)
        if pkg and pkg not in packages: packages.append(pkg)
    marker=folder/".deps.done"
    signature="\n".join(packages)
    if marker.exists() and marker.read_text(errors="ignore") == signature:
        return True, "Dependencies ready."
    if packages:
        cmd=[str(py),"-m","pip","install","--disable-pip-version-check","-q"]+packages
        r=subprocess.run(cmd,cwd=str(folder),capture_output=True,text=True,timeout=600)
        if r.returncode != 0: return False, (r.stderr or r.stdout or "dependency installation failed")[-2500:]
    marker.write_text(signature,encoding="utf-8")
    return True, "Dependencies ready."


def start_bot(bot_id):
    bot=get_bot(bot_id)
    if not bot: return False,"Bot not found."
    bot_id=int(bot[0]); owner_id=int(bot[1]); folder=Path(bot[4]); script=folder/bot[3]
    if not is_owner(owner_id) and not client_enabled(owner_id): return False,"Client is disabled."
    if process_alive(bot_id): return False,"Bot is already running."
    if not script.exists(): return False,"Python file not found."
    ok,msg=prepare_dependencies(bot_id)
    if not ok: write_log(bot_id,"DEPENDENCY ERROR: "+msg); return False,msg
    py=_venv_python(folder)
    log=log_file(bot_id)
    try:
        log.parent.mkdir(parents=True,exist_ok=True)
        with open(log,"a",encoding="utf-8",errors="replace") as h:
            h.write("\n\n"+"="*60+f"\nSTARTING BOT {bot_id}\n"+"="*60+"\n")
            kwargs=dict(cwd=str(folder),stdout=h,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL)
            if os.name=="posix": kwargs["start_new_session"]=True
            process=subprocess.Popen([str(py),"-u",str(script)],**kwargs)
        with process_lock: processes[bot_id]=process
        set_bot_status(bot_id,"running"); set_bot_desired(bot_id,True)
        write_log(bot_id,f"PID: {process.pid}")
        t=threading.Thread(target=watch_process,args=(bot_id,),daemon=True); watchers[bot_id]=t; t.start()
        return True,f"Bot started. PID: {process.pid}"
    except Exception as e:
        write_log(bot_id,f"START ERROR: {repr(e)}"); return False,str(e)


def stop_bot(bot_id, intentional=True):
    bot_id=int(bot_id)
    if intentional: set_bot_desired(bot_id,False)
    with process_lock: process=processes.get(bot_id)
    if not process:
        set_bot_status(bot_id,"stopped")
        return False,"Bot is not running."
    _kill_process_tree(process)
    with process_lock: processes.pop(bot_id,None)
    set_bot_status(bot_id,"stopped"); write_log(bot_id,"Bot stopped.")
    return True,"Bot stopped."


def restart_bot(bot_id):
    stop_bot(bot_id, intentional=True)
    time.sleep(.5)
    return start_bot(bot_id)


def watch_process(bot_id):
    with process_lock: process=processes.get(int(bot_id))
    if not process: return
    code=process.wait()
    with process_lock: processes.pop(int(bot_id),None)
    write_log(bot_id,f"Process exited with {code}")
    bot=get_bot(bot_id)
    if not bot: return
    set_bot_status(bot_id,"stopped")
    desired=bool(bot[7]) if len(bot)>=8 else True
    if code != 0 and bool(bot[6]) and desired and client_enabled(int(bot[1])):
        time.sleep(2)
        current=get_bot(bot_id)
        if current and bool(current[7]):
            write_log(bot_id,"Auto-restarting after crash...")
            start_bot(bot_id)


def stop_all_bots(owner_id):
    bots=get_client_bots(owner_id); count=0
    for b in bots:
        ok,_=stop_bot(int(b[0]),intentional=True)
        if ok: count+=1
    return count


def restart_all_bots(owner_id):
    results=[]
    for b in get_client_bots(owner_id):
        ok,msg=restart_bot(int(b[0])); results.append(f"{b[1]}: {msg}")
    return results


def permanently_delete_bot(bot_id):
    bot=get_bot(bot_id)
    if not bot: return False,"Bot not found."
    stop_bot(bot_id, intentional=True)
    folder=Path(bot[4])
    with db_lock:
        db.execute("DELETE FROM bots WHERE id=?",(int(bot_id),)); db.commit()
    try:
        if folder.exists(): shutil.rmtree(folder,ignore_errors=True)
    except Exception as e: return False,f"Database deleted but folder cleanup failed: {e}"
    return True,"Bot permanently deleted."


def remove_client(chat_id):
    cid=int(chat_id); stop_all_bots(cid)
    with db_lock:
        db.execute("DELETE FROM bots WHERE owner_id=?",(cid,)); db.execute("DELETE FROM clients WHERE chat_id=?",(cid,)); db.commit()
    folder=CLIENTS_DIR/str(cid)
    if folder.exists(): shutil.rmtree(folder,ignore_errors=True)


def show_owner(chat_id):
    send_message(chat_id,f"""👑 {BRAND}

OWNER CONTROL PANEL

🔐 Access: FULL OWNER

You can manage all clients
and all hosted bots.

Choose an option below:""",owner_menu())


def owner_menu():
    return {"inline_keyboard":[
        [{"text":"👥 Clients","callback_data":"clients"},{"text":"➕ Add Client","callback_data":"addclient"}],
        [{"text":"📊 All Bots","callback_data":"allbots"},{"text":"📈 Host Status","callback_data":"hoststatus"}],
        [{"text":"▶️ Run All","callback_data":"runall"},{"text":"🛑 Stop All","callback_data":"stopall"}],
        [{"text":"🔄 Restart All","callback_data":"restartall"}],
        [{"text":"🔒 Lock All Clients","callback_data":"lockall"},{"text":"🔓 Unlock All Clients","callback_data":"unlockall"}],
        [{"text":"🔐 Hosting Access","callback_data":"access"}]
    ]}


def client_menu():
    return {"inline_keyboard":[
        [{"text":"🤖 My Bots","callback_data":"mybots"}],
        [{"text":"📤 Upload .py","callback_data":"upload"}],
        [{"text":"📊 Status","callback_data":"mystatus"}],
        [{"text":"🔄 Refresh","callback_data":"refresh"}],
        [{"text":"▶️ Run All","callback_data":"myrunall"},{"text":"🛑 Stop All","callback_data":"mystopall"}],
        [{"text":"🔄 Restart All","callback_data":"myrestartall"}]
    ]}


def show_client(chat_id):
    bots=get_client_bots(chat_id); running=sum(1 for b in bots if process_alive(int(b[0])))
    username=""
    with db_lock:
        row=db.execute("SELECT username,first_name,last_name FROM clients WHERE chat_id=?",(int(chat_id),)).fetchone()
    if row:
        username=("@"+row[0]) if row[0] else (" ".join(x for x in [row[1],row[2]] if x) or str(chat_id))
    send_message(chat_id,f"""🤖 {BRAND}

Your bots: {len(bots)}
Running: {running}

Hosting Panel ready.

Upload one .py file to create a bot.""",client_menu())


def show_clients(chat_id):
    clients=get_clients()
    if not clients: send_message(chat_id,"👥 CLIENTS\n\nNo clients added yet."); return
    text=f"👥 {BRAND} CLIENTS\n\n"; keyboard=[]
    for c in clients:
        cid,status=c[0],c[1]; username=c[3] if len(c)>3 else ""
        name=" ".join(x for x in [c[4] if len(c)>4 else "",c[5] if len(c)>5 else ""] if x)
        label=f"@{username}" if username else (name or str(cid)); icon="🟢" if status=="enabled" else "🔴"
        count=len(get_client_bots(cid)); text+=f"{icon} {label}\n   🆔 {cid}\n   🤖 Bots: {count}\n   📊 {status.upper()}\n\n"
        keyboard.append([{"text":f"{icon} {label}","callback_data":f"client:{cid}"}])
    send_message(chat_id,text,{"inline_keyboard":keyboard})


def client_management(chat_id,client_id):
    with db_lock: found=db.execute("SELECT chat_id,status,username,first_name,last_name FROM clients WHERE chat_id=?",(int(client_id),)).fetchone()
    if not found: send_message(chat_id,"❌ Client not found."); return
    label=("@"+found[2]) if found[2] else (" ".join(x for x in [found[3],found[4]] if x) or "No username")
    status=found[1]; lock="LOCKED" if global_locked() else "UNLOCKED"
    send_message(chat_id,f"""👤 CLIENT

👤 User: {label}
🆔 Chat ID: {client_id}
📊 Status: {status.upper()}
🔐 Global Lock: {lock}
🤖 Bots: {len(get_client_bots(client_id))}

Select an action:""",{"inline_keyboard":[
        [{"text":"🤖 Bots","callback_data":f"clientbots:{client_id}"}],
        [{"text":"✅ Enable","callback_data":f"enable:{client_id}"},{"text":"🚫 Disable","callback_data":f"disable:{client_id}"}],
        [{"text":"❌ Permanent Delete","callback_data":f"remove:{client_id}"}],
        [{"text":"⬅️ Clients","callback_data":"clients"}]
    ]})


def show_bots(chat_id,owner_id):
    bots=get_client_bots(owner_id)
    if not bots: send_message(chat_id,"🤖 No bots found."); return
    text=f"🤖 {BRAND} BOTS\n\n"; keyboard=[]
    for b in bots:
        bid,name,filename,status=b[0],b[1],b[2],b[3]; icon="🟢" if process_alive(bid) else "🔴"
        text+=f"{icon} #{bid} {name}\n   📄 {filename}\n   📊 {status}\n\n"
        keyboard.append([{"text":f"🤖 #{bid} {name}","callback_data":f"bot:{bid}"}])
    send_message(chat_id,text,{"inline_keyboard":keyboard})


def bot_management(chat_id,bot_id):
    bot=get_bot(bot_id)
    if not bot: send_message(chat_id,"❌ Bot not found."); return
    owner=int(bot[1])
    if not is_owner(chat_id) and int(chat_id)!=owner: send_message(chat_id,"🚫 Access denied."); return
    running=process_alive(bot_id)
    send_message(chat_id,f"""🤖 BOT CONTROL

🆔 Bot ID: {bot_id}
📛 Name: {bot[2]}
📄 File: {bot[3]}

📊 Status: {'🟢 RUNNING' if running else '🔴 STOPPED'}
👤 Owner: {owner}
🔁 Auto Restart: {'ON' if bot[6] else 'OFF'}""",{"inline_keyboard":[
        [{"text":"▶️ Run","callback_data":f"run:{bot_id}"},{"text":"🛑 Stop","callback_data":f"stop:{bot_id}"}],
        [{"text":"🔄 Restart","callback_data":f"restart:{bot_id}"}],
        [{"text":"📜 Logs","callback_data":f"logs:{bot_id}"}],
        [{"text":"🗑️ Permanent Delete","callback_data":f"deletebot:{bot_id}"}]
    ]})


def show_access(chat_id):
    if not is_owner(chat_id): return
    clients=get_clients(); text="🔐 HOSTING ACCESS\n\nTap a client to enable/disable access.\n\n"; kb=[]
    for c in clients:
        cid,status=c[0],c[1]; u=c[3] if len(c)>3 else ""; label=("@"+u) if u else str(cid); icon="🟢" if status=="enabled" else "🔴"
        kb.append([{"text":f"{icon} {label}","callback_data":f"accessclient:{cid}"}])
    send_message(chat_id,text,{"inline_keyboard":kb})


def show_access_client(chat_id,cid):
    if not is_owner(chat_id): return
    with db_lock: row=db.execute("SELECT status,username,first_name,last_name FROM clients WHERE chat_id=?",(int(cid),)).fetchone()
    if not row: send_message(chat_id,"❌ Client not found."); return
    label=("@"+row[1]) if row[1] else (" ".join(x for x in [row[2],row[3]] if x) or str(cid))
    send_message(chat_id,f"🔐 HOSTING ACCESS\n\n👤 {label}\n🆔 {cid}\n📊 {row[0].upper()}",{"inline_keyboard":[[{"text":"✅ Enable","callback_data":f"enable:{cid}"},{"text":"🚫 Disable","callback_data":f"disable:{cid}"}],[{"text":"⬅️ Access List","callback_data":"access"}]]})


def permanent_delete_confirm(chat_id,kind,ident):
    user_states[int(chat_id)]={"state":"delete_confirm","kind":kind,"id":int(ident)}
    send_message(chat_id,f"⚠️ PERMANENT DELETE\n\nThis action is IRREVERSIBLE.\nThe selected {kind} and its stored files/logs will be permanently deleted.\n\nType exactly: CONFIRM\n\nOr send /cancel to cancel.")


def run_all_for_owner(owner_id):
    count=0
    for b in get_client_bots(owner_id):
        ok,_=start_bot(int(b[0]))
        if ok: count+=1
    return count


def render_health_server():
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b"KRUTIK CYBER EXPERT HOST OK\n"
            self.send_response(200); self.send_header("Content-Type","text/plain; charset=utf-8"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self,*args): pass
    port=int(os.getenv("PORT","10000"))
    server=ThreadingHTTPServer(("0.0.0.0",port),HealthHandler)
    print(f"🌐 Render health server listening on 0.0.0.0:{port}")
    server.serve_forever()


def recover_bots():
    with db_lock:
        rows=db.execute("SELECT id,owner_id,desired_running FROM bots WHERE desired_running=1").fetchall()
    for r in rows:
        if client_enabled(int(r[1])):
            threading.Thread(target=lambda bid=int(r[0]): start_bot(bid),daemon=True).start()


def handle_callback(callback):
    callback_id=callback.get("id"); answer_callback(callback_id)
    message=callback.get("message",{}); chat=message.get("chat",{}); chat_id=chat.get("id"); data=callback.get("data","")
    if not authorized(chat_id):
        send_message(chat_id,f"🚫 ACCESS DENIED\n\n{BRAND}\n\nYour Chat ID is not authorized.\n\n🆔 {chat_id}\n\nPlease contact the owner."); return
    if not is_owner(chat_id) and global_locked():
        # Owner retains full access. Registration/notification is unaffected; controls are blocked.
        send_message(chat_id,f"🔒 {BRAND}\n\nAll client hosting access is temporarily locked by the owner."); return
    if data=="clients" and is_owner(chat_id): show_clients(chat_id); return
    if data=="addclient" and is_owner(chat_id): user_states[chat_id]="add_client"; send_message(chat_id,"➕ ADD CLIENT\n\nSend the client's Telegram Chat ID.\n\nExample:\n123456789\n\nSend /cancel to cancel."); return
    if data=="hoststatus" and is_owner(chat_id):
        with db_lock: total=db.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        with process_lock: running=sum(1 for p in processes.values() if p.poll() is None)
        send_message(chat_id,f"📊 {BRAND} HOST STATUS\n\n🤖 Total Bots: {total}\n🟢 Running: {running}\n🔴 Stopped: {total-running}\n\n👥 Clients: {len(get_clients())}\n\n⏱️ Host Uptime: {int(time.time()-START_TIME)} seconds"); return
    if data=="allbots" and is_owner(chat_id):
        rows=[]
        with db_lock: rows=db.execute("SELECT id,owner_id,name,status FROM bots ORDER BY id").fetchall()
        text="📊 ALL BOTS\n\n"+("No bots." if not rows else "".join(f"#{r[0]} {r[2]}\n👤 Owner: {r[1]}\n📊 {r[3]}\n\n" for r in rows)); send_message(chat_id,text); return
    if data=="runall" and is_owner(chat_id): send_message(chat_id,f"▶️ Run All complete.\n\nStarted: {sum(1 for b in get_client_bots(0) if False) if False else run_all_global()}"); return
    if data=="stopall" and is_owner(chat_id):
        with db_lock: ids=[r[0] for r in db.execute("SELECT id FROM bots").fetchall()]
        stopped=sum(1 for bid in ids if stop_bot(bid,True)[0]); send_message(chat_id,f"🛑 Stop All complete.\n\nStopped: {stopped}"); return
    if data=="restartall" and is_owner(chat_id):
        with db_lock: ids=[r[0] for r in db.execute("SELECT id FROM bots").fetchall()]
        n=sum(1 for bid in ids if restart_bot(bid)[0]); send_message(chat_id,f"🔄 Restart All complete.\n\nRestarted: {n}"); return
    if data=="lockall" and is_owner(chat_id): set_global_lock(True); send_message(chat_id,"🔒 All Clients LOCKED.\n\nOwner access remains active."); return
    if data=="unlockall" and is_owner(chat_id): set_global_lock(False); send_message(chat_id,"🔓 All Clients UNLOCKED."); return
    if data=="access" and is_owner(chat_id): show_access(chat_id); return
    if data.startswith("accessclient:") and is_owner(chat_id): show_access_client(chat_id,int(data.split(":",1)[1])); return
    if data.startswith("client:") and is_owner(chat_id): client_management(chat_id,int(data.split(":",1)[1])); return
    if data.startswith("enable:") and is_owner(chat_id): set_client_status(int(data.split(":",1)[1]),"enabled"); send_message(chat_id,"✅ Client ENABLED."); return
    if data.startswith("disable:") and is_owner(chat_id): cid=int(data.split(":",1)[1]); set_client_status(cid,"disabled"); stop_all_bots(cid); send_message(chat_id,f"🚫 Client disabled.\n\n🆔 {cid}\n\nAll of this client's running bots have been stopped."); return
    if data.startswith("remove:") and is_owner(chat_id): permanent_delete_confirm(chat_id,"client",int(data.split(":",1)[1])); return
    if data.startswith("clientbots:") and is_owner(chat_id): show_bots(chat_id,int(data.split(":",1)[1])); return
    if data=="mybots": show_bots(chat_id,chat_id); return
    if data=="upload": user_states[chat_id]="upload"; send_message(chat_id,f"📤 {BRAND}\n\nBOT UPLOAD\n\nSend your Python `.py` file now.\n\nExample:\nmybot.py\n\nThe bot will be stored inside\nyour private client folder."); return
    if data in ("mystatus","refresh"): show_client(chat_id) if data=="refresh" else send_message(chat_id,f"📊 YOUR STATUS\n\n🤖 Total Bots: {len(get_client_bots(chat_id))}\n🟢 Running: {sum(1 for b in get_client_bots(chat_id) if process_alive(b[0]))}\n🔴 Stopped: {sum(1 for b in get_client_bots(chat_id) if not process_alive(b[0]))}\n\n🏷️ {BRAND}"); return
    if data=="myrunall": send_message(chat_id,f"▶️ Started {run_all_for_owner(chat_id)} bot(s)."); return
    if data=="mystopall": send_message(chat_id,f"🛑 Stopped {stop_all_bots(chat_id)} bot(s)."); return
    if data=="myrestartall": send_message(chat_id,"🔄 Restart All\n\n"+"\n".join(restart_all_bots(chat_id))); return
    if data.startswith("bot:"):
        bid=int(data.split(":",1)[1]); bot=get_bot(bid)
        if bot and (is_owner(chat_id) or int(bot[1])==int(chat_id)): bot_management(chat_id,bid)
        return
    if data.startswith("run:"):
        bid=int(data.split(":",1)[1]); bot=get_bot(bid)
        if bot and (is_owner(chat_id) or int(bot[1])==int(chat_id)):
            ok,msg=start_bot(bid); send_message(chat_id,("✅ " if ok else "❌ ")+msg)
        return
    if data.startswith("stop:"):
        bid=int(data.split(":",1)[1]); bot=get_bot(bid)
        if bot and (is_owner(chat_id) or int(bot[1])==int(chat_id)):
            ok,msg=stop_bot(bid,True); send_message(chat_id,("✅ " if ok else "❌ ")+msg)
        return
    if data.startswith("restart:"):
        bid=int(data.split(":",1)[1]); bot=get_bot(bid)
        if bot and (is_owner(chat_id) or int(bot[1])==int(chat_id)):
            ok,msg=restart_bot(bid); send_message(chat_id,("✅ " if ok else "❌ ")+msg)
        return
    if data.startswith("logs:"): send_logs(chat_id,int(data.split(":",1)[1])); return
    if data.startswith("deletebot:"):
        bid=int(data.split(":",1)[1]); bot=get_bot(bid)
        if bot and (is_owner(chat_id) or int(bot[1])==int(chat_id)): permanent_delete_confirm(chat_id,"bot",bid)
        return


def run_all_global():
    with db_lock: ids=[r[0] for r in db.execute("SELECT id FROM bots").fetchall()]
    n=0
    for bid in ids:
        if start_bot(bid)[0]: n+=1
    return n


def download_document(message, owner_id):
    if not control_allowed(owner_id): return
    document=message.get("document") or {}; filename=safe_filename(document.get("file_name","bot.py"))
    if not filename.lower().endswith(".py"): send_message(owner_id,"❌ Sirf `.py` files upload karo."); return
    if int(document.get("file_size",0) or 0)>MAX_UPLOAD_SIZE: send_message(owner_id,"❌ File 2 MB se badi hai."); return
    result=api("getFile",{"file_id":document.get("file_id")})
    if not result.get("ok"): send_message(owner_id,"❌ Telegram file information nahi mil saki."); return
    try:
        response=session.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{result['result']['file_path']}",timeout=60)
        if response.status_code!=200 or len(response.content)>MAX_UPLOAD_SIZE: send_message(owner_id,"❌ File download failed or file too large."); return
        name=Path(filename).stem; bid=create_bot(owner_id,name,filename); bot=get_bot(bid); folder=Path(bot[4]); target=folder/filename; target.write_bytes(response.content)
        ok,msg=prepare_dependencies(bid)
        if not ok:
            send_message(owner_id,f"❌ BOT UPLOAD\n\nBot #{bid} created, but dependency/syntax preparation failed:\n{msg}", {"inline_keyboard":[[{"text":"🤖 Bot Control","callback_data":f"bot:{bid}"}]]}); return
        send_message(owner_id,f"""✅ BOT UPLOADED

🏷️ {BRAND}

🤖 Bot ID: {bid}
📛 Name: {name}
📄 File: {filename}

📁 Client data:
{folder}

Now you can ▶️ Run the bot.""",{"inline_keyboard":[[{"text":"▶️ Run Now","callback_data":f"run:{bid}"}],[{"text":"🤖 Bot Control","callback_data":f"bot:{bid}"}] ]})
    except Exception as e: send_message(owner_id,f"❌ Upload error:\n{e}")


def handle_message(message):
    chat_id=int(message["chat"]["id"]); text=message.get("text","")
    if not is_owner(chat_id): register_client_if_needed(message)
    if "document" in message:
        if not authorized(chat_id): send_message(chat_id,f"🚫 {BRAND}\n\nAccess denied."); return
        download_document(message,chat_id); return
    if text=="/cancel": user_states.pop(chat_id,None); send_message(chat_id,"❌ Current operation cancelled."); return
    if text.startswith("/start"):
        if is_owner(chat_id): show_owner(chat_id); return
        if global_locked(): send_message(chat_id,f"🔒 {BRAND}\n\nYour client access is currently locked by the owner."); return
        if client_enabled(chat_id): show_client(chat_id); return
        send_message(chat_id,f"🚫 ACCESS DENIED\n\n{BRAND}\n\nYour hosting access is disabled.\n\n🆔 {chat_id}\n\nContact the owner for access."); return
    state=user_states.get(chat_id)
    if isinstance(state,dict) and state.get("state")=="delete_confirm":
        if text.strip()=="CONFIRM":
            kind=state["kind"]; ident=state["id"]
            user_states.pop(chat_id,None)
            if kind=="bot":
                bot=get_bot(ident)
                if bot and (is_owner(chat_id) or int(bot[1])==chat_id):
                    ok,msg=permanently_delete_bot(ident); send_message(chat_id,("✅ " if ok else "❌ ")+msg)
                else: send_message(chat_id,"❌ Bot not found or access denied.")
            else:
                if is_owner(chat_id): remove_client(ident); send_message(chat_id,"✅ Client and all of its bots/files/logs permanently deleted.")
            return
        send_message(chat_id,"⚠️ Confirmation not received. Type exactly CONFIRM, or /cancel."); return
    if state=="add_client":
        if not is_owner(chat_id): return
        if text.isdigit() and int(text)!=OWNER_CHAT_ID:
            add_client(int(text)); user_states.pop(chat_id,None); send_message(chat_id,f"✅ CLIENT ADDED\n\n🆔 Chat ID: {text}\n📊 Status: ENABLED\n\nThe client can now access\n{BRAND}.")
        else: send_message(chat_id,"❌ Invalid Chat ID.\n\nOnly numbers allowed and owner ID cannot be added as client.")
        return
    if not authorized(chat_id): send_message(chat_id,f"🚫 ACCESS DENIED\n\n{BRAND}\n\nYou are not an authorized client.\n\n🆔 Chat ID: {chat_id}"); return
    if not is_owner(chat_id) and global_locked(): send_message(chat_id,f"🔒 {BRAND}\n\nAll client hosting access is temporarily locked by the owner."); return
    if text=="/panel": show_owner(chat_id) if is_owner(chat_id) else show_client(chat_id); return
    if text=="/clients" and is_owner(chat_id): show_clients(chat_id); return
    if text=="/bots": show_bots(chat_id,chat_id); return
    if text=="/status": send_message(chat_id,f"📊 YOUR STATUS\n\n👤 Client ID: {chat_id}\n\n🤖 Total Bots: {len(get_client_bots(chat_id))}\n🟢 Running: {sum(1 for b in get_client_bots(chat_id) if process_alive(b[0]))}\n🔴 Stopped: {sum(1 for b in get_client_bots(chat_id) if not process_alive(b[0]))}\n\n🏷️ {BRAND}"); return


def polling():
    print(f"🚀 {BRAND} HOST STARTING...")
    result=api("getMe")
    if not result.get("ok"): print("❌ Telegram API error:",result); return
    print(f"🤖 @{result['result'].get('username')}"); print("🟢 Telegram API: OK")
    api("deleteWebhook",{"drop_pending_updates":True})
    offset=None
    while True:
        try:
            data={"timeout":8,"limit":50,"allowed_updates":json.dumps(["message","callback_query"])}
            if offset is not None: data["offset"]=offset
            result=api("getUpdates",data,timeout=12)
            if not result.get("ok"): print("⚠️ Telegram error:",result); time.sleep(1); continue
            for update in result.get("result",[]):
                offset=update["update_id"]+1
                try:
                    if "message" in update: handle_message(update["message"])
                    elif "callback_query" in update: handle_callback(update["callback_query"])
                except Exception as e: print("⚠️ Update error:",repr(e))
        except KeyboardInterrupt: print("\n🛑 Host stopped."); break
        except Exception as e: print("🔄 Reconnecting:",repr(e)); time.sleep(1)


def main():
    if not BOT_TOKEN or BOT_TOKEN=="PUT_YOUR_HOST_BOT_TOKEN_HERE": print("❌ BOT_TOKEN set karo."); return
    if not OWNER_CHAT_ID or OWNER_CHAT_ID==123456789: print("❌ OWNER_CHAT_ID set karo."); return
    print("="*60); print(f"🔥 {BRAND}"); print("🚀 MULTI-CLIENT PYTHON HOST"); print("="*60)
    threading.Thread(target=render_health_server,daemon=True).start()
    time.sleep(.2)
    recover_bots()
    polling()

if __name__ == "__main__":

    main()
