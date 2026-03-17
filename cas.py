from __future__ import annotations

import logging
import random
import re
import sqlite3
import time
import inspect
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import httpx
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# =========================
# CONFIG (all in one file)
# =========================
BOT_TOKEN = "8748247697:AAFBc7iQJhmO4jSMrq4Dzr-tcNdsHvwNrfE"
CRYPTO_PAY_TOKEN = "550795:AABBqNI8FQXsD2yrokuqyoE5MurVGw2sqH3"  # from @CryptoBot -> Crypto Pay API
ADMIN_IDS = {8203556349}  # add your Telegram user IDs here
DB_PATH = "casino.db"
DEFAULT_STAKE_USD = 1.0
REFERRAL_BONUS_PERCENT = 20
MIN_DEPOSIT_USD = 0.5
MIN_WITHDRAW_USD = 5.0

# UI customization: you can change button texts and emojis here.
# For premium emoji in message text, set IDs from @RawDataBot below.
BTN_PLAY = "🎮 Играть"
BTN_PROFILE = "👤 Профиль"
BTN_BALANCE = "💵 Баланс"
BTN_REF = "👥 Рефералка"
BTN_ADMIN = "🛠 Админка"

# Optional premium emoji IDs (from @RawDataBot), e.g. "5368324170671202286"
EMOJI_WELCOME_ID = "5409048419211682843"
EMOJI_BALANCE_ID = "5409048419211682843"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO
)
logger = logging.getLogger("casino-bot")


class CompatHTTPXRequest(HTTPXRequest):
    """
    Compatibility request class for environments with different httpx versions.

    Some python-telegram-bot/httpx combinations fail with:
    TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxy'

    This adapter remaps `proxy` -> `proxies` when needed.
    """

    def _build_client(self) -> httpx.AsyncClient:  # type: ignore[override]
        kwargs = dict(self._client_kwargs)
        sig = inspect.signature(httpx.AsyncClient.__init__)
        has_proxy = "proxy" in sig.parameters
        has_proxies = "proxies" in sig.parameters

        if "proxy" in kwargs and not has_proxy and has_proxies:
            kwargs["proxies"] = kwargs.pop("proxy")

        return httpx.AsyncClient(**kwargs)


