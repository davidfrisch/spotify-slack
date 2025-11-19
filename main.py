import os
import requests
import asyncio
import time
from typing import Optional
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

# Store tokens and state per user
user_tokens: dict[
    str, dict[str, str]
] = {}  # {user_id: {"access_token": str, "refresh_token": str, "slack_name": str}}
user_current_tracks: dict[str, str] = {}  # {user_id: track_id}
pending_auth: dict[
    str, str
] = {}  # {state: user_id} - maps OAuth state to Slack user_id

SCOPES = "user-read-currently-playing user-read-playback-state"


def load_slack_user_identity(user_id: str):
    """Load Slack user identity for a specific user"""
    response = requests.get(
        f"https://slack.com/api/users.info?user={user_id}",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
    )
    data = response.json()

    if not data.get("ok"):
        return None

    return data["user"]["profile"].get("real_name", data["user"]["name"])


# ----- Spotify OAuth -----
@app.get("/login")
def login(user_id: Optional[str] = None):
    """Initiate Spotify OAuth flow for a specific user"""
    import secrets

    # Generate a unique state token
    state = secrets.token_urlsafe(16)

    if user_id:
        pending_auth[state] = user_id

    url = (
        "https://accounts.spotify.com/authorize"
        f"?client_id={SPOTIFY_CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={SPOTIFY_REDIRECT_URI}"
        f"&scope={SCOPES}"
        f"&state={state}"
    )
    return RedirectResponse(url)


@app.get("/callback")
def callback(code: str, state: Optional[str] = None):
    """Handle Spotify OAuth callback and store tokens per user"""
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

    # Get user_id from state
    user_id = pending_auth.pop(state, None) if state else None

    if user_id:
        # Load Slack user name
        slack_name = load_slack_user_identity(user_id)

        # Store tokens for this user
        user_tokens[user_id] = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "slack_name": slack_name or "Unknown User",
        }
        return f"Spotify login successful for {user_tokens[user_id]['slack_name']}! You can close this page."
    else:
        # Fallback for backward compatibility
        return "Spotify login successful! You can close this page."


# ----- Spotify token refresh -----
def refresh_spotify_token(user_id: str):
    """Refresh Spotify token for a specific user"""
    if user_id not in user_tokens:
        return False

    url = "https://accounts.spotify.com/api/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": user_tokens[user_id]["refresh_token"],
        "client_id": SPOTIFY_CLIENT_ID,
        "client_secret": SPOTIFY_CLIENT_SECRET,
    }
    response = requests.post(url, data=data)
    response.raise_for_status()
    user_tokens[user_id]["access_token"] = response.json()["access_token"]
    return True


# ----- Get currently playing track -----
def get_current_track(user_id: str):
    """Get currently playing track for a specific user"""
    if user_id not in user_tokens:
        return None

    headers = {"Authorization": f"Bearer {user_tokens[user_id]['access_token']}"}
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
        "duration_ms": track["duration_ms"],
        "progress_ms": data.get("progress_ms", 0),
    }


# ----- Post track to Slack channel -----
def post_to_slack(track, user_id: str):
    """Post track to Slack for a specific user"""
    slack_name = user_tokens.get(user_id, {}).get("slack_name", "Unknown User")
    spotify_link = f"<{track['url']}|Open on Spotify>"
    user_text = f"*{slack_name}* is listening to:"

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
def update_slack_status(track, user_id: str):
    """Update Slack status for a specific user"""
    # Note: This requires a user token for each user, which is more complex
    # For now, only update if SLACK_USER_TOKEN is available and matches the user
    if not SLACK_USER_TOKEN:
        return

    duration_ms = track.get("duration_ms", 0)
    progress_ms = track.get("progress_ms", 0)

    # Compute remaining time
    remaining_ms = max(duration_ms - progress_ms, 0)
    remaining_seconds = remaining_ms // 1000

    # Add a buffer of 10 seconds
    remaining_seconds += 60

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
    # Generate a temporary redirect URL with user_id
    redirect_url = f"https://{SERVER_HOST}/login?user_id={user_id}"

    # Respond with ephemeral message containing the redirect link
    return {
        "response_type": "ephemeral",
        "text": f"Click here to authorize Spotify: <{redirect_url}|Authorize Spotify>",
    }


# ----- Background task to watch tracks -----
async def track_watcher():
    """Watch tracks for all authenticated users"""
    while True:
        # Iterate through all users with tokens
        for user_id in list(user_tokens.keys()):
            try:
                # Refresh token for this user
                refresh_spotify_token(user_id)

                # Get current track for this user
                track = get_current_track(user_id)

                # Check if it's a new track
                if track and track["id"] != user_current_tracks.get(user_id):
                    user_current_tracks[user_id] = track["id"]
                    post_to_slack(track, user_id)  # optional: channel post
                    update_slack_status(track, user_id)  # update Slack profile status
            except Exception as e:
                print(f"Error fetching Spotify track for user {user_id}:", e)

        await asyncio.sleep(30)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(track_watcher())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="error")
