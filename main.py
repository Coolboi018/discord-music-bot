# bot.py
import os
import re
import asyncio
import logging
from threading import Thread
from collections import deque

import requests
from flask import Flask

import discord
from discord.ext import commands

import yt_dlp

# Optional Spotify
try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except Exception:
    spotipy = None

# -------------------- Logging --------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("musicbot")

# -------------------- Flask keep-alive --------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "🎵 Music Bot (Render) — Alive"

@app.route("/health")
def health():
    return {"status": "healthy", "bot": "online"}

def run_flask():
    port = int(os.getenv("PORT", 8080))
    log.info(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port)

def start_keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()

# -------------------- Discord bot setup --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, case_insensitive=True)
bot.remove_command("help")

# -------------------- global queues/state --------------------
music_queues = {}   # guild_id => deque of Player objects
now_playing = {}    # guild_id => Player
loop_mode = {}      # guild_id => 'off'|'track'|'queue'

# -------------------- yt-dlp config --------------------
COOKIEFILE_ENV = os.getenv("COOKIEFILE", "")  # optional path to cookies file

def get_ytdl_options():
    """Returns yt-dlp options with cookies if available"""
    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch",
        "source_address": "0.0.0.0",
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "no_check_certificate": True,
        "extract_flat": False,
        "noplaylist": False,
        "nocheckcertificate": True,
        "age_limit": None,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-us,en;q=0.5",
            "Sec-Fetch-Mode": "navigate"
        }
    }
    
    # Add cookies if file exists
    if COOKIEFILE_ENV and os.path.exists(COOKIEFILE_ENV):
        opts["cookiefile"] = COOKIEFILE_ENV
        log.info(f"Using cookies file: {COOKIEFILE_ENV}")
    elif COOKIEFILE_ENV:
        log.warning(f"COOKIEFILE set to '{COOKIEFILE_ENV}' but file not found!")
    
    return opts

ytdl = yt_dlp.YoutubeDL(get_ytdl_options())

ffmpeg_opts = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -b:a 128k"
}