# =========================
# Database layer
# =========================
class DB:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.init()

    def init(self):
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance REAL DEFAULT 0,
                turnover REAL DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                total_bets REAL DEFAULT 0,
                total_wins REAL DEFAULT 0,
                max_win REAL DEFAULT 0,
                referred_by INTEGER,
                referral_earned REAL DEFAULT 0,
                is_admin INTEGER DEFAULT 0,
                created_at INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                game TEXT,
                mode TEXT,
                stake REAL,
                multiplier REAL,
                won INTEGER,
                payout REAL,
                roll INTEGER,
                created_at INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT,
                created_at INTEGER,
                processed_by INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                currency TEXT,
                invoice_id TEXT,
                status TEXT,
                created_at INTEGER
            )
            """
        )
        self.conn.commit()

    def ensure_user(self, user_id: int, username: str, referred_by: Optional[int] = None):
        cur = self.conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        if not cur.fetchone():
            cur.execute(
                """
                INSERT INTO users (user_id, username, referred_by, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, username, referred_by, int(time.time())),
            )
            self.conn.commit()

    def set_admin(self, user_id: int, is_admin: bool = True):
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET is_admin=? WHERE user_id=?", (1 if is_admin else 0, user_id))
        self.conn.commit()

    def is_admin(self, user_id: int) -> bool:
        if user_id in ADMIN_IDS:
            return True
        row = self.conn.execute("SELECT is_admin FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(row and row["is_admin"])

    def get_user(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def update_balance(self, user_id: int, delta: float):
        self.conn.execute("UPDATE users SET balance = balance + ? WHERE user_id=?", (delta, user_id))
        self.conn.commit()

    def apply_bet(
        self,
        user_id: int,
        game: str,
        mode: str,
        stake: float,
        multiplier: float,
        won: bool,
        payout: float,
        roll: int,
    ):
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO bets (user_id, game, mode, stake, multiplier, won, payout, roll, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, game, mode, stake, multiplier, 1 if won else 0, payout, roll, int(time.time())),
        )
        cur.execute(
            """
            UPDATE users SET
                balance = balance - ? + ?,
                turnover = turnover + ?,
                games_played = games_played + 1,
                total_bets = total_bets + ?,
                total_wins = total_wins + ?,
                max_win = CASE WHEN ? > max_win THEN ? ELSE max_win END
            WHERE user_id=?
            """,
            (stake, payout, stake, stake, payout, payout, payout, user_id),
        )

        # Referral bonus from casino profit (simple model)
        user = self.get_user(user_id)
        if user and user["referred_by"]:
            house_profit = max(0.0, stake - payout)
            bonus = house_profit * (REFERRAL_BONUS_PERCENT / 100)
            if bonus > 0:
                cur.execute(
                    "UPDATE users SET balance=balance+?, referral_earned=referral_earned+? WHERE user_id=?",
                    (bonus, bonus, user["referred_by"]),
                )

        self.conn.commit()

    def add_withdrawal(self, user_id: int, amount: float):
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO withdrawals (user_id, amount, status, created_at, processed_by) VALUES (?, ?, 'pending', ?, NULL)",
            (user_id, amount, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_pending_withdrawals(self):
        return self.conn.execute(
            "SELECT * FROM withdrawals WHERE status='pending' ORDER BY id ASC"
        ).fetchall()

    def process_withdrawal(self, wid: int, admin_id: int, approve: bool):
        status = "approved" if approve else "rejected"
        row = self.conn.execute("SELECT * FROM withdrawals WHERE id=?", (wid,)).fetchone()
        if not row or row["status"] != "pending":
            return None
        self.conn.execute(
            "UPDATE withdrawals SET status=?, processed_by=? WHERE id=?", (status, admin_id, wid)
        )
        if not approve:
            self.conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (row["amount"], row["user_id"]))
        self.conn.commit()
        return row

    def users_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    def sum_bets(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(total_bets),0) s FROM users").fetchone()
        return float(row["s"])

    def sum_wins(self) -> float:
        row = self.conn.execute("SELECT COALESCE(SUM(total_wins),0) s FROM users").fetchone()
        return float(row["s"])

    def sum_losses(self) -> float:
        return max(0.0, self.sum_bets() - self.sum_wins())

    def pending_withdrawals_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()
        return int(row["c"])

    def today_withdrawals_sum(self) -> float:
        now = int(time.time())
        start_of_day = now - (now % 86400)
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM withdrawals WHERE created_at>=?",
            (start_of_day,),
        ).fetchone()
        return float(row["s"])

    def all_user_ids(self) -> List[int]:
        return [r[0] for r in self.conn.execute("SELECT user_id FROM users").fetchall()]


db = DB(DB_PATH)


# =========================
# Game definitions
# =========================
@dataclass
class GameMode:
    key: str
    title: str
    multiplier: float
    check: Callable[[int], bool]


GAME_MODES: Dict[str, List[GameMode]] = {
    "cube": [
        GameMode("odd", "Нечёт (1.85x)", 1.85, lambda v: v % 2 == 1),
        GameMode("even", "Чёт (1.85x)", 1.85, lambda v: v % 2 == 0),
        GameMode("less4", "Меньше 4 (1.85x)", 1.85, lambda v: v < 4),
        GameMode("more3", "Больше 3 (1.85x)", 1.85, lambda v: v > 3),
        GameMode("less2", "Меньше 2 (2.2x)", 2.2, lambda v: v < 2),
        GameMode("more5", "Больше 5 (2.2x)", 2.2, lambda v: v > 5),
        GameMode("pvp", "PVP (1.85x)", 1.85, lambda v: random.choice([True, False])),
        GameMode("line", "Линия (1.78x)", 1.78, lambda v: v in [3, 4]),
    ],
    "football": [
        GameMode("goal", "Гол (1.3x)", 1.3, lambda v: v >= 4),
        GameMode("miss", "Промах (1.3x)", 1.3, lambda v: v <= 3),
    ],
    "basketball": [
        GameMode("score", "Попадание (1.5x)", 1.5, lambda v: v >= 4),
        GameMode("miss", "Промах (1.5x)", 1.5, lambda v: v <= 3),
    ],
    "darts": [
        GameMode("hit", "Попадание (1.6x)", 1.6, lambda v: v in [4, 5, 6]),
        GameMode("miss", "Промах (1.6x)", 1.6, lambda v: v in [1, 2, 3]),
    ],
    "bowling": [
        GameMode("strike", "Страйк (2.0x)", 2.0, lambda v: v == 6),
        GameMode("miss", "Промах (2.0x)", 2.0, lambda v: v != 6),
    ],
}

