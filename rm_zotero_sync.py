"""Two-way sync between Zotero and a reMarkable tablet.

Zotero -> reMarkable: items tagged `to-remarkable` have their PDF uploaded to
RM_INBOX; the tag becomes `on-remarkable`.

reMarkable -> Zotero: whenever a tracked document changes on the tablet, its
highlights and pen strokes are written to the Zotero PDF attachment as native
highlight and ink annotations (tagged `reMarkable`). Only annotations this
script created are ever replaced. When the document is moved to RM_DONE, a
final pull is made and the tag becomes `annotated`.

Run with --dry-run to see what would happen without changing anything.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf
from rmscene import read_tree
from rmscene import scene_items as si

TAG_TO = "to-remarkable"
TAG_ON = "on-remarkable"
TAG_DONE = "annotated"
TAG_ANNOT = "reMarkable"
RM_INBOX = "/ToRead"
RM_DONE = "/Read"

ZOTERO_STORAGE = Path.home() / "Zotero" / "storage"
STATE_DIR = Path.home() / "Library" / "Application Support" / "rm-zotero-sync"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = Path.home() / "Library" / "Logs" / "rm-zotero-sync.log"
API = "https://api.zotero.org"

# reMarkable stores coordinates in 226 dpi units, x centred on the page.
RM_SCALE = 72 / 226

# Zotero's highlight palette, so colours line up with the Obsidian template.
HIGHLIGHT_COLORS = {
    "YELLOW": "#ffd400",
    "HIGHLIGHT": "#ffd400",
    "PINK": "#ff6666",  # no red highlighter on the rM2: pink -> Zotero red
    "RED": "#ff6666",
    "GREEN": "#5fb236",
    "BLUE": "#2ea8e5",
    "CYAN": "#2ea8e5",
    "ORANGE": "#f19837",
    "MAGENTA": "#e56eee",
    "GRAY": "#aaaaaa",
    "GREY": "#aaaaaa",
}
INK_COLORS = {"BLACK": "#000000", "GRAY": "#aaaaaa", "GREY": "#aaaaaa", **HIGHLIGHT_COLORS}
HIGHLIGHTER_TOOLS = {"HIGHLIGHTER", "HIGHLIGHTER_2"}
SKIP_TOOLS = {"ERASER", "ERASER_AREA"}

log = logging.getLogger("rm-zotero-sync")


# --------------------------------------------------------------------------
# reMarkable (via rmapi)
# --------------------------------------------------------------------------


def rmapi(*args: str, cwd: str | None = None) -> str:
    env = {**os.environ, "RMAPI_CONCURRENT": "4"}
    res = subprocess.run(
        ["rmapi", *args], capture_output=True, text=True, cwd=cwd, env=env, timeout=300
    )
    if res.returncode != 0:
        raise RuntimeError(f"rmapi {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def rm_stat(path: str) -> dict | None:
    try:
        return json.loads(rmapi("stat", path))
    except (RuntimeError, json.JSONDecodeError):
        return None


def rm_find(name: str) -> str | None:
    """Find a document's current path by exact name, anywhere on the tablet."""
    try:
        out = rmapi("find", "/", "^" + re.escape(name) + "$")
    except RuntimeError:
        return None
    for line in out.splitlines():
        line = re.sub(r"^\[[fd]\]\s*", "", line.strip())
        if line.endswith("/" + name) or line == name:
            return line if line.startswith("/") else "/" + line
    return None


def rm_download(doc_id: str, dest: Path) -> Path:
    """Download a document's raw files (.rmdoc) and unpack them into dest."""
    rmapi("get", "--id", doc_id, cwd=str(dest))
    archives = list(dest.glob("*.rmdoc")) + list(dest.glob("*.zip"))
    if not archives:
        raise RuntimeError(f"no archive downloaded for {doc_id}")
    raw = dest / "raw"
    with zipfile.ZipFile(archives[0]) as z:
        z.extractall(raw)
    return raw


# --------------------------------------------------------------------------
# Zotero Web API
# --------------------------------------------------------------------------