# -------------------- Player wrapper --------------------
class Player(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, ctx, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title") or "Unknown title"
        self.url = data.get("url")  # direct media url for ffmpeg
        self.webpage_url = data.get("webpage_url")
        self.requester = ctx.author
        self.ctx = ctx  # keep ctx so play_next can send messages

    @classmethod
    async def from_query(cls, query, ctx, *, loop=None, timeout=120):
        loop = loop or asyncio.get_event_loop()
        try:
            # Recreate ytdl instance with fresh options (in case cookies were just added)
            ytdl_temp = yt_dlp.YoutubeDL(get_ytdl_options())
            
            # run in executor to avoid blocking
            func = lambda: ytdl_temp.extract_info(query, download=False)
            data = await asyncio.wait_for(loop.run_in_executor(None, func), timeout=timeout)
            
            if not data:
                raise Exception("No data returned from yt-dlp")
            
            if "entries" in data and data["entries"]:
                # If ytsearch returned results, pick the first
                data = data["entries"][0]
            
            if not data.get("url"):
                raise Exception("No playable URL found by yt-dlp")
            
            src = discord.FFmpegPCMAudio(data["url"], **ffmpeg_opts)
            return cls(src, data=data, ctx=ctx)
            
        except asyncio.TimeoutError:
            raise Exception("⏱️ Timeout while contacting YouTube (network slow or video unavailable)")
        except Exception as e:
            err = str(e).lower()
            # Better error messages
            if "sign in" in err or "not available" in err or "members-only" in err:
                raise Exception(
                    "🔒 This video requires YouTube login/cookies.\n"
                    "**How to fix:**\n"
                    "1. Export cookies from your browser using an extension like 'Get cookies.txt'\n"
                    "2. Upload cookies.txt to your Render project\n"
                    "3. Set environment variable: `COOKIEFILE=cookies.txt`"
                )
            elif "private video" in err:
                raise Exception("🔒 This video is private and cannot be accessed")
            elif "video unavailable" in err:
                raise Exception("❌ Video unavailable (deleted, region-locked, or age-restricted)")
            elif "no video formats" in err:
                raise Exception("❌ No playable audio format found for this video")
            else:
                raise Exception(f"yt-dlp error: {str(e)[:200]}")

# -------------------- Spotify helpers --------------------
def spotify_queries_from_url(spotify_url):
    """If Spotify API creds set, return list of "song artist" queries."""
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    
    if not spotipy:
        log.warning("Spotipy library not installed")
        return queries
        
    if not client_id or not client_secret:
        log.warning("Spotify credentials not set in environment")
        return queries

    try:
        auth = SpotifyClientCredentials(client_id=client_id, client_secret=client_secret)
        sp = spotipy.Spotify(auth_manager=auth)
        
        if "track" in spotify_url:
            track = sp.track(spotify_url)
            name = track.get("name")
            artist = track.get("artists", [{}])[0].get("name", "")
            if name:
                queries.append(f"{name} {artist}")
                
        elif "playlist" in spotify_url:
            results = sp.playlist_tracks(spotify_url)
            for item in results.get("items", []):
                t = item.get("track")
                if t:
                    name = t.get("name")
                    artist = t.get("artists", [{}])[0].get("name", "")
                    if name:
                        queries.append(f"{name} {artist}")
                        
        elif "album" in spotify_url:
            results = sp.album_tracks(spotify_url)
            album_info = sp.album(spotify_url)
            album_artist = album_info.get("artists", [{}])[0].get("name", "")
            for item in results.get("items", []):
                name = item.get("name")
                # Try track artist first, fall back to album artist
                artist = item.get("artists", [{}])[0].get("name", album_artist)
                if name:
                    queries.append(f"{name} {artist}")
                    
    except Exception as e:
        log.error(f"Spotify API error: {e}")
        raise Exception(
            f"🎵 Spotify error: {str(e)[:100]}\n"
            "**To fix Spotify support:**\n"
            "1. Go to https://developer.spotify.com/dashboard\n"
            "2. Create an app and get Client ID & Secret\n"
            "3. Set environment variables:\n"
            "   - `SPOTIFY_CLIENT_ID=your_client_id`\n"
            "   - `SPOTIFY_CLIENT_SECRET=your_client_secret`"
        )
    
    return queries

def simple_spotify_title_scrape(spotify_url):
    """Fallback: fetch page title and clean it"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        r = requests.get(spotify_url, timeout=10, headers=headers)
        html = r.text
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if m:
            title = m.group(1).replace(" - Spotify", "").replace(" | Spotify", "").strip()
            # Remove common Spotify metadata
            title = re.sub(r'\s*-\s*song\s+(and\s+)?lyrics.*', '', title, flags=re.I)
            return title
    except Exception as e:
        log.debug("Spotify scrape error: %s", e)
    return None

# -------------------- Core commands --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    log.info(f"Cookies file: {COOKIEFILE_ENV if COOKIEFILE_ENV else 'Not set'}")
    log.info(f"Spotify API: {'Configured' if os.getenv('SPOTIFY_CLIENT_ID') else 'Not configured'}")
    
    # Reset state to avoid stale in-memory queues (useful on redeploy)
    music_queues.clear()
    now_playing.clear()
    loop_mode.clear()

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query: str):
    if not ctx.author.voice:
        await ctx.send("❌ You must be in a voice channel to play music.")
        return

    # Ensure connected
    channel = ctx.author.voice.channel
    if not ctx.voice_client:
        try:
            await channel.connect()
        except Exception as e:
            await ctx.send(f"❌ Could not connect to voice channel: {e}")
            return

    guild_id = ctx.guild.id
    q = music_queues.setdefault(guild_id, deque())
    loop_mode.setdefault(guild_id, "off")

    status = await ctx.send("🔍 Searching...")

    # Handle Spotify input
    if "spotify.com" in query or "spotify.link" in query:
        try:
            # Try API first
            queries = spotify_queries_from_url(query)
            
            # Fallback to scraping if API fails
            if not queries:
                log.info("Spotify API failed, trying scrape fallback")
                scraped = simple_spotify_title_scrape(query)
                if scraped:
                    queries = [scraped]
                else:
                    await status.edit(content=(
                        "❌ **Spotify Error**: Cannot parse this link.\n\n"
                        "**To enable Spotify support:**\n"
                        "1. Get API credentials from https://developer.spotify.com/dashboard\n"
                        "2. Set `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` in Render environment variables\n\n"
                        "Or try searching by song name instead!"
                    ))
                    return

            await status.edit(content=f"🎵 Processing {len(queries)} track(s) from Spotify...")
            added = 0
            failed = 0
            
            for idx, qstr in enumerate(queries, 1):
                try:
                    if idx % 5 == 0:  # Update status every 5 songs
                        await status.edit(content=f"🎵 Processing: {idx}/{len(queries)} tracks...")
                    
                    player = await Player.from_query(f"ytsearch:{qstr}", ctx, loop=bot.loop)
                    music_queues[guild_id].append(player)
                    added += 1
                except Exception as e:
                    failed += 1
                    log.warning(f"Error adding Spotify track '{qstr}': {e}")
            
            result_msg = f"✅ Added **{added}** track(s) from Spotify"
            if failed > 0:
                result_msg += f" ({failed} failed)"
            
            await status.edit(content=result_msg)
            
            # Start playback if nothing playing
            if not ctx.voice_client.is_playing() and guild_id not in now_playing:
                await asyncio.sleep(0.5)
                await status.delete()
                await _start_playback(ctx)
            else:
                await asyncio.sleep(3)
                await status.delete()
            return
            
        except Exception as e:
            await status.edit(content=f"❌ Spotify error: {str(e)[:500]}")
            await asyncio.sleep(5)
            await status.delete()
            return

    # YouTube playlist or direct link vs search
    is_playlist = "playlist" in query and ("youtube.com" in query or "list=" in query)
    
    try:
        if is_playlist:
            await status.edit(content="📋 Extracting playlist...")
            loop = asyncio.get_event_loop()
            
            ytdl_temp = yt_dlp.YoutubeDL(get_ytdl_options())
            data = await loop.run_in_executor(None, lambda: ytdl_temp.extract_info(query, download=False))
            
            entries = data.get("entries", []) if data else []
            added = 0
            failed = 0
            
            for idx, entry in enumerate(entries, 1):
                if not entry:
                    failed += 1
                    continue
                    
                if idx % 10 == 0:  # Update every 10 songs
                    await status.edit(content=f"📋 Processing: {idx}/{len(entries)} videos...")
                
                vid_id = entry.get("id")
                if vid_id:
                    try:
                        player = await Player.from_query(f"https://www.youtube.com/watch?v={vid_id}", ctx, loop=bot.loop, timeout=60)
                        music_queues[guild_id].append(player)
                        added += 1
                    except Exception as e:
                        failed += 1
                        log.warning(f"Playlist entry load failed: {e}")
            
            result_msg = f"✅ Added **{added}** song(s) from playlist"
            if failed > 0:
                result_msg += f" ({failed} unavailable)"
            
            await status.edit(content=result_msg)
            
            if not ctx.voice_client.is_playing() and guild_id not in now_playing:
                await asyncio.sleep(0.5)
                await status.delete()
                await _start_playback(ctx)
            else:
                await asyncio.sleep(3)
                await status.delete()
            return

        # Normal single video / search
        if not query.startswith("http"):
            search_query = f"ytsearch:{query}"
        else:
            search_query = query

        player = await Player.from_query(search_query, ctx, loop=bot.loop)
        music_queues[guild_id].append(player)
        
        await status.edit(content=f"✅ Queued: **{player.title}**")
        
        # Start playback if idle
        if not ctx.voice_client.is_playing() and guild_id not in now_playing:
            await asyncio.sleep(0.5)
            await status.delete()
            await _start_playback(ctx)
        else:
            await asyncio.sleep(2)
            await status.delete()

    except Exception as e:
        await status.edit(content=f"❌ {str(e)[:1000]}")
        await asyncio.sleep(7)
        try:
            await status.delete()
        except:
            pass

# -------------------- Playback control --------------------
async def _start_playback(ctx):
    """Start playing next track in the queue."""
    guild_id = ctx.guild.id
    q = music_queues.get(guild_id)
    
    if not q or not q:
        await _start_idle_timer(ctx)
        return

    mode = loop_mode.get(guild_id, "off")
    
    if mode == "track" and guild_id in now_playing:
        player = now_playing[guild_id]
    else:
        player = q.popleft()
        if mode == "queue":
            q.append(player)

    now_playing[guild_id] = player

    async def _after_play(err):
        if err:
            log.warning(f"Playback error: {err}")
            try:
                await player.ctx.send(f"⚠️ Playback error. Skipping...")
            except Exception:
                pass
            await asyncio.sleep(1)
            await _start_playback(player.ctx)
        else:
            await asyncio.sleep(0.5)
            await _start_playback(player.ctx)

    def after_callback(e):
        fut = asyncio.run_coroutine_threadsafe(_after_play(e), bot.loop)
        try:
            fut.result(timeout=10)
        except Exception as ex:
            log.exception(f"After callback error: {ex}")

    # Play
    try:
        if not player.url:
            raise Exception("No stream URL available")
        
        # Create fresh FFmpeg source
        source = discord.FFmpegPCMAudio(player.data["url"], **ffmpeg_opts)
        
        vc = player.ctx.voice_client
        if not vc or not vc.is_connected():
            await player.ctx.author.voice.channel.connect()
            vc = player.ctx.voice_client
        
        vc.play(source, after=after_callback)
        
    except Exception as e:
        log.exception(f"Failed to start playback: {e}")
        await player.ctx.send(f"❌ Failed to play **{player.title}**: {str(e)[:200]}")
        await asyncio.sleep(1)
        await _start_playback(player.ctx)
        return

    mode_emoji = " 🔂" if mode == "track" else " 🔁" if mode == "queue" else ""
    
    try:
        await player.ctx.send(f"🎶 Now playing: **{player.title}**{mode_emoji}")
    except Exception:
        pass

async def _start_idle_timer(ctx):
    """Disconnect after idle (120s) if still not playing and queue empty."""
    if not ctx.voice_client:
        return
    
    await asyncio.sleep(120)
    guild_id = ctx.guild.id
    
    if ctx.voice_client and not ctx.voice_client.is_playing() and not music_queues.get(guild_id):
        try:
            await ctx.voice_client.disconnect()
        except:
            pass
        music_queues.pop(guild_id, None)
        now_playing.pop(guild_id, None)
        loop_mode.pop(guild_id, None)

# -------------------- Simple control commands --------------------
@bot.command(name="skip", aliases=["s"])
async def skip(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped!")
    else:
        await ctx.send("❌ Nothing is playing.")

@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused.")
    else:
        await ctx.send("❌ Nothing playing.")

@bot.command(name="resume", aliases=["r"])
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed.")
    else:
        await ctx.send("❌ Nothing paused.")

@bot.command(name="stop")
async def stop(ctx):
    gid = ctx.guild.id
    q = music_queues.get(gid)
    if q:
        q.clear()
    loop_mode[gid] = "off"
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped and cleared queue.")
    else:
        await ctx.send("❌ I'm not connected.")

@bot.command(name="leave", aliases=["dc", "disconnect"])
async def leave(ctx):
    gid = ctx.guild.id
    music_queues.pop(gid, None)
    now_playing.pop(gid, None)
    loop_mode.pop(gid, None)
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Disconnected.")
    else:
        await ctx.send("❌ I'm not connected.")

@bot.command(name="queue", aliases=["q"])
async def _queue(ctx):
    gid = ctx.guild.id
    q = music_queues.get(gid, deque())
    
    if gid not in now_playing:
        await ctx.send("❌ Nothing is playing and queue is empty.")
        return
    
    np = now_playing[gid].title
    
    if not q:
        await ctx.send(f"🎵 Now playing: **{np}**\nQueue is empty.")
        return
    
    msg = f"🎵 **Now Playing:** {np}\n\n**Up Next:**\n"
    for i, p in enumerate(list(q)[:15], 1):
        msg += f"{i}. {p.title[:80]}\n"
    
    if len(q) > 15:
        msg += f"\n...and {len(q) - 15} more"
    
    await ctx.send(msg)

@bot.command(name="loop", aliases=["l"])
async def loop_cmd(ctx, mode: str = None):
    gid = ctx.guild.id
    cur = loop_mode.get(gid, "off")
    
    if not mode:
        new = "track" if cur == "off" else "queue" if cur == "track" else "off"
    else:
        m = mode.lower()
        if m in ("track", "t", "song"): 
            new = "track"
        elif m in ("queue", "q", "all"): 
            new = "queue"
        else: 
            new = "off"
    
    loop_mode[gid] = new
    txt = {"off": "❌ Loop disabled", "track": "🔂 Looping current track", "queue": "🔁 Looping queue"}
    await ctx.send(txt[new])

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx):
    gid = ctx.guild.id
    if gid in now_playing:
        p = now_playing[gid]
        mode = loop_mode.get(gid, "off")
        emoji = " 🔂" if mode == "track" else " 🔁" if mode == "queue" else ""
        await ctx.send(f"🎵 Now playing: **{p.title}**{emoji}\nRequested by: {p.requester.mention}")
    else:
        await ctx.send("❌ Nothing playing right now.")

@bot.command(name="help", aliases=["commands", "h"])
async def help_cmd(ctx):
    embed = discord.Embed(
        title="🎶 Music Bot Commands",
        description="A simple music bot for YouTube and Spotify",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="▶️ Playback",
        value=(
            "**!play** `<song>` - Play a song/playlist\n"
            "**!pause** - Pause playback\n"
            "**!resume** - Resume playback\n"
            "**!skip** - Skip current song\n"
            "**!stop** - Stop and clear queue\n"
            "**!leave** - Disconnect bot"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📋 Queue",
        value=(
            "**!queue** - Show queue\n"
            "**!nowplaying** - Current song\n"
            "**!loop** - Toggle loop mode\n"
            "**!clear** - Clear queue (Admin)"
        ),
        inline=False
    )
    
    embed.add_field(
        name="✨ Features",
        value=(
            "• YouTube videos & playlists\n"
            "• Spotify tracks, albums & playlists\n"
            "• Auto-disconnect when inactive\n"
            "• Auto-leave when alone in voice"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Setup Notes",
        value=(
            f"**Auto-leave:** {IDLE_TIMEOUT//60} min inactive or {ALONE_TIMEOUT//60} min alone\n"
            "**YouTube Cookies:** Some videos need login\n"
            "→ Export cookies.txt and set `COOKIEFILE` env var\n"
            "**Spotify:** Requires API credentials\n"
            "→ Get from developer.spotify.com\n"
            "→ Set `SPOTIFY_CLIENT_ID` & `SPOTIFY_CLIENT_SECRET`"
        ),
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def clear_queue(ctx):
    """Admin command to clear the queue"""
    gid = ctx.guild.id
    q = music_queues.get(gid)
    if q:
        count = len(q)
        q.clear()
        await ctx.send(f"🗑️ Cleared {count} song(s) from queue.")
    else:
        await ctx.send("❌ Queue is already empty.")

@bot.command(name="volume", aliases=["vol", "v"])
async def volume(ctx, vol: int = None):
    """Change the player volume (0-100)"""
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing right now.")
        return
    
    if vol is None:
        # Show current volume
        current = int(ctx.voice_client.source.volume * 100)
        await ctx.send(f"🔊 Current volume: **{current}%**")
        return
    
    if vol < 0 or vol > 100:
        await ctx.send("❌ Volume must be between 0 and 100.")
        return
    
    ctx.voice_client.source.volume = vol / 100
    await ctx.send(f"🔊 Volume set to **{vol}%**")

@bot.command(name="settings", aliases=["config"])
async def settings_cmd(ctx):
    """Show bot configuration status"""
    embed = discord.Embed(
        title="⚙️ Bot Configuration",
        color=discord.Color.green()
    )
    
    # Check cookies
    cookies_status = "✅ Configured" if COOKIEFILE_ENV and os.path.exists(COOKIEFILE_ENV) else "❌ Not set"
    embed.add_field(name="YouTube Cookies", value=cookies_status, inline=True)
    
    # Check Spotify
    spotify_status = "✅ Configured" if os.getenv("SPOTIFY_CLIENT_ID") else "❌ Not set"
    embed.add_field(name="Spotify API", value=spotify_status, inline=True)
    
    # Auto-leave settings
    embed.add_field(
        name="Auto-Leave Settings",
        value=f"Idle timeout: {IDLE_TIMEOUT//60} minutes\nAlone timeout: {ALONE_TIMEOUT//60} minute",
        inline=False
    )
    
    # Voice status
    if ctx.voice_client:
        channel = ctx.voice_client.channel
        members = len([m for m in channel.members if not m.bot])
        embed.add_field(
            name="Voice Status",
            value=f"Connected to: {channel.name}\nUsers: {members}",
            inline=False
        )
    else:
        embed.add_field(name="Voice Status", value="Not connected", inline=False)
    
    await ctx.send(embed=embed)

# -------------------- Error handling --------------------
@bot.event
async def on_command_error(ctx, error):
    """Global error handler"""
    if isinstance(error, commands.CommandNotFound):
        return  # Ignore unknown commands
    
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    
    elif isinstance(error, commands.BotMissingPermissions):
        await ctx.send("❌ I don't have the necessary permissions to do that.")
    
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument. Use `!help` for command usage.")
    
    else:
        log.error(f"Command error: {error}", exc_info=error)
        await ctx.send(f"❌ An error occurred: {str(error)[:200]}")

# -------------------- Start up --------------------
if __name__ == "__main__":
    start_keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        log.error("DISCORD_TOKEN not found in environment. Set it in Render service settings.")
        raise SystemExit("DISCORD_TOKEN required")
    
    log.info("Starting Discord Music Bot...")
    log.info(f"Auto-leave settings: {IDLE_TIMEOUT}s idle, {ALONE_TIMEOUT}s alone")
    bot.run(token)Color.blue()
    )
    
    embed.add_field(
        name="▶️ Playback",
        value=(
            "**!play** `<song>` - Play a song\n"
            "**!pause** - Pause playback\n"
            "**!resume** - Resume playback\n"
            "**!skip** - Skip current song\n"
            "**!stop** - Stop and clear queue\n"
            "**!leave** - Disconnect bot"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📋 Queue",
        value=(
            "**!queue** - Show queue\n"
            "**!nowplaying** - Current song\n"
            "**!loop** - Toggle loop mode"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🔧 Setup Notes",
        value=(
            "**YouTube Cookies:** Some videos need login\n"
            "→ Export cookies.txt and set `COOKIEFILE` env var\n\n"
            "**Spotify:** Requires API credentials\n"
            "→ Get from developer.spotify.com\n"
            "→ Set `SPOTIFY_CLIENT_ID` & `SPOTIFY_CLIENT_SECRET`"
        ),
        inline=False
    )
    
    await ctx.send(embed=embed)

@bot.command(name="clear")
@commands.has_permissions(administrator=True)
async def clear_queue(ctx):
    """Admin command to clear the queue"""
    gid = ctx.guild.id
    q = music_queues.get(gid)
    if q:
        count = len(q)
        q.clear()
        await ctx.send(f"🗑️ Cleared {count} song(s) from queue.")
    else:
        await ctx.send("❌ Queue is already empty.")

# -------------------- Start up --------------------
if __name__ == "__main__":
    start_keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    
    if not token:
        log.error("DISCORD_TOKEN not found in environment. Set it in Render service settings.")
        raise SystemExit("DISCORD_TOKEN required")
    
    log.info("Starting Discord Music Bot...")
    bot.run(token)
