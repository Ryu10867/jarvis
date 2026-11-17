"""Working with files by voice: convert images to another type, shrink or zip files, put images into one PDF or
PowerPoint, and research a topic and build a presentation about it.

Which files? Whatever the user means:
    "this" / "these" / "the selected files"   -> the selection in the File Explorer window in front (Finder on a Mac)
    "the last 4 images", "the 4 images from the right", "the first two photos"
                                              -> by position, in the order that Explorer window shows them
    "cat.jpg, dog and sunset"                  -> by name, looked up in that window's folder, then Desktop, Downloads,
                                                 Pictures and Documents (spoken names are matched loosely)
Results are saved next to the originals, or in a folder the user names ("save it on the desktop"). Nothing is ever
overwritten: a clash gets " (2)" added."""
import datetime as dt
import difflib
import functools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from .config import known_folder

log = logging.getLogger("jarvis.files")
WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"
HOME = Path.home()

IMAGE_EXT = {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".ico", ".heic", ".avif"}
OFFICE_EXT = {".doc", ".docx", ".rtf", ".odt", ".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods"}
FORMATS = {                                   # spoken name -> (Pillow format, extension)
    "png": ("PNG", ".png"), "jpg": ("JPEG", ".jpg"), "jpeg": ("JPEG", ".jpg"), "webp": ("WEBP", ".webp"),
    "bmp": ("BMP", ".bmp"), "bitmap": ("BMP", ".bmp"), "gif": ("GIF", ".gif"), "tif": ("TIFF", ".tiff"),
    "tiff": ("TIFF", ".tiff"), "ico": ("ICO", ".ico"), "icon": ("ICO", ".ico"), "pdf": ("PDF", ".pdf"),
}
UA = "Jarvis-assistant/1.1 (offline voice assistant; https://github.com/Ryu10867/jarvis)"


class FileError(Exception):
    """Something to say back to the user, in plain words."""


# ------------------------------------------------------------------ where files are, and which ones the user means
def _folder(name: str) -> Path | None:
    n = name.lower().strip().strip("\"'").rstrip("\\/")
    n = re.sub(r"^(?:the|my)\s+|\s+folder$", "", n)
    if n in ("download", "downloads"):
        return HOME / "Downloads"
    if n in ("home", "user folder"):
        return HOME
    for cand in (n, n + "s"):
        p = known_folder(cand)
        if p:
            return p
    p = Path(os.path.expandvars(name.strip().strip("\"'"))).expanduser()
    if p.is_absolute():
        return p
    for base in (_explorer_folder(), HOME / "Desktop", HOME / "Documents", HOME):     # "save it in Projects"
        if base:
            for child in (base / name.strip(), base / name.strip().title()):
                if child.is_dir():
                    return child
    return None


def destination(save_to: str, first: Path) -> Path:
    """The folder a result goes in: the one the user named, else next to the original."""
    if save_to and save_to.strip().lower() not in ("here", "same folder", "same place", "the same folder", "there"):
        d = _folder(save_to)
        if not d:
            raise FileError(f"I couldn't find a folder called {save_to}.")
        d.mkdir(parents=True, exist_ok=True)
        return d
    return first.parent


def unique(path: Path) -> Path:
    """Never overwrite: 'photo.png' -> 'photo (2).png' if it already exists."""
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} {int(time.time())}{path.suffix}")


def _logical_key():
    """Explorer's own name order ('img2' before 'img10')."""
    if WINDOWS:
        import ctypes
        cmp = ctypes.windll.shlwapi.StrCmpLogicalW
        cmp.argtypes, cmp.restype = [ctypes.c_wchar_p, ctypes.c_wchar_p], ctypes.c_int
        return functools.cmp_to_key(lambda a, b: cmp(a.name, b.name))
    return lambda p: [int(s) if s.isdigit() else s.lower() for s in re.split(r"(\d+)", p.name)]


def _sorted_like(paths: list[Path], sort_columns: str = "") -> list[Path]:
    """In the order the Explorer window shows them (its first sort column: name, date, size or type)."""
    key, reverse = _logical_key(), False
    m = re.search(r"prop:(-?)([\w.]+)", sort_columns or "")
    if m:
        reverse, prop = m.group(1) == "-", m.group(2).lower()
        if "datemodified" in prop or "itemdate" in prop or "datetaken" in prop:
            key = lambda p: p.stat().st_mtime                      # noqa: E731
        elif "datecreated" in prop:
            key = lambda p: p.stat().st_ctime                      # noqa: E731
        elif prop.endswith(".size"):
            key = lambda p: p.stat().st_size                       # noqa: E731
        elif "itemtype" in prop:
            name_key = _logical_key()
            key = lambda p: (p.suffix.lower(), name_key(p))        # noqa: E731
    try:
        return sorted(paths, key=key, reverse=reverse)
    except Exception:
        return sorted(paths, key=lambda p: p.name.lower(), reverse=reverse)


