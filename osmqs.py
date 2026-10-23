#!/usr/bin/env python3
"""
OSM Wiki QuickStatements: bulk-edit OpenStreetMap Wiki data items.

Run `python3 osmqs.py`, log in with an OSM Wiki bot password, paste
QuickStatements commands, preview what will change, then run them.

Every change is one targeted API call (wbsetclaim, wbsetqualifier,
wbsetreference, wbremoveclaims, wbsetlabel, ...). Items are never
re-sent whole, so an edit cannot touch data it was not asked to change.
After a run, each edited item is re-read and compared with how it was
before, to confirm nothing else changed.

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import copy
import http.cookiejar
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass, field

API_URL = os.environ.get("OSMQS_API", "https://wiki.openstreetmap.org/w/api.php")
USER_AGENT = (
    "OSM-Wiki-QuickStatements/0.1 "
    "(https://github.com/GA-Kevin-Codes/OSM-Wiki-QuickStatements)"
)
STRING_DATATYPES = {
    "string", "external-id", "url", "commonsMedia", "math",
    "musical-notation", "tabular-data", "geo-shape",
}
ENTITY_DATATYPES = {"wikibase-item": ("item", "Q"), "wikibase-property": ("property", "P")}
# Wikibase's default length limits; longer values are rejected by the wiki.
MAX_STRING = {"url": 500}
MAX_STRING_DEFAULT = 400
MAX_TERM = 250
DEFAULT_SUMMARY = "OSM Wiki QuickStatements batch"
NEW_NAME = re.compile(r"@[A-Za-z0-9_:.-]+")   # CREATE|@name, then @name as item or value
NEW_ID = re.compile(r"NEW[1-9]\d*")           # stand-in ID of an item not created yet


# ---------------------------------------------------------------------------
# Wiki API client
# ---------------------------------------------------------------------------

class WikiError(Exception):
    def __init__(self, code, info=""):
        super().__init__(f"{code}: {info}" if info else code)
        self.code = code
        self.info = info


class Wiki:
    """Minimal MediaWiki/Wikibase action API client with bot-password login."""

    def __init__(self, api_url=API_URL):
        self.api_url = api_url
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.username = None
        self._csrf = None

    @property
    def wiki_url(self):
        return self.api_url.rsplit("/w/api.php", 1)[0]

    def request(self, params, post=False):
        params = {**params, "format": "json", "formatversion": "2"}
        data = urllib.parse.urlencode(params).encode()
        for attempt in range(5):
            if post:
                req = urllib.request.Request(self.api_url, data=data)
            else:
                req = urllib.request.Request(self.api_url + "?" + data.decode())
            req.add_header("User-Agent", USER_AGENT)
            try:
                with self.opener.open(req, timeout=60) as resp:
                    body = json.load(resp)
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                # Never blindly resend an edit: it may have gone through.
                if post:
                    raise WikiError("network", str(exc))
                time.sleep(2 * (attempt + 1))
                continue
            err = body.get("error")
            if err:
                if err.get("code") == "maxlag":
                    time.sleep(5)
                    continue
                raise WikiError(err.get("code", "error"), err.get("info", ""))
            return body
        raise WikiError("unavailable", "the wiki did not answer after several tries")

    def login(self, username, password):
        token = self.request({"action": "query", "meta": "tokens", "type": "login"})
        result = self.request(
            {
                "action": "login",
                "lgname": username,
                "lgpassword": password,
                "lgtoken": token["query"]["tokens"]["logintoken"],
            },
            post=True,
        )["login"]
        if result.get("result") != "Success":
            raise WikiError("login-failed", result.get("reason") or result.get("result", ""))
        self.username = result.get("lgusername") or username.split("@")[0]
        self._csrf = None
        return self.username

    def logout(self):
        if self.username:
            try:
                self.request({"action": "logout", "token": self._token()}, post=True)
            except WikiError:
                pass
        self.username = None
        self._csrf = None

    def _token(self):
        if not self._csrf:
            res = self.request({"action": "query", "meta": "tokens", "type": "csrf"})
            self._csrf = res["query"]["tokens"]["csrftoken"]
        return self._csrf

    def get_entities(self, ids, props="info|labels|descriptions|aliases|claims|datatype"):
        """Return {id: entity}; ids that don't exist map to None."""
        out = {}
        ids = sorted(set(ids))
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            try:
                res = self.request({"action": "wbgetentities", "ids": "|".join(chunk), "props": props})
            except WikiError as exc:
                # One malformed ID fails the whole request; retry the chunk one by one.
                if exc.code != "no-such-entity":
                    raise
                if len(chunk) == 1:
                    out[chunk[0]] = None
                else:
                    out.update(self.get_entities_one_by_one(chunk, props))
                continue
            for eid, ent in res.get("entities", {}).items():
                out[eid] = None if "missing" in ent else ent
        return out

    def items_labelled(self, text):
        """IDs of items whose label (in English, or the mul fallback) is exactly `text`."""
        res = self.request({"action": "wbsearchentities", "search": text, "language": "en",
                            "type": "item", "limit": "50"})
        return [r["id"] for r in res.get("search", []) if r.get("label") == text]

    def get_entities_one_by_one(self, ids, props):
        out = {}
        for eid in ids:
            out.update(self.get_entities([eid], props))
        return out

    def languages(self, context):
        """Language codes the wiki accepts for 'term' or 'monolingualtext'."""
        cache = self.__dict__.setdefault("_languages", {})
        if context not in cache:
            res = self.request({"action": "query", "meta": "wbcontentlanguages",
                                "wbclcontext": context, "wbclprop": "code"})
            cache[context] = set(res["query"]["wbcontentlanguages"])
        return cache[context]

    def edit(self, params):
        if not self.username:
            raise WikiError("not-logged-in", "log in first")
        params = {**params, "maxlag": "5", "assert": "user"}
        for attempt in range(2):
            params["token"] = self._token()
            try:
                return self.request(params, post=True)
            except WikiError as exc:
                if exc.code == "badtoken" and attempt == 0:
                    self._csrf = None
                    continue
                if exc.code in ("assertuserfailed", "notloggedin"):
                    self.username = None   # session expired: ask to log in again
                    self._csrf = None
                raise
        raise WikiError("badtoken")


