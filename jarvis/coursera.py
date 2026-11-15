"""Coursera link: a tiny local endpoint that the Jarvis Chrome extension (the extension/ folder) talks to.

The extension's content script runs on coursera.org. About once a second it reports the lesson it shows
(title, video playing or not, position) and collects any commands Jarvis has queued for it: play, pause, next
video, previous video, speed, autoplay on/off. Autoplay ("binge mode") itself runs inside the page, so it keeps
working when Jarvis is closed.

Only Chrome extensions are answered (the Origin header must be chrome-extension://...), and the server only
listens on 127.0.0.1, so web pages and other computers can't drive it."""
import json
import logging
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import resolve

log = logging.getLogger("jarvis.coursera")

FRESH = 4.0          # a tab that hasn't reported for this long is gone (closed, or Chrome is shut)


class Bridge:
    def __init__(self):
        self.port = 8765
        self.on_event = None                         # callback(text): things worth telling the user
        self.tabs: dict[str, dict] = {}              # tab id -> {"state": {...}, "seen": t}
        self.queue: list[dict] = []                  # {"id", "tab", "cmd", "value", "at"}
        self.acks: dict[str, str] = {}               # command id -> what the page said happened
        self.autostart_until = 0.0                   # after "open my course": start playing when the tab connects
        self.lock = threading.Lock()
        self.server = None
        self.history_path = resolve("data/coursera.json")
        try:
            self.history = json.loads(self.history_path.read_text(encoding="utf-8"))
        except Exception:
            self.history = {"courses": {}, "last": None}

    # ------------------------------------------------------------------ server
    def start(self, port: int = 8765, on_event=None) -> bool:
        self.port, self.on_event = port, on_event
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                origin = self.headers.get("Origin", "")
                if self.path != "/coursera" or not origin.startswith("chrome-extension://"):
                    self.send_error(403)
                    return
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
                    reply = bridge.sync(str(body.get("tab")), body.get("state") or {})
                except Exception as e:
                    log.warning("bad message from the extension: %s", e)
                    self.send_error(400)
                    return
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path in ("/", "/dashboard"):
                    body, ctype = bridge.dashboard_html().encode("utf-8"), "text/html; charset=utf-8"
                elif path == "/progress":
                    body, ctype = json.dumps(bridge.progress_data()).encode(), "application/json"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            log.error("Coursera link could not listen on port %s: %s", port, e)
            return False
        threading.Thread(target=self.server.serve_forever, daemon=True, name="coursera").start()
        log.info("Coursera link listening on 127.0.0.1:%s", port)
        return True

    # ------------------------------------------------------------------ from the extension
    def sync(self, tab: str, state: dict) -> dict:
        now = time.time()
        if not self.history.get("connected"):
            self.history["connected"] = True          # the extension has been installed and has reached Jarvis
            self._save()
        with self.lock:
            self.tabs[tab] = {"state": state, "seen": now}
            for gone in [k for k, v in self.tabs.items() if now - v["seen"] > 60]:
                del self.tabs[gone]
            for a in state.get("acks") or []:
                self.acks[str(a.get("id"))] = str(a.get("result", ""))
            if len(self.acks) > 50:
                for k in list(self.acks)[:-50]:
                    del self.acks[k]
            out = [q for q in self.queue if q["tab"] in (tab, None)]
            self.queue = [q for q in self.queue if q not in out and now - q["at"] < 10]
            if now < self.autostart_until and state.get("kind") in ("lecture", "home", "other"):
                self.autostart_until = 0.0
                out.append({"id": "auto", "cmd": "binge", "value": "on"})
        self._remember(state)
        for ev in state.get("events") or []:
            log.info("coursera: %s", ev)
            if self.on_event:
                try:
                    self.on_event(str(ev))
                except Exception as e:
                    log.debug("event callback failed: %s", e)
        return {"commands": [{"id": q["id"], "cmd": q["cmd"], "value": q.get("value", "")} for q in out]}

    def _remember(self, state: dict):
        """Keep the last lesson of each course (so "resume" can reopen it) and its progress (for the dashboard)."""
        slug = state.get("course")
        if not slug:
            return
        courses = self.history.setdefault("courses", {})
        c = courses.setdefault(slug, {})
        changed = False
        name = state.get("courseName") or c.get("name") or slug
        if c.get("name") != name:
            c["name"], changed = name, True
        if state.get("kind") == "lecture" and state.get("url"):
            if c.get("url") != state["url"] or c.get("title") != state.get("title", ""):
                c["url"], c["title"], changed = state["url"], state.get("title", ""), True
            if self.history.get("last") != slug:
                self.history["last"], changed = slug, True
        ol = state.get("outline")
        if ol:
            lectures = [i for i in ol if i.get("type") == "lecture"]
            prog = {"total": len(lectures), "done": sum(1 for i in lectures if i.get("done")),
                    "items": [{"type": i.get("type"), "title": i.get("title", ""), "done": bool(i.get("done"))} for i in ol],
                    "at": time.strftime("%Y-%m-%d %H:%M")}
            if c.get("progress", {}).get("done") != prog["done"] or c.get("progress", {}).get("total") != prog["total"] \
                    or len(c.get("progress", {}).get("items", [])) != len(prog["items"]):
                c["progress"], changed = prog, True
        if changed:
            self._save()

    # ------------------------------------------------------------------ progress & dashboard
    def progress_data(self) -> dict:
        cs = []
        for slug, c in self.history.get("courses", {}).items():
            p = c.get("progress") or {}
            cs.append({"slug": slug, "name": c.get("name", slug), "title": c.get("title", ""),
                       "done": p.get("done", 0), "total": p.get("total", 0),
                       "items": p.get("items", []), "at": p.get("at", "")})
        last = self.history.get("last")
        cs.sort(key=lambda x: (x["slug"] != last, x["name"].lower()))
        return {"courses": cs, "last": last, "active": self.active()}

    def progress_summary(self) -> str:
        c = self.history.get("courses", {}).get(self.history.get("last"))
        if not c or not c.get("progress") or not c["progress"].get("total"):
            return ("I haven't recorded any Coursera progress yet. Open a course with the extension running "
                    "and I'll start tracking it.")
        p = c["progress"]
        rem = max(0, p["total"] - p["done"])
        return (f"In {c.get('name')}, you've completed {p['done']} of {p['total']} lecture videos, "
                f"{rem} remaining." + (f" Next up: {c.get('title')}." if c.get("title") and rem else ""))

    def _save(self):
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text(json.dumps(self.history, indent=1), encoding="utf-8")
        except OSError as e:
            log.debug("could not save Coursera history: %s", e)

    @property
    def installed(self) -> bool:
        return bool(self.history.get("connected"))

    # ------------------------------------------------------------------ for Jarvis
    def live_tabs(self) -> dict[str, dict]:
        now = time.time()
        with self.lock:
            return {k: v["state"] for k, v in self.tabs.items() if now - v["seen"] < FRESH}

    def active(self) -> bool:
        return bool(self.live_tabs())

    def target(self) -> tuple[str, dict] | None:
        """The tab a command is meant for: the one playing, else the one on screen, else the latest used."""
        tabs = self.live_tabs()
        if not tabs:
            return None
        return max(tabs.items(), key=lambda kv: (bool(kv[1].get("playing")), bool(kv[1].get("visible")),
                                                 kv[1].get("activeAt", 0)))

    def send(self, cmd: str, value: str = "", wait: float = 3.0) -> str | None:
        """Queue a command for the current Coursera tab and wait for the page's answer.
        None = no Coursera tab is connected. "" = sent, but the page didn't answer in time."""
        tgt = self.target()
        if not tgt:
            return None
        cid = uuid.uuid4().hex[:8]
        with self.lock:
            self.queue.append({"id": cid, "tab": tgt[0], "cmd": cmd, "value": value, "at": time.time()})
        end = time.time() + wait
        while time.time() < end:
            with self.lock:
                if cid in self.acks:
                    return self.acks.pop(cid)
            time.sleep(0.05)
        return ""

    def status(self) -> str | None:
        tgt = self.target()
        if not tgt:
            return None
        s = tgt[1]
        if s.get("kind") != "lecture":
            return f"Coursera is open on {s.get('title') or 'a course page'}; it isn't a video."
        pos, dur = int(s.get("time") or 0), int(s.get("duration") or 0)
        left = max(0, dur - pos)
        return (f"{'Playing' if s.get('playing') else 'Paused on'} '{s.get('title')}'"
                + (f" in {s.get('courseName')}" if s.get("courseName") else "")
                + (f", {left // 60} min {left % 60} s left" if dur else "")
                + f". Autoplay is {'on' if s.get('binge') else 'off'}"
                + (f", speed {s.get('speed')}x" if s.get("speed") and s.get("speed") != 1 else "") + ".")

    def dashboard_html(self) -> str:
        return _DASHBOARD.replace("__PORT__", str(self.port))

    def find_course(self, name: str) -> dict | None:
        name = name.lower().strip()
        courses = self.history.get("courses", {})
        if not name:
            last = self.history.get("last")
            return courses.get(last) if last else None
        words = [w for w in name.split() if len(w) > 2 and w not in ("the", "course", "coursera", "class")]
        best, score = None, 0
        for slug, c in courses.items():
            hay = (slug.replace("-", " ") + " " + c.get("name", "")).lower()
            s = sum(w in hay for w in words)
            if s > score:
                best, score = c, s
        # most of the spoken words must match: "deep learning" is not "machine learning"
        return best if words and score >= -(-len(words) * 3 // 5) else None      # ceil(60 %)


_DASHBOARD = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis · Coursera Progress</title>
<style>
  :root{--bg:#070b12;--card:#0e1621;--line:#1d2b40;--fg:#e7eef8;--muted:#8295b0;--gold:#f4c24e;--cyan:#46d6f0;--green:#4fe0a0;}
  *{box-sizing:border-box;} body{margin:0;background:radial-gradient(120% 80% at 50% -10%,#0c1626,var(--bg) 60%);
    color:var(--fg);font:15px/1.5 "Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;}
  .wrap{max-width:900px;margin:0 auto;padding:28px 20px 48px;}
  header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:20px;}
  h1{font-size:22px;margin:0;letter-spacing:.02em;} h1 span{color:var(--gold);}
  .status{font-size:13px;color:var(--muted);} .status b{color:var(--green);}
  .status.off b{color:var(--muted);}
  .course{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px 20px;margin-bottom:16px;}
  .course h2{margin:0 0 4px;font-size:17px;} .course .sub{color:var(--muted);font-size:13px;margin-bottom:12px;}
  .bar{height:10px;border-radius:999px;background:#17212f;overflow:hidden;margin:10px 0;}
  .bar i{display:block;height:100%;border-radius:999px;background:linear-gradient(90deg,var(--cyan),var(--green));transition:width .5s;}
  .count{font-size:13px;color:var(--muted);margin-bottom:10px;} .count b{color:var(--fg);}
  .items{display:flex;flex-direction:column;gap:2px;max-height:230px;overflow:auto;margin-top:6px;}
  .it{display:flex;align-items:center;gap:9px;padding:5px 2px;font-size:13.5px;border-bottom:1px solid #121b27;}
  .it:last-child{border-bottom:none;}
  .dot{flex:none;width:16px;height:16px;border-radius:4px;display:grid;place-items:center;font-size:11px;}
  .lecture .dot{background:rgba(70,214,240,.15);color:var(--cyan);}
  .graded .dot{background:rgba(244,194,78,.15);color:var(--gold);}
  .reading .dot{background:#1a2432;color:var(--muted);} .other .dot{background:#1a2432;color:var(--muted);}
  .it.done .t{color:var(--muted);text-decoration:line-through;} .it .done-badge{margin-left:auto;color:var(--green);font-size:12px;}
  .it .tag{margin-left:auto;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;}
  .empty{color:var(--muted);text-align:center;padding:50px 0;}
  .foot{color:var(--muted);font-size:12px;margin-top:20px;text-align:center;}
</style></head><body><div class="wrap">
<header><h1>J.A.R.V.I.S · <span>Coursera</span></h1><div class="status" id="status">connecting…</div></header>
<div id="list"><div class="empty">Loading your course progress…</div></div>
<div class="foot">Live from Jarvis on 127.0.0.1:__PORT__ · progress and completion are read from Coursera's own markers · updates every few seconds</div>
</div><script>
const ICON={lecture:"▶",graded:"✎",reading:"‖",activity:"•",home:"⌂",other:"•"};
function esc(s){return (s||"").replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
async function load(){
  let d; try{ d=await (await fetch("/progress",{cache:"no-store"})).json(); }catch(e){ document.getElementById("status").innerHTML="Jarvis offline"; return; }
  const st=document.getElementById("status");
  st.className="status"+(d.active?"":" off");
  st.innerHTML=d.active?'<b>&#9679;</b> a Coursera tab is connected':'<b>&#9679;</b> no Coursera tab open';
  const list=document.getElementById("list");
  if(!d.courses.length){ list.innerHTML='<div class="empty">No courses tracked yet. Open a Coursera lecture with the extension running.</div>'; return; }
  list.innerHTML=d.courses.map(c=>{
    const pct=c.total?Math.round(c.done/c.total*100):0;
    const items=(c.items||[]).map(i=>{
      const k=i.type||"other"; const done=i.done;
      return `<div class="it ${k} ${done?"done":""}"><span class="dot">${ICON[k]||"•"}</span>`+
        `<span class="t">${esc(i.title)||"(untitled)"}</span>`+
        (done?'<span class="done-badge">&#10003; done</span>':`<span class="tag">${k}</span>`)+`</div>`;
    }).join("");
    return `<div class="course"><h2>${esc(c.name)}</h2>`+
      `<div class="sub">${c.at?("updated "+esc(c.at)):""}</div>`+
      `<div class="count"><b>${c.done}</b> of <b>${c.total}</b> lecture videos watched &middot; ${c.total-c.done} remaining</div>`+
      `<div class="bar"><i style="width:${pct}%"></i></div>`+
      (items?`<div class="items">${items}</div>`:"")+`</div>`;
  }).join("");
}
load(); setInterval(load, 4000);
</script></body></html>"""

BRIDGE = Bridge()
