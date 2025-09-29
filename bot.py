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
DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL")

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
        created_at TIMESTAMP DEFAULT NOW(),
        result TEXT DEFAULT 'pending',
        cons_profit FLOAT DEFAULT 0,
        smart_profit FLOAT DEFAULT 0,
        agg_profit FLOAT DEFAULT 0
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
            bet['time_raw'],
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
        "markets": "h2h",
        "oddsFormat": "decimal"
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

def fetch_scores():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/scores/"
    params = {"apiKey": ODDS_API_KEY, "daysFrom": 3}
    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print("❌ Scores API error:", e)
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
                    smart_stake = round(cons_stake * (edge*100/10), 2)
                    agg_stake = round(cons_stake * (1 + (edge*100)), 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": price,
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "time_raw": commence_dt,
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150)
                    })
    return bets

# ---------------------------
# ROI & Results Updater
# ---------------------------
def update_results():
    scores = fetch_scores()
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM bets WHERE result = 'pending'")
    rows = cur.fetchall()

    for row in rows:
        match = row['match']
        team_pick = row['team']

        # Find matching score event
        for event in scores:
            if match.lower() in (f"{event.get('home_team','')} vs {event.get('away_team','')}").lower():
                if not event.get("completed", False):
                    continue
                scores_list = event.get("scores", [])
                if not scores_list or len(scores_list) < 2:
                    continue

                home = scores_list[0]
                away = scores_list[1]

                try:
                    home_score = int(home['score'])
                    away_score = int(away['score'])
                except:
                    continue

                winner = home['name'] if home_score > away_score else away['name']
                result = "win" if team_pick == winner else "loss"

                cons_profit = ((row['odds'] * row['cons_stake']) - row['cons_stake']) if result == "win" else -row['cons_stake']
                smart_profit = ((row['odds'] * row['smart_stake']) - row['smart_stake']) if result == "win" else -row['smart_stake']
                agg_profit = ((row['odds'] * row['agg_stake']) - row['agg_stake']) if result == "win" else -row['agg_stake']

                cur.execute("""
                    UPDATE bets
                    SET result=%s, cons_profit=%s, smart_profit=%s, agg_profit=%s
                    WHERE id=%s
                """, (result, cons_profit, smart_profit, agg_profit, row['id']))
                break

    conn.commit()
    cur.close()
    conn.close()

def calculate_roi():
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT SUM(cons_profit) as cons, SUM(smart_profit) as smart, SUM(agg_profit) as agg FROM bets WHERE result != 'pending'")
    totals = cur.fetchone()
    cur.execute("SELECT COUNT(*) FILTER (WHERE result='win') as wins, COUNT(*) FILTER (WHERE result='loss') as losses FROM bets WHERE result != 'pending'")
    counts = cur.fetchone()
    cur.close()
    conn.close()

    roi_cons = round((totals['cons'] / abs(totals['cons']))*100, 2) if totals['cons'] else 0
    roi_smart = round((totals['smart'] / abs(totals['smart']))*100, 2) if totals['smart'] else 0
    roi_agg = round((totals['agg'] / abs(totals['agg']))*100, 2) if totals['agg'] else 0

    return roi_cons, roi_smart, roi_agg, counts['wins'], counts['losses']

# ---------------------------
# Format Embed
# ---------------------------
def format_bet(b, title, color):
    description = (
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Time:** {b['time']}\n\n"
        f"💵 Cons Stake: ${b['cons_stake']}\n"
        f"🧠 Smart Stake: ${b['smart_stake']}\n"
        f"🔥 Agg Stake: ${b['agg_stake']}\n"
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

    best = max(bets, key=lambda x: x["edge"]) if bets else None
    if best and bet_id(best) not in posted_bets:
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

@tasks.loop(seconds=60)  # fetch less frequently for scores
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)
    update_results()

# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(name="roi", description="Show ROI for all time")
async def roi_cmd(interaction: discord.Interaction):
    roi_cons, roi_smart, roi_agg, wins, losses = calculate_roi()
    msg = (
        f"📊 **ROI Report (All Time)**\n"
        f"Conservative: {roi_cons}%\n"
        f"Smart: {roi_smart}%\n"
        f"Aggressive: {roi_agg}%\n\n"
        f"Total: {wins} Wins / {losses} Losses"
    )
    await interaction.response.send_message(msg)

# ---------------------------
# Run
# ---------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)








