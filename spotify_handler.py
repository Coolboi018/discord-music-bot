import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from youtubesearchpython import VideosSearch
import os

sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET")))


def get_youtube_urls_from_spotify(sp_url):
    urls = []
    try:
        if "playlist" in sp_url:
            playlist = sp.playlist_tracks(sp_url)
            for item in playlist['items']:
                track = item['track']
                query = f"{track['name']} {track['artists'][0]['name']}"
                yt = VideosSearch(query, limit=1).result()
                if yt["result"]:
                    urls.append(yt["result"][0]["link"])

        elif "track" in sp_url:
            track = sp.track(sp_url)
            query = f"{track['name']} {track['artists'][0]['name']}"
            yt = VideosSearch(query, limit=1).result()
            if yt["result"]:
                urls.append(yt["result"][0]["link"])

    except Exception as e:
        print("Error while processing Spotify URL:", e)
    return urls