class Zotero:
    def __init__(self, user_id: str, key: str, dry_run: bool):
        self.base = f"{API}/users/{user_id}"
        self.http = httpx.Client(
            headers={"Zotero-API-Key": key, "Zotero-API-Version": "3"}, timeout=60
        )
        self.dry_run = dry_run

    def get(self, path: str, **params) -> httpx.Response:
        r = self.http.get(self.base + path, params=params)
        r.raise_for_status()
        return r

    def items_tagged(self, tag: str) -> list[dict]:
        return self.get("/items", tag=tag, format="json", limit=100).json()

    def item(self, key: str) -> dict:
        return self.get(f"/items/{key}", format="json").json()

    def children(self, key: str) -> list[dict]:
        return self.get(f"/items/{key}/children", format="json", limit=100).json()

    def set_tags(self, item: dict, remove: str | None, add: str | None) -> None:
        tags = [t for t in item["data"]["tags"] if t["tag"] != remove]
        if add and all(t["tag"] != add for t in tags):
            tags.append({"tag": add})
        log.info("  tags on %s: -%s +%s", item["key"], remove, add)
        if self.dry_run:
            return
        r = self.http.patch(
            f"{self.base}/items/{item['key']}",
            json={"tags": tags},
            headers={"If-Unmodified-Since-Version": str(item["version"])},
        )
        r.raise_for_status()

    def create(self, items: list[dict]) -> list[str]:
        if self.dry_run or not items:
            return []
        keys: list[str] = []
        for i in range(0, len(items), 50):
            r = self.http.post(f"{self.base}/items", json=items[i : i + 50])
            r.raise_for_status()
            res = r.json()
            if res.get("failed"):
                raise RuntimeError(f"Zotero rejected annotations: {res['failed']}")
            keys += [v for _, v in sorted(res["success"].items(), key=lambda kv: int(kv[0]))]
        return keys

    def delete(self, keys: list[str]) -> None:
        if self.dry_run or not keys:
            return
        for i in range(0, len(keys), 50):
            version = self.get("/items", limit=1).headers["Last-Modified-Version"]
            r = self.http.delete(
                f"{self.base}/items",
                params={"itemKey": ",".join(keys[i : i + 50])},
                headers={"If-Unmodified-Since-Version": version},
            )
            if r.status_code not in (204, 404):
                r.raise_for_status()


def pdf_attachment(zot: Zotero, item: dict) -> dict | None:
    if item["data"]["itemType"] == "attachment":
        kids = [item]
    else:
        kids = zot.children(item["key"])
    for k in kids:
        d = k["data"]
        if d.get("itemType") == "attachment" and d.get("contentType") == "application/pdf":
            return k
    return None


def local_pdf(att: dict) -> Path | None:
    d = att["data"]
    if d.get("linkMode") == "linked_file":
        p = Path(d.get("path", ""))
        return p if p.is_file() else None
    p = ZOTERO_STORAGE / att["key"] / d.get("filename", "")
    return p if p.is_file() else None


def display_name(item: dict) -> str:
    d = item["data"]
    creators = d.get("creators") or []
    author = (creators[0].get("lastName") or creators[0].get("name", "")) if creators else ""
    if len(creators) > 2:
        author += " et al."
    elif len(creators) == 2:
        author += " & " + (creators[1].get("lastName") or creators[1].get("name", ""))
    year = re.search(r"\d{4}", d.get("date", "") or "")
    title = d.get("title", "untitled")
    name = " - ".join(x for x in [author.strip(), year.group(0) if year else "", title] if x)
    name = re.sub(r'[/\\:*?"<>|]', "", name)
    return name[:120].strip()


# --------------------------------------------------------------------------
# Converting reMarkable annotations to Zotero annotations
# --------------------------------------------------------------------------


