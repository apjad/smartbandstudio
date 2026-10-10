#!/usr/bin/env python3
"""Local web editor for songs.json — no external dependencies, stdlib only.

Serves a small UI to add/edit/delete songs (chords, bar counts, part
sequence), plus a "Gem og synkroniser" action that commits and pushes
songs.json to GitHub. Same pattern as the Madbank/3udget/Sub3udget editors.

Every mutation is scoped to a single song (POST to add, PUT/DELETE by title)
and is applied against whatever is on disk *at request time* — never a
whole-list overwrite from a possibly-stale browser snapshot, for the same
reason the Madbank editor does this (two people editing at once must not
silently wipe each other's new songs).

Protected with HTTP Basic Auth so it's safe to port-forward — credentials
come from smartbandstudio-editor-credentials.local (sibling of this repo,
one level up from agentclaude/smartbandstudio-pages).
"""
import base64
import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONGS_PATH = os.path.join(REPO_DIR, "songs.json")
CREDS_PATH = os.path.expanduser("~/agentclaude/smartbandstudio-editor-credentials.local")
GROK_BIN = os.path.expanduser("~/.grok/bin/grok")
PORT = 8421
MAX_PARTS = 5
MAX_BARS = 48
# Same range the app's SongImportPlan accepts (RemoteSongCatalog.swift) —
# a song outside it is silently skipped by the app's import.
MIN_TEMPO, MAX_TEMPO = 40, 240
GROK_TIMEOUT = 300

# Mirrors ChordParser.swift (international notation, as the app's songbank
# import uses), so a chart the app cannot read is rejected here instead of
# silently disappearing from the app's import list.
_NOTE = r"[A-Ha-h](?:#|♯|b|♭)?"
_QUALITIES = {
    "", "maj", "major", "M", "m", "min", "mi", "-", "minor", "7", "dom7",
    "maj7", "M7", "Δ", "Δ7", "ma7", "j7", "m7", "min7", "mi7", "-7",
    "dim", "°", "o", "aug", "+", "sus2", "sus4", "sus", "add9", "add2",
}
_LOWER_QUALITIES = {q.lower() for q in _QUALITIES}


def chord_is_valid(token):
    m = re.fullmatch(rf"({_NOTE})(.*?)(?:/({_NOTE}))?", token)
    if not m or m.group(1)[0].upper() not in "ABCDEFGH":
        return False
    quality = m.group(2).strip()
    return quality in _QUALITIES or quality.lower() in _LOWER_QUALITIES


def parse_bar_count(chords):
    """Number of bars the app will read from `chords`, or raises ValueError
    naming the first chord it would reject (same rules as ChordParser.parseBars)."""
    lines = []
    for line in chords.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if "|" not in line:
            tokens = [t for t in re.split(r"[ ,\t]+", line) if t]
            if len(tokens) > 1:
                line = "".join(f"| {t} " for t in tokens) + "|"
        lines.append(line)
    bars = 0
    for raw in "|".join(lines).split("|"):
        raw = raw.strip()
        if not raw:
            continue
        if raw != "%":
            for token in (t for t in re.split(r"[ ,\t]+", raw) if t):
                if token not in (".", "-", "/") and not chord_is_valid(token):
                    raise ValueError(f'appen kan ikke læse akkorden "{token}"')
        bars += 1
    if bars == 0:
        raise ValueError("ingen takter fundet")
    return bars

FILE_LOCK = threading.Lock()
JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(work):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"done": False}

    def run():
        try:
            result = work()
        except Exception as e:  # never leave the browser polling forever
            result = {"ok": False, "error": f"Uventet fejl: {e}"}
        with JOBS_LOCK:
            JOBS[job_id] = {"done": True, "result": result}

    threading.Thread(target=run, daemon=True).start()
    return job_id


