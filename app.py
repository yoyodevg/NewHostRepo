#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=====================================================================
 TELEGRAM BOT HOSTING MANAGER  -  100% Python, ONE file, NO Docker
=====================================================================
Run (Windows):  start.bat          Stop: stop.bat
Run (Linux/Termux):  python main.py

How hosting works (no containers):
  * every hosted bot lives in its own folder  data/user_bots/user_<uid>/bot_<id>/
  * every bot gets its OWN virtual environment (.venv) -> isolated dependencies
  * requirements.txt (or detected imports) are installed with pip inside that venv
  * the bot runs as a normal background process (subprocess) with a CLEAN environment
    (the manager's token / passphrase / keys are never passed to hosted bots)
  * limits: RAM / CPU / process-count / storage / log-size are enforced by a monitor
    (+ hard OS limits where the OS supports them: Linux rlimit, Windows Job Objects)
  * IMPORTANT: without containers all bots run as the SAME OS user. This is NOT a
    hard security sandbox. Only host code you are willing to run on your machine
    (Linux: set BOT_RUN_USER to run bots as a separate low-privilege user).

Needs:  Python 3.10+   and   pip install -r requirements.txt

SETUP (sabse aasan): is file me upar 'USER CONFIG' box me SET_BOT_TOKEN aur SET_OWNER_ID bharein, phir  python main.py
UPGRADE: purani version isi folder me rakhi ho to box khaali hone par token/owner/backup-passphrase usi se le liye jate hain.
CHANNEL JOIN: Admin Panel → 📣 Channel join zaroori se channel jodo/hatao (code badalne ki zaroorat nahi; DB aur backups me save).

SETTINGS (optional: environment variables or a .env file next to main.py — ye box se upar maane jate hain)
  HOSTING_BOT_TOKEN   token of the Hosting Manager bot            (required)
  OWNER_ID            your Telegram numeric ID                    (required)
  BACKUP_PASSPHRASE   encrypts secrets inside backups (auto-created in .env if missing;
                      keep a copy of it OUTSIDE this computer!)
  HOSTING_HOME        base folder (default: folder of main.py)
  DATABASE_PATH, HOSTING_STORAGE_PATH, BACKUP_STORAGE_PATH   (default: <home>/data/...)
  MAX_UPLOAD_SIZE (MB, default 20 = Telegram download limit), DEFAULT_TRIAL_DAYS (7),
  BACKUP_INTERVAL_HOURS (12), RESOURCE_WARNING_THRESHOLD (85), REPORT_TIMEZONE (Asia/Kolkata),
  REPORT_HOUR (6), PROMO_HOUR (12), RESOURCE_SAMPLE_SECONDS (15), LOG_LEVEL,
  DOWNLOAD_HOST (127.0.0.1) / DOWNLOAD_PORT (8765, 0 = off) / PUBLIC_BASE_URL  (download links),
  BOT_RUN_USER (Linux, only when manager runs as root), ENCRYPTION_KEY (Fernet key)

COMMAND LINE
  python main.py                      start the manager
  python main.py --check              environment check (is hosting supported here?)
  python main.py --export             create MASTER backup zip from the command line
  python main.py --restore FILE.zip   restore a MASTER backup (use for big files / new server)
  python main.py --stop               ask a running manager to stop gracefully

SCHEDULED JOBS
  every 12h : backup of every user's bots -> sent to the user, report to admins
  every 12h : encrypted platform backup (database + keys) -> owner
  daily 03:30: MASTER backup kept on disk (last 3)   |  daily 06:00: admin report
  daily 12:00: one promo per free/trial user         |  every 15s: CPU/RAM monitor + crash handling
  every 5 min: trial & premium expiry + reminders
"""
from __future__ import annotations

import ast
import asyncio
import base64
import functools
import hashlib
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path, PurePosixPath

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 ya usse naya version chahiye (python.org se install karein).")

try:
    import psutil
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from telegram import (InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb,
                          ReplyKeyboardMarkup, Update)
    from telegram.constants import ParseMode
    from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
    from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                              CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters)
except ImportError as _e:                     # pragma: no cover
    sys.exit(f"Package missing: {_e.name}\nInstall with:  python -m pip install -r requirements.txt")

# =====================================================================
# >>> USER CONFIG START  — SIRF IS BOX KI VALUES BADLEIN (baaki code ko mat chhuyein) <<<
# =====================================================================
SET_BOT_TOKEN = "9101695004:AAHcL7JjJG5tvM8_bLzc1JOfz10ll9fuP5k"      # @BotFather se mila token, quotes ke andar. Jaise "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
SET_OWNER_ID = 8209524871                         # aapka Telegram numeric ID (bina quotes ke)
SET_BACKUP_PASSPHRASE = "144280"                        # khaali chhod sakte hain: apne aap ban jayega (aur .env me save hoga)
# <<< USER CONFIG END  (agar .env ya environment me HOSTING_BOT_TOKEN / OWNER_ID ho to wo is box se upar maane jate hain)
# =====================================================================


# =====================================================================
# 1. CONFIGURATION (upar ka USER CONFIG box  /  .env  /  environment)
# =====================================================================
IS_WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    try:
        txt = path.read_text(encoding="utf-8-sig")
    except OSError:
        return
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


load_env_file(HERE / ".env")


TOKEN_FORMAT = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,}$")


def _env(n: str, d: str = "") -> str:
    if TOKEN_FORMAT.match(n) or n.isdigit():          # galti se value hi likh di gayi ho (naam ki jagah) -> wahi value use karo
        return n
    return os.environ.get(n, d)


def _int(n: str, d: int) -> int:
    try:
        return int(float(_env(n, str(d)) or d))
    except ValueError:
        return d


def _read_old_config() -> dict:
    """If the USER CONFIG box of this file is empty, take token / owner id / backup passphrase from an OLDER version of this
    program that sits in the same folder (so upgrading never needs copy-paste and old backups stay restorable)."""
    out: dict = {}
    try:
        me = Path(__file__).resolve()
        cands = sorted((p for p in HERE.glob("*.py") if p.resolve() != me and p.stat().st_size < 3_000_000),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    except OSError:
        return out
    for p in cands:
        try:
            head = p.read_text(encoding="utf-8", errors="ignore")[:60000]
        except OSError:
            continue
        i = head.find("USER CONFIG START")
        if i < 0:
            continue
        box = head[i:i + 3500]
        for key, rx in (("token", r'^SET_BOT_TOKEN\s*=\s*["\']([^"\'\n]+)["\']'), ("owner", r'^SET_OWNER_ID\s*=\s*(\d+)'),
                        ("pass", r'^SET_BACKUP_PASSPHRASE\s*=\s*["\']([^"\'\n]*)["\']')):
            m = re.search(rx, box, re.M)
            if m and m[1].strip() and key not in out and not m[1].startswith(("PASTE_YOUR", "CHANGE_THIS")):
                out[key] = m[1].strip()
                out["_" + key] = p.name
    return out


_OLD = _read_old_config()


def _resolve(env_names, box_val, old_key):
    for n in env_names:
        if _env(n):
            return _env(n), "env/.env"
    if box_val:
        return str(box_val), "box"
    if _OLD.get(old_key):
        return str(_OLD[old_key]), "old:" + _OLD.get("_" + old_key, "?")
    return "", ""


_box_tok = "" if SET_BOT_TOKEN.startswith("PASTE_YOUR") else SET_BOT_TOKEN
BOT_TOKEN, _src_tok = _resolve(("HOSTING_BOT_TOKEN", "BOT_TOKEN"), _box_tok, "token")
BOT_TOKEN = BOT_TOKEN.strip().strip("\"'").strip()
_own, _src_own = _resolve(("OWNER_ID",), SET_OWNER_ID or "", "owner")
try:
    OWNER_ID = int(float(_own or 0))
except ValueError:
    OWNER_ID = 0
BASE = Path(_env("HOSTING_HOME", str(HERE))).resolve()
DATA = BASE / "data"
DB_PATH = Path(_env("DATABASE_PATH", str(DATA / "hosting.db"))).resolve()
STORAGE = Path(_env("HOSTING_STORAGE_PATH", str(DATA / "user_bots"))).resolve()
BACKUP_DIR = Path(_env("BACKUP_STORAGE_PATH", str(DATA / "backups"))).resolve()
LOG_DIR = DATA / "logs"
UPLOAD_DIR = DATA / "uploads_orig"          # original .py/.zip files exactly as users uploaded them (admin can download)
TMP_DIR = DATA / "tmp"
PID_FILE = DATA / "manager.pid"
STOP_FLAG = DATA / "stop.flag"
LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
MAX_UPLOAD = int(float(_env("MAX_UPLOAD_SIZE", "20")) * 1024 * 1024)
TRIAL_DAYS = _int("DEFAULT_TRIAL_DAYS", 7)
BACKUP_HOURS = float(_env("BACKUP_INTERVAL_HOURS", "12") or 12)
WARN_PCT = float(_env("RESOURCE_WARNING_THRESHOLD", "85") or 85)
REPORT_HOUR = _int("REPORT_HOUR", 6)
PROMO_HOUR = _int("PROMO_HOUR", 12)
SAMPLE_SEC = max(5, _int("RESOURCE_SAMPLE_SECONDS", 15))
BACKUP_PASSPHRASE, _src_pass = _resolve(("BACKUP_PASSPHRASE",), SET_BACKUP_PASSPHRASE, "pass")
CONFIG_SOURCES = {"token": _src_tok, "owner": _src_own, "backup passphrase": _src_pass}
DOWNLOAD_HOST = _env("DOWNLOAD_HOST", "127.0.0.1")
DOWNLOAD_PORT = _int("DOWNLOAD_PORT", 8765)
PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
BOT_RUN_USER = _env("BOT_RUN_USER")
TG_SEND_LIMIT = 49 * 1024 * 1024          # Telegram bot upload limit (~50MB)
TG_GET_LIMIT = 20 * 1024 * 1024           # Telegram bot download limit (20MB)
ZIP_MAX_FILES = 8000
ZIP_RATIO = 150
INSTALL_TIMEOUT = 900
APP_VERSION = "2.0"
FORMAT_MASTER = "TGH-MASTER"
# First-run seed ONLY. Channels are managed from the Admin Panel (📣 Channel join zaroori) and live in the database/backups.
DEFAULT_FS_CHANNELS = [{"chat_ref": "@techplanettips", "title": "Tech Planet Tips", "link": "https://t.me/techplanettips"}]


def _tz():
    name = _env("REPORT_TIMEZONE", "Asia/Kolkata")
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                                   # tzdata missing (Windows) -> safe fallback
        return timezone(timedelta(hours=5, minutes=30)) if name == "Asia/Kolkata" else timezone.utc


TZ = _tz()

ALL_PERMS = ["view_stats", "users_view", "users_manage", "ban_users", "premium", "bots_view", "bots_control",
             "ban_bots", "logs", "broadcast", "backup", "export", "restore", "migrate", "system",
             "maintenance", "plans", "message", "reports", "admins", "settings"]
DEFAULT_ROLES = {
    "owner": ALL_PERMS,
    "super": [p for p in ALL_PERMS if p not in ("admins", "settings", "restore", "migrate")],
    "support": ["view_stats", "users_view", "users_manage", "ban_users", "bots_view", "message"],
    "technical": ["view_stats", "bots_view", "bots_control", "ban_bots", "logs", "system", "reports",
                  "backup", "export"],
}
# cpu = % of ONE core, ram_mb = RAM per bot, storage_mb = project files per bot (without .venv),
# venv_mb = size cap of the bot's virtual environment, pids = max processes per bot
DEFAULT_PLANS = {
    "free": {"max_bots": 2, "cpu": 50, "ram_mb": 256, "storage_mb": 200, "venv_mb": 800, "pids": 32, "backup_keep": 3},
    "premium": {"max_bots": 10, "cpu": 150, "ram_mb": 1024, "storage_mb": 2048, "venv_mb": 3000, "pids": 128,
                "backup_keep": 10, "premium_days": 30},
}
PLAN_KEYS = ["max_bots", "cpu", "ram_mb", "storage_mb", "venv_mb", "pids", "backup_keep", "premium_days"]


# =====================================================================
# 2. UTILITIES, REDACTION, LOGGING
# =====================================================================

# =====================================================================
# 2. UTILITIES, REDACTION, LOGGING
# =====================================================================
A = asyncio.to_thread


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def pdt(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def esc(s) -> str:
    return html.escape(str(s))


def human(n: float) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "0 B"


def fmt_dur(sec: float) -> str:
    sec = int(max(sec, 0))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"


def short_dt(s: str | None) -> str:
    return pdt(s).astimezone(TZ).strftime("%d %b %Y %H:%M") if s else "—"


_U = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3,
      "gib": 1024 ** 3, "tb": 1000 ** 4, "tib": 1024 ** 4}


def parse_size(s: str) -> int:
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", s or "")
    return int(float(m[1]) * _U.get((m[2] or "b").lower(), 1)) if m else 0


_TOK = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")
_KV = re.compile(r"(?i)\b([A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API[_-]?KEY|PASSPHRASE)[A-Z0-9_]*)\s*([=:])\s*[^\s'\"]+")


def redact(t, extra=()) -> str:
    t = str(t)
    for s in extra:
        if s and len(s) >= 6:
            t = t.replace(s, "***")
    for s in (BOT_TOKEN, BACKUP_PASSPHRASE):
        if s:
            t = t.replace(s, "***")
    return _KV.sub(r"\1\2[REDACTED]", _TOK.sub("[REDACTED_TOKEN]", t))


class RedactFilter(logging.Filter):
    def filter(self, r):
        try:
            r.msg = redact(r.getMessage())
            r.args = ()
        except Exception:
            pass
        return True


def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "bots").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    for h in (logging.StreamHandler(), RotatingFileHandler(LOG_DIR / "system.log", maxBytes=5_000_000, backupCount=5)):
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        root.addHandler(h)
    for n in ("backup", "restore", "admin", "dependency", "hosting"):
        h = RotatingFileHandler(LOG_DIR / f"{n}.log", maxBytes=3_000_000, backupCount=3)
        h.setFormatter(fmt)
        h.addFilter(RedactFilter())
        logging.getLogger(n).addHandler(h)
    for n in ("httpx", "httpcore", "apscheduler"):   # httpx logs URLs that contain the bot token
        logging.getLogger(n).setLevel(logging.WARNING)


log = logging.getLogger("hosting")
blog = logging.getLogger("backup")
rlog = logging.getLogger("restore")
alog_ = logging.getLogger("admin")
dlog = logging.getLogger("dependency")


# =====================================================================
# 3. DATABASE LAYER (SQLite; same file/structure on Windows, Linux, mobile)
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  registered_at TEXT NOT NULL, last_seen TEXT, banned INTEGER NOT NULL DEFAULT 0, ban_reason TEXT,
  banned_at TEXT, banned_by INTEGER,
  blocked INTEGER NOT NULL DEFAULT 0, notify_promo INTEGER NOT NULL DEFAULT 1,
  notify_resource INTEGER NOT NULL DEFAULT 1, notify_backup INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS trials(user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL, expires_at TEXT NOT NULL, extended_days INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, plan TEXT NOT NULL DEFAULT 'premium',
  starts_at TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  notes TEXT, created_by INTEGER);
CREATE TABLE IF NOT EXISTS admin_roles(role TEXT PRIMARY KEY, permissions TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, role TEXT NOT NULL REFERENCES admin_roles(role),
  added_by INTEGER, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hosted_bots(id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, name TEXT NOT NULL, dir TEXT NOT NULL DEFAULT '',
  entry TEXT, desired_state TEXT NOT NULL DEFAULT 'stopped', status TEXT NOT NULL DEFAULT 'stopped',
  restart_policy TEXT NOT NULL DEFAULT 'on-failure', env_enc TEXT, created_at TEXT NOT NULL,
  last_restart TEXT, last_error TEXT, last_activity TEXT, restart_count INTEGER NOT NULL DEFAULT 0,
  backup_status TEXT, last_backup_at TEXT, auto_stopped INTEGER NOT NULL DEFAULT 0,
  banned INTEGER NOT NULL DEFAULT 0, ban_reason TEXT, banned_at TEXT,
  premium_until TEXT, lim_json TEXT, tg_username TEXT, tg_name TEXT, tg_checked TEXT, UNIQUE(user_id,name));
CREATE TABLE IF NOT EXISTS bot_processes(bot_id INTEGER PRIMARY KEY REFERENCES hosted_bots(id) ON DELETE CASCADE,
  pid INTEGER, ctime REAL, started_at TEXT, last_exit_code INTEGER, window_start TEXT,
  restarts_in_window INTEGER NOT NULL DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS backups(id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, kind TEXT NOT NULL, path TEXT, size INTEGER,
  sha256 TEXT, created_at TEXT NOT NULL, status TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
  error TEXT, bots_ok INTEGER NOT NULL DEFAULT 0, bots_failed INTEGER NOT NULL DEFAULT 0, delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS resource_usage(id INTEGER PRIMARY KEY AUTOINCREMENT,
  bot_id INTEGER NOT NULL REFERENCES hosted_bots(id) ON DELETE CASCADE, ts TEXT NOT NULL, cpu REAL, mem_bytes INTEGER,
  disk_bytes INTEGER, net_rx INTEGER, net_tx INTEGER);
CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  kind TEXT NOT NULL, day TEXT NOT NULL, sent_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'sent',
  UNIQUE(user_id,kind,day));
CREATE TABLE IF NOT EXISTS activity_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bot_id INTEGER,
  action TEXT NOT NULL, detail TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hosting_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL,
  action TEXT NOT NULL, target TEXT, detail TEXT, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ban_log(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target INTEGER NOT NULL,
  action TEXT NOT NULL, reason TEXT, admin_id INTEGER, ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS download_links(token TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL,
  expires_at TEXT NOT NULL, admin_id INTEGER, for_user INTEGER, created_at TEXT NOT NULL,
  downloads INTEGER NOT NULL DEFAULT 0, max_downloads INTEGER NOT NULL DEFAULT 3);
CREATE TABLE IF NOT EXISTS bot_uploads(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL REFERENCES hosted_bots(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL, filename TEXT NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, saved_path TEXT, uploaded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS crash_log(id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id INTEGER NOT NULL REFERENCES hosted_bots(id) ON DELETE CASCADE,
  ts TEXT NOT NULL, exit_code INTEGER, cause TEXT, fix TEXT, tail TEXT, sto_pct REAL, ram_pct REAL);
CREATE TABLE IF NOT EXISTS referrals(id INTEGER PRIMARY KEY AUTOINCREMENT, referrer_id INTEGER NOT NULL, referred_id INTEGER NOT NULL UNIQUE,
  created_at TEXT NOT NULL, bonus_days INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_up_bot ON bot_uploads(bot_id,id);
CREATE INDEX IF NOT EXISTS ix_crash_bot ON crash_log(bot_id,ts);
CREATE INDEX IF NOT EXISTS ix_ref_referrer ON referrals(referrer_id);
CREATE TABLE IF NOT EXISTS fs_channels(id INTEGER PRIMARY KEY AUTOINCREMENT, chat_ref TEXT NOT NULL UNIQUE, chat_id TEXT, title TEXT, username TEXT,
  link TEXT NOT NULL, added_by INTEGER, added_at TEXT NOT NULL, can_check INTEGER NOT NULL DEFAULT 1, last_error TEXT);
CREATE TABLE IF NOT EXISTS fs_state(user_id INTEGER PRIMARY KEY, first_warned_at TEXT, stopped_at TEXT, stopped_bots TEXT, last_ok_at TEXT);
CREATE INDEX IF NOT EXISTS ix_bots_user ON hosted_bots(user_id);
CREATE INDEX IF NOT EXISTS ix_bots_state ON hosted_bots(desired_state,status);
CREATE INDEX IF NOT EXISTS ix_backups_user ON backups(user_id,created_at);
CREATE INDEX IF NOT EXISTS ix_res_bot_ts ON resource_usage(bot_id,ts);
CREATE INDEX IF NOT EXISTS ix_act_user ON activity_logs(user_id,ts);
CREATE INDEX IF NOT EXISTS ix_sub_user ON subscriptions(user_id,status,expires_at);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS ix_ban_ts ON ban_log(ts);
"""
REQUIRED_TABLES = {"users", "admins", "admin_roles", "hosted_bots", "bot_processes", "subscriptions", "trials",
                   "backups", "resource_usage", "notifications", "activity_logs", "hosting_settings",
                   "audit_logs", "ban_log", "download_links"}


class DB:
    """Single serialized connection (WAL). All methods are synchronous; call via asyncio.to_thread."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.conn: sqlite3.Connection | None = None
        self.open()

    def open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=15)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=5000")
        self.conn = c

    def close(self):
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None

    def ex(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def one(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def val(self, sql, args=(), default=0):
        r = self.one(sql, args)
        return r[0] if r and r[0] is not None else default

    def tx(self, fn):
        """Run fn(conn) inside one atomic transaction."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                r = fn(self.conn)
                self.conn.execute("COMMIT")
                return r
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def init_schema(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute('PRAGMA table_info(hosted_bots)')}
            for c in ('premium_until', 'lim_json', 'tg_username', 'tg_name', 'tg_checked'):
                if c not in cols:
                    self.conn.execute(f'ALTER TABLE hosted_bots ADD COLUMN {c} TEXT')
            for role, perms in DEFAULT_ROLES.items():
                self.conn.execute("INSERT OR IGNORE INTO admin_roles(role,permissions) VALUES(?,?)", (role, json.dumps(perms)))
            if OWNER_ID:
                self.conn.execute("INSERT OR IGNORE INTO admins(user_id,role,added_by,added_at) VALUES(?,?,?,?)",
                                  (OWNER_ID, "owner", OWNER_ID, iso()))
            # one-time seed of the default channel (also runs after restoring an OLD backup that never knew about channels)
            if not self.conn.execute("SELECT 1 FROM hosting_settings WHERE key='fs_seeded'").fetchone():
                for ch in DEFAULT_FS_CHANNELS:
                    self.conn.execute("INSERT OR IGNORE INTO fs_channels(chat_ref,chat_id,title,username,link,added_by,added_at,can_check) VALUES(?,?,?,?,?,?,?,1)",
                                      (ch["chat_ref"], ch["chat_ref"], ch["title"], ch["chat_ref"].lstrip("@"), ch["link"], 0, iso()))
                self.conn.execute("INSERT OR REPLACE INTO hosting_settings(key,value) VALUES('fs_seeded','true')")

    def integrity_ok(self) -> bool:
        try:
            return self.one("PRAGMA integrity_check")[0] == "ok"
        except Exception:
            return False

    def snapshot_to(self, dest: Path):
        """Consistent copy using the SQLite online-backup API."""
        with self.lock:
            out = sqlite3.connect(dest)
            try:
                self.conn.backup(out)
            finally:
                out.close()

    def swap_with(self, new_file: Path):
        with self.lock:
            self.close()
            for ext in ("-wal", "-shm"):
                Path(str(self.path) + ext).unlink(missing_ok=True)
            shutil.copy2(new_file, self.path)
            self.open()
            self.init_schema()


db: DB = None  # type: ignore


def get_setting(key, default=None):
    r = db.one("SELECT value FROM hosting_settings WHERE key=?", (key,))
    return json.loads(r[0]) if r else default


def set_setting(key, value):
    db.ex("INSERT INTO hosting_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (key, json.dumps(value)))


def get_plans() -> dict:
    p = get_setting("plans") or {}
    return {k: {**DEFAULT_PLANS[k], **p.get(k, {})} for k in DEFAULT_PLANS}


def alog(uid, action, detail="", bid=None):
    try:
        db.ex("INSERT INTO activity_logs(user_id,bot_id,action,detail,ts) VALUES(?,?,?,?,?)",
              (uid, bid, action, redact(detail)[:500], iso()))
    except Exception:
        log.exception("activity log failed")


def audit(admin_id, action, target="", detail=""):
    db.ex("INSERT INTO audit_logs(admin_id,action,target,detail,ts) VALUES(?,?,?,?,?)",
          (admin_id, action, str(target), redact(detail)[:500], iso()))
    alog_.info("admin=%s action=%s target=%s %s", admin_id, action, target, detail)


# ---------- secrets for stored bot env vars ----------
def _fernet() -> Fernet:
    k = _env("ENCRYPTION_KEY")
    if not k:
        kf = DB_PATH.parent / ".secret.key"
        if not kf.exists():
            kf.parent.mkdir(parents=True, exist_ok=True)
            kf.write_bytes(Fernet.generate_key())
            os.chmod(kf, 0o600)
        k = kf.read_text().strip()
    return Fernet(k.encode() if isinstance(k, str) else k)


def enc_env(d: dict) -> str:
    return _fernet().encrypt(json.dumps(d).encode()).decode()


def dec_env(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(_fernet().decrypt(s.encode()))
    except (InvalidToken, ValueError):
        return {}




# =====================================================================
# 4. SECURITY: safe archives, names, entry detection
# =====================================================================
class UnsafeArchive(Exception):
    pass


def safe_name(s: str, default="bot") -> str:
    s = re.sub(r"[^\w\-. ]", "_", s or "").strip(" .")[:40]
    return s or default



WIN_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.I)
_FALLBACK_STDLIB = {"os", "sys", "re", "json", "time", "datetime", "asyncio", "logging", "random", "math", "typing", "pathlib",
                    "collections", "itertools", "functools", "subprocess", "threading", "sqlite3", "csv", "io", "base64",
                    "hashlib", "string", "shutil", "tempfile", "urllib", "http", "socket", "ssl", "uuid", "traceback",
                    "dataclasses", "enum", "abc", "copy", "pickle", "signal", "struct", "textwrap", "zipfile", "gzip",
                    "html", "unittest", "argparse", "configparser", "contextlib", "decimal", "fractions", "glob", "heapq",
                    "inspect", "queue", "secrets", "statistics", "weakref", "zoneinfo", "email", "platform", "operator"}
STDLIB = set(getattr(sys, "stdlib_module_names", _FALLBACK_STDLIB))


def safe_extract(zf: zipfile.ZipFile, dest: Path, prefix: str = "", max_bytes: int = 512 * 1024 * 1024,
                 max_files: int = ZIP_MAX_FILES, check_ratio: bool = True) -> int:
    """Extract with path-traversal, symlink, ZIP-bomb (declared + streamed size, ratio, count) protection.
    Windows: reserved device names and ':' are refused."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    infos = [i for i in zf.infolist() if i.filename.replace("\\", "/").startswith(prefix)]
    if sum(i.file_size for i in infos) > max_bytes:
        raise UnsafeArchive("Archive is too large when extracted (zip-bomb protection).")
    total = count = 0
    for zi in infos:
        name = zi.filename.replace("\\", "/")[len(prefix):]
        if not name or name.endswith("/") or name.startswith("__MACOSX/") or name.endswith(".DS_Store"):
            continue
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or re.match(r"^[A-Za-z]:", name):
            raise UnsafeArchive(f"Unsafe path in archive: {name[:80]}")
        if IS_WIN and (":" in name or any(WIN_RESERVED.match(x) or x.endswith((" ", ".")) for x in p.parts)):
            raise UnsafeArchive(f"File name not allowed on Windows: {name[:80]}")
        if (zi.external_attr >> 16) & 0o170000 == 0o120000:
            raise UnsafeArchive("Symbolic links are not allowed in archives.")
        if check_ratio and zi.compress_size and zi.file_size > 10 * 1024 * 1024 and zi.file_size / zi.compress_size > ZIP_RATIO:
            raise UnsafeArchive("Suspicious compression ratio (zip bomb).")
        count += 1
        if count > max_files:
            raise UnsafeArchive("Too many files in archive.")
        target = (root / Path(*p.parts)).resolve()
        if root not in target.parents:
            raise UnsafeArchive("Path escapes the target directory.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(zi) as src, open(target, "wb") as out:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise UnsafeArchive("Archive expands beyond the allowed size (zip bomb).")
                out.write(chunk)
    return count


def flatten_single_dir(d: Path):
    items = [p for p in d.iterdir() if p.name != "__MACOSX"]
    if len(items) == 1 and items[0].is_dir():
        inner = items[0]
        for p in list(inner.iterdir()):
            shutil.move(str(p), str(d / p.name))
        inner.rmdir()


EXCL_DIRS = {".venv", "__pycache__", ".bot_tmp", ".git", "node_modules", ".pytest_cache", "__MACOSX"}
EXCL_SUFFIX = {".pyc", ".pyo", ".tmp", ".swp"}
SQLITE_EXT = {".db", ".sqlite", ".sqlite3", ".db3"}


def walk_files(d: Path):
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in EXCL_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix.lower() in EXCL_SUFFIX or f == ".DS_Store" or p.is_symlink():
                continue
            yield p


def dir_size(d: Path) -> int:
    t = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                t += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return t


PREFERRED = ["main.py", "bot.py", "app.py", "run.py", "start.py", "__main__.py", "index.py"]
FRAMEWORKS = ("telegram", "aiogram", "telebot", "pyrogram", "telethon", "pytelegrambotapi", "discord", "hydrogram")


def detect_entry(d: Path):
    """Returns (entry|None, candidate list). entry None => ambiguous, ask the user."""
    scored = []
    for p in walk_files(d):
        if p.suffix != ".py":
            continue
        rel = p.relative_to(d).as_posix()
        try:
            txt = p.read_text(errors="ignore")[:200_000]
        except OSError:
            continue
        s = 0
        if rel in PREFERRED:
            s += 5
        if re.search(r"""if\s+__name__\s*==\s*['"]__main__['"]""", txt):
            s += 2
        if any(f in txt.lower() for f in FRAMEWORKS):
            s += 2
        if re.search(r"run_polling|start_polling|infinity_polling|\.run\(|idle\(\)", txt):
            s += 2
        if "/" not in rel:
            s += 1
        scored.append((s, rel))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return None, []
    if len(scored) == 1 or scored[0][0] > scored[1][0]:
        return scored[0][1], [r for _, r in scored[:8]]
    return None, [r for _, r in scored[:8]]


IMPORT_MAP = {"telegram": "python-telegram-bot", "telebot": "pyTelegramBotAPI", "PIL": "pillow", "cv2": "opencv-python-headless",
              "yaml": "pyyaml", "bs4": "beautifulsoup4", "dotenv": "python-dotenv", "sklearn": "scikit-learn",
              "dateutil": "python-dateutil", "Crypto": "pycryptodome", "jwt": "pyjwt", "serial": "pyserial",
              "magic": "python-magic", "docx": "python-docx", "fitz": "pymupdf", "telethon": "telethon",
              "pyrogram": "pyrogram", "aiogram": "aiogram", "apscheduler": "apscheduler", "psycopg2": "psycopg2-binary"}
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,\-]+\])?([=<>!~]=?[A-Za-z0-9_.*]+)?$")


def infer_requirements(d: Path) -> list[str]:
    mods, local, n = set(), set(), 0
    for root, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x not in EXCL_DIRS]
        local.update(dirs)
        for f in files:
            if f.endswith(".py"):
                local.add(f[:-3])
    for p in walk_files(d):
        if p.suffix != ".py":
            continue
        n += 1
        if n > 300:
            break
        try:
            tree = ast.parse(p.read_text(errors="ignore"))
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    out = set()
    for m in mods:
        if m in STDLIB or m in local or m.startswith("_"):
            continue
        out.add(IMPORT_MAP.get(m, m))
    return sorted(x for x in out if PKG_RE.match(x))





# =====================================================================
# 5. PROCESS ENGINE  (venv + subprocess + psutil  -  NO Docker)
#    Everything that touches OS processes lives in this section so the
#    rest of the program stays platform independent.
# =====================================================================
A = asyncio.to_thread
LOCKS: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
STATS: dict[int, dict] = {}
DISK: dict[int, tuple[float, int]] = {}
TASKS: set = set()
AUTOFIX_TRIED: dict[int, set] = defaultdict(set)
CRASHING: set[int] = set()
POPENS: dict[int, subprocess.Popen] = {}
JOBS: dict[int, object] = {}
PSCACHE: dict[int, "psutil.Process"] = {}
CPU_HIST: dict[int, deque] = defaultdict(lambda: deque(maxlen=max(4, 600 // SAMPLE_SEC)))
INSTALL_SEM = asyncio.Semaphore(_int("MAX_CONCURRENT_INSTALLS", 2))
ENGINE = {"ok": False, "msg": "environment not checked yet", "checked": None}
STATE = {"restoring": False}
BLOCKED_ENV = {"PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONUSERBASE",
               "PYTHONEXECUTABLE", "PYTHONUTF8", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
               "COMSPEC", "SYSTEMROOT", "WINDIR", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR",
               "VIRTUAL_ENV", "BACKUP_PASSPHRASE", "ENCRYPTION_KEY", "OWNER_ID", "PATHEXT"}
PASSTHRU = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "PIP_INDEX_URL")
REQ_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*(\[[A-Za-z0-9_,.\-]+\])?\s*"
                      r"(?:(?:===|==|>=|<=|~=|!=|>|<)\s*[A-Za-z0-9.*+!_\-]+\s*,?\s*)*(?:;[^#\n]*)?$")


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(lambda x: (TASKS.discard(x), x.cancelled() or x.exception() and log.error("task failed: %r", x.exception())))
    return t


def rmtree_force(p) -> None:
    def onerr(func, path, *_):
        try:
            os.chmod(path, 0o700)
            func(path)
        except Exception:
            pass
    if not Path(p).exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(p, onexc=onerr)
    else:
        shutil.rmtree(p, onerror=onerr)


def bot_dir(b) -> Path:
    p = (STORAGE / b["dir"]).resolve()
    if STORAGE not in p.parents:
        raise UnsafeArchive("bad bot directory")
    return p


def venv_python(d: Path) -> Path:
    return d / ".venv" / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")


def project_size(d: Path) -> int:
    """Size of the project files (the .venv is counted separately)."""
    t = 0
    for root, dirs, files in os.walk(d):
        if Path(root) == d:
            dirs[:] = [x for x in dirs if x != ".venv"]
        for f in files:
            try:
                t += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return t


def valid_env_name(k: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k)) and k.upper() not in BLOCKED_ENV \
        and not k.upper().startswith(("HOSTING_", "TGH_"))


def validate_requirements_text(txt: str) -> tuple[bool, str]:
    if len(txt) > 100_000:
        return False, "requirements.txt is too large."
    for raw in txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if not REQ_LINE.match(line):
            return False, f"Not allowed in requirements.txt (only plain 'package==version' lines): {line[:70]}"
    return True, "ok"


def base_env(tmp: Path, path_prefix: str = "") -> dict:
    """Clean environment: NOTHING from the manager's own environment is passed on (no tokens/keys)."""
    env = {"PATH": (path_prefix + os.pathsep if path_prefix else "") + os.environ.get("PATH", ""),
           "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
    if IS_WIN:
        sr = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or r"C:\Windows"
        env.update({"SystemRoot": sr, "WINDIR": sr, "COMSPEC": os.environ.get("COMSPEC", sr + r"\system32\cmd.exe"),
                    "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"), "USERPROFILE": str(tmp),
                    "HOME": str(tmp), "TEMP": str(tmp), "TMP": str(tmp), "APPDATA": str(tmp), "LOCALAPPDATA": str(tmp)})
    else:
        env.update({"HOME": str(tmp), "TMPDIR": str(tmp), "LANG": os.environ.get("LANG") or "C.UTF-8"})
    return env


def bot_env(b, d: Path) -> dict:
    tmp = d / ".bot_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp, str(venv_python(d).parent))
    env["VIRTUAL_ENV"] = str(d / ".venv")
    for k, v in dec_env(b["env_enc"]).items():
        if valid_env_name(k):
            env[k] = v
    return env


def install_env(d: Path) -> dict:
    tmp = d / ".bot_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = base_env(tmp, str(venv_python(d).parent))
    for k in PASSTHRU:
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def _run(args, timeout=60, cwd=None, env=None) -> tuple[int, str]:
    kw = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        r = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None, env=env, capture_output=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, **kw)
        return r.returncode, (r.stdout + r.stderr).decode(errors="replace")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as e:
        return 127, f"not found: {e}"
    except OSError as e:
        return 126, f"OS error: {e}"


async def run_cmd(args, timeout=60, cwd=None, env=None):
    return await A(_run, args, timeout, cwd, env)


def tail_file(p: Path, n_lines: int = 60, max_bytes: int = 65536) -> str:
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return "\n".join(data.decode(errors="replace").splitlines()[-n_lines:])
    except OSError:
        return ""


# ---------------- OS limits ----------------
def _preexec_fn(lim):
    fs = int(lim["storage_mb"]) * 1024 * 1024

    def fn():                                   # runs in the child, POSIX only; must never raise
        try:
            os.umask(0o077)
            os.nice(10)
        except Exception:
            pass
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            resource.setrlimit(resource.RLIMIT_FSIZE, (fs, fs))
        except Exception:
            pass
    return fn