@dataclass
class PageMap:
    """Maps reMarkable coordinates on one page to PDF points (top-left origin)."""

    width: float
    sx: float = RM_SCALE
    ox: float = 0.0
    sy: float = RM_SCALE
    oy: float = 0.0

    def __post_init__(self):
        self.ox = self.width / 2

    def x(self, v: float) -> float:
        return self.ox + self.sx * v

    def y(self, v: float) -> float:
        return self.oy + self.sy * v

    def fit(self, pairs: list[tuple[float, float, float, float]]) -> None:
        """Refine the mapping from (rm_x, rm_y, pdf_x, pdf_y) matched points."""
        if len(pairs) < 2:
            return
        for axis in (0, 1):
            src = [p[axis] for p in pairs]
            dst = [p[axis + 2] for p in pairs]
            n = len(src)
            ms, md = sum(src) / n, sum(dst) / n
            var = sum((s - ms) ** 2 for s in src)
            if var < 1e-6:
                continue
            scale = sum((s - ms) * (d - md) for s, d in zip(src, dst)) / var
            if not 0.25 < scale < 0.4:  # implausible: keep the default
                continue
            if axis == 0:
                self.sx, self.ox = scale, md - scale * ms
            else:
                self.sy, self.oy = scale, md - scale * ms


@dataclass
class Highlight:
    color: str
    lines: list = field(default_factory=list)  # GlyphRange items, in order


def read_pages(raw: Path):
    """Yield (pdf_page_index, scene tree) for every annotated PDF page."""
    content = json.loads(next(raw.glob("*.content")).read_text())
    pages = (content.get("cPages") or {}).get("pages") or [{"id": p} for p in content.get("pages") or []]
    for idx, p in enumerate(pages):
        if p.get("deleted"):
            continue
        rm = list(raw.glob(f"*/{p['id']}.rm"))
        if not rm:
            continue
        redir = p.get("redir", {}).get("value") if "cPages" in content else idx
        if redir is None:
            log.info("  page %d is a notebook page added on the tablet; skipped", idx + 1)
            continue
        with open(rm[0], "rb") as f:
            yield redir, list(read_tree(f).walk())


def group_highlights(items) -> list[Highlight]:
    """Merge consecutive same-colour highlight lines into one highlight."""
    groups: list[Highlight] = []
    for it in items:
        if not isinstance(it, si.GlyphRange) or not it.rectangles:
            continue
        color = it.color.name
        if groups and groups[-1].color == color:
            prev = groups[-1].lines[-1].rectangles[-1]
            cur = it.rectangles[0]
            if 0 <= cur.y - prev.y <= prev.h * 1.6:
                groups[-1].lines.append(it)
                continue
        groups.append(Highlight(color, [it]))
    return groups


def clean_text(parts: list[str]) -> str:
    text = ""
    for p in parts:
        p = p.strip()
        if text.endswith("-") and p[:1].islower():
            text = text[:-1] + p
        else:
            text = (text + " " + p).strip()
    return re.sub(r"\s+", " ", text)


def sort_index(page: int, offset: int, top: float) -> str:
    return f"{page:05d}|{offset:06d}|{max(0, int(top)):05d}"


