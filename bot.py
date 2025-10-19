import os
import math
import json
import asyncio
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks

import psycopg2
import psycopg2.extras

import requests

# ===============================
# ENV / CONFIG (leave as you had)
# ===============================
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

# Optional: a dedicated Value Bets testing channel the user gave earlier
VALUE_BETS_TESTING_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE_TESTING", "1422337929392689233"))

DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

# bookmaker filter you were using (AU focus + a couple others)
ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab", "unibet", "grosvenor", "william hill"
]

# Your existing stake settings (units)
BANKROLL_UNITS = 1000.0
CONSERVATIVE_PCT = 0.015  # 1.5%


# ===============================
# DISCORD BOT
# ===============================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ===============================
# DB
# ===============================
def get_db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL / DATABASE_PUBLIC_URL env var missing.")
    return psycopg2.connect(DATABASE_URL)


def ensure_tables():
    conn = get_db_conn()
    cur = conn.cursor()
    # bets: your paper feed capture (lightweight; you may already have it)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        id SERIAL PRIMARY KEY,
        bet_key TEXT UNIQUE,
        event_id TEXT,
        sport_key TEXT,
        league TEXT,
        match TEXT,
        bookmaker TEXT,
        team TEXT,
        odds NUMERIC,
        edge NUMERIC,
        commence_time TIMESTAMP,
        category TEXT,            -- 'best' | 'quick' | 'long' etc
        created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
    );
    """)
    # user_bets: what users place via buttons (paper trading)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_bets (
        id SERIAL PRIMARY KEY,
        user_id TEXT,
        username TEXT,
        bet_key TEXT,
        event_id TEXT,
        sport TEXT,
        team TEXT,
        odds NUMERIC,
        stake_type TEXT,         -- 'conservative' | 'smart' | 'aggressive'
        stake_units NUMERIC,
        -- settlement fields (new)
        result TEXT DEFAULT 'pending',     -- 'pending' | 'win' | 'loss'
        settled_at TIMESTAMP NULL,
        payout_units NUMERIC DEFAULT 0,
        commence_time TIMESTAMP NULL,
        created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
    );
    """)
    conn.commit()
    cur.close()
    conn.close()


# ===============================
# UTILS
# ===============================
def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)


def _dt_from_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def short_bet_key(event_id: str, team: str, bookmaker: str) -> str:
    base = f"{event_id}:{team}:{bookmaker}"
    return base[:128]  # safe short key


# ===============================
# FETCH ODDS (kept lightweight)
# ===============================
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

def fetch_upcoming_odds():
    """Basic upcoming odds pull, filtered to your bookmakers and common markets."""
    url = f"{ODDS_API_BASE}/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,totals,spreads",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []


# ===============================
# STAKING / EMBED FORMAT (as-is)
# ===============================
def stake_blocks(price: float, edge_pct: float):
    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
    smart_units = round(cons_units * (1 + max(edge_pct, 0)/100 * 2.0), 2)
    aggr_units  = round(cons_units * (1 + max(edge_pct, 0)/100 * 4.0), 2)

    cons_payout = round(cons_units * price, 2)
    smart_payout = round(smart_units * price, 2)
    aggr_payout = round(aggr_units * price, 2)

    # Expected profit (approx) using consensus is kept from your earlier logic
    # For display we keep "units" instead of "$"
    return {
        "conservative": (cons_units, cons_payout),
        "smart": (smart_units, smart_payout),
        "aggressive": (aggr_units, aggr_payout),
    }