def _explorer_state() -> dict:
    """{'folder': Path|None, 'selected': [Path], 'sort': str} for the File Explorer window in front (Windows), or the
    front Finder window (Mac). The front-most Explorer window is used even if Jarvis's own orb has focus."""
    if MAC:
        return _finder_state()
    if not WINDOWS:
        return {"folder": None, "selected": [], "sort": ""}
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    order, titles, desktop_front = [], {}, False
    fg = user32.GetForegroundWindow()
    cls = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(fg, cls, 64)
    if cls.value in ("Progman", "WorkerW"):
        desktop_front = True

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):                                   # top-level windows, front to back
        c = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, c, 64)
        if c.value == "CabinetWClass" and user32.IsWindowVisible(hwnd):
            t = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, t, 512)
            order.append(int(hwnd))
            titles[int(hwnd)] = t.value
        return True
    user32.EnumWindows(each, 0)

    import comtypes
    import comtypes.client
    comtypes.CoInitialize()
    try:
        shell = comtypes.client.CreateObject("Shell.Application", dynamic=True)
        if desktop_front:                                # files selected on the desktop itself
            sel = _desktop_selection(shell)
            if sel:
                return {"folder": HOME / "Desktop", "selected": sel, "sort": ""}
        views = []
        wins = shell.Windows()
        for i in range(wins.Count):
            try:
                w = wins.Item(i)
                if w is None:
                    continue
                hwnd = int(w.HWND)
                if hwnd not in titles:
                    continue
                doc = w.Document
                folder = str(doc.Folder.Self.Path)
                items = doc.SelectedItems()
                sel = [Path(str(items.Item(j).Path)) for j in range(items.Count)]
                try:
                    sort = str(doc.SortColumns)
                except Exception:
                    sort = ""
                name = str(doc.Folder.Self.Name)
                views.append((order.index(hwnd), titles[hwnd].startswith(name), bool(sel), folder, sel, sort))
            except Exception as e:                       # a window that is closing, a search result page ...
                log.debug("explorer window skipped: %s", e)
        if not views:
            return {"folder": None, "selected": [], "sort": ""}
        # front window first; within it (Windows 11 tabs share one window) the tab whose name is in the title
        views.sort(key=lambda v: (v[0], not v[1], not v[2]))
        _, _, _, folder, sel, sort = views[0]
        f = Path(folder) if folder and not folder.startswith("::") else None
        return {"folder": f if f and f.is_dir() else None, "selected": [p for p in sel if p.exists()], "sort": sort}
    finally:
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass


def _desktop_selection(shell) -> list[Path]:
    try:                                                 # the desktop's own view: ShellWindows.FindWindowSW(desktop)
        import ctypes
        hwnd = ctypes.c_long(0)
        desk = shell.Windows().FindWindowSW(0, 0, 8, ctypes.byref(hwnd), 1)
        items = desk.Document.SelectedItems()
        return [Path(str(items.Item(j).Path)) for j in range(items.Count)]
    except Exception as e:
        log.debug("desktop selection unavailable: %s", e)
        return []


def _finder_state() -> dict:
    script = ('tell application "Finder"\nset out to ""\ntry\nset out to POSIX path of (target of front window as alias)\n'
              'end try\nset out to out & linefeed\nrepeat with f in (selection as alias list)\n'
              'set out to out & POSIX path of f & linefeed\nend repeat\nreturn out\nend tell')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        lines = r.stdout.split("\n")
        folder = Path(lines[0]) if lines and lines[0].strip() else None
        sel = [Path(x) for x in lines[1:] if x.strip()]
        return {"folder": folder, "selected": [p for p in sel if p.exists()], "sort": ""}
    except Exception as e:
        log.debug("Finder state unavailable: %s", e)
        return {"folder": None, "selected": [], "sort": ""}


def _explorer_folder() -> Path | None:
    try:
        return _explorer_state()["folder"]
    except Exception:
        return None


KIND_EXT = {"image": IMAGE_EXT, "photo": IMAGE_EXT, "picture": IMAGE_EXT, "pic": IMAGE_EXT, "screenshot": IMAGE_EXT,
            "pdf": {".pdf"}, "document": OFFICE_EXT | {".pdf", ".txt"}, "file": None}
NUMS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "both": 2, "couple of": 2}
SELECTED = re.compile(r"^(?:this|that|it|these|those|them|they|selected|the selected(?: \w+)?|selection|the selection|"
                      r"(?:this|that|these|those|the selected|the highlighted|highlighted) (?:\d+ |\w+ )?(?:files?|images?|"
                      r"photos?|pictures?|pics|documents?|items?|screenshots?|ones?|folders?)|"
                      r"(?:all )?(?:the |my )?(?:files?|images?|photos?|pictures?|pics|screenshots?|documents?|ones?) "
                      r"(?:(?:that |which )?i(?:'ve| have| just)? (?:just )?)?(?:selected|picked|chose|chosen|highlighted|marked|"
                      r"clicked(?: on)?))$")
POSITION = re.compile(r"^(?:all |the )?(?:(first|last|top|bottom|latest|newest|oldest|leftmost|rightmost)\s+)?"
                      r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|both|couple of)?\s*"
                      r"(images?|photos?|pictures?|pics|screenshots?|files?|pdfs?|documents?)"
                      r"(?:\s+(?:from|on|at|of)\s+(?:the\s+)?(left|right|start|end|beginning|top|bottom|front|back))?"
                      r"(?:\s+(?:in|from)\s+(?:this|the|my)\s+(?:folder|window))?$")