# ---------------------------------------------------------------------------
# Parsing QuickStatements (v1) lines
# ---------------------------------------------------------------------------

@dataclass
class Command:
    line_no: int
    raw: str
    item: str = ""          # Q123, or NEW1, NEW2, ... for items created in this batch
    kind: str = ""          # create | statement | remove | label | description | alias
    name: str = ""          # CREATE|@name: lets later lines refer to the new item as @name
    prop: str = ""
    value: tuple = ()
    lang: str = ""
    qualifiers: list = field(default_factory=list)   # [(prop, value)]
    sources: list = field(default_factory=list)      # [(prop, value)]
    summary: str = ""
    error: str = ""


def _split_outside_quotes(text, separators):
    """Split on any of `separators`, ignoring those inside "double quotes"."""
    parts, cur, in_quote, i = [], [], False, 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_quote = not in_quote
        if not in_quote:
            sep = next((s for s in separators if text.startswith(s, i)), None)
            if sep:
                parts.append("".join(cur))
                cur = []
                i += len(sep)
                continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def split_lines(text):
    """New lines always end a command (so an unclosed quote can't swallow the
    next lines); `||` separates commands too, unless it's inside quotes."""
    text = text.replace("“", '"').replace("”", '"')
    lines = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.extend(_split_outside_quotes(line, ["||"]))
    return lines


def parse_value(token):
    t = token.strip()
    if re.fullmatch(r"[QqPp][1-9]\d*", t):
        return ("entity", t.upper())
    if t.upper() == "LAST" or NEW_NAME.fullmatch(t):
        return ("entity", t.upper())   # an item created in this batch; see parse()
    if t in ("somevalue", "novalue"):
        return (t,)
    m = re.fullmatch(r'([a-z][a-z0-9-]*):"(.*)"', t, re.S)
    if m:
        return ("monolingual", m.group(1), m.group(2))
    m = re.fullmatch(r'"(.*)"', t, re.S)
    if m:
        return ("string", m.group(1))
    raise ValueError(f"can't read the value {t!r}")


def parse_line(line_no, raw):
    cmd = Command(line_no=line_no, raw=raw.strip())
    text = cmd.raw
    m = re.search(r"\s*/\*(.*)\*/\s*$", text, re.S)
    if m and text[:m.start()].count('"') % 2 == 0:
        cmd.summary = m.group(1).strip()
        text = text[:m.start()]
    fields = [f.strip() for f in _split_outside_quotes(text, ["|", "\t"])]
    try:
        remove = fields[0].startswith("-")
        cmd.item = fields[0].lstrip("-").strip().upper()
        if cmd.item == "CREATE":
            cmd.kind = "create"
            cmd.item = ""
            if remove or len(fields) > 2 or (len(fields) == 2 and not NEW_NAME.fullmatch(fields[1])):
                raise ValueError("CREATE takes nothing else, or a name like CREATE|@nacs")
            cmd.name = fields[1].upper() if len(fields) == 2 else ""
            return cmd
        if len(fields) < 3:
            raise ValueError("expected at least: item | property | value")
        if not (re.fullmatch(r"Q[1-9]\d*", cmd.item) or cmd.item == "LAST" or NEW_NAME.fullmatch(cmd.item)):
            raise ValueError(f"{fields[0]!r} is not an item ID like Q123 (or LAST, @name)")
        term = re.fullmatch(r"([LDA])([a-z][a-z0-9-]*)", fields[1])
        if term:
            if remove:
                raise ValueError("removing labels, descriptions or aliases is not supported")
            cmd.kind = {"L": "label", "D": "description", "A": "alias"}[term.group(1)]
            cmd.lang = term.group(2)
            value = parse_value(fields[2])
            if value[0] != "string" or len(fields) > 3:
                raise ValueError(f'a {cmd.kind} must be a single "quoted text"')
            cmd.value = value
            return cmd
        if not re.fullmatch(r"P[1-9]\d*", fields[1].upper()):
            raise ValueError(f"{fields[1]!r} is not a property like P13 (or Len/Den/Aen)")
        cmd.kind = "remove" if remove else "statement"
        cmd.prop = fields[1].upper()
        cmd.value = parse_value(fields[2])
        rest = fields[3:]
        if len(rest) % 2:
            raise ValueError("qualifiers and sources come in pairs: |P123|value")
        if remove and rest:
            raise ValueError("a removal takes no qualifiers or sources")
        for key, val in zip(rest[::2], rest[1::2]):
            key = key.upper()
            if re.fullmatch(r"P[1-9]\d*", key):
                cmd.qualifiers.append((key, parse_value(val)))
            elif re.fullmatch(r"S[1-9]\d*", key):
                cmd.sources.append(("P" + key[1:], parse_value(val)))
            else:
                raise ValueError(f"{key!r} is not a qualifier (P…) or source (S…)")
    except ValueError as exc:
        cmd.error = str(exc)
        if not (re.fullmatch(r"Q[1-9]\d*", cmd.item) or cmd.item == "LAST" or NEW_NAME.fullmatch(cmd.item)):
            cmd.item = ""
    return cmd