def _win_job(proc, lim) -> tuple[object, str]:
    """Windows Job Object: hard RAM cap, process-count cap and CPU cap for the bot's process tree."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("a", "b", "c", "d", "e", "f")]

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class EXT(ctypes.Structure):
        _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class CPUR(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise OSError("CreateJobObject failed")
    notes = []
    info = EXT()
    info.Basic.LimitFlags = 0x8 | 0x200            # ACTIVE_PROCESS | JOB_MEMORY
    info.Basic.ActiveProcessLimit = int(lim["pids"])
    info.JobMemoryLimit = int(lim["ram_mb"]) * 1024 * 1024
    if k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        notes.append(f"job: RAM {lim['ram_mb']}MB, procs {lim['pids']}")
    try:
        rate = int(float(lim["cpu"]) / (max(os.cpu_count() or 1, 1) * 100.0) * 10000)
        cr = CPUR(0x1 | 0x4, max(1, min(rate, 10000)))   # ENABLE | HARD_CAP
        if k.SetInformationJobObject(job, 15, ctypes.byref(cr), ctypes.sizeof(cr)):
            notes.append(f"cpu cap {lim['cpu']}%")
    except Exception:
        pass
    if not k.AssignProcessToJobObject(job, int(proc._handle)):
        k.CloseHandle(job)
        raise OSError("AssignProcessToJobObject failed")
    return job, ", ".join(notes) or "job attached"


def _close_job(bid: int) -> None:
    j = JOBS.pop(bid, None)
    if j and IS_WIN:
        try:
            import ctypes
            ctypes.WinDLL("kernel32").CloseHandle(j)
        except Exception:
            pass


def engine_spawn(b, d: Path, entry: str, env: dict, lim: dict) -> tuple[int, float, str]:
    bid = b["id"]
    lp = LOG_DIR / "bots" / f"bot_{bid}.log"
    lp.parent.mkdir(parents=True, exist_ok=True)
    if lp.exists() and lp.stat().st_size > 5_000_000:               # rotate: keep the last 1 MB
        keep = lp.read_bytes()[-1_000_000:]
        lp.write_bytes(keep)
    with open(lp, "ab") as lf:
        lf.write(f"\n=== started {iso()} ===\n".encode())
        lf.flush()
        kw = dict(cwd=str(d), env=env, stdin=subprocess.DEVNULL, stdout=lf, stderr=subprocess.STDOUT, close_fds=True)
        if IS_WIN:
            kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                                   | subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            kw["start_new_session"] = True
            kw["preexec_fn"] = _preexec_fn(lim)
            if BOT_RUN_USER and hasattr(os, "geteuid") and os.geteuid() == 0:
                kw["user"] = BOT_RUN_USER
        p = subprocess.Popen([str(venv_python(d)), "-u", entry], **kw)
    POPENS[bid] = p
    note = "limits: monitor (RAM/CPU/procs/storage)" + ("; rlimit" if not IS_WIN else "")
    if IS_WIN:
        try:
            job, n = _win_job(p, lim)
            JOBS[bid] = job
            note += "; " + n
        except Exception as e:
            note += f"; job object unavailable ({e})"
    try:
        ct = psutil.Process(p.pid).create_time()
    except psutil.Error:
        ct = time.time()
    return p.pid, ct, note


def proc_alive(pid, ctime=None) -> bool:
    if not pid:
        return False
    try:
        p = psutil.Process(int(pid))
        if ctime and abs(p.create_time() - float(ctime)) > 3:
            return False
        return p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def kill_tree(pid, ctime=None, grace: float = 8.0) -> None:
    if not proc_alive(pid, ctime):
        return
    try:
        root = psutil.Process(int(pid))
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return
    if not IS_WIN:                                           # graceful first (SIGTERM), Windows has no equivalent
        try:
            root.send_signal(signal.SIGTERM)
            psutil.wait_procs([root], timeout=grace)
        except psutil.Error:
            pass
    for p in procs:
        try:
            if p.is_running():
                p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=3)
    if not IS_WIN:
        try:
            os.killpg(int(pid), signal.SIGKILL)                # stragglers in the bot's own session
        except (OSError, ProcessLookupError):
            pass


def reap(bid: int) -> int | None:
    p = POPENS.pop(bid, None)
    if not p:
        return None
    try:
        return p.wait(timeout=5)
    except Exception:
        return p.poll()


def sample_all(entries) -> dict:
    """entries: [(bot_id, pid, ctime)] -> {bot_id: stats | None(dead)}.  Sums the bot's whole process tree."""
    out, seen = {}, set()
    for bid, pid, ctime in entries:
        try:
            root = PSCACHE.get(pid)
            if root is None or not root.is_running():
                root = psutil.Process(pid)
            if ctime and abs(root.create_time() - float(ctime)) > 3:
                out[bid] = None
                continue
            if root.status() == psutil.STATUS_ZOMBIE:
                POPENS[bid].poll() if bid in POPENS else None
                out[bid] = None
                continue
            procs = [root] + root.children(recursive=True)
            cpu = rss = 0.0
            for p in procs:
                c = PSCACHE.get(p.pid)
                if c is None or c != p:
                    c = PSCACHE[p.pid] = p
                seen.add(p.pid)
                try:
                    cpu += c.cpu_percent(None)
                    rss += c.memory_info().rss
                except psutil.Error:
                    pass
            out[bid] = {"cpu": cpu, "mem": int(rss), "procs": len(procs), "rx": 0, "tx": 0, "started": ctime}
        except psutil.NoSuchProcess:
            out[bid] = None
        except psutil.Error:
            out[bid] = {"cpu": 0.0, "mem": 0, "procs": 1, "rx": 0, "tx": 0, "started": ctime}
    for pid in list(PSCACHE):
        if pid not in seen:
            PSCACHE.pop(pid, None)
    return out