def resolve_files(spec: str, want: str = "") -> tuple[list[Path], str]:
    """Turn what the user said into real files. Returns (files, how) where `how` explains the choice in a few words
    (said back to the user so a wrong guess is easy to spot). `want` = 'image' keeps only images."""
    s = re.sub(r"\s+", " ", (spec or "selected").strip().lower().strip(".,")).strip()
    s = re.sub(r"^(?:of |from |with |for )", "", s)
    state = _explorer_state()
    allowed = KIND_EXT.get(want)

    def keep(paths):
        return [p for p in paths if p.is_dir() or allowed is None or p.suffix.lower() in allowed]

    if SELECTED.match(s) or s in ("", "these images", "these files"):
        sel = keep(state["selected"])
        if not sel:
            where = "in File Explorer" if WINDOWS else "in Finder"
            raise FileError(f"I don't see any {want or 'file'}s selected {where}, sir. Select them first, or tell me "
                            f"their names.")
        return _sorted_like(sel, state["sort"]), f"the {len(sel)} selected"

    m = POSITION.match(s)
    if m:
        which, count, kind, side = m.group(1), m.group(2), m.group(3), m.group(4)
        n = int(count) if count and count.isdigit() else NUMS.get(count or "", 0)
        kind = re.sub(r"s$", "", kind)
        exts = KIND_EXT.get(kind) or allowed
        if state["selected"] and not which and not side and len(keep(state["selected"])) == n:
            sel = keep(state["selected"])                      # "these 4 images" said with 4 selected
            return _sorted_like(sel, state["sort"]), f"the {n} selected"
        folder = state["folder"]
        if not folder:
            raise FileError("Open the folder in File Explorer first, sir, so I know which images you mean.")
        files = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
                 and (exts is None or p.suffix.lower() in exts)]
        if not files:
            raise FileError(f"There are no {kind}s in {folder.name}, sir.")
        files = _sorted_like(files, state["sort"])
        if which in ("latest", "newest"):
            files, which = sorted(files, key=lambda p: p.stat().st_mtime), "last"
        elif which == "oldest":
            files, which = sorted(files, key=lambda p: p.stat().st_mtime), "first"
        last = which in ("last", "bottom", "rightmost") or side in ("right", "end", "bottom", "back")
        n = n or len(files)
        chosen = files[-n:] if last else files[:n]
        return chosen, f"the {'last' if last else 'first'} {len(chosen)} {kind}s in {folder.name}"

    names = [x.strip(" \"'") for x in re.split(r",\s*|\s+and\s+|\s*&\s*", s) if x.strip(" \"'")]
    names = [re.sub(r"^(?:the |my )?(?:(?:image|photo|picture|file|document)s? (?:called |named )?)?", "", x)
             for x in names]
    found = [find_named(n, state["folder"], allowed) for n in names]
    missing = [n for n, f in zip(names, found) if not f]
    if missing:
        raise FileError(f"I couldn't find {', '.join(missing)}, sir.")
    return found, "the files you named"


def _norm(s: str) -> str:
    s = s.lower().replace(" dot ", ".").replace("image ", "img ").replace("screen shot", "screenshot")
    return re.sub(r"[^a-z0-9]", "", s)


def find_named(name: str, folder: Path | None = None, allowed: set | None = None) -> Path | None:
    """Loose lookup of a spoken file name: exact name, name without extension, letters and digits only, then a close
    match ('image 2034' finds IMG_2034.jpg). Looks in the open Explorer folder, then the usual places."""
    name = name.strip().replace(" dot ", ".")
    p = Path(os.path.expandvars(name)).expanduser()
    if p.is_absolute() and p.exists():
        return p
    places = [folder] if folder else []
    places += [HOME / "Desktop", known_folder("desktop"), HOME / "Downloads", known_folder("pictures"),
               known_folder("documents"), HOME / "Pictures", HOME / "Documents"]
    seen, cands = set(), []
    for d in places:
        if not d or not d.is_dir() or d in seen:
            continue
        seen.add(d)
        try:
            cands += [c for c in d.iterdir() if allowed is None or c.is_dir() or c.suffix.lower() in allowed]
        except OSError:
            continue
    if not cands:
        return None
    want, want_n = name.lower(), _norm(name)
    digits = re.findall(r"\d+", name)
    for test in (lambda c: c.name.lower() == want, lambda c: c.stem.lower() == want,
                 lambda c: _norm(c.name) == want_n, lambda c: _norm(c.stem) == want_n,
                 lambda c: bool(want_n) and _norm(c.stem).startswith(want_n),
                 lambda c: bool(digits) and all(d in c.stem for d in digits) and len(want_n) > 2
                 and (want_n.strip("0123456789") in _norm(c.stem) or not want_n.strip("0123456789"))):
        hits = [c for c in cands if test(c)]
        if hits:
            return hits[0]
    best = difflib.get_close_matches(want_n, [_norm(c.stem) for c in cands], n=1, cutoff=0.72)
    if best:
        return next(c for c in cands if _norm(c.stem) == best[0])
    return None


# ------------------------------------------------------------------ images
def _pil():
    try:
        from PIL import Image, ImageOps
        return Image, ImageOps
    except ImportError:
        raise FileError("I need the Pillow library for images: run  pip install Pillow  in Jarvis's environment.")