def parse(text):
    """Parse a batch. Each CREATE gets a stand-in ID (NEW1, NEW2, ...); LAST and
    @name are replaced by it here, and by the real ID once the item exists."""
    commands = []
    new_count, names, last = 0, {}, None

    def ref(x):
        if x == "LAST":
            if not last:
                raise ValueError("LAST is used before any CREATE line")
            return last
        if x.startswith("@"):
            if x not in names:
                raise ValueError(f"{x} is not named by an earlier CREATE|{x} line")
            return names[x]
        return x

    def ref_value(v):
        return ("entity", ref(v[1])) if v[0] == "entity" else v

    for n, raw in enumerate(split_lines(text), start=1):
        if not raw.strip():
            continue
        cmd = parse_line(n, raw)
        commands.append(cmd)
        if cmd.kind == "create":
            # Even a CREATE line with an error gets an ID, so the lines after it
            # fail with it instead of landing on the previous new item.
            new_count += 1
            cmd.item = last = f"NEW{new_count}"
            if cmd.name and not cmd.error:
                if cmd.name in names:
                    cmd.error = f"{cmd.name} is already the name of an earlier CREATE"
                else:
                    names[cmd.name] = cmd.item
            continue
        try:
            cmd.item = ref(cmd.item) if cmd.item else ""
            if cmd.error:
                continue
            cmd.value = ref_value(cmd.value)
            cmd.qualifiers = [(p, ref_value(v)) for p, v in cmd.qualifiers]
            cmd.sources = [(p, ref_value(v)) for p, v in cmd.sources]
        except ValueError as exc:
            cmd.error = cmd.error or str(exc)
            if not NEW_ID.fullmatch(cmd.item) and not re.fullmatch(r"Q[1-9]\d*", cmd.item):
                cmd.item = ""
    return commands


# ---------------------------------------------------------------------------
# Planning: compare commands with the live items
# ---------------------------------------------------------------------------

def check_text(what, text, limit):
    if not text.strip():
        raise ValueError(f"{what}: the text is empty")
    if len(text) > limit:
        raise ValueError(f"{what}: {len(text)} characters is over the wiki's limit of {limit}")


def make_snak(prop, value, datatype):
    if value[0] in ("somevalue", "novalue"):
        return {"snaktype": value[0], "property": prop}
    if datatype in ENTITY_DATATYPES:
        kind, prefix = ENTITY_DATATYPES[datatype]
        new = value[0] == "entity" and NEW_ID.fullmatch(value[1])
        if value[0] != "entity" or (value[1][0] != prefix and not (new and kind == "item")):
            raise ValueError(f"{prop} needs a{'n' if kind == 'item' else ''} {kind} ID like {prefix}123")
        # A new item's numeric ID is only known once it exists; see swap_new_ids().
        dv = {"type": "wikibase-entityid",
              "value": {"entity-type": kind, "id": value[1]} if new else
              {"entity-type": kind, "numeric-id": int(value[1][1:]), "id": value[1]}}
    elif datatype in STRING_DATATYPES:
        if value[0] != "string":
            raise ValueError(f'{prop} needs "quoted text"')
        check_text(prop, value[1], MAX_STRING.get(datatype, MAX_STRING_DEFAULT))
        dv = {"type": "string", "value": value[1]}
    elif datatype == "monolingualtext":
        if value[0] != "monolingual":
            raise ValueError(f'{prop} needs language-tagged text like en:"text"')
        check_text(prop, value[2], MAX_STRING_DEFAULT)
        dv = {"type": "monolingualtext", "value": {"text": value[2], "language": value[1]}}
    else:
        raise ValueError(f"{prop} has datatype {datatype!r}, which this tool doesn't support")
    return {"snaktype": "value", "property": prop, "datavalue": dv}


def snak_key(snak):
    """Identity of a snak's value, ignoring hashes and verbose entity fields."""
    dv = snak.get("datavalue")
    value = dv.get("value") if dv else None
    if isinstance(value, dict) and value.get("id"):
        value = value["id"]
    return (snak.get("property"), snak.get("snaktype"), json.dumps(value, sort_keys=True))


def group_snaks(snaks):
    grouped, order = {}, []
    for s in snaks:
        if s["property"] not in grouped:
            order.append(s["property"])
        grouped.setdefault(s["property"], []).append(s)
    return grouped, order


def show_value(value):
    if value[0] == "entity":
        return value[1]
    if value[0] == "monolingual":
        return f'{value[1]}:"{value[2]}"'
    if value[0] == "string":
        return f'"{value[1]}"'
    return value[0]


@dataclass
class Step:
    line_no: int
    raw: str
    item: str
    action: str              # human-readable description
    status: str              # ready | skip | error, then done | failed
    params: dict = field(default_factory=dict)
    expect: dict = field(default_factory=dict)  # what verification should find
    message: str = ""
    revid: int = 0
    needs: tuple = ()        # stand-in IDs (NEW1, ...) that must be created first


@dataclass
class Plan:
    steps: list
    before: dict             # item -> entity as fetched (untouched copy)
    labels: dict             # entity id -> display label
    created: dict = field(default_factory=dict)   # NEW1 -> Q123, filled in while running


def label_of(entity):
    if not entity:
        return ""
    labels = entity.get("labels") or {}
    lab = labels.get("en") or next(iter(labels.values()), None)
    return lab["value"] if lab else ""