GAME_TITLES = {
    "cube": "🎲 Куб",
    "football": "⚽ Футбол",
    "basketball": "🏀 Баскетбол",
    "darts": "🎯 Дартс",
    "bowling": "🎳 Боулинг",
}

EMOJI_BY_GAME = {
    "cube": "🎲",
    "football": "⚽",
    "basketball": "🏀",
    "darts": "🎯",
    "bowling": "🎳",
}


# =========================
# Keyboards
# =========================
def main_menu_keyboard(user_id: Optional[int] = None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_PLAY), KeyboardButton(BTN_PROFILE)],
        [KeyboardButton(BTN_BALANCE), KeyboardButton(BTN_REF)],
    ]
    if user_id is not None and db.is_admin(user_id):
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def game_select_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [
            InlineKeyboardButton("🎲 Куб", callback_data="game:cube"),
            InlineKeyboardButton("⚽ Футбол", callback_data="game:football"),
        ],
        [
            InlineKeyboardButton("🏀 Баскетбол", callback_data="game:basketball"),
            InlineKeyboardButton("🎯 Дартс", callback_data="game:darts"),
        ],
        [InlineKeyboardButton("🎳 Боулинг", callback_data="game:bowling")],
    ]
    return InlineKeyboardMarkup(kb)


def modes_keyboard(game_key: str) -> InlineKeyboardMarkup:
    modes = GAME_MODES[game_key]
    kb = [[InlineKeyboardButton(m.title, callback_data=f"bet:{game_key}:{m.key}")] for m in modes]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back:games")])
    return InlineKeyboardMarkup(kb)


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Пополнить", callback_data="balance:deposit")],
            [InlineKeyboardButton("➖ Вывести", callback_data="balance:withdraw")],
            [InlineKeyboardButton("📊 Статистика", callback_data="profile:stats")],
        ]
    )


DEPOSIT_AMOUNT = 90
WITHDRAW_AMOUNT = 100
BET_AMOUNT = 110
BET_CONFIRM = 111
ADMIN_BROADCAST = 200
ADMIN_GIVE_BALANCE = 210
ADMIN_WITHDRAW_PAYOUT = 220


# =========================
# Helpers
# =========================
def rank_by_turnover(turnover: float) -> str:
    if turnover >= 30000:
        return "💎 Бриллиант"
    if turnover >= 15000:
        return "🔷 Алмаз"
    if turnover >= 7000:
        return "🥇 Золото"
    if turnover >= 3000:
        return "🥈 Серебро"
    return "🥉 Бронза"


def premium_emoji(emoji_id: str, fallback: str) -> str:
    """Render premium emoji by id (if provided), fallback to normal emoji."""
    if emoji_id:
        return f'<a href="tg://emoji?id={emoji_id}">🙂</a>'
    return fallback


def find_mode(game_key: str, mode_key: str) -> Optional[GameMode]:
    for m in GAME_MODES.get(game_key, []):
        if m.key == mode_key:
            return m
    return None


def crypto_headers() -> Dict[str, str]:
    return {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}


