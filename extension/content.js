// Jarvis Coursera Link: runs on coursera.org.
//  - Reports the open lesson to Jarvis and carries out his commands (play, pause, next / previous video, speed,
//    autoplay on/off). Talks to Jarvis through background.js.
//  - Autoplay ("binge mode"): starts each lecture video and, when it ends, opens the next video in the course.
//    Readings and other ungraded items in between are skipped; it stops at graded items (quizzes, assignments)
//    and tells you. It never answers, submits or marks anything as complete.
(() => {
  if (window.__jarvisCoursera) return;
  window.__jarvisCoursera = true;

  const DEFAULTS = { binge: false, speed: 1 };
  const SEEK = "jarvisSeek";          // sessionStorage: walking item by item to the next video (1 / -1)
  const HOPS = "jarvisSeekHops";
  const PLAY = "jarvisPlayOnArrive";  // a "next video" command: start the video once it loads
  const NEXT_RE = /^(go to )?next( item)?$|^next item\b|^go to next item/i;
  const PREV_RE = /^(go to )?previous( item)?$|^previous item\b|^go to previous item/i;

  let settings = { ...DEFAULTS };
  const events = [];
  const acks = [];
  let activeAt = Date.now();
  let weMuted = false;
  let page = fresh(location.href);
  const watched = new WeakSet();

  chrome.storage.local.get(DEFAULTS, (s) => (settings = { ...settings, ...s }));
  chrome.storage.onChanged.addListener((ch) => {
    for (const k of Object.keys(DEFAULTS)) if (ch[k]) settings[k] = ch[k].newValue;
    const v = video();
    if (ch.speed && v) v.playbackRate = settings.speed;   // changed from the toolbar popup
  });
  const save = (patch) => {
    Object.assign(settings, patch);
    chrome.storage.local.set(patch);
  };

  function fresh(url) {
    return { url, arrived: Date.now(), autoStarted: false, advanced: false, userPaused: false, walked: false, rateSet: false };
  }

  // ---------------------------------------------------------------- reading the page
  function info() {
    const m = location.pathname.match(/^\/learn\/([^/]+)(?:\/([^/]+)(?:\/([^/]+))?)?/);
    if (!m) return { course: "", kind: "other", id: "" };
    const t = m[2] || "";
    let kind = "other";
    if (t === "lecture") kind = "lecture";
    else if (t === "supplement") kind = "reading";
    else if (/^(quiz|exam|programming|peer|gradedLab|gradedLti|assignment-submission)$/.test(t)) kind = "graded";
    else if (t === "home") kind = "home";
    else if (t) kind = "activity";
    return { course: m[1], kind, id: m[3] || "" };
  }

  function title() {
    const h = document.querySelector("main h1, h1");
    const t = (h && h.textContent.trim()) || document.title.replace(/\s*\|\s*Coursera\s*$/i, "");
    return t.slice(0, 120);
  }

  function courseName() {
    for (const sel of ['nav[aria-label*="readcrumb"] a', '[data-e2e="course-name"]', '[class*="CourseName"]']) {
      const el = document.querySelector(sel);
      if (el && el.textContent.trim()) return el.textContent.trim().slice(0, 100);
    }
    return "";
  }

  const video = () => document.querySelector("video");

  function visible(el) {
    return el && el.offsetParent !== null && !el.disabled && el.getAttribute("aria-disabled") !== "true";
  }

  function findButton(re) {
    for (const el of document.querySelectorAll('button, a, [role="button"]')) {
      const label = (el.getAttribute("aria-label") || el.textContent || "").trim();
      if (label.length < 40 && re.test(label) && visible(el)) return el;
    }
    return null;
  }

  // The course outline's item links, in page order: /learn/<course>/<type>/<id>/...
  function itemLinks(course) {
    const seen = new Set();
    const out = [];
    for (const a of document.querySelectorAll(`a[href*="/learn/${course}/"]`)) {
      const m = (a.getAttribute("href") || "").match(/\/learn\/[^/]+\/([^/]+)\/([^/?#]+)/);
      if (!m || m[1] === "home" || seen.has(m[2])) continue;
      seen.add(m[2]);
      out.push({ a, type: m[1], id: m[2] });
    }
    return out;
  }

  // Coursera's outline labels read "Video, <title>, Completed, 11 m" — strip the type prefix and the status/
  // duration suffix to get a clean title for announcements and the dashboard.
  function cleanLabel(s) {
    s = (s || "").replace(/\s+/g, " ").trim();
    s = s.replace(/^(Video|Reading|Lecture|Graded Assignment|Assignment|Practice Quiz|Quiz|Exam|Discussion Prompt|Ungraded (?:App|Plugin|Lab)|Peer-graded Assignment|Programming Assignment|Lab)\s*,\s*/i, "");
    s = s.replace(/\s*,\s*(Completed|Not completed|Started|Locked|In progress|Due\b)[^]*$/i, "");
    s = s.replace(/\s*,\s*\d+\s*(?:m|min|mins|minutes|h|hour|hours|hr|hrs|sec|seconds)\b[^]*$/i, "");
    return s.trim();
  }
  const linkTitle = (a) => cleanLabel(a.getAttribute("aria-label") || a.textContent || "").slice(0, 100);
  // "Completed" in the label means done — but "Not completed" does not.
  const isDone = (s) => /\bcompleted\b/i.test(s || "") && !/\bnot\s+completed\b/i.test(s || "");

  // The outline with each item's completion state, for progress tracking. Completion is read from Coursera's own
  // markers (an aria-label or a "completed" class it sets), so it is best-effort and never written by us.
  function outline(course) {
    return itemLinks(course).map((l) => {
      const row = l.a.closest('li, [role="listitem"]') || l.a.parentElement;
      const al = l.a.getAttribute("aria-label") || "";
      const done = isDone(al) ||
        !!(row && (row.querySelector('[class*="completed"], [class*="Completed"], [data-test*="completed"]') ||
                   isDone(row.getAttribute("aria-label"))));
      return { type: l.type, id: l.id, title: linkTitle(l.a), done };
    });
  }

  // Dismiss a blocking pop-up if one is in the way (cookie bars, "continue" nags, upgrade prompts). Conservative:
  // only inside a dialog/modal, only safe button labels, never anything that looks like an assessment or payment.
  const POPUP_OK = /^(no thanks|not now|maybe later|dismiss|close|got it|skip|continue|continue learning|accept all|accept|i understand|ok|okay)$/i;
  const POPUP_NO = /submit|pay|buy|purchase|upgrade|delete|unenroll|sign ?out|log ?out|start (quiz|exam|assignment)/i;
  function dismissPopups() {
    for (const dlg of document.querySelectorAll('[role="dialog"],[aria-modal="true"],[class*="modal"],[class*="Modal"],[class*="Dialog"]')) {
      if (!visible(dlg)) continue;
      if (/quiz|assignment|submit|graded|exam/i.test(dlg.textContent || "")) continue;   // leave assessments alone
      for (const el of dlg.querySelectorAll('button,[role="button"],a')) {
        const lab = (el.getAttribute("aria-label") || el.textContent || "").trim();
        if (!visible(el) || lab.length > 24 || POPUP_NO.test(lab)) continue;
        if (POPUP_OK.test(lab) || el.getAttribute("aria-label") === "Close") {
          el.click();
          events.push("Dismissed a pop-up (" + (lab || "close").slice(0, 20) + ").");
          return true;
        }
      }
    }
    return false;
  }

  // ---------------------------------------------------------------- doing things
  async function play(v) {
    if (weMuted) {
      v.muted = false;
      weMuted = false;
    }
    try {
      await v.play();
      return "Playing.";
    } catch (e) {
      // Chrome won't start sound on a page you haven't clicked yet; muted playback is always allowed.
      try {
        v.muted = true;
        weMuted = true;
        await v.play();
        events.push("Chrome blocked the sound, so the video is playing muted. Click the speaker icon in the player once; after that videos play with sound.");
        return "Playing muted. Chrome wants one click on the page before it allows sound.";
      } catch (e2) {
        return "";
      }
    }
  }

  // Go to the next (1) or previous (-1) lecture video. Returns the title, "" if unknown, or null if impossible.
  function goVideo(dir) {
    const { course, id, kind } = info();
    const links = itemLinks(course);
    const i = links.findIndex((l) => l.id === id);
    if (i >= 0) {
      let skippedGraded = 0;
      for (let j = i + dir; j >= 0 && j < links.length; j += dir) {
        if (links[j].type === "lecture") {
          if (skippedGraded && dir > 0) events.push(`I skipped past ${skippedGraded} graded item${skippedGraded > 1 ? "s" : ""}; they're still waiting for you in the course.`);
          sessionStorage.setItem(PLAY, String(Date.now()));
          links[j].a.click();
          return linkTitle(links[j].a);
        }
        if (/^(quiz|exam|programming|peer|gradedLab|gradedLti|assignment-submission)$/.test(links[j].type)) skippedGraded++;
      }
    }
    if (kind === "home" && dir > 0) {
      const first = links.find((l) => l.type === "lecture");
      if (first) {
        sessionStorage.setItem(PLAY, String(Date.now()));
        first.a.click();
        return linkTitle(first.a);
      }
    }
    // The outline on this page doesn't go that far: step through items with the page's own Next button.
    const btn = findButton(dir > 0 ? NEXT_RE : PREV_RE);
    if (btn) {
      sessionStorage.setItem(SEEK, String(dir));
      sessionStorage.setItem(PLAY, String(Date.now()));
      btn.click();
      return "";
    }
    return null;
  }

  // Arrived somewhere while walking to the next video: keep going, or stop and say why.
  function walk() {
    const dir = Number(sessionStorage.getItem(SEEK) || 0);
    if (!dir || page.walked || Date.now() - page.arrived < 2000) return;
    page.walked = true;
    const { kind } = info();
    const hops = Number(sessionStorage.getItem(HOPS) || 0) + 1;
    sessionStorage.setItem(HOPS, String(hops));
    const stop = (msg) => {
      sessionStorage.removeItem(SEEK);
      sessionStorage.removeItem(HOPS);
      if (msg) {
        sessionStorage.removeItem(PLAY);
        events.push(msg);
      }
    };
    if (kind === "lecture") return stop();
    if (kind === "graded") return stop(`Next up is a graded item, "${title()}". I've stopped here. Say "next video" to skip past it.`);
    if (hops > 15) return stop("I couldn't find another video after looking through 15 items.");
    if (goVideo(dir) === null) stop("That was the last video I can find in this course.");
  }

  async function run(c) {
    const v = video();
    const isLecture = info().kind === "lecture";
    const noVideo = () => (isLecture ? "The video player hasn't loaded yet." : `This page isn't a video, it's "${title()}".`);
    switch (c.cmd) {
      case "play":
        if (!v) return noVideo();
        page.userPaused = false;
        return (await play(v)) || "Chrome wouldn't start the video.";
      case "pause":
        if (!v) return noVideo();
        v.pause();
        page.userPaused = true;
        return "Paused.";
      case "next":
      case "previous": {
        const t = goVideo(c.cmd === "next" ? 1 : -1);
        if (t === null) return c.cmd === "next" ? "There's no next video on this page." : "There's no previous video on this page.";
        return t ? `Opening "${t}".` : `Going to the ${c.cmd} video.`;
      }
      case "restart":
        if (!v) return noVideo();
        v.currentTime = 0;
        return "Back to the start.";
      case "seek": {
        if (!v) return noVideo();
        const s = Number(c.value) || 10;
        v.currentTime = Math.max(0, Math.min((v.duration || 1e9) - 0.5, v.currentTime + s));
        return s > 0 ? `Forward ${s} seconds.` : `Back ${-s} seconds.`;
      }
      case "speed": {
        const n = parseFloat(c.value);
        if (!(n >= 0.25 && n <= 3)) return "The speed has to be between 0.25 and 3.";
        save({ speed: n });
        if (v) v.playbackRate = n;
        return `Speed ${n}x.`;
      }
      case "binge": {
        const on = c.value !== "off";
        save({ binge: on });
        if (on && v && v.paused && !v.ended && isLecture) {
          page.userPaused = false;
          page.autoStarted = true;
          await play(v);
        }
        return on ? "Autoplay on. I'll play each video and go on to the next one." : "Autoplay off.";
      }
      default:
        return "";
    }
  }

  // ---------------------------------------------------------------- the loop
  function tick() {
    if (location.href !== page.url) page = fresh(location.href);
    const { kind } = info();
    const v = video();
    if (document.visibilityState === "visible" || (v && !v.paused)) activeAt = Date.now();

    // during autoplay, clear pop-ups that would block playback or navigation
    if (settings.binge && Date.now() - page.arrived > 1500) dismissPopups();

    if (kind === "lecture" && v) {
      if (!watched.has(v)) {
        watched.add(v);
        // A pause that isn't the end of the video is the user's (or Jarvis's) choice: don't restart it.
        v.addEventListener("pause", () => {
          if (!v.ended && !(v.duration && v.currentTime >= v.duration - 1)) page.userPaused = true;
        });
        v.addEventListener("play", () => (page.userPaused = false));
        // Move on the moment the video ends: in a background tab Chrome slows timers to once a minute,
        // but events still arrive at once.
        v.addEventListener("ended", () => {
          if (!settings.binge || page.advanced || video() !== v) return;
          page.advanced = true;
          if (goVideo(1) === null) events.push("That was the last video I can find in this course.");
        });
      }
      if (!page.rateSet && v.readyState >= 1 && settings.speed && settings.speed !== 1) {
        v.playbackRate = settings.speed;
        page.rateSet = true;
      }
      const wantPlay = settings.binge || Date.now() - Number(sessionStorage.getItem(PLAY) || 0) < 60000;
      if (wantPlay && !page.autoStarted && !page.userPaused && v.paused && !v.ended && v.readyState >= 2
          && Date.now() - page.arrived > 1200) {
        page.autoStarted = true;
        sessionStorage.removeItem(PLAY);
        play(v);
      }
      const atEnd = v.ended || (v.duration && isFinite(v.duration) && v.paused && v.currentTime >= v.duration - 0.6);
      if (settings.binge && atEnd && !page.advanced) {
        page.advanced = true;
        const here = location.href;
        setTimeout(() => {
          // Coursera's own "up next" may already have moved on
          if (location.href === here && settings.binge && goVideo(1) === null)
            events.push("That was the last video I can find in this course.");
        }, 2500);
      }
    }
    walk();
    sync();
  }

  let busy = false;
  let timer = null;
  let lastOutline = 0;
  function sync() {
    if (busy) return;
    const v = video();
    const inf = info();
    const state = {
      url: location.href, course: inf.course, courseName: courseName(), kind: inf.kind, title: title(),
      playing: !!(v && !v.paused && !v.ended), time: v ? v.currentTime : 0,
      duration: v && isFinite(v.duration) ? v.duration : 0, speed: settings.speed, binge: settings.binge,
      visible: document.visibilityState === "visible", activeAt,
      events: events.splice(0), acks: acks.splice(0),
    };
    // the outline is bigger, so send it at most every 4s and only where the page actually lists it
    if (inf.course && Date.now() - lastOutline > 4000) {
      const ol = outline(inf.course);
      if (ol.length) { state.outline = ol; lastOutline = Date.now(); }
    }
    busy = true;
    try {
      chrome.runtime.sendMessage({ type: "sync", state }, async (reply) => {
        busy = false;
        if (chrome.runtime.lastError || !reply || reply.offline) return; // Jarvis isn't running: autoplay still works
        const cmds = reply.commands || [];
        for (const c of cmds) {
          let r;
          try {
            r = await run(c);
          } catch (e) {
            r = "Something went wrong: " + e.message;
          }
          acks.push({ id: c.id, result: r || "" });
        }
        if (cmds.length) sync(); // answer straight away so Jarvis can say what happened
      });
    } catch (e) {
      clearInterval(timer); // the extension was reloaded; this copy of the script is orphaned
    }
  }

  timer = setInterval(tick, 700);
})();