def make_plan(wiki, commands, summary=""):
    items = {c.item for c in commands if not c.error and not NEW_ID.fullmatch(c.item)}
    props = set()
    for c in commands:
        if not c.error and c.prop:
            props.add(c.prop)
            props.update(p for p, _ in c.qualifiers + c.sources)
    entities = wiki.get_entities(items) if items else {}
    prop_entities = wiki.get_entities(props, props="labels|datatype") if props else {}
    labels = {eid: label_of(e) for eid, e in {**entities, **prop_entities}.items()}

    # New items start empty. Refuse to create one whose label is already taken,
    # so running the same batch twice can't make duplicates.
    new_labels = {}
    for c in commands:
        if c.kind == "label" and not c.error and NEW_ID.fullmatch(c.item):
            new_labels.setdefault(c.item, c.value[1])
    for c in commands:
        if c.kind != "create" or c.error:
            continue
        label = new_labels.get(c.item, "")
        taken = wiki.items_labelled(label) if label else []
        if taken:
            c.error = f'an item labelled "{label}" already exists: {", ".join(taken)}'
            continue
        labels[c.item] = label
        entities[c.item] = {"id": c.item, "type": "item", "labels": {}, "descriptions": {},
                            "aliases": {}, "claims": {}}
    before = copy.deepcopy(entities)
    work = copy.deepcopy(entities)   # updated as steps are planned

    def datatype(prop):
        ent = prop_entities.get(prop)
        if not ent:
            raise ValueError(f"property {prop} does not exist")
        return ent.get("datatype")

    def check_language(code, context):
        if code not in wiki.languages(context):
            raise ValueError(f"{code!r} is not a language code the wiki accepts")

    def values_of(c):
        return [c.value] + [v for _, v in c.qualifiers + c.sources]

    def check_values(c):
        for v in values_of(c):
            if v and v[0] == "monolingual":
                check_language(v[1], "monolingualtext")
            if v and v[0] == "entity" and NEW_ID.fullmatch(v[1]) and v[1] not in work:
                raise ValueError("it refers to a new item whose CREATE line has an error")

    steps = []
    for c in commands:
        def step(action, status="ready", **kw):
            line_summary = "; ".join(s for s in (summary.strip(), c.summary) if s) or DEFAULT_SUMMARY
            params = kw.pop("params", {})
            if params:
                params["summary"] = line_summary
            ids = [] if c.kind == "create" else \
                [c.item] + [v[1] for v in values_of(c) if v and v[0] == "entity"]
            needs = tuple(sorted({x for x in ids if NEW_ID.fullmatch(x)}))
            steps.append(Step(c.line_no, c.raw, c.item, action, status, params=params, needs=needs, **kw))

        if c.error:
            step("Can't create this item" if c.kind == "create" else "Can't read this line",
                 "error", message=c.error)
            continue
        if c.kind == "create":
            label = labels.get(c.item)
            step(f'Create a new item "{label}"' if label else "Create a new item",
                 message="" if label else "no label is set for it in this batch",
                 params={"action": "wbeditentity", "new": "item", "data": "{}"})
            continue
        ent = work.get(c.item)
        if ent is None:
            step("Item not found", "error", message="its CREATE line has an error"
                 if NEW_ID.fullmatch(c.item) else f"{c.item} does not exist on the wiki")
            continue
        try:
            if c.kind in ("label", "description", "alias"):
                check_text(c.kind, c.value[1], MAX_TERM)
                check_language(c.lang, "term")
            else:
                check_values(c)
            if c.kind in ("label", "description"):
                section = c.kind + "s"
                current = (ent.get(section) or {}).get(c.lang, {}).get("value")
                if current == c.value[1]:
                    step(f"{c.kind.capitalize()} ({c.lang}) already set", "skip")
                    continue
                verb = "Replace" if current else "Add"
                note = f'currently "{current}"' if current else ""
                mul = (ent.get(section) or {}).get("mul", {}).get("value")
                if not current and c.lang != "mul" and mul == c.value[1]:
                    # OSM Wiki key/tag items keep their label in "mul".
                    note = f'same as the multilingual (mul) {c.kind}; usually not needed'
                step(f'{verb} {c.kind} ({c.lang}): "{c.value[1]}"',
                     message=note,
                     params={"action": f"wbset{c.kind}", "id": c.item,
                             "language": c.lang, "value": c.value[1]},
                     expect={"term": [section, c.lang, c.value[1]]})
                ent.setdefault(section, {})[c.lang] = {"language": c.lang, "value": c.value[1]}
                continue
            if c.kind == "alias":
                have = [a["value"] for a in (ent.get("aliases") or {}).get(c.lang, [])]
                if c.value[1] in have:
                    step(f"Alias ({c.lang}) already present", "skip")
                    continue
                if "|" in c.value[1]:
                    raise ValueError("an alias can't contain |")
                step(f'Add alias ({c.lang}): "{c.value[1]}"',
                     params={"action": "wbsetaliases", "id": c.item,
                             "language": c.lang, "add": c.value[1]},
                     expect={"alias": [c.lang, c.value[1]]})
                ent.setdefault("aliases", {}).setdefault(c.lang, []).append(
                    {"language": c.lang, "value": c.value[1]})
                continue

            main = make_snak(c.prop, c.value, datatype(c.prop))
            claims = ent.setdefault("claims", {}).setdefault(c.prop, [])
            matching = [cl for cl in claims if snak_key(cl["mainsnak"]) == snak_key(main)]
            what = f"{c.prop} = {show_value(c.value)}"

            if c.kind == "remove":
                if not matching:
                    step(f"Remove {what}: not on the item", "skip")
                    continue
                ids = [cl["id"] for cl in matching]
                step(f"Remove {what}",
                     params={"action": "wbremoveclaims", "claim": "|".join(ids)},
                     expect={"removed": ids})
                claims[:] = [cl for cl in claims if cl["id"] not in ids]
                continue

            quals = [make_snak(p, v, datatype(p)) for p, v in c.qualifiers]
            refs = [make_snak(p, v, datatype(p)) for p, v in c.sources]
            if not matching:
                guid = f"{c.item}${uuid.uuid4()}"
                claim = {"id": guid, "type": "statement", "rank": "normal", "mainsnak": main}
                if quals:
                    claim["qualifiers"], claim["qualifiers-order"] = group_snaks(quals)
                if refs:
                    snaks, order = group_snaks(refs)
                    claim["references"] = [{"snaks": snaks, "snaks-order": order}]
                extra = len(quals) + len(refs)
                step(f"Add {what}" + (f" (+{extra} qualifier/source)" if extra else ""),
                     params={"action": "wbsetclaim", "claim": json.dumps(claim)},
                     expect={"claim": claim})
                claims.append(claim)
                continue

            target = matching[0]
            changed = False
            for q in quals:
                have = target.setdefault("qualifiers", {}).setdefault(q["property"], [])
                if any(snak_key(h) == snak_key(q) for h in have):
                    continue
                params = {"action": "wbsetqualifier", "claim": target["id"],
                          "property": q["property"], "snaktype": q["snaktype"]}
                if q["snaktype"] == "value":
                    params["value"] = json.dumps(q["datavalue"]["value"])
                step(f"Add qualifier to {what}: {q['property']}", params=params,
                     expect={"qualifier": [target["id"], q]})
                have.append(q)
                changed = True
            if refs:
                want = sorted(snak_key(s) for s in refs)
                existing = [sorted(snak_key(s) for ss in r.get("snaks", {}).values() for s in ss)
                            for r in target.get("references", [])]
                if want not in existing:
                    snaks, order = group_snaks(refs)
                    step(f"Add source to {what}",
                         params={"action": "wbsetreference", "statement": target["id"],
                                 "snaks": json.dumps(snaks), "snaks-order": json.dumps(order)},
                         expect={"reference": [target["id"], refs]})
                    target.setdefault("references", []).append({"snaks": snaks})
                    changed = True
            if not changed:
                step(f"{what} already on the item", "skip")
        except ValueError as exc:
            step("Can't apply this line", "error", message=str(exc))
    return Plan(steps=steps, before=before, labels=labels)


