#!/usr/bin/env python3
"""Claude Rate Limit Monitor helper.

Prints ONE line of JSON to stdout:
  {"percent": 42, "label": "Claude 42%", "lines": ["...", "..."]}
  {"error": "message"}

Config: ~/.config/claude-monitor/config.json
  {
    "mode": "api",                  # "api" or "subscription"
    "api_key_env": "ANTHROPIC_API_KEY",
    "model": "claude-haiku-4-5",    # cheap model for the probe request

    # subscription mode:
    "window_hours": 5,              # rolling window Anthropic resets on
    "token_cap": null               # your known cap, if any, to compute a %
  }

Stdlib only — no pip installs, so the extension works out of the box.
"""

import json
import os
import sys
import time
import glob
import datetime as dt
import urllib.request
import urllib.error

CONFIG_PATH = os.path.expanduser("~/.config/claude-monitor/config.json")


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()
    sys.exit(0)


def load_config():
    defaults = {
        "mode": "api",
        "api_key_env": "ANTHROPIC_API_KEY",
        "model": "claude-haiku-4-5",
        "window_hours": 5,
        "token_cap": None,
    }
    try:
        with open(CONFIG_PATH) as f:
            defaults.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        emit({"error": f"bad config: {e}"})
    return defaults


# --------------------------------------------------------------------------- #
# API mode: read anthropic-ratelimit-* headers off a minimal Messages request. #
# --------------------------------------------------------------------------- #
def run_api(cfg):
    key = os.environ.get(cfg["api_key_env"])
    if not key:
        # GNOME spawns the helper without your shell env, so export the key in a
        # systemd user environment or a wrapper. See README.
        emit({"error": f"{cfg['api_key_env']} not set in helper environment"})

    body = json.dumps({
        "model": cfg["model"],
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        headers = resp.headers
    except urllib.error.HTTPError as e:
        # 429 still carries the rate-limit headers + retry-after.
        headers = e.headers
        if e.code != 429:
            emit({"error": f"HTTP {e.code}: {e.read()[:120].decode(errors='replace')}"})
    except Exception as e:
        emit({"error": f"request failed: {e}"})

    def num(name):
        v = headers.get(name)
        try:
            return int(v)
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

    worst = None
    lines = []
    for name, (lk, rk) in dims.items():
        limit, remaining = num(lk), num(rk)
        if limit and remaining is not None:
            used_pct = round(100 * (limit - remaining) / limit)
            worst = used_pct if worst is None else max(worst, used_pct)
            lines.append(f"{name}: {remaining:,}/{limit:,} left ({used_pct}% used)")

    retry = headers.get("retry-after")
    if retry:
        lines.append(f"retry-after: {retry}s")

    if not lines:
        emit({"error": "no anthropic-ratelimit-* headers (check key/model)"})

    label = f"Claude {worst}%" if worst is not None else "Claude OK"
    emit({"percent": worst, "label": label, "lines": lines})


# --------------------------------------------------------------------------- #
# Subscription mode: estimate usage from Claude Code's local JSONL transcripts. #
# No official API exists for Pro/Max limits — this is a best-effort tally.       #
# --------------------------------------------------------------------------- #
def run_subscription(cfg):
    window = float(cfg.get("window_hours", 5))
    cap = cfg.get("token_cap")
    cutoff = time.time() - window * 3600

    pattern = os.path.expanduser("~/.claude/projects/**/*.jsonl")
    files = glob.glob(pattern, recursive=True)
    if not files:
        emit({"error": "no ~/.claude logs found — is Claude Code installed?"})

    total_in = total_out = 0
    for path in files:
        # Cheap pre-filter: skip files untouched within the window.
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    ts = _parse_ts(rec.get("timestamp"))
                    if ts is None or ts < cutoff:
                        continue
                    usage = (rec.get("message") or {}).get("usage") or rec.get("usage")
                    if not usage:
                        continue
                    total_in += int(usage.get("input_tokens", 0) or 0)
                    total_in += int(usage.get("cache_read_input_tokens", 0) or 0)
                    total_in += int(usage.get("cache_creation_input_tokens", 0) or 0)
                    total_out += int(usage.get("output_tokens", 0) or 0)
        except OSError:
            continue

    total = total_in + total_out
    lines = [
        f"window: last {window:g}h",
        f"input:  {total_in:,} tokens",
        f"output: {total_out:,} tokens",
        f"total:  {total:,} tokens",
    ]

    pct = None
    if cap:
        pct = round(100 * total / cap)
        lines.append(f"cap:    {cap:,} ({pct}% used)")
        label = f"Claude {pct}%"
    else:
        label = f"Claude {_short(total)}"
        lines.append("set token_cap in config for a %")

    emit({"percent": pct, "label": label, "lines": lines})


def _parse_ts(value):
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, AttributeError):
        return None


def _short(n):
    for unit in ("", "K", "M"):
        if abs(n) < 1000:
            return f"{n:.0f}{unit}"
        n /= 1000
    return f"{n:.0f}B"


def main():
    cfg = load_config()
    if cfg["mode"] == "subscription":
        run_subscription(cfg)
    else:
        run_api(cfg)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # never crash silently — the panel shows this
        emit({"error": f"helper crashed: {e}"})
