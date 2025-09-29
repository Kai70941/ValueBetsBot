import os
import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import psycopg2
from psycopg2.extras import RealDictCursor

# ---------------------------
# Config
# ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY")
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL")  # ✅ use public URL for Railway

BANKROLL = 1000
CONSERVATIVE_PCT = 0.015

# ---------------------------
# DB Connection
# ---------------------------
def get_db_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_conn()
    cur = conn.cursor()
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
        created_at TIMESTAMP DEFAULT NOW()
    )
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_bet_to_db(bet, category):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            bet['match'],
            bet['bookmaker'],
            bet['team'],
            bet['odds'],
            bet['edge'],
            bet['time_raw'],   # ✅ proper datetime for Postgres
            category,
            datetime.utcnow()
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ Failed to save bet:", e)

# ---------------------------
# Bot setup
# ---------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

posted_bets = set()

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

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

                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)
                    smart_stake = round(cons_stake * (edge*100/10), 2)  # hybrid smart
                    agg_stake = round(cons_stake * (1 + (edge*100)), 2)

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
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),  # for Discord
                        "time_raw": commence_dt,                        # ✅ for DB
                        "probability": round(implied_p*100, 2),
                        "consensus": round(consensus_p*100, 2),
                        "edge": round(edge*100, 2),
                        "cons_stake": cons_stake,
                        "agg_stake": agg_stake,
                        "smart_stake": smart_stake,
                        "cons_payout": cons_payout,
                        "agg_payout": agg_payout,
                        "smart_payout": smart_payout,
                        "cons_exp_profit": cons_exp_profit,
                        "agg_exp_profit": agg_exp_profit,
                        "smart_exp_profit": smart_exp_profit,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150)
                    })
    return bets

# ---------------------------
# Format Embed
# ---------------------------
def format_bet(b, title, color):
    indicator = "🟢 Value Bet" if b['edge'] >= 2 else "🛑 Low Value"
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
        f"🧠 **Smart Stake:** ${b['smart_stake']} → Payout: ${b['smart_payout']} | Exp. Profit: ${b['smart_exp_profit']}\n"
        f"🔥 **Aggressive Stake:** ${b['agg_stake']} → Payout: ${b['agg_payout']} | Exp. Profit: ${b['agg_exp_profit']}\n"
    )
    return discord.Embed(title=title, description=description, color=color)

def bet_id(b):
    return f"{b['match']}|{b['team']}|{b['bookmaker']}|{b['time']}"

# ---------------------------
# Post Bets
# ---------------------------
async def post_bets(bets):
    if not bets:
        return

    best = max(bets, key=lambda x: (x["consensus"], x["edge"]))
    if bet_id(best) not in posted_bets:
        posted_bets.add(bet_id(best))
        save_bet_to_db(best, "best")
        channel = bot.get_channel(BEST_BETS_CHANNEL)
        if channel:
            await channel.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))

    quick = [b for b in bets if b["quick_return"] and bet_id(b) not in posted_bets]
    q_channel = bot.get_channel(QUICK_RETURNS_CHANNEL)
    if q_channel:
        for b in quick[:5]:
            posted_bets.add(bet_id(b))
            save_bet_to_db(b, "quick")
            await q_channel.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))

    long_plays = [b for b in bets if b["long_play"] and bet_id(b) not in posted_bets]
    l_channel = bot.get_channel(LONG_PLAYS_CHANNEL)
    if l_channel:
        for b in long_plays[:5]:
            posted_bets.add(bet_id(b))
            save_bet_to_db(b, "long")
            await l_channel.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))

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

    init_db()

    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=30)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(name="fetchbets", description="Preview current bets (no posting)")
async def fetch_bets_cmd(interaction: discord.Interaction):
    data = fetch_odds()
    bets = calculate_bets(data)
    if not bets:
        await interaction.response.send_message("⚠️ No bets right now.")
        return
    preview = "\n\n".join([f"**{b['match']}**\n{b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%" for b in bets[:5]])
    await interaction.response.send_message(f"🎲 Bets Preview:\n\n{preview}")

@bot.tree.command(name="stats", description="Show database stats")
async def stats_cmd(interaction: discord.Interaction):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("SELECT category, COUNT(*) as count FROM bets GROUP BY category")
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            await interaction.response.send_message("📊 No bets saved yet.")
            return

        msg = "📊 **Bets saved so far:**\n"
        for row in rows:
            msg += f"- {row['category']}: {row['count']} bets\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error fetching stats: {e}")

# ---------------------------
# Run
# ---------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)








