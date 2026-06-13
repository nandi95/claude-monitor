#!/usr/bin/env python3
"""Claude Rate Limit Monitor helper.

Prints ONE line of JSON to stdout:
  {"percent": 7, "label": "C 7%", "lines": ["...", "..."]}
  {"error": "message"}

Config: ~/.config/claude-monitor/config.json
  {
    "mode": "subscription",         # "subscription" (Pro/Max) or "api"
    "api_key_env": "ANTHROPIC_API_KEY",
    "model": "claude-haiku-4-5",    # api mode: cheap model for the probe
    "panel_metric": "max"           # which % the top bar shows:
  }                                 #   "max" | "session" | "week" | "week_sonnet"

Stdlib only — no pip installs, so the extension works out of the box.

Subscription mode reads the same endpoint the web UI's "Plan usage limits" panel
and Claude Code's /usage command use:
  GET https://api.anthropic.com/api/oauth/usage
authenticated with the Claude Code OAuth token in ~/.claude/.credentials.json.
This is an undocumented endpoint — it can change without notice.
"""

import json
import os
import sys
import time
import datetime as dt
import urllib.request
import urllib.error

CONFIG_PATH = os.path.expanduser("~/.config/claude-monitor/config.json")
CREDS_PATH = os.path.expanduser("~/.claude/.credentials.json")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URLS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://claude.ai/v1/oauth/token",
]
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code public client
OAUTH_BETA = "oauth-2025-04-20"
REFRESH_BUFFER_S = 300  # refresh if the token expires within this many seconds


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    sys.exit(0)


def load_config():
    cfg = {
        "mode": "subscription",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-haiku-4-5",
        "panel_metric": "max",
    }
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        emit({"error": f"bad config: {e}"})
    return cfg


# --------------------------------------------------------------------------- #
# Subscription mode — the real usage endpoint, with token refresh.            #
# --------------------------------------------------------------------------- #
def _read_creds():
    try:
        with open(CREDS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        emit({"error": "no ~/.claude/.credentials.json — log in with Claude Code"})
    except Exception as e:
        emit({"error": f"can't read credentials: {e}"})


def _write_creds(creds):
    """Atomically write creds back, preserving 0600 perms."""
    tmp = CREDS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f)
    os.replace(tmp, CREDS_PATH)


def _refresh(creds):
    oauth = creds["claudeAiOauth"]
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth["refreshToken"],
        "client_id": CLIENT_ID,
    }).encode()

    last_err = None
    for url in TOKEN_URLS:
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            tok = json.load(resp)
            oauth["accessToken"] = tok["access_token"]
            if tok.get("refresh_token"):
                oauth["refreshToken"] = tok["refresh_token"]
            if tok.get("expires_in"):
                oauth["expiresAt"] = int((time.time() + tok["expires_in"]) * 1000)
            _write_creds(creds)
            return oauth["accessToken"], None
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"
        except Exception as e:
            last_err = str(e)
    # Don't exit here — let the caller decide whether the current token still
    # works. Transient network/Cloudflare blips shouldn't blank the panel.
    return None, last_err


def _bearer():
    creds = _read_creds()
    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires_at = (oauth.get("expiresAt") or 0) / 1000
    if not token:
        emit({"error": "no accessToken in credentials"})
    now = time.time()
    if expires_at and expires_at - now < REFRESH_BUFFER_S:
        new_token, err = _refresh(creds)
        if new_token:
            return new_token
        # Refresh failed (often transient). Keep using the existing token if it
        # hasn't actually expired yet; only error out if it's truly dead.
        if not expires_at or expires_at <= now:
            emit({"error": f"token refresh failed: {err}"})
    return token


def _fmt_reset(iso):
    """'resets_at' ISO string -> short human label."""
    if not iso:
        return None
    try:
        when = dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None
    delta = when - dt.datetime.now().astimezone()
    secs = delta.total_seconds()
    if secs <= 0:
        return "resetting"
    if secs < 24 * 3600:  # within a day → relative
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        return f"resets in {h}h {m}m" if h else f"resets in {m}m"
    return f"resets {when:%a %H:%M}"  # e.g. "resets Thu 10:00"


