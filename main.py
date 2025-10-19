# lyrics_webapp.py
# deps:
#   pip install flask spotipy requests pypinyin argostranslate opencc-python-reimplemented

import re
import time
import threading
import requests
from dataclasses import dataclass
from typing import List, Optional
from flask import Flask, jsonify, Response
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from pypinyin import lazy_pinyin, Style

import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# NEW: offline CN->EN translation + (optional) trad->simp normalization
import argostranslate.translate as ar_translate
from opencc import OpenCC
from dotenv import load_dotenv
import os

load_dotenv()
# ───────── CONFIG ─────────
SCOPES    = "user-read-playback-state user-read-currently-playing"
REDIRECT  = "http://127.0.0.1:8888/callback"   # must match your Spotify app
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")

POLL_SEC = 2.0
LRCLIB_TIMEOUT = 10
ADD_TRANSLATION = True   # ✅ turn on translations
# ──────────────────────────

app = Flask(__name__)

@dataclass
class LrcLine:
    t: float
    text: str
    pinyin: str = ""
    trans: str = ""

@dataclass
class TrackState:
    track_id: Optional[str] = None
    title: str = ""
    artists: str = ""
    duration_ms: int = 0
    progress_ms: int = 0
    is_playing: bool = False
    lrc_lines: List[LrcLine] = None
    plain_lyrics: str = ""
    last_error: str = ""

state = TrackState(lrc_lines=[])

# ───────── spotify auth ─────────
sp = None
def make_sp_client() -> spotipy.Spotify:
    auth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT,
        scope=SCOPES,
        open_browser=True
    )
    client = spotipy.Spotify(auth_manager=auth)
    me = client.current_user()
    print(f"[oauth] logged in as: {me['id']} - {me.get('display_name')}")
    return client

def ensure_sp():
    global sp
    if sp is None:
        print("[init] creating spotify client…")
        sp = make_sp_client()

# ───────── lyrics helpers ─────────
LRC_TIME = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,2}))?\]")

def normalize_title(title: str) -> str:
    t = title
    t = re.sub(r"\s*-\s*(remaster(ed)?\s*\d{2,4}|single version|album version|radio edit|clean|explicit)\b.*", "", t, flags=re.I)
    t = re.sub(r"\s*\(feat\..*?\)", "", t, flags=re.I)
    t = re.sub(r"\s*\[.*?version.*?\]", "", t, flags=re.I)
    return t.strip()

def primary_artist(artists_csv: str) -> str:
    return artists_csv.split(",")[0].strip()

def fetch_lrclib(artist: str, title: str, duration_sec: Optional[int]):
    base = "https://lrclib.net/api"
    params = {"artist_name": artist, "track_name": title}
    if duration_sec:
        params["duration"] = duration_sec
    r = requests.get(f"{base}/get", params=params, timeout=LRCLIB_TIMEOUT)
    if r.status_code == 404:
        r = requests.get(f"{base}/search", params={"track_name": title, "artist_name": artist}, timeout=LRCLIB_TIMEOUT)
        if r.status_code != 200 or not r.json():
            return None
        best = r.json()[0]
        r = requests.get(f"{base}/get", params={"id": best["id"]}, timeout=LRCLIB_TIMEOUT)
    if r.status_code != 200:
        return None
    return r.json()

def is_cjk(s: str) -> bool:
    return any('\u4e00' <= ch <= '\u9fff' for ch in s)

# Optional Traditional -> Simplified (improves pinyin/MT consistency for zh-TW lyrics)
_opencc = OpenCC("t2s")

def to_pinyin(line: str) -> str:
    if not line or not is_cjk(line):
        return ""
    # convert to simplified for more consistent pinyin
    simp = _opencc.convert(line)
    py = lazy_pinyin(simp, style=Style.TONE, neutral_tone_with_five=True)
    return " ".join(tok for tok in py if tok.strip())