def engine_selfcheck() -> tuple[bool, str]:
    """Proves that subprocess + venv + pip + background processes really work here. Never pretends."""
    problems = []
    td = Path(tempfile.mkdtemp(prefix="tgh_check_", dir=str(TMP_DIR) if TMP_DIR.exists() else None))
    try:
        env = base_env(td)
        rc, out = _run([sys.executable, "-c", "print('subprocess-ok')"], 30, td, env)
        if rc != 0 or "subprocess-ok" not in out:
            problems.append(f"subprocess not working ({out[-200:].strip()})")
        else:
            vd = td / "v"
            rc, out = _run([sys.executable, "-m", "venv", str(vd)], 180, td, env)
            vpy = vd / ("Scripts" if IS_WIN else "bin") / ("python.exe" if IS_WIN else "python")
            if rc != 0 or not vpy.exists():
                problems.append("virtual environments (venv/ensurepip) not supported: " + out[-300:].strip())
            else:
                rc, out = _run([str(vpy), "-m", "pip", "--version"], 60, td, env)
                if rc != 0:
                    problems.append("pip missing inside venv: " + out[-200:].strip())
                kw = {}
                if IS_WIN:
                    kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                else:
                    kw["start_new_session"] = True
                try:
                    p = subprocess.Popen([str(vpy), "-c", "import time; time.sleep(60)"], cwd=str(td), env=env,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                    time.sleep(1.0)
                    if not proc_alive(p.pid):
                        problems.append("background process did not stay alive")
                    else:
                        st = sample_all([(-1, p.pid, None)]).get(-1)
                        if st is None:
                            problems.append("psutil cannot read process statistics")
                        kill_tree(p.pid, None, 2.0)
                        try:
                            p.wait(timeout=5)
                        except Exception:
                            problems.append("could not stop a background process")
                except OSError as e:
                    problems.append(f"cannot start background processes: {e}")
        try:
            psutil.virtual_memory()
        except Exception as e:
            problems.append(f"psutil system stats limited ({e})")
    finally:
        rmtree_force(td)
        PSCACHE.pop(-1, None)
    fatal = [p for p in problems if "system stats limited" not in p]
    if fatal:
        return False, "UNSUPPORTED ENVIRONMENT - hosting is DISABLED:\n- " + "\n- ".join(problems)
    note = f"OK: {sys.platform}, Python {sys.version.split()[0]}"
    if problems:
        note += " (note: " + "; ".join(problems) + ")"
    return True, note


def run_engine_check() -> tuple[bool, str]:
    ok, msg = engine_selfcheck()
    ENGINE.update(ok=ok, msg=msg, checked=iso())
    return ok, msg


# ---------------- plan / bot helpers ----------------
def plan_of(uid: int) -> dict | None:
    u = db.one("SELECT banned FROM users WHERE id=?", (uid,))
    if not u:
        return None
    n = iso()
    plans = get_plans()
    sub = db.one("SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1", (uid, n))
    tr = db.one("SELECT * FROM trials WHERE user_id=? AND status='active' AND expires_at>?", (uid, n))
    if u["banned"]:
        name, lim, exp = "banned", plans["free"], None
    elif sub:
        name, lim, exp = "premium", plans["premium"], sub["expires_at"]
    elif db.val("SELECT MAX(premium_until) FROM hosted_bots WHERE user_id=? AND premium_until>?", (uid, n)):
        name, lim, exp = "premium", plans["premium"], db.val("SELECT MAX(premium_until) FROM hosted_bots WHERE user_id=? AND premium_until>?", (uid, n))
    elif tr:
        name, lim, exp = "trial", plans["free"], tr["expires_at"]
    else:
        name, lim, exp = "expired", plans["free"], None
    return {"name": name, "limits": lim, "expires": exp, "active": name in ("premium", "trial")}


def bot_plan(b) -> dict:
    """Effective plan of ONE bot: account premium > this bot's own premium > free trial. Admin per-bot limit overrides on top."""
    n = iso()
    plans = get_plans()
    u = db.one("SELECT banned FROM users WHERE id=?", (b["user_id"],))
    sub = db.one("SELECT expires_at FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1", (b["user_id"], n))
    tr = db.one("SELECT expires_at FROM trials WHERE user_id=? AND status='active' AND expires_at>?", (b["user_id"], n))
    if not u or u["banned"]:
        name, base, exp = "banned", plans["free"], None
    elif sub:
        name, base, exp = "premium", plans["premium"], sub["expires_at"]
    elif b["premium_until"] and b["premium_until"] > n:
        name, base, exp = "premium", plans["premium"], b["premium_until"]
    elif tr:
        name, base, exp = "trial", plans["free"], tr["expires_at"]
    else:
        name, base, exp = "expired", plans["free"], None
    lim = dict(base)
    try:
        lim.update(json.loads(b["lim_json"] or "{}"))
    except ValueError:
        pass
    return {"name": name, "limits": lim, "expires": exp, "active": name in ("premium", "trial")}


def get_bot(bid: int):
    return db.one("SELECT * FROM hosted_bots WHERE id=?", (bid,))


def set_bot(bid: int, **kw):
    cols = ",".join(f"{k}=?" for k in kw)
    db.ex(f"UPDATE hosted_bots SET {cols} WHERE id=?", (*kw.values(), bid))


def bot_pid(bid: int):
    r = db.one("SELECT pid,ctime FROM bot_processes WHERE bot_id=?", (bid,))
    return (r["pid"], r["ctime"]) if r and r["pid"] else (None, None)


def bot_is_running(bid: int) -> bool:
    pid, ct = bot_pid(bid)
    return proc_alive(pid, ct)


def unsupported_msg() -> str:
    return ENGINE["msg"] if not ENGINE["ok"] else ""


async def install_deps(bot, extra: tuple = ()) -> tuple[bool, str]:
    """Create the bot's own venv (if missing) and install requirements.txt (+ detected imports)."""
    bid = bot["id"]
    d = bot_dir(bot)
    req = d / "requirements.txt"
    pkgs = [x for x in extra if PKG_RE.match(x)]
    explicit = bool(pkgs)
    if req.exists():
        okr, why = validate_requirements_text(req.read_text(errors="ignore"))
        if not okr:
            return False, why
    elif not pkgs:
        pkgs = await A(infer_requirements, d)
    pl = await A(bot_plan, bot)
    lim = pl["limits"]
    env = await A(install_env, d)
    out_all = []
    async with INSTALL_SEM:
        vpy = venv_python(d)
        if not vpy.exists():
            rc, out = await run_cmd([sys.executable, "-m", "venv", str(d / ".venv")], 300, d, env)
            out_all.append(out)
            if rc != 0 or not vpy.exists():
                _install_log(bid, rc, out_all)
                return False, "Could not create the virtual environment:\n" + out[-800:]
        pip = [str(vpy), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-cache-dir"]
        rc = 0
        if req.exists():
            rc, out = await run_cmd(pip + ["-r", str(req)], INSTALL_TIMEOUT, d, env)
            out_all.append(out)
        if rc == 0 and pkgs:
            if explicit:
                rc, out = await run_cmd(pip + pkgs, INSTALL_TIMEOUT, d, env)
                out_all.append(out)
            else:                                            # detected imports: a wrong guess must not block the bot
                bad = []
                for pk in pkgs:
                    r2, o2 = await run_cmd(pip + [pk], INSTALL_TIMEOUT, d, env)
                    out_all.append(o2)
                    if r2 != 0:
                        bad.append(pk)
                if bad:
                    out_all.append(f"(could not install guessed packages: {', '.join(bad)})")
    _install_log(bid, rc, out_all)
    if rc != 0:
        return False, "\n".join(out_all)[-1500:]
    vs = await A(dir_size, d / ".venv")
    if vs > lim["venv_mb"] * 1024 * 1024:
        await A(rmtree_force, d / ".venv")
        return False, f"Installed packages are too big ({human(vs)} > {lim['venv_mb']} MB plan limit)."
    return True, "\n".join(out_all)[-1500:]


def _install_log(bid: int, rc: int, parts: list) -> None:
    try:
        ilog = LOG_DIR / "bots" / f"bot_{bid}_install.log"
        ilog.parent.mkdir(parents=True, exist_ok=True)
        ilog.write_text(f"=== {iso()} rc={rc} ===\n{redact(chr(10).join(parts))[-60000:]}", encoding="utf-8")
        dlog.info("bot %s install rc=%s", bid, rc)
    except OSError:
        pass


async def start_bot(bid: int, restart: bool = False) -> tuple[bool, str]:
    async with LOCKS[bid]:
        b = get_bot(bid)
        if not b:
            return False, "Bot not found."
        if not ENGINE["ok"]:
            return False, unsupported_msg()
        u = await A(db.one, "SELECT banned,ban_reason FROM users WHERE id=?", (b["user_id"],))
        if u and u["banned"]:
            return False, f"Account is banned. Reason: {u['ban_reason'] or '-'}"
        if b["banned"]:
            return False, f"This bot is banned by the admin. Reason: {b['ban_reason'] or '-'}"
        _fs = await A(db.one, "SELECT stopped_at FROM fs_state WHERE user_id=?", (b["user_id"],))
        if _fs and _fs["stopped_at"]:                      # bots of a user who did not join the channel stay stopped
            _r = await fs_check(b["user_id"], force=True)
            if not _r["ok"]:
                return False, "Pehle channel join karein: " + " ".join(c["link"] for c in _r["missing"]) + " — phir ▶️ Start dabayein."
            await A(db.ex, "DELETE FROM fs_state WHERE user_id=?", (b["user_id"],))
        pl = await A(bot_plan, b)
        if not pl["active"]:
            return False, "Hosting is not active for this account (trial expired or banned)."
        if not b["entry"]:
            return False, "Main Python file is not set. Open Bot Settings -> Main file."
        if await A(bot_is_running, bid):
            return True, "Bot is already running."
        d = bot_dir(b)
        ep = (d / b["entry"]).resolve()
        if b["entry"].startswith("-") or not ep.is_file() or d not in ep.parents or ep.suffix != ".py":
            return False, "Main file is invalid or missing."
        lim = pl["limits"]
        if await A(project_size, d) > lim["storage_mb"] * 1024 * 1024:
            return False, f"Project is bigger than the storage limit ({lim['storage_mb']} MB)."
        if not venv_python(d).exists():
            set_bot(bid, status="installing")
            ok, tail = await install_deps(b)
            if not ok:
                set_bot(bid, status="error", last_error=tail[-400:])
                return False, f"Dependency installation failed:\n{tail[-600:]}"
        env = await A(bot_env, b, d)
        try:
            pid, ct, note = await A(engine_spawn, b, d, b["entry"], env, lim)
        except Exception as e:
            msg = redact(str(e), extra=list(dec_env(b["env_enc"]).values()))[-400:]
            set_bot(bid, status="error", last_error=msg)
            return False, f"Could not start the process: {msg}"
        t = iso()
        db.ex("INSERT INTO bot_processes(bot_id,pid,ctime,started_at,note) VALUES(?,?,?,?,?) "
              "ON CONFLICT(bot_id) DO UPDATE SET pid=excluded.pid,ctime=excluded.ctime,started_at=excluded.started_at,note=excluded.note",
              (bid, pid, ct, t, note))
        extra = {"restart_count": b["restart_count"] + 1} if restart else {}
        if not restart:                                       # manual start: give auto-restart a fresh crash budget
            db.ex("UPDATE bot_processes SET restarts_in_window=0, window_start=NULL WHERE bot_id=?", (bid,))
        set_bot(bid, status="running", desired_state="running", last_restart=t, last_activity=t,
                auto_stopped=0, last_error=None, **extra)
        alog(b["user_id"], "bot_start", b["name"], bid)
    await asyncio.sleep(3)                                    # fail fast: did it die immediately (syntax error, no token...)?
    if not await A(bot_is_running, bid):
        code = await A(reap, bid)
        tail = redact(await A(tail_file, LOG_DIR / "bots" / f"bot_{bid}.log", 12), extra=list(dec_env(b["env_enc"]).values()))[-600:]
        set_bot(bid, status="crashed", last_error=f"Exited immediately (code {code})\n{tail}"[:700])
        cause, fix = diagnose(tail)
        return False, f"Bot start hote hi band ho gaya (code {code}).\n🔍 Wajah: {cause}\n🛠 Upay: {fix}\n\nAakhri log:\n{tail}"
    spawn(post_start_tasks(bid))
    return True, "Bot started."


async def stop_bot(bid: int, auto: bool = False, reason: str = "") -> tuple[bool, str]:
    async with LOCKS[bid]:
        b = get_bot(bid)
        if not b:
            return False, "Bot not found."
        pid, ct = await A(bot_pid, bid)
        if pid:
            await A(kill_tree, pid, ct, 8.0)
        await A(reap, bid)
        _close_job(bid)
        await A(db.ex, "UPDATE bot_processes SET pid=NULL WHERE bot_id=?", (bid,))
        STATS.pop(bid, None)
        CPU_HIST.pop(bid, None)
        set_bot(bid, status="stopped", desired_state="stopped", auto_stopped=1 if auto else 0,
                **({"last_error": reason} if reason else {}))
        alog(b["user_id"], "bot_stop", reason or b["name"], bid)
        return True, "Bot stopped."


async def restart_bot(bid: int):
    await stop_bot(bid)
    return await start_bot(bid, restart=True)


async def delete_bot(bid: int):
    b = get_bot(bid)
    if not b:
        return
    await stop_bot(bid)
    try:
        await A(rmtree_force, bot_dir(b))
    except UnsafeArchive:
        pass
    for p in (LOG_DIR / "bots").glob(f"bot_{bid}*.log"):
        p.unlink(missing_ok=True)
    shutil.rmtree(UPLOAD_DIR / f"user_{b['user_id']}" / f"bot_{bid}", ignore_errors=True)
    db.ex("DELETE FROM hosted_bots WHERE id=?", (bid,))
    alog(b["user_id"], "bot_delete", b["name"], bid)


async def get_logs(bid: int, n=60) -> str:
    b = get_bot(bid)
    extra = list(dec_env(b["env_enc"]).values()) if b else []
    txt = await A(tail_file, LOG_DIR / "bots" / f"bot_{bid}.log", n)
    return redact(txt, extra)[-60000:]


async def pipeline(bid: int, start: bool = True):
    """Install dependencies and start (runs in the background - never blocks the event loop)."""
    b = get_bot(bid)
    if not b:
        return
    if not ENGINE["ok"]:
        set_bot(bid, status="error", last_error="Unsupported environment")
        await notify(b["user_id"], "❌ Hosting is not supported on this server right now. The admin has been informed.")
        return
    set_bot(bid, status="installing")
    ok, tail = await install_deps(b)
    if not ok:
        set_bot(bid, status="error", last_error=tail[-400:])
        await notify(b["user_id"], f"❌ <b>Dependency installation failed</b> for <b>{esc(b['name'])}</b>:\n<pre>{esc(tail[-900:])}</pre>\n"
                                   "Upload a corrected requirements.txt and I will retry automatically.")
        return
    if not start:
        set_bot(bid, status="stopped")
        return
    ok, msg = await start_bot(bid)
    if ok:
        await notify(b["user_id"], f"✅ <b>{esc(b['name'])}</b> is now running (ID <code>{bid}</code>).", markup=Kb([[Btn("🤖 Open", callback_data=f"b:view:{bid}")]]))
    else:
        await notify(b["user_id"], f"❌ <b>{esc(b['name'])}</b> could not start:\n<pre>{esc(msg[-900:])}</pre>")




# ---------------------------------------------------------------------
# 5b. CRASH DIAGNOSIS, BOT IDENTITY (@username via Telegram getMe), ACTIVITY INSPECTION
# ---------------------------------------------------------------------
TOKEN_RE = re.compile(r"(?<![\w:])(\d{6,12}:[A-Za-z0-9_-]{30,})(?![\w-])")
DIAG = [
    (r"No space left on device|Errno 28|disk quota|database or disk is full|disk I/O error", "Storage/disk full ho gaya — likhne ke liye jagah nahi bachi", "📂 Bot ke bade/purane data, downloads aur logs hatayein (bot card me Storage dekhein). Zyada jagah chahiye to Admin se Premium/zyada Storage maangein, phir Restart."),
    (r"Too many open files|Errno 24", "Bot bahut saari files/connections khol raha hai (limit paar)", "Code me file/connection use ke baad band karein (with open(...) use karein), phir Restart."),
    (r"FloodWait|RetryAfter|Too Many Requests|\b429\b", "Telegram ne bot ko slow kar diya (bahut zyada messages — flood limit)", "Ek saath bahut messages na bhejein; messages ke beech delay (sleep) rakhein."),
    (r"Address already in use|Errno 98|Errno 10048", "Code jo port use kar raha hai wo pehle se kisi aur ne le rakha hai", "Alag port use karein ya bot ka ek hi instance chalayein (normal Telegram polling bot ko port nahi chahiye)."),
    (r"UnicodeEncodeError|UnicodeDecodeError", "Text encoding ki galti (Hindi/emoji wali file padhte ya print karte waqt)", "File kholte waqt open(file, encoding='utf-8') use karein."),
    (r"Segmentation fault|SIGSEGV|core dumped", "Python ki koi native library crash ho gayi", "requirements.txt me us library ka dusra (stable) version likhein."),
    (r"unexpected keyword argument|has no attribute '(Update|ApplicationBuilder|Updater|Dispatcher|executor|run_polling)'|cannot import name '(Updater|Filters)'", "python-telegram-bot/aiogram ka version aapke code se match nahi karta", "requirements.txt me wahi version likhein jis par code bana tha (purane code ke liye jaise python-telegram-bot==13.15, naye ke liye ==21.6)."),
    (r"CERTIFICATE_VERIFY_FAILED|SSLError|ssl\.SSL", "Secure connection (SSL) ki dikkat", "Restart karein; server ki date/time sahi hai ye check karein."),
    (r"No module named '([\w\.\-]+)'", "Ek Python library install nahi hai: {0}", "requirements.txt me us library ko add karke dobara upload karein, ya ⚙️ Settings se libraries dobara install karwayein."),
    (r"Unauthorized|InvalidToken|Not Found.*bot|401", "BOT_TOKEN galat hai ya revoke ho chuka hai", "BotFather se sahi/naya token lein aur ⚙️ Settings → Environment variables me BOT_TOKEN set karein, phir Restart."),
    (r"Conflict: terminated by other getUpdates|terminated by other", "Isi token ka bot kahin aur bhi chal raha hai (purana server/laptop/dusra instance)", "Dusri jagah wala bot band karein. Ek token par sirf ek hi bot chal sakta hai."),
    (r"SyntaxError|IndentationError|TabError", "Code me syntax/indentation ki galti hai", "Code theek karke dobara upload karein. Logs me line number likha hota hai."),
    (r"KeyError: '?(BOT_TOKEN|TOKEN|API_[A-Z_]+)'?|environ.*None|TOKEN.*not set|token.*missing", "Zaroori environment variable set nahi hai", "⚙️ Settings → Environment variables me BOT_TOKEN (aur jo chahiye) daalein."),
    (r"MemoryError|Killed|out of memory|RAM limit", "RAM kam padi (memory limit)", "Bot ko kam memory me chalayein ya admin se Premium/zyada RAM maangein."),
    (r"FileNotFoundError|No such file or directory", "Code kisi file ko dhundh raha hai jo maujood nahi", "Zaroori file (data/config) zip me daal kar dobara upload karein; path relative rakhein."),
    (r"PermissionError", "File/folder ki permission ki dikkat", "Bot sirf apne folder me likh sakta hai; bahar ke path use na karein."),
    (r"database is locked|OperationalError", "Database lock/error", "Ek hi database file ko do jagah se na kholein; bot Restart karein."),
    (r"ConnectionError|TimeoutError|getaddrinfo|NetworkError|ConnectTimeout|ReadTimeout|Name or service", "Internet/network ki dikkat", "Thodi der baad Restart karein. Baar-baar ho to server ka internet check karein."),
    (r"RuntimeError.*event loop|asyncio.*already running", "asyncio/event-loop ka code galat tareeke se chal raha hai", "Library version (python-telegram-bot) code se match karein; requirements.txt me version likhein."),
    (r"ImportError|cannot import name", "Library ka version code se match nahi karta", "requirements.txt me sahi version (package==version) likhkar dobara upload karein."),
]


EXIT_NOTES = {
    0: "Bot ka code poora chal kar khatam ho gaya (exit 0) — polling/loop chal hi nahi raha",
    1: "Code ke andar error aaya (exit 1)",
    2: "Galat command ya syntax (exit 2)",
    137: "Zabardasti band kiya gaya (kill) — aksar RAM limit ya system ne roka",
    -9: "Zabardasti band kiya gaya (kill) — aksar RAM limit ya system ne roka",
    143: "Normal tareeke se terminate kiya gaya",
    -15: "Normal tareeke se terminate kiya gaya",
    139: "Segmentation fault (native library crash)",
    -11: "Segmentation fault (native library crash)",
    3221225786: "Windows par process band kiya gaya (Ctrl+C / close)",
    3221225477: "Windows memory access violation (native library crash)",
}


def exit_meaning(code) -> str:
    return EXIT_NOTES.get(code, "") if code is not None else ""


def diagnose(text: str, exit_code=None) -> tuple[str, str]:
    t = text or ""
    for pat, cause, fix in DIAG:
        m = re.search(pat, t, re.I)
        if m:
            return cause.format(*(m.groups() or ("",))), fix
    last = [l for l in t.strip().splitlines() if l.strip()]
    if exit_code == 0 and not last:
        return (EXIT_NOTES[0], "Code me <code>app.run_polling()</code> / <code>executor.start_polling()</code> / <code>bot.infinity_polling()</code> jaisi line ho aur "
                               "sahi Main file chuni ho (⚙️ Settings → Main file).")
    if exit_code in EXIT_NOTES and exit_code not in (0, 1) and not last:
        return EXIT_NOTES[exit_code], "📜 Logs dekhein; RAM/Storage limit ya library ki dikkat ho sakti hai (🩺 Crash jaanch dabayein)."
    return ("Code ke andar koi error aaya" + (f": {last[-1][:140]}" if last else ""),
            "📜 Logs dekhein; error ki aakhri line wajah batati hai. Theek karke dobara upload karein.")


def find_tokens(b, d: Path) -> list[str]:
    toks = []
    for v in dec_env(b["env_enc"]).values():
        toks += TOKEN_RE.findall(v or "")
    if not toks:
        n = 0
        for p in walk_files(d):
            if p.suffix.lower() in (".py", ".env", ".txt", ".json", ".ini", ".cfg") and p.stat().st_size < 300_000:
                try:
                    toks += TOKEN_RE.findall(p.read_text(errors="ignore"))
                except OSError:
                    pass
                n += 1
                if n > 40 or toks:
                    break
    return list(dict.fromkeys(toks))[:3]


def fetch_identity(bid: int) -> str:
    """Asks Telegram (getMe) who the hosted bot is. The token is used only for this call and never stored or shown."""
    import urllib.request, urllib.error
    b = get_bot(bid)
    if not b:
        return "bot nahi mila"
    d = bot_dir(b)
    toks = find_tokens(b, d)
    if not toks:
        set_bot(bid, tg_checked=iso())
        return "BOT_TOKEN nahi mila (env me ya code me)"
    err = "?"
    for tok in toks:
        try:
            with urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/getMe", timeout=8) as r:
                res = json.loads(r.read().decode())["result"]
            set_bot(bid, tg_username=res.get("username"), tg_name=res.get("first_name"), tg_checked=iso())
            return f"@{res.get('username')}"
        except urllib.error.HTTPError as e:
            err = f"Telegram ne token reject kiya ({e.code})"
        except Exception as e:
            err = redact(str(e), extra=toks)[:100]
    set_bot(bid, tg_checked=iso())
    return err


CODE_FLAGS = [
    (r"xmrig|minerd|stratum\+tcp|cryptonight|monero", "🔴", "Crypto-mining jaisa code"),
    (r"HOSTING_BOT_TOKEN|BACKUP_PASSPHRASE|hosting\.db|\.secret\.key|manager\.pid|\.tghbak|MASTER_BACKUP", "🔴", "Manager ke secret/files ko chhune ki koshish"),
    (r"/etc/passwd|/etc/shadow|\.ssh/|id_rsa|\.aws/credentials", "🔴", "System ke secret files padhne ka code"),
    (r"\.\./\.\./|\.\.\\\.\.\\", "🟡", "Apne folder se bahar jaane wale path"),
    (r"os\.system\(|subprocess\.|Popen\(", "🟡", "Computer par command chalata hai"),
    (r"socket\.socket\(|\bnmap\b|scapy", "🟡", "Raw network/port scan jaisa code"),
    (r"\bexec\(|\beval\(|b64decode", "🟡", "Chhupa/dynamic code chalane ka tareeka"),
    (r"shutil\.rmtree|os\.remove\(|os\.rmdir\(", "🟡", "Files delete karne ka code"),
    (r"smtplib|ftplib|paramiko", "🟡", "Email/FTP/SSH use karta hai"),
]


def scan_code(d: Path) -> list[tuple[str, str, str]]:
    found = {}
    n = 0
    for p in walk_files(d):
        if p.suffix.lower() != ".py" or p.stat().st_size > 1_000_000:
            continue
        n += 1
        if n > 300:
            break
        try:
            txt = p.read_text(errors="ignore")
        except OSError:
            continue
        for pat, sev, why in CODE_FLAGS:
            if why not in found and re.search(pat, txt, re.I):
                found[why] = (sev, why, p.relative_to(d).as_posix())
    return sorted(found.values(), key=lambda x: x[0] != "🔴")


def inspect_bot(bid: int) -> str:
    """Plain-language activity report for the admin (what is this bot doing right now?)."""
    b = get_bot(bid)
    if not b:
        return "Bot nahi mila."
    out = []
    pid, ct = bot_pid(bid)
    if pid and proc_alive(pid, ct):
        try:
            root = psutil.Process(pid)
            procs = [root] + root.children(recursive=True)
            out.append(f"⚙️ Process: {len(procs)} chal rahe")
            conns, ips = 0, {}
            odd = set()
            files = threads = 0
            for p in procs:
                try:
                    threads += p.num_threads()
                    files += len(p.open_files())
                    cs = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
                    for c in cs:
                        if c.raddr:
                            conns += 1
                            ips[c.raddr.ip] = ips.get(c.raddr.ip, 0) + 1
                            if c.raddr.port not in (80, 443, 53, 8443):
                                odd.add(c.raddr.port)
                except (psutil.Error, OSError):
                    pass
            out.append(f"🧵 Threads {threads} · 📂 Khuli files {files}")
            out.append(f"🌐 Internet connections: {conns}" + (" · remote: " + ", ".join(f"{ip}×{n}" for ip, n in sorted(ips.items(), key=lambda x: -x[1])[:6]) if ips else ""))
            if odd:
                out.append(f"🟡 Ye non-web ports par connection hai: {sorted(odd)[:8]} (normal bot sirf 443/80 use karta hai)")
            if conns > 60:
                out.append("🟡 Bahut zyada connections — scanning/spam ka shak ho sakta hai")
            cmd = " ".join(root.cmdline())[:200]
            out.append(f"▶️ Command: <code>{html.escape(redact(cmd))}</code>")
        except psutil.Error as e:
            out.append(f"(process padh nahi paya: {type(e).__name__})")
    else:
        out.append("⏹ Abhi process chal nahi raha.")
    flags = scan_code(bot_dir(b))
    out.append("\n🔎 <b>Code jaanch</b> (sirf sanket hain, pakka saboot nahi):")
    out += [f"{s} {w} — <i>{html.escape(f)}</i>" for s, w, f in flags] or ["✅ Koi shak wali cheez nahi mili"]
    log = redact(tail_file(LOG_DIR / "bots" / f"bot_{bid}.log", 12), extra=list(dec_env(b["env_enc"]).values()))
    out.append("\n📜 <b>Aakhri log:</b>\n<pre>" + html.escape(log[-900:] or "(khaali)") + "</pre>")
    if b["last_error"]:
        c, f = diagnose(b["last_error"])
        out.append(f"\n🩺 <b>Crash ki wajah:</b> {html.escape(c)}\n🛠 <b>Upay:</b> {html.escape(f)}")
    return "\n".join(out)


async def post_start_tasks(bid: int):
    """After a start: learn the bot's @username and scan the code; alert admins on red flags."""
    try:
        await A(fetch_identity, bid)
        b = get_bot(bid)
        if not b:
            return
        reds = [x for x in await A(scan_code, bot_dir(b)) if x[0] == "🔴"]
        if reds:
            await notify_admins(f"🔴 <b>Shak wala code</b> — Bot #{bid} (user <code>{b['user_id']}</code>, @{b['tg_username'] or '?'}):\n"
                                + "\n".join(f"• {esc(w)} ({esc(f)})" for _, w, f in reds) + "\nAdmin Panel → Sabhi Bots → is bot par 🔎 Jaanch dabayein.",
                                "bots_view", kind=f"scan_{bid}", day=iso()[:10])
    except Exception:
        log.exception("post_start_tasks")




# =====================================================================
# 5c. CRASH ANALYSIS (with storage/RAM check), ORIGINAL UPLOADS, FILE VIEWER, REFERRAL
# =====================================================================
MANAGER = {"username": None}
UPLOAD_KEEP = 8                                   # originals kept per bot
UPLOAD_CAP_BYTES = 200 * 1024 * 1024              # max originals per bot
MiB = 1024 * 1024


def usage_pcts(b) -> dict:
    """How full is this bot's storage / RAM / CPU compared with its limits (24h peak for RAM/CPU)."""
    lim = bot_plan(b)["limits"]
    pk = db.one("SELECT MAX(mem_bytes) AS m, MAX(cpu) AS c FROM resource_usage WHERE bot_id=? AND ts>?", (b["id"], iso(now() - timedelta(hours=24))))
    try:
        disk = dir_size(bot_dir(b))
    except Exception:
        disk = 0
    mem = (pk["m"] or 0) if pk else 0
    cpu = (pk["c"] or 0) if pk else 0
    return {"disk": disk, "mem": mem, "cpu": cpu, "lim": lim,
            "sto": disk / max(lim["storage_mb"] * MiB, 1) * 100, "ram": mem / max(lim["ram_mb"] * MiB, 1) * 100,
            "cpu_pct": cpu / max(lim["cpu"], 1) * 100}


def record_crash(b, code, cause: str, fix: str, tail: str) -> dict:
    u = usage_pcts(b)
    db.ex("INSERT INTO crash_log(bot_id,ts,exit_code,cause,fix,tail,sto_pct,ram_pct) VALUES(?,?,?,?,?,?,?,?)",
          (b["id"], iso(), code, (cause or "")[:300], (fix or "")[:400], (tail or "")[-400:], u["sto"], u["ram"]))
    db.ex("DELETE FROM crash_log WHERE bot_id=? AND id NOT IN (SELECT id FROM crash_log WHERE bot_id=? ORDER BY id DESC LIMIT 50)", (b["id"], b["id"]))
    return u


def crash_report(bid: int) -> str:
    """Plain-language 'why did my bot crash and what should I do' report."""
    b = get_bot(bid)
    if not b:
        return "Bot nahi mila."
    d = bot_dir(b)
    bp = bot_plan(b)
    lim = bp["limits"]
    row = db.one("SELECT last_exit_code FROM bot_processes WHERE bot_id=?", (bid,))
    code = row["last_exit_code"] if row else None
    lp = LOG_DIR / "bots" / f"bot_{bid}.log"
    log_txt = redact(tail_file(lp, 200), list(dec_env(b["env_enc"]).values()))
    cause, fix = diagnose(log_txt + "\n" + (b["last_error"] or ""), code)
    u = usage_pcts(b)
    n24 = db.val("SELECT COUNT(*) FROM crash_log WHERE bot_id=? AND ts>?", (bid, iso(now() - timedelta(hours=24))))
    checks, steps = [], []

    def add(ok, text, step=None):
        checks.append(("✅" if ok else "⚠️") + " " + text)
        if not ok and step:
            steps.append(step)
    add(u["sto"] < 85, f"💽 Storage: {human(u['disk'])} / {lim['storage_mb']} MB ({u['sto']:.0f}%)" + ("" if u["sto"] < 85 else " — lagbhag full!"),
        "💽 Storage khaali karein: bade data/download/purane logs hatayein (📂 Meri files me dekhein), ya Admin se Premium/zyada Storage maangein.")
    add(u["ram"] < 85, f"🧠 RAM (24h peak): {human(u['mem'])} / {lim['ram_mb']} MB ({u['ram']:.0f}%)" + ("" if u["ram"] < 85 else " — memory kam pad rahi hai!"),
        "🧠 Memory kam use karein (badi list/file ek saath load na karein) ya Admin se zyada RAM (Premium) maangein.")
    add(u["cpu_pct"] < 90, f"⚙️ CPU (24h peak): {u['cpu']:.0f}% (limit {lim['cpu']}% = {lim['cpu'] / 100:g} core)" + ("" if u["cpu_pct"] < 90 else " — CPU limit ke paas!"),
        "⚙️ Bhaari loop me time.sleep()/await asyncio.sleep() daalein; infinite busy-loop na rakhein.")
    py = venv_python(d)
    add(py.exists(), "📦 Libraries (.venv): " + ("taiyaar" if py.exists() else "install nahi hui"), "📦 ⚙️ Settings → 'Libraries dobara install' dabayein ya requirements.txt bhejein.")
    ent = b["entry"]
    add(bool(ent) and (d / ent).is_file(), f"🎯 Main file: {ent or 'set nahi'}", "🎯 ⚙️ Settings → Main file me sahi .py file chunein.")
    try:
        tok_ok = bool(find_tokens(b, d))
    except Exception:
        tok_ok = False
    add(tok_ok, "🔑 BOT_TOKEN: " + ("mila" if tok_ok else "nahi mila"), "🔑 ⚙️ Settings → Environment variables me BOT_TOKEN=... daalein.")
    lsz = lp.stat().st_size if lp.exists() else 0
    add(lsz < 20 * MiB, f"📜 Log file: {human(lsz)}", "📜 Code me bahut zyada print() na karein (log bahut bada ho raha hai).")
    add(bp["active"], f"📅 Plan: {bp['name']}", "📅 Plan/Trial khatam hai — 📞 Contact Admin se Premium lein.")
    add(n24 < 3, f"🔁 Pichhle 24 ghante me crash: {n24}", "🔁 Baar-baar crash ho raha hai: pehle 📜 Logs ki aakhri error line theek karein.")
    top = ""
    if u["sto"] >= 95 or "Storage" in cause:
        top = "💽 <b>Sabse mumkin wajah: Storage full</b> — bot ke paas likhne ki jagah nahi bachi."
    elif u["ram"] >= 90 or "RAM" in cause or code in (137, -9):
        top = "🧠 <b>Sabse mumkin wajah: RAM kam padi</b> — bot memory limit paar kar gaya."
    elif not tok_ok:
        top = "🔑 <b>Sabse mumkin wajah: BOT_TOKEN nahi mila.</b>"
    elif not py.exists():
        top = "📦 <b>Sabse mumkin wajah: libraries install nahi hui.</b>"
    else:
        top = f"🔍 <b>Sabse mumkin wajah (log se):</b> {html.escape(cause)}"
    out = [f"🩺 <b>Crash jaanch — {html.escape(b['name'])}</b> (Bot ID <code>{bid}</code>)",
           f"{STATUS_ICON.get(b['status'], b['status'])}" + (f" · aakhri exit code: <b>{code}</b> — {html.escape(exit_meaning(code))}" if code is not None and exit_meaning(code) else ""),
           "", top, "", f"🔍 <b>Log se wajah:</b> {html.escape(cause)}", f"🛠 <b>Upay:</b> {fix}", "", "📋 <b>Jaanch ki list:</b>"] + checks
    if steps:
        out += ["", "➡️ <b>Ab ye karein:</b>"] + [f"{i}. {t}" for i, t in enumerate(dict.fromkeys(steps), 1)]
    if log_txt.strip():
        out += ["", "📜 <b>Log ki aakhri lines:</b>", "<pre>" + html.escape("\n".join(log_txt.strip().splitlines()[-8:])[-700:]) + "</pre>"]
    return "\n".join(out)


def save_upload(bid: int, uid: int, filename: str, kind: str, src: Path):
    """Keeps the ORIGINAL file/zip a user uploaded (so an admin can see exactly what was uploaded)."""
    d = UPLOAD_DIR / f"user_{uid}" / f"bot_{bid}"
    d.mkdir(parents=True, exist_ok=True)
    clean = re.sub(r"[^\w.\- ]", "_", filename or "upload")[-70:] or "upload"
    if not Path(clean).suffix:
        clean += {"py": ".py", "zip": ".zip", "req": ".txt"}.get(kind, "")
    dst = d / f"{now():%Y%m%d_%H%M%S}_{clean}"
    shutil.copy2(src, dst)
    h = hashlib.sha256()
    with open(dst, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    db.ex("INSERT INTO bot_uploads(bot_id,user_id,filename,kind,size,sha256,saved_path,uploaded_at) VALUES(?,?,?,?,?,?,?,?)",
          (bid, uid, filename or clean, kind, dst.stat().st_size, h.hexdigest(), str(dst), iso()))
    total = 0
    for i, r in enumerate(db.all("SELECT id,saved_path,size FROM bot_uploads WHERE bot_id=? ORDER BY id DESC", (bid,))):
        total += r["size"]
        if i >= UPLOAD_KEEP or total > UPLOAD_CAP_BYTES:
            Path(r["saved_path"]).unlink(missing_ok=True)
            db.ex("DELETE FROM bot_uploads WHERE id=?", (r["id"],))


def py_list(bid: int) -> list[str]:
    b = get_bot(bid)
    d = bot_dir(b)
    files = sorted(p.relative_to(d).as_posix() for p in walk_files(d) if p.suffix == ".py")
    if b["entry"] in files:
        files.remove(b["entry"])
        files.insert(0, b["entry"])
    return files[:12]


def file_tree_text(bid: int) -> str:
    b = get_bot(bid)
    d = bot_dir(b)
    items = []
    for p in walk_files(d):
        try:
            items.append((p.relative_to(d).as_posix(), p.stat().st_size))
        except OSError:
            pass
    items.sort()
    total = sum(sz for _, sz in items)
    lines = [f"{'⭐' if rel == b['entry'] else '📄'} <code>{html.escape(rel[:60])}</code> — {human(sz)}" for rel, sz in items[:30]]
    if len(items) > 30:
        lines.append(f"… aur {len(items) - 30} files")
    return "\n".join(lines) + f"\n\n📦 Kul: {len(items)} files · {human(total)}  (⭐ = main file)" if items else "(folder khaali hai)"


# ---------------- referral + global settings (trial days, referral bonus) ----------------
GLOBAL_DEFAULTS = {"trial_days": TRIAL_DAYS, "ref_bonus": 2, "ref_max": 25}
GLOBAL_KEYS = [("trial_days", "🎁 Free trial (din)", 1, 1), ("ref_bonus", "🤝 Referral bonus (din)", 1, 0), ("ref_max", "🤝 Max referrals / user", 5, 1)]


def get_global(key: str) -> int:
    return int(get_setting(key, GLOBAL_DEFAULTS[key]))


def step_global(key: str, direction: int):
    spec = next(x for x in GLOBAL_KEYS if x[0] == key)
    set_setting(key, int(max(spec[3], get_global(key) + direction * spec[2])))


def parse_ref(update) -> int | None:
    t = (update.message.text or "") if update.message else ""
    m = re.match(r"^/start(?:@\w+)?\s+ref_(\d{3,15})$", t.strip())
    return int(m[1]) if m else None


def apply_referral(referrer: int, new_uid: int) -> dict | None:
    """Referrer gets +N free-hosting days — only for brand-new users, once per user, no self-referral, capped."""
    if referrer == new_uid or not db.one("SELECT 1 FROM users WHERE id=?", (referrer,)):
        return None
    if db.one("SELECT 1 FROM referrals WHERE referred_id=?", (new_uid,)):
        return None
    if db.val("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (referrer,)) >= get_global("ref_max"):
        return {"capped": True}
    bonus = get_global("ref_bonus")
    if bonus <= 0:
        return None
    db.ex("INSERT INTO referrals(referrer_id,referred_id,created_at,bonus_days) VALUES(?,?,?,?)", (referrer, new_uid, iso(), bonus))
    tr = db.one("SELECT expires_at FROM trials WHERE user_id=?", (referrer,))
    base = max(pdt(tr["expires_at"]), now()) if tr else now()
    new_exp = iso(base + timedelta(days=bonus))
    if tr:
        db.ex("UPDATE trials SET expires_at=?, extended_days=extended_days+?, status='active' WHERE user_id=?", (new_exp, bonus, referrer))
    else:
        db.ex("INSERT INTO trials(user_id,started_at,expires_at) VALUES(?,?,?)", (referrer, iso(), new_exp))
    alog(referrer, "referral", f"+{bonus}d for new user {new_uid}")
    return {"bonus": bonus, "expires": new_exp}


async def handle_referral(referrer: int, newbie):
    try:
        r = await A(apply_referral, referrer, newbie.id)
        if r and r.get("bonus"):
            await notify(referrer, f"🎉 <b>Referral bonus!</b>\n{esc(newbie.first_name or 'Ek naya user')} aapke link se juda.\n"
                                   f"🎁 Aapko <b>+{r['bonus']} din free hosting</b> mili. Ab free hosting: <b>{dleft(r['expires'])} din baaki</b>.")
            await after_entitlement_change(referrer)          # bots that were stopped after the trial ended start again
        elif r and r.get("capped"):
            await notify(referrer, "ℹ️ Naya user aapke link se juda, lekin referral bonus ki limit poori ho chuki hai.")
    except Exception:
        log.exception("referral failed")


async def menu_refer(update, ctx):
    uid = update.effective_user.id
    me = await ctx.bot.get_me()
    MANAGER["username"] = me.username
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    n = await A(db.val, "SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,))
    days = await A(db.val, "SELECT COALESCE(SUM(bonus_days),0) FROM referrals WHERE referrer_id=?", (uid,))
    bonus, mx = await A(get_global, "ref_bonus"), await A(get_global, "ref_max")
    share = "https://t.me/share/url?url=" + urllib.parse.quote(link) + "&text=" + urllib.parse.quote("Python Telegram bot ki free hosting 🚀")
    await say(update, f"🤝 <b>Refer & Earn</b>\n\nApna link dosto ko bhejein. Jo bhi <b>naya user</b> is link se bot start karega, "
                      f"aapko <b>{bonus} din ki free hosting</b> mil jayegi.\n\n🔗 <b>Aapka link:</b>\n<code>{link}</code>\n\n"
                      f"👥 Aapke referrals: <b>{n}/{mx}</b>\n🎁 Ab tak mile din: <b>{days}</b>\n\n"
                      "ℹ️ Sirf naye users count hote hain. Khud ka link ya purana user count nahi hota.",
              Kb([[Btn("📤 Link share karein", url=share)]]))



# =====================================================================
# 5d. FORCE-SUBSCRIBE  (users must join your channel(s) before hosting; channels are managed from the Admin Panel)
# =====================================================================
FS_CACHE: dict = {}
GLOBAL_RANGE = {"trial_days": (1, 3650), "ref_bonus": (0, 365), "ref_max": (1, 100000)}


def set_global_value(key: str, val: int) -> int:
    lo, hi = GLOBAL_RANGE[key]
    val = max(lo, min(hi, int(val)))
    set_setting(key, val)
    return val


def fs_enabled() -> bool:
    return bool(get_setting("fs_enabled", True))


def fs_grace_hours() -> int:
    return max(1, int(get_setting("fs_grace_hours", 24)))


def fs_channels() -> list:
    return db.all("SELECT * FROM fs_channels ORDER BY id")


def fs_mark(cid: int, err: str | None):
    if err:
        db.ex("UPDATE fs_channels SET can_check=0,last_error=? WHERE id=?", (err[:200], cid))
    else:
        db.ex("UPDATE fs_channels SET can_check=1,last_error=NULL WHERE id=? AND can_check=0", (cid,))


def fs_parse_ref(text: str):
    """'@name' / 't.me/name' / 'https://t.me/name' / '-100123...' (+ optional invite link) -> (chat_ref, link, error)."""
    ref, link = None, None
    for t in text.replace(",", " ").split():
        m = re.match(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(.+)$", t, re.I)
        if m:
            path = m[1].split("?")[0].strip("/")
            if path.startswith("+") or path.startswith("joinchat/"):
                link = "https://t.me/" + path
                continue
            name = path.split("/")[0]
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", name):
                ref = ref or "@" + name
                continue
            return None, None, "Link samajh nahi aaya."
        if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", t):
            ref = ref or t
        elif re.fullmatch(r"-100\d{5,}", t):
            ref = ref or t
    if not ref:
        return None, link, "Channel ka @username (public) ya numeric ID -100... (private) chahiye."
    return ref, link, None


async def fs_is_member(ch, uid: int):
    """True = joined · False = not joined · None = cannot be verified (bot is not admin there / network problem)."""
    chat = str(ch["chat_id"] or ch["chat_ref"])
    chat_id = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
    for _ in range(2):
        try:
            await throttle()
            m = await APP.bot.get_chat_member(chat_id=chat_id, user_id=uid)
            st = getattr(m.status, "value", m.status)
            if not ch["can_check"]:
                await A(fs_mark, ch["id"], None)
            if st in ("member", "administrator", "creator"):
                return True
            if st == "restricted":
                return bool(getattr(m, "is_member", True))
            return False
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
        except BadRequest as e:
            low = str(e).lower()
            if "user not found" in low or "participant_id_invalid" in low:
                return False
            await A(fs_mark, ch["id"], str(e))
            return None
        except Forbidden as e:
            await A(fs_mark, ch["id"], str(e))
            return None
        except TelegramError:
            return None
    return None


async def fs_alert_admin(unver: list):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    for c in unver:
        await notify_admins(f"⚠️ <b>Channel join check nahi ho raha:</b> {esc(c['title'] or c['chat_ref'])}\n"
                            "Bot ko us channel me <b>Admin</b> banayein (Channel → Administrators → Add admin → apna bot). "
                            "Jab tak ye theek nahi hota, kisi user ko roka nahi ja raha.", "settings", kind=f"fs_unver_{c['id']}", day=today)


async def fs_check(uid: int, force: bool = False) -> dict:
    """{'ok': bool, 'missing': [channels not joined], 'unverified': [channels that cannot be checked]} — admins are always ok."""
    ok_all = {"ok": True, "missing": [], "unverified": []}
    if await A(role_of, uid):
        return ok_all
    chans = await A(fs_channels)
    if not chans or not await A(fs_enabled):
        return ok_all
    c = FS_CACHE.get(uid)
    if not force and c and time.time() - c[0] < 120:
        return c[1]
    missing, unver = [], []
    for ch in chans:
        r = await fs_is_member(ch, uid)
        if r is False:
            missing.append(ch)
        elif r is None:
            unver.append(ch)
    res = {"ok": not missing, "missing": missing, "unverified": unver}      # unverifiable channels never block anyone
    FS_CACHE[uid] = (time.time(), res)
    if unver:
        spawn(fs_alert_admin(unver))
    return res


def fs_prompt(missing: list, mode: str = "new", grace: int = 24, left: int = 0):
    names = "\n".join(f"• <b>{esc(c['title'] or c['chat_ref'])}</b>" for c in missing)
    if mode == "warn":
        txt = (f"⚠️ <b>Zaroori suchna</b>\n\nAapke bot(s) hamare server par host hain. Neeche ke channel(s) join karna <b>zaroori</b> hai:\n{names}\n\n"
               f"⏳ <b>{grace} ghante</b> me join nahi kiya to aapke bots <b>band ho jayenge</b>.\nNeeche ke button se seedha join karein, phir ✅ dabayein.")
    elif mode == "remind":
        txt = (f"⏳ <b>Yaad dilana</b>\n\nAbhi tak ye channel(s) join nahi hue:\n{names}\n\n"
               f"Lagbhag <b>{left} ghante</b> baad aapke bots band ho jayenge. Join karke ✅ dabayein.")
    elif mode == "stopped":
        txt = (f"⛔ <b>Aapke bots band kar diye gaye</b>\n\nKyunki ye channel(s) join nahi kiye gaye:\n{names}\n\n"
               "Join karte hi ✅ dabayein — aapke bots apne aap dobara chalu ho jayenge.")
    else:
        txt = (f"🔒 <b>Pehle hamara channel join karein</b>\n\nBot host karne ke liye ye channel(s) join karna zaroori hai:\n{names}\n\n"
               "1️⃣ Neeche ke button se channel join karein\n2️⃣ Phir <b>✅ Maine join kar liya</b> dabayein")
    rows = [[Btn(f"📢 {(c['title'] or c['chat_ref'])[:30]} — Join karein", url=c["link"])] for c in missing]
    rows.append([Btn("✅ Maine join kar liya", callback_data="fs:ok")])
    return txt, Kb(rows)


async def fs_gate(update, ctx=None) -> bool:
    """True = user may host. Otherwise the 'join first' screen is shown and False is returned."""
    res = await fs_check(update.effective_user.id)
    if res["ok"]:
        return True
    txt, kb = fs_prompt(res["missing"])
    await say(update, txt, kb)
    return False


async def fs_resume(uid: int) -> int:
    st = await A(db.one, "SELECT * FROM fs_state WHERE user_id=?", (uid,))
    if not st:
        return 0
    ids = json.loads(st["stopped_bots"] or "[]")
    await A(db.ex, "DELETE FROM fs_state WHERE user_id=?", (uid,))
    n = 0
    for bid in ids:
        b = await A(get_bot, bid)
        if b and b["auto_stopped"]:
            ok, _ = await start_bot(bid)
            n += 1 if ok else 0
    return n


async def cb_fs(update, ctx, p):
    uid = update.callback_query.from_user.id
    res = await fs_check(uid, force=True)
    if res["ok"]:
        n = await fs_resume(uid)
        extra = f"\n▶️ Aapke {n} bots dobara chalu kiye gaye." if n else ""
        return await edit(update, "✅ <b>Shukriya!</b> Aapne channel join kar liya hai." + extra + "\n\nAb 🚀 <b>Host New Bot</b> dabakar apna bot host kar sakte hain.")
    txt, kb = fs_prompt(res["missing"])
    await edit(update, "❌ <b>Abhi tak join nahi dikha.</b> Pehle channel join karein, phir ✅ dabayein.\n\n" + txt.split("\n\n", 1)[1] +
               f"\n<i>Jaanch: {now():%H:%M:%S}</i>", kb)


async def fs_enforce(manual: bool = False) -> dict:
    """Existing users with bots: warn -> reminder -> stop after the grace time; resume automatically when they join."""
    st = {"checked": 0, "missing": 0, "warned": 0, "stopped": 0, "resumed": 0}
    if not await A(fs_enabled) or not await A(fs_channels):
        return st
    grace = await A(fs_grace_hours)
    users = await A(db.all, "SELECT DISTINCT u.id AS id FROM users u JOIN hosted_bots b ON b.user_id=u.id WHERE u.banned=0")
    for r in users:
        uid = r["id"]
        if await A(role_of, uid):
            continue
        res = await fs_check(uid, force=True)
        st["checked"] += 1
        row = await A(db.one, "SELECT * FROM fs_state WHERE user_id=?", (uid,))
        if res["ok"]:
            if row:
                n = await fs_resume(uid)
                if row["stopped_at"]:
                    await notify(uid, "✅ <b>Shukriya!</b> Channel join mil gaya." + (f" Aapke {n} bots dobara chalu kiye gaye." if n else ""))
                    st["resumed"] += 1
            continue
        st["missing"] += 1
        if not row:
            await A(db.ex, "INSERT INTO fs_state(user_id, first_warned_at) VALUES(?,?)", (uid, iso()))
            txt, kb = fs_prompt(res["missing"], "warn", grace)
            await notify(uid, txt, markup=kb)
            st["warned"] += 1
            continue
        if row["stopped_at"]:
            continue
        el = (now() - pdt(row["first_warned_at"])).total_seconds() / 3600
        if el >= grace:
            ids = []
            for b in await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=?", (uid,)):
                if b["status"] in ("running", "installing") or b["desired_state"] == "running":
                    await stop_bot(b["id"], auto=True, reason="Channel join nahi kiya")
                    ids.append(b["id"])
            await A(db.ex, "UPDATE fs_state SET stopped_at=?, stopped_bots=? WHERE user_id=?", (iso(), json.dumps(ids), uid))
            txt, kb = fs_prompt(res["missing"], "stopped")
            await notify(uid, txt, markup=kb)
            alog(uid, "fs_stop", f"bots {ids}")
            st["stopped"] += 1
        elif el >= grace / 2:
            txt, kb = fs_prompt(res["missing"], "remind", grace, left=max(1, int(grace - el)))
            await notify(uid, txt, kind="fs_remind", day=row["first_warned_at"], markup=kb)
    return st


async def fs_job(ctx=None):
    try:
        await fs_enforce()
    except Exception:
        log.exception("force-subscribe job failed")


async def fs_add_channel(ref: str, link: str | None, admin: int) -> tuple[bool, str]:
    try:
        chat = await APP.bot.get_chat(int(ref) if re.fullmatch(r"-?\d+", ref) else ref)
    except TelegramError as e:
        return False, f"❌ Channel nahi mila ({esc(e)}). @username sahi hai? Private channel ho to bot ko usme admin banakar numeric ID bhejein."
    username = getattr(chat, "username", None)
    title = getattr(chat, "title", None) or ref
    use_link = f"https://t.me/{username}" if username else link
    if not use_link:
        try:
            use_link = await APP.bot.export_chat_invite_link(chat.id)
        except TelegramError:
            return False, ("❌ Ye private channel hai, join-link chahiye. Dobara aise bhejein:\n<code>-100123456789 https://t.me/+abcdEFG</code>\n"
                           "(ya bot ko channel me admin banayein taaki wo khud link bana le)")
    dup = await A(db.one, "SELECT 1 FROM fs_channels WHERE lower(chat_ref)=lower(?) OR chat_id=? OR lower(chat_ref)=lower(?)",
                  (ref, str(chat.id), "@" + username if username else ref))
    if dup:
        return False, "ℹ️ Ye channel pehle se jodha hua hai."
    can, err = 1, None
    try:
        me = await APP.bot.get_chat_member(chat.id, APP.bot.id)
        if getattr(me.status, "value", me.status) not in ("administrator", "creator"):
            can, err = 0, "bot admin nahi"
    except TelegramError as e:
        can, err = 0, str(e)[:160]
    cref = "@" + username if username else str(chat.id)
    await A(db.ex, "INSERT INTO fs_channels(chat_ref,chat_id,title,username,link,added_by,added_at,can_check,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
            (cref, str(chat.id), title, username, use_link, admin, iso(), can, err))
    FS_CACHE.clear()
    await A(audit, admin, "fs_add", cref)
    return True, (f"✅ Channel jod diya: <b>{esc(title)}</b>" + ("" if can else
                  "\n\n⚠️ Bot is channel ka <b>ADMIN nahi</b> hai, isliye join check nahi ho sakta (kisi ko roka nahi jayega).\n"
                  "Channel → Administrators → Add admin → apna bot chuno, phir 🔍 Test dabayein."))


async def fs_test(c, admin_uid: int) -> str:
    chat = str(c["chat_id"] or c["chat_ref"])
    chat_id = int(chat) if re.fullmatch(r"-?\d+", chat) else chat
    lines = [f"🔍 <b>Test — {esc(c['title'] or c['chat_ref'])}</b>"]
    try:
        me = await APP.bot.get_chat_member(chat_id=chat_id, user_id=APP.bot.id)
        st = getattr(me.status, "value", me.status)
        lines.append("🤖 Bot ka status: " + ("✅ administrator" if st in ("administrator", "creator") else f"⚠️ {st} (admin banana zaroori)"))
    except TelegramError as e:
        lines.append(f"🤖 Bot ka status: ❌ {esc(e)}")
    r = await fs_is_member(c, admin_uid)
    lines.append("👤 Aap (admin) is channel me: " + {True: "✅ member", False: "❌ member nahi", None: "⚠️ check nahi ho paya"}[r])
    if r is not None:
        lines.append("✅ Join-check sahi kaam kar raha hai.")
    return "\n".join(lines)


async def fs_screen():
    en, grace, chans = await A(fs_enabled), await A(fs_grace_hours), await A(fs_channels)
    warned = await A(db.val, "SELECT COUNT(*) FROM fs_state WHERE stopped_at IS NULL")
    stopped = await A(db.val, "SELECT COUNT(*) FROM fs_state WHERE stopped_at IS NOT NULL")
    lines = []
    for i, c in enumerate(chans, 1):
        okt = "✅ check ho raha hai" if c["can_check"] else "⚠️ Bot channel ka ADMIN nahi — check nahi ho paa raha"
        lines.append(f"{i}. <b>{esc(c['title'] or c['chat_ref'])}</b> · <code>{esc(c['chat_ref'])}</code>\n    🔗 {esc(c['link'])}\n    {okt}")
    txt = (f"📣 <b>Channel join zaroori (Force-Subscribe)</b>\n\nStatus: {'🟢 <b>ON</b>' if en else '🔴 <b>OFF</b>'} · Purane users ko join karne ka time: <b>{grace} ghante</b>\n\n"
           + ("\n\n".join(lines) or "Koi channel nahi jodha hua (to kisi ko roka nahi ja raha).")
           + f"\n\n👥 Warning mili (abhi time hai): <b>{warned}</b> · Bots band kiye gaye: <b>{stopped}</b>\n\n"
           "ℹ️ <b>Naye user</b> join kiye bina bot host nahi kar sakte. <b>Purane users</b> (jinke bots chal rahe hain) ko message jata hai; "
           "grace time me join na kiya to bots band; join karte hi apne aap chalu.\n"
           "⚠️ Bot ko channel me <b>Admin</b> banana zaroori hai, warna Telegram join check karne nahi deta.")
    rows = [[Btn(f"🔒 Join zaroori: {'ON ✅' if en else 'OFF ❌'} (badalne ke liye dabayein)", callback_data="A:fst")]]
    for c in chans:
        rows.append([Btn(f"🔍 Test: {(c['title'] or c['chat_ref'])[:16]}", callback_data=f"A:fsc:{c['id']}"),
                     Btn(f"❌ Hatao: {(c['title'] or c['chat_ref'])[:16]}", callback_data=f"A:fsx:{c['id']}")])
    rows += [[Btn("➕ Channel jodo", callback_data="A:fsa")],
             [Btn("➖6", callback_data="A:fsg:-6"), Btn(f"⏳ Grace: {grace} ghante", callback_data="A:noop"), Btn("➕6", callback_data="A:fsg:6"),
              Btn("✏️", callback_data="A:fsgc")],
             [Btn("👥 Kisne join nahi kiya", callback_data="A:fsl"), Btn("🔄 Abhi sabko check karo", callback_data="A:fsr")]]
    return txt, nav(*rows)


async def do_trial_adjust(admin: int, uid: int, days: int) -> str:
    """days > 0 adds, days < 0 removes free-trial days (trial ends if it goes below today)."""
    if days > 0:
        return await do_extend(admin, uid, days)
    tr = await A(db.one, "SELECT * FROM trials WHERE user_id=?", (uid,))
    if not tr:
        return "User nahi mila."
    new = pdt(tr["expires_at"]) + timedelta(days=days)
    if new <= now():
        await A(db.ex, "UPDATE trials SET expires_at=?, status='expired' WHERE user_id=?", (iso(now()), uid))
        msg = "⏰ Aapka free trial Admin ne khatam kar diya hai."
        res = "✅ Trial khatam kar diya gaya."
    else:
        await A(db.ex, "UPDATE trials SET expires_at=?, extended_days=extended_days+? WHERE user_id=?", (iso(new), days, uid))
        msg = f"ℹ️ Aapke free trial me <b>{-days} din kam</b> kiye gaye. Ab {dleft(iso(new))} din baaki."
        res = f"✅ Trial {-days} din kam kiya. Ab {dleft(iso(new))} din baaki."
    await A(audit, admin, "trial_reduce", uid, f"{days}d")
    await notify(uid, msg)
    await after_entitlement_change(uid)
    return res


# =====================================================================
# 6. NOTIFICATIONS (rate-limited, deduplicated, block-aware)
# =====================================================================
APP: Application = None  # type: ignore
_rl_lock = asyncio.Lock()
_last_send = [0.0]


async def throttle():
    async with _rl_lock:
        d = 0.04 - (time.monotonic() - _last_send[0])
        if d > 0:
            await asyncio.sleep(d)
        _last_send[0] = time.monotonic()


async def notify(uid: int, text: str, kind: str | None = None, day: str | None = None, pref: str | None = None,
                 markup=None, silent_dedupe=True) -> bool:
    if kind:
        u = await A(db.one, "SELECT blocked,notify_promo,notify_resource,notify_backup FROM users WHERE id=?", (uid,))
        if u and u["blocked"]:
            return False
        if pref and u and not u[pref]:
            return False
        cur = await A(db.ex, "INSERT OR IGNORE INTO notifications(user_id,kind,day,sent_at) VALUES(?,?,?,?)", (uid, kind, day or "once", iso()))
        if cur.rowcount == 0:
            return False                       # already sent (survives scheduler restarts)
    for attempt in range(2):
        try:
            await throttle()
            await APP.bot.send_message(uid, text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
            return True
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
        except Forbidden:
            await A(db.ex, "UPDATE users SET blocked=1 WHERE id=?", (uid,))
            return False
        except TelegramError as e:
            log.warning("send to %s failed: %s", uid, e)
            if kind:
                await A(db.ex, "DELETE FROM notifications WHERE user_id=? AND kind=? AND day=?", (uid, kind, day or "once"))
            return False
    return False


async def send_doc(uid: int, path: Path, caption: str = "", filename: str | None = None) -> bool:
    for _ in range(2):
        try:
            await throttle()
            with open(path, "rb") as f:
                await APP.bot.send_document(uid, f, filename=filename or path.name, caption=caption[:1000], parse_mode=ParseMode.HTML)
            return True
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)
        except Forbidden:
            await A(db.ex, "UPDATE users SET blocked=1 WHERE id=?", (uid,))
            return False
        except TelegramError as e:
            log.warning("send_doc to %s failed: %s", uid, e)
            return False
    return False


async def send_file_any_size(uid: int, path: Path, caption: str) -> bool:
    if path.stat().st_size <= TG_SEND_LIMIT:
        return await send_doc(uid, path, caption)
    # Too large for Telegram: split in parts (user joins with: cat name.zip.part* > name.zip)
    ok, i = True, 0
    tmpd = Path(tempfile.mkdtemp(prefix="tgh_split_"))
    try:
        with open(path, "rb") as f:
            while chunk := f.read(45 * 1024 * 1024):
                i += 1
                part = tmpd / f"{path.name}.part{i:03d}"
                part.write_bytes(chunk)
                ok &= await send_doc(uid, part, caption + f"\n<i>Part {i} — join: Linux/Termux <code>cat {path.name}.part* &gt; {path.name}</code> · Windows <code>copy /b {path.name}.part001+{path.name}.part002 {path.name}</code></i>")
                part.unlink()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
    return ok


def admin_ids_with(perm: str) -> list[int]:
    ids = {OWNER_ID} if OWNER_ID else set()
    for r in db.all("SELECT a.user_id,a.role,r.permissions FROM admins a JOIN admin_roles r ON r.role=a.role"):
        if r["role"] == "owner" or perm in json.loads(r["permissions"]):
            ids.add(r["user_id"])
    return list(ids)


async def notify_admins(text: str, perm: str = "view_stats", kind: str | None = None, day: str | None = None):
    for aid in await A(admin_ids_with, perm):
        await notify(aid, text, kind=kind, day=day)


async def send_long(uid: int, text: str):
    for i in range(0, len(text), 3800):
        await notify(uid, text[i:i + 3800])





# =====================================================================
# 7. USER BACKUPS  (individual bot / all bots / complete user data)
# =====================================================================
def _sqlite_copy(p: Path, tmpdir: Path, tag) -> Path:
    """Consistent copy of a (possibly live) SQLite file via the online-backup API; non-SQLite -> the file itself."""
    try:
        with open(p, "rb") as fh:
            if not fh.read(16).startswith(b"SQLite format 3"):
                return p
    except OSError:
        return p
    cp = tmpdir / f"{tag}_{secrets.token_hex(4)}.db"
    for uri in (p.as_uri() + "?mode=ro", None):
        s = t = None
        try:
            s = sqlite3.connect(uri, uri=True, timeout=10) if uri else sqlite3.connect(str(p), timeout=10)
            t = sqlite3.connect(str(cp))
            s.backup(t)
            return cp
        except sqlite3.Error:
            continue
        finally:
            for c in (s, t):
                if c:
                    c.close()
    return p


def add_dir_to_zip(zf: zipfile.ZipFile, d: Path, prefix: str, tmpdir: Path, tag) -> tuple[int, int]:
    n = size = 0
    for p in walk_files(d):
        rel = p.relative_to(d).as_posix()
        src = _sqlite_copy(p, tmpdir, tag) if p.suffix.lower() in SQLITE_EXT else p
        try:
            zf.write(src, prefix + rel)
            size += src.stat().st_size
            n += 1
        except FileNotFoundError:                       # file vanished while a running bot was cleaning up
            continue
    return n, size


def user_data_json(uid: int) -> dict:
    def rows(sql, a=()):
        return [dict(r) for r in db.all(sql, a)]
    u = db.one("SELECT id,username,first_name,registered_at,last_seen,banned,ban_reason FROM users WHERE id=?", (uid,))
    t = db.one("SELECT * FROM trials WHERE user_id=?", (uid,))
    return {"exported_at": iso(), "user": dict(u) if u else None, "trial": dict(t) if t else None,
            "subscriptions": rows("SELECT plan,starts_at,expires_at,status FROM subscriptions WHERE user_id=?", (uid,)),
            "bots": [{"id": b["id"], "name": b["name"], "entry": b["entry"], "status": b["status"],
                      "restart_policy": b["restart_policy"], "created_at": b["created_at"], "banned": b["banned"],
                      "env_keys": list(dec_env(b["env_enc"]).keys())}
                     for b in db.all("SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))],
            "backups": rows("SELECT id,kind,created_at,size,status FROM backups WHERE user_id=? ORDER BY id DESC LIMIT 50", (uid,)),
            "activity": rows("SELECT action,detail,ts FROM activity_logs WHERE user_id=? ORDER BY id DESC LIMIT 100", (uid,))}


def build_user_backup(uid: int, kind: str = "auto", bot_ids=None, with_meta: bool = False) -> dict:
    """Blocking. One verified ZIP with the user's bots (+ manifest). Running bots are NOT stopped;
    SQLite files are copied with the online-backup API (consistent)."""
    q, args = "SELECT * FROM hosted_bots WHERE user_id=?", [uid]
    if bot_ids:
        q += " AND id IN (%s)" % ",".join("?" * len(bot_ids))
        args += list(bot_ids)
    bots = db.all(q + " ORDER BY id", tuple(args))
    out_dir = BACKUP_DIR / f"user_{uid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%d_%H%M%S")
    final = out_dir / f"backup_{kind}_{stamp}.zip"
    tmp = final.with_suffix(".zip.part")
    manifest = {"version": 1, "created_at": iso(), "user_id": uid, "kind": kind, "bots": []}
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="tgh_bk_", dir=str(TMP_DIR)))
    ok_n = fail_n = 0
    errors = []
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True, strict_timestamps=False) as zf:
            for b in bots:
                prefix = f"bots/{b['id']}_{safe_name(b['name'])}/"
                meta = {"id": b["id"], "name": b["name"], "entry": b["entry"], "restart_policy": b["restart_policy"],
                        "desired_state": b["desired_state"], "env_keys": list(dec_env(b["env_enc"]).keys()),
                        "created_at": b["created_at"], "prefix": prefix, "files": 0, "bytes": 0, "ok": True}
                try:
                    d = bot_dir(b)
                    if not d.exists():
                        raise FileNotFoundError("bot directory missing")
                    meta["files"], meta["bytes"] = add_dir_to_zip(zf, d, prefix, tmpdir, b["id"])
                    ok_n += 1
                except Exception as e:
                    meta["ok"] = False
                    meta["error"] = str(e)[:200]
                    fail_n += 1
                    errors.append(f"{b['name']}: {e}")
                manifest["bots"].append(meta)
            if with_meta:
                zf.writestr("user_data.json", json.dumps(user_data_json(uid), indent=1, default=str))
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        with zipfile.ZipFile(tmp) as zf:                  # integrity verification
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"corrupt entry {bad}")
            json.loads(zf.read("manifest.json"))
        h = hashlib.sha256()
        with open(tmp, "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
        os.replace(tmp, final)                           # atomic; timestamped => never overwrites a newer backup
        size = final.stat().st_size
        status = "ok" if not fail_n else ("partial" if ok_n else "failed")
        cur = db.ex("INSERT INTO backups(user_id,kind,path,size,sha256,created_at,status,verified,error,bots_ok,bots_failed) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (uid, kind, str(final), size, h.hexdigest(), iso(), status, 1, "; ".join(errors)[:500], ok_n, fail_n))
        if kind in ("auto", "manual"):
            for b, m in zip(bots, manifest["bots"]):
                set_bot(b["id"], backup_status="ok" if m["ok"] else "failed", last_backup_at=iso())
        blog.info("user %s backup %s status=%s size=%s", uid, final.name, status, size)
        return {"id": cur.lastrowid, "path": final, "size": size, "status": status, "ok": ok_n, "failed": fail_n, "errors": errors}
    except Exception as e:
        tmp.unlink(missing_ok=True)
        db.ex("INSERT INTO backups(user_id,kind,created_at,status,error,bots_ok,bots_failed) VALUES(?,?,?,?,?,?,?)",
              (uid, kind, iso(), "failed", str(e)[:500], 0, len(bots)))
        blog.error("user %s backup failed: %s", uid, e)
        return {"id": None, "path": None, "size": 0, "status": "failed", "ok": 0, "failed": len(bots), "errors": [str(e)]}
    finally:
        rmtree_force(tmpdir)





def prune_backups(uid: int, keep: int):
    for kind, k in (("auto", keep), ("manual", keep), ("snapshot", 3), ("send", 3), ("export", 3)):
        rows = db.all("SELECT id,path FROM backups WHERE user_id=? AND kind=? AND path IS NOT NULL ORDER BY created_at DESC", (uid, kind))
        for r in rows[k:]:
            Path(r["path"]).unlink(missing_ok=True)
            db.ex("DELETE FROM backups WHERE id=?", (r["id"],))


async def backup_one_user(uid: int, kind: str = "auto", deliver: bool = True) -> dict:
    res = await A(build_user_backup, uid, kind)
    pl = await A(plan_of, uid)
    await A(prune_backups, uid, (pl or {"limits": DEFAULT_PLANS["free"]})["limits"]["backup_keep"])
    res["delivered"] = False
    if res["path"] and deliver:
        u = await A(db.one, "SELECT notify_backup FROM users WHERE id=?", (uid,))
        if u and u["notify_backup"]:
            cap = (f"💾 <b>Backup complete</b>\n🕒 {short_dt(iso())}\n📦 {human(res['size'])} · bots OK: {res['ok']}"
                   f"{' · failed: ' + str(res['failed']) if res['failed'] else ''}\n🔐 Integrity verified (SHA-256)")
            res["delivered"] = await send_file_any_size(uid, res["path"], cap)
            await A(db.ex, "UPDATE backups SET delivered=? WHERE id=?", (1 if res["delivered"] else 0, res["id"]))
        else:
            res["delivered"] = None
    elif not res["path"]:
        await notify(uid, f"❌ <b>Backup failed</b>: {esc('; '.join(res['errors'])[:300])}", kind="backup_fail", day=iso()[:13], pref="notify_backup")
    return res


async def backup_job(ctx=None, manual_by: int | None = None):
    """Runs every BACKUP_INTERVAL_HOURS: backs up every user, then reports to admins."""
    t0 = now()
    users = await A(db.all, "SELECT DISTINCT u.id,u.username FROM users u JOIN hosted_bots b ON b.user_id=u.id WHERE u.banned=0")
    sem = asyncio.Semaphore(3)
    results = []

    async def one(u):
        async with sem:
            try:
                r = await backup_one_user(u["id"], "manual" if manual_by else "auto")
            except Exception as e:                     # never let one user break the run
                r = {"status": "failed", "ok": 0, "failed": 0, "size": 0, "errors": [str(e)], "delivered": False, "path": None}
                blog.exception("backup user %s", u["id"])
            results.append((u, r))
    await asyncio.gather(*(one(u) for u in users))
    await A(set_setting, "last_user_backup", iso())
    okc = sum(1 for _, r in results if r["status"] == "ok")
    par = sum(1 for _, r in results if r["status"] == "partial")
    fl = sum(1 for _, r in results if r["status"] == "failed")
    bots_ok = sum(r["ok"] for _, r in results)
    bots_fail = sum(r["failed"] for _, r in results)
    tot = sum(r["size"] for _, r in results)
    undelivered = [u for u, r in results if r.get("delivered") is False and r["path"]]
    lines = [f"📦 <b>Backup Report</b> ({'manual' if manual_by else f'every {BACKUP_HOURS:g}h'})",
             f"🕒 {short_dt(iso())} · took {fmt_dur((now() - t0).total_seconds())}",
             f"👥 Users processed: <b>{len(results)}</b>",
             f"✅ Successful: <b>{okc}</b> · ⚠️ Partial: <b>{par}</b> · ❌ Failed: <b>{fl}</b>",
             f"🤖 Bots backed up: <b>{bots_ok}</b> · failed: <b>{bots_fail}</b>",
             f"💽 Total size: <b>{human(tot)}</b> · not delivered to user (blocked/too large/error): <b>{len(undelivered)}</b>"]
    bad = [(u, r) for u, r in results if r["status"] != "ok"]
    if bad:
        lines.append("\n<b>Problems:</b>")
        for u, r in bad[:25]:
            lines.append(f"• <code>{u['id']}</code> @{esc(u['username'] or '-')}: {esc('; '.join(r['errors'])[:160] or r['status'])}")
        if len(bad) > 25:
            lines.append(f"… and {len(bad) - 25} more")
    for aid in await A(admin_ids_with, "reports"):
        await send_long(aid, "\n".join(lines))
    return results


async def platform_backup_job(ctx=None):
    try:
        p = await A(build_platform_backup, "auto")
        await A(set_setting, "last_platform_backup", iso())
        cap = f"🗄 <b>Platform backup</b> (encrypted)\n{short_dt(iso())} · {human(p.stat().st_size)}"
        if OWNER_ID:
            await send_file_any_size(OWNER_ID, p, cap)
    except Exception as e:
        blog.exception("platform backup failed")
        await notify_admins(f"❌ <b>Platform backup failed</b>: {esc(e)}", "backup")


def validate_user_zip(path: Path, uid: int, is_admin=False, max_bytes=2 * 1024 ** 3) -> tuple[bool, str | dict]:
    try:
        with zipfile.ZipFile(path) as zf:
            if zf.testzip():
                return False, "Archive is corrupted."
            names = [i.filename.replace("\\", "/") for i in zf.infolist()]
            for n in names:
                p = PurePosixPath(n)
                if p.is_absolute() or ".." in p.parts:
                    return False, "Archive contains unsafe paths."
            if len(names) > ZIP_MAX_FILES * 4:
                return False, "Too many files."
            if sum(i.file_size for i in zf.infolist()) > max_bytes:
                return False, "Archive too large when extracted."
            if "manifest.json" not in names:
                return False, "Not a hosting backup (manifest.json missing)."
            m = json.loads(zf.read("manifest.json"))
            if m.get("version") != 1 or "bots" not in m:
                return False, "Unsupported backup format."
            if m.get("user_id") != uid and not is_admin:
                return False, "This backup belongs to a different account."
            return True, m
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as e:
        return False, f"Invalid backup: {e}"







def _swap_dir(stage: Path, d: Path) -> None:
    """Atomic-ish replace of a bot folder: old folder is kept until the new one is in place."""
    old = d.with_name(d.name + ".old_" + secrets.token_hex(3))
    had = d.exists()
    if had:
        d.rename(old)
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(d)
    except Exception:
        if had and old.exists():
            old.rename(d)
        raise
    if had:
        rmtree_force(old)


async def restore_user_backup(uid: int, zip_path: Path, restart: bool, manifest: dict) -> list[str]:
    rep = []
    pl = await A(plan_of, uid)
    lim = (pl or {"limits": DEFAULT_PLANS["free"]})["limits"]
    if await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,)):          # safety snapshot FIRST
        snap = await A(build_user_backup, uid, "snapshot")
        rep.append(f"🛟 Safety snapshot: {snap['status']}" + (f" ({snap['path'].name})" if snap["path"] else ""))
        if not snap["path"]:
            rep.append("❌ Aborted: could not create the safety snapshot, nothing was changed.")
            return rep
    to_start = []
    for m in manifest["bots"]:
        if not m.get("ok", True):
            rep.append(f"⏭ {m['name']}: skipped (was not backed up correctly)")
            continue
        stage = None
        try:
            if not re.fullmatch(r"bots/\d+_[^/\\]+/", m.get("prefix", "")):
                raise UnsafeArchive("bad prefix")
            ex = await A(db.one, "SELECT * FROM hosted_bots WHERE user_id=? AND name=?", (uid, m["name"]))
            if not ex and await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,)) >= lim["max_bots"]:
                rep.append(f"❌ {m['name']}: bot limit of your plan reached")
                continue
            STORAGE.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".tgh_rs_", dir=str(STORAGE)))
            with zipfile.ZipFile(zip_path) as zf:
                n = await A(safe_extract, zf, stage, m["prefix"], lim["storage_mb"] * 1024 * 1024 * 2)
            if ex:
                bid = ex["id"]
                await stop_bot(bid)
                d = bot_dir(ex)
                if (d / ".venv").exists():                                  # keep the venv, pip will just re-sync it
                    shutil.move(str(d / ".venv"), str(stage / ".venv"))
            else:
                cur = await A(db.ex, "INSERT INTO hosted_bots(user_id,name,dir,entry,restart_policy,created_at) VALUES(?,?,?,?,?,?)",
                              (uid, m["name"], "", m.get("entry"), m.get("restart_policy", "on-failure"), iso()))
                bid = cur.lastrowid
                await A(set_bot, bid, dir=f"user_{uid}/bot_{bid}")
                d = bot_dir(get_bot(bid))
            await A(_swap_dir, stage, d)
            stage = None
            await A(set_bot, bid, entry=m.get("entry"), restart_policy=m.get("restart_policy", "on-failure"))
            rep.append(f"✅ {m['name']}: {n} files restored" + (" (overwritten existing)" if ex else " (new bot)"))
            if m.get("env_keys"):
                rep.append(f"   ⚠️ Re-add env vars in Bot Settings: {', '.join(m['env_keys'])}")
            ok, tail = await install_deps(get_bot(bid))
            rep.append("   📦 dependencies reinstalled" if ok else f"   ❌ dependency install failed: {tail[-150:]}")
            if ok and restart and m.get("desired_state") == "running":
                to_start.append(bid)
        except Exception as e:
            rlog.exception("restore bot failed")
            rep.append(f"❌ {m['name']}: {e}")
        finally:
            if stage:
                await A(rmtree_force, stage)
    for bid in to_start:
        ok, msg = await start_bot(bid)
        rep.append(f"{'▶️' if ok else '❌'} bot #{bid}: {msg[:120]}")
    alog(uid, "restore", " | ".join(rep)[:400])
    rlog.info("user %s restore: %s", uid, rep)
    return rep