def _load(path: Path):
    Image, ImageOps = _pil()
    try:
        im = Image.open(path)
        im.load()
    except Exception:
        raise FileError(f"I can't read {path.name} as an image.")
    return ImageOps.exif_transpose(im) or im                      # phone photos the right way up


def _flat(im, background=(255, 255, 255)):
    """RGB without transparency (for JPEG, BMP and PDF), placed on white."""
    Image, _ = _pil()
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, background)
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB") if im.mode != "RGB" else im


def _save_image(im, out: Path, fmt: str):
    if fmt == "JPEG":
        _flat(im).save(out, "JPEG", quality=92, optimize=True, progressive=True)
    elif fmt == "BMP":
        _flat(im).save(out, "BMP")
    elif fmt == "PDF":
        _flat(im).save(out, "PDF", resolution=150.0)
    elif fmt == "ICO":
        side = min(256, max(im.size))
        icon = im.convert("RGBA")
        icon.thumbnail((side, side))
        sizes = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side]
        icon.save(out, "ICO", sizes=sizes)
    elif fmt == "WEBP":
        im.save(out, "WEBP", quality=90, method=5)
    elif fmt == "PNG":
        im.save(out, "PNG", optimize=True)
    elif fmt == "GIF":
        (im if im.mode in ("P", "L") else _flat(im).quantize(256)).save(out, "GIF")
    else:
        im.save(out, fmt)


def _size(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(1, round(n / 1024))} KB"


def _where(folder: Path) -> str:
    for label, p in (("on the Desktop", known_folder("desktop")), ("on the Desktop", HOME / "Desktop"),
                     ("in Downloads", HOME / "Downloads"), ("in Documents", known_folder("documents")),
                     ("in Pictures", known_folder("pictures"))):
        if p and folder == p:
            return label
    return f"in {folder.name}"


# ------------------------------------------------------------------ the actions
def convert(files: list[Path], to: str, save_to: str = "", name: str = "") -> str:
    to = to.lower().strip(". ").replace("jpeg", "jpg")
    if to not in FORMATS:
        raise FileError(f"I can convert images to PNG, JPG, WebP, BMP, GIF, TIFF, ICO or PDF, sir, not {to}.")
    fmt, ext = FORMATS[to]
    images = [p for p in files if p.suffix.lower() in IMAGE_EXT]
    docs = [p for p in files if p.suffix.lower() in OFFICE_EXT]
    if fmt == "PDF" and len(images) > 1 and not docs:
        return make_pdf(images, save_to, name)                    # several images "to PDF" means one PDF of them
    if not images and not docs:
        raise FileError("Those aren't images or Office documents, sir, so I can't convert them.")
    if docs and fmt != "PDF":
        raise FileError("Word, PowerPoint and Excel files can only be turned into PDF, sir.")
    out_dir = destination(save_to, files[0])
    made = []
    for p in images:
        if p.suffix.lower() == ext or (ext == ".jpg" and p.suffix.lower() in (".jpeg", ".jfif")):
            continue
        out = unique(out_dir / (p.stem + ext))
        _save_image(_load(p), out, fmt)
        made.append(out)
    for p in docs:
        made.append(office_to_pdf(p, out_dir))
    if not made:
        return f"{'That is' if len(files) == 1 else 'Those are'} already {to.upper()}, sir."
    if len(made) == 1:
        return f"Done, sir. Saved {made[0].name} {_where(made[0].parent)}."
    return f"Done, sir. Converted {len(made)} files to {to.upper()}, saved {_where(out_dir)}."


def office_to_pdf(path: Path, out_dir: Path) -> Path:
    """Word, PowerPoint or Excel to PDF through Office itself (Windows, Office installed)."""
    if not WINDOWS:
        raise FileError("Turning Office files into PDF needs Microsoft Office on Windows, sir.")
    out = unique(out_dir / (path.stem + ".pdf"))
    ext = path.suffix.lower()
    import comtypes
    import comtypes.client
    comtypes.CoInitialize()
    app = None
    try:
        if ext in (".doc", ".docx", ".rtf", ".odt"):
            app = comtypes.client.CreateObject("Word.Application", dynamic=True)
            app.Visible = False
            doc = app.Documents.Open(str(path), False, True)
            doc.ExportAsFixedFormat(str(out), 17)
            doc.Close(False)
        elif ext in (".ppt", ".pptx", ".odp"):
            app = comtypes.client.CreateObject("PowerPoint.Application", dynamic=True)
            pres = app.Presentations.Open(str(path), True, False, False)
            pres.SaveAs(str(out), 32)
            pres.Close()
        else:
            app = comtypes.client.CreateObject("Excel.Application", dynamic=True)
            app.Visible = False
            app.DisplayAlerts = False
            wb = app.Workbooks.Open(str(path), 0, True)
            wb.ExportAsFixedFormat(0, str(out))
            wb.Close(False)
    except FileError:
        raise
    except Exception as e:
        log.warning("office export failed: %s", e)
        raise FileError(f"I couldn't turn {path.name} into a PDF. Is Microsoft Office installed, sir?")
    finally:
        try:
            if app is not None and ext not in (".ppt", ".pptx", ".odp"):
                app.Quit()
        except Exception:
            pass
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
    return out


