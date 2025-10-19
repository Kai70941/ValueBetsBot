# -------------------------------
# ValueBets Bot — full version
# -------------------------------
import os
import re
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction, Embed, Colour

import aiohttp

# Try to import asyncpg; we’ll degrade gracefully if not present
try:
    import asyncpg  # type: ignore
    ASYNCPG_AVAILABLE = True
except Exception:
    asyncpg = None
    ASYNCPG_AVAILABLE = False

# ---------- CONFIG ----------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0") or 0)
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0") or 0)
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0") or 0)
VALUE_BETS_CHANNEL = int(os.getenv("VALUE_BETS_CHANNEL_ID", "1422337929392689233") or 0)

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Bankroll in units
BANKROLL_UNITS = 1000
CONSERVATIVE_PCT = 0.015  # 1.5% of bankroll in units

# Only these bookmakers (lowercased substring)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

EDGE_VALUE_THRESHOLD = 2.0  # % edge threshold to label as Value
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("valuebets")

SESSION: Optional[aiohttp.ClientSession] = None
FETCH_LOCK = asyncio.Lock()
posted_bets: set[str] = set()
pool: Optional["asyncpg.Pool"] = None  # type: ignore

# ---------- Sport/League helpers ----------
SPORT_EMOJI = {
    "soccer": "⚽",
    "americanfootball": "🏈",
    "basketball": "🏀",
    "baseball": "⚾",
    "icehockey": "🏒",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "cricket": "🏏",
    "aussierules": "🏉",
    "rugbyleague": "🏉",
    "rugbyunion": "🏉",
    "esports": "🎮",
    "golf": "⛳",
    "tabletennis": "🏓",
}

def canonical_sport_key(sport_key: str) -> str:
    if "soccer" in sport_key.lower():
        return "soccer"
    return re.sub(r"[^a-z]", "", sport_key.lower())

def sport_title_from_key(key: str) -> str:
    m = {
        "soccer": "Soccer",
        "americanfootball": "American Football",
        "basketball": "Basketball",
        "baseball": "Baseball",
        "icehockey": "Ice Hockey",
        "tennis": "Tennis",
        "mma": "MMA",
        "boxing": "Boxing",
        "cricket": "Cricket",
        "aussierules": "Aussie Rules",
        "rugbyleague": "Rugby League",
        "rugbyunion": "Rugby Union",
        "esports": "Esports",
        "golf": "Golf",
        "tabletennis": "Table Tennis",
    }
    return m.get(key, key.capitalize())

def extract_league(sport_title: str, event_title: str) -> str:
    # Prefer "Sport title - League Name" from API, else try common patterns
    if " - " in sport_title:
        parts = sport_title.split(" - ", 1)
        if len(parts) == 2 and parts[1].strip():
            return parts[1].strip()
    patterns = [
        r"(Serie [ABCD])",
        r"(La Liga|Premier League|Bundesliga|Ligue 1|Eredivisie)",
        r"(NCAA|NFL|NBA|NHL|MLB)",
        r"(A-League|K-League|J-League)",
        r"(Big Bash|IPL|CPL|PSL)",
        r"(Brazil Série [AB])",
    ]
    for pat in patterns:
        m = re.search(pat, event_title, re.IGNORECASE)
        if m:
            return m.group(1).title()
    return "Unknown League"

def sport_and_league(event: dict) -> Tuple[str, str, str]:
    skey_raw = event.get("sport_key", "") or ""
    stitle_raw = event.get("sport_title", "") or ""
    title = event.get("title", "") or ""
    key = canonical_sport_key(skey_raw)
    emoji = SPORT_EMOJI.get(key, "🎲")
    sport_name = sport_title_from_key(key)
    league = extract_league(stitle_raw or sport_name, title)
    return emoji, sport_name, league

# ---------- HTTP ----------
async def get_http_session() -> aiohttp.ClientSession:
    global SESSION
    if SESSION is None or SESSION.closed:
        SESSION = aiohttp.ClientSession(timeout=HTTP_TIMEOUT)
    return SESSION