# ---------------------------------------------------------------------------
# Running a plan and verifying the result
# ---------------------------------------------------------------------------

@dataclass
class Job:
    plan: Plan
    running: bool = True
    cancelled: bool = False
    verification: list = field(default_factory=list)


# Places a stand-in ID (NEW1) can appear as an item ID or a statement GUID ("NEW1$...").
GUID_KEYS = {"id", "claim", "statement", "removed", "qualifier", "reference"}
# Edit parameters that hold JSON which may contain entity values.
JSON_PARAMS = {"wbsetclaim": "claim", "wbsetqualifier": "value", "wbsetreference": "snaks"}


def _swap(x, created, key=None):
    """Replace stand-in IDs of created items with their real IDs, in entity
    values and in item IDs or statement GUIDs, recursively."""
    if isinstance(x, dict):
        if x.get("entity-type") == "item" and x.get("id") in created:
            real = created[x["id"]]
            return {**x, "numeric-id": int(real[1:]), "id": real}
        return {k: _swap(v, created, k) for k, v in x.items()}
    if isinstance(x, list):
        return [_swap(v, created, key) for v in x]
    if isinstance(x, str) and key in GUID_KEYS:
        parts = (g.partition("$") for g in x.split("|"))
        return "|".join(created.get(head, head) + sep + rest for head, sep, rest in parts)
    return x


def swap_new_ids(params, created):
    out = {}
    for k, v in params.items():
        if JSON_PARAMS.get(params.get("action")) == k and v.startswith(("{", "[")):
            out[k] = json.dumps(_swap(json.loads(v), created))
        else:
            out[k] = _swap(v, created, k)
    return out


def run_plan(wiki, job, bot=False, stop_on_error=True, delay=0.5):
    plan = job.plan
    revs = {eid: (ent or {}).get("lastrevid") for eid, ent in plan.before.items()}
    try:
        for step in plan.steps:
            if step.status != "ready":
                continue
            if job.cancelled:
                step.status, step.message = "skip", "cancelled before it ran"
                continue
            missing = [n for n in step.needs if n not in plan.created]
            if missing:
                step.status, step.message = "failed", "the new item it needs was not created"
                if stop_on_error:
                    job.cancelled = True
                continue
            params = swap_new_ids(step.params, plan.created)
            if bot:
                params["bot"] = "1"
            if revs.get(step.item):
                params["baserevid"] = str(revs[step.item])
            try:
                res = wiki.edit(params)
            except WikiError as exc:
                step.status, step.message = "failed", str(exc)
                if stop_on_error:
                    job.cancelled = True
                continue
            if params.get("new"):
                plan.created[step.item] = res["entity"]["id"]
            rev = (res.get("pageinfo") or {}).get("lastrevid") or \
                (res.get("entity") or {}).get("lastrevid")
            if rev:
                revs[step.item] = rev
                step.revid = rev
            step.status = "done"
            time.sleep(delay)
        job.verification = verify(wiki, plan)
    finally:
        job.running = False


def _strip_hashes(x):
    if isinstance(x, dict):
        return {k: _strip_hashes(v) for k, v in x.items() if k not in ("hash", "lastrevid", "modified")}
    if isinstance(x, list):
        return [_strip_hashes(v) for v in x]
    return x


