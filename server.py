#!/usr/bin/env python3
"""
Apple Music MCP Server

A small, self-contained MCP server (HTTP JSON-RPC, works with any MCP client
that supports `"type": "http"`, e.g. Claude Code's .mcp.json) that lets an
LLM assistant search Apple Music, "play" a song by emitting a card-marker
tag for your own frontend to render, and manage playlists in a *real* Apple
Music library via MusicKit.

Origin / credit
----------------
The architecture of this server (HTTP JSON-RPC MCP server, card-marker tag
convention for chat frontends, developer-token + user-token split, playlist
history auto-sync) is directly derived from Cheiineeey's netease-music-mcp:
https://github.com/Cheiineeey/netease-music-mcp
We originally ran that project as-is against NetEase Cloud Music. We later
ported it to Apple Music because our user's household prefers Apple Music
over NetEase — the structure, tool shapes, and the [xxx:...] card-marker
idea all trace back to that repo. See README.md for the full story.

Credentials, two layers:
  developer token — signed with an Apple Media Services key (.p8, ES256).
                    Configure via APPLE_TEAM_ID / APPLE_KEY_ID / APPLE_KEY_FILE.
                    Missing config = "keyless" mode: tools return a helpful
                    error instead of crashing.
  Music User Token — obtained client-side via MusicKit (iOS/web) after the
                    user authorizes your app, then stored at
                    DATA_DIR/user_token.json (see README for how to get one).
                    Required for anything that touches the user's library
                    (create/list/add-to playlist, history auto-sync).

Card marker: play_music() returns "[amusic:<id>:<title>:<artist>:<artwork_url>]note"
             — a plain-text convention for a chat frontend to detect and
             render as a rich music card. This server does not render
             anything itself; it's just a text tag your frontend parses.

Play history: every successful play_music() call is appended to a local
             JSONL log and (if a user token is available) also mirrored
             into a real "history" playlist in the user's library — created
             on first use, then appended to (never removed from — the
             Apple Music API has no "remove track from playlist" endpoint,
             so the sync logic is deliberately "better to skip a dup check
             than to fail" — see README's pitfalls section).
"""
import json, os, secrets, time, urllib.request, urllib.parse, http.server
from http.server import HTTPServer
from pathlib import Path

import jwt  # PyJWT + cryptography, ES256


