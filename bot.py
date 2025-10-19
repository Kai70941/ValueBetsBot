# bot.py  —  ValueBets (full build)

import os
import asyncio
import json
import math
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple

import aiohttp
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ------------- Config & constants -------------

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CH_BEST = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
CH_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
CH_LONG = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
CH_VALUE = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))  # your duplicate value-bets channel
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

DB_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL") or ""

# base stakes in units
CONSERVATIVE_UNITS = 15.0

# bookmakers (limit to your 9)
ALLOWED_BOOKMAKERS = {
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
}

# sport emoji map (covers commonly returned sports from the-odds-api)
SPORT_EMOJIS = {
    "soccer": "⚽",
    "basketball": "🏀",
    "americanfootball": "🏈",
    "aussierules": "🏉",
    "tennis": "🎾",
    "mma": "🥊",
    "icehockey": "🏒",
    "baseball": "⚾",
    "cricket": "🏏",
    "esports": "🎮",
    "golf": "⛳",
    "boxing": "🥊",
    "rugbyleague": "🏉",
    "rugbunion": "🏉",
    "handball": "🤾",
}

# Discord intents
intents = discord.Intents.default()
intents.message_content = True

# Avoid duplicate CommandTree error
class ValueBetsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        # Single CommandTree instance
        self.tree = app_commands.CommandTree(self)

bot = ValueBetsBot()

# Odds API
API_BASE = "https://api.the-odds-api.com/v4"

# In-memory duplicate suppression per run
posted_keys = set()

# ------------- DB helpers -------------

def get_db() -> psycopg2.extensions.connection:
    if not DB_URL:
        raise RuntimeError("DATABASE_URL / DATABASE_PUBLIC_URL is not set.")
    return psycopg2.connect(DB_URL, cursor_factory=RealDictCursor)

def init_db():
    """Create tables if not exist, add columns (safe migrations)."""
    conn = get_db()
    cur = conn.cursor()

    # paper-trading bets universe (system)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        id SERIAL PRIMARY KEY,
        bet_key TEXT UNIQUE,
        event_id TEXT,
        match TEXT,
        bookmaker TEXT,
        team TEXT,
        odds NUMERIC,
        edge NUMERIC,
        probability NUMERIC,
        consensus NUMERIC,
        bet_time TIMESTAMPTZ,
        category TEXT,
        sport TEXT,
        league TEXT,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)

    # user bets (button clicks)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_bets (
        id SERIAL PRIMARY KEY,
        user_id BIGINT,
        username TEXT,
        bet_key TEXT,
        event_id TEXT,
        sport TEXT,
        league TEXT,
        stake_type TEXT,      -- conservative/smart/aggressive
        stake_units NUMERIC,
        placed_at TIMESTAMPTZ DEFAULT NOW(),
        result TEXT,          -- win/loss/push/void/NULL
        pnl NUMERIC,          -- realized P/L units
        settled_at TIMESTAMPTZ
    );
    """)

    # safe add columns (in case older schema exists)
    safe_add = [
        ("bets", "league", "TEXT"),
        ("bets", "sport", "TEXT"),
        ("bets", "probability", "NUMERIC"),
        ("bets", "consensus", "NUMERIC")
    ]
    for table, col, typ in safe_add:
        cur.execute(f"""
            SELECT 1
            FROM information_schema.columns
            WHERE table_name='{table}' AND column_name='{col}' ;
        """)
        if cur.fetchone() is None:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ};")

    conn.commit()
    cur.close()
    conn.close()

def upsert_bet(b: Dict[str, Any]):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO bets (bet_key, event_id, match, bookmaker, team, odds, edge, probability, consensus,
                          bet_time, category, sport, league)
        VALUES (%(bet_key)s, %(event_id)s, %(match)s, %(bookmaker)s, %(team)s, %(odds)s, %(edge)s,
                %(probability)s, %(consensus)s, %(bet_time)s, %(category)s, %(sport)s, %(league)s)
        ON CONFLICT (bet_key) DO UPDATE SET
            edge = EXCLUDED.edge,
            probability = EXCLUDED.probability,
            consensus = EXCLUDED.consensus,
            bet_time = EXCLUDED.bet_time,
            category = EXCLUDED.category,
            sport = EXCLUDED.sport,
            league = EXCLUDED.league;
    """, b)
    conn.commit()
    cur.close()
    conn.close()

