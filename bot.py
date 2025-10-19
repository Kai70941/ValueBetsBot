import os
import re
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple, Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction, Embed, Colour

import aiohttp

# Try to import asyncpg, but don't crash if it's missing.
try:
    import asyncpg  # type: ignore
    ASYNCPG_AVAILABLE = True
except Exception:
    asyncpg = None
    ASYNCPG_AVAILABLE = False

# ------------- CONFIG -------------

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0") or 0)
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0") or 0)
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0") or 0)
VALUE_BETS_CHANNEL = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0") or 0)
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# bankroll & unit settings (units instead of dollar amounts)
BANKROLL_UNITS = 1000
CONSERVATIVE_PCT = 0.015  # 1.5% of units

# bookmakers we allow (lowercased substring match)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

EDGE_VALUE_THRESHOLD = 2.0  # % edge to label as "Value"

# HTTP
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20, connect=10)
SESSION: Optional[aiohttp.ClientSession] = None
FETCH_LOCK = asyncio.Lock()

# Track posted bets to avoid duplicate spam
posted_bets = set()

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("valuebets")

# SPORT helpers
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

# HTTP client
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
                log.warning(f"Odds API non-200 {resp.status}: {body[:300]}")
                return []
            return await resp.json()
    except asyncio.TimeoutError:
        log.warning("Odds API timeout")
    except Exception as e:
        log.exception(f"Odds API error: {e}")
    return []

# --------- DB (optional) ----------
pool = None

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

async def init_db():
    global pool
    if not ASYNCPG_AVAILABLE or not DATABASE_URL:
        log.warning("DB disabled (asyncpg missing or DATABASE_URL unset).")
        return
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=4)
    async with pool.acquire() as con:
        await con.execute(CREATE_BETS_TABLE)
    log.info("DB ready.")

