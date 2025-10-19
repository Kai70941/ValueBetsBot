# bot.py
import os
import re
import math
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import requests
import discord
from discord.ext import commands, tasks

# --- Optional DB (paper trading) ---
DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:
    psycopg2 = None
    RealDictCursor = None

# --------------- Config from env ---------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_CHANNEL_ID = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Betting bank (units, not dollar)
BANKROLL_UNITS = 1000
CONSERVATIVE_PCT = 0.015  # 1.5% of bankroll for cons. stake
SMART_BASE_UNITS = 5      # baseline for "smart" stake

# thresholds
EDGE_VALUE_THRESHOLD = 2.0  # percentage points edge to call "Value"

# --------------- Discord setup ---------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Keep track of sent bets to avoid duplicates by channel
posted_by_channel = {
    "quick": set(),
    "long": set(),
    "best": set(),
    "value": set(),  # duplicates to value channel
}

# --------------- Bookmaker allow-list (exact 9) ---------------
# Robust matcher: case-insensitive, strips non-alphanumerics, matches variants like "betfairexchange" or "tabau"
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab",
]
def _allowed_bookmaker(title: str) -> bool:
    if not title:
        return False
    t = re.sub(r"[^a-z0-9]+", "", title.lower())
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

# --------------- Helpers ---------------

def sport_from_title(sport_title: str) -> str:
    """Normalize sport name from TheOddsAPI sport_title."""
    if not sport_title:
        return "Unknown"
    s = sport_title.lower()
    if "soccer" in s or "football (soccer)" in s:
        return "Soccer"
    if "american football" in s or "nfl" in s or "ncaa football" in s:
        return "American Football"
    if "basketball" in s:
        return "Basketball"
    if "tennis" in s:
        return "Tennis"
    if "baseball" in s or "mlb" in s:
        return "Baseball"
    if "ice hockey" in s or "nhl" in s:
        return "Ice Hockey"
    if "mma" in s or "ufc" in s:
        return "MMA"
    if "cricket" in s:
        return "Cricket"
    if "rugby" in s:
        return "Rugby"
    if "aussie rules" in s or "afl" in s:
        return "Aussie Rules"
    return sport_title  # fallback nicely

SPORT_EMOJI = {
    "Soccer": "⚽",
    "American Football": "🏈",
    "Basketball": "🏀",
    "Tennis": "🎾",
    "Baseball": "⚾",
    "Ice Hockey": "🏒",
    "MMA": "🥊",
    "Cricket": "🏏",
    "Rugby": "🏉",
    "Aussie Rules": "🏉",
}
def sport_emoji(name: str) -> str:
    return SPORT_EMOJI.get(name, "🎲")

def safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def bet_key(b: dict) -> str:
    return f"{b.get('match')}|{b.get('team')}|{b.get('bookmaker')}|{b.get('time')}"

# --------------- Odds Fetch & Build ---------------

def fetch_odds():
    """Fetch upcoming odds from TheOddsAPI."""
    if not ODDS_API_KEY:
        logging.warning("ODDS_API_KEY missing.")
        return []
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal",
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"Odds API error: {e}")
        return []

