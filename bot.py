# bot.py
# ---- ENV NEEDED ------------------------------------------------------------
# DISCORD_BOT_TOKEN
# DISCORD_CHANNEL_ID_BEST
# DISCORD_CHANNEL_ID_QUICK
# DISCORD_CHANNEL_ID_LONG
# VALUE_BETS_CHANNEL_ID            <- extra testing channel for value bets
# ODDS_API_KEY
# DATABASE_PUBLIC_URL or DATABASE_URL (Railway Postgres)
#
# ---------------------------------------------------------------------------

import os
import math
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord import app_commands

import aiohttp
import psycopg2
import psycopg2.extras

# ---------------------- Config (kept like before) ---------------------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_CHANNEL_ID = int(os.getenv("VALUE_BETS_CHANNEL_ID", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

DB_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")

BANKROLL_UNITS = 1000.0
CONSERVATIVE_PCT = 0.015   # 1.5%  -> ~15.0u default
EDGE_VALUE_THRESHOLD = 2.0 # % edge to call "Value Bet"

# bookmaker allow-list (unchanged)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

# ------------- Sport / league label & emoji helpers (kept stable) ----------
def sport_from_key(sport_key: str):
    # returns (emoji, sport_name, league_name)
    # The Odds API sport_key examples: 'soccer_brazil_serie_b', 'americanfootball_ncaaf', etc.
    key = (sport_key or "").lower()

    def pretty_league(rest: str) -> str:
        if not rest:
            return "Unknown League"
        name = rest.replace("_", " ").strip().title()
        # special accents / common leagues
        name = name.replace("Serie A", "Série A").replace("Serie B", "Série B")
        name = name.replace("Ncaa", "NCAA").replace("Nfl", "NFL").replace("Ncaaf", "NCAA F")
        name = name.replace("Euroliga", "EuroLiga")
        return name

    if key.startswith("soccer_"):
        return "⚽", "Soccer", pretty_league(key[len("soccer_"):])
    if key.startswith("americanfootball"):
        return "🏈", "American Football", pretty_league(key[len("americanfootball_"):])
    if key.startswith("basketball"):
        return "🏀", "Basketball", pretty_league(key[len("basketball_"):])
    if key.startswith("baseball"):
        return "⚾", "Baseball", pretty_league(key[len("baseball_"):])
    if key.startswith("icehockey"):
        return "🏒", "Ice Hockey", pretty_league(key[len("icehockey_"):])
    if key.startswith("mma"):
        return "🥊", "MMA", pretty_league(key[len("mma_"):])
    if key.startswith("tennis"):
        return "🎾", "Tennis", pretty_league(key[len("tennis_"):])
    if key.startswith("golf"):
        return "⛳", "Golf", pretty_league(key[len("golf_"):])
    if key.startswith("esports"):
        return "🎮", "Esports", pretty_league(key[len("esports_"):])
    if key.startswith("cricket"):
        return "🏏", "Cricket", pretty_league(key[len("cricket_"):])
    # default
    return "🎯", "Sport", "Unknown League"

# ---------------------------- DB utils -------------------------------------
def get_db_conn():
    return psycopg2.connect(DB_URL)

def init_db():
    ddl = """
    CREATE TABLE IF NOT EXISTS bets (
        id SERIAL PRIMARY KEY,
        match TEXT,
        bookmaker TEXT,
        team TEXT,
        odds NUMERIC,
        edge NUMERIC,
        bet_time TIMESTAMPTZ,
        category TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        sport TEXT,
        league TEXT,
        strategy TEXT,
        stake_units NUMERIC,
        return_units NUMERIC,
        result TEXT,
        settled_at TIMESTAMPTZ
    );
    """
    alters = [
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS sport TEXT;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS league TEXT;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS strategy TEXT;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS stake_units NUMERIC;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS return_units NUMERIC;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS result TEXT;",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ;"
    ]
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(ddl)
            for q in alters:
                cur.execute(q)
        conn.commit()
    finally:
        conn.close()

# Paper trading flag + setter
PAPER_MODE = True  # unchanged behavior; you can toggle with /paper

# --------------------------- Bot setup --------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# per-channel posted tracking (to keep duplication reliable)
posted_by_channel = {
    "best": set(),
    "quick": set(),
    "long": set(),
    "value": set()
}

def bet_key(b: dict) -> str:
    return f"{b['match']}|{b['bookmaker']}|{b['team']}|{b['odds']}|{b['bet_time']}"

