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

# ========= ENV =========
TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_BETS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_RETURNS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_PLAYS_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))
VALUE_BETS_TESTING_CHANNEL = int(os.getenv("DISCORD_CHANNEL_ID_VALUE_TESTING", "1422337929392689233"))

DATABASE_URL = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab", "unibet", "grosvenor", "william hill"
]

BANKROLL_UNITS = 1000.0
CONSERVATIVE_PCT = 0.015

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ========= BOT =========
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= DB =========
def get_db_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL / DATABASE_PUBLIC_URL env var missing.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def ensure_tables():
    conn = get_db_conn()
    cur = conn.cursor()
    # core tables (idempotent)
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
      category TEXT,
      created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
    );
    """)
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
      stake_type TEXT,
      stake_units NUMERIC,
      result TEXT DEFAULT 'pending',
      settled_at TIMESTAMP NULL,
      payout_units NUMERIC DEFAULT 0,
      commence_time TIMESTAMP NULL,
      created_at TIMESTAMP DEFAULT (NOW() AT TIME ZONE 'utc')
    );
    """)
    # Unique so a user can press once per stake type per bet
    cur.execute("""
    DO $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname='user_bets_one_tap_per_stake'
      ) THEN
        ALTER TABLE user_bets
        ADD CONSTRAINT user_bets_one_tap_per_stake
        UNIQUE (user_id, bet_key, stake_type);
      END IF;
    END$$;
    """)
    # helpful indexes
    cur.execute("CREATE INDEX IF NOT EXISTS user_bets_result_idx ON user_bets(result);")
    cur.execute("CREATE INDEX IF NOT EXISTS user_bets_commence_idx ON user_bets(commence_time);")
    cur.execute("CREATE INDEX IF NOT EXISTS user_bets_user_idx ON user_bets(user_id);")
    conn.commit()
    cur.close()
    conn.close()

# ========= UTILS =========
def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