def load_credentials():
    """One 'username:password' per line; '#'-prefixed and blank lines are ignored."""
    users = {}
    try:
        with open(CREDS_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                username, sep, password = line.partition(":")
                if sep and username and password:
                    users[username] = password
    except FileNotFoundError:
        pass
    if not users:
        sys.exit(f"Mangler login i {CREDS_PATH} — kan ikke starte serveren uden.")
    return users


AUTH_USERS = load_credentials()
INDEX_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")


def clean_part(part, index_label):
    if not isinstance(part, dict):
        raise ValueError(f"{index_label}: en del er ikke et gyldigt objekt")
    label = str(part.get("label", "")).strip()
    if not label:
        raise ValueError(f"{index_label}: en del mangler et navn")
    try:
        bar_count = int(part.get("barCount", 0))
    except (TypeError, ValueError):
        raise ValueError(f"{index_label}: \"{label}\" har et ugyldigt antal bars")
    if not (1 <= bar_count <= MAX_BARS):
        raise ValueError(f"{index_label}: \"{label}\" skal have 1-{MAX_BARS} bars")
    chords = str(part.get("chords", "")).strip()
    if not chords:
        raise ValueError(f"{index_label}: \"{label}\" mangler akkorder")
    try:
        bars = parse_bar_count(chords)
    except ValueError as e:
        raise ValueError(f"{index_label}: \"{label}\" — {e}")
    if bars > bar_count:
        raise ValueError(
            f"{index_label}: \"{label}\" har {bars} takter akkorder men kun {bar_count} bars — ret antallet")
    return {"label": label, "barCount": bar_count, "chords": chords}


def clean_song(song, index_label):
    if not isinstance(song, dict):
        raise ValueError(f"{index_label} er ikke et gyldigt objekt")
    title = str(song.get("title", "")).strip()
    if not title:
        raise ValueError(f"{index_label} mangler en titel")
    artist = str(song.get("artist", "")).strip()
    try:
        tempo = float(song.get("tempoBPM", 120))
    except (TypeError, ValueError):
        raise ValueError(f"\"{title}\" har et ugyldigt tempo")
    if not (MIN_TEMPO <= tempo <= MAX_TEMPO):
        raise ValueError(f"\"{title}\" skal have et tempo mellem {MIN_TEMPO} og {MAX_TEMPO} BPM")
    try:
        beats_per_bar = int(song.get("beatsPerBar", 4))
    except (TypeError, ValueError):
        raise ValueError(f"\"{title}\" har en ugyldig taktart")
    if not (1 <= beats_per_bar <= 12):
        raise ValueError(f"\"{title}\" skal have 1-12 slag pr. takt")
    raw_parts = song.get("parts", [])
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError(f"\"{title}\" mangler mindst én del")
    if len(raw_parts) > MAX_PARTS:
        raise ValueError(f"\"{title}\" har {len(raw_parts)} dele — appen har kun {MAX_PARTS} part-slots")
    parts = [clean_part(p, f'"{title}"') for p in raw_parts]
    labels = [p["label"] for p in parts]
    if len(set(label.lower() for label in labels)) != len(labels):
        raise ValueError(f"\"{title}\" har to dele med samme navn")
    sequence = [str(s).strip() for s in song.get("sequence", []) if str(s).strip()]
    label_set = {label.lower() for label in labels}
    for step in sequence:
        if step.lower() not in label_set:
            raise ValueError(f"\"{title}\": sequence nævner \"{step}\", som ikke findes i parts")
    return {
        "title": title, "artist": artist, "tempoBPM": tempo, "beatsPerBar": beats_per_bar,
        "parts": parts, "sequence": sequence,
    }


def load_songs_unlocked():
    with open(SONGS_PATH, encoding="utf-8") as f:
        return json.load(f)["songs"]


def save_songs_unlocked(songs):
    with open(SONGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"songs": songs}, f, indent=2, ensure_ascii=False)
        f.write("\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "SongbankEditor/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _check_auth(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
        except Exception:
            return False
        return AUTH_USERS.get(user) == password

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Songbank"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw)

    def _song_title_from_path(self, prefix):
        if not self.path.startswith(prefix):
            return None
        return urllib.parse.unquote(self.path[len(prefix):])

    def do_GET(self):
        if not self._check_auth():
            return self._require_auth()
        if self.path == "/":
            with open(INDEX_HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/jobs/"):
            with JOBS_LOCK:
                job = JOBS.get(self.path[len("/api/jobs/"):])
                if job and job["done"]:
                    JOBS.pop(self.path[len("/api/jobs/"):], None)
            if job is None:
                return self._send_json(404, {"error": "Ukendt job — prøv igen"})
            self._send_json(200, job)
        elif self.path == "/api/songs":
            with FILE_LOCK:
                songs = load_songs_unlocked()
            self._send_json(200, {"songs": songs})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if not self._check_auth():
            return self._require_auth()
        if self.path == "/api/songs":
            return self._add_song()
        if self.path == "/api/sync":
            return self._send_json(200, self._run_sync())
        if self.path == "/api/suggest-song":
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                body = {}
            title = str(body.get("title", "")).strip()
            artist = str(body.get("artist", "")).strip()
            if not title:
                return self._send_json(400, {"error": "Mangler titel på sangen"})
            return self._send_json(200, {"job": start_job(lambda: self._suggest_song(title, artist))})
        if self.path == "/api/suggest-song-from-url":
            try:
                url = self._read_json_body().get("url", "").strip()
            except json.JSONDecodeError:
                url = ""
            if not url:
                return self._send_json(400, {"error": "Mangler link"})
            return self._send_json(200, {"job": start_job(lambda: self._suggest_song_from_url(url))})
        self.send_response(404)
        self.end_headers()

    def do_PUT(self):
        if not self._check_auth():
            return self._require_auth()
        original_title = self._song_title_from_path("/api/songs/")
        if original_title is None:
            self.send_response(404)
            self.end_headers()
            return
        self._edit_song(original_title)

    def do_DELETE(self):
        if not self._check_auth():
            return self._require_auth()
        original_title = self._song_title_from_path("/api/songs/")
        if original_title is None:
            self.send_response(404)
            self.end_headers()
            return
        self._delete_song(original_title)

    def _add_song(self):
        try:
            new_song = clean_song(self._read_json_body(), "Sangen")
        except (json.JSONDecodeError, ValueError) as e:
            return self._send_json(400, {"error": str(e)})
        with FILE_LOCK:
            songs = load_songs_unlocked()
            if any(s["title"].lower() == new_song["title"].lower() for s in songs):
                return self._send_json(409, {"error": f'"{new_song["title"]}" findes allerede'})
            songs.append(new_song)
            save_songs_unlocked(songs)
        self._send_json(200, {"ok": True, "songs": songs, "sync": self._run_sync()})

    def _edit_song(self, original_title):
        try:
            updated = clean_song(self._read_json_body(), "Sangen")
        except (json.JSONDecodeError, ValueError) as e:
            return self._send_json(400, {"error": str(e)})
        with FILE_LOCK:
            songs = load_songs_unlocked()
            index = next((i for i, s in enumerate(songs) if s["title"].lower() == original_title.lower()), None)
            if index is None:
                return self._send_json(404, {
                    "error": f'"{original_title}" findes ikke længere — nogen har nok allerede ændret den. Genindlæs listen.'
                })
            renamed = updated["title"].lower() != original_title.lower()
            if renamed and any(i != index and s["title"].lower() == updated["title"].lower() for i, s in enumerate(songs)):
                return self._send_json(409, {"error": f'"{updated["title"]}" findes allerede'})
            songs[index] = updated
            save_songs_unlocked(songs)
        self._send_json(200, {"ok": True, "songs": songs, "sync": self._run_sync()})

    def _delete_song(self, original_title):
        with FILE_LOCK:
            songs = load_songs_unlocked()
            filtered = [s for s in songs if s["title"].lower() != original_title.lower()]
            if len(filtered) != len(songs):
                save_songs_unlocked(filtered)
            songs = filtered
        self._send_json(200, {"ok": True, "songs": songs, "sync": self._run_sync()})

    _SCHEMA = json.dumps({
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "artist": {"type": "string"},
            "tempoBPM": {"type": "number"},
            "beatsPerBar": {"type": "integer"},
            "parts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "barCount": {"type": "integer"},
                        "chords": {"type": "string"},
                    },
                    "required": ["label", "barCount", "chords"],
                },
            },
            "sequence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "artist", "tempoBPM", "beatsPerBar", "parts", "sequence"],
    })

    _RULES = (
        "Rules for the chord chart you produce: "
        "- At most 5 parts (label each distinctly, e.g. Intro/Verse/Chorus/Bridge/Outro — a "
        "repeated section is one part referenced again in `sequence`, not a near-duplicate part). "
        "- `chords` is lead-sheet text, bars separated by `|`, e.g. `| C | Am | F G |` (multiple "
        "chords in one bar split it evenly). Use `.` to hold the previous chord in a slot, `%` to "
        "repeat the previous bar. Use simple chord symbols the app can parse: a root (C D E F G A "
        "B, # or b), then nothing/maj/m/7/maj7/m7/dim/aug/sus2/sus4/add9, optionally a slash bass "
        "like `D/F#`. Avoid symbols like 9, 13, m7b5 — substitute the nearest simple chord. "
        "- `barCount` for a part must equal how many bars its `chords` text actually has. "
        "- `sequence` is the real play order (part labels, each may repeat), every entry must be "
        "one of the `parts` labels. "
        "- Use the song's real, actual chord progression — not a guess — the most common/well-"
        "known version if there's ambiguity."
    )

    def _suggest_song(self, title, artist):
        who = f' by {artist}' if artist else ''
        prompt = (
            f'Look up the real chord chart for the song "{title}"{who}. ' + self._RULES
        )
        # Web search stays on: getting a real song's actual chords right benefits
        # from an actual lookup, unlike Madbank's generic-ingredient guesses.
        return self._ask_grok(prompt, web_search=True)

    def _suggest_song_from_url(self, url):
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
            })
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
        except Exception as e:
            return {"ok": False, "error": f"Kunne ikke hente siden: {e}"}

        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        text = text[:20000]

        prompt = (
            "Extract the chord chart from this webpage (raw HTML/text below), which likely shows "
            "chords over lyrics or a tab-style chord sheet. " + self._RULES + "\n\n" + text
        )
        return self._ask_grok(prompt, web_search=False)

    @staticmethod
    def _grok_song(prompt, web_search):
        """Runs grok and returns (song dict or None, error text or None)."""
        args = [GROK_BIN, "-p", prompt, "--json-schema", Handler._SCHEMA]
        if not web_search:
            args.append("--disable-web-search")
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=GROK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return None, f"AI svarede ikke inden for {GROK_TIMEOUT} sekunder"
        except FileNotFoundError as e:
            return None, str(e)
        if result.returncode != 0:
            return None, result.stderr.strip() or "grok fejlede"
        try:
            reply = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None, "Kunne ikke aflæse svar fra AI"
        if isinstance(reply.get("structuredOutput"), dict):
            return reply["structuredOutput"], None
        # With web search on, grok often writes several JSON objects back to back
        # (a first draft, then a corrected one) and its own structured-output
        # check rejects the whole reply. Use the last complete song object.
        text, decoder, found, i = reply.get("text") or "", json.JSONDecoder(), None, 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                break
            try:
                obj, end = decoder.raw_decode(text, j)
            except json.JSONDecodeError:
                i = j + 1
                continue
            if isinstance(obj, dict) and isinstance(obj.get("parts"), list):
                found = obj
            i = end
        if found is None:
            return None, "Kunne ikke aflæse svar fra AI"
        return found, None

    @staticmethod
    def _fix_bar_counts(song):
        """AI answers often miscount bars; the chord text is what the app plays."""
        for part in song.get("parts") or []:
            if isinstance(part, dict):
                try:
                    part["barCount"] = parse_bar_count(str(part.get("chords", "")))
                except ValueError:
                    pass
        return song

    def _ask_grok(self, prompt, web_search):
        song, error = self._grok_song(prompt, web_search)
        if song is None:
            return {"ok": False, "error": error}
        try:
            return {"ok": True, "song": clean_song(self._fix_bar_counts(song), "Sangen")}
        except ValueError as e:
            first_error = str(e)
        # One repair round, without web search (the content is already here).
        repair = (
            "This chord chart JSON breaks a rule: " + first_error + ". Fix it and return the "
            "corrected chart. " + self._RULES + "\n\n" + json.dumps(song, ensure_ascii=False)
        )
        fixed, error = self._grok_song(repair, web_search=False)
        if fixed is None:
            return {"ok": False, "error": f"AI-svaret var ugyldigt: {first_error}"}
        try:
            return {"ok": True, "song": clean_song(self._fix_bar_counts(fixed), "Sangen")}
        except ValueError as e:
            return {"ok": False, "error": f"AI-svaret var ugyldigt: {e}"}

    def _run_sync(self):
        def run(*args):
            return subprocess.run(
                args, cwd=REPO_DIR, capture_output=True, text=True, timeout=30
            )

        with FILE_LOCK:
            # Commit first: pulling with an uncommitted songs.json fails as soon
            # as GitHub has a newer songs.json than this clone.
            status = run("git", "status", "--porcelain", "songs.json")
            if status.stdout.strip():
                run("git", "add", "songs.json")
                commit = run("git", "commit", "-q", "-m", "Opdater songs.json via web-editor")
                if commit.returncode != 0:
                    return {"ok": False, "step": "commit", "log": commit.stderr}

            pull = run("git", "pull", "--rebase", "--autostash", "origin", "main", "--quiet")
            if pull.returncode != 0:
                run("git", "rebase", "--abort")
                return {"ok": False, "step": "pull", "log": pull.stderr}

            ahead = run("git", "rev-list", "--count", "origin/main..HEAD")
            if ahead.stdout.strip() == "0":
                return {"ok": True, "changed": False, "log": "Ingen ændringer at synkronisere."}

            push = run("git", "push", "origin", "main", "--quiet")
            if push.returncode != 0:
                return {"ok": False, "step": "push", "log": push.stderr}

        return {"ok": True, "changed": True, "log": "Sendt til GitHub — live om et øjeblik."}


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Songbank-editor kører på http://localhost:{PORT}  ({len(AUTH_USERS)} bruger(e), se {CREDS_PATH})")
    print("Tryk Ctrl+C for at stoppe.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