# --------------------------- Odds fetch -------------------------------------
async def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=15) as r:
                if r.status != 200:
                    txt = await r.text()
                    print("❌ Odds API error:", r.status, txt)
                    return []
                return await r.json()
    except Exception as e:
        print("❌ Odds API exception:", e)
        return []

def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

# ----------------------- Betting logic (same style) -------------------------
def calculate_bets(data):
    now = datetime.now(timezone.utc)
    bets = []

    for event in data:
        # Teams & time
        home, away = event.get("home_team"), event.get("away_team")
        if not home or not away:
            continue
        match_name = f"{home} vs {away}"
        commence_time = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat((commence_time or "").replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # Sport/league
        sport_key = event.get("sport_key", "")
        sport_emoji, sport_name, league_name = sport_from_key(sport_key)

        # Build consensus using inverse odds across books/outcomes
        consensus_by_outcome = defaultdict(list)
        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for market in book.get("markets", []):
                for outc in market.get("outcomes", []):
                    price = outc.get("price")
                    name  = outc.get("name")
                    if price and name:
                        key = f"{market['key']}:{name}"
                        consensus_by_outcome[key].append(1.0/float(price))

        if not consensus_by_outcome:
            continue

        # simple global consensus baseline
        denom = sum(len(v) for v in consensus_by_outcome.values())
        global_consensus = (sum(sum(v) for v in consensus_by_outcome.values()) / denom) if denom else 0.5

        # Create one bet per bookmaker outcome
        for book in event.get("bookmakers", []):
            title = book.get("title", "Unknown")
            if not _allowed_bookmaker(title):
                continue
            for market in book.get("markets", []):
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name  = outcome.get("name")
                    if not price or not name:
                        continue

                    price = float(price)
                    implied_p = 1.0/price
                    okey = f"{market['key']}:{name}"
                    if okey in consensus_by_outcome:
                        consensus_p = sum(consensus_by_outcome[okey]) / len(consensus_by_outcome[okey])
                    else:
                        consensus_p = global_consensus
                    edge = (consensus_p - implied_p) * 100.0  # in %

                    # Stakes (units), kept same style but shown as "u"
                    cons_stake = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)       # ~15.0u
                    # smart stake scaled by edge, min 0.5u
                    smart_stake = round(max(0.5, cons_stake * max(edge, 0)/10.0), 2)
                    # aggressive stake slightly larger
                    agg_stake   = round(cons_stake * (1.0 + max(edge,0)/50.0), 2)

                    bets.append({
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": price,
                        "bet_time": commence_dt.isoformat(),
                        "probability": round(implied_p*100, 2),
                        "consensus": round(consensus_p*100, 2),
                        "edge": round(edge, 2),
                        "cons_stake": cons_stake,
                        "smart_stake": smart_stake,
                        "agg_stake": agg_stake,
                        "cons_payout": round(cons_stake * price, 2),
                        "smart_payout": round(smart_stake * price, 2),
                        "agg_payout": round(agg_stake * price, 2),
                        "cons_exp_profit": round(consensus_p * (cons_stake * price) - cons_stake, 2),
                        "smart_exp_profit": round(consensus_p * (smart_stake * price) - smart_stake, 2),
                        "agg_exp_profit": round(consensus_p * (agg_stake   * price) - agg_stake, 2),
                        "quick_return": delta <= timedelta(hours=48),
                        "long_play": timedelta(hours=48) < delta <= timedelta(days=150),
                        "sport_emoji": sport_emoji,
                        "sport": sport_name,
                        "league": league_name,
                        "is_value": edge >= EDGE_VALUE_THRESHOLD
                    })

    return bets