# =====================================================================
# 8. PLATFORM BACKUP (encrypted .tghbak = database + keys) / MASTER BACKUP (everything) / MIGRATION
# =====================================================================
MAGIC = b"TGHB1"


def _backup_pass() -> str:
    if BACKUP_PASSPHRASE:
        return BACKUP_PASSPHRASE
    raise RuntimeError("BACKUP_PASSPHRASE is not set")


def _derive(pw: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=300_000)
    return base64.urlsafe_b64encode(kdf.derive(pw.encode()))


def encrypt_blob(data: bytes) -> bytes:
    salt = os.urandom(16)
    return MAGIC + salt + Fernet(_derive(_backup_pass(), salt)).encrypt(data)


def decrypt_blob(b: bytes) -> bytes:
    if not b.startswith(MAGIC):
        raise ValueError("Not a platform backup file.")
    salt = b[len(MAGIC):len(MAGIC) + 16]
    return Fernet(_derive(_backup_pass(), salt)).decrypt(b[len(MAGIC) + 16:])


def ensure_passphrase() -> bool:
    """Make sure a backup passphrase exists. Returns True if a NEW one was generated (owner must save it)."""
    global BACKUP_PASSPHRASE
    if BACKUP_PASSPHRASE:
        return False
    BACKUP_PASSPHRASE = secrets.token_urlsafe(24)
    os.environ["BACKUP_PASSPHRASE"] = BACKUP_PASSPHRASE
    envf = HERE / ".env"
    try:
        with open(envf, "a", encoding="utf-8") as f:
            f.write(f"\nBACKUP_PASSPHRASE={BACKUP_PASSPHRASE}\n")
        if not IS_WIN:
            os.chmod(envf, 0o600)
    except OSError as e:
        log.error("could not write BACKUP_PASSPHRASE to .env: %s", e)
    return True


def _public_config() -> dict:
    keys = ("DATABASE_PATH", "HOSTING_STORAGE_PATH", "BACKUP_STORAGE_PATH", "LOG_LEVEL", "MAX_UPLOAD_SIZE",
            "DEFAULT_TRIAL_DAYS", "BACKUP_INTERVAL_HOURS", "RESOURCE_WARNING_THRESHOLD", "REPORT_TIMEZONE",
            "REPORT_HOUR", "PROMO_HOUR", "RESOURCE_SAMPLE_SECONDS", "OWNER_ID", "DOWNLOAD_PORT")
    return {k: os.environ[k] for k in keys if k in os.environ}      # NON-secret settings only


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def build_platform_backup(tag: str = "manual") -> Path:
    d = BACKUP_DIR / "platform"
    d.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmpd = Path(tempfile.mkdtemp(prefix="tgh_pb_", dir=str(TMP_DIR)))
    try:
        dbc = tmpd / "hosting.db"
        db.snapshot_to(dbc)
        files = {"hosting.db": dbc.read_bytes(), "config.json": json.dumps(_public_config(), indent=1).encode()}
        kf = DB_PATH.parent / ".secret.key"
        if kf.exists() and not _env("ENCRYPTION_KEY"):
            files["secret.key"] = kf.read_bytes()                  # lets a new server decrypt stored bot env vars
        counts = {t: db.val(f"SELECT COUNT(*) FROM {t}") for t in sorted(REQUIRED_TABLES)}
        manifest = {"version": 1, "app": APP_VERSION, "created_at": iso(), "tag": tag, "counts": counts,
                    "sha256": {k: _sha_bytes(v) for k, v in files.items()}}
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for k, v in files.items():
                zf.writestr(k, v)
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        out = d / f"platform_{tag}_{now().strftime('%Y%m%d_%H%M%S')}.tghbak"
        tmp = out.with_suffix(".part")
        tmp.write_bytes(encrypt_blob(buf.getvalue()))
        os.replace(tmp, out)
        keep = int(get_setting("platform_backup_keep", 14))
        old = sorted(d.glob("platform_*.tghbak"), key=lambda p: p.stat().st_mtime)
        for p in old[:-keep]:
            p.unlink(missing_ok=True)
        blog.info("platform backup %s", out.name)
        return out
    finally:
        rmtree_force(tmpd)


def inspect_platform_backup(path: Path) -> tuple[bool, dict | str, dict]:
    try:
        raw = decrypt_blob(path.read_bytes())
    except InvalidToken:
        return False, "Decryption failed (wrong BACKUP_PASSPHRASE or tampered file).", {}
    except (ValueError, RuntimeError) as e:
        return False, str(e), {}
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        if zf.testzip():
            return False, "Backup archive is corrupted.", {}
        man = json.loads(zf.read("manifest.json"))
        data = {}
        for k, h in man["sha256"].items():
            data[k] = zf.read(k)
            if _sha_bytes(data[k]) != h:
                return False, f"Integrity check failed for {k}.", {}
        if "hosting.db" not in data:
            return False, "Database missing in backup.", {}
        return True, man, data
    except Exception as e:
        return False, f"Invalid backup: {e}", {}


def restore_platform_core(path: Path, pre_backup: bool = True) -> tuple[bool, list[str]]:
    """DATABASE-ONLY restore (+ encryption key). Bot files are not touched (use MASTER backup for those)."""
    ok, man, data = inspect_platform_backup(path)
    if not ok:
        return False, [f"❌ {man}"]
    rep = [f"🔎 Backup created {short_dt(man['created_at'])} · integrity ✅"]
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmpd = Path(tempfile.mkdtemp(prefix="tgh_pr_", dir=str(TMP_DIR)))
    try:
        newdb = tmpd / "new.db"
        newdb.write_bytes(data["hosting.db"])
        c = sqlite3.connect(str(newdb))
        try:
            if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, rep + ["❌ Restored database failed integrity_check."]
            have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
        if not REQUIRED_TABLES <= have:
            return False, rep + [f"❌ Database lacks tables: {sorted(REQUIRED_TABLES - have)}"]
        if pre_backup:
            try:
                rep.append(f"🛟 Pre-restore backup: {build_platform_backup('prerestore').name}")
            except Exception as e:
                return False, rep + [f"❌ Could not create pre-restore backup ({e}); aborted, nothing changed."]
        if "secret.key" in data and not _env("ENCRYPTION_KEY"):
            kf = DB_PATH.parent / ".secret.key"
            if kf.exists():
                shutil.copy2(kf, kf.with_name(".secret.key.bak_" + now().strftime("%Y%m%d%H%M%S")))
            kf.write_bytes(data["secret.key"])
            if not IS_WIN:
                os.chmod(kf, 0o600)
            rep.append("🔑 Encryption key restored (stored bot env vars can be decrypted again).")
        db.swap_with(newdb)
        rep.append("🗄 Database restored (users, admins/roles, subscriptions, trials, bot records, settings, history).")
        for t, n in man["counts"].items():
            rep.append(f"   • {t}: {n}")
        rlog.info("platform restore from %s", path.name)
        return True, rep
    finally:
        rmtree_force(tmpd)


# ---------------- MASTER backup (everything, one ZIP, per-user folders) ----------------
def _master_name(tag: str) -> str:
    return f"MASTER_BACKUP_{datetime.now(TZ):%Y_%m_%d_%H%M%S}" + ("" if tag in ("manual", "auto") else f"_{tag}") + ".zip"


def build_master_backup(tag: str = "manual") -> Path:
    """MASTER_BACKUP_YYYY_MM_DD_HHMMSS.zip
         manifest.json | db/hosting.db | secrets.enc (bot env vars, encrypted with BACKUP_PASSPHRASE) |
         config/config.json | users/<uid>/user.json | users/<uid>/bots/<id>_<name>/...project files...
       Running bots are not stopped; SQLite files are copied consistently. The passphrase is NOT in the zip."""
    d = BACKUP_DIR / "master"
    d.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    tmpd = Path(tempfile.mkdtemp(prefix="tgh_mb_", dir=str(TMP_DIR)))
    out = d / _master_name(tag)
    part = out.with_suffix(".zip.part")
    try:
        dbc = tmpd / "hosting.db"
        db.snapshot_to(dbc)
        secret_map = {}
        c = sqlite3.connect(str(dbc))
        try:
            for r in c.execute("SELECT id, env_enc FROM hosted_bots WHERE env_enc IS NOT NULL"):
                env = dec_env(r[1])
                if env:
                    secret_map[str(r[0])] = env
            c.execute("UPDATE hosted_bots SET env_enc=NULL, status='stopped'")        # desired_state is kept
            for t in ("bot_processes", "resource_usage", "download_links"):
                c.execute(f"DELETE FROM {t}")
            c.commit()
            c.execute("VACUUM")
        finally:
            c.close()
        sec_blob = encrypt_blob(json.dumps(secret_map).encode())
        db_bytes = dbc.read_bytes()
        users = [r["id"] for r in db.all("SELECT id FROM users ORDER BY id")]
        bots = db.all("SELECT * FROM hosted_bots ORDER BY id")
        manifest = {"format": FORMAT_MASTER, "version": 2, "app": APP_VERSION, "created_at": iso(), "tag": tag,
                    "platform": sys.platform, "users": users, "bots": [],
                    "counts": {t: db.val(f"SELECT COUNT(*) FROM {t}") for t in sorted(REQUIRED_TABLES)}}
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True, strict_timestamps=False) as zf:
            zf.writestr("db/hosting.db", db_bytes)
            zf.writestr("secrets.enc", sec_blob)
            zf.writestr("config/config.json", json.dumps(_public_config(), indent=1))
            for uid in users:
                zf.writestr(f"users/{uid}/user.json", json.dumps(user_data_json(uid), indent=1, default=str))
            for b in bots:
                m = {"id": b["id"], "user_id": b["user_id"], "name": b["name"], "dir": b["dir"],
                     "prefix": f"users/{b['user_id']}/bots/{b['id']}_{safe_name(b['name'])}/", "files": 0, "bytes": 0, "ok": True}
                try:
                    bd = bot_dir(b)
                    if not bd.exists():
                        raise FileNotFoundError("bot directory missing")
                    m["files"], m["bytes"] = add_dir_to_zip(zf, bd, m["prefix"], tmpd, b["id"])
                except Exception as e:
                    m["ok"], m["error"] = False, str(e)[:200]
                manifest["bots"].append(m)
            manifest["sha256"] = {"db/hosting.db": _sha_bytes(db_bytes), "secrets.enc": _sha_bytes(sec_blob)}
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        with zipfile.ZipFile(part) as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"corrupt entry {bad}")
        os.replace(part, out)
        h = hashlib.sha256()
        with open(out, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out.with_suffix(".sha256").write_text(f"{h.hexdigest()}  {out.name}\n")
        if tag in ("auto", "manual"):
            keep = int(get_setting("master_keep", 3))
            for p in sorted(d.glob("MASTER_BACKUP_*.zip"), key=lambda x: x.stat().st_mtime)[:-keep]:
                p.unlink(missing_ok=True)
                p.with_suffix(".sha256").unlink(missing_ok=True)
        blog.info("master backup %s (%s)", out.name, human(out.stat().st_size))
        return out
    except Exception:
        part.unlink(missing_ok=True)
        raise
    finally:
        rmtree_force(tmpd)


def validate_master(path: Path) -> tuple[bool, dict | str]:
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                return False, f"Backup is corrupted ({bad})."
            names = [i.filename.replace("\\", "/") for i in zf.infolist()]
            for n in names:
                pp = PurePosixPath(n)
                if pp.is_absolute() or ".." in pp.parts or ":" in n:
                    return False, "Backup contains unsafe paths."
            if "manifest.json" not in names:
                return False, "Not a MASTER backup (manifest.json missing)."
            m = json.loads(zf.read("manifest.json"))
            if m.get("format") != FORMAT_MASTER or m.get("version") not in (1, 2):
                return False, "Not a MASTER backup of this program version."
            for k, h in m["sha256"].items():
                if _sha_bytes(zf.read(k)) != h:
                    return False, f"Integrity check failed for {k}."
            try:
                json.loads(decrypt_blob(zf.read("secrets.enc")))
            except InvalidToken:
                return False, "BACKUP_PASSPHRASE of this server does not match the one used for this backup."
            for b in m["bots"]:
                if not re.fullmatch(r"user_\d+/bot_\d+", b.get("dir", "")) or not b["prefix"].startswith("users/"):
                    return False, "Backup has an invalid bot entry."
            return True, m
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError, ValueError, OSError) as e:
        return False, f"Invalid backup: {e}"


def stop_all_bots_sync() -> int:
    n = 0
    for r in db.all("SELECT bot_id,pid,ctime FROM bot_processes WHERE pid IS NOT NULL"):
        if proc_alive(r["pid"], r["ctime"]):
            kill_tree(r["pid"], r["ctime"], 5.0)
            n += 1
    for bid in list(POPENS):
        reap(bid)
    db.ex("UPDATE bot_processes SET pid=NULL")
    return n


def restore_master(path: Path) -> tuple[bool, list[str]]:
    """Full migration restore (blocking). Safety backup first, staging area, automatic rollback on any failure."""
    ok, m = validate_master(path)
    if not ok:
        return False, [f"❌ {m}"]
    rep = [f"🔎 Backup valid ✅ (created {short_dt(m['created_at'])}, {len(m['users'])} users, {len(m['bots'])} bots)"]
    try:
        safety = build_master_backup("prerestore")
        rep.append(f"🛟 Safety backup of the CURRENT system saved: {safety}")
    except Exception as e:
        return False, rep + [f"❌ Could not create the safety backup ({e}). Nothing was changed."]
    STORAGE.parent.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=".tgh_restore_", dir=str(STORAGE.parent)))
    stamp = now().strftime("%Y%m%d_%H%M%S")
    old_storage = None
    db_before = TMP_DIR / f"db_before_{stamp}.db"
    swapped = False
    try:
        with zipfile.ZipFile(path) as zf:
            secret_map = json.loads(decrypt_blob(zf.read("secrets.enc")))
            newdb = stage_root / "hosting.db"
            newdb.write_bytes(zf.read("db/hosting.db"))
            c = sqlite3.connect(str(newdb))
            try:
                if c.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return False, rep + ["❌ Database in the backup failed integrity_check."]
                have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES <= have:
                    return False, rep + [f"❌ Database lacks tables: {sorted(REQUIRED_TABLES - have)}"]
                for bid, env in secret_map.items():                      # re-encrypt secrets with THIS server's key
                    c.execute("UPDATE hosted_bots SET env_enc=? WHERE id=?", (enc_env(env), int(bid)))
                c.commit()
            finally:
                c.close()
            rep.append("🗄 Database validated, secrets re-encrypted for this server.")
            st = stage_root / "storage"
            st.mkdir()
            restored, failed = 0, []
            for bm in m["bots"]:
                if not bm.get("ok", True):
                    failed.append(f"#{bm['id']} {bm['name']} (was not backed up correctly)")
                    continue
                n = safe_extract(zf, st / bm["dir"], bm["prefix"], 200 * 1024 ** 3, 1_000_000, check_ratio=False)
                restored += 1
            rep.append(f"📁 Project files staged: {restored} bots")
        stop_all_bots_sync()
        db.snapshot_to(db_before)
        if STORAGE.exists():
            old_storage = STORAGE.with_name(STORAGE.name + f".old_{stamp}")
            STORAGE.rename(old_storage)
        (stage_root / "storage").rename(STORAGE)
        swapped = True
        db.swap_with(newdb)
        if OWNER_ID:
            db.ex("INSERT OR IGNORE INTO admins(user_id,role,added_by,added_at) VALUES(?,?,?,?)", (OWNER_ID, "owner", OWNER_ID, iso()))
        db.ex("UPDATE hosted_bots SET status='stopped'")
        for b in db.all("SELECT id,dir FROM hosted_bots"):
            try:
                if not bot_dir(b).exists():
                    set_bot(b["id"], status="error", desired_state="stopped", last_error="Files missing in backup - restore a user backup.")
                    failed.append(f"#{b['id']} (files missing)")
            except UnsafeArchive:
                set_bot(b["id"], status="error", desired_state="stopped", last_error="invalid directory")
        counts = {t: db.val(f"SELECT COUNT(*) FROM {t}") for t in ("users", "admins", "hosted_bots", "subscriptions", "trials")}
        rep.append("✅ RESTORED: " + " · ".join(f"{k}: {v}" for k, v in counts.items()))
        if failed:
            rep.append("⚠️ Problems: " + "; ".join(failed[:10]))
        if old_storage:
            rep.append(f"🗂 Previous bot files kept at: {old_storage} (delete it manually when you are sure)")
        rep.append("📦 Virtual environments are rebuilt automatically when each bot starts (needs internet for pip).")
        rlog.info("master restore from %s: %s", path.name, rep)
        return True, rep
    except Exception as e:
        rlog.exception("master restore failed - rolling back")
        try:
            if swapped and old_storage and old_storage.exists():
                rmtree_force(STORAGE)
                old_storage.rename(STORAGE)
            elif swapped and STORAGE.exists() and not old_storage:
                rmtree_force(STORAGE)
            if swapped and db_before.exists():
                db.swap_with(db_before)
            rep.append(f"❌ Restore FAILED and was rolled back automatically: {e}")
        except Exception as e2:
            rep.append(f"❌ Restore failed ({e}) AND rollback had a problem ({e2}). Use the safety backup above.")
        return False, rep
    finally:
        rmtree_force(stage_root)
        db_before.unlink(missing_ok=True)


# ---------------- download links (tiny built-in HTTP server, standard library only) ----------------
class LinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        m = re.fullmatch(r"/d/([A-Za-z0-9_\-]{20,80})", self.path.split("?")[0])
        row = db.one("SELECT * FROM download_links WHERE token=?", (m[1],)) if m else None
        try:
            ok = bool(row and row["expires_at"] > iso() and row["downloads"] < row["max_downloads"])
            p = Path(row["path"]) if ok else None
            if not ok or not p.is_file() or LINK_DIR not in p.resolve().parents:
                self.send_error(404, "Link expired or not found")
                return
            db.ex("UPDATE download_links SET downloads=downloads+1 WHERE token=?", (row["token"],))
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(p.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{safe_name(row["name"], "backup.zip")}"')
            self.end_headers()
            with open(p, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
        except (BrokenPipeError, ConnectionError):
            pass
        except Exception:
            log.exception("download link error")


LINK_DIR = (BACKUP_DIR / "links").resolve()
LINK_SERVER = {"srv": None}


def start_link_server() -> str:
    if not DOWNLOAD_PORT:
        return "download links disabled (DOWNLOAD_PORT=0)"
    try:
        LINK_DIR.mkdir(parents=True, exist_ok=True)
        srv = ThreadingHTTPServer((DOWNLOAD_HOST, DOWNLOAD_PORT), LinkHandler)
        threading.Thread(target=srv.serve_forever, daemon=True, name="links").start()
        LINK_SERVER["srv"] = srv
        return f"download links on {DOWNLOAD_HOST}:{DOWNLOAD_PORT}"
    except OSError as e:
        return f"download link server could not start: {e}"


def make_download_link(src: Path, name: str, admin_id: int, for_user: int | None, hours: int = 24, max_dl: int = 3):
    """Copies the file into the links folder and returns (url, token) or (None, reason)."""
    if not LINK_SERVER["srv"]:
        return None, "Link server is off. Set DOWNLOAD_PORT (and PUBLIC_BASE_URL if you use a tunnel) in .env and restart."
    LINK_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    dst = LINK_DIR / f"{token[:12]}_{safe_name(name, 'backup.zip')}"
    shutil.copy2(src, dst)
    db.ex("INSERT INTO download_links(token,path,name,expires_at,admin_id,for_user,created_at,max_downloads) VALUES(?,?,?,?,?,?,?,?)",
          (token, str(dst), name, iso(now() + timedelta(hours=hours)), admin_id, for_user, iso(), max_dl))
    base = PUBLIC_BASE_URL or f"http://{DOWNLOAD_HOST}:{DOWNLOAD_PORT}"
    return f"{base}/d/{token}", token


def prune_links():
    for r in db.all("SELECT token,path FROM download_links WHERE expires_at<? OR downloads>=max_downloads", (iso(),)):
        Path(r["path"]).unlink(missing_ok=True)
        db.ex("DELETE FROM download_links WHERE token=?", (r["token"],))


async def reconcile_processes() -> list[str]:
    """After startup / restore: adopt bots that are still alive, start the ones that should run."""
    rep = []
    if not ENGINE["ok"]:
        return ["⚠️ Environment unsupported - bots not started."]
    for b in await A(db.all, "SELECT * FROM hosted_bots"):
        bid = b["id"]
        try:
            d = bot_dir(b)
        except UnsafeArchive:
            continue
        if not d.exists():
            await A(set_bot, bid, status="error", desired_state="stopped", last_error="Files missing - restore a user backup.")
            rep.append(f"⚠️ bot #{bid} {b['name']}: files missing")
            continue
        alive = await A(bot_is_running, bid)
        if b["desired_state"] == "running":
            if alive:
                await A(set_bot, bid, status="running")
                rep.append(f"✔️ bot #{bid} adopted (still running)")
            else:
                ok, msg = await start_bot(bid)
                rep.append(f"{'▶️' if ok else '❌'} bot #{bid} {b['name']}: {msg[:100]}")
                await asyncio.sleep(0.5)
        elif alive:
            await stop_bot(bid)
            rep.append(f"⏹ bot #{bid} was running but should be stopped - stopped")
        elif b["status"] in ("running", "installing"):
            await A(set_bot, bid, status="stopped")
    return rep


def shutdown_bots() -> int:
    """Graceful manager shutdown: stop all hosted bots but KEEP desired_state, so they resume on next start."""
    n = 0
    for r in db.all("SELECT bot_id,pid,ctime FROM bot_processes WHERE pid IS NOT NULL"):
        if proc_alive(r["pid"], r["ctime"]):
            kill_tree(r["pid"], r["ctime"], 5.0)
            n += 1
        db.ex("UPDATE bot_processes SET pid=NULL WHERE bot_id=?", (r["bot_id"],))
        db.ex("UPDATE hosted_bots SET status='stopped' WHERE id=? AND status IN ('running','installing')", (r["bot_id"],))
    for bid in list(POPENS):
        reap(bid)
    return n


def startup_recovery():
    """Open DB; if corrupt, auto-recover from the newest valid platform backup."""
    global db
    try:
        db = DB(DB_PATH)
        if not db.integrity_ok():
            raise sqlite3.DatabaseError("integrity_check failed")
        db.init_schema()
        return
    except sqlite3.DatabaseError as e:
        log.error("Database problem: %s - attempting automatic recovery", e)
    try:
        db.close()
    except Exception:
        pass
    if DB_PATH.exists():
        DB_PATH.rename(DB_PATH.with_suffix(f".corrupt-{int(time.time())}"))
    db = DB(DB_PATH)
    db.init_schema()
    for p in sorted((BACKUP_DIR / "platform").glob("platform_*.tghbak"), key=lambda x: x.stat().st_mtime, reverse=True):
        ok, rep = restore_platform_core(p, pre_backup=False)
        log.warning("auto-recovery from %s: %s", p.name, ok)
        if ok:
            return
    log.error("No valid platform backup found; started with an empty database.")



# =====================================================================
# 9. MONITOR: CPU/RAM sampling, limit enforcement, crash handling (no Docker - psutil process trees)
# =====================================================================
VENV_SZ: dict[int, tuple[float, int]] = {}


async def monitor_cycle(ctx=None):
    if STATE["restoring"] or not ENGINE["ok"]:
        return
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE desired_state='running' OR status='running'")
    if not bots:
        return
    pids = {r["bot_id"]: (r["pid"], r["ctime"]) for r in await A(db.all, "SELECT bot_id,pid,ctime FROM bot_processes WHERE pid IS NOT NULL")}
    entries = [(b["id"], *pids[b["id"]]) for b in bots if b["id"] in pids]
    live = await A(sample_all, entries) if entries else {}
    rows = []
    for b in bots:
        bid = b["id"]
        s = live.get(bid)
        if s:
            STATS[bid] = s
            CPU_HIST[bid].append(s["cpu"])
            if b["status"] != "running":
                await A(set_bot, bid, status="running")
            lim = (await A(bot_plan, b))["limits"]
            if bid not in DISK or time.time() - DISK[bid][0] > 120:
                try:
                    DISK[bid] = (time.time(), await A(project_size, bot_dir(b)))
                except UnsafeArchive:
                    DISK[bid] = (time.time(), 0)
            disk = DISK[bid][1]
            rows.append((bid, iso(), s["cpu"], s["mem"], disk, 0, 0))
            await A(set_bot, bid, last_activity=iso())
            await check_thresholds(b, s, lim, disk)
        elif b["desired_state"] == "running" and bid not in CRASHING and not LOCKS[bid].locked() and b["status"] != "installing":
            CRASHING.add(bid)
            code = await A(reap, bid)
            STATS.pop(bid, None)
            spawn(handle_crash(b, -1 if code is None else code))
    if rows:
        await A(lambda: db.tx(lambda c: c.executemany(
            "INSERT INTO resource_usage(bot_id,ts,cpu,mem_bytes,disk_bytes,net_rx,net_tx) VALUES(?,?,?,?,?,?,?)", rows)))


async def _limit_stop(b, reason: str, user_msg: str):
    await stop_bot(b["id"], auto=True, reason=reason)
    await A(set_bot, b["id"], status="crashed")
    await A(record_crash, b, None, reason, user_msg, "")
    await notify(b["user_id"], f"🛑 <b>{esc(b['name'])}</b> was stopped: {user_msg}", markup=Kb([[Btn("🩺 Poori jaanch + upay", callback_data=f"b:diag:{b['id']}")]]))
    await notify_admins(f"🛑 Bot #{b['id']} (user <code>{b['user_id']}</code>) auto-stopped: {esc(reason)}", "bots_view",
                        kind=f"adm_lim_{b['id']}", day=f"{now():%Y%m%d%H}")


async def check_thresholds(b, s, lim, disk):
    bid = b["id"]
    cpu_pct = s["cpu"] / max(lim["cpu"], 1) * 100                  # lim["cpu"] is % of one core
    mem_pct = s["mem"] / max(lim["ram_mb"] * 1024 * 1024, 1) * 100
    bucket = f"{now():%Y%m%d}-{now().hour // 3}"                   # warning cooldown: max one per 3 hours per bot
    if disk > lim["storage_mb"] * 1024 * 1024:
        return await _limit_stop(b, "Storage limit exceeded", f"storage limit ({lim['storage_mb']} MB) exceeded. Free space and start it again.")
    if s["mem"] > lim["ram_mb"] * 1024 * 1024 * 1.05:
        return await _limit_stop(b, "RAM limit exceeded", f"it used {human(s['mem'])} RAM (limit {lim['ram_mb']} MB).")
    if s["procs"] > lim["pids"]:
        pid, ct = await A(bot_pid, bid)
        await A(kill_tree, pid, ct, 1.0)
        return await _limit_stop(b, "Process limit exceeded", f"too many processes ({s['procs']} > {lim['pids']}).")
    h = CPU_HIST[bid]
    if len(h) == h.maxlen and sum(h) / len(h) > lim["cpu"] * 1.5:
        h.clear()
        return await _limit_stop(b, "Sustained CPU limit exceeded", f"CPU stayed above its limit for 10 minutes (limit {lim['cpu']}% of one core).")
    v = VENV_SZ.get(bid)
    if not v or time.time() - v[0] > 600:
        try:
            v = VENV_SZ[bid] = (time.time(), await A(dir_size, bot_dir(b) / ".venv"))
        except Exception:
            v = (time.time(), 0)
    if v[1] > lim["venv_mb"] * 1024 * 1024:
        return await _limit_stop(b, "Virtual environment too large", f"its libraries take {human(v[1])} (limit {lim['venv_mb']} MB).")
    lp = LOG_DIR / "bots" / f"bot_{bid}.log"
    try:
        if lp.exists() and lp.stat().st_size > 50_000_000:         # runaway log: keep the tail only
            keep = lp.read_bytes()[-1_000_000:]
            with open(lp, "r+b") as f:
                f.seek(0)
                f.write(keep)
                f.truncate()
    except OSError:
        pass
    sto_pct = disk / max(lim["storage_mb"] * 1024 * 1024, 1) * 100
    if sto_pct >= WARN_PCT:                               # DAILY storage warning (one message per bot per day)
        bpn = (await A(bot_plan, b))["name"]
        hint = "Storage badhwane ke liye Admin se contact karein." if bpn == "premium" else "Zyada storage ke liye Premium plan lein."
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        await notify(b["user_id"], f"⚠️ <b>Storage warning</b>\n\nAapka bot <b>{esc(b['name'])}</b> (ID <code>{bid}</code>) zyada storage le raha hai.\n"
                                   f"💽 {human(disk)} / {lim['storage_mb']} MB ({sto_pct:.0f}%)\n\nStorage full hote hi bot band ho jayega. {hint}\n"
                                   "<i>(Ye message roz ek baar aata hai jab tak storage kam nahi hota.)</i>",
                     kind=f"stor_warn_{bid}", day=today, pref="notify_resource",
                     markup=Kb([[Btn("📞 Contact Admin", callback_data="menu:contact"), Btn("🩺 Jaanch", callback_data=f"b:diag:{bid}")]]))
        if sto_pct >= 95:
            await notify_admins(f"⚠️ Storage {sto_pct:.0f}%: bot #{bid} (user <code>{b['user_id']}</code>) {human(disk)}", "bots_view", kind=f"adm_stor_{bid}", day=today)
    if cpu_pct >= WARN_PCT or mem_pct >= WARN_PCT:
        await notify(b["user_id"],
                     f"⚠️ <b>RESOURCE WARNING</b>\n\nYour bot <b>{esc(b['name'])}</b> is consuming unusually high system resources.\n\n"
                     f"CPU Usage: {s['cpu']:.1f}% (limit {lim['cpu']}%)\nRAM Usage: {human(s['mem'])} (limit {lim['ram_mb']} MB)\n\n"
                     "Your current hosting plan has limited resources.\nConsider upgrading to Premium Hosting.",
                     kind=f"res_warn_{bid}", day=bucket, pref="notify_resource")
        if cpu_pct >= 95 or mem_pct >= 95:
            await notify_admins(f"⚠️ High usage · bot #{bid} (user <code>{b['user_id']}</code>): CPU {s['cpu']:.0f}% RAM {human(s['mem'])}",
                                "bots_view", kind=f"adm_res_{bid}", day=bucket)


async def handle_crash(b, exit_code: int):
    bid = b["id"]
    try:
        async with LOCKS[bid]:
            txt = await A(tail_file, LOG_DIR / "bots" / f"bot_{bid}.log", 200)
            _close_job(bid)
            await A(db.ex, "UPDATE bot_processes SET pid=NULL, last_exit_code=? WHERE bot_id=?", (exit_code, bid))
        STATS.pop(bid, None)
        txt = redact(txt, list(dec_env(b["env_enc"]).values()))
        tail = "\n".join(txt.strip().splitlines()[-12:])[-500:]
        reason = f"Exited with code {exit_code}" if exit_code >= 0 else "Process ended unexpectedly"
        cause, fix = diagnose(txt, exit_code)
        dtxt = f"\n🔍 <b>Wajah:</b> {esc(cause)}\n🛠 <b>Upay:</b> {esc(fix)}"
        _u = await A(record_crash, b, exit_code, cause, fix, tail)
        if _u["sto"] >= 85:
            dtxt += f"\n💽 <b>Storage {_u['sto']:.0f}% bhara hai</b> ({human(_u['disk'])} / {_u['lim']['storage_mb']} MB) — ye crash ki wajah ho sakti hai. Files/logs hatayein ya Premium (zyada storage) lein."
        if _u["ram"] >= 85:
            dtxt += f"\n🧠 <b>RAM {_u['ram']:.0f}%</b> tak gayi thi — memory kam padi ho sakti hai."
        dkb = Kb([[Btn("🩺 Poori jaanch + upay", callback_data=f"b:diag:{bid}")]])
        await A(set_bot, bid, status="crashed" if exit_code != 0 else "stopped", last_error=f"{reason}\n{tail}"[:700])
        pol = b["restart_policy"]
        m = re.search(r"No module named '([\w\.\-]+)'", txt)           # automatic missing-dependency repair
        if m:
            mod = m[1].split(".")[0]
            pkg = IMPORT_MAP.get(mod, mod)
            if pkg not in AUTOFIX_TRIED[bid] and PKG_RE.match(pkg):
                AUTOFIX_TRIED[bid].add(pkg)
                ok, _ = await install_deps(get_bot(bid), (pkg,))
                await notify(b["user_id"], f"🔧 Missing module <code>{esc(mod)}</code> detected in <b>{esc(b['name'])}</b>; "
                                           f"{'installed ' + esc(pkg) + ' and restarting' if ok else 'automatic install failed'}.")
                if ok:
                    await start_bot(bid, restart=True)
                    return
        should = pol == "always" or (pol == "on-failure" and exit_code != 0)
        if not should:
            await A(set_bot, bid, desired_state="stopped")
            await notify(b["user_id"], f"ℹ️ <b>{esc(b['name'])}</b> stopped ({esc(reason)}).{dtxt}", kind=f"crash_{bid}", day=iso()[:15], markup=dkb)
            return
        bp = await A(db.one, "SELECT * FROM bot_processes WHERE bot_id=?", (bid,))
        ws, n = (bp["window_start"], bp["restarts_in_window"]) if bp else (None, 0)
        if not ws or (now() - pdt(ws)).total_seconds() > 600:
            ws, n = iso(), 0
        if n >= 5:
            await A(set_bot, bid, desired_state="stopped", status="crashed")
            await notify(b["user_id"], f"💥 <b>{esc(b['name'])}</b> baar-baar crash ho raha hai ({esc(reason)}). Auto-restart band kar diya.{dtxt}\n<pre>{esc(tail)}</pre>\nTheek karne ke baad ▶️ Start dabayein (auto-restart dobara chalu ho jayega).", markup=dkb)
            await notify_admins(f"💥 Crash loop: bot #{bid} (@{esc(b['tg_username'] or '?')}) user <code>{b['user_id']}</code>{dtxt}", "bots_view", kind=f"adm_loop_{bid}", day=iso()[:13])
            return
        await A(db.ex, "UPDATE bot_processes SET window_start=?, restarts_in_window=? WHERE bot_id=?", (ws, n + 1, bid))
        await notify(b["user_id"], f"💥 <b>{esc(b['name'])}</b> crash hua ({esc(reason)}). Apne aap dobara chalu ho raha hai…{dtxt}\n<pre>{esc(tail)}</pre>",
                     kind=f"crash_{bid}", day=iso()[:15], markup=dkb)
        await asyncio.sleep(min(5 * (n + 1), 30))
        cur = await A(get_bot, bid)
        if not cur or cur["desired_state"] != "running" or cur["banned"]:           # stopped/banned meanwhile
            return
        ok, msg = await start_bot(bid, restart=True)
        if ok:
            await notify(b["user_id"], f"🔄 <b>{esc(b['name'])}</b> restarted.", kind=f"restart_{bid}", day=iso()[:15])
    except Exception:
        log.exception("handle_crash %s", bid)
    finally:
        CRASHING.discard(bid)



# =====================================================================
# 10. SUBSCRIPTIONS / TRIALS / NOTIFICATION JOBS
# =====================================================================
def grant_premium(uid: int, days: int, admin_id: int | None) -> str:
    n = now()
    cur = db.one("SELECT * FROM subscriptions WHERE user_id=? AND status='active' AND expires_at>? ORDER BY expires_at DESC LIMIT 1", (uid, iso(n)))
    if cur:
        exp = pdt(cur["expires_at"]) + timedelta(days=days)
        db.ex("UPDATE subscriptions SET expires_at=? WHERE id=?", (iso(exp), cur["id"]))
    else:
        exp = n + timedelta(days=days)
        db.ex("INSERT INTO subscriptions(user_id,plan,starts_at,expires_at,created_by) VALUES(?,?,?,?,?)", (uid, "premium", iso(n), iso(exp), admin_id))
    return iso(exp)


async def expiry_job(ctx=None):
    n = iso()
    for r in await A(db.all, "SELECT user_id FROM trials WHERE status='active' AND expires_at<=?", (n,)):
        await A(db.ex, "UPDATE trials SET status='expired' WHERE user_id=?", (r["user_id"],))
        await notify(r["user_id"], "⏰ <b>Your 14-day free trial has ended.</b>\nYour bots were stopped, but all your files and backups are safe. "
                                    "Contact the Admin for Premium Hosting to continue.", kind="trial_expired", day="once")
        if await A(get_setting, "expiry_policy", "stop") == "stop":
            await after_entitlement_change(r["user_id"])
    for r in await A(db.all, "SELECT id,user_id FROM subscriptions WHERE status='active' AND expires_at<=?", (n,)):
        await A(db.ex, "UPDATE subscriptions SET status='expired' WHERE id=?", (r["id"],))
        await notify(r["user_id"], "⏰ <b>Your Premium subscription has expired.</b>", kind=f"prem_expired_{r['id']}", day="once")
        await after_entitlement_change(r["user_id"])
    soon = iso(now() + timedelta(days=3))
    for r in await A(db.all, "SELECT user_id,expires_at FROM trials WHERE status='active' AND expires_at<=?", (soon,)):
        left = (pdt(r["expires_at"]) - now()).total_seconds() / 86400
        k = "trial_1d" if left <= 1 else "trial_3d"
        await notify(r["user_id"], f"⏳ Your free trial ends in <b>{max(1, int(left + 0.99))} day(s)</b> ({short_dt(r['expires_at'])}). Contact Admin for Premium.", kind=k, day="once")
    for r in await A(db.all, "SELECT id,user_id,expires_at FROM subscriptions WHERE status='active' AND expires_at<=?", (soon,)):
        await notify(r["user_id"], f"⏳ Your Premium ends on <b>{short_dt(r['expires_at'])}</b>.", kind=f"prem_soon_{r['id']}", day="once")
    for r in await A(db.all, "SELECT id,user_id,name FROM hosted_bots WHERE premium_until IS NOT NULL AND premium_until<=?", (n,)):
        await A(set_bot, r["id"], premium_until=None)
        await notify(r["user_id"], f"⏰ Bot <b>#{r['id']} {esc(r['name'])}</b> ka <b>Premium khatam</b> ho gaya.", kind=f"bprem_exp_{r['id']}_{n[:10]}", day="once")
        await after_entitlement_change(r["user_id"])
    for r in await A(db.all, "SELECT id,user_id,name,premium_until FROM hosted_bots WHERE premium_until IS NOT NULL AND premium_until>? AND premium_until<=?", (n, soon)):
        await notify(r["user_id"], f"⏳ Bot <b>#{r['id']} {esc(r['name'])}</b> ka Premium <b>{short_dt(r['premium_until'])}</b> ko khatam hoga. Badhane ke liye Contact Admin.",
                     kind=f"bprem_soon_{r['id']}", day=r["premium_until"][:10])


PROMO = ("🚀 <b>Upgrade to Premium Hosting!</b>\n\nYour bot deserves faster and more reliable hosting.\n\n<b>Premium benefits:</b>\n"
         "• Higher CPU limits\n• Higher RAM limits\n• More hosted bots\n• Increased storage\n• Better resource availability\n• Priority support\n\n"
         "For Premium Hosting, contact our Admin.")


async def promo_catchup(ctx=None):
    h = datetime.now(TZ).hour
    if PROMO_HOUR <= h < PROMO_HOUR + 6:
        await promo_job()


async def collect_stats() -> dict:
    n = iso()
    midnight = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)

    def q():
        v = db.val
        total = v("SELECT COUNT(*) FROM users")
        PU = "(SELECT user_id FROM subscriptions WHERE status='active' AND expires_at>? UNION SELECT user_id FROM hosted_bots WHERE premium_until>?)"
        prem = v(f"SELECT COUNT(*) FROM {PU}", (n, n))
        trial = v("SELECT COUNT(*) FROM trials t WHERE t.status='active' AND t.expires_at>? AND t.user_id NOT IN "
                  "(SELECT user_id FROM subscriptions WHERE status='active' AND expires_at>? UNION SELECT user_id FROM hosted_bots WHERE premium_until>?)", (n, n, n))
        return dict(
            total=total, premium=prem, trial=trial, expired=max(total - prem - trial, 0),
            banned=v("SELECT COUNT(*) FROM users WHERE banned=1"),
            new_today=v("SELECT COUNT(*) FROM users WHERE registered_at>=?", (iso(midnight),)),
            bots=v("SELECT COUNT(*) FROM hosted_bots"), running=v("SELECT COUNT(*) FROM hosted_bots WHERE status='running'"),
            crashed=v("SELECT COUNT(*) FROM hosted_bots WHERE status IN ('crashed','error')"),
            stopped=v("SELECT COUNT(*) FROM hosted_bots WHERE status='stopped'"),
            storage=dir_size(STORAGE) if STORAGE.exists() else 0, backup_storage=dir_size(BACKUP_DIR) if BACKUP_DIR.exists() else 0)
    s = await A(q)
    s["active"] = s["premium"] + s["trial"]
    s["cpu"] = sum(x["cpu"] for x in STATS.values())
    s["ram"] = sum(x["mem"] for x in STATS.values())
    s["host_cpu"] = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    s["host_ram"] = (vm.used, vm.total)
    du = shutil.disk_usage(STORAGE if STORAGE.exists() else BASE)
    s["host_disk"] = (du.used, du.total)
    return s


def dash_text(s: dict, title="📊 <b>Hosting Dashboard</b>") -> str:
    return (f"{title}\n\n👥 <b>Users:</b> {s['total']} total · {s['active']} active · 🆕 {s['new_today']} today\n"
            f"🎁 Trial: <b>{s['trial']}</b> · 💎 Premium: <b>{s['premium']}</b> · ⌛ Expired: {s['expired']} · 🚫 Banned: {s['banned']}\n\n"
            f"🤖 <b>Bots:</b> {s['bots']} hosted · 🟢 {s['running']} running · 🔴 {s['stopped']} stopped · 💥 {s['crashed']} crashed/error\n\n"
            f"⚙️ CPU (all bots): <b>{s['cpu']:.1f}%</b> (1 core = 100%)\n🧠 RAM (all bots): <b>{human(s['ram'])}</b>\n"
            f"💽 Storage (files+venvs): <b>{human(s['storage'])}</b> · backups: {human(s['backup_storage'])}"
            "\n"
            f"🖥 <b>Server:</b> CPU {s['host_cpu']:.0f}% · RAM {human(s['host_ram'][0])}/{human(s['host_ram'][1])} · Disk {human(s['host_disk'][0])}/{human(s['host_disk'][1])}")


async def daily_report_job(ctx=None, force: bool = False):
    today = datetime.now(TZ).strftime("%Y-%m-%d")
    if not force and await A(get_setting, "last_daily_report") == today:
        return
    await A(set_setting, "last_daily_report", today)
    s = await collect_stats()
    lb = await A(get_setting, "last_user_backup")
    txt = dash_text(s, f"🗓 <b>Daily Admin Report</b> — {datetime.now(TZ):%d %b %Y, %H:%M}")
    if lb:
        txt += f"\n\n💾 Last user backup: {short_dt(lb)}"
    for aid in await A(admin_ids_with, "reports"):
        await notify(aid, txt)


async def daily_report_catchup(ctx=None):
    if datetime.now(TZ).hour >= REPORT_HOUR:
        await daily_report_job()


async def prune_job(ctx=None):
    await A(db.ex, "DELETE FROM resource_usage WHERE ts<?", (iso(now() - timedelta(days=3)),))
    await A(db.ex, "DELETE FROM notifications WHERE sent_at<?", (iso(now() - timedelta(days=60)),))
    await A(db.ex, "DELETE FROM activity_logs WHERE ts<?", (iso(now() - timedelta(days=90)),))




async def after_entitlement_change(uid: int, restart: bool = False):
    """Bot-wise: a bot without active trial/premium stops; auto-stopped bots with a valid plan start again."""
    for b in await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=?", (uid,)):
        if b["banned"]:
            continue
        bp = await A(bot_plan, b)
        if not bp["active"]:
            if b["status"] in ("running", "installing") or b["desired_state"] == "running":
                await stop_bot(b["id"], auto=True, reason=f"Hosting {bp['name']}")
        elif b["auto_stopped"]:
            await start_bot(b["id"])
        elif restart and b["status"] == "running":
            await restart_bot(b["id"])




# =====================================================================
# 11. TELEGRAM HELPERS / PERMISSIONS
# =====================================================================
MENU = [["🚀 Host New Bot", "🤖 My Hosted Bots"], ["📊 Hosting Statistics", "💾 My Backups"],
        ["🔄 Restore Backup", "👤 My Account"], ["💎 Premium Plans", "📞 Contact Admin"], ["📚 Help & Support", "🤝 Refer & Earn"]]
MENU_KB = ReplyKeyboardMarkup(MENU, resize_keyboard=True)
MENU_KB_ADMIN = ReplyKeyboardMarkup(MENU + [["🛡 Admin Panel"]], resize_keyboard=True)
STATUS_ICON = {"running": "🟢 Running", "stopped": "🔴 Stopped", "crashed": "💥 Crashed", "installing": "⏳ Installing", "error": "❗ Error"}


def role_of(uid: int) -> str | None:
    if uid == OWNER_ID:
        return "owner"
    r = db.one("SELECT role FROM admins WHERE user_id=?", (uid,))
    return r["role"] if r else None


def perms_of(uid: int) -> set:
    role = role_of(uid)
    if not role:
        return set()
    if role == "owner":
        return set(ALL_PERMS)
    r = db.one("SELECT permissions FROM admin_roles WHERE role=?", (role,))
    return set(json.loads(r[0])) if r else set()


async def has_perm(uid: int, perm: str) -> bool:
    return perm in await A(perms_of, uid)


def admin_cmd(perm: str):
    def deco(fn):
        @functools.wraps(fn)
        async def w(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not await has_perm(update.effective_user.id, perm):
                await update.effective_message.reply_text("⛔ Permission denied.")
                return
            return await fn(update, ctx)
        return w
    return deco


async def say(update: Update, text: str, markup=None):
    chat = update.effective_chat
    if markup is MENU_KB and update.effective_user and role_of(update.effective_user.id):
        markup = MENU_KB_ADMIN
    for i in range(0, max(len(text), 1), 3900):
        await chat.send_message(text[i:i + 3900], parse_mode=ParseMode.HTML, reply_markup=markup if i + 3900 >= len(text) else None,
                                disable_web_page_preview=True)


async def edit(update: Update, text: str, markup=None):
    q = update.callback_query
    try:
        await q.edit_message_text(text[:4000], parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            await say(update, text, markup)


def uname(u) -> str:
    return f"@{u['username']}" if u["username"] else (u["first_name"] or "-")


def register_user(uid, username, first) -> bool:
    tdays = int(get_setting("trial_days", TRIAL_DAYS) or TRIAL_DAYS)
    def tx(c):
        r = c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone()
        if r:
            c.execute("UPDATE users SET username=?,first_name=?,last_seen=?,blocked=0 WHERE id=?", (username, first, iso(), uid))
            return False
        n = now()
        c.execute("INSERT INTO users(id,username,first_name,registered_at,last_seen) VALUES(?,?,?,?,?)", (uid, username, first, iso(n), iso(n)))
        c.execute("INSERT INTO trials(user_id,started_at,expires_at) VALUES(?,?,?)", (uid, iso(n), iso(n + timedelta(days=tdays))))
        c.execute("INSERT INTO activity_logs(user_id,action,detail,ts) VALUES(?,?,?,?)", (uid, "register", "trial started", iso(n)))
        return True
    return db.tx(tx)




# =====================================================================
# 12. USER PANEL
# =====================================================================

# =====================================================================
# 12. USER PANEL
# =====================================================================
async def cmd_start(update: Update, ctx):
    u = await A(plan_of, update.effective_user.id)
    extra = ""
    if u["name"] == "trial":
        extra = f"\n🎁 Trial ends: <b>{short_dt(u['expires'])}</b>"
    await say(update, f"👋 <b>Multi-User Python Bot Hosting</b>\nPlan: <b>{u['name'].title()}</b>{extra}\n\nChoose an option below.", MENU_KB)
    _res = await fs_check(update.effective_user.id)
    if not _res["ok"]:
        _t, _k = fs_prompt(_res["missing"])
        await say(update, _t, _k)


async def cmd_cancel(update: Update, ctx):
    ctx.user_data.pop("await", None)
    await say(update, "Cancelled.", MENU_KB)


async def menu_host(update, ctx):
    uid = update.effective_user.id
    if not ENGINE["ok"]:
        return await say(update, "❌ Hosting is not supported on this server right now:\n" + esc(unsupported_msg()[:600]))
    if not await fs_gate(update, ctx):                     # must have joined the channel(s) first
        return
    pl = await A(plan_of, uid)
    if not pl["active"]:
        return await say(update, "⛔ Your trial has expired. Contact Admin for Premium.")
    n = await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,))
    ctx.user_data["await"] = {"t": "host"}
    await say(update, f"🚀 <b>Host a new bot</b>\n\nSend me:\n• a <code>.py</code> file, or\n• a <code>.zip</code> project (may include requirements.txt), or\n"
                      f"• <code>requirements.txt</code> (applies to your latest uploaded bot)\n\nMax size: {human(MAX_UPLOAD)} · Bots: {n}/{pl['limits']['max_bots']}\n"
                      "After upload I extract, detect the main file, create a private virtual environment, install dependencies and start it.\n"
                      "Put your bot token in <b>Bot Settings → Environment variables</b> (e.g. BOT_TOKEN) or in your code.\n/cancel to abort.")


async def host_upload(update: Update, ctx):
    uid = update.effective_user.id
    doc = update.message.document
    fn = (doc.file_name or "").lower()
    kind = "py" if fn.endswith(".py") else "zip" if fn.endswith(".zip") else "req" if fn.endswith(".txt") and "requirements" in fn else None
    if not kind:
        return await say(update, "❌ Only <code>.py</code>, <code>.zip</code> and <code>requirements.txt</code> are accepted.")
    if doc.file_size and doc.file_size > MAX_UPLOAD:
        return await say(update, f"❌ File too large (max {human(MAX_UPLOAD)}).")
    if not ENGINE["ok"]:
        return await say(update, "❌ Hosting is not supported on this server right now. The admin was informed.")
    if not await fs_gate(update, ctx):                     # must have joined the channel(s) first
        return
    pl = await A(plan_of, uid)
    if not pl["active"]:
        return await say(update, "⛔ Hosting is not active for your account.")
    lim = pl["limits"]
    tmp = Path(tempfile.mkdtemp(prefix="tgh_up_"))
    try:
        status = await update.effective_chat.send_message("📥 Receiving file…")
        f = await doc.get_file()
        up = tmp / "upload.bin"
        await f.download_to_drive(up)
        if kind == "req":
            bid = ctx.user_data.get("last_bot")
            b = await A(get_bot, bid) if bid else None
            if not b or b["user_id"] != uid:
                return await status.edit_text("Upload your .py or .zip first, then send requirements.txt.")
            txt = up.read_text(errors="ignore")
            okr, why = validate_requirements_text(txt)
            if not okr:
                return await status.edit_text("❌ " + why)
            (bot_dir(b) / "requirements.txt").write_text(txt)
            await A(save_upload, b["id"], uid, doc.file_name or "requirements.txt", "req", up)
            await status.edit_text(f"📦 requirements.txt saved for <b>{esc(b['name'])}</b>. Installing and restarting…", parse_mode=ParseMode.HTML)
            await stop_bot(b["id"])
            spawn(pipeline(b["id"]))
            return
        if await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,)) >= lim["max_bots"]:
            return await status.edit_text(f"❌ Bot limit reached ({lim['max_bots']}). Delete a bot or upgrade to Premium.")
        base = safe_name(Path(doc.file_name).stem)
        name, i = base, 1
        while await A(db.one, "SELECT 1 FROM hosted_bots WHERE user_id=? AND name=?", (uid, name)):
            i += 1
            name = f"{base}_{i}"
        cur = await A(db.ex, "INSERT INTO hosted_bots(user_id,name,dir,created_at,status) VALUES(?,?,?,?,'installing')", (uid, name, "", iso()))
        bid = cur.lastrowid
        rel = f"user_{uid}/bot_{bid}"
        await A(set_bot, bid, dir=rel)
        d = (STORAGE / rel)
        d.mkdir(parents=True, exist_ok=True)
        try:
            if kind == "py":
                shutil.copy(up, d / safe_name(Path(doc.file_name).name, "main.py"))
            else:
                if not zipfile.is_zipfile(up):
                    raise UnsafeArchive("Not a valid ZIP file.")
                with zipfile.ZipFile(up) as zf:
                    await A(safe_extract, zf, d, "", min(lim["storage_mb"], 1024) * 1024 * 1024)
                await A(flatten_single_dir, d)
            if await A(dir_size, d) > lim["storage_mb"] * 1024 * 1024:
                raise UnsafeArchive(f"Project exceeds your storage limit ({lim['storage_mb']} MB).")
        except (UnsafeArchive, zipfile.BadZipFile) as e:
            await A(rmtree_force, d)
            await A(db.ex, "DELETE FROM hosted_bots WHERE id=?", (bid,))
            return await status.edit_text(f"❌ Rejected: {e}")
        entry, cands = await A(detect_entry, d)
        ctx.user_data["last_bot"] = bid
        await A(save_upload, bid, uid, doc.file_name or "upload", kind, up)
        alog(uid, "upload", f"{doc.file_name} -> bot {bid}", bid)
        if not cands:
            await status.edit_text("❌ No Python files found in the upload.")
            return await A(set_bot, bid, status="error", last_error="No .py files")
        if entry is None:
            await A(set_bot, bid, status="stopped")
            kb = Kb([[Btn(c[:50], callback_data=f"b:setentry:{bid}:{i}")] for i, c in enumerate(cands)])
            return await status.edit_text("🤔 I found several possible main files. Which one should I run?", reply_markup=kb)
        await A(set_bot, bid, entry=entry)
        await status.edit_text(f"✅ Received <b>{esc(name)}</b> (ID <code>{bid}</code>)\n🎯 Main file: <code>{esc(entry)}</code>\n"
                               "📦 Creating the virtual environment and installing dependencies… I'll message you when it's running.", parse_mode=ParseMode.HTML)
        spawn(pipeline(bid))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def dl_disk(b) -> int:
    c = DISK.get(b["id"])
    if c and time.time() - c[0] < 120:
        return c[1]
    try:
        v = await A(dir_size, bot_dir(b))
    except UnsafeArchive:
        v = 0
    DISK[b["id"]] = (time.time(), v)
    return v