def run_subscription(cfg):
    token = _bearer()
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="GET",
    )
    try:
        data = json.load(urllib.request.urlopen(req, timeout=15))
    except urllib.error.HTTPError as e:
        emit({"error": f"usage HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"})
    except Exception as e:
        emit({"error": f"usage request failed: {e}"})

    # Each block: {"utilization": <0-100|null>, "resets_at": <iso|null>}.
    def util(block):
        b = data.get(block)
        if isinstance(b, dict) and b.get("utilization") is not None:
            return round(float(b["utilization"])), b.get("resets_at")
        return None, None

    sess_p, sess_r = util("five_hour")
    week_p, week_r = util("seven_day")
    son_p, son_r = util("seven_day_sonnet")
    opus_p, opus_r = util("seven_day_opus")

    lines = []
    if sess_p is not None:
        lines.append(f"Session:  {sess_p}%  ({_fmt_reset(sess_r) or '—'})")
    if week_p is not None:
        lines.append(f"Weekly:   {week_p}%  ({_fmt_reset(week_r) or '—'})")
    if opus_p is not None:
        lines.append(f"  Opus:   {opus_p}%")
    if son_p is not None:
        lines.append(f"  Sonnet: {son_p}%")

    if not lines:
        emit({"error": "usage endpoint returned no utilization data"})

    metrics = {
        "session": sess_p,
        "week": week_p,
        "week_sonnet": son_p,
        "max": max([p for p in (sess_p, week_p, son_p, opus_p) if p is not None],
                   default=None),
    }
    panel = metrics.get(cfg.get("panel_metric", "max"))
    if panel is None:
        panel = metrics["max"]

    emit({"percent": panel, "label": f"Claude {panel}%" if panel is not None else "Claude –",
          "lines": lines})


# --------------------------------------------------------------------------- #
# API mode — read anthropic-ratelimit-* headers off a minimal Messages call.  #
# --------------------------------------------------------------------------- #
def run_api(cfg):
    key = os.environ.get(cfg["api_key_env"])
    if not key:
        emit({"error": f"{cfg['api_key_env']} not set in helper environment"})

    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")

    try:
        headers = urllib.request.urlopen(req, timeout=15).headers
    except urllib.error.HTTPError as e:
        headers = e.headers
        if e.code != 429:
            emit({"error": f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"})
    except Exception as e:
        emit({"error": f"request failed: {e}"})

    def num(name):
        try:
            return int(headers.get(name))
        except (TypeError, ValueError):
            return None

    dims = {
        "requests": ("anthropic-ratelimit-requests-limit",
                     "anthropic-ratelimit-requests-remaining"),
        "tokens": ("anthropic-ratelimit-tokens-limit",
                   "anthropic-ratelimit-tokens-remaining"),
        "input": ("anthropic-ratelimit-input-tokens-limit",
                  "anthropic-ratelimit-input-tokens-remaining"),
        "output": ("anthropic-ratelimit-output-tokens-limit",
                   "anthropic-ratelimit-output-tokens-remaining"),
    }
    worst, lines = None, []
    for name, (lk, rk) in dims.items():
        limit, remaining = num(lk), num(rk)
        if limit and remaining is not None:
            used = round(100 * (limit - remaining) / limit)
            worst = used if worst is None else max(worst, used)
            lines.append(f"{name}: {remaining:,}/{limit:,} ({used}% used)")
    if headers.get("retry-after"):
        lines.append(f"retry-after: {headers.get('retry-after')}s")
    if not lines:
        emit({"error": "no anthropic-ratelimit-* headers (check key/model)"})
    emit({"percent": worst, "label": f"Claude {worst}%" if worst is not None else "Claude OK",
          "lines": lines})


def main():
    cfg = load_config()
    if cfg["mode"] == "api":
        run_api(cfg)
    else:
        run_subscription(cfg)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        emit({"error": f"helper crashed: {e}"})
