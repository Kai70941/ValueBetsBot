# bot.py
import os
import json
import math
import time
import random
import asyncio
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord import app_commands, Interaction, Embed, Color
from discord.ext import commands, tasks

import psycopg2
import psycopg2.extras
import requests

# ------------- ENV -------------
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_DUP_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))  # optional
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip() or os.getenv("THEODDS_API_KEY", "").strip()

# Postgres connection (prefer DATABASE_URL, else DATABASE_PUBLIC_URL)
DATABASE_URL = (
    os.getenv("DATABASE_URL", "").strip()
    or os.getenv("DATABASE_PUBLIC_URL", "").strip()
)

# ------------- CONSTANTS -------------
BANKROLL_UNITS = 1000.0                  # “units” bankroll notion (not currency)
CONSERVATIVE_PCT = 0.015                 # 1.5% conservative
BOOKMAKER_WHITELIST = {
    # Your 9 AU/UK books
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
}

# --- Bookmaker → Discord channel routing (duplicate posts to selected app channels) ---
BOOKMAKER_CHANNEL_IDS = {
    "tabtouch": 1452828790567993415,
    "sportsbet": 1452828858658324596,
    "bet365": 1452828976060956753,
    "neds": 1452829020306800681,
    "ladbrokes": 1452829097440055306,
    "pointsbet": 1452829191945981963,
    "tab": 1452829245490335967,
    "betfair": 1452829323747659849,
}

def _normalize_bookmaker(name: str) -> str:
    if not name:
        return ""
    n = name.strip().lower()
    aliases = {
        "tab touch": "tabtouch",
        "tab-touch": "tabtouch",
        "tabtouch": "tabtouch",
        "tab": "tab",
        "bet 365": "bet365",
        "bet-365": "bet365",
        "points bet": "pointsbet",
        "pointbet": "pointsbet",
    }
    return aliases.get(n, n)

# Sport emoji + league name mapper (league fallback “Unknown League”)
SPORT_EMOJI = {
    "soccer": "⚽",
    "basketball": "🏀",
    "tennis": "🎾",
    "americanfootball": "🏈",
    "icehockey": "🏒",
    "baseball": "⚾",
    "aussierules": "🏉",
    "mma": "🥊",
    "boxing": "🥊",
    "cricket": "🏏",
    "formula1": "🏎️",
    "rugbyleague": "🏉",
    "rugbynunion": "🏉",
}

# In-memory index of posted bets so our buttons know what to save
# bet_key -> bet dict
POSTED_BETS: dict[str, dict] = {}

# ------------- DB HELPERS -------------
def get_db_conn():
    if not DATABASE_URL:
        return None
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def ensure_schema():
    """Create tables if missing and ensure all expected columns exist."""
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    conn.autocommit = True
    cur = conn.cursor()
    # bets table (for paper feed / auditing)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
          id SERIAL PRIMARY KEY,
          event_id TEXT,
          bet_key TEXT UNIQUE,
          match TEXT,
          bookmaker TEXT,
          team TEXT,
          odds NUMERIC,
          edge NUMERIC,
          bet_time TIMESTAMPTZ,
          category TEXT,
          sport TEXT,
          league TEXT,
          created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    # user_bets table (button clicks by real users)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
          id SERIAL PRIMARY KEY,
          user_id BIGINT,
          username TEXT,
          bet_key TEXT,
          event_id TEXT,
          sport TEXT,
          league TEXT,
          stake_type TEXT,       -- 'conservative'|'smart'|'aggressive'
          stake_units NUMERIC,
          odds NUMERIC,
          placed_at TIMESTAMPTZ DEFAULT NOW(),
          -- paper-trading settlement:
          result TEXT,           -- 'win'|'loss'|'void'|NULL
          settled_at TIMESTAMPTZ,
          pnl_units NUMERIC
        );
    """)

    # add missing columns defensively (safe to run repeatedly)
    for (table, col, typ) in [
        ("user_bets", "stake_type", "TEXT"),
        ("user_bets", "pnl_units", "NUMERIC"),
        ("user_bets", "result", "TEXT"),
        ("user_bets", "settled_at", "TIMESTAMPTZ"),
        ("user_bets", "league", "TEXT"),

        # NEW: needed for settlement of spreads/totals too
        ("bets", "market", "TEXT"),
        ("bets", "point", "NUMERIC"),
        ("user_bets", "market", "TEXT"),
        ("user_bets", "point", "NUMERIC"),
    ]:
        cur.execute(f"""
          DO $$
          BEGIN
            IF NOT EXISTS (
              SELECT 1 FROM information_schema.columns
               WHERE table_name='{table}'
                 AND column_name='{col}'
            ) THEN
              ALTER TABLE {table} ADD COLUMN {col} {typ};
            END IF;
          END$$;
        """)
    cur.close()
    conn.close()

def save_bet_row(bet: dict):
    """Insert the bet in bets table (ignore if exists)."""
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO bets (event_id, bet_key, match, bookmaker, team, odds, edge, bet_time,
                        category, sport, league, market, point)
      VALUES (%(event_id)s, %(bet_key)s, %(match)s, %(bookmaker)s, %(team)s, %(odds)s,
              %(edge)s, %(bet_time)s, %(category)s, %(sport)s, %(league)s, %(market)s, %(point)s)
      ON CONFLICT (bet_key) DO NOTHING;
    """, bet)
    conn.commit()
    cur.close()
    conn.close()