async def fetch_odds() -> List[dict]:
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    try:
        session = await get_http_session()
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning(f"Odds API non-200 {resp.status}: {body[:200]}")
                return []
            return await resp.json()
    except Exception as e:
        log.exception(f"Odds fetch error: {e}")
        return []

# ---------- DB ----------
CREATE_BETS_TABLE = """
CREATE TABLE IF NOT EXISTS bets (
    id SERIAL PRIMARY KEY,
    event_id TEXT,
    match TEXT,
    bookmaker TEXT,
    team TEXT,
    odds NUMERIC,
    consensus NUMERIC,
    implied NUMERIC,
    edge NUMERIC,
    cons_stake_units NUMERIC,
    smart_stake_units NUMERIC,
    agg_stake_units NUMERIC,
    cons_exp_profit NUMERIC,
    smart_exp_profit NUMERIC,
    agg_exp_profit NUMERIC,
    bet_time TIMESTAMPTZ,
    category TEXT,
    sport TEXT,
    league TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_USER_BETS_TABLE = """
CREATE TABLE IF NOT EXISTS user_bets (
    id SERIAL PRIMARY KEY,
    user_id TEXT,
    username TEXT,
    bet_key TEXT,
    event_id TEXT,
    sport TEXT,
    league TEXT,
    match TEXT,
    bookmaker TEXT,
    team TEXT,
    odds NUMERIC,
    strategy TEXT,         -- conservative/smart/aggressive
    stake_units NUMERIC,
    placed_at TIMESTAMPTZ DEFAULT NOW()
);
"""

async def init_db():
    global pool
    if not ASYNCPG_AVAILABLE or not DATABASE_URL:
        log.warning("DB unavailable (asyncpg missing or DATABASE_URL not set).")
        return
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)  # type: ignore
    async with pool.acquire() as con:
        await con.execute(CREATE_BETS_TABLE)
        await con.execute(CREATE_USER_BETS_TABLE)
    log.info("DB ready.")

async def save_bet_row(row: dict):
    if not pool:  # DB optional
        return
    q = """
    INSERT INTO bets (
        event_id, match, bookmaker, team, odds, consensus, implied, edge,
        cons_stake_units, smart_stake_units, agg_stake_units,
        cons_exp_profit, smart_exp_profit, agg_exp_profit,
        bet_time, category, sport, league
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,
        $9,$10,$11,$12,$13,$14,$15,$16,$17,$18
    );
    """
    async with pool.acquire() as con:
        await con.execute(q,
            row.get("event_id"),
            row.get("match"),
            row.get("bookmaker"),
            row.get("team"),
            row.get("odds"),
            row.get("consensus"),
            row.get("implied"),
            row.get("edge"),
            row.get("cons_stake"),
            row.get("smart_stake"),
            row.get("agg_stake"),
            row.get("cons_exp_profit"),
            row.get("smart_exp_profit"),
            row.get("agg_exp_profit"),
            row.get("bet_time"),
            row.get("category"),
            row.get("sport"),
            row.get("league"),
        )

async def log_user_bet(
    interaction: Interaction,
    bet: dict,
    strategy: str,
    stake_units: float
):
    if not pool:
        await interaction.response.send_message("📦 Database not configured; couldn’t save this bet.", ephemeral=True)
        return
    q = """
    INSERT INTO user_bets (
        user_id, username, bet_key, event_id, sport, league, match, bookmaker, team, odds, strategy, stake_units
    ) VALUES (
        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12
    );
    """
    bet_key = bet_id(bet)
    async with pool.acquire() as con:
        await con.execute(q,
            str(interaction.user.id),
            str(interaction.user),
            bet_key,
            bet.get("event_id"),
            bet.get("sport"),
            bet.get("league"),
            bet.get("match"),
            bet.get("bookmaker"),
            bet.get("team"),
            bet.get("odds"),
            strategy,
            stake_units
        )
    await interaction.response.send_message(f"✅ Logged your **{strategy}** bet ({stake_units}u).", ephemeral=True)

# ---------- Betting logic ----------
def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

def calculate_bets(data: List[dict]) -> List[dict]:
    now = datetime.now(timezone.utc)
    out: List[dict] = []
    for event in data:
        home = event.get("home_team") or "Home"
        away = event.get("away_team") or "Away"
        match_name = f"{home} vs {away}"

        # time filter
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat((commence_time or "").replace("Z", "+00:00"))
        except Exception:
            continue
        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # consensus across allowed books
        imps: Dict[str, float] = {}
        cnts: Dict[str, int] = {}
        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for mkt in book.get("markets", []):
                mkey = mkt.get("key", "h2h")
                for oc in mkt.get("outcomes", []):
                    name = oc.get("name")
                    price = oc.get("price")
                    if not name or not price:
                        continue
                    k = f"{mkey}:{name}"
                    imps[k] = imps.get(k, 0.0) + (1.0 / float(price))
                    cnts[k] = cnts.get(k, 0) + 1
        if not imps:
            continue

        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not _allowed_bookmaker(title):
                continue
            for mkt in book.get("markets", []):
                mkey = mkt.get("key", "h2h")
                for oc in mkt.get("outcomes", []):
                    name = oc.get("name")
                    price = oc.get("price")
                    if not name or not price:
                        continue
                    k = f"{mkey}:{name}"
                    if k not in imps:
                        continue
                    consensus_p = imps[k] / max(1, cnts[k])
                    implied_p = 1.0 / float(price)
                    edge_pct = (consensus_p - implied_p) * 100.0

                    cons = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
                    smart = round(min(cons * (1 + max(0.0, edge_pct) / 50.0), cons * 5), 2)
                    agg = round(min(cons * (1 + max(0.0, edge_pct) / 20.0), cons * 15), 2)

                    cons_payout = cons * float(price)
                    smart_payout = smart * float(price)
                    agg_payout = agg * float(price)

                    cons_exp = round(consensus_p * cons_payout - cons, 2)
                    smart_exp = round(consensus_p * smart_payout - smart, 2)
                    agg_exp = round(consensus_p * agg_payout - agg, 2)

                    emoji, sport_name, league = sport_and_league(event)

                    out.append({
                        "event_id": event.get("id"),
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": float(price),
                        "consensus": round(consensus_p * 100, 2),
                        "implied": round(implied_p * 100, 2),
                        "edge": round(edge_pct, 2),
                        "cons_stake": cons,
                        "smart_stake": smart,
                        "agg_stake": agg,
                        "cons_exp_profit": cons_exp,
                        "smart_exp_profit": smart_exp,
                        "agg_exp_profit": agg_exp,
                        "bet_time": commence_dt,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "sport": sport_name,
                        "league": league,
                        "emoji": emoji,
                        "event": event,  # keep raw
                    })
    return out

def bet_id(b: dict) -> str:
    return f"{b.get('event_id')}|{b['match']}|{b['team']}|{b['bookmaker']}|{b['bet_time']}"

def value_indicator(b: dict) -> Tuple[str, Colour]:
    if b["edge"] >= EDGE_VALUE_THRESHOLD:
        return "🟢 Value Bet", Colour.from_str("#2ecc71")
    return "🔴 Low Value", Colour.from_str("#e74c3c")

def best_bet_selection(bets: List[dict]) -> Optional[dict]:
    candidates = [b for b in bets if b["edge"] >= EDGE_VALUE_THRESHOLD]
    if not candidates:
        return None
    def score(b):  # probability * value
        return (b["consensus"]/100.0) * (b["odds"] - 1.0)
    return max(candidates, key=score)

class StakeButtons(discord.ui.View):
    def __init__(self, bet: dict, timeout: int = 120):
        super().__init__(timeout=timeout)
        self.bet = bet

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.secondary, emoji="💵")
    async def cons(self, interaction: Interaction, button: discord.ui.Button):
        await log_user_bet(interaction, self.bet, "conservative", self.bet["cons_stake"])

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def smart(self, interaction: Interaction, button: discord.ui.Button):
        await log_user_bet(interaction, self.bet, "smart", self.bet["smart_stake"])

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def aggressive(self, interaction: Interaction, button: discord.ui.Button):
        await log_user_bet(interaction, self.bet, "aggressive", self.bet["agg_stake"])

def format_bet_embed(b: dict, title: str) -> Embed:
    label, col = value_indicator(b)
    em = Embed(title=title, colour=col)
    em.add_field(name="", value=f"{b['emoji']} **{b['sport']} ({b['league']})**", inline=False)
    em.add_field(name="Match", value=b["match"], inline=False)
    em.add_field(name="Pick", value=f"{b['team']} @ {b['odds']}", inline=False)
    em.add_field(name="Bookmaker", value=b["bookmaker"], inline=False)
    em.add_field(name="Consensus %", value=f"{b['consensus']}%", inline=True)
    em.add_field(name="Implied %", value=f"{b['implied']}%", inline=True)
    em.add_field(name="Edge", value=f"{b['edge']}%", inline=True)
    em.add_field(name="Time", value=b["bet_time"].strftime("%d/%m/%y %H:%M"), inline=False)
    em.add_field(name="💵 Conservative Stake", value=f"{b['cons_stake']}u → Payout: {(b['cons_stake']*b['odds']):.2f}u | Exp. Profit: {b['cons_exp_profit']:.2f}u", inline=False)
    em.add_field(name="🧠 Smart Stake", value=f"{b['smart_stake']}u → Payout: {(b['smart_stake']*b['odds']):.2f}u | Exp. Profit: {b['smart_exp_profit']:.2f}u", inline=False)
    em.add_field(name="🔥 Aggressive Stake", value=f"{b['agg_stake']}u → Payout: {(b['agg_stake']*b['odds']):.2f}u | Exp. Profit: {b['agg_exp_profit']:.2f}u", inline=False)
    em.description = label
    return em

async def post_one(channel_id: int, bet: dict, title: str, category: str):
    if channel_id <= 0:
        return
    ch = bot.get_channel(channel_id)
    if not ch:
        return
    view = StakeButtons(bet) if pool else None  # buttons only if DB present
    await ch.send(embed=format_bet_embed(bet, title), view=view)
    await save_bet_row({**bet, "category": category})

async def post_bets(bets: List[dict]):
    if not bets:
        ch = bot.get_channel(BEST_BETS_CHANNEL)
        if ch:
            await ch.send("⚠️ No bets this cycle.")
        return

    # Best bet: must be value
    best = best_bet_selection(bets)
    if best and bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        await post_one(BEST_BETS_CHANNEL, best, "⭐ Best Bet", "best")

    # Quick
    for b in [x for x in bets if x["quick_return"]]:
        if bet_id(b) in posted_bets:
            continue
        posted_bets.add(bet_id(b))
        await post_one(QUICK_RETURNS_CHANNEL, b, "⏱ Quick Return Bet", "quick")
        # Duplicate value bets to the value channel
        if b["edge"] >= EDGE_VALUE_THRESHOLD and VALUE_BETS_CHANNEL:
            await post_one(VALUE_BETS_CHANNEL, b, "✅ Value Bet (Testing)", "quick")

    # Long
    for b in [x for x in bets if x["long_play"]]:
        if bet_id(b) in posted_bets:
            continue
        posted_bets.add(bet_id(b))
        await post_one(LONG_PLAYS_CHANNEL, b, "📅 Longer Play Bet", "long")
        if b["edge"] >= EDGE_VALUE_THRESHOLD and VALUE_BETS_CHANNEL:
            await post_one(VALUE_BETS_CHANNEL, b, "✅ Value Bet (Testing)", "long")

# ---------- Analytics ----------
async def compute_roi(strategy: Optional[str] = None) -> Tuple[float, int]:
    if not pool:
        return 0.0, 0
    base = """
        SELECT (cons_exp_profit+smart_exp_profit+agg_exp_profit) AS exp_profit,
               (cons_stake_units+smart_stake_units+agg_stake_units) AS stake
        FROM bets
    """
    params: List = []
    if strategy and strategy.lower() in {"best","quick","long"}:
        base += " WHERE category = $1"
        params.append(strategy.lower())
    async with pool.acquire() as con:
        rows = await con.fetch(base, *params)
    total_profit = sum(float(r["exp_profit"] or 0) for r in rows)
    total_stake  = sum(float(r["stake"] or 0) for r in rows)
    if total_stake <= 0:
        return 0.0, len(rows)
    return (total_profit / total_stake) * 100.0, len(rows)

async def user_stats(user_id: int) -> Tuple[int, float, float]:
    """#bets, total units staked, most-used strategy fraction"""
    if not pool:
        return 0, 0.0, 0.0
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT strategy, stake_units FROM user_bets WHERE user_id=$1", str(user_id))
    n = len(rows)
    staked = sum(float(r["stake_units"] or 0) for r in rows)
    counts: Dict[str,int] = {}
    for r in rows:
        counts[str(r["strategy"])] = counts.get(str(r["strategy"]), 0) + 1
    frac = 0.0
    if n>0:
        frac = max(counts.values())/n
    return n, staked, frac