def create_crypto_invoice(amount_usd: float, payload: str) -> Optional[Tuple[str, str]]:
    """Returns (invoice_id, pay_url) or None."""
    try:
        resp = requests.post(
            "https://pay.crypt.bot/api/createInvoice",
            headers=crypto_headers(),
            json={
                "asset": "USDT",
                "amount": str(round(amount_usd, 2)),
                "description": "Casino bot deposit",
                "payload": payload,
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ok"):
            result = data["result"]
            return str(result["invoice_id"]), result["pay_url"]
    except Exception as e:
        logger.error("create_crypto_invoice failed: %s", e)
    return None


# =========================
# Bot handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ref = None
    if context.args:
        try:
            ref = int(context.args[0])
            if ref == user.id:
                ref = None
        except ValueError:
            ref = None

    db.ensure_user(user.id, user.username or "no_username", referred_by=ref)
    welcome_icon = premium_emoji(EMOJI_WELCOME_ID, "👋")
    text = (
        f"{welcome_icon} Добро пожаловать, @{user.username or user.first_name}!\n\n"
        "Это казино-бот. Нажми «Играть», чтобы выбрать игру."
    )
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=main_menu_keyboard(user.id))


async def menu_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = db.get_user(update.effective_user.id)
    bal = user["balance"] if user else 0
    text = (
        "🎮 Выберите игру или режим.\n"
        f"Ваш баланс: ${bal:.2f}\n"
        f"Ставка по умолчанию: ${DEFAULT_STAKE_USD:.2f}"
    )
    await update.message.reply_text(text, reply_markup=game_select_keyboard())


async def menu_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = db.get_user(update.effective_user.id)
    rank = rank_by_turnover(u["turnover"])
    txt = (
        f"👤 Профиль игрока\n"
        f"ID: {u['user_id']}\n"
        f"Username: @{u['username']}\n"
        f"Баланс: ${u['balance']:.2f}\n"
        f"Оборот: ${u['turnover']:.2f}\n"
        f"Сыграно: {u['games_played']}\n"
        f"Ранг: {rank}"
    )
    await update.message.reply_text(txt, reply_markup=profile_keyboard())


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📈 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton("👮 Назначить админа", callback_data="admin:setadmin")],
            [InlineKeyboardButton("💸 Заявки на вывод", callback_data="admin:withdraws")],
            [InlineKeyboardButton("💵 Выдать баланс", callback_data="admin:give")],
        ]
    )


async def menu_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = db.get_user(update.effective_user.id)
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Пополнить", callback_data="balance:deposit")],
            [InlineKeyboardButton("➖ Вывести", callback_data="balance:withdraw")],
        ]
    )
    balance_icon = premium_emoji(EMOJI_BALANCE_ID, "💰")
    await update.message.reply_text(f"{balance_icon} Ваш баланс: ${u['balance']:.2f}", parse_mode="HTML", reply_markup=kb)


