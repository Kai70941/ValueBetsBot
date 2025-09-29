import os
import discord
from discord.ext import commands, tasks
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ---------------------------
# Config
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))  # Value Bets testing channel
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL")

BANKROLL = 1000
CONSERVATIVE_PCT = 0.015

# ---------------------------
# Sport Emojis
# ---------------------------
SPORT_EMOJIS = {
    "soccer": "⚽",
    "basketball": "🏀",
    "baseball": "⚾",
    "americanfootball": "🏈",
    "icehockey": "🏒",
    "tennis": "🎾",
    "golf": "⛳",
    "boxing": "🥊",
    "mma": "🥋",
    "rugby": "🏉",
    "cricket": "🏏",
    "default": "🎲"
}

# ---------------------------
# Normalization Helpers
# ---------------------------
def normalize_base(text: str) -> str:
    return (text or "").lower().replace("-", "").replace("_", "").replace(" ", "")

def split_sport_title(sport_title: str):
    """
    The Odds API usually returns sport_title like 'Soccer - Brazil Série B'.
    We split that into ('Soccer', 'Brazil Série B').
    If no hyphen, we treat the whole thing as sport name and league unknown.
    """
    title = sport_title or ""
    if " - " in title:
        sport_name, league = title.split(" - ", 1)
    else:
        sport_name, league = (title, "")
    sport_name = sport_name.strip()
    league = league.strip()
    return sport_name, league

def normalize_sport(sport_key: str, sport_title: str):
    """
    Returns (base_key_for_emoji, pretty_sport_name, league_name)
    E.g. ('soccer', 'Soccer', 'Brazil Série B')
    """
    # Prefer parsing from sport_title because it's more consistent for display
    sport_name_from_title, league = split_sport_title(sport_title)
    base_from_title = normalize_base(sport_name_from_title)

    # Try from title first
    mapping = {
        "soccer": ("soccer", "Soccer"),
        "basketball": ("basketball", "Basketball"),
        "baseball": ("baseball", "Baseball"),
        "americanfootball": ("americanfootball", "American Football"),
        "footballamerican": ("americanfootball", "American Football"),
        "icehockey": ("icehockey", "Ice Hockey"),
        "hockeyice": ("icehockey", "Ice Hockey"),
        "tennis": ("tennis", "Tennis"),
        "golf": ("golf", "Golf"),
        "boxing": ("boxing", "Boxing"),
        "mma": ("mma", "MMA"),
        "rugby": ("rugby", "Rugby"),
        "cricket": ("cricket", "Cricket"),
    }
    for keynorm, (base, pretty) in mapping.items():
        if keynorm in base_from_title:
            return base, pretty, league

    # Fallback: try from sport_key (e.g., 'soccer_brazil_serie_b')
    keynorm = normalize_base(sport_key)
    # Check a few common patterns:
    if keynorm.startswith("soccer") or "soccer" in keynorm:
        return "soccer", "Soccer", league
    if keynorm.startswith("basketball") or "basketball" in keynorm or "nba" in keynorm:
        return "basketball", "Basketball", league
    if keynorm.startswith("americanfootball") or "nfl" in keynorm:
        return "americanfootball", "American Football", league
    if keynorm.startswith("icehockey") or "icehockey" in keynorm or "nhl" in keynorm:
        return "icehockey", "Ice Hockey", league
    if "baseball" in keynorm or "mlb" in keynorm:
        return "baseball", "Baseball", league
    if "tennis" in keynorm:
        return "tennis", "Tennis", league
    if "rugby" in keynorm:
        return "rugby", "Rugby", league
    if "mma" in keynorm or "ufc" in keynorm:
        return "mma", "MMA", league
    if "boxing" in keynorm:
        return "boxing", "Boxing", league
    if "cricket" in keynorm:
        return "cricket", "Cricket", league
    if "golf" in keynorm:
        return "golf", "Golf", league

    # Default
    return "default", sport_name_from_title or "Sport", league

# ---------------------------
# Database Helpers
# ---------------------------
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def init_db():
    with get_db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    match TEXT,
                    bookmaker TEXT,
                    team TEXT,
                    odds FLOAT,
                    edge FLOAT,
                    bet_time TIMESTAMP,
                    category TEXT,
                    sport TEXT,
                    league TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
        conn.commit()

def save_bet_to_db(bet):
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, sport, league)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    bet["match"], bet["bookmaker"], bet["team"], bet["odds"], bet["edge"],
                    bet["time_dt"], bet["category"], bet.get("sport_friendly") or bet.get("sport"),
                    bet.get("league")
                ))
            conn.commit()
    except Exception as e:
        print(f"❌ Failed to save bet: {e}")

# ---------------------------
# Bot Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
posted_bets = set()

# ---------------------------
# Odds API
# ---------------------------
def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

# ---------------------------
# Betting Logic
# ---------------------------
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