# ---------- Slash commands ----------
@app_commands.command(name="ping", description="Latency check")
async def ping(interaction: Interaction):
    await interaction.response.send_message("🏓 Pong!", ephemeral=True)

@app_commands.command(name="roi", description="Show expected ROI (all or per strategy: best/quick/long)")
@app_commands.describe(strategy="Optional: best, quick, or long")
async def roi_cmd(interaction: Interaction, strategy: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not pool:
        await interaction.followup.send("📦 Database not configured; ROI unavailable.", ephemeral=True)
        return
    try:
        roi, n = await compute_roi(strategy)
        sfx = f" for **{strategy}**" if strategy else ""
        await interaction.followup.send(f"📈 Expected ROI{sfx}: **{roi:.2f}%** (from **{n}** posted bets).", ephemeral=True)
    except Exception as e:
        log.exception("ROI error")
        await interaction.followup.send(f"ROI failed: {e}", ephemeral=True)

@app_commands.command(name="stats", description="Your paper-trading stats (bets you logged via buttons).")
async def stats_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not pool:
        await interaction.followup.send("📦 Database not configured; no personal stats available.", ephemeral=True)
        return
    n, staked, top_frac = await user_stats(interaction.user.id)
    if n == 0:
        await interaction.followup.send("You haven't logged any bets yet. Tap the buttons on a card to log one!", ephemeral=True)
        return
    await interaction.followup.send(
        f"🧾 **Your stats:**\n• Bets logged: **{n}**\n• Units staked: **{staked:.2f}u**\n• Most-used strategy share: **{top_frac*100:.0f}%**",
        ephemeral=True
    )

@app_commands.command(name="fetchbets", description="Force pull & post qualifying bets now.")
async def fetchbets_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if FETCH_LOCK.locked():
        await interaction.followup.send("Another fetch is running—try again shortly.", ephemeral=True)
        return
    async with FETCH_LOCK:
        try:
            data = await asyncio.wait_for(fetch_odds(), timeout=25)
        except Exception as e:
            await interaction.followup.send(f"Fetch failed: {e}", ephemeral=True)
            return
        bets = calculate_bets(data)
        try:
            await post_bets(bets)
        except Exception as e:
            log.exception("post_bets error")
            await interaction.followup.send(f"Fetched {len(bets)} bets, but posting failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"Fetched {len(bets)} candidate bets and posted qualifying ones.", ephemeral=True)

# ---------- Loop ----------
@tasks.loop(minutes=10)
async def bet_loop():
    if FETCH_LOCK.locked():
        return
    async with FETCH_LOCK:
        try:
            data = await asyncio.wait_for(fetch_odds(), timeout=25)
        except Exception as e:
            log.warning(f"Loop fetch error: {e}")
            return
        bets = calculate_bets(data)
        try:
            await post_bets(bets)
        except Exception:
            log.exception("Loop post_bets failed")

# ---------- Events ----------
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # Init DB (optional but enables buttons/ROI/stats)
    try:
        await init_db()
    except Exception:
        log.exception("DB init failed (continuing without DB).")
    try:
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash sync failed.")
    if not bet_loop.is_running():
        bet_loop.start()

if not TOKEN:
    raise SystemExit("❌ DISCORD_BOT_TOKEN missing.")
bot.run(TOKEN)