def build_annotations(raw: Path, attachment_key: str) -> list[dict]:
    pdf = pymupdf.open(next(raw.glob("*.pdf")))
    out: list[dict] = []
    for page_idx, items in read_pages(raw):
        if page_idx >= len(pdf):
            continue
        page = pdf[page_idx]
        H = page.rect.height
        pmap = PageMap(page.rect.width)
        label = page.get_label() or str(page_idx + 1)

        # Locate each highlighted line on the PDF's own text layer.
        located = []
        pairs = []
        for it in items:
            if isinstance(it, si.GlyphRange) and it.rectangles and it.text.strip():
                hits = page.search_for(it.text.strip())
                r0 = it.rectangles[0]
                if hits:
                    best = min(hits, key=lambda h: abs(h.y0 - pmap.y(r0.y)) + abs(h.x0 - pmap.x(r0.x)) / 4)
                    located.append((it, best))
                    pairs.append((r0.x, r0.y, best.x0, best.y0))
                else:
                    located.append((it, None))
        pmap.fit(pairs)
        found = {id(it): rect for it, rect in located}

        glyph_boxes = []
        for n, hl in enumerate(group_highlights(items)):
            rects = []
            for line in hl.lines:
                hit = found.get(id(line))
                if hit is not None:
                    x0, y0, x1, y1 = hit.x0, hit.y0, hit.x1, hit.y1
                else:
                    r = line.rectangles[0]
                    x0, y0 = pmap.x(r.x), pmap.y(r.y)
                    x1, y1 = pmap.x(r.x + r.w), pmap.y(r.y + r.h)
                glyph_boxes.append((x0, y0, x1, y1))
                rects.append([round(x0, 3), round(H - y1, 3), round(x1, 3), round(H - y0, 3)])
            top = H - max(r[3] for r in rects)
            out.append(
                {
                    "itemType": "annotation",
                    "parentItem": attachment_key,
                    "annotationType": "highlight",
                    "annotationText": clean_text([l.text for l in hl.lines]),
                    "annotationComment": "",
                    "annotationColor": HIGHLIGHT_COLORS.get(hl.color, "#ffd400"),
                    "annotationPageLabel": label,
                    "annotationSortIndex": sort_index(page_idx, n, top),
                    "annotationPosition": json.dumps({"pageIndex": page_idx, "rects": rects}),
                    "tags": [{"tag": TAG_ANNOT}],
                }
            )

        # Pen strokes become ink; freehand highlighter strokes that are not
        # already covered by a text highlight become (text-less) highlights.
        paths_by_color: dict[str, list] = {}
        for it in items:
            if not isinstance(it, si.Line) or len(it.points) < 2:
                continue
            tool = it.tool.name
            if tool in SKIP_TOOLS:
                continue
            xs = [pmap.x(p.x) for p in it.points]
            ys = [pmap.y(p.y) for p in it.points]
            if tool in HIGHLIGHTER_TOOLS:
                box = (min(xs), min(ys), max(xs), max(ys))
                covered = any(
                    box[0] < g[2] and g[0] < box[2] and box[1] < g[3] and g[1] < box[3]
                    for g in glyph_boxes
                )
                if covered:
                    continue
                out.append(
                    {
                        "itemType": "annotation",
                        "parentItem": attachment_key,
                        "annotationType": "highlight",
                        "annotationText": page.get_textbox(pymupdf.Rect(*box)).strip(),
                        "annotationComment": "",
                        "annotationColor": HIGHLIGHT_COLORS.get(it.color.name, "#ffd400"),
                        "annotationPageLabel": label,
                        "annotationSortIndex": sort_index(page_idx, 500, box[1]),
                        "annotationPosition": json.dumps(
                            {"pageIndex": page_idx, "rects": [[box[0], H - box[3], box[2], H - box[1]]]}
                        ),
                        "tags": [{"tag": TAG_ANNOT}],
                    }
                )
                continue
            path = []
            for x, y in zip(xs, ys):
                path += [round(x, 2), round(H - y, 2)]
            paths_by_color.setdefault(INK_COLORS.get(it.color.name, "#000000"), []).append((path, min(ys)))

        for color, strokes in paths_by_color.items():
            out.append(
                {
                    "itemType": "annotation",
                    "parentItem": attachment_key,
                    "annotationType": "ink",
                    "annotationComment": "",
                    "annotationColor": color,
                    "annotationPageLabel": label,
                    "annotationSortIndex": sort_index(page_idx, 900, min(t for _, t in strokes)),
                    "annotationPosition": json.dumps(
                        {"pageIndex": page_idx, "width": 1, "paths": [p for p, _ in strokes]}
                    ),
                    "tags": [{"tag": TAG_ANNOT}],
                }
            )
    return out


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"docs": {}}


