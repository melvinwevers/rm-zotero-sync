# rm-zotero-sync

Two-way sync between Zotero and a reMarkable 2, run by launchd every 30 minutes.

- Tag a Zotero item `to-remarkable` → its PDF is uploaded to `/ToRead`; tag becomes `on-remarkable`.
- Highlights and pen strokes made on the tablet come back as native Zotero highlight/ink
  annotations (tagged `reMarkable`) whenever the document changes. Only annotations this
  script created are ever replaced.
- Move the document to `/Read` on the tablet → final pull, tag becomes `annotated`.

Highlighter colours: yellow → yellow, green → green, pink → red, blue → blue, gray → gray.

Needs `rmapi` (ddvk fork) in `~/.local/bin` and `ZOTERO_USER_ID` / `ZOTERO_API_KEY` in
`~/.env.private`. Changes reach Zotero desktop on its next sync.

    ./run.sh --dry-run      # show what would happen
    ./run.sh --force        # re-pull all annotations
    tail -f ~/Library/Logs/rm-zotero-sync.log

State: `~/Library/Application Support/rm-zotero-sync/state.json`.
Schedule: copy `launchd.plist.example` to `~/Library/LaunchAgents/`, fix the paths, and load it with
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<file>.plist`.
