"""kiro-history: Fuzzy-search and browse Kiro CLI conversation history.

A terminal UI for searching, browsing, and resuming Kiro CLI sessions.
Reads from three stores (all read-only, never modifies session data):
  1. ~/.kiro/sessions/cli/*.json+jsonl        (v3: JSONL format, used by --classic mode)
  2. ~/Library/Application Support/kiro-cli/data.sqlite3 conversations_v2 (v2: SQLite, used by new TUI mode)
  3. ~/Library/Application Support/kiro-cli/data.sqlite3 conversations    (v1: SQLite, legacy)
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Static, ListView, ListItem, RichLog
from textual.message import Message
from rich.text import Text
from rich.markdown import Markdown


# --- Data Layer (read-only) ---

# Paths — override with KIRO_DEMO_DIR env var for demo/recording
_DEMO_DIR = os.environ.get("KIRO_DEMO_DIR", "")
SESSIONS_DIR = Path(_DEMO_DIR) / "kiro" / "sessions" / "cli" if _DEMO_DIR else Path.home() / ".kiro" / "sessions" / "cli"
SQLITE_DB = Path(_DEMO_DIR) / "kiro-cli" / "data.sqlite3" if _DEMO_DIR else Path.home() / "Library" / "Application Support" / "kiro-cli" / "data.sqlite3"
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB guard


def _extract_messages_from_history(history, limit=None):
    """Extract messages from a SQLite v1/v2 history array."""
    messages = []
    for entry in history:
        # User message
        user = entry.get("user", {})
        content = user.get("content", {})
        if "Prompt" in content:
            prompt_text = content["Prompt"].get("prompt", "")
            if prompt_text:
                messages.append({"role": "you", "text": prompt_text})
                if limit and len(messages) >= limit:
                    return messages
        # Assistant message — structure varies:
        #   assistant.content.Text (older format)
        #   assistant.Response.content (text reply)
        #   assistant.ToolUse.content (thinking before tool) + .tool_uses (tools called)
        assistant = entry.get("assistant", {})
        if isinstance(assistant, dict):
            a_content = assistant.get("content", {})
            if "Text" in a_content:
                messages.append({"role": "kiro", "text": a_content["Text"]})
                if limit and len(messages) >= limit:
                    return messages
            elif "Response" in assistant:
                resp = assistant["Response"]
                if isinstance(resp, dict) and resp.get("content"):
                    messages.append({"role": "kiro", "text": resp["content"]})
                    if limit and len(messages) >= limit:
                        return messages
            elif "ToolUse" in assistant:
                tu = assistant["ToolUse"]
                if isinstance(tu, dict):
                    txt = tu.get("content", "")
                    tools = tu.get("tool_uses", [])
                    tool_names = ", ".join(t.get("name", "?") for t in tools[:3]) if tools else ""
                    display = txt if txt else ""
                    if tool_names:
                        display = f"{display}\n[tools: {tool_names}]" if display else f"[tools: {tool_names}]"
                    if display:
                        messages.append({"role": "kiro", "text": display})
                        if limit and len(messages) >= limit:
                            return messages
    return messages


def _get_first_prompt_from_history(history):
    """Get the first user prompt from a history array for use as title."""
    for entry in history:
        user = entry.get("user", {})
        content = user.get("content", {})
        if "Prompt" in content:
            txt = content["Prompt"].get("prompt", "").strip()
            if txt:
                return txt[:60]
    return "(untitled)"


def _load_sqlite_sessions():
    """Load sessions from the SQLite database (v1 + v2 tables)."""
    sessions = []
    if not SQLITE_DB.exists():
        return sessions

    try:
        conn = sqlite3.connect(f"file:{SQLITE_DB}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sessions

    # V2 sessions (Dec 2025 - Mar 2026) — have timestamps and session IDs
    try:
        rows = conn.execute(
            "SELECT key, conversation_id, value, created_at, updated_at "
            "FROM conversations_v2 ORDER BY updated_at DESC"
        ).fetchall()
        for cwd, conv_id, value, created_ms, updated_ms in rows:
            try:
                d = json.loads(value)
                history = d.get("history", [])
                title = _get_first_prompt_from_history(history)
                created = datetime.fromtimestamp(created_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")
                updated = datetime.fromtimestamp(updated_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S")
                msg_count = len(history)
                duration_min = int((updated_ms - created_ms) / 1000 / 60)
                sessions.append({
                    "session_id": conv_id,
                    "title": title,
                    "cwd": cwd,
                    "created_at": created,
                    "updated_at": updated,
                    "source": "sqlite_v2",
                    "msg_count": msg_count,
                    "duration_min": duration_min,
                    "_history": history,
                })
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    except sqlite3.OperationalError:
        pass

    # V1 sessions (Nov 2025 - Dec 2025) — keyed by directory, no timestamps
    try:
        rows = conn.execute("SELECT key, value FROM conversations").fetchall()
        for cwd, value in rows:
            try:
                d = json.loads(value)
                history = d.get("history", [])
                title = _get_first_prompt_from_history(history)
                conv_id = d.get("conversation_id", "")
                sessions.append({
                    "session_id": conv_id,
                    "title": title,
                    "cwd": cwd,
                    "created_at": "",
                    "updated_at": "",
                    "source": "sqlite_v1",
                    "msg_count": len(history),
                    "duration_min": 0,
                    "_history": history,
                })
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    except sqlite3.OperationalError:
        pass

    conn.close()
    return sessions


def _load_jsonl_sessions():
    """Load sessions from ~/.kiro/sessions/cli/*.json (v3: current format)."""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions

    for json_file in SESSIONS_DIR.glob("*.json"):
        try:
            if json_file.stat().st_size > MAX_FILE_SIZE:
                continue
            with open(json_file) as f:
                meta = json.load(f)
            created = meta.get("created_at") or ""
            updated = meta.get("updated_at") or ""
            # Count messages in JSONL
            jsonl_path = str(json_file).replace(".json", ".jsonl")
            msg_count = 0
            jp = Path(jsonl_path)
            if jp.exists():
                with open(jp) as jf:
                    for line in jf:
                        try:
                            ld = json.loads(line)
                            if ld.get("kind") in ("Prompt", "AssistantMessage"):
                                msg_count += 1
                        except (json.JSONDecodeError, ValueError):
                            pass
            # Compute duration
            duration_min = 0
            if created and updated:
                try:
                    c = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    u = datetime.fromisoformat(updated.replace("Z", "+00:00"))
                    duration_min = int((u - c).total_seconds() / 60)
                except (ValueError, TypeError):
                    pass
            sessions.append({
                "session_id": meta.get("session_id", ""),
                "title": meta.get("title") or "(untitled)",
                "cwd": meta.get("cwd") or "",
                "created_at": created,
                "updated_at": updated,
                "source": "jsonl",
                "msg_count": msg_count,
                "duration_min": duration_min,
                "jsonl_path": jsonl_path,
            })
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            pass
    return sessions


def get_sessions():
    """Load all sessions from all stores, deduplicated, sorted by recency."""
    jsonl = _load_jsonl_sessions()
    sqlite = _load_sqlite_sessions()

    # Deduplicate: if same session_id exists in both, prefer JSONL (newer format)
    seen_ids = {s["session_id"] for s in jsonl if s["session_id"]}
    for s in sqlite:
        if s["session_id"] and s["session_id"] not in seen_ids:
            jsonl.append(s)
            seen_ids.add(s["session_id"])

    # Sort: sessions with timestamps first (descending), then untimed ones at the end
    jsonl.sort(key=lambda s: s.get("updated_at") or s.get("created_at") or "0", reverse=True)
    return jsonl


def extract_messages(session, limit=None):
    """Extract conversation messages from any session format."""
    # SQLite sessions carry _history inline
    if "_history" in session:
        return _extract_messages_from_history(session["_history"], limit)

    # JSONL sessions read from file
    jsonl_path = session.get("jsonl_path", "")
    if not jsonl_path:
        return []
    path = Path(jsonl_path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    if path.stat().st_size > MAX_FILE_SIZE:
        return [{"role": "system", "text": "(File too large to preview)"}]

    messages = []
    with open(path) as f:
        for line in f:
            try:
                d = json.loads(line)
                kind = d.get("kind", "")
                if kind not in ("Prompt", "AssistantMessage"):
                    continue
                data = d.get("data", {})
                content = data.get("content", [])
                txt = ""
                for block in (content if isinstance(content, list) else []):
                    if isinstance(block, dict) and block.get("kind") == "text":
                        txt = block.get("data", "")
                        break
                if txt:
                    role = "you" if kind == "Prompt" else "kiro"
                    messages.append({"role": role, "text": txt})
                    if limit and len(messages) >= limit:
                        break
            except (json.JSONDecodeError, KeyError, ValueError):
                pass
    return messages


def _fuzzy_match(query, text):
    """Fuzzy match: all query tokens must appear in text, in any order.
    Supports multi-word queries ('mem leak' matches 'Debug memory leak')
    and tolerates partial words ('depl' matches 'deployment').
    """
    text_lower = text.lower()
    tokens = query.lower().split()
    return all(token in text_lower for token in tokens)


def search_sessions(query, sessions):
    """Fuzzy-search sessions by title, cwd, and conversation content across all stores."""
    if not query:
        return sessions

    results = []

    for session in sessions:
        # Check title and cwd first (fast)
        title = (session.get("title") or "")
        cwd = (session.get("cwd") or "")
        if _fuzzy_match(query, title) or _fuzzy_match(query, cwd):
            results.append(session)
            continue

        # Search conversation content
        if "_history" in session:
            # SQLite sessions — search inline history
            found = False
            for entry in session["_history"]:
                user = entry.get("user", {})
                content = user.get("content", {})
                if "Prompt" in content:
                    if _fuzzy_match(query, content["Prompt"].get("prompt", "")):
                        found = True
                        break
                assistant = entry.get("assistant", {})
                if isinstance(assistant, dict):
                    a_content = assistant.get("content", {})
                    if "Text" in a_content and _fuzzy_match(query, a_content["Text"]):
                        found = True
                        break
                    if "Response" in assistant:
                        resp = assistant["Response"]
                        if isinstance(resp, dict) and _fuzzy_match(query, resp.get("content", "")):
                            found = True
                            break
                    if "ToolUse" in assistant:
                        tu = assistant["ToolUse"]
                        if isinstance(tu, dict) and _fuzzy_match(query, tu.get("content", "")):
                            found = True
                            break
            if found:
                results.append(session)
        elif session.get("jsonl_path"):
            # JSONL sessions — search file
            jsonl_path = Path(session["jsonl_path"])
            if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
                continue
            if jsonl_path.stat().st_size > MAX_FILE_SIZE:
                continue
            found = False
            with open(jsonl_path) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        kind = d.get("kind", "")
                        if kind not in ("Prompt", "AssistantMessage"):
                            continue
                        content = d.get("data", {}).get("content", [])
                        for block in (content if isinstance(content, list) else []):
                            if isinstance(block, dict) and block.get("kind") == "text":
                                if _fuzzy_match(query, block.get("data", "")):
                                    found = True
                                    break
                        if found:
                            break
                    except (json.JSONDecodeError, KeyError, ValueError):
                        pass
            if found:
                results.append(session)

    return results


# --- UI Components ---

class SessionItem(ListItem):
    """A single session row in the list."""

    def __init__(self, session: dict) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        raw_ts = (self.session.get("updated_at") or "")[:10]
        try:
            dt = datetime.strptime(raw_ts, "%Y-%m-%d")
            ts = dt.strftime("%-d %b %Y")
        except (ValueError, TypeError):
            ts = raw_ts
        title = (self.session.get("title") or "(untitled)")[:60]
        cwd = os.path.basename(self.session.get("cwd") or "")
        msgs = self.session.get("msg_count", 0)
        dur = self.session.get("duration_min", 0)
        dur_str = f"{dur}m" if dur < 60 else f"{dur // 60}h {dur % 60}m"
        yield Static(
            f"[bold]{title}[/bold]\n"
            f"[dim]{cwd}[/dim]  [dim italic]{ts}[/dim italic]  [dim cyan]{msgs} msgs[/dim cyan]  [dim green]{dur_str}[/dim green]",
            markup=True,
        )


class KiroHistory(App):
    """Kiro CLI session browser and search."""

    TITLE = "kiro-cli-history"
    CSS = """
    Screen {
        layout: horizontal;
    }
    #left-pane {
        width: 2fr;
        min-width: 40;
        border-right: solid $accent;
    }
    #right-pane {
        width: 3fr;
        min-width: 50;
    }
    #search-input {
        dock: top;
        margin: 0 1;
    }
    #session-list {
        height: 1fr;
    }
    #preview {
        height: 1fr;
        padding: 0 1;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    SessionItem {
        padding: 0 1;
        height: 3;
    }
    SessionItem:hover {
        background: $boost;
    }
    ListView > SessionItem.-highlight {
        background: $accent;
    }
    """

    BINDINGS = [
        Binding("ctrl+r", "resume", "Resume session"),
        Binding("ctrl+y", "copy_conversation", "Copy to clipboard"),
        Binding("ctrl+f", "search_content", "Search in conversation"),
        Binding("escape", "clear_or_quit", "Clear / Quit"),
        Binding("ctrl+c", "quit", "Quit"),
        Binding("/", "focus_search", "Search"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    def __init__(self):
        super().__init__()
        self.all_sessions = []
        self.filtered_sessions = []
        self.selected_session = None
        self._viewer_search_query = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left-pane"):
                yield Input(placeholder="Search sessions...", id="search-input")
                yield ListView(id="session-list")
            with Vertical(id="right-pane"):
                yield RichLog(id="preview", wrap=True, highlight=True, markup=True)
        yield Static("", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.all_sessions = get_sessions()
        self.filtered_sessions = self.all_sessions
        self._populate_list(self.filtered_sessions)
        status = self.query_one("#status-bar", Static)
        status.update(f" {len(self.all_sessions)} sessions | Ctrl+R resume | / search | ? help")

    def _populate_list(self, sessions):
        list_view = self.query_one("#session-list", ListView)
        list_view.clear()
        for session in sessions:
            list_view.append(SessionItem(session))

    # --- Search ---

    @on(Input.Changed, "#search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        self._do_search(event.value)

    @work(thread=True)
    def _do_search(self, query: str) -> None:
        results = search_sessions(query, self.all_sessions)
        self.filtered_sessions = results
        self.call_from_thread(self._populate_list, results)
        status_text = f" {len(results)}/{len(self.all_sessions)} sessions"
        if query:
            status_text += f" matching '{query}'"
        status_text += " | Ctrl+R resume | / search"
        self.call_from_thread(
            self.query_one("#status-bar", Static).update, status_text
        )

    # --- Preview ---

    @on(ListView.Highlighted, "#session-list")
    def on_session_highlighted(self, event: ListView.Highlighted) -> None:
        if event.item is None:
            return
        session = event.item.session
        self.selected_session = session
        self._load_preview(session)

    @work(thread=True)
    def _load_preview(self, session: dict) -> None:
        preview = self.query_one("#preview", RichLog)
        self.call_from_thread(preview.clear)

        # Header
        title = session.get("title") or "(untitled)"
        cwd = session.get("cwd") or ""
        created = (session.get("created_at") or "")[:19].replace("T", " ")
        updated = (session.get("updated_at") or "")[:19].replace("T", " ")
        msgs = session.get("msg_count", 0)
        dur = session.get("duration_min", 0)
        dur_str = f"{dur}m" if dur < 60 else f"{dur // 60}h {dur % 60}m"

        header = (
            f"[bold]SESSION:[/bold] {title}\n"
            f"[bold]DIRECTORY:[/bold] {cwd}\n"
            f"[bold]CREATED:[/bold] {created}\n"
            f"[bold]UPDATED:[/bold] {updated}\n"
            f"[bold]MESSAGES:[/bold] {msgs}\n"
            f"[bold]DURATION:[/bold] {dur_str}\n"
            f"[bold]ID:[/bold] {session.get('session_id', '')}\n"
        )
        self.call_from_thread(preview.write, Text.from_markup(header))
        self.call_from_thread(preview.write, Text("─" * 50))
        self.call_from_thread(preview.write, Text(""))

        # Messages
        messages = extract_messages(session)
        if not messages:
            self.call_from_thread(preview.write, Text("(no conversation data)"))
            return

        for msg in messages:
            role = msg["role"]
            txt = msg["text"]
            if role == "you":
                label = Text.from_markup(f"[bold cyan][YOU]:[/bold cyan]")
            else:
                label = Text.from_markup(f"[bold green][KIRO]:[/bold green]")
            self.call_from_thread(preview.write, label)
            # Try to render as markdown for assistant messages
            if role == "kiro":
                try:
                    self.call_from_thread(preview.write, Markdown(txt))
                except Exception:
                    self.call_from_thread(preview.write, Text(txt))
            else:
                self.call_from_thread(preview.write, Text(txt))
            self.call_from_thread(preview.write, Text(""))

    # --- Actions ---

    def action_resume(self) -> None:
        if not self.selected_session:
            return
        cwd = self.selected_session.get("cwd", "")
        if not cwd or not os.path.isdir(cwd):
            self.notify(f"Directory not found: {cwd}", severity="error")
            return
        self.exit(result=("resume", self.selected_session))

    def action_focus_search(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_clear_or_quit(self) -> None:
        search = self.query_one("#search-input", Input)
        if search.value:
            search.value = ""
            search.focus()
        else:
            self.exit()

    def action_cursor_down(self) -> None:
        self.query_one("#session-list", ListView).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#session-list", ListView).action_cursor_up()

    def action_search_content(self) -> None:
        self.query_one("#search-input", Input).focus()

    def action_copy_conversation(self) -> None:
        if not self.selected_session:
            return
        messages = extract_messages(self.selected_session)
        if not messages:
            self.notify("No messages to copy", severity="warning")
            return
        text = ""
        for msg in messages:
            label = "[YOU]" if msg["role"] == "you" else "[KIRO]"
            text += f"{label}:\n{msg['text']}\n\n"
        try:
            process = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            process.communicate(text.encode("utf-8"))
            self.notify(f"Copied {len(messages)} messages to clipboard")
        except FileNotFoundError:
            self.notify("pbcopy not found (macOS only)", severity="error")


# --- Entry Point ---

def main():
    app = KiroHistory()
    result = app.run()

    if result and isinstance(result, tuple) and result[0] == "resume":
        session = result[1]
        cwd = session["cwd"]
        print(f"\nResuming session: {session['title']}")
        print(f"Directory: {cwd}\n")
        os.chdir(cwd)
        session_id = session.get("session_id", "")
        cmd = ["kiro-cli", "chat", "--resume-id", session_id] if session_id else ["kiro-cli", "chat", "--resume"]
        os.execvp("kiro-cli", cmd)


if __name__ == "__main__":
    main()
