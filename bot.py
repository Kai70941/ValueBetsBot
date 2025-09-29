import os
import discord
from discord.ext import commands, tasks
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from datetime import datetime, timezone, timedelta

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

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# Database helpers
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
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_bet(bet, category):
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category)
            VALUES (%s, %s, %s, %s, %s, %s, %s);
        """, (
            bet["match"], bet["bookmaker"], bet["team"], bet["odds"],
            bet["edge"], datetime.strptime(bet["time"], "%d/%m/%y %H:%M"),
            category
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ Failed to save bet:", e)

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
# Betting logic
# ---------------------------
def calculate_bets(data):
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
        except:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        for book in event.get("bookmakers", []):
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price, name = outcome.get("price"), outcome.get("name")
                    if not price or not name:
                        continue

                    implied_p = 1 / price
                    consensus_p = 0.5  # dummy consensus
                    edge = round((consensus_p - implied_p) * 100, 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": book.get("title", "Unknown"),
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "edge": edge,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150)
                    })
    return bets

def format_bet(b, title, color):
    indicator = "🟢 Value Bet" if b["edge"] >= 2 else "🛑 Low Value"
    desc = (
        f"{indicator}\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

# ---------------------------
# Bot events
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
    await post_bets(bets)

async def post_bets(bets):
    if not bets:
        return
    best = max(bets, key=lambda x: x.get("edge", 0))
    chan = bot.get_channel(BEST_BETS_CHANNEL)
    if chan:
        await chan.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))
        save_bet(best, "best")

    for b in [b for b in bets if b["quick_return"]]:
        chan = bot.get_channel(QUICK_RETURNS_CHANNEL)
        if chan:
            await chan.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))
            save_bet(b, "quick")

    for b in [b for b in bets if b["long_play"]]:
        chan = bot.get_channel(LONG_PLAYS_CHANNEL)
        if chan:
            await chan.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))
            save_bet(b, "long")

# ---------------------------
# ROI Command
# ---------------------------
@bot.tree.command(name="roi", description="Show ROI report per strategy")
async def roi(interaction: discord.Interaction):
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        await interaction.response.send_message("⚠️ No bets recorded yet.", ephemeral=True)
        return

    categories = {"best": [], "quick": [], "long": []}
    for row in rows:
        categories[row["category"]].append(row)

    msg = "📊 **ROI Report (All-Time)**\n\n"
    for cat, bets in categories.items():
        if not bets:
            continue
        stype = {"best": "⭐ Best Bets", "quick": "⏱ Quick Return", "long": "📅 Long Plays"}[cat]

        # Paper ROI
        total_stake = len(bets) * 15
        total_return = 0
        for b in bets:
            win_prob = 1 / b["odds"]
            expected_outcome = "win" if b["edge"] > 0 else "loss"
            if expected_outcome == "win":
                total_return += 15 * b["odds"]
        paper_profit = total_return - total_stake
        paper_roi = (paper_profit / total_stake) * 100 if total_stake > 0 else 0

        # Average Edge ROI
        avg_edge = sum(b["edge"] for b in bets) / len(bets)

        msg += f"{stype} → {len(bets)} bets\n"
        msg += f"   • Paper ROI: {paper_roi:.2f}% | Profit: ${paper_profit:.2f}\n"
        msg += f"   • Avg Edge: {avg_edge:.2f}%\n\n"

    await interaction.response.send_message(msg)

# ---------------------------
# Run
# ---------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")
bot.run(TOKEN)