def _dt_from_iso(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None

def short_bet_key(event_id: str, team: str, bookmaker: str) -> str:
    return (f"{event_id}:{team}:{bookmaker}")[:128]

def stake_blocks(price: float, edge_pct: float):
    cons_units = round(BANKROLL_UNITS * CONSERVATIVE_PCT, 2)
    smart_units = round(cons_units * (1 + max(edge_pct, 0)/100 * 2.0), 2)
    aggr_units  = round(cons_units * (1 + max(edge_pct, 0)/100 * 4.0), 2)
    cons_payout = round(cons_units * price, 2)
    smart_payout = round(smart_units * price, 2)
    aggr_payout = round(aggr_units * price, 2)
    return {
        "conservative": (cons_units, cons_payout),
        "smart": (smart_units, smart_payout),
        "aggressive": (aggr_units, aggr_payout),
    }

def sport_emoji_and_name(sport_key: str, league: str | None):
    s = (sport_key or "").lower()
    if "soccer" in s or s == "soccer":
        emoji, sport = "⚽", "Soccer"
    elif "americanfootball" in s or "nfl" in s or "ncaaf" in s:
        emoji, sport = "🏈", "American Football"
    elif "basketball" in s:
        emoji, sport = "🏀", "Basketball"
    elif "tennis" in s:
        emoji, sport = "🎾", "Tennis"
    elif "baseball" in s:
        emoji, sport = "⚾", "Baseball"
    elif "icehockey" in s:
        emoji, sport = "🏒", "Ice Hockey"
    elif "mma" in s:
        emoji, sport = "🥊", "MMA"
    elif "cricket" in s:
        emoji, sport = "🏏", "Cricket"
    elif "esports" in s:
        emoji, sport = "🎮", "Esports"
    else:
        emoji, sport = "🎯", "Sport"
    league_text = league if league else "Unknown League"
    return f"{emoji} {sport} ({league_text})"

def format_bet_embed(title: str, b: dict) -> discord.Embed:
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
    color = 0x2ECC71 if b.get("edge", 0) >= 2 else 0x95A5A6
    e = discord.Embed(title=title, description=description, color=color)
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

# ========= ODDS FETCH =========
def fetch_upcoming_odds():
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

def classify_event(event: dict):
    out = []
    home, away = event.get("home_team"), event.get("away_team")
    match_name = f"{home} vs {away}"
    commence_iso = event.get("commence_time")
    commence_dt = _dt_from_iso(commence_iso)
    if not commence_dt:
        return out
    delta = commence_dt - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0 or delta > timedelta(days=150):
        return out

    sport_key = event.get("sport_key") or ""
    league = event.get("sport_title") or ""

    prices_map = {}
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

    def outcomes():
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
                    plist = prices_map.get(key, [])
                    if plist:
                        consensus = sum(1/p for p in plist) / len(plist)
                    else:
                        # fallback to global
                        consensus = sum(1/p for plist2 in prices_map.values() for p in plist2) / \
                                    max(1, sum(len(pl) for pl in prices_map.values()))
                    edge = (consensus - implied) * 100.0
                    yield {
                        "market": m["key"], "team": name, "odds": price,
                        "consensus": round(consensus*100, 2),
                        "implied": round(implied*100, 2),
                        "edge": round(edge, 2),
                        "bookmaker": book.get("title") or "Unknown",
                    }

    category = "quick" if delta <= timedelta(hours=48) else "long"
    for o in outcomes():
        out.append({
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
            "category": category,
        })
    return out

# ========= SAVE/POST =========
class PlaceBetView(discord.ui.View):
    def __init__(self, bet_payload: dict, timeout: float = 120):
        super().__init__(timeout=timeout)
        self.bet_payload = bet_payload

    async def _handle(self, interaction: discord.Interaction, stake_type: str):
        try:
            conn = get_db_conn()
            cur = conn.cursor()
            edge = float(self.bet_payload.get("edge") or 0)
            odds = float(self.bet_payload.get("odds"))
            units = stake_blocks(odds, edge)[stake_type][0]

            # Upsert with our unique composite (user, bet_key, stake_type)
            cur.execute("""
              INSERT INTO user_bets
                (user_id, username, bet_key, event_id, sport, team, odds,
                 stake_type, stake_units, result, settled_at, payout_units, commence_time)
              VALUES
                (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',NULL,0,%s)
              ON CONFLICT (user_id, bet_key, stake_type) DO UPDATE
              SET stake_units=EXCLUDED.stake_units
            """, (
                str(interaction.user.id), str(interaction.user.name),
                self.bet_payload["bet_key"], self.bet_payload.get("event_id"),
                self.bet_payload.get("sport_key"), self.bet_payload.get("team"),
                odds, stake_type, float(units), _dt_from_iso(self.bet_payload.get("commence_time"))
            ))
            conn.commit()
            cur.close(); conn.close()

            await interaction.response.send_message(
                f"✅ Recorded **{stake_type}** bet: {units} units on `{self.bet_payload.get('team')}` @ {odds}",
                ephemeral=True
            )
        except Exception as e:
            # send the actual reason so we can fix quickly
            await interaction.response.send_message(
                f"❌ Could not save your bet.\n```\n{e}\n```",
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

async def post_bets():
    data = fetch_upcoming_odds()
    if not data:
        return
    candidates = []
    for ev in data:
        candidates.extend(classify_event(ev))
    if not candidates:
        return

    best = max(candidates, key=lambda x: (x["consensus"], x["edge"]))
    if (ch := bot.get_channel(BEST_BETS_CHANNEL)):
        await post_bet(ch, "⭐ Best Bet", best)

    quick = [b for b in candidates if b["category"] == "quick"][:3]
    longp = [b for b in candidates if b["category"] == "long"][:3]

    if (qch := bot.get_channel(QUICK_RETURNS_CHANNEL)):
        for b in quick:
            await post_bet(qch, "⏱ Quick Return Bet", b)
    if (lch := bot.get_channel(LONG_PLAYS_CHANNEL)):
        for b in longp:
            await post_bet(lch, "📅 Longer Play Bet", b)

    if (vtc := bot.get_channel(VALUE_BETS_TESTING_CHANNEL)):
        for b in [best] + quick[:2]:
            await post_bet(vtc, "🔰 Value Bet (Testing)", b)

@tasks.loop(minutes=10)
async def bet_loop():
    try:
        await post_bets()
    except Exception as e:
        print("bet loop error:", e)

# ========= SCORES + SETTLEMENT =========
ODDS_SCORES_URL = f"{ODDS_API_BASE}/sports/{{sport_key}}/scores/"

def _fetch_scores_for_sport(sport_key: str, days_from: int = 7):
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

def _winner_name_from_scores(ev: dict):
    try:
        if not ev.get("completed"):
            return None
        scores = ev.get("scores") or []
        if len(scores) < 2:
            return None
        s_sorted = sorted(scores, key=lambda x: float(x.get("score") or 0), reverse=True)
        top = s_sorted[0]
        # draw?
        if float(s_sorted[0].get("score") or 0) == float(s_sorted[1].get("score") or 0):
            return "draw"
        return (top.get("name") or "").strip().lower()
    except Exception:
        return None

def _match_by_event_id(scores, event_id: str):
    for ev in scores:
        if ev.get("id") == event_id:
            return ev
    return None

def _match_by_teams_and_time(scores, team_pick: str, commence_dt: datetime):
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

def _settle_row(row, ev):
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
        cur.close(); conn.close()
    except Exception as e:
        print(f"[settle] loop error: {e}")

# ========= SLASH COMMANDS =========
@bot.tree.command(name="fetchbets", description="Force a one-off fetch/post of bets now.")
async def fetchbets_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await post_bets()
        await interaction.followup.send("✅ Fetched and posted a batch of bets.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fetch failed: {e}", ephemeral=True)

@bot.tree.command(name="stats", description="Your personal paper-trading stats (and totals).")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        conn = get_db_conn()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        uid = str(interaction.user.id)

        # per-user, settled only
        cur.execute("""
          SELECT stake_type,
                 COUNT(*) AS bets,
                 SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(stake_units),0) AS staked,
                 COALESCE(SUM(payout_units),0) AS returned
          FROM user_bets
          WHERE user_id=%s AND result IN ('win','loss')
          GROUP BY stake_type
          ORDER BY stake_type
        """, (uid,))
        rows = cur.fetchall()

        cur.execute("""
          SELECT COUNT(*) AS bets,
                 SUM(CASE WHEN result='win' THEN 1 ELSE 0 END) AS wins,
                 SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END) AS losses,
                 COALESCE(SUM(stake_units),0) AS staked,
                 COALESCE(SUM(payout_units),0) AS returned
          FROM user_bets
          WHERE user_id=%s AND result IN ('win','loss')
        """, (uid,))
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
        lines.append(f"\n**Your Total** → {tb} bets | win-rate **{twr:.1f}%** | P&L **{tpnl:.2f} units** | ROI **{troi:.2f}%**")

        await interaction.followup.send("\n".join(lines) if lines else "No settled bets yet.", ephemeral=True)
        cur.close(); conn.close()
    except Exception as e:
        await interaction.followup.send(f"⚠️ Could not compute stats: `{e}`", ephemeral=True)

@bot.tree.command(name="roi", description="System-wide ROI from settled paper trades.")
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

        await interaction.followup.send("\n".join(lines) if lines else "No settled bets yet.", ephemeral=True)
        cur.close(); conn.close()
    except Exception as e:
        await interaction.followup.send(f"⚠️ Could not compute ROI: `{e}`", ephemeral=True)

# ========= READY =========
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

# ========= RUN =========
if not TOKEN:
    raise SystemExit("❌ Missing DISCORD_BOT_TOKEN env var")
bot.run(TOKEN)






