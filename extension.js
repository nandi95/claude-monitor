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

const REFRESH_SECONDS = 120; // API mode bills a tiny request each poll; tune this.

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

        this._refresh();
        this._timeout = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, REFRESH_SECONDS, () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _refresh() {
        const helper = GLib.build_filenamev([this._extension.path, 'helper.py']);
        let proc;
        try {
            proc = Gio.Subprocess.new(
                ['python3', helper],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
        } catch (e) {
            this._label.set_text('Claude ✗');
            this._detail.label.set_text(`spawn error: ${e.message}`);
            return;
        }

        proc.communicate_utf8_async(null, null, (p, res) => {
            let out = '';
            try {
                const [, stdout, stderr] = p.communicate_utf8_finish(res);
                out = stdout;
                if (!stdout && stderr) throw new Error(stderr.trim());
            } catch (e) {
                this._label.set_text('Claude ✗');
                this._detail.label.set_text(String(e.message).slice(0, 200));
                return;
            }
            this._render(out);
        });
    }

    _render(jsonLine) {
        let data;
        try {
            data = JSON.parse(jsonLine);
        } catch {
            this._label.set_text('Claude ?');
            this._detail.label.set_text(`bad output: ${jsonLine.slice(0, 120)}`);
            return;
        }

        if (data.error) {
            this._label.set_text('Claude ✗');
            this._detail.label.set_text(data.error);
            return;
        }

        // data.percent: 0–100 of the most-constrained dimension (or null).
        // data.label:   short panel text. data.lines: array of detail strings.
        const pct = data.percent;
        this._label.set_text(data.label ?? 'Claude');

        // Tint the label as usage climbs.
        this._label.remove_style_class_name('claude-warn');
        this._label.remove_style_class_name('claude-crit');
        if (pct != null && pct >= 90) this._label.add_style_class_name('claude-crit');
        else if (pct != null && pct >= 70) this._label.add_style_class_name('claude-warn');

        this._detail.label.set_text((data.lines ?? []).join('\n') || 'No data');
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
