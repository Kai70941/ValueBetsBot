import os
import asyncio
import hashlib
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button

import psycopg2
import psycopg2.extras

import aiohttp

# =========================
# ENV & CONSTANTS
# =========================

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BEST_CH = int(os.getenv("DISCORD_CHANNEL_ID_BEST", "0"))
QUICK_CH = int(os.getenv("DISCORD_CHANNEL_ID_QUICK", "0"))
LONG_CH  = int(os.getenv("DISCORD_CHANNEL_ID_LONG", "0"))

# Optional mirror for value-bets
VALUE_CH = int(os.getenv("DISCORD_CHANNEL_ID_VALUE", "0"))

ODDS_API_KEY = os.getenv("ODDS_API_KEY")

DB_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PUBLIC_URL")

# Basic bankroll/stake model (in UNITS, not $) – keep identical to your previous logic
CONSERVATIVE_UNITS = 15.0
SMART_UNITS        = 5.0
AGGRESSIVE_UNITS   = 20.0

ALLOWED_BOOKMAKER_KEYS = [
    "sportsbet", "bet365", "ladbrokes", "tabtouch", "neds",
    "pointsbet", "dabble", "betfair", "tab"
]

INTENTS = discord.Intents.default()
INTENTS.message_content = True

bot = commands.Bot(command_prefix="!", intents=INTENTS)


# =========================
# DB HELPERS & INIT
# =========================

def get_db_conn():
    if not DB_URL:
        raise RuntimeError("No DATABASE_URL / DATABASE_PUBLIC_URL provided")
    # auto-ssl: require
    return psycopg2.connect(DB_URL, sslmode="require", cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        # bets table – one row per detected bet
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bets (
            id          BIGSERIAL PRIMARY KEY,
            bet_key     TEXT UNIQUE,
            event_id    TEXT,
            match       TEXT NOT NULL,
            bookmaker   TEXT NOT NULL,
            team        TEXT NOT NULL,
            odds        NUMERIC NOT NULL,
            consensus   NUMERIC,
            implied     NUMERIC,
            edge        NUMERIC,
            bet_time    TIMESTAMPTZ NOT NULL,
            category    TEXT NOT NULL, -- best / quick / long / value (mirror)
            sport       TEXT,
            league      TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        # user_bets – which user placed which stakes on which bet
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_bets (
            id         BIGSERIAL PRIMARY KEY,
            user_id    TEXT NOT NULL,
            username   TEXT NOT NULL,
            bet_key    TEXT NOT NULL,
            event_id   TEXT,
            sport      TEXT,
            strategy   TEXT NOT NULL,   -- conservative / smart / aggressive
            units      NUMERIC NOT NULL,
            placed_at  TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        # button_map – custom_id -> bet_key strategy
        cur.execute("""
        CREATE TABLE IF NOT EXISTS button_map (
            button_id  TEXT PRIMARY KEY,
            bet_key    TEXT NOT NULL,
            strategy   TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS button_map_created_at_idx ON button_map (created_at);")
    conn.close()

def save_bet_to_db(bet: dict):
    """Upsert a detected bet row."""
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO bets (bet_key, event_id, match, bookmaker, team, odds,
                          consensus, implied, edge, bet_time, category, sport, league)
        VALUES (%(bet_key)s, %(event_id)s, %(match)s, %(bookmaker)s, %(team)s, %(odds)s,
                %(consensus)s, %(implied)s, %(edge)s, %(bet_time)s, %(category)s, %(sport)s, %(league)s)
        ON CONFLICT (bet_key) DO NOTHING;
        """, bet)
    conn.commit()
    conn.close()

def save_user_bet(*, user_id: int, username: str, bet_key: str, event_id: str, sport: str, strategy: str, units: float):
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO user_bets (user_id, username, bet_key, event_id, sport, strategy, units)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (str(user_id), username, bet_key, event_id, sport, strategy, units))
    conn.commit()
    conn.close()

def short_id_from_bet_key(bet_key: str) -> str:
    return hashlib.sha1(bet_key.encode()).hexdigest()[:12]

def save_button_mapping(button_id: str, bet_key: str, strategy: str):
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        cur.execute("""
        INSERT INTO button_map (button_id, bet_key, strategy)
        VALUES (%s, %s, %s)
        ON CONFLICT (button_id) DO NOTHING
        """, (button_id, bet_key, strategy))
    conn.commit()
    conn.close()

def load_recent_buttons(hours: int = 72):
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        cur.execute("""
        SELECT button_id, bet_key, strategy
        FROM button_map
        WHERE created_at > NOW() - INTERVAL %s
        """, (f"{hours} hours",))
        rows = cur.fetchall()
    conn.close()
    return rows


# =========================
# FETCH ODDS (simplified)
# =========================

def _allowed_bookmaker(title: str) -> bool:
    return any(key in (title or "").lower() for key in ALLOWED_BOOKMAKER_KEYS)

async def fetch_odds():
    url = "https://api.the-odds-api.com/v4/sports/upcoming/odds/"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "au,us,uk",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "decimal"
    }
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, params=params, timeout=15) as resp:
                resp.raise_for_status()
                return await resp.json()
    except Exception as e:
        print("❌ Odds API error:", e)
        return []

