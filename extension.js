// Claude Rate Limit Monitor — GNOME Shell 45+ (ESM)
//
// The panel widget stays thin: it spawns helper.py on a timer, reads one line
// of JSON, and renders it. All network/parsing logic lives in helper.py so the
// shell never blocks.

import GObject from 'gi://GObject';
import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Normal poll interval. The usage endpoint is rate-limited per account by
// request frequency (shared with Claude Code's own usage checks), so we keep
// this gentle and back off on errors rather than hammering.
const REFRESH_SECONDS = 120;
// On a failed poll (429, network blip), wait this long and double on each
// repeat, capped here. Resets to REFRESH_SECONDS after a success.
const MAX_BACKOFF_SECONDS = 1800;

const ClaudeIndicator = GObject.registerClass(
class ClaudeIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.0, 'Claude Rate Limit Monitor');
        this._extension = extension;

        this._label = new St.Label({
            text: 'Claude …',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'claude-panel-label',
        });
        this.add_child(this._label);

        // Detail rows shown in the dropdown.
        this._detail = new PopupMenu.PopupMenuItem('Fetching…', {reactive: false});
        this.menu.addMenuItem(this._detail);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const refresh = new PopupMenu.PopupMenuItem('Refresh now');
        refresh.connect('activate', () => this._refresh());
        this.menu.addMenuItem(refresh);

        this._backoff = 0;
        this._refresh();
    }

    // Self-rescheduling one-shot timer so we can vary the delay (backoff).
    _scheduleNext(seconds) {
        if (this._timeout) {
            GLib.source_remove(this._timeout);
            this._timeout = null;
        }
        this._timeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, seconds, () => {
                this._timeout = null;
                this._refresh();
                return GLib.SOURCE_REMOVE;
            });
    }

    // Decide when to poll next: base interval on success, growing backoff on
    // failure so we stop adding to a rate limit that's already unhappy.
    _afterPoll(ok) {
        if (ok) {
            this._backoff = 0;
            this._scheduleNext(REFRESH_SECONDS);
        } else {
            this._backoff = this._backoff
                ? Math.min(this._backoff * 2, MAX_BACKOFF_SECONDS)
                : REFRESH_SECONDS * 2;
            this._scheduleNext(this._backoff);
        }
    }

    _refresh() {
        const helper = GLib.build_filenamev([this._extension.path, 'helper.py']);
        let proc;
        try {
            proc = Gio.Subprocess.new(
                ['python3', helper],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            this._setError(`spawn error: ${e.message}`);
            this._afterPoll(false);
            return;
        }

        proc.communicate_utf8_async(null, null, (p, res) => {
            let ok = false;
            try {
                const [, stdout, stderr] = p.communicate_utf8_finish(res);
                if (!stdout && stderr) throw new Error(stderr.trim());
                ok = this._render(stdout);
            } catch (e) {
                this._setError(String(e.message).slice(0, 200));
            }
            this._afterPoll(ok);
        });
    }

    _render(jsonLine) {
        let data;
        try {
            data = JSON.parse(jsonLine);
        } catch {
            this._setError(`bad output: ${jsonLine.slice(0, 120)}`);
            return false;
        }

        if (data.error) {
            this._setError(data.error);
            return false;
        }

        // data.percent: 0–100 of the most-constrained dimension (or null).
        // data.label:   short panel text. data.lines: array of detail strings.
        const pct = data.percent;
        this._lastLabel = data.label ?? 'Claude';
        this._label.set_text(this._lastLabel);
        this._label.remove_style_class_name('claude-stale');

        // Tint the label as usage climbs.
        this._label.remove_style_class_name('claude-warn');
        this._label.remove_style_class_name('claude-crit');
        if (pct != null && pct >= 90) this._label.add_style_class_name('claude-crit');
        else if (pct != null && pct >= 70) this._label.add_style_class_name('claude-warn');

        this._detail.label.set_text((data.lines ?? []).join('\n') || 'No data');
        return true;
    }

    // A poll failed (rate limit, network blip, transient endpoint error).
    // Keep the last good reading on the panel — just dim it and show the error
    // in the dropdown — so a single hiccup doesn't blank the indicator.
    _setError(msg) {
        this._detail.label.set_text(msg);
        if (this._lastLabel) {
            this._label.set_text(this._lastLabel);
            this._label.add_style_class_name('claude-stale');
        } else {
            this._label.set_text('Claude ✗');
        }
    }

    destroy() {
        if (this._timeout) {
            GLib.source_remove(this._timeout);
            this._timeout = null;
        }
        super.destroy();
    }
});

export default class ClaudeMonitorExtension extends Extension {
    enable() {
        this._indicator = new ClaudeIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