def compress(files: list[Path], save_to: str = "", name: str = "", zip_it: bool = False) -> str:
    """Images are re-saved smaller (same type, next to the original as '<name> (compressed)'); anything else, or
    when a zip is asked for, goes into one .zip."""
    images = [p for p in files if p.is_file() and p.suffix.lower() in IMAGE_EXT - {".gif", ".ico"}]
    if zip_it or len(images) != len(files):
        return make_zip(files, save_to, name)
    Image, _ = _pil()
    out_dir = destination(save_to, files[0])
    before = after = 0
    made = []
    for p in images:
        im = _load(p)
        if max(im.size) > 2560:
            im.thumbnail((2560, 2560), Image.LANCZOS)
        ext = p.suffix.lower()
        out = unique(out_dir / f"{p.stem} (compressed){'.jpg' if ext in ('.jpeg', '.jfif', '.heic', '.avif') else ext}")
        if ext in (".png",):
            im.save(out, "PNG", optimize=True)
            if out.stat().st_size > 0.85 * p.stat().st_size:          # screenshots and flat art: fewer colours
                q = im.convert("RGBA").quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.FLOYDSTEINBERG)
                q.save(out, "PNG", optimize=True)
        elif ext == ".webp":
            im.save(out, "WEBP", quality=70, method=6)
        elif ext in (".bmp", ".tif", ".tiff"):
            out = unique(out.with_suffix(".jpg"))
            _flat(im).save(out, "JPEG", quality=75, optimize=True, progressive=True)
        else:
            _flat(im).save(out, "JPEG", quality=72, optimize=True, progressive=True)
        b, a = p.stat().st_size, out.stat().st_size
        if a >= b:                                                    # already as small as it gets
            out.unlink(missing_ok=True)
            continue
        before += b
        after += a
        made.append(out)
    if not made:
        return "Those images are already about as small as they get, sir. Shall I zip them instead?"
    saved = 100 - round(after * 100 / max(before, 1))
    what = made[0].name if len(made) == 1 else f"{len(made)} images"
    return f"Done, sir. {what}: {_size(before)} down to {_size(after)}, {saved} percent smaller, saved {_where(out_dir)}."


def make_zip(files: list[Path], save_to: str = "", name: str = "") -> str:
    out_dir = destination(save_to, files[0])
    base = name.strip() or (files[0].stem if len(files) == 1 else f"{files[0].parent.name or 'Files'} ({len(files)} files)")
    out = unique(out_dir / (re.sub(r'[\\/:*?"<>|]', "", base) + ".zip"))
    before = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in files:
            if p.is_dir():
                for f in p.rglob("*"):
                    if f.is_file() and f != out:
                        z.write(f, Path(p.name) / f.relative_to(p))
                        before += f.stat().st_size
            elif p.is_file():
                z.write(p, p.name)
                before += p.stat().st_size
    after = out.stat().st_size
    pct = 100 - round(after * 100 / max(before, 1))
    return (f"Done, sir. {out.name} is {_size(after)}" + (f", {pct} percent smaller than the originals" if pct > 2 else "")
            + f", saved {_where(out_dir)}.")