def verify(wiki, plan):
    """Re-read every edited item: planned changes must be there, nothing else may differ."""
    done = [s for s in plan.steps if s.status == "done"]
    for s in done:
        s.expect = _swap(s.expect, plan.created)
    items = sorted({s.item for s in done})
    real = {item: plan.created.get(item, item) for item in items}
    after = wiki.get_entities(real.values()) if items else {}
    report = []
    for item in items:
        # A new item's "before" is the empty item the batch started from.
        before, now = plan.before[item], after.get(real[item])
        problems = []
        if now is None:
            report.append({"item": real[item], "ok": False, "problems": ["item could not be re-read"]})
            continue
        now_claims = {c["id"]: c for cs in (now.get("claims") or {}).values() for c in cs}
        before_claims = {c["id"]: c for cs in (before.get("claims") or {}).values() for c in cs}
        touched_ids, touched_terms = set(), set()
        item_steps = [s for s in done if s.item == item]
        # A later line in the batch may remove a statement or re-set a term
        # that an earlier line changed; only the final outcome is checked.
        removed_later = {cid for s in item_steps for cid in s.expect.get("removed", [])}
        last_term = {}
        for s in item_steps:
            if "term" in s.expect:
                last_term[tuple(s.expect["term"][:2])] = s
        for s in item_steps:
            e = s.expect
            cid = (e.get("claim") or {}).get("id") or (e.get("qualifier") or e.get("reference") or [None])[0]
            if cid in removed_later and "removed" not in e:
                touched_ids.add(cid)
                continue
            if "term" in e and last_term[tuple(e["term"][:2])] is not s:
                touched_terms.add(tuple(e["term"][:2]))
                continue
            if "claim" in e:
                touched_ids.add(cid)
                got = now_claims.get(cid)
                if not got or snak_key(got["mainsnak"]) != snak_key(e["claim"]["mainsnak"]):
                    problems.append(f"line {s.line_no}: new statement {cid} not found")
            elif "removed" in e:
                touched_ids.update(e["removed"])
                for cid in e["removed"]:
                    if cid in now_claims:
                        problems.append(f"line {s.line_no}: statement {cid} still there")
            elif "qualifier" in e:
                cid, q = e["qualifier"]
                touched_ids.add(cid)
                quals = (now_claims.get(cid) or {}).get("qualifiers", {}).get(q["property"], [])
                if not any(snak_key(x) == snak_key(q) for x in quals):
                    problems.append(f"line {s.line_no}: qualifier {q['property']} not found")
            elif "reference" in e:
                cid, snaks = e["reference"]
                want = sorted(snak_key(x) for x in snaks)
                touched_ids.add(cid)
                refs = [sorted(snak_key(x) for xs in r.get("snaks", {}).values() for x in xs)
                        for r in (now_claims.get(cid) or {}).get("references", [])]
                if want not in refs:
                    problems.append(f"line {s.line_no}: source not found")
            elif "term" in e:
                section, lang, value = e["term"]
                touched_terms.add((section, lang))
                if (now.get(section) or {}).get(lang, {}).get("value") != value:
                    problems.append(f"line {s.line_no}: {section[:-1]} ({lang}) not set")
            elif "alias" in e:
                lang, value = e["alias"]
                touched_terms.add(("aliases", lang))
                if value not in [a["value"] for a in (now.get("aliases") or {}).get(lang, [])]:
                    problems.append(f"line {s.line_no}: alias ({lang}) not found")
        # Everything not planned must be exactly as it was.
        for cid, claim in before_claims.items():
            if cid in touched_ids:
                continue
            if cid not in now_claims:
                problems.append(f"statement {cid} ({claim['mainsnak']['property']}) disappeared")
            elif _strip_hashes(now_claims[cid]) != _strip_hashes(claim):
                problems.append(f"statement {cid} ({claim['mainsnak']['property']}) changed")
        for cid in now_claims.keys() - before_claims.keys() - touched_ids:
            problems.append(f"unexpected new statement {cid} (someone else editing?)")
        for section in ("labels", "descriptions", "aliases"):
            b, n = before.get(section) or {}, now.get(section) or {}
            for lang in set(b) | set(n):
                if (section, lang) not in touched_terms and \
                        _strip_hashes(b.get(lang)) != _strip_hashes(n.get(lang)):
                    problems.append(f"{section[:-1]} ({lang}) changed unexpectedly")
        report.append({"item": real[item], "ok": not problems, "problems": problems})
    return report


# ---------------------------------------------------------------------------
# Local web page
# ---------------------------------------------------------------------------

class App:
    def __init__(self, wiki):
        self.wiki = wiki
        self.token = secrets.token_urlsafe(24)
        self.plan = None
        self.job = None
        self.lock = threading.Lock()

    def steps_json(self, plan):
        return [
            {"line": s.line_no, "raw": s.raw, "item": plan.created.get(s.item, s.item),
             "label": plan.labels.get(s.item, ""),
             "action": s.action, "status": s.status, "message": s.message, "revid": s.revid}
            for s in plan.steps
        ]

    def state(self):
        job = self.job
        return {
            "username": self.wiki.username,
            "wiki": self.wiki.wiki_url,
            "steps": self.steps_json(self.plan) if self.plan else [],
            "running": bool(job and job.running),
            "verification": job.verification if job else [],
        }

    def handle(self, path, body):
        if path == "/api/state":
            return self.state()
        if path == "/api/login":
            self.wiki.login(body.get("username", "").strip(), body.get("password", ""))
            return self.state()
        if path == "/api/logout":
            self.wiki.logout()
            return self.state()
        if path == "/api/preview":
            with self.lock:
                if self.job and self.job.running:
                    raise WikiError("busy", "a batch is still running")
                self.job = None
                self.plan = make_plan(self.wiki, parse(body.get("commands", "")), body.get("summary", ""))
            return self.state()
        if path == "/api/run":
            with self.lock:
                if not self.plan:
                    raise WikiError("no-preview", "preview the commands first")
                if self.job:
                    raise WikiError("already-run", "preview again to run another batch")
                if not self.wiki.username:
                    raise WikiError("not-logged-in", "log in first")
                self.job = Job(self.plan)
                threading.Thread(
                    target=run_plan, daemon=True,
                    args=(self.wiki, self.job),
                    kwargs={"bot": bool(body.get("bot")),
                            "stop_on_error": body.get("stop_on_error", True)},
                ).start()
            return self.state()
        if path == "/api/cancel":
            if self.job:
                self.job.cancelled = True
            return self.state()
        raise WikiError("not-found", path)


class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 64   # the default of 5 drops connections under bursts
    daemon_threads = True


