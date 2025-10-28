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
ytdl_opts = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "skip_unavailable_fragments": True,
    "ignoreerrors": False,
    "no_check_certificate": True,
    "extract_flat": False,
    "noplaylist": False,
    "http_headers": {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }
}
if COOKIEFILE_ENV:
    ytdl_opts["cookiefile"] = COOKIEFILE_ENV

ytdl = yt_dlp.YoutubeDL(ytdl_opts)

ffmpeg_opts = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn"
}

# -------------------- Player wrapper --------------------
class Player(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, ctx, volume=0.7):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get("title") or "Unknown title"
        self.url = data.get("url")  # direct media url for ffmpeg
        self.webpage_url = data.get("webpage_url")
        self.requester = ctx.author
        self.ctx = ctx  # keep ctx so play_next can send messages

    @classmethod
    async def from_query(cls, query, ctx, *, loop=None, timeout=90):
        loop = loop or asyncio.get_event_loop()
        try:
            # run in executor to avoid blocking
            func = lambda: ytdl.extract_info(query, download=False)
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
            raise Exception("Timeout while contacting YouTube/yt-dlp (slow network)")
        except Exception as e:
            err = str(e)
            if "confirm you're not a bot" in err or "Sign in to confirm" in err:
                if COOKIEFILE_ENV:
                    raise Exception("YouTube requires cookies for this video, cookies were set but extraction still failed.")
                else:
                    raise Exception("YouTube may require cookies for this video. Provide a cookies.txt and set COOKIEFILE env.")
            raise

# -------------------- Spotify helpers --------------------
def spotify_queries_from_url(spotify_url):
    """If Spotify API creds set, return list of "song artist" queries."""
    queries = []
    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not spotipy or not client_id or not client_secret:
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
            for item in results.get("items", []):
                name = item.get("name")
                artist = item.get("artists", [{}])[0].get("name", "")
                if name:
                    queries.append(f"{name} {artist}")
    except Exception as e:
        log.warning("Spotify API error: %s", e)
    return queries

def simple_spotify_title_scrape(spotify_url):
    """Fallback: fetch page title and clean it"""
    try:
        r = requests.get(spotify_url, timeout=8)
        html = r.text
        m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        if m:
            title = m.group(1).replace(" - Spotify", "").strip()
            # For playlists/albums titles this might include "Various Artists" etc.
            return title
    except Exception as e:
        log.debug("Spotify scrape error: %s", e)
    return None

