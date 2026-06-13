# Claude Rate Limit Monitor

A GNOME Shell top-bar extension that shows Claude rate-limit / usage. Works in
two modes so it's useful regardless of how you access Claude.

## Modes

Edit `~/.config/claude-monitor/config.json`:

```json
{
  "mode": "api",                 // or "subscription"
  "api_key_env": "ANTHROPIC_API_KEY",
  "model": "claude-haiku-4-5",
  "window_hours": 5,
  "token_cap": null
}
```

### `api` mode (API key + billing)
Makes one minimal `POST /v1/messages` request each poll and reads the
`anthropic-ratelimit-*` response headers (requests/tokens/input/output limits +
remaining). There is **no** "get my limits" GET endpoint, so it reads them off a
real call — that bills a few tokens per poll, so the default refresh is 120s.

**Environment gotcha:** GNOME Shell spawns the helper *without* your shell's
environment, so `ANTHROPIC_API_KEY` from `.bashrc`/`.zshrc` won't be visible.
Put it where the systemd user session (and thus gnome-shell) will see it:

```
mkdir -p ~/.config/environment.d
printf 'ANTHROPIC_API_KEY=sk-ant-...\n' > ~/.config/environment.d/claude.conf
# then log out and back in
```

### `subscription` mode (Pro/Max)
No official API exists for consumer-subscription limits. This tallies token
usage from Claude Code's local transcripts (`~/.claude/projects/**/*.jsonl`)
over the rolling `window_hours`. It's a best-effort estimate.

- Set `token_cap` to your known cap to get a `%`; otherwise it shows the raw
  token count.
- The tally currently includes `cache_read_input_tokens`, which Claude Code
  uses heavily and inflates the number. To track "real" usage, edit
  `run_subscription` in `helper.py` and drop the cache lines.

## Install

The files live in:
`~/.local/share/gnome-shell/extensions/claude-monitor@nandork.github.io/`

Enable it:
```
gnome-extensions enable claude-monitor@nandork.github.io
```

On **Wayland** you must **log out and back in** after installing or editing
(you can't restart gnome-shell in place). After that, toggling enable/disable
is instant.

## Debugging

```
# Run the helper directly:
python3 ~/.local/share/gnome-shell/extensions/claude-monitor@nandork.github.io/helper.py

# Watch shell logs for extension errors:
journalctl --user -f -o cat /usr/bin/gnome-shell
```

## Sharing

To publish on extensions.gnome.org, zip the extension directory contents (not
the parent folder) and upload. The `helper.py` ships inside the extension, so
end users only need Python 3 (stdlib only — no pip installs).