def save_user_bet(user: discord.User | discord.Member, bet: dict, stake_type: str, stake_units: float) -> int:
    """Insert a user's placed bet; returns inserted id."""
    if not DATABASE_URL:
        raise RuntimeError("DB not configured")
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      INSERT INTO user_bets
        (user_id, username, bet_key, event_id, sport, league, stake_type, stake_units, odds, market, point)
      VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
      RETURNING id;
    """, (
        int(user.id), str(user.name), bet["bet_key"], bet.get("event_id"),
        bet.get("sport"), bet.get("league"), stake_type, stake_units, bet.get("odds"),
        bet.get("market"), bet.get("point")
    ))
    row_id = cur.fetchone()["id"]
    conn.commit()
    cur.close()
    conn.close()
    return row_id

def db_agg_total() -> dict:
    """System-wide paper-stats from user_bets."""
    if not DATABASE_URL:
        return {"bets": 0, "staked": 0.0, "pnl": 0.0, "wins": 0, "settled": 0}
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        COUNT(*)::INT as bets,
        COALESCE(SUM(stake_units),0) as staked,
        COALESCE(SUM(pnl_units),0) as pnl,
        COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0)::INT as wins,
        COALESCE(SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END),0)::INT as settled
      FROM user_bets;
    """)
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def db_agg_user(user_id: int) -> dict:
    if not DATABASE_URL:
        return {"bets": 0, "staked": 0.0, "pnl": 0.0, "wins": 0, "settled": 0}
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        COUNT(*)::INT as bets,
        COALESCE(SUM(stake_units),0) as staked,
        COALESCE(SUM(pnl_units),0) as pnl,
        COALESCE(SUM(CASE WHEN result='win' THEN 1 ELSE 0 END),0)::INT as wins,
        COALESCE(SUM(CASE WHEN result IS NOT NULL THEN 1 ELSE 0 END),0)::INT as settled
      FROM user_bets
      WHERE user_id = %s;
    """, (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row

def db_get_unsettled(limit: int = 500) -> list[dict]:
    """Fetch unsettled user bets, enriched with bet details when available."""
    if not DATABASE_URL:
        return []
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      SELECT
        ub.id as user_bet_id,
        ub.user_id,
        ub.bet_key,
        ub.event_id,
        ub.sport,
        ub.stake_units,
        ub.odds,
        COALESCE(ub.market, b.market) as market,
        COALESCE(ub.point, b.point) as point,
        COALESCE(b.team, '') as team,
        COALESCE(b.match, '') as match,
        COALESCE(b.bet_time, ub.placed_at) as bet_time
      FROM user_bets ub
      LEFT JOIN bets b ON b.bet_key = ub.bet_key
      WHERE ub.result IS NULL
      ORDER BY ub.placed_at ASC
      LIMIT %s;
    """, (limit,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def db_settle_user_bet(user_bet_id: int, result: str, pnl_units: float):
    if not DATABASE_URL:
        return
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute("""
      UPDATE user_bets
      SET result = %s,
          pnl_units = %s,
          settled_at = NOW()
      WHERE id = %s
        AND result IS NULL;
    """, (result, pnl_units, user_bet_id))
    conn.commit()
    cur.close(); conn.close()

# ------------- ODDS FETCH (TheOddsAPI) -------------
def allowed_book(title: str) -> bool:
    return any(k in (title or "").lower() for k in BOOKMAKER_WHITELIST)

def theodds_fetch_upcoming():
    """Fetch a small sample for /fetchbets on-demand (safe on credits)."""
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,uk,us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []

def theodds_fetch_scores(sport_key: str, days_from: int = 7):
    """Fetch completed scores for a sport to settle bets."""
    url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/"
    params = {
        "apiKey": ODDS_API_KEY,
        "daysFrom": days_from,
        "dateFormat": "iso",
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []

def _parse_score_value(x):
    try:
        return float(x)
    except Exception:
        return None

def _event_scores_map(scores_payload: list[dict]) -> dict:
    """
    Build map: event_id -> dict with home, away, home_score, away_score, completed
    TheOddsAPI scores payload usually includes: id, home_team, away_team, completed, scores:[{name,score},...]
    """
    out = {}
    for ev in scores_payload or []:
        eid = ev.get("id")
        if not eid:
            continue
        completed = bool(ev.get("completed"))
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        hs = None
        as_ = None

        scores = ev.get("scores") or []
        if isinstance(scores, list):
            for s in scores:
                nm = (s.get("name") or "").strip()
                sc = _parse_score_value(s.get("score"))
                if not nm:
                    continue
                if nm == home:
                    hs = sc
                elif nm == away:
                    as_ = sc

        out[eid] = {
            "completed": completed,
            "home_team": home,
            "away_team": away,
            "home_score": hs,
            "away_score": as_,
        }
    return out

def _settle_calc(market: str, team: str, point, home_team: str, away_team: str, home_score: float, away_score: float):
    """
    Returns: 'win'|'loss'|'void'|None
    Supports: h2h, totals, spreads
    """
    market = (market or "").lower()
    team_norm = (team or "").strip().lower()

    if home_score is None or away_score is None:
        return None

    # H2H
    if market == "h2h":
        if home_score == away_score:
            return "void"
        winner = home_team if home_score > away_score else away_team
        return "win" if team.strip().lower() == winner.strip().lower() else "loss"

    # TOTALS
    if market == "totals":
        if point is None:
            return None
        try:
            line = float(point)
        except Exception:
            return None
        total = home_score + away_score
        if abs(total - line) < 1e-9:
            return "void"
        if team_norm == "over":
            return "win" if total > line else "loss"
        if team_norm == "under":
            return "win" if total < line else "loss"
        return None

    # SPREADS
    if market == "spreads":
        if point is None:
            return None
        try:
            spread = float(point)
        except Exception:
            return None

        # outcome name is usually the team; apply spread to that team's score
        if team.strip().lower() == home_team.strip().lower():
            adj = home_score + spread
            if abs(adj - away_score) < 1e-9:
                return "void"
            return "win" if adj > away_score else "loss"

        if team.strip().lower() == away_team.strip().lower():
            adj = away_score + spread
            if abs(adj - home_score) < 1e-9:
                return "void"
            return "win" if adj > home_score else "loss"

        return None

    # Unknown market
    return None

def compute_bets_from_payload(payload):
    """Very similar to what you had — compute bets & classifications."""
    now = datetime.now(timezone.utc)
    results = []
    for ev in payload:
        home = ev.get("home_team"); away = ev.get("away_team")
        if not home or not away:
            continue
        match_name = f"{home} vs {away}"
        commence = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(commence.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt <= now or dt > now + timedelta(days=150):
            continue

        # sport key + league try from event
        sport_key = (ev.get("sport_key") or "").lower()
        league = ev.get("sport_title") or ev.get("sport_title_long") or "Unknown League"
        sport_emoji = SPORT_EMOJI.get(sport_key, "🎲")

        # consensus from allowed books
        cs_map = defaultdict(list)
        for bk in ev.get("bookmakers", []):
            if not allowed_book(bk.get("title", "")):
                continue
            for m in bk.get("markets", []):
                for oc in m.get("outcomes", []):
                    nm = oc.get("name"); pr = oc.get("price")
                    if nm and pr:
                        cs_map[f"{m['key']}:{nm}"].append(1/float(pr))

        if not cs_map:
            continue
        # global consensus fallback
        tot_ps = [p for arr in cs_map.values() for p in arr]
        global_c = sum(tot_ps) / max(1, len(tot_ps))

        # each offered
        for bk in ev.get("bookmakers", []):
            if not allowed_book(bk.get("title", "")):
                continue
            for m in bk.get("markets", []):
                mk = m.get("key") or ""
                for oc in m.get("outcomes", []):
                    nm = oc.get("name"); pr = oc.get("price")
                    if not nm or not pr:
                        continue
                    implied = 1/float(pr)
                    keyo = f"{mk}:{nm}"
                    consensus = (sum(cs_map[keyo])/len(cs_map[keyo])) if keyo in cs_map else global_c
                    edge = (consensus - implied) * 100.0
                    if edge <= 0:
                        continue

                    # classification
                    delta = dt - now
                    quick = (delta <= timedelta(hours=48))
                    longp = (delta > timedelta(hours=48))
                    category = "quick" if quick else "long" if longp else "other"

                    # stake units
                    conservative_units = round(BANKROLL_UNITS*CONSERVATIVE_PCT, 2)
                    smart_units = round(conservative_units * max(1.0, (consensus*100)/50.0), 2)
                    aggressive_units = round(conservative_units * (1 + (edge/10.0)), 2)

                    bet_key = f"{match_name}|{mk}|{nm}|{bk['title']}|{dt.isoformat()}"

                    bet = {
                        "event_id": ev.get("id") or bet_key,
                        "bet_key": bet_key,
                        "match": match_name,
                        "bookmaker": bk.get("title", "Unknown"),
                        "team": nm,
                        "odds": float(pr),
                        "edge": round(edge, 2),
                        "probability": round(consensus*100, 2),
                        "consensus": round(consensus*100, 2),
                        "bet_time": dt,
                        "category": category,
                        "quick_return": quick,
                        "long_play": longp,
                        "sport": sport_key or "unknown",
                        "league": league,
                        "emoji": sport_emoji,
                        "conservative_units": conservative_units,
                        "smart_units": smart_units,
                        "aggressive_units": aggressive_units,

                        # NEW: settlement fields
                        "market": mk,
                        "point": oc.get("point"),  # for spreads/totals (None for h2h)
                    }
                    results.append(bet)
    return results

# ------------- EMBEDS + BUTTONS -------------
def value_indicator(edge_pct: float) -> str:
    return "🟢 Value Bet" if edge_pct >= 2 else "🛑 Low Value"

def bet_embed(bet: dict, title: str, color: int) -> Embed:
    ind = value_indicator(bet["edge"])
    sport_line = f"{bet['emoji']} {bet['sport'].title()} ({bet.get('league') or 'Unknown League'})"

    market = (bet.get("market") or "").lower()
    point = bet.get("point")
    market_line = ""
    if market == "totals" and point is not None:
        market_line = f"\n**Market:** Totals ({bet['team']} {point})"
    elif market == "spreads" and point is not None:
        market_line = f"\n**Market:** Spreads ({bet['team']} {point:+})"
    elif market:
        market_line = f"\n**Market:** {market.upper()}"

    desc = (
        f"{ind}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Pick:** {bet['team']} @ {bet['odds']}\n"
        f"**Bookmaker:** {bet['bookmaker']}"
        f"{market_line}\n"
        f"**Consensus %:** {bet['consensus']}%\n"
        f"**Implied %:** {round((1/bet['odds'])*100,2)}%\n"
        f"**Edge:** {bet['edge']}%\n"
        f"**Time:** {bet['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
        f"💵 **Conservative Stake:** {bet['conservative_units']} units\n"
        f"🧠 **Smart Stake:** {bet['smart_units']} units\n"
        f"🔥 **Aggressive Stake:** {bet['aggressive_units']} units\n"
    )
    e = Embed(title=title, description=desc, color=color)
    e.set_footer(text="Click a stake button below to record your paper-trade.")
    return e

class StakeButtons(discord.ui.View):
    def __init__(self, bet_key: str, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.bet_key = bet_key

    @discord.ui.button(label="Conservative", emoji="💵", style=discord.ButtonStyle.secondary)
    async def cons_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "conservative")

    @discord.ui.button(label="Smart", emoji="🧠", style=discord.ButtonStyle.primary)
    async def smart_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "smart")

    @discord.ui.button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger)
    async def aggr_btn(self, interaction: Interaction, button: discord.ui.Button):
        await self._save(interaction, "aggressive")

    async def _save(self, interaction: Interaction, stake_type: str):
        bet = POSTED_BETS.get(self.bet_key)
        if not bet:
            await interaction.response.send_message(
                "Sorry, I couldn't find this bet yet. Please try again in a few seconds.",
                ephemeral=True
            )
            return
        units = {
            "conservative": bet["conservative_units"],
            "smart": bet["smart_units"],
            "aggressive": bet["aggressive_units"]
        }[stake_type]
        try:
            row_id = save_user_bet(interaction.user, bet, stake_type, units)
        except Exception:
            await interaction.response.send_message(
                "❌ Could not save your bet. Is the database configured?",
                ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Saved your **{stake_type}** bet ({units} units). Entry #{row_id}.",
            ephemeral=True
        )

# ------------- DISCORD BOT -------------
intents = discord.Intents.default()
intents.message_content = True

class ValueBetsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.synced = False

    async def setup_hook(self):
        ensure_schema()

bot = ValueBetsBot()

@bot.event
async def on_ready():
    if not bot.synced:
        await bot.tree.sync()
        bot.synced = True
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

# -------- /ping ----------
@bot.tree.command(name="ping", description="Check bot latency.")
async def ping_cmd(interaction: Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)}ms", ephemeral=True)