def detect_sport_and_league(event: dict) -> tuple[str, str]:
    # Try to infer (depends on feed). Keep graceful fallback:
    sport = (event.get("sport_title") or event.get("sport_key") or "Soccer").title()
    league = event.get("league") or event.get("sport_title") or "Unknown League"
    # Remap Football→Soccer if needed
    if sport.lower() == "football" and "soccer" in (event.get("sport_key") or "").lower():
        sport = "Soccer"
    return sport, league

def calc_bets_from_odds(data):
    now = datetime.now(timezone.utc)
    bets = []
    for ev in data:
        home, away = ev.get("home_team"), ev.get("away_team")
        if not home or not away:
            continue
        match_name = f"{home} vs {away}"
        commence = ev.get("commence_time")
        try:
            dt = datetime.fromisoformat(str(commence).replace("Z", "+00:00"))
        except Exception:
            continue
        delta = dt - now
        if delta.total_seconds() <= 0 or delta > timedelta(days=150):
            continue

        # compute market consensus & pick
        consensus_by_outcome = defaultdict(list)
        for book in ev.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title", "")):
                continue
            for m in book.get("markets", []):
                for out in m.get("outcomes", []):
                    price = out.get("price")
                    name  = out.get("name")
                    if price and name:
                        key = f"{m['key']}:{name}"
                        consensus_by_outcome[key].append(1/float(price))

        if not consensus_by_outcome:
            continue
        # global consensus average of all probabilities
        n = sum(len(v) for v in consensus_by_outcome.values())
        global_consensus = sum(sum(v) for v in consensus_by_outcome.values()) / max(n, 1)

        # Pick "best" offering (largest edge) from allowed books:
        best_candidate = None
        for book in ev.get("bookmakers", []):
            if not _allowed_bookmaker(book.get("title","")):
                continue
            for m in book.get("markets", []):
                for out in m.get("outcomes", []):
                    price = out.get("price"); name = out.get("name")
                    if not price or not name:
                        continue
                    implied_p = 1/float(price)
                    key = f"{m['key']}:{name}"
                    consensus_p = (
                        sum(consensus_by_outcome[key]) / len(consensus_by_outcome[key])
                        if key in consensus_by_outcome else global_consensus
                    )
                    edge = consensus_p - implied_p
                    # choose largest edge
                    if (best_candidate is None) or (edge > best_candidate["edge"]):
                        best_candidate = {
                            "match": match_name,
                            "bookmaker": book.get("title","Unknown"),
                            "team": name,
                            "odds": float(price),
                            "consensus": round(consensus_p*100, 2),
                            "implied": round(implied_p*100, 2),
                            "edge": edge,
                            "bet_time": dt
                        }
        if not best_candidate:
            continue

        sport, league = detect_sport_and_league(ev)

        # categorize
        category = "quick" if delta <= timedelta(hours=48) else "long"

        # best vs value: You can keep your own rule; here best=top edge in batch; value if edge>=2% (example)
        # We'll tag "best" later when we compute per-batch max.

        bet_key = f"{match_name}|{best_candidate['team']}|{best_candidate['bookmaker']}|{dt.isoformat()}"
        bets.append({
            "bet_key": bet_key,
            "event_id": str(ev.get("id") or ""),
            "match": match_name,
            "bookmaker": best_candidate["bookmaker"],
            "team": best_candidate["team"],
            "odds": best_candidate["odds"],
            "consensus": best_candidate["consensus"],
            "implied": best_candidate["implied"],
            "edge": round(best_candidate["edge"]*100, 2),  # store as %
            "bet_time": dt,
            "category": category,
            "sport": sport,
            "league": league
        })

    # label the single highest edge as "best" in feed (optional)
    if bets:
        top = max(bets, key=lambda b: b["edge"])
        top["category"] = "best"
    return bets


# =========================
# EMBEDS & BUTTONS (PERSISTENT)
# =========================