async def menu_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = db.get_user(uid)
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={uid}"
    invited = db.conn.execute("SELECT COUNT(*) c FROM users WHERE referred_by=?", (uid,)).fetchone()["c"]
    txt = (
        "👥 Реферальная программа\n"
        f"Ваша ссылка: {link}\n"
        f"Приглашено: {invited}\n"
        f"Заработано: ${u['referral_earned']:.2f}\n\n"
        f"Ваши друзья получают доступ к играм, а вы получаете {REFERRAL_BONUS_PERCENT}% от прибыли казино по их ставкам."
    )
    await update.message.reply_text(txt)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data.startswith("game:"):
        game = data.split(":", 1)[1]
        await q.edit_message_text(
            f"{GAME_TITLES.get(game, game)}\nВыберите режим:", reply_markup=modes_keyboard(game)
        )
        return

    if data == "back:games":
        await q.edit_message_text("🎮 Выберите игру:", reply_markup=game_select_keyboard())
        return

    if data == "admin:stats":
        if not db.is_admin(uid):
            await q.message.reply_text("Нет доступа")
            return
        await q.message.reply_text(
            "📈 Статистика\n"
            f"Пользователей: {db.users_count()}\n"
            f"Размер ставок (оборот): ${db.sum_bets():.2f}\n"
            f"Выигрыши: ${db.sum_wins():.2f}\n"
            f"Проигрыши: ${db.sum_losses():.2f}\n"
            f"Заявки на вывод (pending): {db.pending_withdrawals_count()}\n"
            f"Выводы за сегодня: ${db.today_withdrawals_sum():.2f}"
        )
        return

    if data == "admin:withdraws":
        if not db.is_admin(uid):
            await q.message.reply_text("Нет доступа")
            return
        rows = db.get_pending_withdrawals()
        if not rows:
            await q.message.reply_text("Нет заявок на вывод")
            return
        for row in rows:
            kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("💸 Вывести", callback_data=f"admin:withdraw:{row['id']}:payout"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:withdraw:{row['id']}:reject"),
                ]]
            )
            await q.message.reply_text(
                f"Заявка #{row['id']}\nUser: {row['user_id']}\nСумма: ${row['amount']:.2f}",
                reply_markup=kb,
            )
        return

    if data == "profile:stats":
        u = db.get_user(uid)
        txt = (
            "📊 Статистика\n"
            f"Сыграно: {u['games_played']}\n"
            f"Сумма ставок: ${u['total_bets']:.2f}\n"
            f"Сумма выигрышей: ${u['total_wins']:.2f}\n"
            f"Макс. выигрыш: ${u['max_win']:.2f}"
        )
        await q.message.reply_text(txt)
        return

    if data.startswith("admin:withdraw:"):
        if not db.is_admin(uid):
            await q.message.reply_text("Нет доступа")
            return
        _, _, wid, action = data.split(":")
        if action == "payout":
            return
        row = db.process_withdrawal(int(wid), uid, approve=False)
        if not row:
            await q.message.reply_text("Заявка не найдена или уже обработана")
            return
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        msg = f"Заявка #{wid} {'выполнена' if action == 'payout' else 'отклонена'}"
        await q.message.reply_text(msg)
        if action != "payout":
            try:
                await context.bot.send_message(
                    chat_id=row["user_id"],
                    text=f"Ваша заявка на вывод ${row['amount']:.2f} — отклонена",
                )
            except Exception:
                pass
        return


async def deposit_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["finance_action"] = "deposit"
    await q.message.reply_text(
        f"Введите сумму пополнения (USD). Минимум: ${MIN_DEPOSIT_USD:.2f}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return DEPOSIT_AMOUNT


async def play_bet_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    _, game, mode_key = q.data.split(":", 2)
    mode = find_mode(game, mode_key)
    if not mode:
        await q.message.reply_text("Режим не найден")
        return ConversationHandler.END
    context.user_data["bet_game"] = game
    context.user_data["bet_mode"] = mode_key
    await q.message.reply_text(
        f"{GAME_TITLES.get(game, game)} | {mode.title}\nВведите сумму ставки (USD):",
        reply_markup=ReplyKeyboardRemove(),
    )
    return BET_AMOUNT


async def bet_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text.strip().replace(",", ".")
    game = context.user_data.get("bet_game")
    mode_key = context.user_data.get("bet_mode")
    mode = find_mode(game, mode_key) if game and mode_key else None
    if not mode:
        await update.message.reply_text("Сессия ставки не найдена. Начните заново.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END
    try:
        stake = float(txt)
    except ValueError:
        await update.message.reply_text("Введите корректную сумму числом.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END
    if stake <= 0:
        await update.message.reply_text("Ставка должна быть больше 0.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END
    u = db.get_user(uid)
    if u["balance"] < stake:
        await update.message.reply_text("Недостаточно средств для ставки.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    context.user_data["bet_stake"] = stake
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Играть", callback_data="betconfirm:play")],
            [InlineKeyboardButton("❌ Отмена", callback_data="betconfirm:cancel")],
        ]
    )
    await update.message.reply_text(
        "Подтвердите ставку:\n"
        f"Игра: {GAME_TITLES.get(game, game)}\n"
        f"Режим: {mode.title}\n"
        f"Сумма: ${stake:.2f}\n"
        f"Коэффициент: x{mode.multiplier}",
        reply_markup=kb,
    )
    return BET_CONFIRM


async def bet_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    action = q.data.split(":", 1)[1]
    if action == "cancel":
        await q.edit_message_text("Ставка отменена")
        await q.message.reply_text("Возвращаемся в меню.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    game = context.user_data.get("bet_game")
    mode_key = context.user_data.get("bet_mode")
    stake = float(context.user_data.get("bet_stake", 0))
    mode = find_mode(game, mode_key) if game and mode_key else None
    if not mode or stake <= 0:
        await q.edit_message_text("Сессия ставки не найдена")
        return ConversationHandler.END

    u = db.get_user(uid)
    if u["balance"] < stake:
        await q.edit_message_text("Недостаточно средств на балансе")
        return ConversationHandler.END

    roll_msg = await context.bot.send_dice(chat_id=uid, emoji=EMOJI_BY_GAME[game])
    roll = roll_msg.dice.value
    won = bool(mode.check(roll))
    payout = round(stake * mode.multiplier, 2) if won else 0.0
    db.apply_bet(uid, game, mode.title, stake, mode.multiplier, won, payout, roll)

    u2 = db.get_user(uid)
    if won:
        result_text = f"🎉 Поздравляем, вы выиграли ${payout:.2f}!"
    else:
        result_text = "😔 Вы проиграли эту ставку."

    await q.edit_message_text(
        f"{GAME_TITLES.get(game, game)} | {mode.title}\n"
        f"Ставка: ${stake:.2f}\n"
        f"Результат броска: {roll}\n"
        f"{result_text}\n"
        f"Текущий баланс: ${u2['balance']:.2f}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔁 Играть ещё", callback_data=f"game:{game}")],
                [InlineKeyboardButton("🎮 К играм", callback_data="back:games")],
            ]
        ),
    )
    return ConversationHandler.END