# ------------------------- Card formatting (units) --------------------------
def format_bet(b, title, color):
    # Indicator (green for value, red for low value)
    indicator = "🟢 Value Bet" if b.get("is_value") else "🔴 Low Value"

    # Title line includes sport + league
    header_line = f"{b.get('sport_emoji','🎯')} {b.get('sport','Sport')} ({b.get('league','Unknown League')})"

    desc = (
        f"{indicator}\n\n"
        f"**{header_line}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['probability']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['bet_time']}\n\n"
        f"💵 **Conservative Stake:** {b['cons_stake']:.2f}u → Payout: {b['cons_payout']:.2f}u | Exp. Profit: {b['cons_exp_profit']:.2f}u\n"
        f"🧠 **Smart Stake:** {b['smart_stake']:.2f}u → Payout: {b['smart_payout']:.2f}u | Exp. Profit: {b['smart_exp_profit']:.2f}u\n"
        f"🔥 **Aggressive Stake:** {b['agg_stake']:.2f}u → Payout: {b['agg_payout']:.2f}u | Exp. Profit: {b['agg_exp_profit']:.2f}u\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

# ---------------------- Paper trading storage -------------------------------
def save_paper_trades(b: dict, category: str):
    """Insert three rows (conservative, smart, aggressive) for this pick."""
    if not PAPER_MODE:
        return
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            sql = """
            INSERT INTO bets (match, bookmaker, team, odds, edge, bet_time, category,
                              sport, league, strategy, stake_units)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
            """
            bt = datetime.fromisoformat(b["bet_time"])
            rows = [
                (b["match"], b["bookmaker"], b["team"], b["odds"], b["edge"], bt, category,
                 b["sport"], b["league"], "conservative", b["cons_stake"]),
                (b["match"], b["bookmaker"], b["team"], b["odds"], b["edge"], bt, category,
                 b["sport"], b["league"], "smart", b["smart_stake"]),
                (b["match"], b["bookmaker"], b["team"], b["odds"], b["edge"], bt, category,
                 b["sport"], b["league"], "aggressive", b["agg_stake"]),
            ]
            for r in rows:
                cur.execute(sql, r)
        conn.commit()
    finally:
        conn.close()

# --------------------------- Posting ----------------------------------------
async def post_bets(bets):
    if not bets:
        return

    # Best = highest consensus then edge; and force indicator as "value"
    best = max(bets, key=lambda x: (x["consensus"], x["edge"])) if bets else None
    if best:
        best["is_value"] = True  # never show Low Value on Best Bet

    quick = [b for b in bets if b["quick_return"]]
    long_plays = [b for b in bets if b["long_play"]]

    # Post Quick
    qchan = bot.get_channel(QUICK_RETURNS_CHANNEL)
    if qchan:
        for b in quick[:5]:
            k = bet_key(b)
            if k in posted_by_channel["quick"]:
                continue
            await qchan.send(embed=format_bet(b, "⏱ Quick Return Bet", 0x2ECC71))
            posted_by_channel["quick"].add(k)
            save_paper_trades(b, "quick")

            # ALSO duplicate value bets into the dedicated channel
            if b.get("is_value") and VALUE_BETS_CHANNEL_ID:
                vchan = bot.get_channel(VALUE_BETS_CHANNEL_ID)
                if vchan and k not in posted_by_channel["value"]:
                    await vchan.send(embed=format_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71))
                    posted_by_channel["value"].add(k)

    # Post Long
    lchan = bot.get_channel(LONG_PLAYS_CHANNEL)
    if lchan:
        for b in long_plays[:5]:
            k = bet_key(b)
            if k in posted_by_channel["long"]:
                continue
            await lchan.send(embed=format_bet(b, "📅 Longer Play Bet", 0x3498DB))
            posted_by_channel["long"].add(k)
            save_paper_trades(b, "long")

            if b.get("is_value") and VALUE_BETS_CHANNEL_ID:
                vchan = bot.get_channel(VALUE_BETS_CHANNEL_ID)
                if vchan and k not in posted_by_channel["value"]:
                    await vchan.send(embed=format_bet(b, "🟢 Value Bet (Testing)", 0x2ECC71))
                    posted_by_channel["value"].add(k)

    # Best Bet
    bchan = bot.get_channel(BEST_BETS_CHANNEL)
    if best and bchan:
        k = bet_key(best)
        if k not in posted_by_channel["best"]:
            await bchan.send(embed=format_bet(best, "⭐ Best Bet", 0xFFD700))
            posted_by_channel["best"].add(k)
            save_paper_trades(best, "best")

        # optional duplicate Best into value channel too
        if VALUE_BETS_CHANNEL_ID:
            vchan = bot.get_channel(VALUE_BETS_CHANNEL_ID)
            if vchan and k not in posted_by_channel["value"]:
                await vchan.send(embed=format_bet(best, "🟢 Value Bet (Testing)", 0x2ECC71))
                posted_by_channel["value"].add(k)

