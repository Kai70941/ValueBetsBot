import os
import asyncio
import discord
from discord.ext import commands, tasks
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import psycopg2
import psycopg2.extras as pgextras

# ========= ENV =========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CH_BEST = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CH_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CH_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# Paper-trading DB (Railway Postgres)
DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")

# ========= SETTINGS =========
BANKROLL = 1000
CONSERVATIVE_PCT = 0.015  # gives your $15 on $1k
MAX_BETS_PER_CHANNEL = 5
FETCH_INTERVAL_SECS = 30

# Only these 9 books (inferred by substring match on bookmaker title)
ALLOWED_BOOKMAKER_KEYS = {
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
}

def allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

# ========= DISCORD =========
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
posted_bets = set()  # de-dupe

# ========= DB HELPERS =========
def get_db_conn():
    if not DB_URL:
        return None
    # Force SSL for Railway public URL
    return psycopg2.connect(DB_URL, sslmode="require", cursor_factory=pgextras.RealDictCursor)

def ensure_table():
    if not DB_URL:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id SERIAL PRIMARY KEY,
            match TEXT NOT NULL,
            bookmaker TEXT NOT NULL,
            team TEXT NOT NULL,
            odds NUMERIC NOT NULL,
            edge NUMERIC NOT NULL,
            consensus NUMERIC NOT NULL,
            implied NUMERIC NOT NULL,
            bet_time TIMESTAMPTZ NOT NULL,
            category TEXT NOT NULL, -- best|quick|long
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)
    conn.commit()
    cur.close()
    conn.close()