def _a4_page(im, dpi: int):
    """The image on a white A4 page (portrait or landscape to suit it), as large as fits with a margin."""
    Image, _ = _pil()
    w_in, h_in = (11.69, 8.27) if im.width > im.height else (8.27, 11.69)
    W, H, margin = int(w_in * dpi), int(h_in * dpi), int(0.35 * dpi)
    im = _flat(im)
    scale = min((W - 2 * margin) / im.width, (H - 2 * margin) / im.height)
    if scale < 1:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.LANCZOS)
    page = Image.new("RGB", (W, H), (255, 255, 255))
    page.paste(im, ((W - im.width) // 2, (H - im.height) // 2))
    return page


def make_pdf(files: list[Path], save_to: str = "", name: str = "", open_it: bool = True) -> str:
    images = [p for p in files if p.suffix.lower() in IMAGE_EXT]
    if not images:
        raise FileError("I can only put images into a PDF, sir. For a Word or PowerPoint file, ask me to convert it to PDF.")
    dpi = 200 if len(images) <= 20 else 150
    pages = [_a4_page(_load(p), dpi) for p in images]
    out_dir = destination(save_to, images[0])
    base = name.strip() or (images[0].stem if len(images) == 1 else f"{images[0].parent.name or 'Images'} ({len(images)} images)")
    out = unique(out_dir / (re.sub(r'[\\/:*?"<>|]', "", base) + ".pdf"))
    pages[0].save(out, "PDF", resolution=float(dpi), save_all=True, append_images=pages[1:])
    if open_it:
        open_file(out)
    return f"Done, sir. {out.name}, {len(pages)} page{'s' if len(pages) != 1 else ''}, saved {_where(out_dir)}" + \
           (" and opened." if open_it else ".")


# ------------------------------------------------------------------ PowerPoint
INK, PAPER, GOLD, MUTED = "0B0D12", "F2F4F8", "F2B84B", "9AA4B5"


def _pptx():
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
        from pptx.util import Emu, Inches, Pt
        return Presentation, RGBColor, Inches, Pt, Emu, PP_ALIGN, MSO_ANCHOR
    except ImportError:
        raise FileError("I need the python-pptx library for PowerPoint: run  pip install python-pptx  in Jarvis's environment.")


def _deck():
    Presentation, *_ = _pptx()
    from pptx.util import Inches
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)        # 16:9
    return prs


def _blank(prs, colour=INK):
    _, RGBColor, *_ = _pptx()
    s = prs.slides.add_slide(prs.slide_layouts[6])
    fill = s.background.fill
    fill.solid()
    fill.fore_color.rgb = RGBColor.from_string(colour)
    return s


def _text(slide, x, y, w, h, text, size, colour=PAPER, bold=False, font="Segoe UI", align=None, anchor=None):
    _, RGBColor, Inches, Pt, _, PP_ALIGN, MSO_ANCHOR = _pptx()
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor or MSO_ANCHOR.TOP
    lines = text if isinstance(text, list) else [text]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align or PP_ALIGN.LEFT
        r = p.add_run()
        r.text = line
        r.font.size, r.font.bold, r.font.name = Pt(size), bold, font
        r.font.color.rgb = RGBColor.from_string(colour)
    return box


def _bar(slide, x, y, w, h, colour=GOLD):
    from pptx.enum.shapes import MSO_SHAPE
    _, RGBColor, Inches, *_ = _pptx()
    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shp.fill.solid()
    shp.fill.fore_color.rgb = RGBColor.from_string(colour)
    shp.line.fill.background()
    return shp


def _picture(slide, path: Path, x, y, w, h):
    """Place an image inside a box, as large as fits, centred (never stretched)."""
    _, _, Inches, _, Emu, *_ = _pptx()
    im = _load(path)
    tmp = None
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff"):
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=".png")[1])
        im.save(tmp, "PNG")
    bw, bh = Inches(w), Inches(h)
    scale = min(bw / im.width, bh / im.height)
    pw, ph = int(im.width * scale), int(im.height * scale)
    slide.shapes.add_picture(str(tmp or path), Inches(x) + (bw - pw) // 2, Inches(y) + (bh - ph) // 2, Emu(pw), Emu(ph))
    if tmp:
        tmp.unlink(missing_ok=True)


def make_pptx(files: list[Path], save_to: str = "", name: str = "", open_it: bool = True) -> str:
    """One image per slide, as large as fits, on a dark slide with its name underneath."""
    images = [p for p in files if p.suffix.lower() in IMAGE_EXT]
    if not images:
        raise FileError("I can only make slides from images, sir. For a presentation about a topic, tell me the topic.")
    prs = _deck()
    for p in images:
        s = _blank(prs)
        _picture(s, p, 0.5, 0.45, 12.333, 6.15)
        _text(s, 0.5, 6.72, 12.333, 0.45, p.stem, 12, MUTED)
    out_dir = destination(save_to, images[0])
    base = name.strip() or f"{images[0].parent.name or 'Images'} ({len(images)} slides)"
    out = unique(out_dir / (re.sub(r'[\\/:*?"<>|]', "", base) + ".pptx"))
    prs.save(out)
    if open_it:
        open_file(out)
    return f"Done, sir. {out.name}, {len(images)} slide{'s' if len(images) != 1 else ''}, saved {_where(out_dir)}" + \
           (" and opened." if open_it else ".")


def open_file(path: Path):
    try:
        if WINDOWS:
            os.startfile(str(path))
        elif MAC:
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as e:
        log.warning("could not open %s: %s", path, e)


# ------------------------------------------------------------------ research + presentation
def _get(url: str, timeout: float = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _download(url: str | None) -> Path | None:
    if not url:
        return None
    import tempfile
    try:
        img = Path(tempfile.gettempdir()) / f"jarvis-ppt-{abs(hash(url)) % 10**9}{Path(urllib.parse.urlparse(url).path).suffix or '.jpg'}"
        if not img.exists():
            img.write_bytes(_get(url, timeout=20))
        _load(img)                                              # a real, readable picture?
        return img
    except Exception as e:
        log.debug("image %s skipped: %s", url, e)
        return None


def _article_images(api: str, title: str, skip: str | None = None, limit: int = 3) -> list[Path]:
    """A few more photographs from the article (no icons, logos, maps or diagrams drawn as SVG)."""
    try:
        q = json.loads(_get(api + urllib.parse.urlencode({
            "action": "query", "generator": "images", "titles": title, "gimlimit": 30, "prop": "imageinfo",
            "iiprop": "url|size|mime", "iiurlwidth": 1100, "format": "json"})))
        pages = (q.get("query") or {}).get("pages", {}).values()
        good = []
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            name = p.get("title", "").lower()
            if ii.get("mime") not in ("image/jpeg", "image/png") or min(ii.get("width", 0), ii.get("height", 0)) < 400:
                continue
            if re.search(r"logo|icon|flag|symbol|map|diagram|chart|graph|signature|commons-|wiki|edit|question", name):
                continue
            url = ii.get("thumburl") or ii.get("url")
            if url and (not skip or Path(urllib.parse.urlparse(url).path).name not in skip):
                good.append(url)
        return [p for p in (_download(u) for u in good[:limit + 2]) if p][:limit]
    except Exception as e:
        log.debug("article images unavailable: %s", e)
        return []


def research(topic: str) -> dict:
    """What Wikipedia says about a topic: {'title', 'url', 'text', 'image'(Path|None)}. Empty dict when offline."""
    api = "https://en.wikipedia.org/w/api.php?"
    try:
        q = json.loads(_get(api + urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": topic,
                                                          "srlimit": 1, "format": "json"})))
        hits = q.get("query", {}).get("search", [])
        if not hits:
            return {}
        title = hits[0]["title"]
        page = json.loads(_get(api + urllib.parse.urlencode({
            "action": "query", "prop": "extracts|pageimages|info", "explaintext": 1, "exsectionformat": "plain",
            "titles": title, "piprop": "thumbnail", "pithumbsize": 1280, "inprop": "url", "redirects": 1, "format": "json"})))
        info = next(iter(page["query"]["pages"].values()))
        text = info.get("extract", "")
        text = re.split(r"\n(?:See also|References|Notes|Further reading|External links|Bibliography)\n", text)[0]
        out = {"title": info.get("title", title), "url": info.get("fullurl", ""), "text": text.strip(), "image": None,
               "images": []}
        thumb = (info.get("thumbnail") or {}).get("source")
        out["image"] = _download(thumb)
        out["images"] = _article_images(api, info.get("title", title), skip=thumb)
        return out
    except Exception as e:
        log.warning("research failed for %r: %s", topic, e)
        return {}