async def withdraw_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["finance_action"] = "withdraw"
    await q.message.reply_text(
        f"Введите сумму вывода (числом, USD). Минимум: ${MIN_WITHDRAW_USD:.2f}",
        reply_markup=ReplyKeyboardRemove(),
    )
    return WITHDRAW_AMOUNT


async def deposit_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text.strip().replace(",", ".")
    try:
        amount = float(txt)
    except ValueError:
        await update.message.reply_text("Введите корректную сумму числом.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    if amount < MIN_DEPOSIT_USD:
        await update.message.reply_text(
            f"Минимальная сумма пополнения: ${MIN_DEPOSIT_USD:.2f}",
            reply_markup=main_menu_keyboard(uid),
        )
        return ConversationHandler.END

    invoice = create_crypto_invoice(amount, payload=f"deposit_{uid}_{int(time.time())}")
    if not invoice:
        await update.message.reply_text(
            "Не удалось создать invoice CryptoPay. Проверьте API token.",
            reply_markup=main_menu_keyboard(uid),
        )
        return ConversationHandler.END

    invoice_id, pay_url = invoice
    db.conn.execute(
        "INSERT INTO deposits (user_id, amount, currency, invoice_id, status, created_at) VALUES (?, ?, 'USDT', ?, 'created', ?)",
        (uid, amount, invoice_id, int(time.time())),
    )
    db.conn.commit()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💳 Оплатить", url=pay_url)]])
    await update.message.reply_text(
        f"Счёт на пополнение: ${amount:.2f}\nНажмите кнопку ниже для оплаты.",
        reply_markup=kb,
    )
    await update.message.reply_text("После оплаты вернитесь в меню.", reply_markup=main_menu_keyboard(uid))
    return ConversationHandler.END


async def withdraw_amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    txt = update.message.text.strip().replace(",", ".")
    try:
        amount = float(txt)
    except ValueError:
        await update.message.reply_text("Введите корректную сумму числом.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    if amount < MIN_WITHDRAW_USD:
        await update.message.reply_text(
            f"Минимальная сумма вывода: ${MIN_WITHDRAW_USD:.2f}",
            reply_markup=main_menu_keyboard(uid),
        )
        return ConversationHandler.END

    u = db.get_user(uid)
    if u["balance"] < amount:
        await update.message.reply_text("Недостаточно средств.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    db.update_balance(uid, -amount)
    wid = db.add_withdrawal(uid, amount)

    await update.message.reply_text(
        f"Заявка на вывод ${amount:.2f} создана. Ожидайте обработку в админ-панели.",
        reply_markup=main_menu_keyboard(uid),
    )

    for admin_id in db.all_user_ids():
        if db.is_admin(admin_id):
            kb = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("💸 Вывести", callback_data=f"admin:withdraw:{wid}:payout"),
                        InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:withdraw:{wid}:reject"),
                    ]
                ]
            )
            try:
                await context.bot.send_message(
                    admin_id,
                    f"Новый вывод\nUser: {uid}\nСумма: ${amount:.2f}",
                    reply_markup=kb,
                )
            except Exception:
                pass

    return ConversationHandler.END


