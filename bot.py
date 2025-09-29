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
DATABASE_URL = os.getenv("DATABASE_URL")

BANKROLL_START = 1000
CONSERVATIVE_PCT = 0.015

# ---------------------------
# Discord Setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bets = set()

# ---------------------------
# Database Setup
# ---------------------------
conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS bets (
    id SERIAL PRIMARY KEY,
    match TEXT,
    pick TEXT,
    bookmaker TEXT,
    odds FLOAT,
    stake FLOAT,
    payout FLOAT,
    edge FLOAT,
    status TEXT DEFAULT 'pending',
    placed_at TIMESTAMP DEFAULT NOW()
)
""")
conn.commit()

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
        resp = requests.get(url, params=params, timeout=10)
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
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
        except:
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
                        consensus_by_outcome[key].append(1/outcome["price"])

        if not consensus_by_outcome:
            continue
        global_consensus = sum(p for plist in consensus_by_outcome.values() for p in plist) / max(1, sum(len(plist) for plist in consensus_by_outcome.values()))

        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not _allowed_bookmaker(title):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price, name = outcome.get("price"), outcome.get("name")
                    if not price or not name:
                        continue
                    implied_p = 1/price
                    outcome_key = f"{market['key']}:{name}"
                    consensus_p = sum(consensus_by_outcome[outcome_key])/len(consensus_by_outcome[outcome_key]) if outcome_key in consensus_by_outcome else global_consensus
                    edge = consensus_p - implied_p
                    if edge <= 0:
                        continue

                    cons_stake = round(BANKROLL_START * CONSERVATIVE_PCT, 2)
                    agg_stake = round(cons_stake * (1 + (edge*100)), 2)
                    cons_payout = round(cons_stake * price, 2)
                    agg_payout = round(agg_stake * price, 2)
                    cons_exp_profit = round(consensus_p * cons_payout - cons_stake, 2)
                    agg_exp_profit = round(consensus_p * agg_payout - agg_stake, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "probability": round(implied_p*100, 2),
                        "consensus": round(consensus_p*100, 2),
                        "edge": round(edge*100, 2),
                        "cons_stake": cons_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": cons_payout,
                        "agg_payout": agg_payout,
                        "cons_exp_profit": cons_exp_profit,
                        "agg_exp_profit": agg_exp_profit,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150)
                    })
    return bets

def format_bet(b, title, color):
    if b['edge'] >= 2:
        indicator = "🟢 Value Bet"
    else:
        indicator = "🛑 Low Value"

    description = (
        f"{indicator}\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 **Conservative Stake:** ${b['cons_stake']} → Payout: ${b['cons_payout']} | Exp. Profit: ${b['cons_exp_profit']}\n"
        f"💰 **Aggressive Stake:** ${b['agg_stake']} → Payout: ${b['agg_payout']} | Exp. Profit: ${b['agg_exp_profit']}\n"
    )
    return discord.Embed(title=title, description=description, color=color)

def bet_id(b):
    return f"{b['match']}|{b['team']}|{b['bookmaker']}|{b['time']}"

def log_bet_to_db(b):
    cur.execute("""
        INSERT INTO bets (match, pick, bookmaker, odds, stake, payout, edge)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (b['match'], b['team'], b['bookmaker'], b['odds'], b['cons_stake'], b['cons_payout'], b['edge']))
    conn.commit()

async def post_bets(bets):
    if not bets:
        return

    best = max(bets, key=lambda x: (x["consensus"], x["edge"]))
    if bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        log_bet_to_db(best)
        channel = bot.get_channel(BEST_BETS_CHANNEL)
        if channel:
            await channel.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))

    quick = [b for b in bets if b["quick_return"] and bet_id(b) not in posted_bets]
    q_channel = bot.get_channel(QUICK_RETURNS_CHANNEL)
    if q_channel:
        for b in quick[:5]:
            posted_bets.add(bet_id(b))
            log_bet_to_db(b)
            await q_channel.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))

    long_plays = [b for b in bets if b["long_play"] and bet_id(b) not in posted_bets]
    l_channel = bot.get_channel(LONG_PLAYS_CHANNEL)
    if l_channel:
        for b in long_plays[:5]:
            posted_bets.add(bet_id(b))
            log_bet_to_db(b)
            await l_channel.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))

# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(name="stats", description="Show betting stats and bankroll")
async def stats(interaction: discord.Interaction):
    cur.execute("SELECT COUNT(*) FROM bets")
    total = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) FROM bets WHERE status='win'")
    wins = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) FROM bets WHERE status='loss'")
    losses = cur.fetchone()['count']

    cur.execute("SELECT COUNT(*) FROM bets WHERE status='pending'")
    pending = cur.fetchone()['count']

    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    bankroll = BANKROLL_START + (wins - losses) * 10  # simple demo formula

    embed = discord.Embed(title="📊 Betting Stats", color=0x1ABC9C)
    embed.add_field(name="Total Bets", value=total, inline=True)
    embed.add_field(name="Wins", value=wins, inline=True)
    embed.add_field(name="Losses", value=losses, inline=True)
    embed.add_field(name="Pending", value=pending, inline=True)
    embed.add_field(name="Win Rate", value=f"{win_rate}%", inline=True)
    embed.add_field(name="Bankroll", value=f"${bankroll}", inline=True)

    await interaction.response.send_message(embed=embed)

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

if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)