async def menu_bots(update, ctx):
    uid = update.effective_user.id
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    if not bots:
        return await say(update, "You have no hosted bots yet. Tap <b>🚀 Host New Bot</b>.")
    for b in bots[:10]:
        t, k = await bot_card(b)
        await say(update, t, k)
    if len(bots) > 10:
        await say(update, "More bots:", Kb([[Btn(f"{b['name']} (#{b['id']})", callback_data=f"b:view:{b['id']}")] for b in bots[10:30]]))


async def menu_backups(update, ctx):
    uid = update.effective_user.id
    rows = await A(db.all, "SELECT * FROM backups WHERE user_id=? ORDER BY created_at DESC LIMIT 10", (uid,))
    txt = "💾 <b>My Backups</b> (auto every %gh)\n\n" % BACKUP_HOURS
    kb = []
    for r in rows:
        ic = {"ok": "✅", "partial": "⚠️", "failed": "❌"}.get(r["status"], "•")
        txt += f"{ic} #{r['id']} · {r['kind']} · {short_dt(r['created_at'])} · {human(r['size'] or 0)}\n"
        if r["path"] and Path(r["path"]).exists():
            kb.append([Btn(f"⬇️ Download #{r['id']}", callback_data=f"bk:get:{r['id']}")])
    if not rows:
        txt += "No backups yet."
    kb.append([Btn("➕ Backup now", callback_data="bk:now")])
    await say(update, txt, Kb(kb))


async def menu_restore(update, ctx):
    uid = update.effective_user.id
    rows = await A(db.all, "SELECT * FROM backups WHERE user_id=? AND path IS NOT NULL AND status IN ('ok','partial') ORDER BY created_at DESC LIMIT 8", (uid,))
    kb = [[Btn(f"♻️ #{r['id']} · {r['kind']} · {short_dt(r['created_at'])}", callback_data=f"rs:pick:{r['id']}")] for r in rows]
    ctx.user_data["await"] = {"t": "restore_up"}
    await say(update, "🔄 <b>Restore Backup</b>\n\nSelect an existing backup below, or upload a backup <code>.zip</code> now.\n"
                      "A safety snapshot of your current data is made first, and nothing is overwritten without your confirmation.", Kb(kb) if kb else None)


async def restore_stage(update: Update, ctx, path: Path, backup_id=None):
    uid = update.effective_user.id
    ok, m = await A(validate_user_zip, path, uid, False)
    if not ok:
        return await say(update, f"❌ {esc(m)}")
    existing = {b["name"] for b in await A(db.all, "SELECT name FROM hosted_bots WHERE user_id=?", (uid,))}
    lines = [f"🔍 <b>Backup inspected</b> (created {short_dt(m['created_at'])}, verified ✅)\n"]
    for b in m["bots"]:
        lines.append(f"• <b>{esc(b['name'])}</b>: {b.get('files', 0)} files, {human(b.get('bytes', 0))} — "
                     f"{'⚠️ will OVERWRITE current bot' if b['name'] in existing else 'new bot'}")
    last = await A(db.val, "SELECT MAX(created_at) FROM backups WHERE user_id=? AND status IN ('ok','partial')", (uid,), None)
    if last and last > m["created_at"]:
        lines.append(f"\n⚠️ A <b>newer</b> backup exists ({short_dt(last)}). Restoring this older one replaces newer data (a snapshot is taken first).")
    lines.append("\nProceed?")
    ctx.user_data["restore"] = {"path": str(path), "manifest": m, "bid": backup_id}
    await say(update, "\n".join(lines), Kb([[Btn("✅ Restore & restart bots", callback_data="rs:go:1")], [Btn("✅ Restore only", callback_data="rs:go:0")],
                                           [Btn("❌ Cancel", callback_data="rs:cancel")]]))


async def restore_upload(update: Update, ctx):
    uid = update.effective_user.id
    doc = update.message.document
    if not (doc.file_name or "").lower().endswith(".zip") or (doc.file_size or 0) > MAX_UPLOAD:
        return await say(update, "❌ Send a backup .zip within the size limit.")
    d = BACKUP_DIR / f"user_{uid}" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"upload_{now():%Y%m%d_%H%M%S}.zip"
    f = await doc.get_file()
    await f.download_to_drive(p)
    ctx.user_data.pop("await", None)
    await restore_stage(update, ctx, p)


async def menu_contact(update, ctx):
    ctx.user_data["await"] = {"t": "contact"}
    await say(update, "📞 <b>Contact Admin</b>\nType your message now and I will forward it. /cancel to abort.")



def ent_line(bp) -> str:
    n = bp["name"]
    if n == "premium":
        return f"💎 Premium: <b>{dleft(bp['expires'])} din baaki</b> ({short_dt(bp['expires'])} tak)"
    if n == "trial":
        return f"🎁 Free trial: <b>{dleft(bp['expires'])} din baaki</b> ({short_dt(bp['expires'])} tak)"
    if n == "banned":
        return "🚫 Account/bot banned"
    return "⌛ Plan khatam — bot band hai. 📞 Contact Admin se Premium lein"


async def bot_card(b) -> tuple[str, Kb]:
    bid = b["id"]
    b = await A(get_bot, bid)
    bp = await A(bot_plan, b)
    lim = bp["limits"]
    s = STATS.get(bid)
    disk = await dl_disk(b)
    up = "—"
    if s and b["status"] == "running" and s.get("started"):
        try:
            up = fmt_dur(time.time() - float(s["started"]))
        except Exception:
            up = "—"
    cpu_s = f"{s['cpu']:.1f}%" if s else "—"
    ram_s = human(s["mem"]) if s else "—"
    procs = f" · process {s['procs']}/{lim['pids']}" if s else ""
    bk_ic = "✅" if b["backup_status"] == "ok" else "❌" if b["backup_status"] == "failed" else "—"
    ban = f"\n🚫 <b>BANNED</b> by admin: {esc(b['ban_reason'] or '-')}" if b["banned"] else ""
    tgu = f"\n🔗 Telegram: @{esc(b['tg_username'])}" if b["tg_username"] else ""
    txt = (f"🤖 <b>{esc(b['name'])}</b> · Bot ID <code>{bid}</code>{tgu}\n{STATUS_ICON.get(b['status'], b['status'])}{ban}\n{ent_line(bp)}\n\n"
           f"⚙️ CPU abhi: {cpu_s} (limit {lim['cpu']}% = {lim['cpu'] / 100:g} core)\n"
           f"🧠 RAM abhi: {ram_s} (limit {lim['ram_mb']} MB){procs}\n💽 Storage: {human(disk)} (limit {lim['storage_mb']} MB)\n"
           f"⏱ Chal raha: {up} · 🔄 Aakhri restart: {short_dt(b['last_restart'])} · Restart: {b['restart_count']}\n"
           f"💾 Backup: {bk_ic} {short_dt(b['last_backup_at'])}\n"
           f"🎯 Main file: <code>{esc(b['entry'] or 'set nahi')}</code> · Crash par: {b['restart_policy']}")
    if b["last_error"]:
        c_, f_ = diagnose(b["last_error"])
        txt += f"\n\n❗ <b>Aakhri error:</b>\n<pre>{esc(b['last_error'][-350:])}</pre>\n🔍 <b>Wajah:</b> {esc(c_)}\n🛠 <b>Upay:</b> {esc(f_)}"
    kb = Kb([[Btn("▶️ Start", callback_data=f"b:start:{bid}"), Btn("⏹ Stop", callback_data=f"b:stop:{bid}"), Btn("🔄 Restart", callback_data=f"b:restart:{bid}")],
             [Btn("📜 Logs", callback_data=f"b:logs:{bid}"), Btn("📦 Install log", callback_data=f"b:ilog:{bid}"), Btn("⚙️ Settings", callback_data=f"b:set:{bid}")],
             [Btn("✏️ Naam badlo", callback_data=f"b:ren:{bid}"), Btn("⬇️ Files", callback_data=f"b:dl:{bid}"), Btn("🗑 Delete", callback_data=f"b:del:{bid}")],
             [Btn("🩺 Crash jaanch", callback_data=f"b:diag:{bid}"), Btn("📂 Meri files", callback_data=f"b:ups:{bid}")],
             [Btn("🔃 Refresh", callback_data=f"b:view:{bid}"), Btn("ℹ️ Buttons ka matlab", callback_data="menu:help")]])
    return txt, kb


async def menu_stats(update, ctx):
    uid = update.effective_user.id
    pl = await A(plan_of, uid)
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    lines = []
    for b in bots:
        bp = await A(bot_plan, b)
        s = STATS.get(b["id"])
        use = f"CPU {s['cpu']:.0f}% · RAM {human(s['mem'])}" if s else "abhi band"
        lines.append(f"{S_ICON.get(b['status'], '•')} <b>#{b['id']} {esc(b['name'])}</b> — {use}\n    {ent_line(bp)} · limit {bp['limits']['cpu']}% / {bp['limits']['ram_mb']} MB")
    disk = await A(dir_size, STORAGE / f"user_{uid}") if (STORAGE / f"user_{uid}").exists() else 0
    await say(update, f"📊 <b>Aapke bots ka status</b>\nPlan: <b>{pl['name'].title()}</b> · Bots {len(bots)}/{pl['limits']['max_bots']}\n\n"
                      + ("\n\n".join(lines) or "Abhi koi bot host nahi hai. 🚀 Host New Bot dabayein.")
                      + f"\n\n💽 Total disk (files + libraries): {human(disk)}\n\n<i>CPU 100% = 1 core. Limit paar hone par bot apne aap band ho jaata hai.</i>")


async def menu_account(update, ctx):
    uid = update.effective_user.id
    u = await A(db.one, "SELECT * FROM users WHERE id=?", (uid,))
    pl = await A(plan_of, uid)
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    t = lambda v: "🔔" if v else "🔕"
    ls = []
    for b in bots:
        ls.append(f"  #{b['id']} {esc(b['name'])} — {ent_line(await A(bot_plan, b))}")
    await say(update, f"👤 <b>Mera Account</b>\n\nID: <code>{uid}</code>\nUsername: {esc(uname(u))}\nJoin: {short_dt(u['registered_at'])}\n"
                      f"Plan: <b>{pl['name'].title()}</b> · Bots: {len(bots)}/{pl['limits']['max_bots']}\n\n<b>Aapke bots aur baaki din:</b>\n" + ("\n".join(ls) or "  koi bot nahi"),
              Kb([[Btn(f"{t(u['notify_promo'])} Promotion msg", callback_data="np:promo"), Btn(f"{t(u['notify_resource'])} Resource alert", callback_data="np:resource")],
                  [Btn(f"{t(u['notify_backup'])} Backup file", callback_data="np:backup")]]))


async def menu_premium(update, ctx):
    p = get_plans()
    f, pr = p["free"], p["premium"]
    uid = update.effective_user.id
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    ls = []
    for b in bots:
        ls.append(f"  #{b['id']} {esc(b['name'])}: {ent_line(await A(bot_plan, b))}")
    await say(update, f"💎 <b>Premium Plans</b>\n\n<b>Free / Trial ({TRIAL_DAYS} din)</b>\n• {f['max_bots']} bots · {f['cpu']}% CPU · {f['ram_mb']} MB RAM · {f['storage_mb']} MB storage\n\n"
                      f"<b>Premium</b>\n• {pr['max_bots']} bots · {pr['cpu']}% CPU · {pr['ram_mb']} MB RAM · {pr['storage_mb']} MB storage · Priority support\n\n"
                      "<b>Aapke bots:</b>\n" + ("\n".join(ls) or "  koi bot nahi") +
                      "\n\nPremium Admin chalu karta hai: <b>📞 Contact Admin</b> dabayein aur apna <b>Bot ID</b> aur kitne din chahiye likhein.",
              Kb([[Btn("📞 Contact Admin", callback_data="menu:contact")]]))


async def menu_help(update, ctx):
    await say(update, "📚 <b>Madad — ye bot kaise kaam karta hai</b>\n\n"
                      "Aap apna Telegram bot (.py ya .zip) yahan upload karte hain. Ye us ke liye alag folder aur alag Python libraries banata hai aur use 24x7 chalata hai.\n\n"
                      "<b>Neeche ke menu buttons:</b>\n"
                      "🚀 <b>Host New Bot</b> – naya bot upload karke chalu karo\n"
                      "🤖 <b>My Hosted Bots</b> – aapke bots ki list (Bot ID, status). Bot dabayein to control milta hai\n"
                      "📊 <b>Hosting Statistics</b> – har bot ne kitna CPU/RAM use kiya aur kitne din baaki hain\n"
                      f"💾 <b>My Backups</b> – apne bots ka backup banao/download karo (har {BACKUP_HOURS:g} ghante auto bhi aata hai)\n"
                      "🔄 <b>Restore Backup</b> – purana backup zip bhejkar bot wapas lao\n"
                      "👤 <b>My Account</b> – plan aur har bot ke baaki din\n"
                      "💎 <b>Premium Plans</b> – plans ki limit aur kaise lein\n"
                      "📞 <b>Contact Admin</b> – admin ko message (Premium ke liye bhi yahin)\n\n"
                      "<b>Bot ke andar ke buttons:</b>\n"
                      "▶️ Start – chalao · ⏹ Stop – roko · 🔄 Restart – dobara chalao\n"
                      "📜 Logs – bot ka output/errors · 📦 Install log – libraries install ka hisaab\n"
                      "⚙️ Settings – BOT_TOKEN jaise secret (Environment variables), crash par kya kare, main file badalna\n"
                      "✏️ Naam badlo · ⬇️ Files – apni files download · 🗑 Delete – bot hamesha ke liye hatao\n"
                      "🔃 Refresh – taaza status\n\n"
                      "<b>Zaroori:</b> BOT_TOKEN code me mat likho; ⚙️ Settings → Environment variables me daalo. requirements.txt me sirf <code>package==version</code> likhein.\n"
                      "Limit paar karne par (CPU/RAM/storage) bot apne aap band hota hai aur aapko message milta hai.\n\n"
                      "<b>Naye buttons:</b>\n🩺 <b>Crash jaanch</b> – bot band/crash ho to wajah (storage, RAM, token, libraries) aur upay\n"
                      "📂 <b>Meri files</b> – aapne jo file/ZIP upload ki thi aur bot ke folder ki files\n🤝 <b>Refer & Earn</b> – link bhejo, naya user judne par extra free din")


MENU_ROUTES = {"🚀 Host New Bot": menu_host, "🤖 My Hosted Bots": menu_bots, "📊 Hosting Statistics": menu_stats, "💾 My Backups": menu_backups,
               "🔄 Restore Backup": menu_restore, "👤 My Account": menu_account, "💎 Premium Plans": menu_premium,
               "📞 Contact Admin": menu_contact, "📚 Help & Support": menu_help, "🤝 Refer & Earn": menu_refer}


async def on_text(update: Update, ctx):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if text == "🛡 Admin Panel":
        ctx.user_data.pop("await", None)
        return await menu_admin(update, ctx)
    if text in MENU_ROUTES:
        ctx.user_data.pop("await", None)
        return await MENU_ROUTES[text](update, ctx)
    aw = ctx.user_data.get("await")
    if not aw:
        return await say(update, "Use the menu below 👇", MENU_KB)
    t = aw["t"]
    if t == "contact":
        ctx.user_data.pop("await")
        u = await A(db.one, "SELECT * FROM users WHERE id=?", (uid,))
        for aid in await A(admin_ids_with, "message"):
            await notify(aid, f"📩 <b>Message from</b> <code>{uid}</code> {esc(uname(u))}:\n\n{esc(text[:1500])}\n\nUser ID: <code>{uid}</code>", markup=Kb([[Btn("↩️ Jawab do", callback_data=f"A:mu:{uid}"), Btn("👤 User kholo", callback_data=f"A:u:{uid}")]]))
        return await say(update, "✅ Your message was sent to the Admin.", MENU_KB)
    b = await A(get_bot, aw.get("bot")) if aw.get("bot") else None
    if b and b["user_id"] != uid and not await has_perm(uid, "bots_control"):
        return
    if t == "rename":
        name = safe_name(text, "")
        if not name:
            return await say(update, "Invalid name.")
        if await A(db.one, "SELECT 1 FROM hosted_bots WHERE user_id=? AND name=? AND id<>?", (b["user_id"], name, b["id"])):
            return await say(update, "You already have a bot with that name.")
        await A(set_bot, b["id"], name=name)
        ctx.user_data.pop("await")
        return await say(update, f"✅ Renamed to <b>{esc(name)}</b>.", MENU_KB)
    if t == "env_add":
        try:
            await update.message.delete()                 # remove the secret from the chat
        except TelegramError:
            pass
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]{0,63})\s*=\s*(.+)$", text, re.S)
        if not m:
            return await say(update, "Format: <code>NAME=value</code> (message deleted). Try again or /cancel.")
        if not valid_env_name(m[1]):
            return await say(update, "❌ This variable name is reserved. Use another name (e.g. BOT_TOKEN).")
        env = dec_env(b["env_enc"])
        if len(env) >= 30 and m[1] not in env:
            return await say(update, "Maximum 30 variables.")
        env[m[1]] = m[2].strip()
        await A(set_bot, b["id"], env_enc=enc_env(env))
        ctx.user_data.pop("await")
        return await say(update, f"🔐 <code>{esc(m[1])}</code> saved (encrypted). Restart the bot to apply.",
                         Kb([[Btn("🔄 Restart now", callback_data=f"b:restart:{b['id']}")]]))
    if t in UI_AWAITS:
        return await ui_text(update, ctx, aw, text)
    if t == "send_files":
        if not await has_perm(uid, "export"):
            return
        return await do_send_files(update, ctx, aw, text)
    if t == "admin_msg":
        ctx.user_data.pop("await")


async def on_doc(update: Update, ctx):
    aw = ctx.user_data.get("await") or {}
    if aw.get("t") == "restore_up":
        return await restore_upload(update, ctx)
    if aw.get("t") == "plat_up":
        return await platform_upload(update, ctx)
    if aw.get("t") in ("send_file", "admin_zip"):
        return await ui_doc(update, ctx, aw)
    return await host_upload(update, ctx)


# ---------- inline callbacks ----------
VIEW_ACTS = {"view", "logs", "ilog", "diag", "crh", "ups", "fil"}


async def on_cb(update: Update, ctx):
    q = update.callback_query
    uid = q.from_user.id
    parts = q.data.split(":")
    try:
        await q.answer()
    except TelegramError:
        pass
    k = parts[0]
    if k == "b":
        return await cb_bot(update, ctx, parts)
    if k == "bk":
        return await cb_backup(update, ctx, parts)
    if k == "rs":
        return await cb_restore(update, ctx, parts)
    if k == "np":
        col = {"promo": "notify_promo", "resource": "notify_resource", "backup": "notify_backup"}[parts[1]]
        await A(db.ex, f"UPDATE users SET {col}=1-{col} WHERE id=?", (uid,))
        return await say(update, "✅ Preference updated. Open 👤 My Account to review.")
    if k == "fs":
        return await cb_fs(update, ctx, parts)
    if k == "menu" and parts[1] == "contact":
        return await menu_contact(update, ctx)
    if k == "menu" and parts[1] == "help":
        return await menu_help(update, ctx)
    if k == "A":
        return await cb_ui(update, ctx, parts)
    if k == "ex":
        return await cb_export(update, ctx, parts)
    if k in ("adm", "au", "pr", "ab"):
        return await cb_admin(update, ctx, parts)


