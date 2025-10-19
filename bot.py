# ... (imports stay the same)
import os
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks
import requests
import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("valuebets")

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

CH_BEST  = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0") or "0")
CH_QUICK = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0") or "0")
CH_LONG  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0") or "0")
CH_VALUE = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0") or "0")

CONSERVATIVE_UNITS = float(os.getenv("CONSERVATIVE_UNITS", "15.0"))

DEFAULT_BOOKS = [
    "sportsbet","bet365","ladbrokes","tabtouch","neds",
    "pointsbet","dabble","betfair","tab"
]
ALLOWED_BOOKMAKER_KEYS = [
    s.strip().lower() for s in os.getenv("ALLOWED_BOOKMAKERS", ",".join(DEFAULT_BOOKS)).split(",") if s.strip()
]

VALUE_EDGE_THRESHOLD = 2.0

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

posted_keys = set()

# ---------------------------
# DB URL fallback (IMPORTANT)
# ---------------------------
DATABASE_URL = (os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or "").strip()
if not DATABASE_URL:
    log.warning("No database URL set (DATABASE_PUBLIC_URL or DATABASE_URL). DB writes will be skipped.")

DB_OK = bool(DATABASE_URL)

def _connect():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require",
        cursor_factory=psycopg2.extras.RealDictCursor
    )

def _migrate():
    if not DB_OK: 
        return
    conn = _connect()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bets (
        id SERIAL PRIMARY KEY,
        bet_key TEXT UNIQUE,
        match TEXT,
        bookmaker TEXT,
        team TEXT,
        odds DOUBLE PRECISION,
        edge DOUBLE PRECISION,
        bet_time TIMESTAMPTZ,
        category TEXT,
        sport TEXT,
        league TEXT,
        consensus DOUBLE PRECISION,
        implied DOUBLE PRECISION,
        created_at TIMESTAMPTZ DEFAULT NOW()
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
        league TEXT,
        strategy TEXT,
        units DOUBLE PRECISION,
        odds DOUBLE PRECISION,
        exp_profit DOUBLE PRECISION,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bets_bet_key ON bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_bet_key ON user_bets(bet_key);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id);")
    conn.commit()
    cur.close()
    conn.close()
    log.info("DB migration complete")

def save_bet_row(bet: dict):
    if not DB_OK:
        return False
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bets
              (bet_key, match, bookmaker, team, odds, edge, bet_time, category, sport, league, consensus, implied)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (bet_key) DO NOTHING;
        """, (
            bet.get("bet_key"),
            bet.get("match"),
            bet.get("bookmaker"),
            bet.get("team"),
            float(bet.get("odds") or 0),
            float(bet.get("edge") or 0),
            bet.get("bet_time"),
            bet.get("category"),
            bet.get("sport"),
            bet.get("league"),
            float(bet.get("consensus") or 0),
            float(bet.get("implied") or 0),
        ))
        conn.commit()
        cur.close()
        conn.close()
        log.info("Saved bet: %s", bet.get("bet_key"))
        return True
    except Exception:
        log.exception("Failed to save bet row")
        return False

def save_user_bet(user, bet: dict, strategy: str, units: float, odds: float, exp_profit: float):
    if not DB_OK:
        return False
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_bets
               (user_id, username, bet_key, event_id, sport, league, strategy, units, odds, exp_profit)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);
        """, (
            str(user.id), str(user),
            bet.get("bet_key"), bet.get("event_id") or None,
            bet.get("sport"), bet.get("league"),
            strategy, float(units or 0), float(odds or 0), float(exp_profit or 0)
        ))
        conn.commit()
        cur.close()
        conn.close()
        return True
    except Exception:
        log.exception("Failed to save user bet")
        return False

def fetch_bet_row(bet_key: str):
    if not DB_OK:
        return None
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM bets WHERE bet_key=%s LIMIT 1;", (bet_key,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return row
    except Exception:
        log.exception("fetch_bet_row failed")
        return None

# Emojis etc. (unchanged)
SPORT_EMOJI = {
    "soccer":"⚽","americanfootball":"🏈","basketball":"🏀","baseball":"⚾","icehockey":"🏒",
    "tennis":"🎾","cricket":"🏏","mma":"🥊","boxing":"🥊","aussierules":"🏉",
    "rugbyleague":"🏉","rugbyunion":"🏉","golf":"⛳","esports":"🎮",
}
def sport_label_and_emoji(sport_key: str, league: str | None) -> str:
    key = (sport_key or "").lower()
    emoji = SPORT_EMOJI.get(key, "🎲")
    if key == "soccer": sport_name = "Soccer"
    elif key == "americanfootball": sport_name = "American Football"
    else: sport_name = key.capitalize() if key else "Sport"
    return f"{emoji} {sport_name} ({league or 'Unknown League'})"