OUTLINE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"}, "subtitle": {"type": "string"},
        "slides": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"},
            "bullets": {"type": "array", "items": {"type": "string"}},
            "highlight": {"type": "string"},
            "notes": {"type": "string"}}, "required": ["title", "bullets", "highlight", "notes"]}},
    },
    "required": ["title", "subtitle", "slides"],
}


def outline(topic: str, notes: str, slides: int, cfg: dict) -> dict:
    """Ask the local language model for the deck: titles, short bullets and speaker notes, as JSON."""
    llm = cfg.get("llm", {})
    url = llm.get("url", "http://127.0.0.1:11434/api/chat")
    source = (f"Use ONLY facts from these research notes (from Wikipedia):\n\"\"\"\n{notes[:7000]}\n\"\"\"\n" if notes else
              "No sources could be fetched, so use well-established facts you are sure of, and keep claims general.\n")
    prompt = (f"Write a presentation about: {topic}\n{source}"
              f"Make exactly {slides} content slides that tell a clear story (introduction first, a conclusion or key "
              "takeaways last). Each slide has: a short title (max 6 words); 4 or 5 bullets, each one informative "
              "sentence of 10 to 18 words with concrete specifics (names, dates, numbers, examples), no numbering, no "
              "emojis; a 'highlight' that is the slide's single most striking fact in at most 6 words (a number with its "
              "unit, a date, or a short phrase, e.g. '1916: Schwarzschild's solution'); and 2 to 3 sentences of speaker "
              "notes that add detail beyond the bullets. Also give an engaging presentation title and a one-line "
              "subtitle. Answer in JSON only.")
    body = {"model": llm.get("model", "qwen2.5:7b"), "stream": False, "format": OUTLINE_SCHEMA,
            "messages": [{"role": "system", "content": "You write clear, accurate presentation slides."},
                         {"role": "user", "content": prompt}],
            "options": {"temperature": 0.3, "num_ctx": max(4096, int(llm.get("num_ctx", 4096))), "num_predict": 1800}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = json.loads(r.read())
    deck = json.loads(data["message"]["content"])
    deck["slides"] = [s for s in deck.get("slides", []) if s.get("title") and s.get("bullets")][:max(3, slides)]
    if not deck["slides"]:
        raise ValueError("empty outline")
    return deck


def _fallback_outline(topic: str, notes: str, slides: int) -> dict:
    """Without the language model: one slide per paragraph of the article, its first sentences as bullets."""
    paras = [p.strip() for p in notes.split("\n") if len(p.strip()) > 120]
    out = []
    for i, p in enumerate(paras[:slides]):
        sents = re.split(r"(?<=[.!?])\s+", p)
        out.append({"title": topic.title() if i == 0 else " ".join(sents[0].split()[:6]).rstrip(",.;:"),
                    "bullets": [s if len(s) < 150 else s[:147].rsplit(" ", 1)[0] + "..." for s in sents[:4]],
                    "notes": ""})
    return {"title": topic.title(), "subtitle": "An overview", "slides": out}


def build_presentation(topic: str, deck: dict, src: dict, out: Path) -> Path:
    _, RGBColor, Inches, Pt, _, PP_ALIGN, MSO_ANCHOR = _pptx()
    prs = _deck()
    # title slide: the picture on the right, the title on the left
    s = _blank(prs)
    img = src.get("image")
    if img and Path(img).exists():
        _picture(s, Path(img), 7.3, 0.6, 5.5, 6.3)
    w = 6.4 if img else 11.8
    _bar(s, 0.75, 2.35, 0.9, 0.07)
    _text(s, 0.75, 2.55, w, 2.2, deck.get("title") or topic.title(), 40, PAPER, True, "Segoe UI Semibold")
    _text(s, 0.75, 4.55, w, 1.0, deck.get("subtitle", ""), 18, MUTED)
    _text(s, 0.75, 6.55, w, 0.4, f"Prepared by J.A.R.V.I.S.  ·  {dt.date.today():%d %B %Y}", 11, MUTED)
    total = len(deck["slides"])
    pics = [p for p in src.get("images", []) if p and Path(p).exists()]
    for i, sl in enumerate(deck["slides"], 1):
        s = _blank(prs)
        _bar(s, 0.75, 0.62, 0.6, 0.06)
        _text(s, 0.75, 0.78, 11.8, 1.0, sl["title"], 30, PAPER, True, "Segoe UI Semibold")
        bullets = [b.strip() for b in sl["bullets"][:6] if b.strip()]
        size = 20 if sum(len(b) for b in bullets) < 330 else 18
        box = _text(s, 0.75, 1.95, 7.35, 4.85, [""], size, PAPER)
        tf = box.text_frame
        for j, b in enumerate(bullets):
            p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
            p.space_after = Pt(12)
            ppr = p._p.get_or_add_pPr()                       # hanging indent: wrapped lines align with the text
            ppr.set("marL", str(int(Pt(size * 1.55))))
            ppr.set("indent", str(-int(Pt(size * 1.55))))
            dot = p.add_run()
            dot.text = "—  "
            dot.font.size, dot.font.color.rgb, dot.font.name = Pt(size), RGBColor.from_string(GOLD), "Segoe UI"
            r = p.add_run()
            r.text = b.rstrip(".") + "."
            r.font.size, r.font.color.rgb, r.font.name = Pt(size), RGBColor.from_string(PAPER), "Segoe UI"
        # the right-hand column: one of the article's photographs on every other slide, else the slide's key fact
        if i % 2 == 0 and pics:
            _picture(s, Path(pics.pop(0)), 8.55, 1.95, 4.05, 4.6)
        elif sl.get("highlight"):
            panel = _bar(s, 8.55, 1.95, 4.05, 4.6, "141822")
            panel.line.color.rgb = RGBColor.from_string("2A3242")
            _bar(s, 8.55, 1.95, 0.07, 4.6)
            _text(s, 8.95, 2.3, 3.4, 0.4, "KEY FACT", 11, GOLD, True)
            hl = sl["highlight"].strip().rstrip(".")
            _text(s, 8.95, 2.85, 3.4, 3.4, hl, 28 if len(hl) <= 36 else 22, PAPER, True, "Segoe UI Semibold",
                  anchor=MSO_ANCHOR.MIDDLE)
        _text(s, 11.6, 6.85, 1.0, 0.35, f"{i} / {total}", 10, MUTED, align=PP_ALIGN.RIGHT)
        if sl.get("notes"):
            s.notes_slide.notes_text_frame.text = sl["notes"]
    s = _blank(prs)                                           # sources
    _bar(s, 0.75, 0.62, 0.6, 0.06)
    _text(s, 0.75, 0.78, 11.8, 1.0, "Sources", 30, PAPER, True, "Segoe UI Semibold")
    lines = ([f"{src['title']} — Wikipedia", src["url"]] if src.get("url") else
             ["Written from general knowledge: no online sources could be reached."])
    lines += ["", "Researched and drafted by J.A.R.V.I.S. Check the key facts before presenting."]
    _text(s, 0.75, 2.0, 11.8, 4.0, lines, 16, MUTED)
    prs.save(out)
    return out


def presentation(topic: str, slides: int = 8, save_to: str = "", cfg: dict | None = None, done=None) -> str:
    """Research the topic, have the language model draft the slides, build a .pptx and open it. Runs in the
    background (about a minute); `done(message)` is called at the end, to be spoken."""
    topic = re.sub(r"\s+", " ", topic.strip(" .?!\"'")).strip()
    if not topic:
        raise FileError("What should the presentation be about, sir?")
    slides = max(3, min(int(slides or 8), 15))
    if save_to:
        folder = destination(save_to, HOME / "x")
    else:
        folder = (known_folder("documents") or HOME / "Documents") / "Jarvis" / "Presentations"
    folder.mkdir(parents=True, exist_ok=True)
    out = unique(folder / (re.sub(r'[\\/:*?"<>|]', "", topic.title())[:80] + ".pptx"))

    def work():
        try:
            t0 = time.time()
            src = research(topic)
            try:
                deck = outline(topic, src.get("text", ""), slides, cfg or {})
            except Exception as e:
                log.warning("outline from the language model failed (%s); building from the article", e)
                if not src.get("text"):
                    raise FileError(f"I couldn't research {topic}: no internet, and my language core didn't answer.")
                deck = _fallback_outline(topic, src["text"], slides)
            build_presentation(topic, deck, src, out)
            open_file(out)
            log.info("presentation %s built in %.0f s", out, time.time() - t0)
            msg = (f"Sir, your presentation on {topic} is ready: {len(deck['slides']) + 2} slides, saved {_where(folder)}. "
                   f"I've opened it for you." + ("" if src else " I couldn't reach the internet, so it's from my own "
                                                 "knowledge; do check the facts."))
        except FileError as e:
            msg = f"Sir, I couldn't finish the presentation. {e}"
        except Exception as e:
            log.exception("presentation failed")
            msg = f"Sir, I couldn't finish the presentation on {topic}: {e}"
        if done:
            done(msg)
    threading.Thread(target=work, daemon=True, name="presentation").start()
    return f"Researching {topic} now, sir. I'll have the presentation ready in about a minute."