def save_bet_row(b):
    if not DB_URL:
        return
    try:
        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets (match, bookmaker, team, odds, edge, consensus, implied, bet_time, category, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW());
        """, (
            b["match"], b["bookmaker"], b["team"], b["odds"],
            b["edge"], b["consensus"], b["probability"],
            b["commence_dt"], b["category"]
        ))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("❌ DB insert error:", e)

# ========= ODDS =========
def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

def calculate_bets(data):
    """Return a list of dicts with fields needed for embeds + DB."""
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

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # --- Build consensus only from allowed books ---
        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            if not allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name  = outcome.get("name")
                    if price and name:
                        key = f"{market['key']}:{name}"
                        consensus_by_outcome[key].append(1/price)

        if not consensus_by_outcome:
            continue

        # global baseline (from allowed only)
        denom = sum(len(pl) for pl in consensus_by_outcome.values())
        global_cons = (sum(p for pl in consensus_by_outcome.values() for p in pl) / denom) if denom else 0.0

        # --- Build candidate bets only from allowed books ---
        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown Bookmaker")
            if not allowed_bookmaker(title):
                continue

            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name  = outcome.get("name")
                    if not price or not name:
                        continue

                    implied_p = 1/price
                    key = f"{market['key']}:{name}"
                    if key in consensus_by_outcome:
                        consensus_p = sum(consensus_by_outcome[key]) / len(consensus_by_outcome[key])
                    else:
                        consensus_p = global_cons

                    edge = consensus_p - implied_p  # positive => value
                    if edge <= 0:
                        continue

                    # stakes
                    cons_stake = round(BANKROLL * CONSERVATIVE_PCT, 2)  # your $15
                    smart_stake = round(max(0.0, (edge * 100) / 100) * cons_stake, 2)  # simple edge-weighted
                    agg_stake   = round(cons_stake * (1 + (edge * 100) * 0.5 / 100), 2)  # ramp with edge

                    # payouts
                    cons_payout = round(cons_stake * price, 2)
                    smart_payout = round(smart_stake * price, 2)
                    agg_payout  = round(agg_stake * price, 2)

                    # expected profits from consensus
                    cons_exp = round(consensus_p * cons_payout - cons_stake, 2)
                    smart_exp = round(consensus_p * smart_payout - smart_stake, 2)
                    agg_exp  = round(consensus_p * agg_payout - agg_stake, 2)

                    # classification
                    category = "quick" if delta <= timedelta(hours=48) else "long"

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": round(price, 2),
                        "probability": round(implied_p * 100, 2),
                        "consensus": round(consensus_p * 100, 2),
                        "edge": round(edge * 100, 2),     # store as %
                        "edge_raw": edge,                 # keep raw for math if needed
                        "time": commence_dt.strftime("%d/%m/%y %H:%M"),
                        "commence_dt": commence_dt,       # for DB
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": cons_payout,
                        "smart_payout": smart_payout,
                        "agg_payout": agg_payout,
                        "cons_exp_profit": cons_exp,
                        "smart_exp_profit": smart_exp,
                        "agg_exp_profit": agg_exp,
                        "category": category
                    })

    return bets

# ========= EMBEDS (original card style) =========
def value_indicator(edge_pct: float) -> str:
    return "🟢 Value Bet" if edge_pct >= 2 else "🔴 Low Value"

def format_bet_embed(b, title, color):
    desc = (
        f"{value_indicator(b['edge'])}\n\n"
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
    return discord.Embed(title=title, description=desc, color=color)

def bet_fingerprint(b):
    return f"{b['match']}|{b['team']}|{b['bookmaker']}|{b['time']}"

# ========= POSTING =========
async def post_bets(bets):
    if not bets:
        return

    # save everything to DB (paper trading)
    for b in bets:
        save_bet_row(b)

    # Best bet = highest edge from the filtered list
    best = max(bets, key=lambda x: x["edge"]) if bets else None
    if best and CH_BEST and bet_fingerprint(best) not in posted_bets:
        posted_bets.add(bet_fingerprint(best))
        ch = bot.get_channel(CH_BEST)
        if ch:
            await ch.send(embed=format_bet_embed(best, "⭐ Best Bet", 0xFFD700))

    # Quick
    quick = [b for b in bets if b["category"] == "quick" and bet_fingerprint(b) not in posted_bets]
    if CH_QUICK:
        chq = bot.get_channel(CH_QUICK)
        if chq:
            for b in quick[:MAX_BETS_PER_CHANNEL]:
                posted_bets.add(bet_fingerprint(b))
                await chq.send(embed=format_bet_embed(b, "⏱ Quick Return Bet", 0x2ECC71))

    # Long
    longb = [b for b in bets if b["category"] == "long" and bet_fingerprint(b) not in posted_bets]
    if CH_LONG:
        chl = bot.get_channel(CH_LONG)
        if chl:
            for b in longb[:MAX_BETS_PER_CHANNEL]:
                posted_bets.add(bet_fingerprint(b))
                await chl.send(embed=format_bet_embed(b, "📅 Longer Play Bet", 0x3498DB))

# ========= SLASH: ROI (clean text report) =========
@bot.tree.command(name="roi", description="Show all-time ROI/EV per strategy (from paper-trade logs)")
async def roi(interaction: discord.Interaction):
    if not DB_URL:
        await interaction.response.send_message("⚠️ Database not configured.", ephemeral=True)
        return

    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("SELECT category, odds, edge, bet_time FROM bets;")
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        await interaction.response.send_message("⚠️ No bets recorded yet.", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    buckets = {"best": [], "quick": [], "long": []}
    for r in rows:
        cat = r["category"]
        if cat in buckets:
            buckets[cat].append(r)

    report = "📊 **ROI / EV (All-Time, paper-trade)**\n\n"
    for cat, data in buckets.items():
        if not data:
            continue
        label = {"best": "⭐ Best Bets", "quick": "⏱ Quick Return", "long": "📅 Long Plays"}[cat]

        # Only count bets that should be "finished" for paper ROI (kick at bet_time < now)
        finished = [x for x in data if x["bet_time"] <= now]

        # paper ROI assuming $15 stake and naive edge-proxy outcome (for quick sanity)
        # (If you later add real results, replace this block with settled W/L logic.)
        stake_per = round(BANKROLL * CONSERVATIVE_PCT, 2) or 15.0
        total_stake = stake_per * len(finished)
        # naive proxy: treat edge>0 as a “value position” but we don't know result -> estimate EV instead
        # EV per bet = edge (as probability delta) * stake * odds approx. We'll use expected profit as proxy:
        ev_profit = 0.0
        for x in finished:
            implied = 1/float(x["odds"])
            consensus = implied + float(x["edge"])/100.0  # reverse of earlier
            ev_profit += (consensus * stake_per * float(x["odds"])) - stake_per

        paper_roi = (ev_profit / total_stake) * 100 if total_stake > 0 else 0.0

        avg_edge = sum(float(x["edge"]) for x in data) / len(data)
        report += (
            f"**{label}** ({len(data)} logged, {len(finished)} finished)\n"
            f" • Paper ROI (EV proxy): **{paper_roi:.2f}%** on ${total_stake:.2f}\n"
            f" • Avg Edge: **{avg_edge:.2f}%**\n\n"
        )

    await interaction.response.send_message(report)

# ========= BOT EVENTS =========
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    ensure_table()
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("❌ Slash sync failed:", e)
    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=FETCH_INTERVAL_SECS)
async def bet_loop():
    data = fetch_odds()
    bets = calculate_bets(data)
    # guard KeyError/empty
    if bets:
        await post_bets(bets)

# ========= START =========
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")
bot.run(TOKEN)