def units_for_strategy(strategy: str) -> float:
    if strategy == "conservative":
        return CONSERVATIVE_UNITS
    if strategy == "smart":
        return SMART_UNITS
    if strategy == "aggressive":
        return AGGRESSIVE_UNITS
    return 1.0

def value_indicator(edge_pct: float) -> str:
    return "🟢 Value Bet" if edge_pct >= 2.0 else "🔴 Low Value"

def format_bet_embed(b: dict, title: str, color: int) -> discord.Embed:
    indicator = value_indicator(b["edge"])
    sport_line = f"{b.get('sport','Sport')} ({b.get('league','Unknown League')})"
    desc = (
        f"{indicator}\n\n"
        f"**{sport_line}**\n\n"
        f"**Match:** {b['match']}\n"
        f"**Pick:** {b['team']} @ {b['odds']}\n"
        f"**Bookmaker:** {b['bookmaker']}\n"
        f"**Consensus %:** {b['consensus']}%\n"
        f"**Implied %:** {b['implied']}%\n"
        f"**Edge:** {b['edge']}%\n"
        f"**Time:** {b['bet_time'].strftime('%d/%m/%y %H:%M')}\n\n"
        f"💵 **Conservative Stake:** {CONSERVATIVE_UNITS}u\n"
        f"🧠 **Smart Stake:** {SMART_UNITS}u\n"
        f"🔥 **Aggressive Stake:** {AGGRESSIVE_UNITS}u\n"
    )
    return discord.Embed(title=title, description=desc, color=color)

class BetActionView(View):
    def __init__(self, bet_key: str):
        super().__init__(timeout=None)  # persistent
        self.bet_key = bet_key

        buttons = [
            ("Conservative", "💵", discord.ButtonStyle.secondary, "conservative"),
            ("Smart",        "🧠", discord.ButtonStyle.primary,   "smart"),
            ("Aggressive",   "🔥", discord.ButtonStyle.danger,    "aggressive"),
        ]
        for label, emoji, style, strategy in buttons:
            cid = f"place|{strategy}|{short_id_from_bet_key(bet_key)}"
            self.add_item(Button(label=label, emoji=emoji, style=style, custom_id=cid))

async def handle_place_button(interaction: discord.Interaction):
    # Defer immediately to avoid "This interaction failed"
    await interaction.response.defer(ephemeral=True, thinking=False)
    try:
        custom_id = interaction.data.get("custom_id", "")
        # place|strategy|shortid
        parts = custom_id.split("|")
        if len(parts) != 3:
            await interaction.followup.send("Sorry, I couldn't parse that button.", ephemeral=True)
            return
        _, strategy, shortid = parts

        # Resolve to bet_key
        conn = get_db_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT bet_key FROM button_map WHERE button_id=%s", (custom_id,))
            row = cur.fetchone()
        conn.close()

        if not row:
            await interaction.followup.send(
                "That button is no longer active (maybe from an older message). Try a newer card.",
                ephemeral=True
            )
            return

        bet_key = row["bet_key"]

        # We might want sport/event_id to store with user bet (look them up from bets table)
        event_id = ""; sport = ""
        conn = get_db_conn()
        with conn, conn.cursor() as cur:
            cur.execute("SELECT event_id, sport FROM bets WHERE bet_key=%s LIMIT 1", (bet_key,))
            r2 = cur.fetchone()
        conn.close()
        if r2:
            event_id = r2.get("event_id") or ""
            sport    = r2.get("sport") or ""

        save_user_bet(
            user_id=interaction.user.id,
            username=str(interaction.user),
            bet_key=bet_key,
            event_id=event_id,
            sport=sport,
            strategy=strategy,
            units=units_for_strategy(strategy)
        )

        await interaction.followup.send(f"✅ Saved your **{strategy.title()}** placement for this bet.", ephemeral=True)

    except Exception as e:
        print("Button handler error:", repr(e))
        try:
            await interaction.followup.send("❌ Could not save your bet. Is the database configured?", ephemeral=True)
        except:
            pass


def send_bet_card(channel: discord.abc.Messageable, embed: discord.Embed, bet: dict):
    """Attach persistent view and record button mappings."""
    view = BetActionView(bet["bet_key"])
    # save custom_id -> bet_key for each button
    for item in view.children:
        if isinstance(item, Button) and item.custom_id and item.custom_id.startswith("place|"):
            strategy = item.custom_id.split("|")[1]
            save_button_mapping(item.custom_id, bet["bet_key"], strategy)
    return channel.send(embed=embed, view=view)


# =========================
# POSTING LOGIC
# =========================

