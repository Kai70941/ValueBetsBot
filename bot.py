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
VALUE_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))  # ✅ New channel for Value Bets
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

BANKROLL = 1000
CONSERVATIVE_PCT = 0.015

# ---------------------------
# Bot Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bets = set()

# ---------------------------
# Sport Emojis
# ---------------------------
SPORT_EMOJIS = {
    "soccer": "⚽",
    "basketball": "🏀",
    "baseball": "⚾",
    "icehockey": "🏒",
    "americanfootball": "🏈",
    "tennis": "🎾",
    "mma": "🥊",
    "boxing": "🥊",
    "golf": "⛳",
    "esports": "🎮",
    "cricket": "🏏",
    "rugby": "🏉"
}

def get_sport_label(sport_key, sport_title):
    sport_key = (sport_key or "").lower()
    emoji = SPORT_EMOJIS.get(sport_key, "❓")
    if sport_title:
        return f"{emoji} {sport_title}"
    return f"{emoji} Unknown League"

# ---------------------------
# Database
# ---------------------------
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def init_db():
    with get_db_conn() as conn, conn.cursor() as cur:
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
            created_at TIMESTAMP DEFAULT NOW()
        )
        """)
        conn.commit()

def save_bet_to_db(bet):
    with get_db_conn() as conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, sport)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            bet["match"], bet["bookmaker"], bet["team"], bet["odds"], bet["edge"],
            bet["time_db"], bet["category"], bet["sport"]
        ))
        conn.commit()

# ---------------------------
# Odds API
# ---------------------------
def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {"apiKey": ODDS_API_KEY, "regions": "au,us,uk", "markets": "h2h,spreads,totals", "oddsFormat": "decimal"}
    resp = requests.get(url, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()

def calculate_bets(data):
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        home, away = event.get("home_team"), event.get("away_team")
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except:
            continue

        if commence_dt - now <= timedelta(0) or commence_dt - now > timedelta(days=150):
            continue

        sport_label = get_sport_label(event.get("sport_key"), event.get("sport_title"))

        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if outcome.get("price"):
                        consensus_by_outcome[outcome["name"]].append(1/outcome["price"])

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    if not outcome.get("price"):
                        continue

                    price = outcome["price"]
                    name = outcome["name"]
                    implied_p = 1 / price
                    consensus_p = sum(consensus_by_outcome[name])/len(consensus_by_outcome[name]) if name in consensus_by_outcome else implied_p
                    edge = consensus_p - implied_p

                    if edge <= 0:
                        continue

                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)
                    smart_stake = round((BANKROLL * (edge * 100 / 1000)), 2)
                    agg_stake = round(cons_stake * (1 + (edge*100)), 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": book["title"],
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "time_db": commence_dt,
                        "consensus": round(consensus_p*100, 2),
                        "probability": round(implied_p*100, 2),
                        "edge": round(edge*100, 2),
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": round(cons_stake * price, 2),
                        "smart_payout": round(smart_stake * price, 2),
                        "agg_payout": round(agg_stake * price, 2),
                        "cons_exp_profit": round(consensus_p * (cons_stake * price) - cons_stake, 2),
                        "smart_exp_profit": round(consensus_p * (smart_stake * price) - smart_stake, 2),
                        "agg_exp_profit": round(consensus_p * (agg_stake * price) - agg_stake, 2),
                        "quick_return": commence_dt - now <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < (commence_dt - now) <= timedelta(days=150),
                        "category": "value",
                        "sport": sport_label
                    })
    return bets

# ---------------------------
# Formatting
# ---------------------------
def format_bet(b, title, color):
    description = (
        f"🟢 Value Bet\n\n"
        f"**{b['sport']}**\n\n"
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

# ---------------------------
# Bot Events
# ---------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    init_db()
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=30)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    for b in bets:
        save_bet_to_db(b)

    await post_bets(bets)

async def post_bets(bets):
    if not bets:
        return

    # Send to normal channels
    best_channel = bot.get_channel(BEST_BETS_CHANNEL)
    quick_channel = bot.get_channel(QUICK_RETURNS_CHANNEL)
    long_channel = bot.get_channel(LONG_PLAYS_CHANNEL)
    value_channel = bot.get_channel(VALUE_BETS_CHANNEL)

    for b in bets:
        embed = format_bet(b, "⭐ Value Bet", 0x2ECC71)
        if value_channel:
            await value_channel.send(embed=embed)
        if b["quick_return"] and quick_channel:
            await quick_channel.send(embed=embed)
        if b["long_play"] and long_channel:
            await long_channel.send(embed=embed)
        if best_channel and b["edge"] > 5:  # mark only higher edges as "best"
            await best_channel.send(embed=embed)

bot.run(TOKEN)












