import os
import requests
import asyncio
import time
from fastapi import FastAPI, Form
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

# Spotify credentials
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")

# Slack credentials
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN")
SLACK_CHANNEL = os.getenv("SLACK_CHANNEL")
SERVER_HOST = os.getenv("SERVER_HOST")

slack_user_name = None

if not all(
    [
        SPOTIFY_CLIENT_ID,
        SPOTIFY_CLIENT_SECRET,
        SPOTIFY_REDIRECT_URI,
        SLACK_BOT_TOKEN,
        SLACK_CHANNEL,
    ]
):
    raise EnvironmentError("One or more required environment variables are missing.")

spotify_access_token = None
spotify_refresh_token = None
current_track_id = None

SCOPES = "user-read-currently-playing user-read-playback-state"


def load_slack_user_identity():
    global slack_user_name

    response = requests.get(
        "https://slack.com/api/users.profile.get",
        headers={"Authorization": f"Bearer {SLACK_USER_TOKEN}"},
    )
    print(response.text)
    data = response.json()

    if not data.get("ok"):
        print("Failed to load Slack user identity:", data)
        return

    slack_user_name = data["profile"]["real_name"]  # real name
    print(f"Loaded Slack user name: {slack_user_name}")


# ----- Spotify OAuth -----
@app.get("/login")
def login():
    url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={SPOTIFY_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={SPOTIFY_REDIRECT_URI}"
        f"&scope={SCOPES}"
    )
    return RedirectResponse(url)


@app.get("/callback")
def callback(code: str):
    global spotify_access_token, spotify_refresh_token
    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": SPOTIFY_REDIRECT_URI,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    tokens = response.json()
    spotify_access_token = tokens["access_token"]
    spotify_refresh_token = tokens["refresh_token"]
    return "Spotify login successful! You can close this page."


# ----- Spotify token refresh -----
def refresh_spotify_token():
    global spotify_access_token
    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": spotify_refresh_token,
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    spotify_access_token = response.json()["access_token"]


# ----- Get currently playing track -----
def get_current_track():
    headers = {"Authorization": f"Bearer {spotify_access_token}"}
    url = "https://api.spotify.com/v1/me/player/currently-playing"
    response = requests.get(url, headers=headers)
    if response.status_code != 200 or response.text == "":
        return None
    data = response.json()
    track = data["item"]
    return {
        "id": track["id"],
        "name": track["name"],
        "artist": ", ".join([a["name"] for a in track["artists"]]),
        "album_cover": track["album"]["images"][0]["url"],
        "url": track["external_urls"]["spotify"],
    }


# ----- Post track to Slack channel -----
def post_to_slack(track):
    spotify_link = f"<{track['url']}|Open on Spotify>"
    user_text = f"*{slack_user_name}* is listening to:"

    message = {
        "channel": SLACK_CHANNEL,
        "text": (
            f"🎧 {user_text}\n*{track['name']}* by *{track['artist']}*\n{spotify_link}"
        ),
        "attachments": [{"image_url": track["album_cover"], "alt_text": "Album cover"}],
    }

    requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=message,
    )


# ----- Update Slack user status -----
def update_slack_status(track):
    duration_ms = track.get("duration_ms", 0)
    progress_ms = track.get("progress_ms", 0)

    # Compute remaining time
    remaining_ms = max(duration_ms - progress_ms, 0)
    remaining_seconds = remaining_ms // 1000

    # Slack expects a UNIX timestamp
    expiration_timestamp = int(time.time()) + remaining_seconds

    profile = {
        "status_text": f"{track['name']} by {track['artist']}",
        "status_emoji": ":musical_note:",
        "status_expiration": expiration_timestamp,
    }

    requests.post(
        "https://slack.com/api/users.profile.set",
        headers={"Authorization": f"Bearer {SLACK_USER_TOKEN}"},
        json={"profile": profile},
    )


@app.post("/slack/start")
async def slack_start(command: str = Form(...), user_id: str = Form(...)):
    """
    Handles /start command.
    Sends a temporary redirect page that auto-opens /login in the browser.
    """
    # Generate a temporary redirect URL
    # This URL can include the Slack user_id if needed
    redirect_url = f"https://{SERVER_HOST}/login"

    # Respond with ephemeral message containing the redirect link
    return {
        "response_type": "ephemeral",
        "text": f"Click here to authorize Spotify: <{redirect_url}|Authorize Spotify>",
    }


# ----- Background task to watch tracks -----
async def track_watcher():
    global current_track_id
    while True:
        if spotify_access_token:
            try:
                refresh_spotify_token()
                track = get_current_track()
                if track and track["id"] != current_track_id:
                    current_track_id = track["id"]
                    post_to_slack(track)  # optional: channel post
                    update_slack_status(track)  # update Slack profile status
            except Exception as e:
                print("Error fetching Spotify track:", e)
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup_event():
    load_slack_user_identity()
    asyncio.create_task(track_watcher())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