def calculate_bets(data):
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue

        match_name = f"{home} vs {away}"

        sport_key = event.get("sport_key", "")
        sport_title = event.get("sport_title", "")
        base_sport, sport_friendly, league = normalize_sport(sport_key, sport_title)

        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("price") and outcome.get("name"):
                        key = f"{market['key']}:{outcome['name']}"
                        consensus_by_outcome[key].append(1 / outcome["price"])

        if not consensus_by_outcome:
            continue

        total_probs = sum(len(plist) for plist in consensus_by_outcome.values())
        global_consensus = sum(p for plist in consensus_by_outcome.values() for p in plist) / max(1, total_probs)

        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not _allowed_bookmaker(title):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price, name = outcome.get("price"), outcome.get("name")
                    if not price or not name:
                        continue

                    implied_p = 1 / price
                    outcome_key = f"{market['key']}:{name}"
                    consensus_p = (
                        sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key])
                        if outcome_key in consensus_by_outcome else global_consensus
                    )
                    edge = consensus_p - implied_p
                    if edge <= 0:
                        continue

                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)
                    # Smart stake: middle ground — a third of conservative
                    smart_stake = round(cons_stake / 3, 2)
                    agg_stake = round(cons_stake * (1 + (edge * 100)), 2)

                    cons_payout = round(cons_stake * price, 2)
                    smart_payout = round(smart_stake * price, 2)
                    agg_payout = round(agg_stake * price, 2)

                    cons_exp_profit = round(consensus_p * cons_payout - cons_stake, 2)
                    smart_exp_profit = round(consensus_p * smart_payout - smart_stake, 2)
                    agg_exp_profit = round(consensus_p * agg_payout - agg_stake, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "time_dt": commence_dt,
                        "probability": round(implied_p * 100, 2),
                        "consensus": round(consensus_p * 100, 2),
                        "edge": round(edge * 100, 2),
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "agg_payout": agg_payout,
                        "cons_exp_profit": cons_exp_profit,
                        "smart_exp_profit": smart_exp_profit,
                        "agg_exp_profit": agg_exp_profit,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "category": "quick" if delta <= timedelta(hours=48) else "long",
                        "sport": base_sport,                # for emoji lookup
                        "sport_friendly": sport_friendly,   # pretty ("Soccer")
                        "league": league or "Unknown League"
                    })
    return bets

# ---------------------------
# Bet Formatting
# ---------------------------
def format_bet(b, title, color):
    indicator = "🟢 Value Bet" if b['edge'] >= 2 else "🛑 Low Value"
    emoji = SPORT_EMOJIS.get(b.get("sport"), SPORT_EMOJIS["default"])
    sport_line = f"{emoji} {b.get('sport_friendly', 'Sport')} ({b.get('league', 'Unknown League')})"

    description = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 **Conservative Stake:** ${b['cons_stake']} → Payout: ${b['cons_payout']} | Exp. Profit: ${b['cons_exp_profit']}\n"
        f"🧠 **Smart Stake:** ${b['smart_stake']} → Payout: ${b['smart_payout']} | Exp. Profit: ${b['smart_exp_profit']}\n"
        f"🔥 **Aggressive Stake:** ${b['agg_stake']} → Payout: ${b['agg_payout']} | Exp. Profit: ${b['agg_exp_profit']}\n"
    )
    return discord.Embed(title=title, description=description, color=color)

def bet_id(b):
    return f"{b['match']}|{b['team']}|{b['bookmaker']}|{b['time']}"

# ---------------------------
# Posting Bets
# ---------------------------
async def post_bets(bets):
    if not bets:
        return

    # Best bet
    best = max(bets, key=lambda x: (x["consensus"], x["edge"]))
    if bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        channel = bot.get_channel(BEST_BETS_CHANNEL)
        if channel:
            await channel.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))
        # Also mirror to Value Bets channel if it's a value bet
        if VALUE_BETS_CHANNEL and best["edge"] >= 2:
            v_channel = bot.get_channel(VALUE_BETS_CHANNEL)
            if v_channel:
                await v_channel.send(embed=format_bet(best, "⭐ Value Bet", 0x2ECC71))
        save_bet_to_db(best)

    # Quick
    quick = [b for b in bets if b["quick_return"] and bet_id(b) not in posted_bets]
    q_channel = bot.get_channel(QUICK_RETURNS_CHANNEL)
    if q_channel:
        for b in quick[:5]:
            posted_bets.add(bet_id(b))
            await q_channel.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))
            if VALUE_BETS_CHANNEL and b["edge"] >= 2:
                v_channel = bot.get_channel(VALUE_BETS_CHANNEL)
                if v_channel:
                    await v_channel.send(embed=format_bet(b, "⭐ Value Bet", 0x2ECC71))
            save_bet_to_db(b)

    # Long
    long_plays = [b for b in bets if b["long_play"] and bet_id(b) not in posted_bets]
    l_channel = bot.get_channel(LONG_PLAYS_CHANNEL)
    if l_channel:
        for b in long_plays[:5]:
            posted_bets.add(bet_id(b))
            await l_channel.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))
            if VALUE_BETS_CHANNEL and b["edge"] >= 2:
                v_channel = bot.get_channel(VALUE_BETS_CHANNEL)
                if v_channel:
                    await v_channel.send(embed=format_bet(b, "⭐ Value Bet", 0x2ECC71))
            save_bet_to_db(b)

# ---------------------------
# Events
# ---------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")

    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=30)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# ---------------------------
# Start
# ---------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

init_db()
bot.run(TOKEN)