def make_handler(app, port):
    allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self, need_token):
            # Only answer this machine's own page: block DNS rebinding and
            # requests sent by other websites open in the browser.
            if self.headers.get("Host") not in allowed_hosts:
                return False
            return not need_token or secrets.compare_digest(
                self.headers.get("X-OSMQS-Token", ""), app.token)

        def do_GET(self):
            if self.path == "/" and self._allowed(False):
                page = PAGE.replace("__TOKEN__", app.token)
                return self._send(200, page, "text/html")
            if self.path == "/api/state" and self._allowed(True):
                return self._send(200, app.state())
            self._send(403 if self.path.startswith("/api") else 404, {"error": "not allowed"})

        def do_POST(self):
            if not self._allowed(True):
                return self._send(403, {"error": "not allowed"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "the request was not valid JSON"})
            try:
                return self._send(200, app.handle(self.path, body))
            except WikiError as exc:
                return self._send(400, {"error": exc.info or exc.code})
            except Exception as exc:  # report, don't crash the server
                return self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    return Handler


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OSM Wiki QuickStatements</title>
<style>
:root { --bg:#f7f7f5; --card:#fff; --text:#1d1d1b; --muted:#6b6b66; --line:#e2e2dc;
  --accent:#2a6f4f; --accent-text:#fff; --ready:#1f5fa8; --skip:#77776f; --bad:#b3261e; --ok:#2a6f4f; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --card:#20201e; --text:#ecece6;
  --muted:#a3a39b; --line:#34342f; --accent:#5fb48a; --accent-text:#0f1f17; --ready:#7fb0ef;
  --skip:#9a9a92; --bad:#f2827a; --ok:#6cc596; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:980px; margin:0 auto; padding:24px 16px 64px; }
h1 { font-size:22px; margin:0 0 2px; } h2 { font-size:16px; margin:0 0 12px; }
.sub { color:var(--muted); margin:0 0 20px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
label { display:block; font-size:13px; color:var(--muted); margin:0 0 4px; }
input[type=text], input[type=password], textarea { width:100%; padding:8px 10px; border:1px solid var(--line);
  border-radius:6px; background:var(--bg); color:var(--text); font:inherit; }
textarea { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px; min-height:180px; resize:vertical; }
.row { display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end; }
.row > div { flex:1 1 200px; }
button { font:inherit; padding:8px 16px; border-radius:6px; border:1px solid var(--line);
  background:var(--card); color:var(--text); cursor:pointer; }
button.primary { background:var(--accent); color:var(--accent-text); border-color:var(--accent); font-weight:600; }
button:disabled { opacity:.5; cursor:default; }
.checks { display:flex; gap:16px; flex-wrap:wrap; margin:12px 0; font-size:14px; }
.checks label { display:inline; color:var(--text); font-size:14px; }
.counts { color:var(--muted); font-size:14px; margin:8px 0 12px; }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:14px; }
th, td { text-align:left; padding:7px 8px; border-top:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:500; font-size:13px; border-top:0; }
td.num { color:var(--muted); width:1%; white-space:nowrap; }
code { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12.5px; word-break:break-all; }
a { color:var(--ready); }
.badge { display:inline-block; font-size:12px; font-weight:600; padding:1px 8px; border-radius:99px;
  border:1px solid currentColor; white-space:nowrap; }
.ready { color:var(--ready); } .skip { color:var(--skip); } .error, .failed { color:var(--bad); } .done { color:var(--ok); }
.msg { color:var(--muted); font-size:13px; }
.error-box { color:var(--bad); margin:8px 0 0; min-height:1em; }
.hidden { display:none; }
ul.verify { margin:0; padding-left:20px; }
</style>
</head>
<body>
<main>
  <h1>OSM Wiki QuickStatements</h1>
  <p class="sub">Bulk-edit <a id="wiki-link" href="https://wiki.openstreetmap.org/wiki/Data_items">OSM Wiki data items</a>. Preview first; nothing is sent until you press Run.</p>

  <section class="card" id="login-card">
    <h2>Log in</h2>
    <div id="logged-out">
      <p class="msg" style="margin-top:0">Use a bot password from
        <a id="botpw-link" href="https://wiki.openstreetmap.org/wiki/Special:BotPasswords" target="_blank">Special:BotPasswords</a>
        with "Edit existing pages" (and "High-volume editing" for big batches). It stays in memory only.</p>
      <div class="row">
        <div><label for="user">Bot username</label><input type="text" id="user" placeholder="YourName@BotName" autocomplete="username"></div>
        <div><label for="pass">Bot password</label><input type="password" id="pass" autocomplete="current-password"></div>
        <div style="flex:0 0 auto"><button class="primary" id="login-btn">Log in</button></div>
      </div>
    </div>
    <div id="logged-in" class="hidden row" style="align-items:center">
      <div>Logged in as <strong id="who"></strong></div>
      <div style="flex:0 0 auto"><button id="logout-btn">Log out</button></div>
    </div>
    <p class="error-box" id="login-error"></p>
  </section>

  <section class="card">
    <h2>Commands</h2>
    <label for="commands">One per line, QuickStatements style: <code>Q99|P13|"value"</code>, <code>-Q99|P8|"url"</code>, <code>Q99|Den|"description"</code>, new items <code>CREATE</code> then <code>LAST|P2|Q7</code>, qualifiers <code>|P50|"x"</code>, sources <code>|S11|"url"</code></label>
    <textarea id="commands" spellcheck="false" placeholder='Q99|P13|"Q[1-9]\d*(;Q[1-9]\d*)*"'></textarea>
    <div class="row" style="margin-top:12px">
      <div><label for="summary">Edit summary (optional)</label><input type="text" id="summary" placeholder="OSM Wiki QuickStatements batch"></div>
      <div style="flex:0 0 auto"><button class="primary" id="preview-btn">Preview</button></div>
    </div>
    <p class="error-box" id="preview-error"></p>
  </section>

  <section class="card hidden" id="plan-card">
    <h2 id="plan-title">Preview</h2>
    <div class="counts" id="counts"></div>
    <div class="checks" id="run-options">
      <span><input type="checkbox" id="stop-on-error" checked> <label for="stop-on-error">Stop at the first failed edit</label></span>
      <span><input type="checkbox" id="bot"> <label for="bot">Mark as bot edits (needs a bot-flagged account)</label></span>
    </div>
    <div class="row" style="margin-bottom:12px">
      <div style="flex:0 0 auto"><button class="primary" id="run-btn">Run</button></div>
      <div style="flex:0 0 auto"><button id="cancel-btn" class="hidden">Stop</button></div>
    </div>
    <div class="card hidden" id="verify-card" style="margin:0 0 12px">
      <h2>After-run check</h2>
      <div id="verify"></div>
    </div>
    <div class="table-wrap"><table>
      <thead><tr><th>Line</th><th>Item</th><th>Change</th><th>Status</th></tr></thead>
      <tbody id="steps"></tbody>
    </table></div>
  </section>
</main>
<script>
const TOKEN = "__TOKEN__";
let WIKI = "https://wiki.openstreetmap.org";
let poller = null;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

async function api(path, body) {
  const opts = {headers: {"X-OSMQS-Token": TOKEN}};
  if (body !== undefined) { opts.method = "POST"; opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const STATUS_TEXT = {ready: "will edit", skip: "no change", error: "error", done: "done", failed: "failed"};

function render(state) {
  WIKI = state.wiki || WIKI;
  $("wiki-link").href = WIKI + "/wiki/Data_items";
  $("botpw-link").href = WIKI + "/wiki/Special:BotPasswords";
  $("logged-out").classList.toggle("hidden", !!state.username);
  $("logged-in").classList.toggle("hidden", !state.username);
  $("who").textContent = state.username || "";

  const steps = state.steps || [];
  $("plan-card").classList.toggle("hidden", steps.length === 0);
  const n = (st) => steps.filter((s) => s.status === st).length;
  const ran = steps.some((s) => s.status === "done" || s.status === "failed");
  $("plan-title").textContent = state.running ? "Running…" : ran ? "Results" : "Preview";
  const parts = [];
  for (const st of ["ready", "done", "failed", "skip", "error"]) if (n(st)) parts.push(`${n(st)} ${STATUS_TEXT[st]}`);
  $("counts").textContent = parts.join(" · ");
  $("run-btn").disabled = state.running || ran || n("ready") === 0 || !state.username;
  $("run-btn").title = !state.username ? "Log in first" : "";
  $("run-options").classList.toggle("hidden", ran || state.running);
  $("cancel-btn").classList.toggle("hidden", !state.running);

  $("steps").innerHTML = steps.map((s) => {
    // NEW1, NEW2, ... are items this batch will create; they have no page yet.
    const id = /^Q\d+$/.test(s.item) ? `<a href="${WIKI}/wiki/Item:${esc(s.item)}" target="_blank">${esc(s.item)}</a>` : esc(s.item);
    const item = s.item ? `${id}${s.label ? `<div class="msg">${esc(s.label)}</div>` : ""}` : "";
    const rev = s.revid ? ` <a class="msg" href="${WIKI}/w/index.php?diff=${s.revid}" target="_blank">diff</a>` : "";
    return `<tr><td class="num">${s.line}</td><td>${item}</td>
      <td>${esc(s.action)}${s.message ? `<div class="msg">${esc(s.message)}</div>` : ""}<div><code class="msg">${esc(s.raw)}</code></div></td>
      <td><span class="badge ${s.status}">${STATUS_TEXT[s.status] || s.status}</span>${rev}</td></tr>`;
  }).join("");

  const ver = state.verification || [];
  $("verify-card").classList.toggle("hidden", ver.length === 0);
  const bad = ver.filter((v) => !v.ok);
  $("verify").innerHTML = ver.length === 0 ? "" : bad.length === 0
    ? `<p class="done" style="margin:0">All ${ver.length} edited item(s) have exactly the planned changes, and nothing else on them changed.</p>`
    : `<p class="failed" style="margin:0 0 8px">${bad.length} of ${ver.length} item(s) need a look:</p><ul class="verify">` +
      bad.map((v) => `<li><a href="${WIKI}/wiki/Item:${esc(v.item)}" target="_blank">${esc(v.item)}</a>: ${v.problems.map(esc).join("; ")}</li>`).join("") + "</ul>";

  if (state.running && !poller) poller = setInterval(refresh, 1000);
  if (!state.running && poller) { clearInterval(poller); poller = null; }
}

async function refresh() { try { render(await api("/api/state")); } catch (e) { $("preview-error").textContent = e.message; } }

async function act(btn, errBox, fn) {
  btn.disabled = true; $(errBox).textContent = "";
  try { render(await fn()); } catch (e) { $(errBox).textContent = e.message; }
  finally { btn.disabled = false; refresh(); }
}

$("login-btn").onclick = () => act($("login-btn"), "login-error", async () => {
  const st = await api("/api/login", {username: $("user").value, password: $("pass").value});
  $("pass").value = ""; return st;
});
$("pass").addEventListener("keydown", (e) => { if (e.key === "Enter") $("login-btn").click(); });
$("logout-btn").onclick = () => act($("logout-btn"), "login-error", () => api("/api/logout", {}));
$("preview-btn").onclick = async () => {
  const lines = $("commands").value.split("\n").filter((l) => l.trim()).length;
  $("preview-btn").textContent = lines > 200 ? `Checking ${lines} lines… (large batches take a minute or two)` : "Checking…";
  await act($("preview-btn"), "preview-error",
    () => api("/api/preview", {commands: $("commands").value, summary: $("summary").value}));
  $("preview-btn").textContent = "Preview";
};
$("run-btn").onclick = () => act($("run-btn"), "preview-error",
  () => api("/api/run", {bot: $("bot").checked, stop_on_error: $("stop-on-error").checked}));
$("cancel-btn").onclick = () => act($("cancel-btn"), "preview-error", () => api("/api/cancel", {}));
refresh();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Bulk-edit OSM Wiki data items from your browser.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("OSMQS_PORT", 8765)))
    parser.add_argument("--no-browser", action="store_true", help="don't open the page automatically")
    args = parser.parse_args()

    app = App(Wiki())
    server = Server(("127.0.0.1", args.port), make_handler(app, args.port))
    url = f"http://localhost:{args.port}/"
    print(f"OSM Wiki QuickStatements is running at {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