# -------- /fetchbets ----
@bot.tree.command(name="fetchbets", description="Manually fetch a preview of incoming bets.")
async def fetchbets_cmd(interaction: Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    payload = theodds_fetch_upcoming()
    if not payload:
        await interaction.followup.send("No odds available (or API limit/unauthorized).", ephemeral=True)
        return
    bets = compute_bets_from_payload(payload)
    if not bets:
        await interaction.followup.send("No value bets found right now.", ephemeral=True)
        return

    lines = []
    for b in bets[:3]:
        lines.append(f"**{b['match']}** · {b['team']} @ {b['odds']} ({b['bookmaker']}) | Edge: {b['edge']}%")
    await interaction.followup.send("🎲 Bets Preview:\n" + "\n".join(lines), ephemeral=True)

# -------- /roi (system-wide) ----
@bot.tree.command(name="roi", description="System-wide ROI (all recorded user paper trades).")
async def roi_cmd(interaction: Interaction):
    agg = db_agg_total()
    staked = float(agg["staked"])
    pnl = float(agg["pnl"])
    roi = (pnl / staked * 100.0) if staked > 0 else 0.0
    wr = (agg["wins"] / agg["settled"] * 100.0) if agg["settled"] > 0 else 0.0
    msg = (f"📊 **System ROI**\n"
           f"- Bets: {agg['bets']}\n"
           f"- Staked: {staked:.2f} units\n"
           f"- P/L: {pnl:.2f} units\n"
           f"- ROI: {roi:.2f}%\n"
           f"- Win rate (settled): {wr:.2f}%\n"
           f"- Settled bets: {agg['settled']}")
    await interaction.response.send_message(msg, ephemeral=True)

# -------- /stats (personal) ----
@bot.tree.command(name="stats", description="Your personal paper-trading stats.")
async def stats_cmd(interaction: Interaction):
    agg = db_agg_user(interaction.user.id)
    staked = float(agg["staked"])
    pnl = float(agg["pnl"])
    roi = (pnl / staked * 100.0) if staked > 0 else 0.0
    wr = (agg["wins"] / agg["settled"] * 100.0) if agg["settled"] > 0 else 0.0
    msg = (f"🧾 **Your Stats**\n"
           f"- Bets: {agg['bets']}\n"
           f"- Staked: {staked:.2f} units\n"
           f"- P/L: {pnl:.2f} units\n"
           f"- ROI: {roi:.2f}%\n"
           f"- Win rate (settled): {wr:.2f}%\n"
           f"- Settled bets: {agg['settled']}")
    await interaction.response.send_message(msg, ephemeral=True)

# ------------- Posting helpers (used by your loop or elsewhere) -------------
async def post_bet_to_channels(bet: dict):
    """Posts a single bet embed with buttons; duplicates to app-specific channel; duplicates to VALUE_DUP_CHANNEL if configured."""
    channel_id = BEST_BETS_CHANNEL
    title = "⭐ Best Bet"
    color = Color.gold().value

    if bet.get("quick_return"):
        channel_id = QUICK_RETURNS_CHANNEL
        title = "⏱ Quick Return Bet"
        color = 0x2ECC71
    elif bet.get("long_play"):
        channel_id = LONG_PLAYS_CHANNEL
        title = "📅 Longer Play Bet"
        color = 0x3498DB

    e = bet_embed(bet, title, color)
    view = StakeButtons(bet["bet_key"])

    POSTED_BETS[bet["bet_key"]] = bet

    try:
        save_bet_row(bet)
    except Exception:
        pass

    if channel_id:
        ch = bot.get_channel(channel_id)
        if ch:
            await ch.send(embed=e, view=view)

    # duplicate to the appropriate bookmaker channel
    try:
        bm_key = _normalize_bookmaker(bet.get("bookmaker", ""))
        bm_channel_id = BOOKMAKER_CHANNEL_IDS.get(bm_key)
        if bm_channel_id:
            bm_ch = bot.get_channel(int(bm_channel_id))
            if bm_ch:
                await bm_ch.send(embed=e, view=StakeButtons(bet["bet_key"]))
    except Exception:
        pass

    if VALUE_DUP_CHANNEL:
        ch2 = bot.get_channel(VALUE_DUP_CHANNEL)
        if ch2:
            e2 = bet_embed(bet, "⭐ Value Bet (Testing)", Color.green().value)
            await ch2.send(embed=e2, view=StakeButtons(bet["bet_key"]))

# ------------- Settlement loop -------------
@tasks.loop(minutes=10)
async def settle_loop():
    if not ODDS_API_KEY or not DATABASE_URL:
        return

    unsettled = db_get_unsettled(limit=500)
    if not unsettled:
        return

    # group by sport to minimize API calls
    by_sport = defaultdict(list)
    for row in unsettled:
        sport_key = (row.get("sport") or "").lower()
        # only attempt settlement after start time
        bt = row.get("bet_time")
        if isinstance(bt, datetime):
            if bt.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc):
                continue
        if sport_key:
            by_sport[sport_key].append(row)

    for sport_key, rows in by_sport.items():
        scores_payload = theodds_fetch_scores(sport_key, days_from=7)
        smap = _event_scores_map(scores_payload)

        for r in rows:
            eid = r.get("event_id")
            if not eid or eid not in smap:
                continue

            ev = smap[eid]
            if not ev.get("completed"):
                continue

            hs = ev.get("home_score")
            aw = ev.get("away_score")
            if hs is None or aw is None:
                continue

            market = (r.get("market") or "").lower()
            team = r.get("team") or ""
            point = r.get("point")

            result = _settle_calc(market, team, point, ev["home_team"], ev["away_team"], hs, aw)
            if result is None:
                # can't determine; skip for now
                continue

            stake = float(r.get("stake_units") or 0.0)
            odds = float(r.get("odds") or 0.0)

            if result == "win":
                pnl = stake * (odds - 1.0)
            elif result == "loss":
                pnl = -stake
            else:  # void
                pnl = 0.0

            try:
                db_settle_user_bet(int(r["user_bet_id"]), result, float(round(pnl, 4)))
            except Exception:
                continue

# ------------- Example background loop (optional) -------------
@tasks.loop(minutes=5)
async def bet_loop():
    if not ODDS_API_KEY:
        return
    payload = theodds_fetch_upcoming()
    if not payload:
        return
    bets = compute_bets_from_payload(payload)
    if not bets:
        return

    bets.sort(key=lambda x: (x["edge"], x["consensus"]), reverse=True)
    for b in bets[:5]:
        try:
            await post_bet_to_channels(b)
            await asyncio.sleep(1.0)
        except Exception:
            continue

@bot.event
async def on_connect():
    if not bet_loop.is_running():
        bet_loop.start()
    if not settle_loop.is_running():
        settle_loop.start()

# ------------- RUN -------------
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")

bot.run(TOKEN)