def save_user_bet(user_id: int, username: str, stake_type: str, stake_units: float,
                  bet_key: str) -> Tuple[bool, str]:
    """Save a user bet; returns success and message."""
    conn = get_db()
    cur = conn.cursor()
    # ensure bet exists (ingested)
    cur.execute("SELECT * FROM bets WHERE bet_key=%s", (bet_key,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False, "Sorry, I couldn't find this bet yet. Please try again in a few seconds."

    cur.execute("""
        INSERT INTO user_bets (user_id, username, bet_key, event_id, sport, league, stake_type, stake_units)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id;
    """, (user_id, username, row["bet_key"], row["event_id"], row["sport"], row["league"],
          stake_type, stake_units))
    new_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return True, f"Saved your {stake_type} bet ({stake_units:.2f} units). Entry #{new_id}."

# ------------- Odds logic -------------

def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower().replace(" ", "")
    return any(bk in t for bk in ALLOWED_BOOKMAKERS)

def _edge(prob_consensus: float, implied: float) -> float:
    # edge% = (consensus - implied) * 100
    return round((prob_consensus - implied) * 100.0, 2)

def _consensus_for_outcome(consensus_parts: Dict[str, List[float]], key: str, fallback: float) -> float:
    lst = consensus_parts.get(key)
    if not lst:
        return fallback
    return sum(lst)/len(lst)

def _sport_and_league(event: Dict[str, Any]) -> Tuple[str, str]:
    # TheOddsAPI returns "sport_title": e.g. "Soccer - Brazil Serie B"
    # We'll split on " - " to get league; sport key is e.g. "soccer"
    sport_key = (event.get("sport_key") or "").lower()
    sport_title = event.get("sport_title") or ""
    league = "Unknown League"
    if sport_title:
        # try to extract text after " - "
        parts = sport_title.split(" - ", 1)
        if len(parts) == 2:
            league = parts[1]
        else:
            # if no dash, use sport_title as league
            league = sport_title
    return sport_key, league

async def fetch_odds(session: aiohttp.ClientSession) -> List[Dict[str, Any]]:
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    url = f"{API_BASE}/sports/upcoming/odds/"
    async with session.get(url, params=params, timeout=20) as resp:
        if resp.status == 200:
            return await resp.json()
        else:
            txt = await resp.text()
            print("Odds API error:", resp.status, txt)
            return []

def format_units(u: float) -> str:
    return f"{u:.2f} units" if u < 1000 else f"{int(round(u))} units"

def stake_suggestions(edge_pct: float) -> Tuple[float, float, float]:
    # conservative fixed, smart Kelly-ish, aggressive scales with edge
    cons = CONSERVATIVE_UNITS
    # Kelly fraction ~ edge% / odds. We’ll approximate using edge factor safely
    edge_f = max(edge_pct/100.0, 0.0)
    smart = max(round(cons * (0.25 + edge_f*1.5), 2), 0.5)
    aggr = round(cons * (1.0 + edge_f*5.0), 2)
    return cons, smart, aggr

def make_bet_key(event_id: str, bookmaker: str, market_key: str, outcome_name: str) -> str:
    return f"{event_id}|{bookmaker}|{market_key}|{outcome_name}"

def category_for_delta(d: timedelta) -> str:
    if d <= timedelta(hours=48):
        return "quick"
    elif d <= timedelta(days=150):
        return "long"
    return "ignore"

def pick_best(bets: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    # Choose best by (consensus prob * edge) as combined score, but ensure value (edge>0.5)
    ranked = [b for b in bets if b["edge"] >= 0.5]
    if not ranked:
        return None
    ranked.sort(key=lambda x: (x["probability"]*x["edge"]), reverse=True)
    return ranked[0]

def embed_from_bet(b: Dict[str, Any], title: str, color: discord.Color) -> discord.Embed:
    # value indicator
    indicator = "🟢 Value Bet" if b["edge"] >= 2.0 else "🛑 Low Value"
    sport_key = b["sport"] or ""
    emoji = SPORT_EMOJIS.get(sport_key, "🎲")
    league = b.get("league") or "Unknown League"

    desc = (
        f"{indicator}\n\n"
        f"**{emoji} {sport_key.capitalize()} ({league})**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']:.2f}%\n"
        f"**Implied %:** {b['probability']*100:.2f}%\n"
        f"**Edge:** {b['edge']:.2f}%\n"
        f"**Time:** {b['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
    )
    cons, smart, aggr = stake_suggestions(b["edge"])
    desc += (
        f"💵 **Conservative Stake:** {format_units(cons)} → "
        f"Payout: {format_units(cons*b['odds'])} | Exp. Profit: {format_units((b['consensus']/100.0)*cons*b['odds'] - cons)}\n"
        f"🧠 **Smart Stake:** {format_units(smart)} → "
        f"Payout: {format_units(smart*b['odds'])} | Exp. Profit: {format_units((b['consensus']/100.0)*smart*b['odds'] - smart)}\n"
        f"🔥 **Aggressive Stake:** {format_units(aggr)} → "
        f"Payout: {format_units(aggr*b['odds'])} | Exp. Profit: {format_units((b['consensus']/100.0)*aggr*b['odds'] - aggr)}\n"
    )
    emb = discord.Embed(title=title, description=desc, color=color)
    emb.set_footer(text=b["bet_key"])
    return emb

def stake_buttons(bet_key: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for label, style, skey in [
        ("Conservative", discord.ButtonStyle.success, "conservative"),
        ("Smart", discord.ButtonStyle.primary, "smart"),
        ("Aggressive", discord.ButtonStyle.danger, "aggressive"),
    ]:
        custom = json.dumps({"t": "stake", "k": bet_key, "s": skey})
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=custom))
    return view

# ------------- Bot tasks -------------

async def fetch_and_post():
    async with aiohttp.ClientSession() as session:
        data = await fetch_odds(session)
    now = datetime.now(timezone.utc)
    all_bets: List[Dict[str, Any]] = []

    for event in data:
        event_id = event.get("id") or event.get("event_id") or ""
        home, away = event.get("home_team"), event.get("away_team")
        match_name = f"{home} vs {away}"
        commence = event.get("commence_time")
        try:
            commence_dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue

        delta = commence_dt - now
        cat = category_for_delta(delta)
        if cat == "ignore":
            continue

        sport_key, league = _sport_and_league(event)

        # consensus probability per outcome across books (by market key + outcome name)
        consensus_parts: Dict[str, List[float]] = {}
        for bk in event.get("bookmakers", []):
            if not _allowed_bookmaker(bk.get("title", "")):
                continue
            for market in bk.get("markets", []):
                mkey = market.get("key")
                for outc in market.get("outcomes", []):
                    price = outc.get("price")
                    name = outc.get("name")
                    if not price or not name:
                        continue
                    implied = 1.0/float(price)
                    k = f"{mkey}:{name}"
                    consensus_parts.setdefault(k, []).append(implied)

        # average fallback
        flat = [x for v in consensus_parts.values() for x in v]
        global_avg = sum(flat)/len(flat) if flat else 0.5

        for bk in event.get("bookmakers", []):
            title = bk.get("title", "Unknown")
            if not _allowed_bookmaker(title):
                continue
            for market in bk.get("markets", []):
                mkey = market.get("key")
                for outc in market.get("outcomes", []):
                    price = outc.get("price")
                    name = outc.get("name")
                    if not price or not name:
                        continue
                    implied = 1.0/float(price)
                    cons_prob = _consensus_for_outcome(consensus_parts, f"{mkey}:{name}", global_avg)
                    ed = _edge(cons_prob, implied)
                    if ed <= 0:
                        continue

                    bet_key = make_bet_key(event_id, title, mkey, name)
                    bet = {
                        "bet_key": bet_key,
                        "event_id": event_id,
                        "match": match_name,
                        "bookmaker": title,
                        "team": name,
                        "odds": float(price),
                        "edge": ed,
                        "probability": implied,
                        "consensus": cons_prob*100.0,
                        "bet_time": commence_dt,
                        "category": cat,
                        "sport": sport_key,
                        "league": league
                    }
                    all_bets.append(bet)

    # choose best (if any) ensuring it's a value (edge >= 0.5)
    best = pick_best(all_bets)

    # post
    if best:
        if best["bet_key"] not in posted_keys:
            posted_keys.add(best["bet_key"])
            ch = bot.get_channel(CH_BEST)
            if ch:
                emb = embed_from_bet(best, "⭐ Best Bet", discord.Color.gold())
                await ch.send(embed=emb, view=stake_buttons(best["bet_key"]))
            # duplicate to value-channel too
            if CH_VALUE:
                chv = bot.get_channel(CH_VALUE)
                if chv:
                    emb = embed_from_bet(best, "⭐ Best Bet", discord.Color.gold())
                    await chv.send(embed=emb, view=stake_buttons(best["bet_key"]))
            upsert_bet(best)

    # quick + long (also send to value channel)
    for bet in all_bets:
        if bet["bet_key"] in posted_keys:
            # ensure DB upsert
            upsert_bet(bet)
            continue

        title = "⏱ Quick Return Bet" if bet["category"] == "quick" else "📅 Longer Play Bet"
        color = discord.Color.green() if bet["edge"] >= 2.0 else discord.Color.dark_grey()
        channel_id = CH_QUICK if bet["category"] == "quick" else CH_LONG
        ch = bot.get_channel(channel_id)
        if ch:
            posted_keys.add(bet["bet_key"])
            emb = embed_from_bet(bet, title, color)
            await ch.send(embed=emb, view=stake_buttons(bet["bet_key"]))
        # duplicate to value channel
        if CH_VALUE:
            chv = bot.get_channel(CH_VALUE)
            if chv:
                emb = embed_from_bet(bet, title, color)
                await chv.send(embed=emb, view=stake_buttons(bet["bet_key"]))
        upsert_bet(bet)

# background loop
@tasks.loop(minutes=5)
async def bet_loop():
    try:
        await fetch_and_post()
    except Exception as e:
        print("bet_loop error:", e)

# ------------- Interaction handlers -------------

@bot.event
async def on_interaction(inter: discord.Interaction):
    try:
        if inter.type == discord.InteractionType.component and inter.data:
            data = json.loads(inter.data.get("custom_id", "{}"))
            if data.get("t") == "stake":
                bet_key = data.get("k")
                skey = data.get("s")
                if skey not in {"conservative", "smart", "aggressive"}:
                    await inter.response.send_message("Unknown stake.", ephemeral=True)
                    return

                # need the bet to compute stake suggestions
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT * FROM bets WHERE bet_key=%s", (bet_key,))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if not row:
                    await inter.response.send_message(
                        "Sorry, I couldn't find this bet yet. Please try again in a few seconds.",
                        ephemeral=True
                    )
                    return

                cons, smart, aggr = stake_suggestions(float(row["edge"]))
                stake_map = {
                    "conservative": cons,
                    "smart": smart,
                    "aggressive": aggr
                }
                stake_units = stake_map[skey]
                ok, msg = save_user_bet(inter.user.id, inter.user.name, skey, stake_units, bet_key)
                await inter.response.send_message(msg, ephemeral=True)
    except Exception as e:
        try:
            await inter.response.send_message(f"⚠️ Error: {e}", ephemeral=True)
        except:
            pass

# ------------- Commands -------------

@bot.tree.command(name="ping", description="Bot latency check")
async def ping_cmd(inter: discord.Interaction):
    await inter.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Manually fetch & post new bets now")
async def fetchbets_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    try:
        await fetch_and_post()
        await inter.followup.send("✅ Fetched and posted (if any).", ephemeral=True)
    except Exception as e:
        await inter.followup.send(f"❌ Error: {e}", ephemeral=True)

@bot.tree.command(name="stats", description="Your personal performance (paper trading)")
async def stats_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    conn = get_db()
    cur = conn.cursor()
    uid = inter.user.id

    # personal realized
    cur.execute("""
        SELECT 
            COUNT(*) AS total_bets,
            COALESCE(SUM(CASE WHEN result='win' THEN 1 WHEN result='loss' THEN 0 END),0) AS wins,
            COALESCE(SUM(pnl),0) AS pnl
        FROM user_bets
        WHERE user_id=%s
    """, (uid,))
    r = cur.fetchone()
    total_bets = int(r["total_bets"] or 0)
    wins = int(r["wins"] or 0)
    pnl = float(r["pnl"] or 0.0)
    winrate = (wins/total_bets*100.0) if total_bets else 0.0

    # expected
    # join to bets to compute expected pnl from consensus
    cur.execute("""
        SELECT ub.stake_units, b.odds, b.consensus
        FROM user_bets ub
        JOIN bets b ON b.bet_key=ub.bet_key
        WHERE ub.user_id=%s
    """, (uid,))
    rows = cur.fetchall()
    exp = 0.0
    stake_sum = 0.0
    for rr in rows:
        stake = float(rr["stake_units"] or 0.0)
        odds = float(rr["odds"] or 0.0)
        cons = float(rr["consensus"] or 0.0)/100.0
        exp += cons*stake*odds - stake
        stake_sum += stake
    roi_real = (pnl/stake_sum*100.0) if stake_sum else 0.0
    roi_exp = (exp/stake_sum*100.0) if stake_sum else 0.0

    cur.close()
    conn.close()

    msg = (
        f"**Your Stats (@{inter.user.name})**\n"
        f"Total bets: **{total_bets}**\n"
        f"Win rate: **{winrate:.2f}%**\n"
        f"Realized P/L: **{pnl:.2f} units** | ROI: **{roi_real:.2f}%**\n"
        f"Expected P/L: **{exp:.2f} units** | Exp. ROI: **{roi_exp:.2f}%**\n"
    )
    await inter.followup.send(msg, ephemeral=True)

@bot.tree.command(name="roi", description="System-wide ROI (paper trading)")
async def roi_cmd(inter: discord.Interaction):
    await inter.response.defer(ephemeral=True, thinking=True)
    conn = get_db()
    cur = conn.cursor()

    # realized
    cur.execute("""
        SELECT 
            COALESCE(SUM(pnl),0) AS pnl,
            COALESCE(SUM(stake_units),0) AS st
        FROM user_bets
    """)
    r = cur.fetchone()
    pnl_all = float(r["pnl"] or 0.0)
    stake_all = float(r["st"] or 0.0)
    roi_real = (pnl_all/stake_all*100.0) if stake_all else 0.0

    # expected (join on bets)
    cur.execute("""
        SELECT ub.stake_units, b.odds, b.consensus
        FROM user_bets ub
        JOIN bets b ON b.bet_key=ub.bet_key
    """)
    rows = cur.fetchall()
    exp = 0.0
    st2 = 0.0
    for rr in rows:
        stake = float(rr["stake_units"] or 0.0)
        odds = float(rr["odds"] or 0.0)
        cons = float(rr["consensus"] or 0.0)/100.0
        exp += cons*stake*odds - stake
        st2 += stake

    roi_exp = (exp/st2*100.0) if st2 else 0.0
    cur.close()
    conn.close()

    msg = (
        f"**System ROI**\n"
        f"Realized P/L: **{pnl_all:.2f} units** | ROI: **{roi_real:.2f}%** (across all users)\n"
        f"Expected P/L: **{exp:.2f} units** | Exp. ROI: **{roi_exp:.2f}%**\n"
    )
    await inter.followup.send(msg, ephemeral=True)

# ------------- Startup -------------

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    # sync commands fresh each boot (avoids stale state)
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("Slash sync failed:", e)

    # init DB
    try:
        init_db()
        print("✅ DB ready.")
    except Exception as e:
        print("DB init failed:", e)

    if not bet_loop.is_running():
        bet_loop.start()

# ------------- Run -------------

if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")

bot.run(TOKEN)