async def cb_bot(update: Update, ctx, p):
    q = update.callback_query
    uid = q.from_user.id
    act, bid = p[1], int(p[2])
    b = await A(get_bot, bid)
    if not b:
        return await say(update, "Bot not found.")
    if b["user_id"] != uid:                                          # strict isolation: only owner or permitted admin
        need = "bots_view" if act in VIEW_ACTS else "bots_control"
        if not await has_perm(uid, need):
            return await say(update, "⛔ Not your bot.")
        await A(audit, uid, f"bot_{act}", bid, f"owner={b['user_id']}")
    if act == "view":
        t, kb = await bot_card(b)
        return await edit(update, t, kb)
    if act == "start":
        await edit(update, "⏳ Starting…")
        ok, msg = await start_bot(bid)
        t, kb = await bot_card(b)
        return await edit(update, f"{'✅' if ok else '❌'} {esc(msg[:300])}\n\n{t}", kb)
    if act == "stop":
        await stop_bot(bid)
        t, kb = await bot_card(b)
        return await edit(update, "⏹ Stopped.\n\n" + t, kb)
    if act == "restart":
        await edit(update, "⏳ Restarting…")
        ok, msg = await restart_bot(bid)
        t, kb = await bot_card(b)
        return await edit(update, f"{'✅' if ok else '❌'} {esc(msg[:300])}\n\n{t}", kb)
    if act in ("logs", "ilog"):
        if act == "logs":
            txt = await get_logs(bid, 80)
        else:
            ip = LOG_DIR / "bots" / f"bot_{bid}_install.log"
            txt = redact(ip.read_text(errors="ignore")[-6000:]) if ip.exists() else ""
        txt = txt.strip() or "(no logs yet)"
        if len(txt) <= 3300:
            return await say(update, f"📜 <pre>{esc(txt[-3300:])}</pre>", Kb([[Btn("🔃 Refresh", callback_data=f"b:{act}:{bid}"), Btn("⬅️ Back", callback_data=f"b:view:{bid}")]]))
        tmp = Path(tempfile.mkdtemp()) / f"bot_{bid}_{act}.txt"
        tmp.write_text(txt)
        await send_doc(uid, tmp, "📜 Logs")
        return shutil.rmtree(tmp.parent, ignore_errors=True)
    if act == "ren":
        ctx.user_data["await"] = {"t": "rename", "bot": bid}
        return await say(update, "✏️ Send the new name (/cancel to abort).")
    back_cb = f"A:b:{bid}" if b["user_id"] != uid else f"b:view:{bid}"
    back_btn = Btn("⬅️ Wapas", callback_data=back_cb)
    if act == "diag":
        await edit(update, "🩺 Jaanch ho rahi hai…")
        t = await A(crash_report, bid)
        kb = Kb([[Btn("🔄 Restart", callback_data=f"b:restart:{bid}"), Btn("📜 Logs", callback_data=f"b:logs:{bid}")],
                 [Btn("📦 Libraries dobara install", callback_data=f"b:reinst:{bid}"), Btn("⚙️ Settings", callback_data=f"b:set:{bid}")],
                 [Btn("💥 Crash history", callback_data=f"b:crh:{bid}"), Btn("📞 Contact Admin", callback_data="menu:contact")], [back_btn]])
        return await edit(update, t[:4000], kb)
    if act == "crh":
        rows = await A(db.all, "SELECT * FROM crash_log WHERE bot_id=? ORDER BY id DESC LIMIT 8", (bid,))
        parts = []
        for r in rows:
            extra = f"\n💽 Storage {r['sto_pct']:.0f}% · 🧠 RAM {r['ram_pct']:.0f}%" if r["sto_pct"] is not None else ""
            parts.append(f"🕒 {short_dt(r['ts'])} · exit {r['exit_code'] if r['exit_code'] is not None else '—'}\n🔍 {esc(r['cause'] or '')}\n🛠 {esc((r['fix'] or '')[:140])}{extra}")
        txt = f"💥 <b>Crash history — {esc(b['name'])}</b> (Bot ID <code>{bid}</code>)\n\n" + ("\n\n".join(parts) or "Abhi tak koi crash record nahi hai ✅")
        return await edit(update, txt[:3900], Kb([[Btn("🩺 Poori jaanch", callback_data=f"b:diag:{bid}")], [back_btn]]))
    if act == "ups":
        ups = await A(db.all, "SELECT * FROM bot_uploads WHERE bot_id=? ORDER BY id DESC LIMIT 10", (bid,))
        icon = {"py": "🐍", "zip": "🗜", "req": "📄"}
        lines = [f"{icon.get(r['kind'], '📄')} <b>{esc(r['filename'])}</b> · {human(r['size'])} · {short_dt(r['uploaded_at'])}" for r in ups]
        tree = await A(file_tree_text, bid)
        txt = (f"📂 <b>{esc(b['name'])}</b> (Bot ID <code>{bid}</code>) · 👤 Owner <code>{b['user_id']}</code>\n\n<b>Upload ki hui file/ZIP:</b>\n"
               + ("\n".join(lines) or "(purana bot — upload ka record nahi hai)") + "\n\n<b>Abhi bot ke folder me:</b>\n" + tree)
        rows_kb = [[Btn(f"⬇️ {r['filename'][:28]}", callback_data=f"b:upd:{bid}:{r['id']}")] for r in ups[:6]]
        rows_kb.append([Btn("⬇️ Poora project ZIP", callback_data=f"b:dl:{bid}")])
        pys = await A(py_list, bid)
        rows_kb += [[Btn(f"🐍 {pys[i][-24:]}", callback_data=f"b:ffd:{bid}:{i}") for i in range(j, min(j + 2, len(pys)))] for j in range(0, len(pys), 2)]
        rows_kb.append([back_btn])
        return await edit(update, txt[:3900], Kb(rows_kb))
    if act == "upd":
        r = await A(db.one, "SELECT * FROM bot_uploads WHERE id=? AND bot_id=?", (int(p[3]), bid))
        path = Path(r["saved_path"]).resolve() if r else None
        if not r or UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
            return await say(update, "❌ Ye file ab maujood nahi hai.")
        return await send_file_any_size(uid, path, f"⬇️ <b>{esc(r['filename'])}</b> — Bot #{bid} · uploaded {short_dt(r['uploaded_at'])}")
    if act == "ffd":
        pys = await A(py_list, bid)
        i = int(p[3])
        if i >= len(pys):
            return await say(update, "❌ File nahi mili.")
        root = bot_dir(b).resolve()
        fp = (root / pys[i]).resolve()
        if root not in fp.parents or not fp.is_file():
            return await say(update, "❌ File nahi mili.")
        return await send_file_any_size(uid, fp, f"🐍 <code>{esc(pys[i])}</code> — Bot #{bid}")
    if act == "dl":
        tmpd = Path(tempfile.mkdtemp(prefix="tgh_dl_"))
        try:
            zp = tmpd / f"{safe_name(b['name'])}_files.zip"

            def mk():
                with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
                    d = bot_dir(b)
                    for f in walk_files(d):
                        zf.write(f, f.relative_to(d).as_posix())
            await A(mk)
            if zp.stat().st_size > TG_SEND_LIMIT:
                return await say(update, "❌ Files exceed Telegram's 50 MB limit. Use 💾 My Backups (auto-split).")
            await send_doc(uid, zp, f"⬇️ Files of <b>{esc(b['name'])}</b>")
        finally:
            shutil.rmtree(tmpd, ignore_errors=True)
        return
    if act == "del":
        return await edit(update, f"🗑 Delete <b>{esc(b['name'])}</b> and ALL its files permanently?",
                          Kb([[Btn("✅ Yes, delete", callback_data=f"b:delok:{bid}"), Btn("❌ Cancel", callback_data=f"b:view:{bid}")]]))
    if act == "delok":
        await delete_bot(bid)
        return await edit(update, "🗑 Bot deleted.")
    if act == "set":
        return await edit(update, f"⚙️ <b>Settings — {esc(b['name'])}</b>\nMain file: <code>{esc(b['entry'])}</code>\nRestart policy: <b>{b['restart_policy']}</b>",
                          Kb([[Btn("🎯 Main file", callback_data=f"b:entry:{bid}"), Btn(f"🔁 Policy: {b['restart_policy']}", callback_data=f"b:pol:{bid}")],
                              [Btn("🔐 Environment variables", callback_data=f"b:env:{bid}"), Btn("📦 Reinstall deps", callback_data=f"b:reinst:{bid}")],
                              [Btn("⬅️ Back", callback_data=f"b:view:{bid}")]]))
    if act == "pol":
        order = ["on-failure", "always", "never"]
        await A(set_bot, bid, restart_policy=order[(order.index(b["restart_policy"]) + 1) % 3])
        b = await A(get_bot, bid)
        p[1] = "set"
        return await cb_bot(update, ctx, p)
    if act == "entry":
        files = await A(lambda: sorted(f.relative_to(bot_dir(b)).as_posix() for f in walk_files(bot_dir(b)) if f.suffix == ".py")[:30])
        ctx.user_data[f"files_{bid}"] = files
        return await edit(update, "🎯 Choose the main file:", Kb([[Btn(f[:55], callback_data=f"b:setentry:{bid}:{i}")] for i, f in enumerate(files)]))
    if act == "setentry":
        i = int(p[3])
        files = ctx.user_data.get(f"files_{bid}")
        if files is None:
            _, files = await A(detect_entry, bot_dir(b))
        if i >= len(files):
            return
        await A(set_bot, bid, entry=files[i])
        await say(update, f"🎯 Main file set to <code>{esc(files[i])}</code>. Installing & starting…")
        return spawn(pipeline(bid))
    if act == "reinst":
        await say(update, "📦 Reinstalling dependencies and restarting…")
        await stop_bot(bid)
        await A(rmtree_force, bot_dir(b) / ".venv")
        AUTOFIX_TRIED.pop(bid, None)
        return spawn(pipeline(bid))
    if act == "env":
        env = dec_env(b["env_enc"])
        kb = [[Btn(f"🗑 {k}", callback_data=f"b:envdel:{bid}:{i}")] for i, k in enumerate(env)]
        kb += [[Btn("➕ Add variable", callback_data=f"b:envadd:{bid}")], [Btn("⬅️ Back", callback_data=f"b:set:{bid}")]]
        return await edit(update, "🔐 <b>Environment variables</b> (stored encrypted, values never shown)\n" + ("\n".join(f"• <code>{esc(k)}</code>" for k in env) or "None"), Kb(kb))
    if act == "envadd":
        ctx.user_data["await"] = {"t": "env_add", "bot": bid}
        return await say(update, "Send <code>NAME=value</code> (e.g. <code>BOT_TOKEN=123:ABC</code>). The message is deleted right after.")
    if act == "envdel":
        env = dec_env(b["env_enc"])
        keys = list(env)
        i = int(p[3])
        if i < len(keys):
            env.pop(keys[i])
            await A(set_bot, bid, env_enc=enc_env(env))
        p[1] = "env"
        return await cb_bot(update, ctx, p)


async def cb_backup(update: Update, ctx, p):
    uid = update.callback_query.from_user.id
    if p[1] == "now":
        if not await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (uid,)):
            return await say(update, "You have no bots to back up.")
        last = await A(db.val, "SELECT MAX(created_at) FROM backups WHERE user_id=? AND kind='manual'", (uid,), None)
        if last and (now() - pdt(last)).total_seconds() < 300:
            return await say(update, "⏳ Please wait a few minutes between manual backups.")
        await say(update, "⏳ Creating backup…")
        r = await backup_one_user(uid, "manual")
        if not r["path"]:
            await say(update, "❌ Backup failed: " + esc("; ".join(r["errors"])[:300]))
        return
    if p[1] == "get":
        r = await A(db.one, "SELECT * FROM backups WHERE id=? AND user_id=?", (int(p[2]), uid))
        if not r or not r["path"] or not Path(r["path"]).exists():
            return await say(update, "Backup not found.")
        await send_file_any_size(uid, Path(r["path"]), f"💾 Backup #{r['id']} · {short_dt(r['created_at'])}")


