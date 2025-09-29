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
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL")

BANKROLL = 1000
CONSERVATIVE_PCT = 0.015

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bets = set()

# ---------------------------
# Database Helpers
# ---------------------------
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def migrate_db():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE bets 
            ADD COLUMN IF NOT EXISTS consensus NUMERIC,
            ADD COLUMN IF NOT EXISTS implied NUMERIC,
            ADD COLUMN IF NOT EXISTS cons_stake NUMERIC,
            ADD COLUMN IF NOT EXISTS cons_payout NUMERIC,
            ADD COLUMN IF NOT EXISTS cons_profit NUMERIC,
            ADD COLUMN IF NOT EXISTS smart_stake NUMERIC,
            ADD COLUMN IF NOT EXISTS smart_payout NUMERIC,
            ADD COLUMN IF NOT EXISTS smart_profit NUMERIC,
            ADD COLUMN IF NOT EXISTS agg_stake NUMERIC,
            ADD COLUMN IF NOT EXISTS agg_payout NUMERIC,
            ADD COLUMN IF NOT EXISTS agg_profit NUMERIC,
            ADD COLUMN IF NOT EXISTS sport_key TEXT;
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Migration complete")

def save_bet(bet):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, created_at,
                              consensus, implied,
                              cons_stake, cons_payout, cons_profit,
                              smart_stake, smart_payout, smart_profit,
                              agg_stake, agg_payout, agg_profit,
                              sport_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(),
                    %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s)
        """, (
            bet["match"], bet["bookmaker"], bet["team"], bet["odds"], bet["edge"], bet["time"], bet["category"],
            bet["consensus"], bet["probability"],
            bet["cons_stake"], bet["cons_payout"], bet["cons_exp_profit"],
            bet["smart_stake"], bet["smart_payout"], bet["smart_exp_profit"],
            bet["agg_stake"], bet["agg_payout"], bet["agg_exp_profit"],
            bet["sport_key"]
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ Failed to save bet:", e)

# ---------------------------
# Odds Fetch + Bets
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
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

SPORT_EMOJIS = {
    "basketball": "🏀",
    "americanfootball": "🏈",
    "soccer": "⚽",
    "tennis": "🎾",
    "baseball": "⚾",
    "icehockey": "🏒",
    "mma": "🥊",
    "boxing": "🥊",
    "golf": "⛳",
    "cricket": "🏏",
    "default": "🎲"
}

def calculate_bets(data):
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        sport_key = event.get("sport_key", "default")

        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("price") and outcome.get("name"):
                        key = f"{market['key']}:{outcome['name']}"
                        consensus_by_outcome[key].append(1 / outcome["price"])

        if not consensus_by_outcome:
            continue
        global_consensus = sum(p for plist in consensus_by_outcome.values() for p in plist) / max(1, sum(len(plist) for plist in consensus_by_outcome.values()))

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price, name = outcome.get("price"), outcome.get("name")
                    if not price or not name:
                        continue
                    implied_p = 1 / price
                    outcome_key = f"{market['key']}:{name}"
                    consensus_p = sum(consensus_by_outcome[outcome_key]) / len(consensus_by_outcome[outcome_key]) if outcome_key in consensus_by_outcome else global_consensus
                    edge = round((consensus_p - implied_p) * 100, 2)
                    if edge <= 0:
                        continue

                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)
                    smart_stake = round(cons_stake * (edge / 10), 2)
                    agg_stake = round(cons_stake * (1 + edge / 100), 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": book.get("title", "Unknown"),
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "probability": round(implied_p * 100, 2),
                        "consensus": round(consensus_p * 100, 2),
                        "edge": edge,
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": round(cons_stake * price, 2),
                        "smart_payout": round(smart_stake * price, 2),
                        "agg_payout": round(agg_stake * price, 2),
                        "cons_exp_profit": round(consensus_p * cons_stake * price - cons_stake, 2),
                        "smart_exp_profit": round(consensus_p * smart_stake * price - smart_stake, 2),
                        "agg_exp_profit": round(consensus_p * agg_stake * price - agg_stake, 2),
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "category": "quick" if delta <= timedelta(hours=48) else "long",
                        "sport_key": sport_key
                    })
    return bets

def format_bet(b, title, color):
    emoji = SPORT_EMOJIS.get(b["sport_key"].split("_")[0], SPORT_EMOJIS["default"])
    description = (
        f"{emoji} **{title}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 Conservative Stake: ${b['cons_stake']} → Payout: ${b['cons_payout']} | Exp. Profit: ${b['cons_exp_profit']}\n"
        f"🧠 Smart Stake: ${b['smart_stake']} → Payout: ${b['smart_payout']} | Exp. Profit: ${b['smart_exp_profit']}\n"
        f"🔥 Aggressive Stake: ${b['agg_stake']} → Payout: ${b['agg_payout']} | Exp. Profit: ${b['agg_exp_profit']}\n"
    )
    return discord.Embed(description=description, color=color)

# ---------------------------
# Bot Events
# ---------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    migrate_db()  # Run DB migration automatically
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=30)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    for bet in bets:
        save_bet(bet)

bot.run(TOKEN)