def calculate_bets(data):
    """Create bet candidates with edges, stakes, etc. Only from your 9 bookmakers."""
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue
        match_name = f"{home} vs {away}"

        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except Exception:
            continue

        # Only within next ~150 days
        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # Build consensus using only allowed bookmakers
        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            btitle = (book.get("title") or "").strip()
            if not _allowed_bookmaker(btitle):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("price") and outcome.get("name"):
                        key = f"{market['key']}:{outcome['name']}"
                        try:
                            price = float(outcome["price"])
                            if price <= 1.0:
                                continue
                            consensus_by_outcome[key].append(1.0 / price)
                        except Exception:
                            continue

        if not consensus_by_outcome:
            continue

        global_consensus = (
            sum(p for lst in consensus_by_outcome.values() for p in lst) /
            max(1, sum(len(lst) for lst in consensus_by_outcome.values()))
        )

        # sport + league line
        sport_title = event.get("sport_title") or ""
        sport = sport_from_title(sport_title)
        league = sport_title or "Unknown League"

        # Create one bet per allowed bookmaker outcome
        for book in event.get("bookmakers", []):
            btitle = (book.get("title") or "Unknown Bookmaker").strip()
            if not _allowed_bookmaker(btitle):
                continue

            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = safe_float(outcome.get("price"), 0.0)
                    name  = outcome.get("name")
                    if not name or price <= 1.0:
                        continue

                    implied_p = 1.0 / price
                    outcome_key = f"{market['key']}:{name}"
                    consensus_p = (
                        sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key])
                        if outcome_key in consensus_by_outcome else global_consensus
                    )
                    edge = (consensus_p - implied_p) * 100.0  # in percentage points

                    if edge <= 0:
                        continue

                    # stakes (units)
                    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)         # ~15.0u
                    smart_units = round(max(1.0, min(100.0, SMART_BASE_UNITS * (1.0 + edge / 5.0))), 2)
                    aggr_units  = round(cons_units * (1.0 + edge / 10.0), 2)

                    cons_payout = round(cons_units * price, 2)
                    smart_payout = round(smart_units * price, 2)
                    aggr_payout = round(aggr_units * price, 2)

                    cons_exp = round(consensus_p * cons_payout - cons_units, 2)
                    smart_exp = round(consensus_p * smart_payout - smart_units, 2)
                    aggr_exp = round(consensus_p * aggr_payout - aggr_units, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": btitle,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "probability": round(implied_p * 100.0, 2),
                        "consensus": round(consensus_p * 100.0, 2),
                        "edge": round(edge, 2),

                        "cons_units": cons_units,
                        "smart_units": smart_units,
                        "aggr_units": aggr_units,

                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "aggr_payout": aggr_payout,

                        "cons_exp_profit": cons_exp,
                        "smart_exp_profit": smart_exp,
                        "aggr_exp_profit": aggr_exp,

                        # classification flags
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),

                        # value flag
                        "is_value": edge >= EDGE_VALUE_THRESHOLD,

                        # sport/league
                        "sport": sport,
                        "league": league,
                    })

    return bets

def format_bet(b: dict, title: str, color: int) -> discord.Embed:
    """Card layout with sport + league line and units."""
    indicator = "🟢 Value Bet" if b.get("edge", 0) >= EDGE_VALUE_THRESHOLD else "🔴 Low Value"
    s_emoji = sport_emoji(b.get("sport", ""))
    sport_line = f"{s_emoji} {b.get('sport','Unknown')} ({b.get('league','Unknown League')})"

    desc = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 **Conservative Stake:** {b['cons_units']:.2f}u → Payout: {b['cons_payout']:.2f}u | Exp. Profit: {b['cons_exp_profit']:.2f}u\n"
        f"🧠 **Smart Stake:** {b['smart_units']:.2f}u → Payout: {b['smart_payout']:.2f}u | Exp. Profit: {b['smart_exp_profit']:.2f}u\n"
        f"🔥 **Aggressive Stake:** {b['aggr_units']:.2f}u → Payout: {b['aggr_payout']:.2f}u | Exp. Profit: {b['aggr_exp_profit']:.2f}u\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

# --------------- DB for paper trading (stubs kept) ---------------

