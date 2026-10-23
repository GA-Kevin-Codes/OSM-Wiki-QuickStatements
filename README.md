# OSM Wiki QuickStatements

A small local web page for bulk-editing
[OpenStreetMap Wiki data items](https://wiki.openstreetmap.org/wiki/Data_items) with
QuickStatements-style commands. Preview what will change, then run it.

One Python file, nothing to install beyond Python 3.9+ (already on macOS and most Linux).

## Run it

```bash
git clone https://github.com/GA-Kevin-Codes/OSM-Wiki-QuickStatements.git
cd OSM-Wiki-QuickStatements
python3 osmqs.py
```

Your browser opens <http://localhost:8765>. Stop it with Ctrl+C. (On Windows, use `py osmqs.py`.)

## Use it

1. **Log in.** On the OSM Wiki, create a bot password at
   [Special:BotPasswords](https://wiki.openstreetmap.org/wiki/Special:BotPasswords) with
   **Edit existing pages** (add **High-volume editing** for big batches). Enter the
   `YourName@BotName` username and password on the page. The password stays in memory only.
2. **Paste commands and press Preview.** Each line shows what it will do: *will edit*,
   *no change* (already on the item), or *error* with the reason. Nothing is sent yet.
3. **Press Run.** Edits go out one by one. When they finish, every edited item is re-read
   and checked: the planned changes must be there, and nothing else on the item may have
   changed.

## Commands

| Command | Does |
|---|---|
| `Q99\|P13\|"Q[1-9]\d*"` | Add a statement (text, URL or external ID) |
| `Q99\|P6\|Q13` | Add a statement pointing to an item |
| `Q8119\|P31\|en:"Key:architect:wikidata"` | Add language-tagged text |
| `Q99\|P6\|Q13\|S11\|"https://…"` | Add a source (reference) to the statement |
| `Q8119\|P31\|en:"Key:x"\|P50\|"Key:y"` | Add a qualifier to the statement |
| `-Q99\|P8\|"https://…"` | Remove a statement |
| `Q99\|Lmul\|"brand:wikidata"` | Set a label (`Den` description, `Aen` alias) |
| `CREATE` then `LAST\|P2\|Q7` | Create a new item, then edit it with `LAST` |
| `CREATE\|@nacs` … `LAST\|P22\|@nacs` | Name a new item so later lines can use it as `@nacs` |

- Fields are separated by `|` or tabs; lines by new lines or `||`.
- `somevalue` and `novalue` work as values.
- If the statement already exists, only missing qualifiers or sources are added.
- Add `/* summary */` at the end of a line for a per-line edit summary.
- Key and tag items keep their label in `mul` (multilingual); the preview warns if `Len`
  would just duplicate it.
- `LAST` is the most recent `CREATE`; it works as an item or a value. `@name` is an
  extension for when a later item has to point back to an earlier new one.
- A `CREATE` is refused if an item with the same label already exists, so running a batch
  twice can't make duplicates.
- Not supported yet: dates, quantities and coordinates.

## Why it is safe

Each change is one targeted API call (`wbsetclaim`, `wbsetqualifier`, `wbsetreference`,
`wbremoveclaims`, `wbsetlabel`, `wbsetdescription`, `wbsetaliases`). Items are never re-sent
whole, so an edit can't alter qualifiers, references or statements it wasn't asked to
touch. Each edit is tied to the item's current revision, so if someone else edits the item
first, the edit is rejected instead of overwriting their change.

For large batches, announce them on the wiki first and use a bot-flagged account
(tick *Mark as bot edits*).

## Development

```bash
python3 -m unittest test_osmqs
```

Set `OSMQS_API` to point at another Wikibase's `api.php`, or `--port` to change the port.