# -------------------- Core commands --------------------
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
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

    status = await ctx.send("🔍 Preparing your track...")

    # Handle Spotify input
    if "spotify.com" in query:
        # Prefer Spotify API if keys are available
        queries = spotify_queries_from_url(query)
        if not queries:
            # fallback to one title scraped
            scraped = simple_spotify_title_scrape(query)
            if scraped:
                queries = [scraped]
            else:
                await status.edit(content="❌ Couldn't parse Spotify link. Provide Spotify API keys or try the song name.")
                await asyncio.sleep(3)
                await status.delete()
                return

        added = 0
        for qstr in queries:
            try:
                player = await Player.from_query(f"ytsearch:{qstr}", ctx, loop=bot.loop)
                music_queues[guild_id].append(player)
                added += 1
            except Exception as e:
                log.warning("Error adding Spotify-derived track %s: %s", qstr, e)
        await status.edit(content=f"✅ Added {added} tracks from Spotify link.")
        # kick playback if nothing currently playing
        if not ctx.voice_client.is_playing() and guild_id not in now_playing:
            await asyncio.sleep(0.3)
            await status.delete()
            await _start_playback(ctx)
        else:
            await asyncio.sleep(2)
            await status.delete()
        return

    # YouTube playlist or direct link vs search
    is_playlist = "playlist" in query and ("youtube.com" in query or "list=" in query)
    try:
        if is_playlist:
            await status.edit(content="📋 Extracting playlist...")
            # Extract playlist entries (yt-dlp returns dict with entries)
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(query, download=False))
            entries = data.get("entries", []) if data else []
            added = 0
            for entry in entries:
                if not entry:
                    continue
                vid_id = entry.get("id")
                if vid_id:
                    try:
                        player = await Player.from_query(f"https://www.youtube.com/watch?v={vid_id}", ctx, loop=bot.loop)
                        music_queues[guild_id].append(player)
                        added += 1
                    except Exception as e:
                        log.warning("Playlist entry load failed: %s", e)
            await status.edit(content=f"✅ Added {added} songs from playlist.")
            if not ctx.voice_client.is_playing() and guild_id not in now_playing:
                await asyncio.sleep(0.3)
                await status.delete()
                await _start_playback(ctx)
            else:
                await asyncio.sleep(2)
                await status.delete()
            return

        # Normal single video / search
        # If it's not an http link, use ytsearch: to search
        if not query.startswith("http"):
            search_query = f"ytsearch:{query}"
        else:
            search_query = query

        player = await Player.from_query(search_query, ctx, loop=bot.loop)
        music_queues[guild_id].append(player)
        await status.edit(content=f"✅ Queued: **{player.title}** (requested by {ctx.author.display_name})")
        # Start playback if idle
        if not ctx.voice_client.is_playing() and guild_id not in now_playing:
            await asyncio.sleep(0.3)
            await status.delete()
            await _start_playback(ctx)
        else:
            await asyncio.sleep(2)
            await status.delete()

    except Exception as e:
        # Provide actionable message
        msg = str(e)
        if "cookies" in msg.lower() or "require cookies" in msg.lower():
            msg += "\nTip: Add a `cookies.txt` and set COOKIEFILE env to access some videos."
        await ctx.send(f"❌ Error: {msg}")
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
        # schedule idle disconnect: only if voice client exists
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
            log.warning("Playback error: %s", err)
            try:
                await player.ctx.send(f"⚠️ Playback error: {err}. Skipping...")
            except Exception:
                pass
            await asyncio.sleep(1)
            await _start_playback(player.ctx)
        else:
            await asyncio.sleep(0.2)
            await _start_playback(player.ctx)

    # wrap after to run in event loop
    def after_callback(e):
        fut = asyncio.run_coroutine_threadsafe(_after_play(e), bot.loop)
        try:
            fut.result(timeout=10)
        except Exception as ex:
            log.exception("after callback exec error: %s", ex)

    # Play
    try:
        if not player.url:
            raise Exception("No stream URL available.")
        # create a fresh FFmpeg source in case underlying url requires it
        source = discord.FFmpegPCMAudio(player.data["url"], **ffmpeg_opts)
        player.audio_source = source  # keep attribute if needed
        vc = player.ctx.voice_client
        if not vc or not vc.is_connected():
            # attempt reconnect
            await player.ctx.author.voice.channel.connect()
            vc = player.ctx.voice_client
        vc.play(source, after=after_callback)
    except Exception as e:
        log.exception("Failed to start playback: %s", e)
        await player.ctx.send(f"❌ Failed to play **{player.title}**: {e}")
        # Try next one
        await asyncio.sleep(1)
        await _start_playback(player.ctx)
        return

    mode_emoji = " 🔂" if mode == "track" else " 🔁" if mode == "queue" else ""
    try:
        await player.ctx.send(f"🎶 Now playing: **{player.title}** requested by **{player.requester.display_name}**{mode_emoji}")
    except Exception:
        # ignore send errors (channel permission/etc)
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
        ctx.voice_client.stop()  # after callback will start next
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
    for i, p in enumerate(list(q)[:12], 1):
        msg += f"{i}. {p.title}\n"
    await ctx.send(msg)

@bot.command(name="loop", aliases=["l"])
async def loop_cmd(ctx, mode: str = None):
    gid = ctx.guild.id
    cur = loop_mode.get(gid, "off")
    if not mode:
        new = "track" if cur == "off" else "queue" if cur == "track" else "off"
    else:
        m = mode.lower()
        if m in ("track", "t"): new = "track"
        elif m in ("queue", "q"): new = "queue"
        else: new = "off"
    loop_mode[gid] = new
    txt = {"off": "❌ Loop off", "track": "🔂 Loop track", "queue": "🔁 Loop queue"}
    await ctx.send(txt[new])

@bot.command(name="nowplaying", aliases=["np"])
async def nowplaying_cmd(ctx):
    gid = ctx.guild.id
    if gid in now_playing:
        p = now_playing[gid]
        mode = loop_mode.get(gid, "off")
        emoji = " 🔂" if mode == "track" else " 🔁" if mode == "queue" else ""
        await ctx.send(f"🎵 Now playing: **{p.title}**{emoji}")
    else:
        await ctx.send("❌ Nothing playing right now.")

@bot.command(name="help", aliases=["commands"])
async def help_cmd(ctx):
    await ctx.send(
        "🎶 **Music Bot Commands**\n"
        "!play <song name|YouTube url|Spotify url> — Play or queue\n"
        "!skip — Skip song\n"
        "!pause / !resume — Pause/resume\n"
        "!queue — Show queue\n"
        "!loop — Toggle loop (off/track/queue)\n"
        "!stop — Stop & clear queue\n"
        "!leave — Disconnect\n"
        "!nowplaying — Show current song\n\n"
        "Notes:\n- For some YouTube videos (age/region protected) you may need a cookies.txt and set COOKIEFILE env.\n- For Spotify playlist/album support, set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in env.\n"
    )

# -------------------- Start up --------------------
if __name__ == "__main__":
    start_keep_alive()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not found in environment. Set it in Render service settings.")
        raise SystemExit("DISCORD_TOKEN required")
    # Prevent duplicate instances: rely on Render single server process and this single bot object.
    bot.run(token)