async def save_bet_row(row: dict):
    if not ASYNCPG_AVAILABLE or not pool:
        return
    q = """
        INSERT INTO bets (event_id, match, bookmaker, team, odds, consensus, implied, edge,
                          cons_stake_units, smart_stake_units, agg_stake_units,
                          cons_exp_profit, smart_exp_profit, agg_exp_profit,
                          bet_time, category, sport, league)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,
                $9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
    """
    async with pool.acquire() as con:
        await con.execute(
            q,
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

# ---------- BET LOGIC ----------

def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

def calculate_bets(data: List[dict]) -> List[dict]:
    now = datetime.now(timezone.utc)
    bets: List[dict] = []

    for event in data:
        home = event.get("home_team") or "Home"
        away = event.get("away_team") or "Away"
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat((commence_time or "").replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        consensus_by_outcome = {}
        counts_by_outcome = {}

        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name")
                    price = outcome.get("price")
                    if not name or not price:
                        continue
                    key = f"{market.get('key')}:{name}"
                    consensus_by_outcome[key] = consensus_by_outcome.get(key, 0.0) + (1.0 / float(price))
                    counts_by_outcome[key] = counts_by_outcome.get(key, 0) + 1

        if not consensus_by_outcome:
            continue

        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not _allowed_bookmaker(title):
                continue
            for market in book.get("markets", []):
                key_base = market.get("key", "h2h")
                for outcome in market.get("outcomes", []):
                    name = outcome.get("name")
                    price = outcome.get("price")
                    if not name or not price:
                        continue

                    out_key = f"{key_base}:{name}"
                    if out_key not in consensus_by_outcome:
                        continue

                    consensus_p = consensus_by_outcome[out_key] / max(1, counts_by_outcome[out_key])
                    implied_p = 1.0 / float(price)
                    edge = (consensus_p - implied_p) * 100.0  # %

                    cons_stake = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
                    smart_stake = round(min(cons_stake * (1 + max(0.0, edge) / 50.0), cons_stake * 5), 2)
                    agg_stake = round(min(cons_stake * (1 + max(0.0, edge) / 20.0), cons_stake * 15), 2)

                    cons_payout = round(cons_stake * float(price), 2)
                    smart_payout = round(smart_stake * float(price), 2)
                    agg_payout = round(agg_stake * float(price), 2)

                    cons_exp_profit = round(consensus_p * cons_payout - cons_stake, 2)
                    smart_exp_profit = round(consensus_p * smart_payout - smart_stake, 2)
                    agg_exp_profit = round(consensus_p * agg_payout - agg_stake, 2)

                    emoji, sport_name, league = sport_and_league(event)

                    bets.append({
                        "event_id": event.get("id"),
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": float(price),
                        "consensus": round(consensus_p * 100, 2),
                        "implied": round(implied_p * 100, 2),
                        "edge": round(edge, 2),
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_exp_profit": cons_exp_profit,
                        "smart_exp_profit": smart_exp_profit,
                        "agg_exp_profit": agg_exp_profit,
                        "bet_time": commence_dt,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "sport": sport_name,
                        "league": league,
                        "emoji": emoji,
                        "event": event
                    })

    return bets

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
    def score(b): return (b["consensus"] / 100.0) * (b["odds"] - 1.0)
    return max(candidates, key=score)

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
    em.add_field(
        name="💵 Conservative Stake",
        value=f"{b['cons_stake']}u → Payout: {(b['cons_stake']*b['odds']):.2f}u | Exp. Profit: {b['cons_exp_profit']:.2f}u",
        inline=False
    )
    em.add_field(
        name="🧠 Smart Stake",
        value=f"{b['smart_stake']}u → Payout: {(b['smart_stake']*b['odds']):.2f}u | Exp. Profit: {b['smart_exp_profit']:.2f}u",
        inline=False
    )
    em.add_field(
        name="🔥 Aggressive Stake",
        value=f"{b['agg_stake']}u → Payout: {(b['agg_stake']*b['odds']):.2f}u | Exp. Profit: {b['agg_exp_profit']:.2f}u",
        inline=False
    )
    em.description = label
    return em

async def post_bets(bets: List[dict]):
    if not bets:
        ch = bot.get_channel(BEST_BETS_CHANNEL)
        if ch:
            await ch.send("⚠️ No bets this cycle.")
        return

    best = best_bet_selection(bets)
    if best and bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        ch = bot.get_channel(BEST_BETS_CHANNEL)
        if ch:
            await ch.send(embed=format_bet_embed(best, "⭐ Best Bet"))
        await save_bet_row({**best, "category": "best"})

    qch = bot.get_channel(QUICK_RETURNS_CHANNEL)
    for b in [x for x in bets if x["quick_return"]]:
        if bet_id(b) in posted_bets:
            continue
        posted_bets.add(bet_id(b))
        if qch:
            await qch.send(embed=format_bet_embed(b, "⏱ Quick Return Bet"))
        await save_bet_row({**b, "category": "quick"})

    lch = bot.get_channel(LONG_PLAYS_CHANNEL)
    for b in [x for x in bets if x["long_play"]]:
        if bet_id(b) in posted_bets:
            continue
        posted_bets.add(bet_id(b))
        if lch:
            await lch.send(embed=format_bet_embed(b, "📅 Longer Play Bet"))
        await save_bet_row({**b, "category": "long"})

    vch = bot.get_channel(VALUE_BETS_CHANNEL)
    if vch:
        for b in bets:
            if b["edge"] >= EDGE_VALUE_THRESHOLD and bet_id(b) in posted_bets:
                await vch.send(embed=format_bet_embed(b, "✅ Value Bet"))

# ROI
async def compute_roi(strategy: Optional[str] = None) -> Tuple[float, int]:
    if not ASYNCPG_AVAILABLE or not pool:
        return 0.0, 0
    base = "SELECT cons_exp_profit+smart_exp_profit+agg_exp_profit AS exp_profit," \
           " cons_stake_units+smart_stake_units+agg_stake_units AS stake FROM bets"
    cond = ""
    params = []
    if strategy and strategy.lower() in {"best", "quick", "long"}:
        cond = " WHERE category = $1"
        params.append(strategy.lower())
    async with pool.acquire() as con:
        rows = await con.fetch(base + cond, *params)
    total_profit = sum(float(r["exp_profit"] or 0) for r in rows)
    total_stake = sum(float(r["stake"] or 0) for r in rows)
    if total_stake <= 0:
        return 0.0, len(rows)
    roi = (total_profit / total_stake) * 100.0
    return roi, len(rows)

# Commands
@app_commands.command(name="ping", description="Latency check")
async def ping(interaction: Interaction):
    await interaction.response.send_message("🏓 Pong!", ephemeral=True)

@app_commands.command(name="roi", description="Show expected ROI (all or per strategy).")
@app_commands.describe(strategy="Optional: best, quick, or long")
async def roi(interaction: Interaction, strategy: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not ASYNCPG_AVAILABLE or not pool:
        await interaction.followup.send("📦 Database not configured; ROI unavailable.", ephemeral=True)
        return
    try:
        roi_value, n = await compute_roi(strategy)
        sfx = f" for **{strategy}**" if strategy else ""
        await interaction.followup.send(
            f"📈 Expected ROI{sfx}: **{roi_value:.2f}%** based on **{n}** posted bets.",
            ephemeral=True
        )
    except Exception as e:
        log.exception("ROI failed")
        await interaction.followup.send(f"ROI failed: {e}", ephemeral=True)

@app_commands.command(name="fetchbets", description="Force pull & post qualifying bets.")
async def fetchbets(interaction: Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)
    if FETCH_LOCK.locked():
        await interaction.followup.send("Another fetch is running—try again shortly.", ephemeral=True)
        return
    async with FETCH_LOCK:
        try:
            data = await asyncio.wait_for(fetch_odds(), timeout=25)
        except asyncio.TimeoutError:
            await interaction.followup.send("Odds API timed out. Try again.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Fetch failed: {e}", ephemeral=True)
            return
        bets = calculate_bets(data)
        try:
            await post_bets(bets)
        except Exception as e:
            log.exception("post_bets failed")
            await interaction.followup.send(f"Fetched {len(bets)} bets but posting failed: {e}", ephemeral=True)
            return
        await interaction.followup.send(f"Fetched {len(bets)} bets and posted qualifying ones.", ephemeral=True)

# Loop
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

# Events
@bot.event
async def on_ready():
    log.info(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await init_db()
    except Exception:
        log.exception("DB init failed")
    try:
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash sync failed")
    if not bet_loop.is_running():
        bet_loop.start()

@bot.event
async def on_close():
    global SESSION
    if SESSION and not SESSION.closed:
        await SESSION.close()

if not TOKEN:
    raise SystemExit("❌ DISCORD_BOT_TOKEN is missing.")
bot.run(TOKEN)