def _allowed_bookmaker(title: str) -> bool:
    t = (title or "").lower()
    return any(k in t for k in ALLOWED_BOOKMAKER_KEYS)

# ---- Strong bet_key with fallback
def bet_key_from_components(event_id: str | None, teams: str, commence: datetime,
                            book: str, market_key: str, outcome_name: str) -> str:
    if event_id:
        base = event_id
    else:
        # Fallback: teams + timestamp – guarantees uniqueness per event
        base = f"{teams}|{commence.isoformat()}"
    return f"{base}|{book}|{market_key}|{outcome_name}".lower()

def compute_units_and_profit(edge: float, odds: float):
    cons_units = CONSERVATIVE_UNITS
    kelly_frac = max(0.0, min((edge or 0) / 100.0, 0.10))
    smart_units = round(cons_units * (1.0 + 2.0 * kelly_frac), 2)
    aggr_units  = round(cons_units * (1.0 + 5.0 * kelly_frac), 2)
    return cons_units, smart_units, aggr_units

def exp_profit(units: float, odds: float, consensus_pct: float):
    p = max(0.0, min(1.0, (consensus_pct or 0) / 100.0))
    return round(p * (units * (odds - 1.0)) - (1 - p) * units, 2)

API_BASE = "https://api.the-odds-api.com/v4"

def fetch_upcoming_odds():
    url = f"{API_BASE}/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error("Odds API error: %s", e)
        return []

def calculate_bets(raw):
    now = datetime.now(timezone.utc)
    bets = []
    for ev in raw:
        home, away = ev.get("home_team"), ev.get("away_team")
        teams = f"{home} vs {away}" if home and away else ev.get("sport_title","Unknown matchup")

        try:
            commence = datetime.fromisoformat(ev.get("commence_time").replace("Z","+00:00"))
        except Exception:
            continue
        if commence <= now or commence - now > timedelta(days=150):
            continue

        sport_key = (ev.get("sport_key") or "").lower()
        league = ev.get("sport_title") or None
        event_id = ev.get("id") or ev.get("event_id")

        per_outcome = defaultdict(list)
        for book in ev.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title","")):
                continue
            for m in book.get("markets", []):
                mkey = m.get("key")
                for out in m.get("outcomes", []):
                    price, name = out.get("price"), out.get("name")
                    if price and name:
                        per_outcome[f"{mkey}:{name}"].append(1.0/price)

        if not per_outcome:
            continue
        all_inv = [p for lst in per_outcome.values() for p in lst]
        global_cons = sum(all_inv) / max(1, len(all_inv))

        for book in ev.get("bookmakers", []):
            btitle = book.get("title","Unknown")
            if not _allowed_bookmaker(btitle):
                continue
            for m in book.get("markets", []):
                mkey = m.get("key")
                for out in m.get("outcomes", []):
                    price, name = out.get("price"), out.get("name")
                    if not price or not name:
                        continue
                    implied = 100.0 * (1.0/price)
                    oc_key = f"{mkey}:{name}"
                    if oc_key in per_outcome and per_outcome[oc_key]:
                        cons = 100.0 * (sum(per_outcome[oc_key]) / len(per_outcome[oc_key]))
                    else:
                        cons = 100.0 * global_cons
                    edge = cons - implied
                    is_quick = (commence - now) <= timedelta(hours=48)
                    category = "quick" if is_quick else "long"

                    cons_units, smart_units, aggr_units = compute_units_and_profit(edge, price)
                    p = cons / 100.0
                    cons_exp = round(p * (cons_units * (price - 1.0)) - (1 - p) * cons_units, 2)
                    smart_exp = round(p * (smart_units * (price - 1.0)) - (1 - p) * smart_units, 2)
                    aggr_exp  = round(p * (aggr_units  * (price - 1.0)) - (1 - p) * aggr_units, 2)

                    bet_key = bet_key_from_components(event_id, teams, commence, btitle, mkey, name)

                    bet = {
                        "event_id": event_id,
                        "bet_key": bet_key,
                        "match": teams,
                        "bookmaker": btitle,
                        "team": f"{name} @ {price}",
                        "odds": float(price),
                        "edge": round(edge, 2),
                        "bet_time": commence,
                        "category": category,
                        "sport": sport_key,
                        "league": league,
                        "consensus": round(cons, 2),
                        "implied": round(implied, 2),

                        "cons_units": round(cons_units, 2),
                        "smart_units": round(smart_units, 2),
                        "aggr_units": round(aggr_units, 2),
                        "cons_exp": cons_exp,
                        "smart_exp": smart_exp,
                        "aggr_exp": aggr_exp,
                    }
                    bets.append(bet)
    return bets