async def post_bets(bets: list[dict]):
    if not bets:
        return

    # Save to DB and post to channels
    for b in bets:
        # Save bet
        save_bet_to_db(b)

        # Title/color
        if b["category"] == "best":
            title = "⭐ Best Bet"
            color = 0xFFD700
            channel_id = BEST_CH
        elif b["category"] == "quick":
            title = "⏱ Quick Return Bet"
            color = 0x2ECC71
            channel_id = QUICK_CH
        else:
            title = "📅 Longer Play Bet"
            color = 0x3498DB
            channel_id = LONG_CH

        embed = format_bet_embed(b, title, color)

        channel = bot.get_channel(channel_id) if channel_id else None
        if channel:
            await send_bet_card(channel, embed, b)

        # Mirror value bets into VALUE_CH too (rule: edge >= 2%)
        if b["edge"] >= 2.0 and VALUE_CH:
            vch = bot.get_channel(VALUE_CH)
            if vch:
                ve = format_bet_embed(b, "💚 Value Bet (Testing)", 0x00CC66)
                await send_bet_card(vch, ve, b)


# =========================
# BOT EVENTS & SLASH CMDS
# =========================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await bot.tree.sync()
        print("✅ Slash commands synced.")
    except Exception as e:
        print("❌ Slash sync failed:", e)

    # Reattach persistent views for recent buttons
    try:
        rows = load_recent_buttons(72)
        seen = set()
        for button_id, bet_key, strategy in rows:
            if bet_key not in seen:
                bot.add_view(BetActionView(bet_key))
                seen.add(bet_key)
        print(f"🔁 Reattached {len(seen)} persistent bet views.")
    except Exception as e:
        print("❌ Could not reattach views:", repr(e))

    # Start loop if not running
    if not bet_loop.is_running():
        bet_loop.start()

@bot.event
async def on_interaction(interaction: discord.Interaction):
    try:
        data = interaction.data or {}
        ctype = data.get("component_type")
        if ctype == 2:  # button
            custom_id = data.get("custom_id", "")
            if custom_id.startswith("place|"):
                await handle_place_button(interaction)
                return
    except Exception as e:
        print("on_interaction error:", repr(e))
    # let other interactions pass to command router

@bot.tree.command(name="ping", description="Check if the bot is alive.")
async def ping_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!", ephemeral=True)

@bot.tree.command(name="fetchbets", description="Force-fetch odds once (debug).")
async def fetch_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)
    data = await fetch_odds()
    bets = calc_bets_from_odds(data)
    await post_bets(bets)
    await interaction.followup.send(f"Fetched and posted {len(bets)} bets.", ephemeral=True)

@bot.tree.command(name="roi", description="Compute ROI (all strategies).")
async def roi_cmd(interaction: discord.Interaction):
    # Simple placeholder aggregation
    await interaction.response.defer(ephemeral=True, thinking=False)
    conn = get_db_conn()
    total_units = 0.0
    wins = 0
    total = 0
    # This is still a placeholder because actual grading requires result data
    with conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM user_bets")
        total = cur.fetchone()["n"]
        cur.execute("SELECT COALESCE(SUM(units),0) AS u FROM user_bets")
        total_units = float(cur.fetchone()["u"])
    conn.close()
    await interaction.followup.send(
        f"📈 ROI (placeholder): {total} bets; {total_units:.2f}u staked. (Grading requires results feed.)",
        ephemeral=True
    )

@bot.tree.command(name="stats", description="Your placements summary.")
async def stats_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=False)
    conn = get_db_conn()
    with conn, conn.cursor() as cur:
        cur.execute("""
        SELECT strategy, COUNT(*) AS n, COALESCE(SUM(units),0) AS units
        FROM user_bets
        WHERE user_id=%s
        GROUP BY strategy
        """, (str(interaction.user.id),))
        rows = cur.fetchall()
    conn.close()
    if not rows:
        await interaction.followup.send("No placements yet.", ephemeral=True)
        return
    lines = [f"- **{r['strategy']}**: {r['n']} bets | {float(r['units']):.2f}u"]
    await interaction.followup.send("🧾 Your stats:\n" + "\n".join(lines), ephemeral=True)


# =========================
# BACKGROUND LOOP
# =========================

@tasks.loop(minutes=10)
async def bet_loop():
    data = await fetch_odds()
    bets = calc_bets_from_odds(data)
    await post_bets(bets)


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    init_db()
    if not TOKEN:
        raise SystemExit("❌ Missing DISCORD_BOT_TOKEN")
    bot.run(TOKEN)