def _load_dotenv(path=".env"):
    """Minimal .env loader so you don't need python-dotenv. Existing
    process env vars always win (setdefault)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()

PORT = int(os.environ.get("PORT", "3458"))

# NOTE: MCP_TOKEN is only ever read here to hand to you for putting in your
# client's URL as `?token=...`. This server does NOT currently verify it on
# incoming requests (same as the original private build) — treat it as a
# convention / placeholder for your own reverse-proxy or future auth check,
# not as real access control. If you need real auth, add a check in
# _handle_message() before it does anything else.
MCP_TOKEN = os.environ.get("MCP_TOKEN") or secrets.token_urlsafe(24)

APPLE_TEAM_ID = os.environ.get("APPLE_TEAM_ID", "")
APPLE_KEY_ID = os.environ.get("APPLE_KEY_ID", "")
APPLE_KEY_FILE = os.environ.get("APPLE_KEY_FILE", "")
# Apple Music storefront. Use "us" for a US subscription, "cn" for a China
# subscription, etc. — must match the Apple ID whose library you're writing to.
STOREFRONT = os.environ.get("APPLE_STOREFRONT", "us")

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_TOKEN_FILE = DATA_DIR / "user_token.json"
LEDGER_FILE = DATA_DIR / "playlists.json"
HISTORY_FILE = DATA_DIR / "play_history.jsonl"
HIST_STATE_FILE = DATA_DIR / "history_state.json"  # sync state for the real history playlist

HISTORY_PLAYLIST_NAME = os.environ.get("HISTORY_PLAYLIST_NAME", "Songs picked for you")
HISTORY_PLAYLIST_DESC = os.environ.get("HISTORY_PLAYLIST_DESC", "Every song picked for you")

API = "https://api.music.apple.com"

_dev_tok_cache = {"tok": None, "exp": 0}


def _key_config():
    if APPLE_KEY_ID and APPLE_KEY_FILE and Path(APPLE_KEY_FILE).exists():
        return {"KEY_ID": APPLE_KEY_ID, "KEY_FILE": APPLE_KEY_FILE}
    return None


def _dev_token():
    """Return a developer token, or None if unconfigured (keyless mode).
    Cached for 11h, signed for 12h."""
    now = time.time()
    if _dev_tok_cache["tok"] and now < _dev_tok_cache["exp"] - 3600:
        return _dev_tok_cache["tok"]
    cfg = _key_config()
    if not cfg:
        return None
    iat = int(now)
    tok = jwt.encode({"iss": APPLE_TEAM_ID, "iat": iat, "exp": iat + 12 * 3600},
                     Path(cfg["KEY_FILE"]).read_text(), algorithm="ES256",
                     headers={"kid": cfg["KEY_ID"]})
    _dev_tok_cache.update(tok=tok, exp=iat + 12 * 3600)
    return tok


def _user_token():
    try:
        return json.loads(USER_TOKEN_FILE.read_text()).get("token")
    except Exception:
        return None


NO_KEY = ("Apple Music isn't configured yet: generate a MusicKit (Media Services) key in the "
          "Apple Developer portal, download the .p8 file, and set APPLE_TEAM_ID / APPLE_KEY_ID / "
          "APPLE_KEY_FILE (see .env.example and README's Quick Start).")
NO_USER = ("Apple Music library access isn't authorized yet: you need a Music User Token from "
           "MusicKit (obtained client-side after the user authorizes your app) written to "
           "DATA_DIR/user_token.json. Search and play_music work without it; playlist read/write "
           "needs it. See README for how to obtain one.")


def _api(path, method="GET", body=None, need_user=False):
    """Call the Apple Music API. Returns (dict, None) or (None, error_message)."""
    dev = _dev_token()
    if not dev:
        return None, NO_KEY
    headers = {"Authorization": f"Bearer {dev}"}
    if need_user:
        ut = _user_token()
        if not ut:
            return None, NO_USER
        headers["Music-User-Token"] = ut
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            if raw[:2] == b"\x1f\x8b":  # Apple's POST responses are gzip'd, GET responses aren't
                import gzip
                raw = gzip.decompress(raw)
            return (json.loads(raw) if raw else {}), None
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, ("Apple Music API 401: developer token was rejected — the key probably "
                          "doesn't have Media Services enabled, or APPLE_TEAM_ID/APPLE_KEY_ID is wrong.")
        if e.code == 403 and need_user:
            return None, "Apple Music API 403: user token expired/invalid, needs re-authorization."
        return None, f"Apple Music API {e.code}: {e.reason}"
    except Exception as e:
        return None, f"Apple Music request failed: {e}"


def _fmt_song(s):
    a = s.get("attributes", {})
    art = (a.get("artwork", {}).get("url") or "").replace("{w}", "600").replace("{h}", "600")
    return {"id": s.get("id"), "name": a.get("name", ""), "artist": a.get("artistName", ""),
            "album": a.get("albumName", ""), "artwork": art}


# ── Play history log + auto-synced "history" playlist ──

def _record_play(s, note):
    """After a successful play_music(), append to the history log and sync
    the real playlist. Any failure here must never break the card reply."""
    try:
        with open(HISTORY_FILE, "a") as f:
            f.write(json.dumps({"id": s["id"], "name": s["name"], "artist": s["artist"],
                                "artwork": s["artwork"], "note": note or "",
                                "ts": int(time.time())}, ensure_ascii=False) + "\n")
    except Exception:
        pass
    try:
        _sync_history_playlist(s)
    except Exception as e:
        print(f"[history-sync] {e}")


def _sync_history_playlist(s):
    """Maintain a real playlist (HISTORY_PLAYLIST_NAME) in the user's Music
    library: created on first play, appended to after that, same track
    never added twice. The Apple Music API has no "remove track" endpoint,
    so this logic is deliberately skip-on-doubt: it would rather miss a
    dedupe than crash or duplicate. Silently no-ops without a user token."""
    if not _user_token():
        return
    try:
        st = json.loads(HIST_STATE_FILE.read_text())
    except Exception:
        st = {"playlist_id": None, "added": []}
    if not st.get("playlist_id"):
        data, err = _api("/v1/me/library/playlists", method="POST",
                         body={"attributes": {"name": HISTORY_PLAYLIST_NAME,
                                              "description": HISTORY_PLAYLIST_DESC}},
                         need_user=True)
        if err:
            print(f"[history-sync] create: {err}")
            return
        st["playlist_id"] = data["data"][0]["id"]
        HIST_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False))
    if s["id"] in st.get("added", []):
        return
    _, err = _api(f"/v1/me/library/playlists/{st['playlist_id']}/tracks", method="POST",
                  body={"data": [{"id": str(s["id"]), "type": "songs"}]}, need_user=True)
    if err:
        print(f"[history-sync] add: {err}")
        return
    st.setdefault("added", []).append(s["id"])
    HIST_STATE_FILE.write_text(json.dumps(st, ensure_ascii=False))


# ── Tools ──

def play_music(query, note=None):
    data, err = _api(f"/v1/catalog/{STOREFRONT}/search?types=songs&limit=3&term="
                     + urllib.parse.quote(query))
    if err:
        return err
    songs = (data.get("results", {}).get("songs", {}) or {}).get("data", [])
    if not songs:
        return f"No results on Apple Music for '{query}', try different keywords"
    s = _fmt_song(songs[0])
    name = s["name"].replace(":", "：")
    artist = s["artist"].replace(":", "：")
    _record_play(s, note)
    return f"[amusic:{s['id']}:{name}:{artist}:{s['artwork']}]{note or ''}"


def search_music(query, limit=10):
    limit = max(1, min(int(limit or 10), 25))
    data, err = _api(f"/v1/catalog/{STOREFRONT}/search?types=songs&limit={limit}&term="
                     + urllib.parse.quote(query))
    if err:
        return err
    songs = (data.get("results", {}).get("songs", {}) or {}).get("data", [])
    if not songs:
        return f"No results for '{query}'"
    return "\n".join(f"[{x['id']}] {x['name']} — {x['artist']} <<{x['album']}>>"
                     for x in (_fmt_song(s) for s in songs))


def _ledger():
    try:
        return json.loads(LEDGER_FILE.read_text())
    except Exception:
        return []


def create_playlist(name, description=None):
    body = {"attributes": {"name": name, "description": description or ""}}
    data, err = _api("/v1/me/library/playlists", method="POST", body=body, need_user=True)
    if err:
        return err
    try:
        pl = data["data"][0]
        pid = pl["id"]
    except Exception:
        return f"Playlist created but response unparsable: {json.dumps(data)[:200]}"
    led = _ledger()
    led.append({"id": pid, "name": name, "description": description or "",
                "ts": int(time.time()), "songs": []})
    LEDGER_FILE.write_text(json.dumps(led, ensure_ascii=False, indent=1))
    return (f"Created real Apple Music playlist '{name}' (id={pid}) in the user's library. "
            f"Use add_to_playlist to fill it.")


def add_to_playlist(playlist_id, song_id, song_name="", artist=""):
    body = {"data": [{"id": str(song_id), "type": "songs"}]}
    _, err = _api(f"/v1/me/library/playlists/{urllib.parse.quote(str(playlist_id))}/tracks",
                  method="POST", body=body, need_user=True)
    if err:
        return err
    led = _ledger()
    for p in led:
        if p["id"] == playlist_id:
            p.setdefault("songs", []).append(
                {"id": str(song_id), "name": song_name, "artist": artist,
                 "ts": int(time.time())})
            LEDGER_FILE.write_text(json.dumps(led, ensure_ascii=False, indent=1))
            break
    return f"Added '{song_name or song_id}' to playlist {playlist_id}"


def list_playlists():
    led = _ledger()
    lines = []
    if led:
        lines.append("== Playlists created via this server ==")
        for p in led:
            lines.append(f"ID:{p['id']} {p['name']} — {p.get('description','')} "
                         f"({len(p.get('songs',[]))} songs)")
    data, err = _api("/v1/me/library/playlists?limit=25", need_user=True)
    if err:
        return "\n".join(lines) if lines else err
    known = {p["id"] for p in led}
    others = [d for d in data.get("data", []) if d.get("id") not in known]
    if others:
        lines.append("== Other playlists in the library ==")
        for d in others:
            lines.append(f"ID:{d.get('id')} {d.get('attributes',{}).get('name','')}")
    return "\n".join(lines) or "No playlists yet"


TOOLS = [
    {"name": "play_music",
     "description": "Search and play a song from Apple Music (the user's real subscription, "
                    "APPLE_STOREFRONT storefront). The returned [amusic:...] tag is a card-marker "
                    "convention for your chat frontend to render as a rich music card - do NOT "
                    "paste it back into a message as visible text.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "Search query (song name, artist, etc.)"},
         "note": {"type": "string", "description": "Optional note to display with the music card"}},
         "required": ["query"]}},
    {"name": "search_music",
     "description": "Search Apple Music catalog, returns lines of [song_id] title — artist. "
                    "Use the song_id with add_to_playlist, or the title with play_music.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"},
         "limit": {"type": "integer", "description": "1-25, default 10"}},
         "required": ["query"]}},
    {"name": "create_playlist",
     "description": "Create a REAL playlist in the user's Apple Music library (shows up in their "
                    "Music app). Returns the playlist id.",
     "inputSchema": {"type": "object", "properties": {
         "name": {"type": "string"},
         "description": {"type": "string", "description": "Why this playlist / what it's for"}},
         "required": ["name"]}},
    {"name": "add_to_playlist",
     "description": "Add a song (catalog song_id from search_music/play_music card) to one of "
                    "the user's Apple Music library playlists created via create_playlist.",
     "inputSchema": {"type": "object", "properties": {
         "playlist_id": {"type": "string"},
         "song_id": {"type": "string"},
         "song_name": {"type": "string"}, "artist": {"type": "string"}},
         "required": ["playlist_id", "song_id"]}},
    {"name": "list_playlists",
     "description": "List playlists created via this server (with song counts) plus other "
                    "playlists already in the user's library.",
     "inputSchema": {"type": "object", "properties": {}}},
]

DISPATCH = {
    "play_music": lambda a: play_music(a.get("query", ""), a.get("note")),
    "search_music": lambda a: search_music(a.get("query", ""), a.get("limit", 10)),
    "create_playlist": lambda a: create_playlist(a.get("name", ""), a.get("description")),
    "add_to_playlist": lambda a: add_to_playlist(a.get("playlist_id"), a.get("song_id"),
                                                 a.get("song_name", ""), a.get("artist", "")),
    "list_playlists": lambda a: list_playlists(),
}


class MCPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            keyed = _key_config() is not None
            self._json({"status": "ok", "dev_key": keyed, "user_token": bool(_user_token())})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/message" or self.path.startswith("/message?"):
            self._handle_message()
        else:
            self.send_error(404)

    def _json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _handle_message(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        method = body.get("method", "")
        if method == "initialize":
            self._json({"jsonrpc": "2.0", "id": body.get("id"),
                        "result": {"protocolVersion": "2024-11-05",
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "applemusic-mcp", "version": "1.0.0"}}})
        elif method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = body.get("params", {}).get("name", "")
            args = body.get("params", {}).get("arguments", {})
            fn = DISPATCH.get(name)
            try:
                text = fn(args) if fn else f"Unknown tool: {name}"
            except Exception as e:
                text = f"Tool {name} crashed: {e}"
            self._json({"jsonrpc": "2.0", "id": body.get("id"),
                        "result": {"content": [{"type": "text", "text": text}]}})
        elif method == "notifications/initialized":
            self._json({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})
        else:
            self._json({"jsonrpc": "2.0", "id": body.get("id"),
                        "error": {"code": -32601, "message": f"Unknown method: {method}"}})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    keyed = _key_config() is not None
    print(f"Apple Music MCP on {PORT} (dev_key={'ok' if keyed else 'MISSING - keyless mode'})")
    if not os.environ.get("MCP_TOKEN"):
        print(f"MCP_TOKEN not set, generated one for this run: {MCP_TOKEN}")
        print("Set MCP_TOKEN in your environment/.env to keep it stable across restarts.")
    HTTPServer(("127.0.0.1", PORT), MCPHandler).serve_forever()