def build_bet_view(bet: dict) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Conservative", emoji="💵", style=discord.ButtonStyle.secondary,
                                    custom_id=f"place|{bet['bet_key']}|conservative"))
    view.add_item(discord.ui.Button(label="Smart", emoji="🧠", style=discord.ButtonStyle.primary,
                                    custom_id=f"place|{bet['bet_key']}|smart"))
    view.add_item(discord.ui.Button(label="Aggressive", emoji="🔥", style=discord.ButtonStyle.danger,
                                    custom_id=f"place|{bet['bet_key']}|aggressive"))
    return view

def embed_for_bet(title: str, bet: dict, color: int):
    indicator = "🟢 Value Bet" if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD else "🔴 Low Value"
    sport_line = sport_label_and_emoji(bet.get("sport"), bet.get("league"))
    desc = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {bet['match']}\n"
        f"**Pick:** {bet['team']}\n"
        f"**Bookmaker:** {bet['bookmaker']}\n"
        f"**Consensus %:** {bet['consensus']}%\n"
        f"**Implied %:** {bet['implied']}%\n"
        f"**Edge:** {bet['edge']}%\n"
        f"**Time:** {bet['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
        f"🪙 **Conservative Stake:** {bet['cons_units']} units → Payout: {round(bet['cons_units'] * bet['odds'], 2)} | Exp. Profit: {bet['cons_exp']}\n"
        f"🧠 **Smart Stake:** {bet['smart_units']} units → Payout: {round(bet['smart_units'] * bet['odds'], 2)} | Exp. Profit: {bet['smart_exp']}\n"
        f"🔥 **Aggressive Stake:** {bet['aggr_units']} units → Payout: {round(bet['aggr_units'] * bet['odds'], 2)} | Exp. Profit: {bet['aggr_exp']}\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

async def post_bet_to_channels(bet: dict):
    # Save first (so buttons can fetch from DB even after restart)
    save_bet_row(bet)

    title = "Quick Return Bet" if bet["category"] == "quick" else "Longer Play Bet"
    color = 0x2ecc71 if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD else 0xe74c3c
    if bet.get("_is_best"):
        title = "Best Bet"
        color = 0xf1c40f

    emb  = embed_for_bet(title, bet, color)
    view = build_bet_view(bet)

    ch_id = CH_QUICK if bet["category"] == "quick" else CH_LONG
    if bet.get("_is_best"):
        ch_id = CH_BEST
    channel = bot.get_channel(ch_id) if ch_id else None
    if channel:
        try:
            await channel.send(embed=emb, view=view)
        except Exception:
            log.exception("Failed to send embed")

    if bet.get("edge", 0) >= VALUE_EDGE_THRESHOLD and CH_VALUE:
        vch = bot.get_channel(CH_VALUE)
        if vch:
            try:
                v_emb = embed_for_bet("Value Bet (Testing)", bet, 0x2ecc71)
                await vch.send(embed=v_emb, view=view)
            except Exception:
                log.exception("Failed duplicate to value channel")

async def post_pack(bets: list[dict]):
    if not bets:
        return
    cands = [b for b in bets if b.get("edge", 0) >= VALUE_EDGE_THRESHOLD and b.get("consensus", 0) >= 50]
    if cands:
        best = max(cands, key=lambda b: (b.get("edge", 0), b.get("consensus", 0)))
        if best["bet_key"] not in posted_keys:
            best["_is_best"] = True
            posted_keys.add(best["bet_key"])
            await post_bet_to_channels(best)
    for b in bets:
        if b.get("_is_best"): 
            continue
        if b["bet_key"] in posted_keys:
            continue
        posted_keys.add(b["bet_key"])
        await post_bet_to_channels(b)

@bot.listen("on_interaction")
async def handle_place_buttons(inter: discord.Interaction):
    try:
        if inter.type != discord.InteractionType.component:
            return
        cid = inter.data.get("custom_id", "")
        if not cid.startswith("place|"):
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        try:
            _, bet_key, strategy = cid.split("|", 2)
        except ValueError:
            await inter.followup.send("Invalid button payload.", ephemeral=True)
            return
        row = fetch_bet_row(bet_key)
        if not row:
            await inter.followup.send("Bet not found in DB (try again in a moment).", ephemeral=True)
            return
        edge = float(row.get("edge") or 0.0)
        odds = float(row.get("odds") or 0.0)
        consensus = float(row.get("consensus") or 50.0)
        cons_units, smart_units, aggr_units = compute_units_and_profit(edge, odds)
        if strategy == "conservative":
            units = cons_units
        elif strategy == "smart":
            units = smart_units
        else:
            strategy = "aggressive"
            units = aggr_units
        ep = exp_profit(units, odds, consensus)
        bet_payload = {"bet_key": row["bet_key"], "event_id": None, "sport": row.get("sport"), "league": row.get("league")}
        ok = save_user_bet(inter.user, bet_payload, strategy, units, odds, ep)
        if ok:
            await inter.followup.send(
                f"Saved **{strategy.capitalize()}** bet for **{row['match']}** — {row['team']} | {units:.2f} units",
                ephemeral=True
            )
        else:
            await inter.followup.send("❌ Could not save your bet. Is the database configured?", ephemeral=True)
    except Exception:
        log.exception("handle_place_buttons error")