async def admin_action_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END

    action = context.user_data.get("admin_action")
    text = update.message.text.strip()

    if action == "setadmin":
        try:
            target = int(text)
        except ValueError:
            await update.message.reply_text("Введите корректный user_id числом.")
            return ConversationHandler.END
        db.set_admin(target, True)
        await update.message.reply_text(f"Пользователь {target} назначен администратором")
        return ConversationHandler.END

    if action == "give_balance":
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("Формат: @username сумма")
            return ConversationHandler.END
        username = parts[0].lstrip("@")
        try:
            amount = float(parts[1].replace(",", "."))
        except ValueError:
            await update.message.reply_text("Сумма должна быть числом")
            return ConversationHandler.END
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше 0")
            return ConversationHandler.END
        user = db.conn.execute("SELECT user_id FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            await update.message.reply_text("Пользователь с таким username не найден в базе")
            return ConversationHandler.END
        db.update_balance(user["user_id"], amount)
        await update.message.reply_text(f"Выдано ${amount:.2f} пользователю @{username}")
        return ConversationHandler.END

    await update.message.reply_text("Неизвестное действие")
    return ConversationHandler.END


async def admin_broadcast_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not db.is_admin(q.from_user.id):
        await q.message.reply_text("Нет доступа")
        return ConversationHandler.END
    await q.message.reply_text("Введите текст для рассылки:")
    return ADMIN_BROADCAST


async def admin_setadmin_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not db.is_admin(q.from_user.id):
        await q.message.reply_text("Нет доступа")
        return ConversationHandler.END
    context.user_data["admin_action"] = "setadmin"
    await q.message.reply_text("Введите user_id для назначения админом:")
    return ADMIN_GIVE_BALANCE


async def admin_give_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not db.is_admin(q.from_user.id):
        await q.message.reply_text("Нет доступа")
        return ConversationHandler.END
    context.user_data["admin_action"] = "give_balance"
    await q.message.reply_text("Введите: @username сумма\nПример: @user123 50")
    return ADMIN_GIVE_BALANCE


async def admin_withdraw_payout_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not db.is_admin(q.from_user.id):
        await q.message.reply_text("Нет доступа")
        return ConversationHandler.END
    _, _, wid, action = q.data.split(":")
    if action != "payout":
        return ConversationHandler.END
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    row = db.conn.execute("SELECT * FROM withdrawals WHERE id=?", (int(wid),)).fetchone()
    if not row or row["status"] != "pending":
        await q.message.reply_text("Заявка не найдена или уже обработана")
        return ConversationHandler.END
    context.user_data["payout_withdraw_id"] = int(wid)
    context.user_data["payout_user_id"] = row["user_id"]
    context.user_data["payout_amount"] = float(row["amount"])
    await q.message.reply_text("Введите сообщение для пользователя по выплате:")
    return ADMIN_WITHDRAW_PAYOUT


async def admin_withdraw_payout_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    wid = context.user_data.get("payout_withdraw_id")
    user_id = context.user_data.get("payout_user_id")
    amount = context.user_data.get("payout_amount")
    if not wid or not user_id:
        await update.message.reply_text("Заявка не выбрана")
        return ConversationHandler.END

    message = update.message.text.strip()
    if not message:
        await update.message.reply_text("Сообщение не должно быть пустым")
        return ConversationHandler.END

    row = db.process_withdrawal(int(wid), update.effective_user.id, approve=True)
    if not row:
        await update.message.reply_text("Заявка не найдена или уже обработана")
        return ConversationHandler.END

    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                f"Ваша заявка на вывод ${float(amount):.2f} была обработана.\n"
                f"{message}"
            ),
        )
    except Exception:
        pass

    await update.message.reply_text(f"Заявка #{wid} выполнена и отправлена пользователю")
    return ConversationHandler.END