# ---------------------------- Slash commands --------------------------------
@bot.tree.command(name="ping", description="Latency check.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Preview a few bets (ephemeral).")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = await fetch_odds()
    bets = calculate_bets(data)[:3]
    if not bets:
        await interaction.followup.send("No upcoming bets found.", ephemeral=True)
        return
    em = discord.Embed(title="🎲 Bets Preview:", color=0x95A5A6)
    for b in bets:
        em.add_field(
            name=b["match"],
            value=f"{b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%",
            inline=False
        )
    await interaction.followup.send(embed=em, ephemeral=True)

@bot.tree.command(name="paper", description="Turn paper trading on/off, or check status.")
@app_commands.describe(mode="on | off | status")
async def paper_cmd(interaction: discord.Interaction, mode: str):
    global PAPER_MODE
    m = (mode or "").lower()
    if m == "on":
        PAPER_MODE = True
        await interaction.response.send_message("🧾 Paper trading **ON**.", ephemeral=True)
    elif m == "off":
        PAPER_MODE = False
        await interaction.response.send_message("🧾 Paper trading **OFF**.", ephemeral=True)
    else:
        await interaction.response.send_message(f"🧾 Paper trading is **{'ON' if PAPER_MODE else 'OFF'}**.", ephemeral=True)

# --------- ROI (fixed to compute from settled paper-trade rows) --------------
def _roi_sql(strategy: str | None):
    base = """
    SELECT
      COALESCE(strategy,'unknown') AS strategy,
      COUNT(*)                     AS bets,
      SUM(stake_units)             AS staked,
      SUM(return_units - stake_units) AS profit,
      CASE
        WHEN SUM(stake_units) = 0 THEN NULL
        ELSE ROUND(100.0 * SUM(return_units - stake_units) / SUM(stake_units), 2)
      END AS roi_pct
    FROM bets
    WHERE result IN ('win','loss','push')
    """
    if strategy:
        base += " AND strategy = %s"
    base += " GROUP BY 1 ORDER BY 1"
    return base

@bot.tree.command(name="roi", description="Show ROI for paper trades (all-time). Optionally filter by strategy.")
@app_commands.describe(strategy="conservative | smart | aggressive (optional)")
async def roi_cmd(interaction: discord.Interaction, strategy: str | None = None):
    await interaction.response.defer(ephemeral=True)

    if strategy:
        strategy = strategy.lower().strip()
        if strategy not in ("conservative", "smart", "aggressive"):
            await interaction.followup.send("Use strategy: conservative | smart | aggressive", ephemeral=True)
            return

    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = _roi_sql(strategy)
            params = (strategy,) if strategy else tuple()
            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        await interaction.followup.send("No **settled** paper trades yet. Settle some rows (win/loss/push) and try again.", ephemeral=True)
        return

    title = "📈 ROI (all strategies)" if not strategy else f"📈 ROI — {strategy.capitalize()}"
    em = discord.Embed(title=title, color=0x6C5CE7)
    total_bets = total_staked = total_profit = 0.0

    for r in rows:
        s   = r["strategy"]
        bts = int(r["bets"] or 0)
        stk = float(r["staked"] or 0)
        pft = float(r["profit"] or 0)
        roi = r["roi_pct"]
        total_bets   += bts
        total_staked += stk
        total_profit += pft
        em.add_field(
            name=f"• {s.capitalize()}",
            value=(f"**Bets:** {bts}\n"
                   f"**Staked:** {stk:.2f}u\n"
                   f"**Profit:** {pft:+.2f}u\n"
                   f"**ROI:** {('—' if roi is None else f'{roi:.2f}%')}"),
            inline=True
        )

    overall_roi = None if total_staked == 0 else round(100.0 * (total_profit / total_staked), 2)
    em.add_field(
        name="**Total**",
        value=(f"**Bets:** {int(total_bets)}\n"
               f"**Staked:** {total_staked:.2f}u\n"
               f"**Profit:** {total_profit:+.2f}u\n"
               f"**ROI:** {('—' if overall_roi is None else f'{overall_roi:.2f}%')}"),
        inline=False
    )
    await interaction.followup.send(embed=em, ephemeral=True)

# -------------------------- Bot lifecycle -----------------------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    init_db()
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print(f"❌ Slash sync failed: {e}")

    if not bet_loop.is_running():
        bet_loop.start()

@tasks.loop(seconds=30)
async def bet_loop():
    data = await fetch_odds()
    bets = calculate_bets(data)
    await post_bets(bets)

# ---------------------------------------------------------------------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")

bot.run(TOKEN)