@tasks.loop(minutes=2)
async def bet_loop():
    raw = fetch_upcoming_odds()
    bets = calculate_bets(raw)
    await post_pack(bets)

@bot.tree.command(name="ping", description="Ping")
async def ping(ctx: discord.Interaction):
    await ctx.response.send_message("Pong 🏓", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Fetch & post now")
async def fetchbets(ctx: discord.Interaction):
    await ctx.response.defer(ephemeral=True)
    raw = fetch_upcoming_odds()
    bets = calculate_bets(raw)
    await post_pack(bets)
    await ctx.followup.send(f"Fetched {len(bets)} candidate bets.", ephemeral=True)

@bot.tree.command(name="dbcheck", description="Counts in DB")
async def dbcheck(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM bets;")
        n_bets = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM user_bets;")
        n_ub  = cur.fetchone()["n"]
        cur.close()
        conn.close()
        await ctx.response.send_message(f"bets: {n_bets} | user_bets: {n_ub}", ephemeral=True)
    except Exception:
        log.exception("dbcheck failed")
        await ctx.response.send_message("dbcheck failed.", ephemeral=True)

@bot.tree.command(name="dblatest", description="Show 5 latest bets")
async def dblatest(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT match, team, bookmaker, category, created_at
            FROM bets ORDER BY created_at DESC LIMIT 5;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        if not rows:
            await ctx.response.send_message("No rows found.", ephemeral=True)
            return
        msg = "\n".join([f"• {r['match']} | {r['team']} | {r['bookmaker']} | {r['category']} | {r['created_at']:%Y-%m-%d %H:%M}" for r in rows])
        await ctx.response.send_message(msg, ephemeral=True)
    except Exception:
        log.exception("dblatest failed")
        await ctx.response.send_message("dblatest failed.", ephemeral=True)

@bot.tree.command(name="stats", description="Paper-trade stats")
async def stats(ctx: discord.Interaction):
    if not DB_OK:
        await ctx.response.send_message("DB not configured.", ephemeral=True)
        return
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT
              ub.strategy,
              COUNT(*) AS n,
              COALESCE(SUM(ub.units), 0) AS units,
              COALESCE(SUM(ub.exp_profit), 0) AS exp_p,
              AVG(b.consensus) AS avg_consensus
            FROM user_bets ub
            LEFT JOIN bets b ON b.bet_key = ub.bet_key
            GROUP BY ub.strategy
            ORDER BY ub.strategy;
        """)
        per = cur.fetchall()
        cur.execute("""
            SELECT
              COUNT(*) AS n,
              COALESCE(SUM(ub.units), 0) AS units,
              COALESCE(SUM(ub.exp_profit), 0) AS exp_p,
              AVG(b.consensus) AS avg_consensus
            FROM user_bets ub
            LEFT JOIN bets b ON b.bet_key = ub.bet_key;
        """)
        total = cur.fetchone()
        cur.close()
        conn.close()

        if not total or int(total["n"] or 0) == 0:
            await ctx.response.send_message("No saved bets yet.", ephemeral=True)
            return

        lines = []
        for r in per:
            n  = int(r["n"] or 0); u = float(r["units"] or 0); ep = float(r["exp_p"] or 0)
            wr = float(r["avg_consensus"] or 0.0)
            roi = (ep / u * 100.0) if u > 0 else 0.0
            lines.append(f"• **{r['strategy']}** → **{n} bets** | **{u:.2f} units** | **Win rate {wr:.2f}%** | **P&L {ep:.2f}** | **ROI {roi:.2f}%**")

        n  = int(total["n"] or 0); u = float(total["units"] or 0); ep = float(total["exp_p"] or 0)
        wr = float(total["avg_consensus"] or 0.0); roi = (ep / u * 100.0) if u > 0 else 0.0
        lines.append(f"\n**Total** → **{n} bets** | **{u:.2f} units** | **Win rate {wr:.2f}%** | **P&L {ep:.2f}** | **ROI {roi:.2f}%**")

        await ctx.response.send_message("\n".join(lines), ephemeral=True)
    except Exception:
        log.exception("/stats failed")
        await ctx.response.send_message("Stats failed.", ephemeral=True)

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    try:
        _migrate()
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash sync/migrate failed")
    if not bet_loop.is_running():
        bet_loop.start()

if not TOKEN:
    raise SystemExit("Missing DISCORD_BOT_TOKEN")
if not ODDS_API_KEY:
    log.warning("No ODDS_API_KEY set.")
bot.run(TOKEN)