# Admin commands
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not db.is_admin(uid):
        await update.message.reply_text("Нет доступа")
        return
    text = "🛠 Админ-панель\nВыберите действие:" 
    await update.message.reply_text(text, reply_markup=admin_panel_keyboard())


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return
    await update.message.reply_text(
        "📈 Статистика\n"
        f"Пользователей: {db.users_count()}\n"
        f"Размер ставок (оборот): ${db.sum_bets():.2f}\n"
        f"Выигрыши: ${db.sum_wins():.2f}\n"
        f"Проигрыши: ${db.sum_losses():.2f}\n"
        f"Заявки на вывод (pending): {db.pending_withdrawals_count()}\n"
        f"Выводы за сегодня: ${db.today_withdrawals_sum():.2f}"
    )


async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return ConversationHandler.END
    await update.message.reply_text("Введите текст для рассылки:")
    return ADMIN_BROADCAST


async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    sent = 0
    for uid in db.all_user_ids():
        try:
            await context.bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Рассылка завершена. Доставлено: {sent}")
    return ConversationHandler.END


async def admin_setadmin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return
    if not context.args:
        await update.message.reply_text("Использование: /admin_setadmin <user_id>")
        return
    try:
        target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id должен быть числом")
        return
    db.set_admin(target, True)
    await update.message.reply_text(f"Пользователь {target} назначен администратором")


async def menu_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await admin_panel(update, context)


async def admin_withdraws(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.is_admin(update.effective_user.id):
        await update.message.reply_text("Нет доступа")
        return
    rows = db.get_pending_withdrawals()
    if not rows:
        await update.message.reply_text("Нет заявок на вывод")
        return
    for row in rows:
        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("💸 Вывести", callback_data=f"admin:withdraw:{row['id']}:payout"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:withdraw:{row['id']}:reject"),
            ]]
        )
        await update.message.reply_text(
            f"Заявка #{row['id']}\nUser: {row['user_id']}\nСумма: ${row['amount']:.2f}",
            reply_markup=kb,
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.", reply_markup=main_menu_keyboard(update.effective_user.id))
    return ConversationHandler.END


def main():
    if BOT_TOKEN.startswith("PUT_YOUR"):
        print("Set BOT_TOKEN in cas.py first.")
        return

    request = CompatHTTPXRequest()
    get_updates_request = CompatHTTPXRequest()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("admin_stats", admin_stats))
    app.add_handler(CommandHandler("admin_setadmin", admin_setadmin))
    app.add_handler(CommandHandler("admin_withdraws", admin_withdraws))

    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PLAY)}$"), menu_play))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_PROFILE)}$"), menu_profile))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_BALANCE)}$"), menu_balance))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_REF)}$"), menu_ref))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(BTN_ADMIN)}$"), menu_admin))

    finance_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(deposit_entry, pattern=r"^balance:deposit$"),
            CallbackQueryHandler(withdraw_entry, pattern=r"^balance:withdraw$"),
        ],
        states={
            DEPOSIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deposit_amount_received)],
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(finance_conv)

    bet_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(play_bet_entry, pattern=r"^bet:")],
        states={
            BET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, bet_amount_received)],
            BET_CONFIRM: [CallbackQueryHandler(bet_confirm_callback, pattern=r"^betconfirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(bet_conv)

    broadcast_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin_broadcast", admin_broadcast_start),
            CallbackQueryHandler(admin_broadcast_entry, pattern=r"^admin:broadcast$"),
        ],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(broadcast_conv)

    admin_action_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_setadmin_entry, pattern=r"^admin:setadmin$"),
            CallbackQueryHandler(admin_give_entry, pattern=r"^admin:give$"),
        ],
        states={ADMIN_GIVE_BALANCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_action_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(admin_action_conv)

    admin_payout_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_withdraw_payout_entry, pattern=r"^admin:withdraw:\d+:payout$")],
        states={ADMIN_WITHDRAW_PAYOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_withdraw_payout_received)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(admin_payout_conv)

    app.add_handler(CallbackQueryHandler(on_callback))

    print("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