def sport_emoji_and_name(sport_key: str, league: str | None):
    s = (sport_key or "").lower()
    # map
    if "soccer" in s or s in ("football_socc", "soccer"):
        emoji = "⚽"
        sport = "Soccer"
    elif "americanfootball" in s or "nfl" in s or "ncaaf" in s:
        emoji = "🏈"
        sport = "American Football"
    elif "basketball" in s:
        emoji = "🏀"
        sport = "Basketball"
    elif "tennis" in s:
        emoji = "🎾"
        sport = "Tennis"
    elif "baseball" in s:
        emoji = "⚾"
        sport = "Baseball"
    elif "icehockey" in s or "nhl" in s:
        emoji = "🏒"
        sport = "Ice Hockey"
    elif "mma" in s or "ufc" in s:
        emoji = "🥊"
        sport = "MMA"
    elif "cricket" in s:
        emoji = "🏏"
        sport = "Cricket"
    elif "esports" in s:
        emoji = "🎮"
        sport = "Esports"
    else:
        emoji = "🎯"
        sport = "Sport"

    league_text = league if league else "Unknown League"
    label = f"{emoji} {sport} ({league_text})"
    return label


def format_bet_embed(title: str, b: dict) -> discord.Embed:
    # b: dict with keys match, pick(team), bookmaker, consensus, implied, edge, time, odds, sport_key, league
    label = sport_emoji_and_name(b.get("sport_key"), b.get("league"))
    description = (
        f"{label}\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['implied']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['time']}\n"
    )
    color = 0x2ECC71 if b.get("edge", 0) >= 2 else 0x95A5A6  # green for value, gray for low
    e = discord.Embed(title=title, description=description, color=color)

    # stake block (units)
    st = stake_blocks(b["odds"], b["edge"])
    e.add_field(name="🪙 Conservative Stake",
                value=f"{st['conservative'][0]} units → Payout: {st['conservative'][1]} | Exp. Profit: ~",
                inline=False)
    e.add_field(name="🧠 Smart Stake",
                value=f"{st['smart'][0]} units → Payout: {st['smart'][1]} | Exp. Profit: ~",
                inline=False)
    e.add_field(name="🔥 Aggressive Stake",
                value=f"{st['aggressive'][0]} units → Payout: {st['aggressive'][1]} | Exp. Profit: ~",
                inline=False)
    return e


# ===============================
# VIEW BUTTONS (unchanged in spirit)
# ===============================
class PlaceBetView(discord.ui.View):
    def __init__(self, bet_payload: dict, timeout: float = 120):
        super().__init__(timeout=timeout)
        # bet_payload: everything required to record a user bet
        self.bet_payload = bet_payload

    async def _handle(self, interaction: discord.Interaction, stake_type: str):
        try:
            conn = get_db_conn()
            cur = conn.cursor()
            # compute stake units
            edge = float(self.bet_payload.get("edge") or 0)
            odds = float(self.bet_payload.get("odds"))
            st = stake_blocks(odds, edge)
            units = st[stake_type][0]

            # save user bet with commence_time
            save_user_bet(
                conn,
                user_id=interaction.user.id,
                username=interaction.user.name,
                bet_key=self.bet_payload["bet_key"],
                event_id=self.bet_payload.get("event_id"),
                sport_key=self.bet_payload.get("sport_key"),
                team_pick=self.bet_payload.get("team"),
                odds=odds,
                stake_type=stake_type,
                stake_units=units,
                commence_iso=self.bet_payload.get("commence_time"),
            )

            cur.close()
            conn.close()
            await interaction.response.send_message(
                f"✅ Recorded **{stake_type}** bet: {units} units on `{self.bet_payload.get('team')}` @ {odds}",
                ephemeral=True
            )
        except Exception as e:
            print("record bet error:", e)
            await interaction.response.send_message(
                "❌ Could not save your bet. Is the database configured?",
                ephemeral=True
            )

    @discord.ui.button(label="Conservative", style=discord.ButtonStyle.secondary, emoji="🪙")
    async def conservative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "conservative")

    @discord.ui.button(label="Smart", style=discord.ButtonStyle.primary, emoji="🧠")
    async def smart(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "smart")

    @discord.ui.button(label="Aggressive", style=discord.ButtonStyle.danger, emoji="🔥")
    async def aggressive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, "aggressive")