def save_state(state: dict, dry_run: bool) -> None:
    if dry_run:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def push(zot: Zotero, state: dict, dry_run: bool) -> None:
    for item in zot.items_tagged(TAG_TO):
        title = item["data"].get("title", item["key"])
        att = pdf_attachment(zot, item)
        if not att:
            log.warning("no PDF attachment for '%s'; skipped", title)
            continue
        if att["key"] in state["docs"]:
            log.info("'%s' already on the reMarkable; fixing tag only", title)
            zot.set_tags(item, TAG_TO, TAG_ON)
            continue
        src = local_pdf(att)
        if not src:
            log.warning("PDF for '%s' not found locally (not downloaded?); skipped", title)
            continue
        name = display_name(item)
        log.info("upload: %s -> %s/%s", title, RM_INBOX, name)
        if dry_run:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / f"{name}.pdf"
            shutil.copy(src, dst)
            rmapi("put", str(dst), RM_INBOX)
        meta = rm_stat(f"{RM_INBOX}/{name}") or rm_stat(f"{RM_INBOX}/{name}.pdf")
        if not meta:
            raise RuntimeError(f"uploaded '{name}' but cannot find it on the tablet")
        state["docs"][att["key"]] = {
            "item": item["key"],
            "rm_id": meta["ID"],
            "rm_name": meta["Name"],
            "rm_path": f"{RM_INBOX}/{meta['Name']}",
            "modified": meta["ModifiedClient"],
            "annotations": [],
            "done": False,
        }
        save_state(state, dry_run)
        zot.set_tags(item, TAG_TO, TAG_ON)


def pull(zot: Zotero, state: dict, dry_run: bool, force: bool = False) -> None:
    done_dir = rm_stat(RM_DONE)
    done_id = done_dir["ID"] if done_dir else None
    for att_key, doc in state["docs"].items():
        if doc.get("done"):
            continue
        meta = rm_stat(doc["rm_path"])
        if not meta or meta["ID"] != doc["rm_id"]:
            path = rm_find(doc["rm_name"])
            meta = rm_stat(path) if path else None
            if not meta or meta["ID"] != doc["rm_id"]:
                log.warning("'%s' not found on the tablet (deleted?); skipped", doc["rm_name"])
                continue
            doc["rm_path"] = path
        finished = done_id is not None and meta.get("Parent") == done_id
        changed = force or meta["ModifiedClient"] != doc["modified"]
        if changed:
            with tempfile.TemporaryDirectory() as tmp:
                raw = rm_download(doc["rm_id"], Path(tmp))
                annots = build_annotations(raw, att_key)
            n_hl = sum(a["annotationType"] == "highlight" for a in annots)
            n_ink = len(annots) - n_hl
            if annots or doc["annotations"]:
                log.info("pull: '%s': %d highlights, %d ink", doc["rm_name"], n_hl, n_ink)
                zot.delete(doc["annotations"])
                doc["annotations"] = zot.create(annots)
            doc["modified"] = meta["ModifiedClient"]
        if finished:
            log.info("finished: '%s' is in %s", doc["rm_name"], RM_DONE)
            zot.set_tags(zot.item(doc["item"]), TAG_ON, TAG_DONE)
            doc["done"] = True
        save_state(state, dry_run)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    ap.add_argument("--force", action="store_true", help="re-pull annotations even if unchanged")
    ap.add_argument(
        "--parse", metavar="RAW_DIR", help="debug: print annotations parsed from an unpacked .rmdoc"
    )
    args = ap.parse_args()

    handlers = [logging.StreamHandler(sys.stdout)]
    if not args.parse:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    # rmscene warns about metadata blocks from newer firmware that we don't need
    logging.getLogger("rmscene").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.parse:
        for a in build_annotations(Path(args.parse), "TESTKEY"):
            pos = json.loads(a["annotationPosition"])
            desc = a.get("annotationText", "")[:70] or f"{len(pos.get('paths', []))} strokes"
            print(f"p{a['annotationPageLabel']:>3} {a['annotationType']:9} {a['annotationColor']} {desc}")
        return 0

    user, key = os.environ.get("ZOTERO_USER_ID"), os.environ.get("ZOTERO_API_KEY")
    if not (user and key):
        log.error("ZOTERO_USER_ID and ZOTERO_API_KEY must be set (see ~/.env.private)")
        return 2
    zot = Zotero(user, key, args.dry_run)
    state = load_state()
    try:
        push(zot, state, args.dry_run)
        pull(zot, state, args.dry_run, args.force)
    except Exception:
        log.exception("sync failed")
        return 1
    finally:
        save_state(state, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