def init_db():
    if not DB_URL or not psycopg2:
        return
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    match TEXT,
                    bookmaker TEXT,
                    team TEXT,
                    odds NUMERIC,
                    edge NUMERIC,
                    bet_time TIMESTAMP,
                    category TEXT,
                    sport TEXT,
                    league TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                """)
    except Exception as e:
        logging.error(f"DB init error: {e}")

def save_bet_to_db(b: dict, category: str):
    if not DB_URL or not psycopg2:
        return
    try:
        with psycopg2.connect(DB_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, sport, league)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        b.get("match"),
                        b.get("bookmaker"),
                        b.get("team"),
                        b.get("odds"),
                        b.get("edge"),
                        b.get("time"),
                        category,
                        b.get("sport"),
                        b.get("league"),
                    )
                )
    except Exception as e:
        logging.error(f"DB save error: {e}")

# --------------- Value-channel duplication helpers ---------------

async def get_value_channel():
    """Return the Value Bets channel, fetching if not cached."""
    if not VALUE_BETS_CHANNEL_ID:
        return None
    ch = bot.get_channel(VALUE_BETS_CHANNEL_ID)
    if ch is None:
        try:
            ch = await bot.fetch_channel(VALUE_BETS_CHANNEL_ID)
        except Exception as e:
            print(f"⚠️ fetch_channel failed for VALUE_BETS_CHANNEL_ID={VALUE_BETS_CHANNEL_ID}: {e}")
            ch = None
    return ch

async def duplicate_to_value_channel(b: dict):
    """Send a duplicate card for value bets to the Value Bets channel (once)."""
    if not b.get("is_value") or not VALUE_BETS_CHANNEL_ID:
        return
    k = bet_key(b)
    if k in posted_by_channel["value"]:
        return
    vchan = await get_value_channel()
    if not vchan:
        print("⚠️ Value Bets channel not found or no permission. Check VALUE_BETS_CHANNEL_ID and perms.")
        return
    await vchan.send(embed=format_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71))
    posted_by_channel["value"].add(k)

# --------------- Posting flow ---------------

async def post_bets(bets):
    if not bets:
        return

    # choose best by a blend: higher consensus and higher edge
    best = max(bets, key=lambda x: (x["consensus"], x["edge"])) if bets else None

    # channels
    bchan = bot.get_channel(BEST_BETS_CHANNEL) if BEST_BETS_CHANNEL else None
    qchan = bot.get_channel(QUICK_RETURNS_CHANNEL) if QUICK_RETURNS_CHANNEL else None
    lchan = bot.get_channel(LONG_PLAYS_CHANNEL) if LONG_PLAYS_CHANNEL else None

    # BEST
    if best and bchan:
        k = bet_key(best)
        if k not in posted_by_channel["best"]:
            await bchan.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))
            posted_by_channel["best"].add(k)
            save_bet_to_db(best, "best")
        await duplicate_to_value_channel(best)

    # QUICK
    quick = [b for b in bets if b["quick_return"]]
    if qchan:
        for b in quick[:5]:
            k = bet_key(b)
            if k in posted_by_channel["quick"]:
                continue
            await qchan.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))
            posted_by_channel["quick"].add(k)
            save_bet_to_db(b, "quick")
            await duplicate_to_value_channel(b)

    # LONG
    long_plays = [b for b in bets if b["long_play"]]
    if lchan:
        for b in long_plays[:5]:
            k = bet_key(b)
            if k in posted_by_channel["long"]:
                continue
            await lchan.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))
            posted_by_channel["long"].add(k)
            save_bet_to_db(b, "long")
            await duplicate_to_value_channel(b)

# --------------- Tasks & Commands ---------------

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")

    init_db()
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=60)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# ---- Slash commands ----

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong! ✅", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Manually fetch & preview top 3 upcoming edges (ephemeral).")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = fetch_odds()
    bets = calculate_bets(data)
    bets = sorted(bets, key=lambda x: (-x["edge"], -x["consensus"]))[:3]
    if not bets:
        await interaction.followup.send("No bets found right now.", ephemeral=True)
        return
    lines = []
    for b in bets:
        lines.append(f"**{b['match']}** — *{b['team']}* @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%")
    await interaction.followup.send("🎲 **Bets Preview:**\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(name="valuechannel", description="Show configured Value Bets channel and access status.")
async def valuechannel_cmd(interaction: discord.Interaction):
    ch = await get_value_channel()
    if ch:
        await interaction.response.send_message(f"Value Bets channel resolved: {ch.mention} (ID {ch.id})", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Could not resolve Value Bets channel from ID `{VALUE_BETS_CHANNEL_ID}`. "
            "Check env var and bot permissions.", ephemeral=True
        )

# --------------- Main ---------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    bot.run(TOKEN)