# ===============================
# SAVE USER BET (now stores commence_time)
# ===============================
def save_user_bet(conn, *, user_id, username, bet_key, event_id, sport_key,
                  team_pick, odds, stake_type, stake_units, commence_iso):
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_bets
            (user_id, username, bet_key, event_id, sport, team, odds,
             stake_type, stake_units, result, settled_at, payout_units, commence_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NULL,0,%s)
        ON CONFLICT DO NOTHING
    """, (
        str(user_id), str(username), bet_key, event_id, sport_key,
        team_pick, float(odds), stake_type, float(stake_units),
        _dt_from_iso(commence_iso)
    ))
    conn.commit()
    cur.close()


# ===============================
# POST BETS (kept close to your flow)
# ===============================
async def post_bet(channel: discord.abc.Messageable, title: str, b: dict):
    embed = format_bet_embed(title, b)
    view = PlaceBetView({
        "bet_key": b["bet_key"],
        "event_id": b.get("event_id"),
        "sport_key": b.get("sport_key"),
        "team": b["team"],
        "odds": b["odds"],
        "edge": b["edge"],
        "commence_time": b.get("commence_iso")
    })
    await channel.send(embed=embed, view=view)


# ===============================
# FETCH + CLASSIFY + POST LOOP
# (lightweight; unchanged behavior)
# ===============================
def classify_event(event: dict) -> list[dict]:
    """Turn an odds event into one or more bet candidates; only the essence is kept."""
    out = []
    home = event.get("home_team")
    away = event.get("away_team")
    match_name = f"{home} vs {away}"
    commence_iso = event.get("commence_time")
    commence_dt = _dt_from_iso(commence_iso)
    if not commence_dt:
        return out
    delta = commence_dt - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0 or delta > timedelta(days=150):
        return out

    # league best-effort
    sport_key = event.get("sport_key") or ""
    league = event.get("sport_title") or ""

    # consensus (crude aggregation)
    prices_map = {}  # outcome name -> list of prices
    for book in event.get("bookmakers", []):
        if not _allowed_bookmaker(book.get("title", "")):
            continue
        for m in book.get("markets", []):
            for o in m.get("outcomes", []):
                if o.get("price") and o.get("name"):
                    k = f"{m['key']}:{o['name']}"
                    prices_map.setdefault(k, []).append(float(o["price"]))

    if not prices_map:
        return out

    global_consensus = sum(1/p for plist in prices_map.values() for p in plist) / max(
        1, sum(len(plist) for plist in prices_map.values())
    )

    def outcome_iter():
        for book in event.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for m in book.get("markets", []):
                for o in m.get("outcomes", []):
                    if not o.get("price") or not o.get("name"):
                        continue
                    price = float(o["price"])
                    name = o["name"]
                    key = f"{m['key']}:{name}"
                    implied = 1/price
                    consensus = sum(1/p for p in prices_map.get(key, [])) / max(1, len(prices_map.get(key, [])))
                    edge = (consensus - implied) * 100.0
                    yield {
                        "market": m["key"], "team": name, "odds": price,
                        "consensus": round(consensus*100, 2),
                        "implied": round(implied*100, 2),
                        "edge": round(edge, 2),
                        "bookmaker": book.get("title") or "Unknown",
                    }

    cat = "quick" if delta <= timedelta(hours=48) else "long"
    for o in outcome_iter():
        bet = {
            "bet_key": short_bet_key(event.get("id", ""), o["team"], o["bookmaker"]),
            "event_id": event.get("id"),
            "sport_key": sport_key,
            "league": league,
            "match": match_name,
            "team": o["team"],
            "odds": o["odds"],
            "consensus": o["consensus"],
            "implied": o["implied"],
            "edge": o["edge"],
            "bookmaker": o["bookmaker"],
            "time": commence_dt.strftime("%d/%m/%y %H:%M"),
            "commence_iso": commence_iso,
            "category": cat,
        }
        out.append(bet)

    return out


async def post_bets():
    data = fetch_upcoming_odds()
    if not data:
        return

    # choose best + some quick/long (unchanged spirit)
    candidates = []
    for ev in data:
        candidates.extend(classify_event(ev))

    if not candidates:
        return

    # best = max by (consensus, edge)
    best = max(candidates, key=lambda x: (x["consensus"], x["edge"]))
    best_ch = bot.get_channel(BEST_BETS_CHANNEL)
    if best_ch:
        await post_bet(best_ch, "⭐ Best Bet", best)

    # quick & long postings (limit small for noise control)
    q_ch = bot.get_channel(QUICK_RETURNS_CHANNEL)
    l_ch = bot.get_channel(LONG_PLAYS_CHANNEL)

    quick = [b for b in candidates if b["category"] == "quick"]
    longp = [b for b in candidates if b["category"] == "long"]

    for b in quick[:3]:
        if q_ch:
            await post_bet(q_ch, "⏱ Quick Return Bet", b)

    for b in longp[:3]:
        if l_ch:
            await post_bet(l_ch, "📅 Longer Play Bet", b)

    # also mirror Value bets (testing channel) if they qualify
    vtc = bot.get_channel(VALUE_BETS_TESTING_CHANNEL)
    if vtc:
        for b in [best] + quick[:2]:
            await post_bet(vtc, "🔰 Value Bet (Testing)", b)


@tasks.loop(minutes=10)
async def bet_loop():
    try:
        await post_bets()
    except Exception as e:
        print("bet loop error:", e)


# ===============================
# SCORES + SETTLEMENT (NEW)
# ===============================
ODDS_SCORES_URL = f"{ODDS_API_BASE}/sports/{{sport_key}}/scores/"

def _fetch_scores_for_sport(sport_key: str, days_from: int = 7) -> list[dict]:
    try:
        params = {"apiKey": ODDS_API_KEY, "daysFrom": days_from}
        r = requests.get(ODDS_SCORES_URL.format(sport_key=sport_key), params=params, timeout=15)
        r.raise_for_status()
        return r.json() or []
    except Exception as e:
        print(f"[settle] score fetch failed for {sport_key}: {e}")
        return []

def _pick_is_h2h(name: str) -> bool:
    n = (name or "").strip().lower()
    return n not in ("over", "under") and not any(x in n for x in [" over ", " under ", "over ", "under "])

def _winner_name_from_scores(ev: dict) -> str | None:
    try:
        if not ev.get("completed"):
            return None
        scores = ev.get("scores") or []
        if len(scores) < 2:
            return None
        s_sorted = sorted(scores, key=lambda x: float(x.get("score") or 0), reverse=True)
        top = s_sorted[0]
        # draw?
        if len(scores) >= 2 and float(s_sorted[0].get("score") or 0) == float(s_sorted[1].get("score") or 0):
            return "draw"
        return (top.get("name") or "").strip().lower()
    except Exception:
        return None

def _match_by_event_id(scores: list[dict], event_id: str) -> dict | None:
    for ev in scores:
        if ev.get("id") == event_id:
            return ev
    return None

def _match_by_teams_and_time(scores: list[dict], team_pick: str, commence_dt: datetime) -> dict | None:
    lo = commence_dt - timedelta(hours=4)
    hi = commence_dt + timedelta(hours=4)
    pick_l = (team_pick or "").strip().lower()
    for ev in scores:
        try:
            t = _dt_from_iso(ev.get("commence_time"))
            if not t or t < lo or t > hi:
                continue
            names = [ (ev.get("home_team") or "").lower(), (ev.get("away_team") or "").lower() ]
            if any(pick_l in n for n in names):
                return ev
        except Exception:
            continue
    return None

def _settle_row(row, ev) -> tuple[str, float]:
    team_pick = (row["team"] or "").strip().lower()
    if not _pick_is_h2h(team_pick):
        return ("pending", 0.0)

    winner = _winner_name_from_scores(ev)
    if not winner:
        return ("pending", 0.0)

    if winner == "draw":
        result = "win" if team_pick == "draw" else "loss"
    else:
        result = "win" if team_pick == winner else "loss"

    payout = float(row["stake_units"]) * float(row["odds"]) if result == "win" else 0.0
    return (result, payout)

@tasks.loop(minutes=15)
async def settlement_loop():
    try:
        conn = get_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
          SELECT DISTINCT sport
          FROM user_bets
          WHERE result='pending'
            AND commence_time IS NOT NULL
            AND commence_time < (NOW() AT TIME ZONE 'utc') - INTERVAL '2 hours'
        """)
        sports = [r["sport"] for r in cur.fetchall()]

        for sport_key in sports:
            scores = _fetch_scores_for_sport(sport_key, days_from=7)
            if not scores:
                continue

            cur.execute("""
              SELECT id, user_id, username, event_id, sport, team, odds, stake_units, commence_time
              FROM user_bets
              WHERE result='pending' AND sport=%s
                AND commence_time < (NOW() AT TIME ZONE 'utc') - INTERVAL '2 hours'
            """, (sport_key,))
            rows = cur.fetchall()

            for row in rows:
                ev = None
                if row["event_id"]:
                    ev = _match_by_event_id(scores, row["event_id"])
                if not ev and row["commence_time"]:
                    ev = _match_by_teams_and_time(scores, row["team"], row["commence_time"])
                if not ev:
                    continue

                result, payout = _settle_row(row, ev)
                if result == "pending":
                    continue

                cur.execute("""
                  UPDATE user_bets
                  SET result=%s, payout_units=%s, settled_at=(NOW() AT TIME ZONE 'utc')
                  WHERE id=%s
                """, (result, payout, row["id"]))
                conn.commit()

        cur.close()
        conn.close()
    except Exception as e:
        print(f"[settle] loop error: {e}")