# ───────── Argos Translate (offline) ─────────
def get_argos_zh_en():
    """
    Return Argos translator zh->en if the model is installed, else None.
    Install once:
      python -c "import argostranslate.package as p; \
                 pkg=[x for x in p.get_available_packages() if x.from_code=='zh' and x.to_code=='en'][0]; \
                 p.install_from_path(pkg.download())"
    """
    try:
        return ar_translate.get_translation_from_codes("zh", "en")
    except Exception:
        return None

def batch_translate(lines: list[str]) -> dict[str, str]:
    """Offline translation using Argos; returns {original_line: english}."""
    uniq = [s for s in dict.fromkeys(lines) if is_cjk(s)]
    if not uniq:
        return {}
    translator = get_argos_zh_en()
    out = {}
    if translator is None:
        # If model not installed, leave translations empty; UI still renders.
        for s in uniq:
            out[s] = ""
        return out
    for s in uniq:
        try:
            # normalize to simplified for slightly better MT
            simp = _opencc.convert(s)
            out[s] = translator.translate(simp) or ""
        except Exception:
            out[s] = ""
    return out

def parse_lrc(text: str) -> List[LrcLine]:
    if not text:
        return []
    lines: List[LrcLine] = []
    for raw in text.splitlines():
        times = list(LRC_TIME.finditer(raw))
        lyric = LRC_TIME.sub("", raw).strip()
        for m in times:
            mm, ss = int(m.group(1) or 0), int(m.group(2) or 0)
            frac = m.group(3) or ""
            cs = int(frac) if frac else 0
            denom = 10 if len(frac) == 1 else 100
            t = mm * 60 + ss + (cs / denom if denom else 0)
            if lyric:
                lines.append(LrcLine(t=t, text=lyric))
    lines.sort(key=lambda x: x.t)
    return lines

def enrich_with_pinyin_and_trans(lines: List[LrcLine]) -> List[LrcLine]:
    # Always compute pinyin (offline)
    for ln in lines:
        ln.pinyin = to_pinyin(ln.text) or ""

    # Offline Argos translation per unique line
    if ADD_TRANSLATION:
        mapping = batch_translate([ln.text for ln in lines])
        for ln in lines:
            ln.trans = mapping.get(ln.text, "") or ""
    else:
        for ln in lines:
            ln.trans = ""
    return lines

# ───────── state refresh ─────────
def refresh_state():
    global state
    try:
        ensure_sp()
        pb = sp.current_playback()
    except Exception as e:
        state.last_error = f"spotify error: {e}"
        return

    if not pb or pb.get("currently_playing_type") != "track":
        state.is_playing = False
        return

    item = pb.get("item")
    if not item:
        state.is_playing = False
        return

    tid = item["id"]
    changed = (tid != state.track_id)

    state.track_id = tid
    state.title = item.get("name", "")
    state.artists = ", ".join(a["name"] for a in item.get("artists", []))
    state.duration_ms = item.get("duration_ms", 0)
    state.progress_ms = pb.get("progress_ms", 0)
    state.is_playing = pb.get("is_playing", False)
    state.last_error = ""

    if changed:
        print(f"[track] {state.artists} — {state.title}")
        title_q = normalize_title(state.title)
        artist_q = primary_artist(state.artists)
        dur_sec = int((state.duration_ms or 0) / 1000)

        data = (fetch_lrclib(artist_q, title_q, dur_sec)
                or fetch_lrclib(artist_q, title_q, None)
                or fetch_lrclib(state.artists, title_q, dur_sec))

        if data:
            state.plain_lyrics = data.get("plainLyrics") or ""
            synced = data.get("syncedLyrics") or ""
            if synced.strip():
                parsed = parse_lrc(synced)
            else:
                # Fallback: build lines from plain lyrics so we still show pinyin/translation
                parsed = [LrcLine(t=i, text=line.strip())
                          for i, line in enumerate((state.plain_lyrics or "").splitlines())
                          if line.strip()]

            state.lrc_lines = enrich_with_pinyin_and_trans(parsed)

            # Helpful message if Argos not installed and we had CJK lines
            if ADD_TRANSLATION and all((ln.trans == "" for ln in state.lrc_lines)) \
               and any(is_cjk(ln.text) for ln in state.lrc_lines):
                state.last_error = (state.last_error or "") + \
                    " | Argos zh→en model not installed. See code comment for install snippet."

            sample = state.lrc_lines[0].pinyin if state.lrc_lines else "(none)"
            print(f"[lyrics] lines={len(state.lrc_lines)}; sample pinyin: {sample}")
        else:
            state.plain_lyrics = ""
            state.lrc_lines = []
            print("[lyrics] not found")