async def cb_restore(update: Update, ctx, p):
    uid = update.callback_query.from_user.id
    if p[1] == "pick":
        r = await A(db.one, "SELECT * FROM backups WHERE id=? AND user_id=?", (int(p[2]), uid))
        if not r or not r["path"] or not Path(r["path"]).exists():
            return await say(update, "Backup file not found.")
        h = hashlib.sha256()
        with open(r["path"], "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
        if r["sha256"] and h.hexdigest() != r["sha256"]:
            return await say(update, "❌ Integrity check failed: backup file was modified or corrupted.")
        return await restore_stage(update, ctx, Path(r["path"]), r["id"])
    if p[1] == "cancel":
        ctx.user_data.pop("restore", None)
        return await edit(update, "Restore cancelled. Nothing was changed.")
    if p[1] == "go":
        st = ctx.user_data.pop("restore", None)
        if not st:
            return await edit(update, "Session expired. Start again from 🔄 Restore Backup.")
        await edit(update, "⏳ Restoring… (a safety snapshot is created first)")
        rep = await restore_user_backup(uid, Path(st["path"]), p[2] == "1", st["manifest"])
        await say(update, "🔄 <b>Restoration report</b>\n\n" + "\n".join(esc(x) for x in rep))
        await notify_admins(f"🔄 User <code>{uid}</code> restored a backup.", "users_view", kind="restore_note", day=f"{uid}-{iso()[:16]}")




# =====================================================================
# 13. ADMIN PANEL
# =====================================================================

# =====================================================================
# 13. ADMIN PANEL
# =====================================================================
def parse_int(s) -> int | None:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


@admin_cmd("view_stats")
async def cmd_stats(update, ctx):
    await say(update, dash_text(await collect_stats()))


@admin_cmd("reports")
async def cmd_report(update, ctx):
    await daily_report_job(force=True)


@admin_cmd("users_view")
async def cmd_users(update, ctx):
    q = " ".join(ctx.args) if ctx.args else ""
    if q and parse_int(q):
        return await show_user(update, parse_int(q))
    rows = await A(db.all, "SELECT * FROM users WHERE (?='' OR username LIKE ? OR first_name LIKE ?) ORDER BY registered_at DESC LIMIT 40", (q, f"%{q}%", f"%{q}%"))
    lines = []
    for u in rows:
        pl = await A(plan_of, u["id"])
        lines.append(f"<code>{u['id']}</code> {esc(uname(u))} · {pl['name']}")
    await say(update, f"👥 <b>Users</b> ({await A(db.val, 'SELECT COUNT(*) FROM users')} total)\n\n" + ("\n".join(lines) or "none") + "\n\nDetails: /user ID")


async def show_user(update, uid):
    u = await A(db.one, "SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return await say(update, "User not found.")
    pl = await A(plan_of, uid)
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=?", (uid,))
    hist = await A(db.all, "SELECT action,detail,ts FROM activity_logs WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,))
    cpu = sum(STATS.get(b["id"], {}).get("cpu", 0) for b in bots)
    ram = sum(STATS.get(b["id"], {}).get("mem", 0) for b in bots)
    txt = (f"👤 <b>User</b> <code>{uid}</code> {esc(uname(u))}\nJoined: {short_dt(u['registered_at'])} · last seen {short_dt(u['last_seen'])}\n"
           f"Plan: <b>{pl['name']}</b> (until {short_dt(pl['expires'])}) · banned: {bool(u['banned'])} · blocked bot: {bool(u['blocked'])}\n"
           f"Bots: {len(bots)} · CPU {cpu:.1f}% · RAM {human(ram)}\n" + "".join(f"• #{b['id']} {esc(b['name'])} — {b['status']}\n" for b in bots) +
           "\n<b>History:</b>\n" + "\n".join(f"{short_dt(h['ts'])} {h['action']} {esc((h['detail'] or '')[:50])}" for h in hist))
    kb = [[Btn("+7d trial", callback_data=f"au:ext:{uid}:7"), Btn("💎 +30d", callback_data=f"au:prem:{uid}:30"), Btn("❌ Premium", callback_data=f"au:unprem:{uid}")],
          [Btn("🚫 Ban" if not u["banned"] else "✅ Unban", callback_data=f"au:{'ban' if not u['banned'] else 'unban'}:{uid}"),
           Btn("🗑 Delete data", callback_data=f"au:del:{uid}")]]
    kb += [[Btn(f"🤖 #{b['id']} {b['name'][:20]}", callback_data=f"b:view:{b['id']}")] for b in bots[:8]]
    await say(update, txt, Kb(kb))


@admin_cmd("users_view")
async def cmd_user(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid:
        return await say(update, "Usage: /user USER_ID")
    await show_user(update, uid)


async def list_bots(update, ctx, where="", title="🤖 Bots"):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    sql = "SELECT * FROM hosted_bots" + (f" WHERE {where}" if where else "") + (" AND user_id=?" if uid and where else " WHERE user_id=?" if uid else "") + " ORDER BY id LIMIT 60"
    rows = await A(db.all, sql, (uid,) if uid else ())
    await say(update, f"<b>{title}</b>\n\n" + ("\n".join(f"#{b['id']} {esc(b['name'])} · owner <code>{b['user_id']}</code> · {STATUS_ICON.get(b['status'], b['status'])}" for b in rows) or "none") + "\n\nDetail: /bot ID")


@admin_cmd("bots_view")
async def cmd_running(update, ctx):
    return await list_bots(update, ctx, "status='running'", "🟢 Running bots")


@admin_cmd("bots_view")
async def cmd_stopped(update, ctx):
    return await list_bots(update, ctx, "status<>'running'", "🔴 Stopped/crashed bots")


@admin_cmd("bots_view")
async def cmd_bots(update, ctx):
    return await list_bots(update, ctx)


@admin_cmd("bots_view")
async def cmd_bot(update, ctx):
    bid = parse_int(ctx.args[0]) if ctx.args else None
    b = await A(get_bot, bid) if bid else None
    if not b:
        return await say(update, "Usage: /bot BOT_ID")
    t, kb = await bot_card(b)
    kb = Kb(list(kb.inline_keyboard) + [[Btn("✅ Unban bot" if b["banned"] else "🚫 Ban bot", callback_data=f"ab:{'unban' if b['banned'] else 'ban'}:{b['id']}")]])
    await say(update, f"👤 Owner: <code>{b['user_id']}</code> · last activity {short_dt(b['last_activity'])}\n{t}", kb)


def bot_admin_cmd(action):
    @admin_cmd("bots_control")
    async def h(update, ctx):
        bid = parse_int(ctx.args[0]) if ctx.args else None
        if not bid or not await A(get_bot, bid):
            return await say(update, f"Usage: /{action}bot BOT_ID")
        await A(audit, update.effective_user.id, f"{action}_bot", bid)
        ok, msg = await {"start": start_bot, "stop": stop_bot, "restart": restart_bot}[action](bid)
        await say(update, f"{'✅' if ok else '❌'} {esc(msg)}")
    return h


@admin_cmd("broadcast")
async def cmd_broadcast(update, ctx):
    msg = update.message.reply_to_message.text if update.message.reply_to_message and update.message.reply_to_message.text else " ".join(ctx.args)
    if not msg:
        return await say(update, "Usage: /broadcast TEXT (or reply to a message)")
    await A(audit, update.effective_user.id, "broadcast", "", msg[:100])
    users = await A(db.all, "SELECT id FROM users WHERE blocked=0")
    admin = update.effective_user.id

    async def run():
        ok = 0
        for u in users:
            ok += await notify(u["id"], f"📢 <b>Announcement</b>\n\n{esc(msg)}")
        await notify(admin, f"📢 Broadcast finished: {ok}/{len(users)} delivered.")
    spawn(run())
    await say(update, f"📢 Broadcasting to {len(users)} users…")


@admin_cmd("message")
async def cmd_msg(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid or len(ctx.args) < 2:
        return await say(update, "Usage: /msg USER_ID TEXT")
    ok = await notify(uid, f"💬 <b>Message from Admin</b>\n\n{esc(' '.join(ctx.args[1:]))}")
    await A(audit, update.effective_user.id, "msg", uid)
    await say(update, "✅ Sent." if ok else "❌ Could not deliver (user blocked the bot?).")


@admin_cmd("maintenance")
async def cmd_maint(update, ctx):
    if not ctx.args or ctx.args[0] not in ("on", "off"):
        return await say(update, f"Maintenance is <b>{'ON' if await A(get_setting, 'maintenance', False) else 'OFF'}</b>. Usage: /maintenance on|off")
    await A(set_setting, "maintenance", ctx.args[0] == "on")
    await A(audit, update.effective_user.id, "maintenance", ctx.args[0])
    await say(update, f"🛠 Maintenance {ctx.args[0].upper()}.")


@admin_cmd("users_manage")
async def cmd_extend(update, ctx):
    uid, days = (parse_int(ctx.args[0]), parse_int(ctx.args[1])) if len(ctx.args) >= 2 else (None, None)
    if not uid or not days:
        return await say(update, "Usage: /extend USER_ID DAYS")
    await say(update, await do_extend(update.effective_user.id, uid, days))


async def do_extend(admin, uid, days) -> str:
    tr = await A(db.one, "SELECT * FROM trials WHERE user_id=?", (uid,))
    if not tr:
        return "User not found."
    base = max(pdt(tr["expires_at"]), now())
    await A(db.ex, "UPDATE trials SET expires_at=?, extended_days=extended_days+?, status='active' WHERE user_id=?", (iso(base + timedelta(days=days)), days, uid))
    await A(audit, admin, "extend_trial", uid, f"+{days}d")
    await notify(uid, f"🎁 Your free trial was extended by <b>{days} days</b>.")
    await after_entitlement_change(uid)
    return f"✅ Trial extended/reactivated by {days} days."


@admin_cmd("premium")
async def cmd_premium(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    days = parse_int(ctx.args[1]) if len(ctx.args) > 1 else get_plans()["premium"]["premium_days"]
    if not uid or not days or not await A(db.one, "SELECT 1 FROM users WHERE id=?", (uid,)):
        return await say(update, "Usage: /premium USER_ID [DAYS]")
    await say(update, await do_premium(update.effective_user.id, uid, days))


async def do_premium(admin, uid, days) -> str:
    exp = await A(grant_premium, uid, days, admin)
    await A(audit, admin, "premium_on", uid, f"{days}d")
    await notify(uid, f"💎 <b>Premium activated!</b> Valid until <b>{short_dt(exp)}</b>. Enjoy higher limits.")
    await after_entitlement_change(uid, restart=True)
    return f"✅ Premium until {short_dt(exp)}."


@admin_cmd("premium")
async def cmd_unpremium(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid:
        return await say(update, "Usage: /unpremium USER_ID")
    await say(update, await do_unpremium(update.effective_user.id, uid))


async def do_unpremium(admin, uid) -> str:
    await A(db.ex, "UPDATE subscriptions SET status='cancelled' WHERE user_id=? AND status='active'", (uid,))
    await A(audit, admin, "premium_off", uid)
    await notify(uid, "ℹ️ Your Premium plan was deactivated.")
    await after_entitlement_change(uid, restart=True)
    return "✅ Premium deactivated."


@admin_cmd("users_manage")
async def cmd_deleteuser(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid:
        return await say(update, "Usage: /deleteuser USER_ID")
    await say(update, f"⚠️ Delete ALL hosting data (bots, files, backups) of <code>{uid}</code>?",
              Kb([[Btn("✅ Confirm delete", callback_data=f"au:delok:{uid}"), Btn("❌ Cancel", callback_data="au:cancel:0")]]))


async def do_delete_user_data(admin, uid) -> str:
    if await A(role_of, uid):
        return "❌ Cannot delete an administrator's data this way."
    for b in await A(db.all, "SELECT id FROM hosted_bots WHERE user_id=?", (uid,)):
        await delete_bot(b["id"])
    await A(rmtree_force, STORAGE / f"user_{uid}")
    await A(rmtree_force, BACKUP_DIR / f"user_{uid}")
    await A(db.ex, "DELETE FROM backups WHERE user_id=?", (uid,))
    await A(audit, admin, "delete_user_data", uid)
    return "🗑 User hosting data deleted (account record kept)."


@admin_cmd("backup")
async def cmd_backup(update, ctx):
    uid = update.effective_user.id
    if ctx.args and ctx.args[0] == "users":
        await A(audit, uid, "backup_users")
        await say(update, "⏳ Running backup for all users…")
        spawn(backup_job(manual_by=uid))
        return
    await say(update, "⏳ Creating encrypted platform backup…")
    p = await A(build_platform_backup, "manual")
    await A(set_setting, "last_platform_backup", iso())
    await A(audit, uid, "backup_platform", p.name)
    await send_file_any_size(uid, p, f"🗄 <b>Platform backup</b> (encrypted)\n{short_dt(iso())}")


@admin_cmd("logs")
async def cmd_logs(update, ctx):
    what = ctx.args[0] if ctx.args else "system"
    if what == "bot" and len(ctx.args) > 1 and parse_int(ctx.args[1]):
        txt = await get_logs(int(ctx.args[1]), 100)
    elif what == "audit":
        txt = "\n".join(f"{short_dt(r['ts'])} {r['admin_id']} {r['action']} {r['target']} {r['detail'] or ''}" for r in await A(db.all, "SELECT * FROM audit_logs ORDER BY id DESC LIMIT 40"))
    elif what == "activity":
        txt = "\n".join(f"{short_dt(r['ts'])} u{r['user_id']} {r['action']} {r['detail'] or ''}" for r in await A(db.all, "SELECT * FROM activity_logs ORDER BY id DESC LIMIT 40"))
    elif what in ("system", "backup", "restore", "admin", "dependency", "hosting"):
        p = LOG_DIR / f"{what}.log"
        txt = redact("\n".join(p.read_text(errors="ignore").splitlines()[-40:])) if p.exists() else ""
    else:
        return await say(update, "Usage: /logs [system|backup|restore|admin|dependency|audit|activity|bot ID]")
    await say(update, f"<pre>{esc((txt or '(empty)')[-3500:])}</pre>")


@admin_cmd("plans")
async def cmd_plans(update, ctx):
    await say(update, "<b>Plans</b>\n<pre>" + esc(json.dumps(get_plans(), indent=1)) + "</pre>\nChange: /setplan free|premium KEY VALUE")


@admin_cmd("plans")
async def cmd_setplan(update, ctx):
    if len(ctx.args) < 3 or ctx.args[0] not in DEFAULT_PLANS or ctx.args[1] not in DEFAULT_PLANS["free"] | {"premium_days": 0}:
        return await say(update, "Usage: /setplan free|premium KEY VALUE\nKeys: max_bots cpu ram_mb storage_mb pids backup_keep premium_days")
    try:
        val = float(ctx.args[2]) if ctx.args[1] == "cpu" else int(ctx.args[2])
        assert val > 0
    except (ValueError, AssertionError):
        return await say(update, "Invalid value.")
    plans = get_plans()
    plans[ctx.args[0]][ctx.args[1]] = val
    await A(set_setting, "plans", plans)
    await A(audit, update.effective_user.id, "setplan", ctx.args[0], f"{ctx.args[1]}={val}")
    await say(update, "✅ Saved. Limits apply to bots on next (re)start.")


@admin_cmd("view_stats")
async def cmd_admins(update, ctx):
    rows = await A(db.all, "SELECT * FROM admins ORDER BY role,user_id")
    await say(update, "👮 <b>Administrators</b>\n" + "\n".join(f"<code>{r['user_id']}</code> — {r['role']}" for r in rows))


@admin_cmd("admins")
async def cmd_addadmin(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    role = ctx.args[1] if len(ctx.args) > 1 else "support"
    if not uid or not await A(db.one, "SELECT 1 FROM admin_roles WHERE role=?", (role,)):
        return await say(update, "Usage: /addadmin USER_ID [super|support|technical|owner]")
    await A(db.ex, "INSERT INTO admins(user_id,role,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", (uid, role, update.effective_user.id, iso()))
    await A(audit, update.effective_user.id, "addadmin", uid, role)
    await notify(uid, f"🛡 You are now an administrator (<b>{role}</b>). Send /admin.")
    await say(update, f"✅ {uid} is now <b>{role}</b>.")


@admin_cmd("admins")
async def cmd_removeadmin(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid or uid == OWNER_ID:
        return await say(update, "Usage: /removeadmin USER_ID (the primary owner cannot be removed)")
    await A(db.ex, "DELETE FROM admins WHERE user_id=?", (uid,))
    await A(audit, update.effective_user.id, "removeadmin", uid)
    await say(update, "✅ Removed.")


@admin_cmd("admins")
async def cmd_setperm(update, ctx):
    if len(ctx.args) != 3 or ctx.args[0] == "owner" or ctx.args[1] not in ALL_PERMS or ctx.args[2] not in ("on", "off"):
        return await say(update, "Usage: /setperm ROLE PERM on|off\nPerms: " + ", ".join(ALL_PERMS))
    r = await A(db.one, "SELECT permissions FROM admin_roles WHERE role=?", (ctx.args[0],))
    if not r:
        return await say(update, "Unknown role.")
    perms = set(json.loads(r[0]))
    (perms.add if ctx.args[2] == "on" else perms.discard)(ctx.args[1])
    await A(db.ex, "UPDATE admin_roles SET permissions=? WHERE role=?", (json.dumps(sorted(perms)), ctx.args[0]))
    await A(audit, update.effective_user.id, "setperm", ctx.args[0], f"{ctx.args[1]}={ctx.args[2]}")
    await say(update, "✅ Updated.")


async def on_error(update, ctx):
    log.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)




# =====================================================================
# 13b. BAN / UNBAN, EXPORT, SEND BACKUPS, SELF-CHECK, RESTORE (new admin features)
# =====================================================================
async def precheck(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not u or update.effective_chat is None or update.effective_chat.type != "private" or u.is_bot:
        raise ApplicationHandlerStop
    new = await A(register_user, u.id, u.username, u.first_name)
    if new:
        _ref = parse_ref(update)
        if _ref:
            spawn(handle_referral(_ref, u))
        await say(update, f"🎉 <b>Welcome, {esc(u.first_name)}!</b>\nAapka <b>{await A(get_global, 'trial_days')} din ka free trial</b> shuru ho gaya hai. "
                          "Uske baad bots chalane ke liye Premium lena hoga.\n🤝 Dosto ko refer karke extra free din kamayein!", MENU_KB)
        if await A(get_setting, "notify_admin_new_user", True):
            await notify_admins(f"🆕 New user <code>{u.id}</code> {esc(u.first_name)} @{esc(u.username or '-')}", "users_view", kind="newuser", day=str(u.id))
    is_adm = await A(role_of, u.id)
    row = await A(db.one, "SELECT banned,ban_reason FROM users WHERE id=?", (u.id,))
    if row and row["banned"] and not is_adm:
        txt = (update.message.text or "") if update.message else ""
        cb = update.callback_query.data if update.callback_query else ""
        aw = (ctx.user_data.get("await") or {}).get("t")
        if txt in ("📞 Contact Admin", "/cancel") or cb == "menu:contact" or (aw == "contact" and txt and not txt.startswith("/")):
            return                                           # a banned user may only write to the admin
        await update.effective_chat.send_message(
            f"🚫 <b>Your account is banned.</b>\nReason: {esc(row['ban_reason'] or 'not given')}\nYour files are kept safe. "
            "You can only use <b>📞 Contact Admin</b>.", parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardMarkup([["📞 Contact Admin"]], resize_keyboard=True))
        raise ApplicationHandlerStop
    if await A(get_setting, "maintenance", False) and not is_adm:
        await update.effective_chat.send_message("🛠 The platform is under maintenance. Your running bots are not affected. Please try again later.")
        raise ApplicationHandlerStop


async def do_ban_user(admin, uid, on: bool, reason: str = "", start: bool = False) -> str:
    if on and await A(role_of, uid):
        return "❌ Cannot ban an administrator. Remove the admin role first."
    u = await A(db.one, "SELECT 1 FROM users WHERE id=?", (uid,))
    if not u:
        return "User not found."
    if on:
        await A(db.ex, "UPDATE users SET banned=1, ban_reason=?, banned_at=?, banned_by=? WHERE id=?", (reason[:200] or None, iso(), admin, uid))
    else:
        await A(db.ex, "UPDATE users SET banned=0, ban_reason=NULL, banned_at=NULL, banned_by=NULL WHERE id=?", (uid,))
    await A(db.ex, "INSERT INTO ban_log(kind,target,action,reason,admin_id,ts) VALUES('user',?,?,?,?,?)",
            (uid, "ban" if on else "unban", reason[:200], admin, iso()))
    await A(audit, admin, "ban_user" if on else "unban_user", uid, reason)
    if on:
        await notify(uid, f"🚫 <b>Your account was banned by the Admin.</b>\nReason: {esc(reason or 'not given')}\nAll your bots were stopped; your files stay safe. "
                          "Use 📞 Contact Admin if you think this is a mistake.")
        await after_entitlement_change(uid)                  # plan_of() says inactive -> all bots stop (auto_stopped)
        return f"🚫 User {uid} banned; their bots were stopped (data kept)."
    await notify(uid, "✅ <b>Your account was reactivated.</b>" + (" Your bots are starting again." if start else " Start your bots from 🤖 My Hosted Bots."),
                 markup=MENU_KB)
    if start:
        await after_entitlement_change(uid)
    return f"✅ User {uid} unbanned." + (" Bots that were auto-stopped are being started." if start else " Their bots stay stopped until they start them.")


async def do_ban_bot(admin, bid, on: bool, reason: str = "") -> str:
    b = await A(get_bot, bid)
    if not b:
        return "Bot not found."
    if on:
        await A(set_bot, bid, banned=1, ban_reason=reason[:200] or None, banned_at=iso())
        await stop_bot(bid, auto=False, reason="Banned by admin: " + (reason or "-"))
        await A(set_bot, bid, status="stopped")
    else:
        await A(set_bot, bid, banned=0, ban_reason=None, banned_at=None, last_error=None)
    await A(db.ex, "INSERT INTO ban_log(kind,target,action,reason,admin_id,ts) VALUES('bot',?,?,?,?,?)",
            (bid, "ban" if on else "unban", reason[:200], admin, iso()))
    await A(audit, admin, "ban_bot" if on else "unban_bot", bid, reason)
    await notify(b["user_id"], (f"🚫 Your bot <b>{esc(b['name'])}</b> was banned and stopped by the Admin.\nReason: {esc(reason or 'not given')}") if on
                 else f"✅ Your bot <b>{esc(b['name'])}</b> was unbanned. You can start it again.")
    return f"{'🚫 Bot banned and stopped' if on else '✅ Bot unbanned (not started automatically)'}: #{bid}."


@admin_cmd("ban_users")
async def cmd_ban(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid:
        return await say(update, "Usage: /ban USER_ID [reason]")
    await say(update, await do_ban_user(update.effective_user.id, uid, True, " ".join(ctx.args[1:])))


@admin_cmd("ban_users")
async def cmd_unban(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid:
        return await say(update, "Usage: /unban USER_ID [start]   (start = also start their bots again)")
    await say(update, await do_ban_user(update.effective_user.id, uid, False, "", start=len(ctx.args) > 1 and ctx.args[1].lower() == "start"))


@admin_cmd("ban_bots")
async def cmd_banbot(update, ctx):
    bid = parse_int(ctx.args[0]) if ctx.args else None
    if not bid:
        return await say(update, "Usage: /banbot BOT_ID [reason]")
    await say(update, await do_ban_bot(update.effective_user.id, bid, True, " ".join(ctx.args[1:])))


@admin_cmd("ban_bots")
async def cmd_unbanbot(update, ctx):
    bid = parse_int(ctx.args[0]) if ctx.args else None
    if not bid:
        return await say(update, "Usage: /unbanbot BOT_ID")
    await say(update, await do_ban_bot(update.effective_user.id, bid, False))


@admin_cmd("users_view")
async def cmd_bans(update, ctx):
    us = await A(db.all, "SELECT id,username,first_name,ban_reason,banned_at FROM users WHERE banned=1 ORDER BY banned_at DESC LIMIT 40")
    bs = await A(db.all, "SELECT id,name,user_id,ban_reason FROM hosted_bots WHERE banned=1 ORDER BY id LIMIT 40")
    hist = await A(db.all, "SELECT * FROM ban_log ORDER BY id DESC LIMIT 10")
    txt = "🚫 <b>Banned users</b>\n" + ("\n".join(f"<code>{u['id']}</code> {esc(uname(u))} — {esc(u['ban_reason'] or '-')} ({short_dt(u['banned_at'])})" for u in us) or "none")
    txt += "\n\n🚫 <b>Banned bots</b>\n" + ("\n".join(f"#{b['id']} {esc(b['name'])} (user <code>{b['user_id']}</code>) — {esc(b['ban_reason'] or '-')}" for b in bs) or "none")
    txt += "\n\n🧾 <b>Ban history</b>\n" + ("\n".join(f"{short_dt(h['ts'])} {h['kind']} {h['target']} {h['action']} by {h['admin_id']} {esc(h['reason'] or '')}" for h in hist) or "none")
    await say(update, txt + "\n\n/ban /unban /banbot /unbanbot")


# ---------- export & sending of backups ----------
async def _deliver(admin: int, path: Path, caption: str) -> bool:
    return await send_file_any_size(admin, path, caption)


@admin_cmd("export")
async def cmd_export(update, ctx):
    """Bulk export: ONE master zip, a folder per user (users/<id>/user.json + bots/...)."""
    admin = update.effective_user.id
    await A(audit, admin, "export_master")
    await say(update, "⏳ Creating MASTER backup of ALL users (bots keep running)…")
    try:
        p = await A(build_master_backup, "manual")
    except Exception as e:
        blog.exception("master export")
        return await say(update, f"❌ Export failed: {esc(e)}")
    await A(set_setting, "last_master_backup", iso())
    await say(update, f"✅ <b>{esc(p.name)}</b> · {human(p.stat().st_size)}\nSaved on the server: <code>{esc(p)}</code>\n"
                      f"🔑 It can only be restored with this server's BACKUP_PASSPHRASE (keep a copy elsewhere).")
    if not await _deliver(admin, p, f"📦 <b>{esc(p.name)}</b>\nFolder per user: users/&lt;id&gt;/…"):
        await say(update, "⚠️ Could not send the file via Telegram; take it from the path above.")


@admin_cmd("export")
async def cmd_exportuser(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    if not uid or not await A(db.one, "SELECT 1 FROM users WHERE id=?", (uid,)):
        return await say(update, "Usage: /exportuser USER_ID   (all bots + account data of one user)")
    await A(audit, update.effective_user.id, "export_user", uid)
    r = await A(build_user_backup, uid, "export", None, True)
    if not r["path"]:
        return await say(update, "❌ Export failed: " + esc("; ".join(r["errors"])[:300]))
    await _deliver(update.effective_user.id, r["path"], f"📦 Complete data of user <code>{uid}</code> · status {r['status']}")


@admin_cmd("export")
async def cmd_exportbot(update, ctx):
    bid = parse_int(ctx.args[0]) if ctx.args else None
    b = await A(get_bot, bid) if bid else None
    if not b:
        return await say(update, "Usage: /exportbot BOT_ID")
    await A(audit, update.effective_user.id, "export_bot", bid)
    r = await A(build_user_backup, b["user_id"], "export", [bid], True)
    if not r["path"]:
        return await say(update, "❌ Export failed: " + esc("; ".join(r["errors"])[:300]))
    await _deliver(update.effective_user.id, r["path"], f"📦 Bot #{bid} <b>{esc(b['name'])}</b> (user <code>{b['user_id']}</code>)")


@admin_cmd("export")
async def cmd_sendbackup(update, ctx):
    uid = parse_int(ctx.args[0]) if ctx.args else None
    u = await A(db.one, "SELECT * FROM users WHERE id=?", (uid,)) if uid else None
    if not u:
        return await say(update, "Usage: /sendbackup USER_ID")
    await say(update, f"📤 <b>Send backup to</b> <code>{uid}</code> {esc(uname(u))}\nChoose what to send:",
              Kb([[Btn("🤖 One bot", callback_data=f"ex:bot:{uid}"), Btn("🤖 All bots", callback_data=f"ex:all:{uid}")],
                  [Btn("👤 Complete user data", callback_data=f"ex:full:{uid}"), Btn("📄 Selected files", callback_data=f"ex:files:{uid}")],
                  [Btn("🔗 Generate download link", callback_data=f"ex:link:{uid}")]]))


async def do_send_files(update, ctx, aw, text: str):
    """text: 'BOT_ID path1 path2 ...' -> zip of those files sent to the user."""
    ctx.user_data.pop("await", None)
    uid, admin = aw["uid"], update.effective_user.id
    parts = text.split()
    bid = parse_int(parts[0]) if parts else None
    b = await A(get_bot, bid) if bid else None
    if not b or b["user_id"] != uid or len(parts) < 2:
        return await say(update, "❌ Format: <code>BOT_ID file1.py folder/file2.txt</code> (the bot must belong to that user).")
    d = bot_dir(b)
    files, bad = [], []
    for rel in parts[1:21]:
        p = (d / rel).resolve()
        if d in p.parents and p.is_file() and ".venv" not in p.relative_to(d).parts:
            files.append((p, p.relative_to(d).as_posix()))
        else:
            bad.append(rel)
    if not files:
        return await say(update, "❌ None of these files exist: " + esc(", ".join(bad)))
    tmpd = Path(tempfile.mkdtemp(prefix="tgh_sf_"))
    try:
        zp = tmpd / f"selected_files_bot{bid}.zip"

        def mk():
            with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as zf:
                for p, rel in files:
                    zf.write(p, rel)
        await A(mk)
        ok = await send_file_any_size(uid, zp, f"📄 Files from <b>{esc(b['name'])}</b> sent by the Admin")
    finally:
        rmtree_force(tmpd)
    await A(audit, admin, "send_files", uid, f"bot {bid}: {len(files)} files")
    await say(update, f"{'✅ Sent' if ok else '❌ Could not deliver'} {len(files)} file(s)." + (f"\nNot found: {esc(', '.join(bad))}" if bad else ""))


async def cb_export(update: Update, ctx, p):
    admin = update.callback_query.from_user.id
    if not await has_perm(admin, "export"):
        return await say(update, "⛔ Permission denied.")
    act, uid = p[1], int(p[2])
    if act == "bot":
        bots = await A(db.all, "SELECT id,name FROM hosted_bots WHERE user_id=? ORDER BY id LIMIT 30", (uid,))
        if not bots:
            return await say(update, "This user has no bots.")
        return await say(update, "Which bot?", Kb([[Btn(f"#{b['id']} {b['name'][:30]}", callback_data=f"ex:botgo:{b['id']}")] for b in bots]))
    if act == "botgo":
        b = await A(get_bot, uid)                            # here the number is a BOT id
        if not b:
            return await say(update, "Bot not found.")
        r = await A(build_user_backup, b["user_id"], "send", [b["id"]], False)
        if not r["path"]:
            return await say(update, "❌ Backup failed: " + esc("; ".join(r["errors"])[:300]))
        ok = await send_file_any_size(b["user_id"], r["path"], f"💾 Backup of <b>{esc(b['name'])}</b> sent by the Admin")
        await A(audit, admin, "send_backup_bot", b["user_id"], f"bot {b['id']} delivered={ok}")
        return await say(update, "✅ Sent to the user." if ok else "❌ Could not deliver (user blocked the bot?). The file is stored on the server.")
    if act in ("all", "full"):
        await say(update, "⏳ Creating backup…")
        r = await A(build_user_backup, uid, "send", None, act == "full")
        if not r["path"]:
            return await say(update, "❌ Backup failed: " + esc("; ".join(r["errors"])[:300]))
        ok = await send_file_any_size(uid, r["path"], f"💾 {'Complete data' if act == 'full' else 'Backup of all your bots'} sent by the Admin")
        await A(audit, admin, f"send_backup_{act}", uid, f"delivered={ok}")
        return await say(update, ("✅ Sent to the user." if ok else "❌ Could not deliver (user blocked the bot?).") + f" ({r['status']}, {human(r['size'])})")
    if act == "files":
        ctx.user_data["await"] = {"t": "send_files", "uid": uid}
        bots = await A(db.all, "SELECT id,name FROM hosted_bots WHERE user_id=?", (uid,))
        return await say(update, "📄 Send: <code>BOT_ID path1 path2 …</code> (max 20 files, relative to the bot folder)\nBots: "
                                 + (", ".join(f"#{b['id']} {esc(b['name'])}" for b in bots) or "none") + "\n/cancel to abort.")
    if act == "link":
        await say(update, "⏳ Creating backup and link…")
        r = await A(build_user_backup, uid, "send", None, True)
        if not r["path"]:
            return await say(update, "❌ Backup failed: " + esc("; ".join(r["errors"])[:300]))
        url, tok = await A(make_download_link, r["path"], r["path"].name, admin, uid)
        await A(audit, admin, "make_link", uid)
        if not url:
            return await say(update, f"❌ {esc(tok)}")
        return await say(update, f"🔗 Link (valid 24 h, max 3 downloads):\n<code>{esc(url)}</code>\n"
                                 + ("" if PUBLIC_BASE_URL else "⚠️ It works only on THIS computer/network unless you set PUBLIC_BASE_URL (tunnel/domain) in .env."))


# ---------- self-check ----------
@admin_cmd("system")
async def cmd_selfcheck(update, ctx):
    await say(update, "⏳ Testing subprocess / venv / pip / background processes…")
    ok, msg = await A(run_engine_check)
    await say(update, f"{'✅' if ok else '❌'} <pre>{esc(msg)}</pre>")


@admin_cmd("system")
async def cmd_system(update, ctx):
    try:
        vm, du = psutil.virtual_memory(), shutil.disk_usage(BASE)
        cpu = f"{psutil.cpu_percent(interval=0.5):.0f}% ({psutil.cpu_count()} cores)"
        ram, disk = f"{human(vm.used)}/{human(vm.total)}", f"{human(du.used)}/{human(du.total)}"
        up = fmt_dur(time.time() - psutil.boot_time())
    except Exception as e:                                           # Android/Termux may restrict /proc
        cpu = ram = disk = up = f"n/a ({type(e).__name__})"
    load = ""
    if hasattr(os, "getloadavg"):
        try:
            load = f" · load {os.getloadavg()[0]:.2f}"
        except OSError:
            pass
    running = await A(db.val, "SELECT COUNT(*) FROM hosted_bots WHERE status='running'")
    await say(update, f"🖥 <b>System</b>\nCPU: {cpu}{load}\nRAM: {ram}\nDisk: {disk}\n"
                      f"Hosting engine: {'✅ OK' if ENGINE['ok'] else '❌ UNSUPPORTED'} · {esc(ENGINE['msg'][:200])}\n"
                      f"Running bots: {running} · Python {sys.version.split()[0]} · {sys.platform} · uptime {up}\n"
                      f"DB: {human(DB_PATH.stat().st_size)} · backups: {human(await A(dir_size, BACKUP_DIR))}\n"
                      f"Last user backup: {short_dt(await A(get_setting, 'last_user_backup'))}\n"
                      f"Last platform backup: {short_dt(await A(get_setting, 'last_platform_backup'))}\n"
                      f"Last master backup: {short_dt(await A(get_setting, 'last_master_backup'))}\n"
                      f"Maintenance: {await A(get_setting, 'maintenance', False)}")


# ---------- restore / migration (platform .tghbak = database only; MASTER zip = everything) ----------
@admin_cmd("restore")
async def cmd_restore(update, ctx):
    ctx.user_data["await"] = {"t": "plat_up"}
    await say(update, "🗄 <b>Restore / migration</b>\nUpload now:\n• a <code>.tghbak</code> platform backup (database + keys), or\n"
                      "• a <code>MASTER_BACKUP_….zip</code> (database + ALL user files)\nEverything is decrypted, integrity-checked and inspected first; "
                      "a safety backup is made; on any error the old data is put back.\n"
                      f"⚠️ Telegram bots can download files only up to {human(TG_GET_LIMIT)}. Bigger backups: copy the file to the server and run "
                      "<code>python main.py --restore FILE.zip</code>.\n/cancel to abort.")


async def platform_upload(update: Update, ctx):
    uid = update.effective_user.id
    if not await has_perm(uid, "restore"):
        return await say(update, "⛔ Permission denied.")
    doc = update.message.document
    fn = (doc.file_name or "").lower()
    if not (fn.endswith(".tghbak") or fn.endswith(".zip")):
        return await say(update, "❌ Send a .tghbak or a MASTER_BACKUP .zip file.")
    if (doc.file_size or 0) > TG_GET_LIMIT:
        return await say(update, f"❌ {human(doc.file_size)} is above Telegram's {human(TG_GET_LIMIT)} download limit for bots.\n"
                                 "Copy the file to the server and run: <code>python main.py --restore FILE.zip</code>")
    d = BACKUP_DIR / "platform" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"up_{now():%Y%m%d_%H%M%S}{'.tghbak' if fn.endswith('.tghbak') else '.zip'}"
    f = await doc.get_file()
    await f.download_to_drive(p)
    ctx.user_data.pop("await", None)
    if p.suffix == ".tghbak":
        ok, man, _ = await A(inspect_platform_backup, p)
        if not ok:
            return await say(update, f"❌ {esc(man)}")
        ctx.bot_data["plat_restore"] = str(p)
        c = man["counts"]
        return await say(update, f"🔍 <b>Platform backup valid</b> ✅ (decryption + integrity)\nCreated: {short_dt(man['created_at'])}\n"
                                 f"Users: {c['users']} · Bots: {c['hosted_bots']} · Admins: {c['admins']} · Subscriptions: {c['subscriptions']}\n\n"
                                 "⚠️ This REPLACES the current database (bot files are not touched). A pre-restore backup is made first.",
                         Kb([[Btn("✅ Restore now", callback_data="pr:go"), Btn("❌ Cancel", callback_data="pr:no")]]))
    ok, m = await A(validate_master, p)
    if not ok:
        return await say(update, f"❌ {esc(m)}")
    ctx.bot_data["master_restore"] = str(p)
    await say(update, f"🔍 <b>MASTER backup valid</b> ✅ (integrity + passphrase)\nCreated: {short_dt(m['created_at'])}\n"
                      f"Users: {len(m['users'])} · Bots: {len(m['bots'])} · source: {esc(m.get('platform', '?'))}\n\n"
                      "⚠️ This REPLACES the current database AND all user bot files. A safety MASTER backup of the current system is made first; "
                      "if anything fails everything is rolled back. All bots are stopped during the restore.",
              Kb([[Btn("✅ Restore everything", callback_data="pr:gom"), Btn("❌ Cancel", callback_data="pr:no")]]))


async def cb_admin(update: Update, ctx, p):
    uid = update.callback_query.from_user.id
    k = p[0]
    if k == "adm":
        need = {"dash": "view_stats", "users": "users_view", "bots": "bots_view", "system": "system"}[p[1]]
        if not await has_perm(uid, need):
            return await say(update, "⛔ Permission denied.")
        ctx.args = []
        if p[1] == "dash":
            return await say(update, dash_text(await collect_stats()))
        if p[1] == "users":
            return await cmd_users(update, ctx)
        if p[1] == "bots":
            return await cmd_bots(update, ctx)
        return await cmd_system(update, ctx)
    if k == "pr":
        if not await has_perm(uid, "restore"):
            return await say(update, "⛔ Permission denied.")
        if p[1] == "no":
            ctx.bot_data.pop("plat_restore", None)
            ctx.bot_data.pop("master_restore", None)
            return await edit(update, "Restore cancelled. Nothing was changed.")
        if p[1] == "go":
            path = ctx.bot_data.pop("plat_restore", None)
            if not path:
                return await edit(update, "Session expired.")
            await edit(update, "⏳ Restoring platform database…")
            await A(audit, uid, "platform_restore", Path(path).name)
            STATE["restoring"] = True
            try:
                ok, rep = await A(restore_platform_core, Path(path))
                if ok:
                    rep += ["", "🔁 Reconciling processes…"] + await reconcile_processes()
            finally:
                STATE["restoring"] = False
            return await send_long(uid, "🗄 <b>Platform restoration report</b>\n\n" + "\n".join(esc(x) for x in rep))
        if p[1] == "gom":
            path = ctx.bot_data.pop("master_restore", None)
            if not path:
                return await edit(update, "Session expired.")
            await edit(update, "⏳ Restoring EVERYTHING… (safety backup first, bots are stopped). Please wait.")
            await A(audit, uid, "master_restore", Path(path).name)
            STATE["restoring"] = True
            try:
                ok, rep = await A(restore_master, Path(path))
                if ok:
                    rep += ["", "🔁 Starting bots that were running…"] + await reconcile_processes()
            finally:
                STATE["restoring"] = False
            return await send_long(uid, "🧳 <b>Migration / restore report</b>\n\n" + "\n".join(esc(x) for x in rep))
    act, target = p[1], int(p[2])
    if k == "ab":
        if not await has_perm(uid, "ban_bots"):
            return await say(update, "⛔ Permission denied.")
        return await say(update, await do_ban_bot(uid, target, act == "ban"))
    need = {"ext": "users_manage", "prem": "premium", "unprem": "premium", "ban": "ban_users", "unban": "ban_users",
            "del": "users_manage", "delok": "users_manage", "cancel": "view_stats"}[act]
    if not await has_perm(uid, need):
        return await say(update, "⛔ Permission denied.")
    if act == "ext":
        return await say(update, await do_extend(uid, target, int(p[3])))
    if act == "prem":
        return await say(update, await do_premium(uid, target, int(p[3])))
    if act == "unprem":
        return await say(update, await do_unpremium(uid, target))
    if act in ("ban", "unban"):
        return await say(update, await do_ban_user(uid, target, act == "ban", "banned via button"))
    if act == "del":
        return await say(update, f"⚠️ Delete ALL hosting data of <code>{target}</code>?", Kb([[Btn("✅ Confirm", callback_data=f"au:delok:{target}"), Btn("❌ Cancel", callback_data="au:cancel:0")]]))
    if act == "delok":
        return await say(update, await do_delete_user_data(uid, target))
    if act == "cancel":
        return await edit(update, "Cancelled.")


async def master_backup_job(ctx=None):
    try:
        p = await A(build_master_backup, "auto")
        await A(set_setting, "last_master_backup", iso())
        await A(prune_links)
        log.info("daily master backup: %s", p.name)
    except Exception as e:
        blog.exception("master backup failed")
        await notify_admins(f"❌ <b>Daily MASTER backup failed</b>: {esc(e)}", "backup")


async def stop_flag_job(ctx=None):
    if STOP_FLAG.exists():
        log.info("stop flag found - shutting down")
        STOP_FLAG.unlink(missing_ok=True)
        APP.stop_running()




@admin_cmd("view_stats")
async def cmd_admin(update, ctx):
    await ui_home(update, send=True)



# =====================================================================
# 13c. BUTTON-ONLY ADMIN PANEL (only admins ever see it) + simple per-bot premium & limits
# =====================================================================
UI_AWAITS = {"find_user", "u_days", "msg_user", "bcast", "admin_add", "fs_add", "fs_grace", "gset", "u_trial"}
PLAN_OPTS = {"max_bots": [1, 2, 3, 5, 10, 20, 50], "cpu": [25, 50, 100, 150, 200, 300],
             "ram_mb": [128, 256, 512, 1024, 2048, 4096], "storage_mb": [100, 200, 500, 1000, 2048, 5000],
             "premium_days": [7, 15, 30, 60, 90]}
LBL = {"max_bots": "🤖 Max bots", "cpu": "⚙️ CPU % (100 = 1 core)", "ram_mb": "🧠 RAM MB", "storage_mb": "💽 Storage MB", "premium_days": "📅 Premium default din"}
DAY_OPTS = [7, 15, 30, 60, 90, 180, 365]
S_ICON = {"running": "🟢", "stopped": "🔴", "crashed": "💥", "installing": "⏳", "error": "❗"}


def dleft(s) -> int:
    return max(0, int(-(-(pdt(s) - now()).total_seconds() // 86400)))


def ent_short(pl: dict) -> str:
    n = pl["name"]
    if n == "premium":
        return f"💎{dleft(pl['expires'])}d"
    if n == "trial":
        return f"🎁{dleft(pl['expires'])}d"
    return "🚫" if n == "banned" else "⌛"


def bot_ent(b) -> str:
    return ent_short(bot_plan(b))


def _need(perm):
    async def chk(update) -> bool:
        if await has_perm(update.callback_query.from_user.id, perm):
            return True
        await say(update, "⛔ Aapke paas is kaam ki permission nahi hai.")
        return False
    return chk


def nav(*rows, back="A:home", back_txt="⬅️ Admin Menu"):
    return Kb([list(r) for r in rows] + [[Btn(back_txt, callback_data=back)]])


async def menu_admin(update, ctx):
    if not await A(role_of, update.effective_user.id):
        return
    await ui_home(update, send=True)


async def ui_home(update, send=False):
    s = await collect_stats()
    role = await A(role_of, update.effective_user.id)
    txt = (f"🛡 <b>Admin Panel</b> ({role}) · 🤖 Manager: @{MANAGER['username'] or '?'}\n\n👥 Users: <b>{s['total']}</b> · 💎 Premium {s['premium']} · 🎁 Trial {s['trial']} · ⌛ Khatam {s['expired']} · 🚫 Ban {s['banned']}\n"
           f"🤖 Bots: <b>{s['bots']}</b> · 🟢 chal rahe {s['running']} · 🔴 band {s['stopped']} · 💥 crash {s['crashed']}\n\n"
           "Neeche ke button dabayein. Kisi button ka matlab na pata ho to <b>❓ Madad</b> dabayein.")
    kb = Kb([[Btn("👥 Users (kisne bot host kiya)", callback_data="A:ul:0:a")],
             [Btn("🤖 Sabhi Bots", callback_data="A:bl:0:a"), Btn("💎 Premium / Baaki din", callback_data="A:pe")],
             [Btn("📋 Kaun kya host kar raha hai", callback_data="A:who"), Btn("📊 Dashboard", callback_data="A:dash")],
             [Btn("📢 Sabko Message", callback_data="A:bc")],
             [Btn("📤 Backup · Restore · File bhejna", callback_data="A:bk")],
             [Btn("📣 Channel join zaroori (force-subscribe)", callback_data="A:fs")],
             [Btn("⚙️ Settings (limit, admin)", callback_data="A:set"), Btn("🖥 Server", callback_data="A:sys")],
             [Btn("❓ Madad (button ka matlab)", callback_data="A:help")]])
    if send:
        await say(update, txt, kb)
    else:
        await edit(update, txt, kb)


ADMIN_HELP = (
    "❓ <b>Admin Madad — kaun sa button kya karta hai</b>\n\n"
    "👥 <b>Users</b> – sabhi users ki list. User dabayein to uske bots, bot ID, kitne din baaki, aur uske saare buttons milte hain. "
    "Filter: 💎 Premium, 🎁 Trial, ⌛ Khatam, 🚫 Banned.\n\n"
    "🤖 <b>Sabhi Bots</b> – har bot ID, bot ka @username, owner, status aur premium/trial ke din. Filter 💥 Crash = jo crash ho gaye. Bot dabayein: Start/Stop, Logs, Limit, Premium, Ban.\n\n"
    "📋 <b>Kaun kya host kar raha hai</b> – har user ke neeche uske bots aur bots ke @username ki poori list.\n\n"
    "🔎 <b>Jaanch</b> (bot screen me) – ye bot abhi kya kar raha hai: internet connections, process, shak wala code (mining, secret padhna), aakhri log aur crash ki wajah. Galat lage to 🛑 Stop ya 🚫 Ban.\n\n"
    "💎 <b>Premium / Baaki din</b> – kiska premium kitne din me khatam hoga (sabse pehle jo jaldi khatam ho).\n\n"
    "<b>User screen ke buttons:</b>\n"
    "💎 <i>Premium do</i> – pehle bot chuno (ya Sabhi bots), phir din chuno (7/15/30…). Din badh jaate hain, purane din kharab nahi hote.\n"
    "❌ <i>Premium hatao</i> – ek bot ka ya sabka premium band.\n"
    "🎁 <i>Trial +7/+15/+30</i> – free trial ke din badhao.\n"
    "📤 <i>Backup bhejo</i> – us user ko uska backup bhejo (ek bot / sabhi bots / poora data / chuni files / download link).\n"
    "📎 <i>File bhejo</i> – koi bhi file us user ko bhejo (aap file bhejein, bot aage de dega).\n"
    "✉️ <i>Message</i> – us user ko text message.\n"
    "🚫 <i>Ban / ✅ Unban</i> – user ke saare bots band; unban par wapas.\n\n"
    "<b>Bot screen ke buttons:</b> ▶️⏹🔄 chalao/roko, 📜 Logs, ⚙️ <i>Limit</i> (CPU/RAM/Storage us bot ke liye), "
    "💎 premium din, 📤 backup, ⬇️ files, 🚫 bot ban.\n\n"
    "📤 <b>Backup · Restore</b> – sabko backup bhejo, MASTER backup lo, naye server par restore karo.\n"
    "📢 <b>Sabko Message</b> – saare users ko ek announcement.\n"
    "⚙️ <b>Settings</b> – Free/Premium plan ki limit, maintenance, admin banana/hatana.\n"
    "📣 <b>Channel join zaroori</b> – kaun sa channel join karna zaroori hai: ON/OFF, naya channel jodo ya hatao (ek se zyada), purane users ko kitne ghante ka time, "
    "kisne join nahi kiya. Bot ko channel me ADMIN banana zaroori hai.\n"
    "🎁 <b>Trial ke din</b> – Settings → Trial/Referral me kitne bhi din (✏️ likhkar); kisi ek user ke din User screen ke 🎁 Trial badhao/ghatao se.\n"
    "🖥 <b>Server</b> – CPU/RAM/disk aur hosting sahi chal rahi hai ya nahi.\n\n"
    "<b>Premium kaise dein (sabse aasan):</b> Users → user chuno → 💎 Premium do → bot chuno → din chuno. Bas.\n"
    "<b>Naye server par jaana:</b> Backup · Restore → 'MASTER backup lo' (purane par) → naye par 'Restore' → file bhejo."
)


async def user_screen(uid: int):
    u = await A(db.one, "SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        return "User nahi mila.", nav(back="A:ul:0:a", back_txt="⬅️ Users")
    pl = await A(plan_of, uid)
    bots = await A(db.all, "SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    cpu = sum(STATS.get(b["id"], {}).get("cpu", 0) for b in bots)
    ram = sum(STATS.get(b["id"], {}).get("mem", 0) for b in bots)
    lines = []
    for b in bots:
        lines.append(f"{S_ICON.get(b['status'], '•')} <b>Bot #{b['id']}</b> {esc(b['name'])}" + (f" (@{esc(b['tg_username'])})" if b["tg_username"] else "") + f" — {bot_ent(b)}" + (" 🚫BAN" if b["banned"] else ""))
    ex = f" · {dleft(pl['expires'])} din baaki ({short_dt(pl['expires'])})" if pl["expires"] else ""
    txt = (f"👤 <b>{esc(uname(u))}</b> · ID <code>{uid}</code>\nJoin: {short_dt(u['registered_at'])} · aakhri baar: {short_dt(u['last_seen'])}\n"
           f"Plan: <b>{pl['name']}</b>{ex}{' · 🚫 BANNED: ' + esc(u['ban_reason'] or '-') if u['banned'] else ''}\n"
           f"Bots: {len(bots)} · CPU {cpu:.0f}% · RAM {human(ram)}\n\n" + ("\n".join(lines) or "Is user ne abhi koi bot host nahi kiya."))
    rows = [[Btn("💎 Premium do", callback_data=f"A:pm:{uid}"), Btn("❌ Premium hatao", callback_data=f"A:pr:{uid}")],
            [Btn("🎁 Trial +7", callback_data=f"A:te:{uid}:7"), Btn("+15", callback_data=f"A:te:{uid}:15"), Btn("+30", callback_data=f"A:te:{uid}:30")],
            [Btn("🎁 Trial badhao / ghatao (kitne bhi din)", callback_data=f"A:tx:{uid}")]]
    for b in bots[:10]:
        rows.append([Btn(f"🤖 #{b['id']} {b['name'][:14]}" + (f" @{b['tg_username'][:18]}" if b["tg_username"] else "") + " — kholo", callback_data=f"A:b:{b['id']}")])
    rows += [[Btn("📤 Backup bhejo", callback_data=f"A:sb:{uid}"), Btn("📎 File bhejo", callback_data=f"A:sf:{uid}")],
             [Btn("✉️ Message", callback_data=f"A:mu:{uid}"),
              Btn("✅ Unban" if u["banned"] else "🚫 Ban", callback_data=f"au:{'unban' if u['banned'] else 'ban'}:{uid}")],
             [Btn("🗑 Data delete", callback_data=f"au:del:{uid}")]]
    return txt, nav(*rows, back="A:ul:0:a", back_txt="⬅️ Users")


async def bot_screen(bid: int):
    b = await A(get_bot, bid)
    if not b:
        return "Bot nahi mila.", nav()
    t, _ = await bot_card(b)
    uid = b["user_id"]
    o = await A(db.one, "SELECT username,first_name FROM users WHERE id=?", (uid,))
    rows = [[Btn("▶️ Start", callback_data=f"b:start:{bid}"), Btn("⏹ Stop", callback_data=f"b:stop:{bid}"), Btn("🔄 Restart", callback_data=f"b:restart:{bid}")],
            [Btn("📜 Logs", callback_data=f"b:logs:{bid}"), Btn("📦 Install log", callback_data=f"b:ilog:{bid}"), Btn("🔃 Refresh", callback_data=f"A:b:{bid}")],
            [Btn("💎 Premium", callback_data=f"A:pd:{uid}:{bid}"), Btn("⚙️ Limit (CPU/RAM)", callback_data=f"A:lim:{bid}")],
            [Btn("📤 Backup user ko", callback_data=f"ex:botgo:{bid}"), Btn("⬇️ Files mujhe", callback_data=f"b:dl:{bid}")],
            [Btn("✅ Bot Unban" if b["banned"] else "🚫 Bot Ban", callback_data=f"ab:{'unban' if b['banned'] else 'ban'}:{bid}")]]
    rows.insert(2, [Btn("🔎 Jaanch: ye bot kya kar raha hai?", callback_data=f"A:ins:{bid}")])
    rows.insert(3, [Btn("🔄 Bot ka @username pata karo", callback_data=f"A:idn:{bid}")])
    rows.insert(4, [Btn("🩺 Crash jaanch", callback_data=f"b:diag:{bid}"), Btn("💥 Crash history", callback_data=f"b:crh:{bid}")])
    rows.insert(5, [Btn("📂 User ne kya upload kiya + files dekho", callback_data=f"b:ups:{bid}")])
    return (f"👤 <b>Owner:</b> {esc(o['first_name'] or '') if o else ''} {esc(uname(o) if o else '')} · ID <code>{uid}</code>\n"
            f"🔗 <b>Bot ka Telegram username:</b> {('@' + esc(b['tg_username'])) if b['tg_username'] else 'pata nahi (🔄 dabayein)'}\n\n{t}"), nav(*rows, back=f"A:u:{uid}", back_txt="⬅️ User")


def _users_rows(flt):
    out = []
    for u in db.all("SELECT * FROM users ORDER BY registered_at DESC"):
        pl = plan_of(u["id"])
        cat = "b" if u["banned"] else {"premium": "p", "trial": "t"}.get(pl["name"], "e")
        if flt != "a" and cat != flt:
            continue
        out.append((u, pl, db.val("SELECT COUNT(*) FROM hosted_bots WHERE user_id=?", (u["id"],))))
    return out


def _bots_rows(flt):
    out = []
    for b in db.all("SELECT b.*, u.username, u.first_name FROM hosted_bots b JOIN users u ON u.id=b.user_id ORDER BY b.id DESC"):
        bp = bot_plan(b)
        if flt == "r" and b["status"] != "running":
            continue
        if flt == "s" and b["status"] == "running":
            continue
        if flt == "p" and bp["name"] != "premium":
            continue
        if flt == "c" and b["status"] not in ("crashed", "error"):
            continue
        out.append((b, bp))
    return out


def _pager(items, page, per):
    pages = max(1, -(-len(items) // per))
    page = min(max(page, 0), pages - 1)
    return items[page * per:(page + 1) * per], page, pages


async def show_users(update, page, flt, mode=""):
    rows = await A(_users_rows, flt)
    chunk, page, pages = _pager(rows, page, 8)
    cb = (lambda uid: f"A:sb:{uid}") if mode == "sb" else (lambda uid: f"A:u:{uid}")
    kb = [[Btn(f"{'🚫' if u['banned'] else '👤'} {(u['first_name'] or u['username'] or str(u['id']))[:16]} · {ent_short(pl)} · {nb} bot", callback_data=cb(u["id"]))]
          for u, pl, nb in chunk]
    f = lambda c, t: Btn(("• " if flt == c else "") + t, callback_data=f"A:ul:0:{c}" + (f":{mode}" if mode else ""))
    kb.append([f("a", "Sabhi"), f("p", "💎"), f("t", "🎁"), f("e", "⌛"), f("b", "🚫")])
    if pages > 1:
        nb_ = []
        if page > 0:
            nb_.append(Btn("⬅️", callback_data=f"A:ul:{page - 1}:{flt}" + (f":{mode}" if mode else "")))
        nb_.append(Btn(f"{page + 1}/{pages}", callback_data="A:noop"))
        if page < pages - 1:
            nb_.append(Btn("➡️", callback_data=f"A:ul:{page + 1}:{flt}" + (f":{mode}" if mode else "")))
        kb.append(nb_)
    kb.append([Btn("🔍 User dhundo (naam/ID)", callback_data="A:find")])
    await edit(update, f"👥 <b>Users</b> ({len(rows)})\n💎 premium · 🎁 trial · ⌛ khatam · 🚫 ban — number = baaki din\n"
                       + ("<b>Kisko backup bhejna hai? User chuno.</b>" if mode == "sb" else "User dabayein → uske bots aur buttons."),
               nav(*kb))


async def show_bots(update, page, flt):
    rows = await A(_bots_rows, flt)
    chunk, page, pages = _pager(rows, page, 8)
    kb = [[Btn(f"#{b['id']} {('@' + b['tg_username'])[:16] if b['tg_username'] else b['name'][:12]} · {S_ICON.get(b['status'], '•')} · {ent_short(bp)} · {(b['first_name'] or b['username'] or b['user_id'])!s:.10}", callback_data=f"A:b:{b['id']}")]
          for b, bp in chunk]
    f = lambda c, t: Btn(("• " if flt == c else "") + t, callback_data=f"A:bl:0:{c}")
    kb.append([f("a", "Sabhi"), f("r", "🟢 Chal rahe"), f("s", "🔴 Band"), f("c", "💥 Crash"), f("p", "💎 Premium")])
    if pages > 1:
        nb_ = []
        if page > 0:
            nb_.append(Btn("⬅️", callback_data=f"A:bl:{page - 1}:{flt}"))
        nb_.append(Btn(f"{page + 1}/{pages}", callback_data="A:noop"))
        if page < pages - 1:
            nb_.append(Btn("➡️", callback_data=f"A:bl:{page + 1}:{flt}"))
        kb.append(nb_)
    await edit(update, f"🤖 <b>Sabhi Bots</b> ({len(rows)})\nFormat: #BotID @botusername · status · baaki din · owner\nBot dabayein → control.", nav(*kb))


async def apply_premium(admin: int, uid: int, bid: int, days: int) -> str:
    if bid == 0:
        exp = await A(grant_premium, uid, days, admin)
        what = "aapke sabhi bots"
    else:
        b = await A(get_bot, bid)
        if not b or b["user_id"] != uid:
            return "❌ Bot nahi mila."
        cur = pdt(b["premium_until"]) if b["premium_until"] else now()
        exp = iso(max(cur, now()) + timedelta(days=days))
        await A(set_bot, bid, premium_until=exp)
        what = f"bot #{bid} ({b['name']})"
    await A(audit, admin, "premium_on", uid, f"bot={bid} +{days}d")
    await notify(uid, f"💎 <b>Premium chalu!</b>\n{esc(what)} — <b>{days} din</b> add hue.\nKhatam hone ki date: <b>{short_dt(exp)}</b>. Zyada limit ka maza lein 🎉")
    await after_entitlement_change(uid)
    return f"✅ {what}: +{days} din → {short_dt(exp)} tak ({dleft(exp)} din baaki)."


async def remove_premium(admin: int, uid: int, bid: int) -> str:
    if bid == 0:
        await A(db.ex, "UPDATE subscriptions SET status='cancelled' WHERE user_id=? AND status='active'", (uid,))
        await A(db.ex, "UPDATE hosted_bots SET premium_until=NULL WHERE user_id=?", (uid,))
    else:
        await A(set_bot, bid, premium_until=None)
    await A(audit, admin, "premium_off", uid, f"bot={bid}")
    await notify(uid, "ℹ️ Aapka Premium band kar diya gaya.")
    await after_entitlement_change(uid)
    return "✅ Premium hata diya."


def _pick_bot_kb(uid, prefix, with_all=True, all_txt="💎 Sabhi bots (poora account)"):
    bots = db.all("SELECT * FROM hosted_bots WHERE user_id=? ORDER BY id", (uid,))
    rows = [[Btn(all_txt, callback_data=f"{prefix}:{uid}:0")]] if with_all else []
    rows += [[Btn(f"#{b['id']} {b['name'][:20]} — {bot_ent(b)}", callback_data=f"{prefix}:{uid}:{b['id']}")] for b in bots]
    return rows


async def _after_text(update, text, kb):
    await say(update, text, kb)


async def cb_ui(update: Update, ctx, p):
    uid = update.callback_query.from_user.id
    if not await A(role_of, uid):
        return                                         # normal users never get any admin screen
    act = p[1]
    g = lambda i, d="": p[i] if len(p) > i else d
    if act == "noop":
        return
    if act == "home":
        return await ui_home(update)
    if act == "help":
        return await edit(update, ADMIN_HELP, nav())
    perm = {"ul": "users_view", "u": "users_view", "bl": "bots_view", "b": "bots_view", "pe": "bots_view", "dash": "view_stats",
            "pm": "premium", "pd": "premium", "pk": "premium", "pc": "premium", "pr": "premium", "pz": "premium", "te": "users_manage",
            "lim": "bots_control", "lv": "bots_control", "bk": "backup", "bka": "backup", "bkm": "export", "bkp": "backup", "bkf": "export",
            "bkg": "backup", "who": "bots_view", "ins": "logs", "idn": "bots_view", "rs": "restore", "ru": "restore", "sb": "export", "sf": "message", "mu": "message", "bc": "broadcast",
            "bcg": "broadcast", "set": "settings", "ps": "settings", "pv": "settings", "mt": "maintenance", "ad": "admins", "aa": "admins",
            "ar": "admins", "ax": "admins", "sys": "system", "find": "users_view", "fs": "settings", "fst": "settings", "fsa": "settings", "fsc": "settings",
            "fsx": "settings", "fso": "settings", "fsg": "settings", "fsgc": "settings", "fsr": "settings", "fsl": "settings", "gl": "settings",
            "gs": "settings", "gv": "settings", "gc": "settings", "tx": "users_manage", "tv": "users_manage", "tc": "users_manage"}.get(act)
    if perm and not await has_perm(uid, perm):
        return await say(update, "⛔ Is kaam ki permission aapke role me nahi hai.")
    if act == "dash":
        return await edit(update, dash_text(await collect_stats()), nav([Btn("🔃 Refresh", callback_data="A:dash")]))
    if act == "ul":
        return await show_users(update, int(g(2, "0")), g(3, "a"), g(4))
    if act == "bl":
        return await show_bots(update, int(g(2, "0")), g(3, "a"))
    if act == "u":
        t, kb = await user_screen(int(g(2)))
        return await edit(update, t, kb)
    if act == "b":
        t, kb = await bot_screen(int(g(2)))
        return await edit(update, t, kb)
    if act == "who":
        rows = await A(db.all, "SELECT u.id uid,u.first_name,u.username,u.banned,b.id bid,b.name,b.status,b.tg_username FROM users u JOIN hosted_bots b ON b.user_id=u.id ORDER BY u.id,b.id")
        if not rows:
            return await edit(update, "Abhi kisi ne koi bot host nahi kiya.", nav())
        out, cur = [], None
        for r in rows:
            if r["uid"] != cur:
                cur = r["uid"]
                out.append(f"\n👤 <b>{esc(r['first_name'] or '-')}</b> {('@' + esc(r['username'])) if r['username'] else ''} · <code>{cur}</code>{' 🚫' if r['banned'] else ''}")
            out.append(f"   {S_ICON.get(r['status'], '•')} Bot #{r['bid']} <b>{esc(r['name'])}</b> → " + (f"@{esc(r['tg_username'])}" if r["tg_username"] else "(username pata nahi)"))
        await edit(update, "📋 <b>Kaun kya host kar raha hai</b>", nav(back="A:home"))
        return await send_long(uid, "\n".join(out))
    if act == "ins":
        bid = int(g(2))
        await say(update, "🔎 Jaanch ho rahi hai…")
        rep_ = await A(inspect_bot, bid)
        await A(audit, uid, "inspect_bot", bid)
        return await say(update, f"🔎 <b>Bot #{bid} — abhi kya kar raha hai</b>\n\n" + rep_, nav([Btn("🔃 Dobara jaanch", callback_data=f"A:ins:{bid}")], [Btn("🛑 Bot Stop", callback_data=f"b:stop:{bid}"), Btn("🚫 Bot Ban", callback_data=f"ab:ban:{bid}")], back=f"A:b:{bid}", back_txt="⬅️ Bot"))
    if act == "idn":
        bid = int(g(2))
        res = await A(fetch_identity, bid)
        await say(update, f"🔗 Bot #{bid}: {esc(res)}")
        t, kb = await bot_screen(bid)
        return await edit(update, t, kb)
    if act == "find":
        ctx.user_data["await"] = {"t": "find_user"}
        return await say(update, "🔍 User ka <b>naam, @username ya ID</b> likh kar bhejein. (/cancel se wapas)")
    if act == "pe":
        rows = await A(lambda: db.all("SELECT b.id,b.name,b.user_id,b.premium_until FROM hosted_bots b WHERE b.premium_until>? ORDER BY b.premium_until LIMIT 25", (iso(),)))
        subs = await A(lambda: db.all("SELECT user_id,expires_at FROM subscriptions WHERE status='active' AND expires_at>? ORDER BY expires_at LIMIT 25", (iso(),)))
        trials = await A(lambda: db.all("SELECT user_id,expires_at FROM trials WHERE status='active' AND expires_at>? ORDER BY expires_at LIMIT 10", (iso(),)))
        txt = "💎 <b>Premium / Trial — kitne din baaki</b> (jo jaldi khatam hoga wo upar)\n\n"
        txt += "<b>Bot premium:</b>\n" + ("\n".join(f"#{r['id']} {esc(r['name'])} (user <code>{r['user_id']}</code>) — <b>{dleft(r['premium_until'])} din</b>" for r in rows) or "koi nahi") + "\n\n"
        txt += "<b>Poore account ka premium:</b>\n" + ("\n".join(f"user <code>{r['user_id']}</code> — <b>{dleft(r['expires_at'])} din</b>" for r in subs) or "koi nahi") + "\n\n"
        txt += "<b>Trial jo jaldi khatam honge:</b>\n" + ("\n".join(f"user <code>{r['user_id']}</code> — {dleft(r['expires_at'])} din" for r in trials) or "koi nahi")
        kb = [[Btn(f"#{r['id']} {r['name'][:18]} — {dleft(r['premium_until'])}d", callback_data=f"A:b:{r['id']}")] for r in rows[:10]]
        return await edit(update, txt, nav(*kb))
    # ---- premium ----
    if act in ("pm", "pd") and act == "pm":
        t = int(g(2))
        return await edit(update, "💎 <b>Premium kisko dena hai?</b>\nEk bot chuno, ya sabhi bots (poora account).", nav(*_pick_bot_kb(t, "A:pd"), back=f"A:u:{t}", back_txt="⬅️ User"))
    if act == "pd":
        t, bid = int(g(2)), int(g(3))
        who = "sabhi bots" if bid == 0 else f"bot #{bid}"
        rows = [[Btn(f"{d} din", callback_data=f"A:pk:{t}:{bid}:{d}") for d in DAY_OPTS[i:i + 4]] for i in range(0, len(DAY_OPTS), 4)]
        rows.append([Btn("✏️ Apne din likho", callback_data=f"A:pc:{t}:{bid}")])
        return await edit(update, f"💎 <b>{who}</b> — kitne din ka Premium?\n(Pehle se premium hai to din jud jaate hain.)", nav(*rows, back=f"A:pm:{t}", back_txt="⬅️ Bot chuno"))
    if act == "pk":
        t, bid, d = int(g(2)), int(g(3)), int(g(4))
        res = await apply_premium(uid, t, bid, d)
        txt, kb = await user_screen(t)
        return await edit(update, res + "\n\n" + txt, kb)
    if act == "pc":
        ctx.user_data["await"] = {"t": "u_days", "uid": int(g(2)), "bid": int(g(3))}
        return await say(update, "✏️ Kitne <b>din</b> ka Premium? Sirf number likhein (jaise 45). /cancel")
    if act == "pr":
        t = int(g(2))
        return await edit(update, "❌ <b>Kiska Premium hatana hai?</b>", nav(*_pick_bot_kb(t, "A:pz", True, "❌ Sab (poora account)"), back=f"A:u:{t}", back_txt="⬅️ User"))
    if act == "pz":
        t, bid = int(g(2)), int(g(3))
        res = await remove_premium(uid, t, bid)
        txt, kb = await user_screen(t)
        return await edit(update, res + "\n\n" + txt, kb)
    if act == "te":
        t, d = int(g(2)), int(g(3))
        await say(update, await do_extend(uid, t, d))
        txt, kb = await user_screen(t)
        return await edit(update, txt, kb)
    # ---- limits ----
    if act in ("lim", "lv"):
        bid = int(g(2))
        b = await A(get_bot, bid)
        if not b:
            return await say(update, "Bot nahi mila.")
        if act == "lv":
            k, v = g(3), g(4)
            if k == "reset":
                await A(set_bot, bid, lim_json=None)
            elif k in ("cpu", "ram_mb", "storage_mb"):
                cur = json.loads(b["lim_json"] or "{}")
                cur[k] = int(v)
                await A(set_bot, bid, lim_json=json.dumps(cur))
            await A(audit, uid, "bot_limit", bid, f"{k}={v}")
            b = await A(get_bot, bid)
        bp = await A(bot_plan, b)
        L = bp["limits"]
        txt = (f"⚙️ <b>Bot #{bid} {esc(b['name'])} — Limit</b>\nAbhi: CPU <b>{L['cpu']}%</b> · RAM <b>{L['ram_mb']} MB</b> · Storage <b>{L['storage_mb']} MB</b>\n"
               f"({'is bot ke liye custom limit' if b['lim_json'] else 'plan ki default limit'} · plan: {bp['name']})\n\n"
               "Jo number dabayenge wahi us bot ki limit ban jaati hai (monitor 15 second me lagata hai; pakki OS limit ke liye bot Restart karein).")
        rows = []
        for key in ("cpu", "ram_mb", "storage_mb"):
            rows.append([Btn(("✅" if L[key] == v else "") + str(v), callback_data=f"A:lv:{bid}:{key}:{v}") for v in PLAN_OPTS[key]])
            rows[-1].insert(0, Btn(LBL[key].split(" ")[0] + key.split("_")[0].upper(), callback_data="A:noop"))
        rows.append([Btn("🔄 Plan ki default limit par wapas", callback_data=f"A:lv:{bid}:reset:0"), Btn("🔄 Bot Restart", callback_data=f"b:restart:{bid}")])
        return await edit(update, txt, nav(*rows, back=f"A:b:{bid}", back_txt="⬅️ Bot"))
    # ---- messages / files ----
    if act == "mu":
        ctx.user_data["await"] = {"t": "msg_user", "uid": int(g(2))}
        return await say(update, f"✉️ User <code>{g(2)}</code> ko kya message bhejna hai? Likh kar bhejein. (/cancel)")
    if act == "sf":
        ctx.user_data["await"] = {"t": "send_file", "uid": int(g(2))}
        return await say(update, f"📎 Ab wo <b>file bhejein</b> (Document/File ke roop me) jo user <code>{g(2)}</code> ko deni hai. Caption me kuch likhenge to wo bhi jayega. (/cancel)")
    if act == "sb":
        t = int(g(2))
        u = await A(db.one, "SELECT * FROM users WHERE id=?", (t,))
        return await edit(update, f"📤 <b>{esc(uname(u)) if u else t}</b> ko kya bhejna hai?",
                          nav([Btn("🤖 Ek bot ka backup", callback_data=f"ex:bot:{t}"), Btn("🤖 Sabhi bots", callback_data=f"ex:all:{t}")],
                              [Btn("👤 Poora user data", callback_data=f"ex:full:{t}"), Btn("📄 Chuni hui files", callback_data=f"ex:files:{t}")],
                              [Btn("🔗 Download link banao", callback_data=f"ex:link:{t}"), Btn("📎 Koi bhi file", callback_data=f"A:sf:{t}")],
                              back=f"A:u:{t}", back_txt="⬅️ User"))
    # ---- broadcast ----
    if act == "bc":
        ctx.user_data["await"] = {"t": "bcast"}
        return await say(update, "📢 Saare users ko jaane wala message likh kar bhejein. Pehle preview dikhega, phir aap 'Bhejo' dabayenge. (/cancel)")
    if act == "bcg":
        msg = ctx.bot_data.pop("bc_text", None)
        if g(2) == "no" or not msg:
            return await edit(update, "Message cancel.", nav())
        users = await A(db.all, "SELECT id FROM users WHERE blocked=0")
        await A(audit, uid, "broadcast", "", msg[:100])

        async def run():
            ok = 0
            for u in users:
                ok += await notify(u["id"], f"📢 <b>Announcement</b>\n\n{esc(msg)}")
            await notify(uid, f"📢 Message {ok}/{len(users)} users ko pahunch gaya.")
        spawn(run())
        return await edit(update, f"📢 {len(users)} users ko bheja ja raha hai…", nav())
    # ---- backup / restore menu ----
    if act == "bk":
        last = lambda k: short_dt(get_setting(k))
        txt = ("📤 <b>Backup · Restore · File bhejna</b>\n\n"
               f"Aakhri user-backup: {await A(last, 'last_user_backup')}\nAakhri MASTER: {await A(last, 'last_master_backup')}\n\n"
               "• <b>Sabko backup bhejo</b> – har user ko uske bots ka backup Telegram par mil jaata hai (bot band karne se pehle ye dabayein).\n"
               "• <b>Ek user ko</b> – user chuno, phir kya bhejna hai chuno.\n"
               "• <b>MASTER backup</b> – sabhi users ka data ek zip me (naye server par jaane ke liye).\n"
               "• <b>Restore</b> – naye server par MASTER/DB file bhejein, sab wapas.\n"
               "• <b>Ek user ka backup zip restore</b> – user ne jo zip diya wo upload karein.")
        return await edit(update, txt, nav([Btn("📤 Sabhi users ko unka backup bhejo", callback_data="A:bka")],
                                           [Btn("👤 Ek user ko bhejo", callback_data="A:ul:0:a:sb")],
                                           [Btn("🧳 MASTER backup (sabka) mujhe bhejo", callback_data="A:bkm")],
                                           [Btn("🗄 Chhota DB backup mujhe bhejo", callback_data="A:bkp")],
                                           [Btn("📂 Server par rakhe MASTER backups", callback_data="A:bkf")],
                                           [Btn("♻️ Restore: MASTER / DB file upload", callback_data="A:rs")],
                                           [Btn("♻️ Ek user ka backup zip restore", callback_data="A:ru")]))
    if act == "bka":
        n = await A(db.val, "SELECT COUNT(DISTINCT user_id) FROM hosted_bots")
        return await edit(update, f"📤 <b>{n} users</b> ko unka backup bheja jayega (bots chalte rahenge). Pakka?",
                          nav([Btn("✅ Haan, sabko bhejo", callback_data="A:bkg"), Btn("❌ Nahi", callback_data="A:bk")], back="A:bk", back_txt="⬅️ Wapas"))
    if act == "bkg":
        await A(audit, uid, "backup_users")
        spawn(backup_job(manual_by=uid))
        return await edit(update, "⏳ Backup shuru. Khatam hone par aapko report aayegi aur har user ko file milegi.", nav(back="A:bk", back_txt="⬅️ Backup menu"))
    if act == "bkm":
        await edit(update, "⏳ MASTER backup ban raha hai… (bots chalte rahenge)", nav(back="A:bk", back_txt="⬅️ Backup menu"))
        try:
            pth = await A(build_master_backup, "manual")
        except Exception as e:
            return await say(update, f"❌ Backup fail: {esc(e)}")
        await A(set_setting, "last_master_backup", iso())
        await A(audit, uid, "export_master")
        await say(update, f"✅ <b>{esc(pth.name)}</b> ({human(pth.stat().st_size)})\nServer par yahan bhi hai: <code>{esc(pth)}</code>\n"
                          "🔑 Restore ke liye .env ka BACKUP_PASSPHRASE chahiye hoga — use alag safe jagah rakhein.")
        return await _deliver(uid, pth, f"🧳 <b>{esc(pth.name)}</b>")
    if act == "bkp":
        pth = await A(build_platform_backup, "manual")
        await A(set_setting, "last_platform_backup", iso())
        return await _deliver(uid, pth, f"🗄 DB backup (encrypted) {short_dt(iso())}")
    if act == "bkf":
        files = sorted((BACKUP_DIR / "master").glob("MASTER_BACKUP_*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[:6]
        ctx.bot_data["bkf"] = [str(f) for f in files]
        if not files:
            return await edit(update, "Server par abhi koi MASTER backup nahi hai (roz 03:30 par apne aap banta hai).", nav(back="A:bk", back_txt="⬅️ Backup menu"))
        return await edit(update, "📂 <b>Server par rakhe MASTER backups</b> — dabayein to aapko bhej diya jayega:",
                          nav(*[[Btn(f"{f.name[14:33]} · {human(f.stat().st_size)}", callback_data=f"A:bkd:{i}")] for i, f in enumerate(files)], back="A:bk", back_txt="⬅️ Backup menu"))
    if act == "bkd":
        lst = ctx.bot_data.get("bkf") or []
        i = int(g(2))
        if i >= len(lst) or not Path(lst[i]).exists():
            return await say(update, "File nahi mili, list dobara kholein.")
        return await _deliver(uid, Path(lst[i]), "🧳 MASTER backup")
    if act == "rs":
        ctx.user_data["await"] = {"t": "plat_up"}
        return await say(update, "♻️ <b>Restore</b>\nAb <code>MASTER_BACKUP_….zip</code> ya <code>.tghbak</code> file bhejein. Pehle jaanch hogi, phir aapse haan poochha jayega.\n"
                                 f"⚠️ Telegram se {human(TG_GET_LIMIT)} se badi file nahi aati; badi file ke liye server par: <code>python main.py --restore FILE.zip</code>\n/cancel")
    if act == "ru":
        ctx.user_data["await"] = {"t": "admin_zip"}
        return await say(update, "♻️ Us <b>user ka backup zip</b> bhejein (jo user ko 12 ghante wale backup me mila tha). Bot us user ke naam par restore kar dega. /cancel")
    # ---- settings ----
    if act == "set":
        m_on = await A(get_setting, "maintenance", False)
        return await edit(update, "⚙️ <b>Settings</b>\nFree/Premium plan ki limit badalne se us plan ke sabhi bots par asar hota hai.",
                          nav([Btn("🎁 Free/Trial plan limit", callback_data="A:ps:free"), Btn("💎 Premium plan limit", callback_data="A:ps:premium")],
                              [Btn(f"🛠 Maintenance: {'ON ✅' if m_on else 'OFF'} (badalne ke liye dabayein)", callback_data="A:mt")],
                              [Btn("🎁 Trial / 🤝 Referral", callback_data="A:gl")],
                              [Btn("👮 Admins (banana/hatana)", callback_data="A:ad")]))
    if act in ("ps", "pv"):
        plan = g(2)
        if plan not in DEFAULT_PLANS:
            return
        if act == "pv":
            pls = get_plans()
            pls[plan][g(3)] = int(g(4))
            await A(set_setting, "plans", pls)
            await A(audit, uid, "setplan", plan, f"{g(3)}={g(4)}")
        L = get_plans()[plan]
        keys = [k for k in PLAN_OPTS if k != "premium_days" or plan == "premium"]
        rows = []
        for k in keys:
            rows.append([Btn(LBL[k][:14], callback_data="A:noop")])
            rows.append([Btn(("✅" if L.get(k) == v else "") + str(v), callback_data=f"A:pv:{plan}:{k}:{v}") for v in PLAN_OPTS[k]])
        return await edit(update, f"⚙️ <b>{plan.title()} plan</b>\nAbhi: " + " · ".join(f"{LBL[k].split(' ')[0]} {L.get(k)}" for k in keys), nav(*rows, back="A:set", back_txt="⬅️ Settings"))
    if act in ("fs", "fst", "fsg", "fsx", "fso"):
        if act == "fst":
            cur = await A(get_setting, "fs_enabled", True)
            await A(set_setting, "fs_enabled", not cur)
            await A(audit, uid, "fs_toggle", str(not cur))
        if act == "fsg":
            await A(set_setting, "fs_grace_hours", max(1, min(720, await A(fs_grace_hours) + int(g(2)))))
            await A(audit, uid, "fs_grace", g(2))
        if act == "fsx":
            c = await A(db.one, "SELECT * FROM fs_channels WHERE id=?", (int(g(2)),))
            if not c:
                return await edit(update, "Channel nahi mila.", nav(back="A:fs", back_txt="⬅️ Channels"))
            return await edit(update, f"❌ Channel <b>{esc(c['title'] or c['chat_ref'])}</b> hatana hai?\nIske baad ye channel join karna zaroori nahi rahega.",
                              Kb([[Btn("✅ Haan, hatao", callback_data=f"A:fso:{c['id']}"), Btn("⬅️ Nahi", callback_data="A:fs")]]))
        if act == "fso":
            await A(db.ex, "DELETE FROM fs_channels WHERE id=?", (int(g(2)),))
            FS_CACHE.clear()
            await A(audit, uid, "fs_remove", g(2))
        tt, kb = await fs_screen()
        return await edit(update, tt, kb)
    if act == "fsa":
        ctx.user_data["await"] = {"t": "fs_add"}
        return await say(update, "➕ <b>Channel jodo</b>\n\nChannel ka <b>@username</b> ya link bhejein (jaise <code>@techplanettips</code> ya <code>https://t.me/techplanettips</code>).\n"
                                 "Private channel ho to: <code>-100123456789 https://t.me/+invitelink</code>\n\n"
                                 "⚠️ Pehle bot ko us channel me <b>Admin</b> banayein. /cancel")
    if act == "fsgc":
        ctx.user_data["await"] = {"t": "fs_grace"}
        return await say(update, "✏️ Purane users ko join karne ke liye <b>kitne ghante</b> dene hain? Number likhein (1 se 720). /cancel")
    if act == "fsc":
        c = await A(db.one, "SELECT * FROM fs_channels WHERE id=?", (int(g(2)),))
        if not c:
            return await say(update, "Channel nahi mila.")
        return await say(update, await fs_test(c, uid), nav(back="A:fs", back_txt="⬅️ Channels"))
    if act == "fsr":
        r = await fs_enforce(manual=True)
        return await say(update, f"🔄 <b>Check poora</b>\n👥 Check kiye: {r['checked']} · ❌ join nahi kiya: {r['missing']}\n⚠️ Nayi warning: {r['warned']} · ⛔ Bots band: {r['stopped']} · ✅ Wapas chalu: {r['resumed']}",
                         nav(back="A:fs", back_txt="⬅️ Channels"))
    if act == "fsl":
        rows = await A(db.all, "SELECT f.*, u.first_name, u.username, (SELECT COUNT(*) FROM hosted_bots b WHERE b.user_id=f.user_id) AS nb "
                               "FROM fs_state f LEFT JOIN users u ON u.id=f.user_id ORDER BY f.first_warned_at DESC LIMIT 30")
        lines = [f"• <b>{esc((r['first_name'] or '-')[:18])}</b> {('@' + esc(r['username'])) if r['username'] else ''} (<code>{r['user_id']}</code>) · 🤖 {r['nb']} · "
                 + ("⛔ bots band" if r["stopped_at"] else f"⚠️ warning {short_dt(r['first_warned_at'])}") for r in rows]
        return await say(update, "👥 <b>Jinhone channel join nahi kiya</b>\n\n" + ("\n".join(lines) or "Koi nahi ✅"), nav(back="A:fs", back_txt="⬅️ Channels"))
    if act in ("gl", "gs", "gv", "gc"):
        if act == "gs":
            await A(step_global, g(2), int(g(3)))
            await A(audit, uid, "setting", g(2), g(3))
        if act == "gv":
            await A(set_global_value, g(2), int(g(3)))
            await A(audit, uid, "setting", g(2), g(3))
        if act == "gc":
            ctx.user_data["await"] = {"t": "gset", "key": g(2)}
            lo, hi = GLOBAL_RANGE[g(2)]
            return await say(update, f"✏️ Naya number likhein ({lo} se {hi}). /cancel")
        vals = {k: await A(get_global, k) for k, *_ in GLOBAL_KEYS}
        rows = [[Btn("➖", callback_data=f"A:gs:{k}:-1"), Btn(f"{label}: {vals[k]}", callback_data="A:noop"), Btn("➕", callback_data=f"A:gs:{k}:1"),
                 Btn("✏️", callback_data=f"A:gc:{k}")] for k, label, _, _ in GLOBAL_KEYS]
        rows.append([Btn("🎁 Trial ke din seedha chuno:", callback_data="A:noop")])
        for chunk in ((1, 2, 3, 5), (7, 10, 14, 30)):
            rows.append([Btn(("✔" if vals["trial_days"] == n else "") + f"{n}d", callback_data=f"A:gv:trial_days:{n}") for n in chunk])
        return await edit(update, "🎁 <b>Trial aur Referral</b>\n\n🎁 <b>Free trial</b>: naye user ko kitne din free hosting (kitne bhi: 1 din, 2, 5, 100 …). Sirf <b>naye</b> users par lagta hai.\n"
                                  "🤝 <b>Referral bonus</b>: referral se kitne din milte hain (0 = band).\n🤝 <b>Max referrals</b>: ek user kitne referral se din kama sakta hai.\n\n"
                                  "➖ / ➕ dabayein, seedha din chuno, ya ✏️ dabakar koi bhi number likhein.", nav(*rows, back="A:set", back_txt="⬅️ Settings"))
    if act in ("tx", "tv", "tc"):
        t = int(g(2))
        if act == "tc":
            ctx.user_data["await"] = {"t": "u_trial", "uid": t}
            return await say(update, "✏️ Kitne din? Badhane ke liye <code>10</code>, ghatane ke liye <code>-3</code> likhein. (/cancel)")
        if act == "tv":
            await say(update, await do_trial_adjust(uid, t, int(g(3))))
            tt, kb = await user_screen(t)
            return await edit(update, tt, kb)
        rows = [[Btn(f"+{d}", callback_data=f"A:tv:{t}:{d}") for d in (1, 2, 3, 5)], [Btn(f"+{d}", callback_data=f"A:tv:{t}:{d}") for d in (7, 14, 30, 60)],
                [Btn(f"-{d}", callback_data=f"A:tv:{t}:-{d}") for d in (1, 2, 3, 7)], [Btn("✏️ Koi bhi number likhein", callback_data=f"A:tc:{t}")]]
        return await edit(update, f"🎁 <b>Free trial ke din</b> (user <code>{t}</code>)\n➕ badhao  ·  ➖ ghatao (ghatane se trial khatam bhi ho sakta hai).", nav(*rows, back=f"A:u:{t}", back_txt="⬅️ User"))
    if act == "mt":
        cur = await A(get_setting, "maintenance", False)
        await A(set_setting, "maintenance", not cur)
        await A(audit, uid, "maintenance", str(not cur))
        p[1] = "set"
        return await cb_ui(update, ctx, p)
    if act == "ad":
        rows = await A(db.all, "SELECT * FROM admins ORDER BY role,user_id")
        kb = [[Btn(f"{r['user_id']} — {r['role']}" + (" ❌ hatao" if r["user_id"] != OWNER_ID else " (owner)"), callback_data=f"A:ax:{r['user_id']}" if r["user_id"] != OWNER_ID else "A:noop")] for r in rows]
        kb.append([Btn("➕ Naya admin", callback_data="A:aa")])
        return await edit(update, "👮 <b>Admins</b>\nRole: owner (sab), super, technical (bots/backup/server), support (users/message).", nav(*kb, back="A:set", back_txt="⬅️ Settings"))
    if act == "aa":
        ctx.user_data["await"] = {"t": "admin_add"}
        return await say(update, "➕ Naye admin ka <b>Telegram numeric ID</b> bhejein. (/cancel)")
    if act == "ar":
        t, role = int(g(2)), g(3)
        await A(db.ex, "INSERT INTO admins(user_id,role,added_by,added_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role", (t, role, uid, iso()))
        await A(audit, uid, "addadmin", t, role)
        await notify(t, f"🛡 Aap ab admin hain (<b>{role}</b>). Menu me '🛡 Admin Panel' dabayein.", markup=MENU_KB_ADMIN)
        return await edit(update, f"✅ {t} ab {role} hai.", nav(back="A:ad", back_txt="⬅️ Admins"))
    if act == "ax":
        t = int(g(2))
        if t != OWNER_ID:
            await A(db.ex, "DELETE FROM admins WHERE user_id=?", (t,))
            await A(audit, uid, "removeadmin", t)
        p[1] = "ad"
        return await cb_ui(update, ctx, p)
    if act == "sys":
        ctx.args = []
        return await cmd_system(update, ctx)


async def ui_text(update, ctx, aw, text: str):
    uid = update.effective_user.id
    if not await A(role_of, uid):
        ctx.user_data.pop("await", None)
        return
    t = aw["t"]
    ctx.user_data.pop("await", None)
    if t == "find_user":
        q = text.strip().lstrip("@")
        if q.isdigit():
            tt, kb = await user_screen(int(q))
            return await say(update, tt, kb)
        rows = await A(db.all, "SELECT id,first_name,username FROM users WHERE username LIKE ? OR first_name LIKE ? ORDER BY registered_at DESC LIMIT 12", (f"%{q}%", f"%{q}%"))
        if not rows:
            return await say(update, "Koi user nahi mila.", nav(back="A:ul:0:a", back_txt="⬅️ Users"))
        return await say(update, f"🔍 {len(rows)} user mile:", nav(*[[Btn(f"👤 {(r['first_name'] or '')[:16]} @{r['username'] or '-'} ({r['id']})", callback_data=f"A:u:{r['id']}")] for r in rows], back="A:ul:0:a", back_txt="⬅️ Users"))
    if t == "u_days":
        d = parse_int(text)
        if not d or not (0 < d <= 3650):
            return await say(update, "❌ 1 se 3650 ke beech number likhein. Dobara: Premium do se shuru karein.")
        if not await has_perm(uid, "premium"):
            return await say(update, "⛔ Permission nahi.")
        res = await apply_premium(uid, aw["uid"], aw["bid"], d)
        tt, kb = await user_screen(aw["uid"])
        return await say(update, res + "\n\n" + tt, kb)
    if t == "fs_add":
        if not await has_perm(uid, "settings"):
            return
        ref, link, err = fs_parse_ref(text)
        if err:
            return await say(update, f"❌ {esc(err)}", nav(back="A:fs", back_txt="⬅️ Channels"))
        ok, msg = await fs_add_channel(ref, link, uid)
        return await say(update, msg, nav(back="A:fs", back_txt="⬅️ Channels"))
    if t == "fs_grace":
        n = parse_int(text)
        if not n or not (1 <= n <= 720):
            return await say(update, "❌ 1 se 720 ke beech number likhein.", nav(back="A:fs", back_txt="⬅️ Channels"))
        await A(set_setting, "fs_grace_hours", n)
        await A(audit, uid, "fs_grace", str(n))
        tt, kb = await fs_screen()
        return await say(update, tt, kb)
    if t == "gset":
        if not await has_perm(uid, "settings"):
            return
        n = parse_int(text)
        lo, hi = GLOBAL_RANGE[aw["key"]]
        if n is None or not (lo <= n <= hi):
            return await say(update, f"❌ {lo} se {hi} ke beech number likhein.", nav(back="A:gl", back_txt="⬅️ Trial/Referral"))
        await A(set_global_value, aw["key"], n)
        await A(audit, uid, "setting", aw["key"], str(n))
        return await say(update, f"✅ Set ho gaya: <b>{n}</b>", nav([Btn("⚙️ Trial/Referral screen", callback_data="A:gl")], back="A:set", back_txt="⬅️ Settings"))
    if t == "u_trial":
        if not await has_perm(uid, "users_manage"):
            return
        n = parse_int(text)
        if not n or not (-3650 <= n <= 3650):
            return await say(update, "❌ -3650 se 3650 ke beech number likhein (0 nahi).")
        res = await do_trial_adjust(uid, aw["uid"], n)
        tt, kb = await user_screen(aw["uid"])
        return await say(update, res + "\n\n" + tt, kb)
    if t == "msg_user":
        ok = await notify(aw["uid"], f"💬 <b>Admin ka message</b>\n\n{esc(text)}",
                          markup=Kb([[Btn("📞 Jawab do", callback_data="menu:contact")]]))
        await A(audit, uid, "msg", aw["uid"])
        return await say(update, "✅ Bhej diya." if ok else "❌ Pahunch nahi paya (user ne bot block kiya?).", nav(back=f"A:u:{aw['uid']}", back_txt="⬅️ User"))
    if t == "bcast":
        ctx.bot_data["bc_text"] = text
        n = await A(db.val, "SELECT COUNT(*) FROM users WHERE blocked=0")
        return await say(update, f"📢 <b>Preview</b> ({n} users ko jayega):\n\n{esc(text)}",
                         Kb([[Btn("✅ Bhejo", callback_data="A:bcg:go"), Btn("❌ Cancel", callback_data="A:bcg:no")]]))
    if t == "admin_add":
        tid = parse_int(text)
        if not tid:
            return await say(update, "❌ Sirf number (Telegram ID) likhein.")
        return await say(update, f"ID <code>{tid}</code> ko kaunsa role dein?",
                         Kb([[Btn("super", callback_data=f"A:ar:{tid}:super"), Btn("technical", callback_data=f"A:ar:{tid}:technical"), Btn("support", callback_data=f"A:ar:{tid}:support")]]))


async def ui_doc(update, ctx, aw):
    uid = update.effective_user.id
    doc = update.message.document
    t = aw["t"]
    if t == "send_file":
        if not await has_perm(uid, "message"):
            return await say(update, "⛔ Permission nahi.")
        ctx.user_data.pop("await", None)
        cap = (update.message.caption or "")[:900]
        try:
            await APP.bot.send_document(aw["uid"], doc.file_id, caption=("📎 <b>Admin ne file bheji hai</b>\n" + esc(cap)), parse_mode=ParseMode.HTML)
            await A(audit, uid, "send_file", aw["uid"], doc.file_name or "")
            return await say(update, f"✅ File user <code>{aw['uid']}</code> ko bhej di.", nav(back=f"A:u:{aw['uid']}", back_txt="⬅️ User"))
        except TelegramError as e:
            return await say(update, f"❌ Nahi gayi: {esc(e)}")
    if t == "admin_zip":
        if not await has_perm(uid, "restore"):
            return await say(update, "⛔ Permission nahi.")
        if not (doc.file_name or "").lower().endswith(".zip") or (doc.file_size or 0) > TG_GET_LIMIT:
            return await say(update, f"❌ .zip file bhejein ({human(TG_GET_LIMIT)} tak). Badi ho to server par --restore use karein.")
        ctx.user_data.pop("await", None)
        d = BACKUP_DIR / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        pth = d / f"adminzip_{now():%Y%m%d_%H%M%S}.zip"
        f = await doc.get_file()
        await f.download_to_drive(pth)
        ok, man = await A(validate_user_zip, pth, 0, True)
        if not ok:
            return await say(update, f"❌ {esc(man)}")
        tuid = man["user_id"]
        if not await A(db.one, "SELECT 1 FROM users WHERE id=?", (tuid,)):
            await A(register_user, tuid, None, None)
        await say(update, f"⏳ User <code>{tuid}</code> ({len(man['bots'])} bots) restore ho raha hai…")
        rep = await restore_user_backup(tuid, pth, True, man)
        await A(audit, uid, "restore_user_zip", tuid)
        return await send_long(uid, "♻️ <b>Restore report</b>\n\n" + "\n".join(esc(x) for x in rep))


PROMO_VARIANTS = [
    "🚀 <b>Premium Hosting lijiye!</b>\n\n{left}\nAapke bots: {bots}\n\n✅ Zyada CPU aur RAM\n✅ Zyada bots\n✅ Zyada storage\n✅ Priority support\n\nPremium ke liye neeche 📞 Contact Admin dabayein.",
    "⏳ <b>Yaad dilana:</b> {left}\nBots: {bots}\n\nTrial ke baad bots band ho jaate hain, lekin aapki files aur backup safe rehti hain. "
    "Bot chalte rehne ke liye Premium lein — Admin se baat karein.",
    "💎 <b>Aapka bot 24x7 chalta rahe!</b>\n{left}\nBots: {bots}\n\nPremium me aapke bot ko zyada resources milte hain aur crash par auto-restart bhi. Admin ko message bhejein 👇",
]


async def promo_job(ctx=None):
    today = datetime.now(TZ)
    users = await A(db.all, "SELECT id FROM users WHERE blocked=0 AND notify_promo=1 AND banned=0")
    kb = Kb([[Btn("📞 Contact Admin", callback_data="menu:contact"), Btn("🔕 Band karo", callback_data="np:promo")]])
    for u in users:
        pl = await A(plan_of, u["id"])
        if not pl or pl["name"] not in ("trial", "expired"):
            continue
        bots = await A(db.all, "SELECT id,name FROM hosted_bots WHERE user_id=? ORDER BY id", (u["id"],))
        left = (f"🎁 Aapke Free Trial me <b>{dleft(pl['expires'])} din</b> baaki hain." if pl["name"] == "trial"
                else "⌛ Aapka Free Trial khatam ho gaya hai — bots abhi band hain.")
        txt = PROMO_VARIANTS[(today.toordinal() + u["id"]) % len(PROMO_VARIANTS)].format(
            left=left, bots=", ".join(f"#{b['id']} {esc(b['name'])}" for b in bots) or "abhi koi nahi")
        await notify(u["id"], txt, kind="promo", day=today.strftime("%Y-%m-%d"), pref="notify_promo", markup=kb)




# =====================================================================
# 14. MAIN / COMMAND LINE
# =====================================================================
NEW_PASS = {"v": False}


async def post_init(app: Application):
    global APP
    APP = app
    await app.bot.set_my_commands([("start", "Open menu"), ("help", "Help"), ("cancel", "Cancel current action")])
    try:
        me = await app.bot.get_me()
        MANAGER["username"] = me.username
        log.info("Manager bot username: @%s", me.username)
    except Exception:
        log.exception("get_me failed")
    ok, msg = await A(run_engine_check)
    if not ok:
        log.error(msg)
        await notify_admins("❌ <b>This environment cannot host bots</b>\n<pre>" + esc(msg) + "</pre>\nHosting is DISABLED. Uploads are refused until /selfcheck passes.", "system")
    else:
        log.info("engine check: %s", msg)
        rep = await reconcile_processes()
        log.info("startup reconcile: %s", rep)
    if NEW_PASS["v"] and OWNER_ID:
        await notify(OWNER_ID, "🔑 <b>A new BACKUP_PASSPHRASE was created</b> and saved in the <code>.env</code> file next to main.py.\n"
                               "Copy that value to a safe place OUTSIDE this computer: backups cannot be restored on a new server without it.")
    for msg2 in (start_link_server(),):
        log.info(msg2)
    jq = app.job_queue
    last = await A(get_setting, "last_user_backup")
    first = 120 if not last else max(60, BACKUP_HOURS * 3600 - (now() - pdt(last)).total_seconds())      # survives restarts
    jq.run_repeating(monitor_cycle, interval=SAMPLE_SEC, first=20, name="monitor")
    jq.run_repeating(expiry_job, interval=300, first=30, name="expiry")
    jq.run_repeating(fs_job, interval=600, first=150, name="force_sub")
    jq.run_repeating(backup_job, interval=BACKUP_HOURS * 3600, first=first, name="user_backup")
    jq.run_repeating(platform_backup_job, interval=BACKUP_HOURS * 3600, first=first + 900, name="platform_backup")
    jq.run_repeating(stop_flag_job, interval=3, first=3, name="stopflag")
    jq.run_daily(master_backup_job, time=dtime(3, 30, tzinfo=TZ), name="master_backup")
    jq.run_daily(daily_report_job, time=dtime(REPORT_HOUR, 0, tzinfo=TZ), name="daily_report")
    jq.run_daily(promo_job, time=dtime(PROMO_HOUR, 0, tzinfo=TZ), name="promo")
    jq.run_daily(prune_job, time=dtime(3, 0, tzinfo=TZ), name="prune")
    jq.run_once(daily_report_catchup, 45)
    jq.run_once(promo_catchup, 90)
    psutil.cpu_percent(interval=None)
    log.info("Hosting Manager started. Reports %02d:00 %s, backups every %gh", REPORT_HOUR, TZ, BACKUP_HOURS)


async def post_shutdown(app: Application):
    n = await A(shutdown_bots)
    log.info("Manager stopped; %s hosted bot(s) stopped (they start again with the manager).", n)
    srv = LINK_SERVER.get("srv")
    if srv:
        srv.shutdown()
    PID_FILE.unlink(missing_ok=True)


def _prepare():
    for d in (STORAGE, BACKUP_DIR, LOG_DIR, TMP_DIR, DATA):
        d.mkdir(parents=True, exist_ok=True)
    setup_logging()


def _other_manager_running() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        p = psutil.Process(pid)
        if p.is_running() and p.status() != psutil.STATUS_ZOMBIE and any("main.py" in c for c in p.cmdline()):
            return pid
    except psutil.Error:
        pass
    return None


def cli(argv: list[str]) -> bool:
    """Returns True when a CLI command was handled."""
    if not argv:
        return False
    a = argv[0]
    if a in ("-h", "--help"):
        print(__doc__)
        return True
    if a == "--check":
        _prepare()
        ok, msg = run_engine_check()
        print(("SUPPORTED: " if ok else "") + msg)
        sys.exit(0 if ok else 2)
    if a == "--stop":
        pid = _other_manager_running()
        DATA.mkdir(parents=True, exist_ok=True)
        STOP_FLAG.write_text("stop")
        if not pid:
            print("No running manager found (stop flag set, so the start.bat auto-restart loop ends).")
            return True
        for _ in range(60):
            if not _other_manager_running():
                print("Manager stopped.")
                return True
            time.sleep(1)
        print("Manager did not stop in 60 s - forcing.")
        try:
            kill_tree(pid, None, 3.0)
        except Exception as e:
            print("failed:", e)
        STOP_FLAG.unlink(missing_ok=True)
        return True
    if a in ("--export", "--restore"):
        _prepare()
        ensure_passphrase()
        startup_recovery()
        if a == "--export":
            p = build_master_backup("manual")
            print("MASTER backup written:", p)
            return True
        if len(argv) < 2 or not Path(argv[1]).is_file():
            sys.exit("Usage: python main.py --restore FILE.zip|FILE.tghbak")
        if _other_manager_running():
            sys.exit("Stop the running manager first (stop.bat or: python main.py --stop).")
        f = Path(argv[1]).resolve()
        run_engine_check()
        if f.suffix.lower() == ".tghbak":
            ok, rep = restore_platform_core(f)
        else:
            ok, rep = restore_master(f)
        print("\n".join(rep))
        sys.exit(0 if ok else 1)
    return False


def check_token_online(token: str):
    """(True, username) token OK · (False, why) Telegram rejected it · (None, why) could not reach Telegram (no internet?)."""
    import urllib.request, urllib.error
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=12) as r:
            return True, json.loads(r.read().decode())["result"].get("username", "?")
    except urllib.error.HTTPError as e:
        return False, f"Telegram ne token reject kiya (code {e.code})"
    except Exception as e:
        return None, type(e).__name__


def startup_config_check() -> None:
    problems = []
    if not BOT_TOKEN or BOT_TOKEN.startswith("PASTE_YOUR"):
        problems.append("❌ BOT_TOKEN abhi bhara nahi hai.\n   👉 main.py kholein → upar 'USER CONFIG' box me  SET_BOT_TOKEN = \"...\"  me @BotFather wala token daalein.")
    elif not TOKEN_FORMAT.match(BOT_TOKEN):
        problems.append("❌ BOT_TOKEN ka format galat hai (poora copy nahi hua ya extra space/akshar hai).\n   Sahi token aisa dikhta hai:  123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
    if not OWNER_ID:
        problems.append("❌ OWNER_ID bhara nahi hai.\n   👉 USER CONFIG box me  SET_OWNER_ID = 123456789  (apna Telegram numeric ID; @userinfobot se milta hai).")
    if problems:
        print("\n" + "\n\n".join(problems) + "\n\nSahi karke dobara chalayein:  python main.py")
        sys.exit(2)
    ok, info = check_token_online(BOT_TOKEN)
    if ok is False:
        print(f"\n❌ {info}.\n   Token galat ya revoke ho chuka hai. @BotFather → /mybots → apna bot → API Token se naya token lekar SET_BOT_TOKEN me daalein.")
        sys.exit(2)
    if ok is None:
        print(f"⚠️ Telegram se abhi connect nahi ho paya ({info}). Internet check karein — phir bhi shuru kar raha hoon.")
    else:
        print(f"✅ Token sahi hai — Manager bot: @{info}  |  Owner ID: {OWNER_ID}")
    old = sorted({f"{k} ← {v[4:]}" for k, v in CONFIG_SOURCES.items() if v.startswith("old:")})
    if old:
        print("ℹ️ Nayi file ke box me value khaali thi, isliye purani file se li gayi: " + ", ".join(old) +
              "\n   (Chaho to nayi file ke USER CONFIG box me bhar do; box ki value hamesha pehle maani jati hai.)")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if cli(sys.argv[1:]):
        return
    startup_config_check()
    _prepare()
    other = _other_manager_running()
    if other:
        print(f"Manager already running (PID {other}). Pehle stop.bat chalayein.")
        sys.exit(2)
    PID_FILE.write_text(str(os.getpid()))
    STOP_FLAG.unlink(missing_ok=True)
    NEW_PASS["v"] = ensure_passphrase()
    startup_recovery()
    app = (Application.builder().token(BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(TypeHandler(Update, precheck), group=-1)
    cmds = {"start": cmd_start, "help": menu_help, "cancel": cmd_cancel, "admin": cmd_admin, "stats": cmd_stats, "report": cmd_report,
            "users": cmd_users, "user": cmd_user, "bots": cmd_bots, "bot": cmd_bot, "running": cmd_running, "stopped": cmd_stopped,
            "startbot": bot_admin_cmd("start"), "stopbot": bot_admin_cmd("stop"), "restartbot": bot_admin_cmd("restart"),
            "broadcast": cmd_broadcast, "msg": cmd_msg, "maintenance": cmd_maint, "extend": cmd_extend, "premium": cmd_premium,
            "unpremium": cmd_unpremium, "deleteuser": cmd_deleteuser, "ban": cmd_ban, "unban": cmd_unban, "banbot": cmd_banbot,
            "unbanbot": cmd_unbanbot, "bans": cmd_bans, "export": cmd_export, "exportuser": cmd_exportuser, "exportbot": cmd_exportbot,
            "sendbackup": cmd_sendbackup, "selfcheck": cmd_selfcheck, "backup": cmd_backup, "restore": cmd_restore,
            "system": cmd_system, "logs": cmd_logs, "plans": cmd_plans, "setplan": cmd_setplan, "admins": cmd_admins,
            "addadmin": cmd_addadmin, "removeadmin": cmd_removeadmin, "setperm": cmd_setperm}
    for n, f in cmds.items():
        app.add_handler(CommandHandler(n, f, filters=filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.Document.ALL & filters.ChatType.PRIVATE, on_doc))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.add_error_handler(on_error)
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