# ===============================
# SLASH COMMANDS (kept minimal)
# ===============================
@bot.tree.command(name="fetchbets", description="Force a one-off fetch/post of bets now.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await post_bets()
        await interaction.followup.send("✅ Fetched and posted a batch of bets.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fetch failed: {e}", ephemeral=True)

@bot.tree.command(name="roi", description="Show paper-trading ROI (settled bets only).")
async def roi_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        conn = get_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        cur.execute("""
          SELECT stake_type,
                 COUNT(*) AS bets,
                 SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(stake_units),0) AS staked,
                 COALESCE(SUM(payout_units),0) AS returned
          FROM user_bets
          WHERE result IN ('win','loss')
          GROUP BY stake_type
          ORDER BY stake_type
        """)
        rows = cur.fetchall()

        cur.execute("""
          SELECT COUNT(*) AS bets,
                 SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(stake_units),0) AS staked,
                 COALESCE(SUM(payout_units),0) AS returned
          FROM user_bets
          WHERE result IN ('win','loss')
        """)
        tot = cur.fetchone() or {}

        def fmt(r):
            bets = int(r.get("bets") or 0)
            wins = int(r.get("wins") or 0)
            losses = int(r.get("losses") or 0)
            staked = float(r.get("staked") or 0)
            returned = float(r.get("returned") or 0)
            pnl = returned - staked
            roi = (pnl / staked * 100) if staked > 0 else 0.0
            wr = (wins / bets * 100) if bets > 0 else 0.0
            return bets, wins, losses, wr, pnl, roi

        lines = []
        for r in rows:
            bets, wins, losses, wr, pnl, roi = fmt(r)
            lines.append(
                f"• **{r['stake_type']}** → {bets} bets | win-rate **{wr:.1f}%** | P&L **{pnl:.2f} units** | ROI **{roi:.2f}%**"
            )

        tb, tw, tl, twr, tpnl, troi = fmt(tot)
        lines.append(f"\n**Total** → {tb} bets | win-rate **{twr:.1f}%** | P&L **{tpnl:.2f} units** | ROI **{troi:.2f}%**")

        await interaction.followup.send("\n".join(lines), ephemeral=True)
        cur.close()
        conn.close()
    except Exception as e:
        await interaction.followup.send(f"⚠️ Could not compute ROI: `{e}`", ephemeral=True)


# ===============================
# READY
# ===============================
@bot.event
async def on_ready():
    ensure_tables()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
    except Exception as e:
        print("slash sync error:", e)

    if not bet_loop.is_running():
        bet_loop.start()
    if not settlement_loop.is_running():
        settlement_loop.start()


# ===============================
# RUN
# ===============================
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")
bot.run(TOKEN)