def poller():
    while True:
        try:
            refresh_state()
        except Exception as e:
            state.last_error = f"poller error: {e}"
        time.sleep(POLL_SEC)

threading.Thread(target=poller, daemon=True).start()

# ───────── web api ─────────
@app.route("/api/state")
def api_state():
    refresh_state()  # ensure fresh on first render
    payload = {
        "track_id": state.track_id,
        "title": state.title,
        "artists": state.artists,
        "duration_ms": state.duration_ms,
        "progress_ms": state.progress_ms,
        "is_playing": state.is_playing,
        "plain_lyrics": state.plain_lyrics,
        "lrc": [{"t": ln.t, "text": ln.text, "pinyin": ln.pinyin, "trans": ln.trans} for ln in (state.lrc_lines or [])],
        "error": state.last_error,
    }
    return jsonify(payload)

@app.route("/")
def index():
  html = """
<!doctype html>
<html>
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Synced Lyrics (hanzi + pinyin + translation)</title>
<style>
body {
  font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
  background:#0b0b0c;
  color:#e9e9ea;
  margin:0;
  height:100vh;
  overflow:hidden; /* ✅ disable manual scrolling entirely */
}

header {
  position: sticky;
  top: 0;
  z-index: 10;
  background:#0b0b0c;
  padding:16px 20px;
  border-bottom:1px solid #1f1f22;
}
.title { 
  font-size: 28px;  /* increase from 18px to 28px */
  font-weight: 700; /* make it a bit bolder if you want */
}

.subtitle { 
  font-size: 18px;  /* increase from 14px to 18px */
  opacity: 0.8;     /* make it slightly brighter */
  margin-top: 6px; 
}
.err { color:#ff7070; font-size:14px; margin-top:8px; }

/* lyrics scroll area */
#lyrics {
  position: relative;
  max-width:1100px;
  margin:0 auto;
  padding:20px;
  height:calc(100vh - 68px);
  overflow:auto; /* ✅ allow auto-scroll only */
  -ms-overflow-style:none;
  scrollbar-width:none;
}
#lyrics::-webkit-scrollbar { display:none; }

/* lyric lines */
.row {
  display: grid;
  grid-template-columns: minmax(260px, 42%) 1fr;
  column-gap: 24px;
  align-items: start;
  padding: 14px 0;
  border-bottom: 1px dashed #1f1f22;
  opacity: 0.45;
  transition: opacity .25s, transform .25s;
}
.row.active {
  opacity: 1;
  transform: none;
  background: transparent;
}
.row.active .hanzi,
.row.active .pinyin,
.row.active .trans {
  color: #fff;
  text-shadow: 0 0 6px #fff3;
}

.hanzi {
  font-size: 32px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.right-side {
  display: grid;
  grid-auto-rows: min-content;
  row-gap: 6px;
}

.pinyin, .trans {
  font-size: 18px;
  opacity: 0.9;
  margin: 0;
}

.fallback {
  opacity:0.8;
  white-space:pre-wrap;
}

@media (max-width: 900px){
  .row {
    grid-template-columns: 1fr;
    row-gap: 8px;
  }
}
</style>
</head>
<body>
<header>
  <div class="title" id="song">loading…</div>
  <div class="subtitle" id="meta"></div>
  <div class="err" id="err"></div>
</header>

<div id="lyrics"></div>

<script>
let lrc = [];
let startedAt = 0;
let baseProgress = 0;
let lastProgress = 0;
let currentIdx = -1;
let lastLyricsSig = "";

function setSong(title, artists){
  document.getElementById('song').textContent = title || "no track";
  document.getElementById('meta').textContent = artists || "";
}
function setErr(msg){
  document.getElementById('err').textContent = msg || "";
}
function lyricsSignature(arr){
  if (!arr || !arr.length) return "0";
  return arr.length + "|" + arr[0].text + "|" + arr[arr.length-1].text;
}

function renderLyrics(){
  const box = document.getElementById('lyrics');
  if(!lrc.length){
    box.innerHTML = '<div class="fallback">no synced lyrics found. if plain lyrics exist, they will show below.</div>';
    return;
  }
  box.innerHTML = lrc.map((ln, i) => `
    <div class="row" id="r${i}">
      <div class="hanzi">${ln.text}</div>
      <div class="right-side">
        <div class="pinyin">${ln.pinyin || ""}</div>
        <div class="trans">${ln.trans ?? ""}</div>
      </div>
    </div>
  `).join("");
  currentIdx = -1;
}

function highlightLoop(){
  const now = Date.now();
  const elapsed = Math.max(0, now - startedAt);
  const posSec = (baseProgress + elapsed) / 1000.0;

  let lo = 0, hi = lrc.length - 1, cand = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (lrc[mid].t <= posSec) { cand = mid; lo = mid + 1; }
    else { hi = mid - 1; }
  }
  if (cand < 0) cand = 0;

  if (currentIdx === -1) {
    currentIdx = cand;
  } else if (cand > currentIdx) {
    const curT = lrc[currentIdx].t;
    const nextT = (currentIdx + 1 < lrc.length) ? lrc[currentIdx + 1].t : 1e9;
    const threshold = curT + 0.4 * (nextT - curT);
    if (posSec >= threshold) currentIdx = cand;
  } else if (cand < currentIdx) {
    currentIdx = cand;
  }

  for (let i = 0; i < lrc.length; i++) {
    const el = document.getElementById('r' + i);
    if (!el) continue;
    if (i === currentIdx) {
      el.classList.add('active');
      el.scrollIntoView({ behavior: 'smooth', block: 'center' }); /* ✅ auto-scroll still works */
    } else {
      el.classList.remove('active');
    }
  }
  requestAnimationFrame(highlightLoop);
}

async function fetchState(){
  try{
    const r = await fetch('/api/state');
    const j = await r.json();
    setSong(j.title, j.artists);
    setErr(j.error || "");

    if (typeof j.progress_ms === "number") {
      if (j.progress_ms >= lastProgress) {
        baseProgress = j.progress_ms;
        startedAt = Date.now();
      }
      lastProgress = j.progress_ms;
    }

    if (j.lrc && Array.isArray(j.lrc) && j.lrc.length) {
      const sig = lyricsSignature(j.lrc);
      if (sig !== lastLyricsSig) {
        lrc = j.lrc;
        lastLyricsSig = sig;
        renderLyrics();
      }
    } else if (j.plain_lyrics) {
      const box = document.getElementById('lyrics');
      box.innerHTML = '<pre class="fallback">'+
        j.plain_lyrics.replace(/[<>]/g, s=>({'<':'&lt;','>':'&gt;'}[s]))+
        '</pre>';
      lrc = [];
      lastLyricsSig = "0";
      currentIdx = -1;
    }
  } catch(e){
    setErr("frontend error: " + e);
  }
}

// disable all manual scroll inputs but allow auto-scroll
document.addEventListener('wheel', e => e.preventDefault(), { passive: false });
document.addEventListener('touchmove', e => e.preventDefault(), { passive: false });
document.addEventListener('keydown', e => {
  const blocked = ['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '];
  if (blocked.includes(e.key)) e.preventDefault();
});

setInterval(fetchState, 2000);
fetchState().then(()=>{ renderLyrics(); highlightLoop(); });
</script>
</body>
</html>
"""

  return Response(html, mimetype="text/html")

# ───────── main ─────────
if __name__ == "__main__":
    ensure_sp()
    print("[start] open http://127.0.0.1:5000 in your browser")
    app.run(host="127.0.0.1", port=5000, debug=False)
