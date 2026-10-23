"""Tests for osmqs.py, run with: python3 -m unittest test_osmqs"""

import copy
import hashlib
import json
import threading
import unittest
import urllib.error
import urllib.request
import uuid

import osmqs


def item_value(qid):
    return {"type": "wikibase-entityid",
            "value": {"entity-type": "item", "numeric-id": int(qid[1:]), "id": qid}}


def string_snak(prop, text):
    return {"snaktype": "value", "property": prop, "hash": "h",
            "datavalue": {"type": "string", "value": text}}


def q99():
    """Shaped like OSM Wiki Item:Q99 (brand:wikidata)."""
    return {
        "id": "Q99", "type": "item", "lastrevid": 100,
        "labels": {"en": {"language": "en", "value": "brand:wikidata"}},
        "descriptions": {"en": {"language": "en", "value": "Wikidata ID of the brand"}},
        "aliases": {},
        "claims": {
            "P6": [{
                "id": "Q99$status", "type": "statement", "rank": "preferred",
                "mainsnak": {"snaktype": "value", "property": "P6", "hash": "h", "datavalue": item_value("Q13")},
                "references": [{"hash": "r1", "snaks": {"P11": [string_snak(
                    "P11", "https://wiki.openstreetmap.org/wiki/Proposal:Wikidata")]}, "snaks-order": ["P11"]}],
            }],
            "P31": [{
                "id": "Q99$docs", "type": "statement", "rank": "normal",
                "mainsnak": {"snaktype": "value", "property": "P31", "hash": "h", "datavalue": {
                    "type": "monolingualtext", "value": {"text": "Key:brand:wikidata", "language": "en"}}},
                "qualifiers": {"P50": [string_snak("P50", "Key:wikidata")]},
                "qualifiers-order": ["P50"],
            }],
            "P13": [],
        },
    }


PROPS = {
    "P6": "wikibase-item", "P8": "string", "P11": "url", "P13": "external-id",
    "P31": "monolingualtext", "P50": "string", "P20": "wikibase-property",
}


class FakeWiki(osmqs.Wiki):
    """In-memory stand-in for the OSM Wiki action API."""

    def __init__(self, strip_qualifiers_on_edit=False):
        super().__init__("https://wiki.example.org/w/api.php")
        self.entities = {"Q99": q99()}
        self.entities["Q99"]["claims"].pop("P13")
        for pid, dt in PROPS.items():
            self.entities[pid] = {"id": pid, "type": "property", "datatype": dt,
                                  "labels": {"en": {"language": "en", "value": pid.lower()}}}
        self.edits = []
        self.malformed = set()
        self.strip_qualifiers_on_edit = strip_qualifiers_on_edit

    def request(self, params, post=False):
        a = params.get("action")
        if a == "query" and params.get("meta") == "tokens":
            return {"query": {"tokens": {"logintoken": "L", "csrftoken": "C"}}}
        if a == "query" and params.get("meta") == "wbcontentlanguages":
            return {"query": {"wbcontentlanguages": {c: {"code": c} for c in ("mul", "en", "de", "fr")}}}
        if a == "login":
            if params["lgpassword"] != "right":
                return {"login": {"result": "Failed", "reason": "Incorrect password"}}
            return {"login": {"result": "Success", "lgusername": "Tester"}}
        if a == "wbsearchentities":
            hits = [{"id": eid, "label": osmqs.label_of(e)} for eid, e in self.entities.items()
                    if e.get("type") == "item" and params["search"].lower() in osmqs.label_of(e).lower()]
            return {"search": hits}
        if a == "wbgetentities":
            ents = {}
            for eid in params["ids"].split("|"):
                if eid in self.malformed:   # like Q0 on the real wiki: fails the whole request
                    raise osmqs.WikiError("no-such-entity", f'Could not find an entity with the ID "{eid}".')
                ents[eid] = copy.deepcopy(self.entities[eid]) if eid in self.entities else {"id": eid, "missing": ""}
            return {"entities": ents}
        self.edits.append(params)
        return self._edit(params)

    def _hash(self, x):
        return hashlib.sha1(json.dumps(x, sort_keys=True).encode()).hexdigest()[:8]

    def _claim(self, cid):
        for ent in self.entities.values():
            for claims in (ent.get("claims") or {}).values():
                for c in claims:
                    if c["id"] == cid:
                        return ent, c
        raise osmqs.WikiError("no-such-claim", cid)

    def _bump(self, ent):
        ent["lastrevid"] += 1
        if self.strip_qualifiers_on_edit:   # the old full-entity rewrite bug
            for claims in ent["claims"].values():
                for c in claims:
                    c.pop("qualifiers", None)
        return {"success": 1, "pageinfo": {"lastrevid": ent["lastrevid"]}}

    def _edit(self, p):
        a = p["action"]
        if a == "wbsetclaim":
            claim = json.loads(p["claim"])
            ent = self.entities[claim["id"].split("$")[0]]
            ent["claims"].setdefault(claim["mainsnak"]["property"], []).append(claim)
            return self._bump(ent)
        if a == "wbsetqualifier":
            ent, c = self._claim(p["claim"])
            snak = {"snaktype": p["snaktype"], "property": p["property"]}
            if "value" in p:
                value = json.loads(p["value"])
                dtype = PROPS[p["property"]]
                snak["datavalue"] = item_value(value["id"]) if dtype.startswith("wikibase") else \
                    {"type": "string", "value": value}
            snak["hash"] = self._hash(snak)
            c.setdefault("qualifiers", {}).setdefault(p["property"], []).append(snak)
            return self._bump(ent)
        if a == "wbsetreference":
            ent, c = self._claim(p["statement"])
            snaks = json.loads(p["snaks"])
            c.setdefault("references", []).append({"hash": self._hash(snaks), "snaks": snaks})
            return self._bump(ent)
        if a == "wbremoveclaims":
            ent = None
            for cid in p["claim"].split("|"):
                ent, c = self._claim(cid)
                ent["claims"][c["mainsnak"]["property"]].remove(c)
            return self._bump(ent)
        if a in ("wbsetlabel", "wbsetdescription"):
            ent = self.entities[p["id"]]
            section = "labels" if a == "wbsetlabel" else "descriptions"
            ent[section][p["language"]] = {"language": p["language"], "value": p["value"]}
            ent["lastrevid"] += 1
            return {"success": 1, "entity": {"lastrevid": ent["lastrevid"]}}
        if a == "wbsetaliases":
            ent = self.entities[p["id"]]
            ent["aliases"].setdefault(p["language"], []).append({"language": p["language"], "value": p["add"]})
            ent["lastrevid"] += 1
            return {"success": 1, "entity": {"lastrevid": ent["lastrevid"]}}
        if a == "wbeditentity" and p.get("new") == "item":
            eid = f"Q{1000 + sum(1 for e in self.entities.values() if e.get('type') == 'item')}"
            self.entities[eid] = {"id": eid, "type": "item", "lastrevid": 500,
                                  "labels": {}, "descriptions": {}, "aliases": {}, "claims": {}}
            return {"success": 1, "entity": {"id": eid, "lastrevid": 500}}
        raise osmqs.WikiError("unknown-action", a)


def plan_for(wiki, text, summary=""):
    return osmqs.make_plan(wiki, osmqs.parse(text), summary)


def run(wiki, plan, **kw):
    job = osmqs.Job(plan)
    osmqs.run_plan(wiki, job, delay=0, **kw)
    return job


class ParseTest(unittest.TestCase):
    def test_pipes_inside_quotes_are_kept(self):
        cmds = osmqs.parse('Q1|P13|"a|b||c"\nQ2|P8|"x"')
        self.assertEqual([c.item for c in cmds], ["Q1", "Q2"])
        self.assertEqual(cmds[0].value, ("string", "a|b||c"))

    def test_double_pipe_and_tabs_separate(self):
        cmds = osmqs.parse('Q1\tP8\t"x"||Q2|P8|"y"')
        self.assertEqual([(c.item, c.value[1]) for c in cmds], [("Q1", "x"), ("Q2", "y")])

    def test_qualifiers_sources_and_summary(self):
        (c,) = osmqs.parse('Q1|P6|Q13|P50|"q"|S11|"https://x" /* tidy up */')
        self.assertEqual(c.qualifiers, [("P50", ("string", "q"))])
        self.assertEqual(c.sources, [("P11", ("string", "https://x"))])
        self.assertEqual(c.summary, "tidy up")

    def test_terms_removal_and_values(self):
        label, remove, mono = osmqs.parse('Q1|Lde|"Marke"\n-Q1|P8|"u"\nQ1|P31|en:"Key:x"')
        self.assertEqual((label.kind, label.lang), ("label", "de"))
        self.assertEqual(remove.kind, "remove")
        self.assertEqual(mono.value, ("monolingual", "en", "Key:x"))

    def test_unclosed_quote_does_not_swallow_later_lines(self):
        cmds = osmqs.parse('Q1|P8|"abc\nQ2|P8|"ok"\r\nQ3|P8|"ok"')
        self.assertEqual([c.item for c in cmds], ["Q1", "Q2", "Q3"])
        self.assertTrue(cmds[0].error)
        self.assertEqual([c.value for c in cmds[1:]], [("string", "ok")] * 2)

    def test_ids_with_leading_zeros_are_rejected(self):
        cmds = osmqs.parse('Q0|P8|"a"\nQ01|P8|"a"\nQ1|P08|"a"\nQ1|P6|Q0')
        self.assertTrue(all(c.error for c in cmds))

    def test_errors_are_reported_per_line(self):
        cmds = osmqs.parse('CREATE|x\nQ1|P8\nX1|P8|"a"\nQ1|P8|"a"|P50\nQ1|P8|bare')
        self.assertTrue(all(c.error for c in cmds))
        self.assertIn("CREATE takes nothing else", cmds[0].error)

    def test_create_last_and_names(self):
        cmds = osmqs.parse("\n".join([
            "CREATE|@parent", 'LAST|Lmul|"socket:x"',
            "CREATE", "LAST|P22|@parent", "@parent|P46|LAST",
        ]))
        self.assertEqual([c.error for c in cmds], [""] * 5)
        self.assertEqual([c.item for c in cmds], ["NEW1", "NEW1", "NEW2", "NEW2", "NEW1"])
        self.assertEqual([cmds[3].value, cmds[4].value], [("entity", "NEW1"), ("entity", "NEW2")])

    def test_bad_references_to_new_items(self):
        cmds = osmqs.parse('LAST|P8|"a"\nQ1|P6|@nope\nCREATE|@a\nCREATE|@a\nCREATE|nope')
        self.assertIn("before any CREATE", cmds[0].error)
        self.assertIn("not named", cmds[1].error)
        self.assertIn("already the name", cmds[3].error)
        self.assertEqual([c.item for c in cmds[2:]], ["NEW1", "NEW2", "NEW3"])
        self.assertTrue(cmds[4].error)


class PlanTest(unittest.TestCase):
    def test_new_statement_with_qualifier_and_source(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, 'Q99|P13|"Q[1-9]\\d*"|P50|"note"|S11|"https://src"', "regex")
        (step,) = plan.steps
        self.assertEqual(step.status, "ready")
        self.assertEqual(step.params["action"], "wbsetclaim")
        self.assertEqual(step.params["summary"], "regex")
        claim = json.loads(step.params["claim"])
        self.assertTrue(claim["id"].startswith("Q99$"))
        self.assertEqual(claim["mainsnak"]["datavalue"], {"type": "string", "value": "Q[1-9]\\d*"})
        self.assertEqual(claim["qualifiers"]["P50"][0]["datavalue"]["value"], "note")
        self.assertEqual(claim["references"][0]["snaks-order"], ["P11"])

    def test_existing_values_are_skipped(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, "\n".join([
            "Q99|P6|Q13",
            'Q99|P6|Q13|S11|"https://wiki.openstreetmap.org/wiki/Proposal:Wikidata"',
            'Q99|P31|en:"Key:brand:wikidata"|P50|"Key:wikidata"',
            'Q99|Len|"brand:wikidata"',
        ]))
        self.assertEqual([s.status for s in plan.steps], ["skip"] * 4)

    def test_existing_statement_gets_only_the_missing_parts(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, 'Q99|P6|Q13|P50|"extra"|S11|"https://second"')
        self.assertEqual([s.params["action"] for s in plan.steps], ["wbsetqualifier", "wbsetreference"])
        self.assertEqual(plan.steps[0].params["claim"], "Q99$status")

    def test_later_lines_see_earlier_planned_changes(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, 'Q99|P8|"u"\nQ99|P8|"u"\n-Q99|P6|Q13\n-Q99|P6|Q13')
        self.assertEqual([s.status for s in plan.steps], ["ready", "skip", "ready", "skip"])

    def test_values_the_wiki_would_reject_are_errors(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, "\n".join([
            'Q99|P13|"' + "x" * 401 + '"',          # external-id over 400
            'Q99|P11|"https://' + "x" * 480 + '"',   # url under 500 is fine
            'Q99|Den|"' + "x" * 251 + '"',          # description over 250
            'Q99|Dxx-test|"text"',                  # unknown language
            'Q99|P31|zz:"Key:x"',                   # unknown monolingual language
            'Q99|P8|"   "',                         # empty text
        ]))
        self.assertEqual([s.status for s in plan.steps], ["error", "ready", "error", "error", "error", "error"])
        self.assertIn("over the wiki's limit of 400", plan.steps[0].message)
        self.assertIn("not a language code", plan.steps[3].message)

    def test_one_malformed_id_does_not_break_the_others(self):
        wiki = FakeWiki()
        wiki.malformed = {"Q5"}
        self.assertEqual(wiki.get_entities(["Q99", "Q5", "Q404"]), {
            "Q99": wiki.entities["Q99"], "Q5": None, "Q404": None})

    def test_problems_become_error_rows(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, 'Q404|P8|"x"\nQ99|P6|"text"\nQ99|P999|"x"\nQ99|P20|Q5\nnonsense')
        self.assertEqual([s.status for s in plan.steps], ["error"] * 5)
        self.assertIn("does not exist", plan.steps[0].message)
        self.assertIn("item ID", plan.steps[1].message)
        self.assertIn("P999", plan.steps[2].message)
        self.assertIn("property ID", plan.steps[3].message)


class RunAndVerifyTest(unittest.TestCase):
    BATCH = "\n".join([
        'Q99|P13|"Q[1-9]\\d*(;Q[1-9]\\d*)*"',
        'Q99|P6|Q13|S11|"https://example.org/second"',
        'Q99|Dde|"Wikidata-ID der Marke"',
        'Q99|Aen|"brand wikidata"',
    ])

    def test_run_applies_changes_and_leaves_everything_else(self):
        wiki = FakeWiki()
        wiki.login("Tester@bot", "right")
        before = copy.deepcopy(wiki.entities["Q99"])
        job = run(wiki, plan_for(wiki, self.BATCH))
        self.assertEqual([s.status for s in job.plan.steps], ["done"] * 4)
        self.assertEqual(job.verification, [{"item": "Q99", "ok": True, "problems": []}])
        after = wiki.entities["Q99"]
        # The untouched documentation statement keeps its qualifier.
        self.assertEqual(after["claims"]["P31"], before["claims"]["P31"])
        # The status statement keeps its first source and gains the second.
        self.assertEqual(len(after["claims"]["P6"][0]["references"]), 2)
        # Each edit carried the revision it was based on.
        self.assertEqual([e["baserevid"] for e in wiki.edits], ["100", "101", "102", "103"])

    def test_verification_follows_later_lines(self):
        # Add then remove the same statement, and set a description twice.
        wiki = FakeWiki()
        wiki.login("Tester@bot", "right")
        job = run(wiki, plan_for(wiki, 'Q99|P8|"u"|P50|"q"\n-Q99|P8|"u"\nQ99|Dde|"one"\nQ99|Dde|"two"'))
        self.assertEqual([s.status for s in job.plan.steps], ["done"] * 4)
        self.assertEqual(job.verification, [{"item": "Q99", "ok": True, "problems": []}])
        self.assertEqual(wiki.entities["Q99"]["descriptions"]["de"]["value"], "two")

    def test_verification_catches_collateral_damage(self):
        wiki = FakeWiki(strip_qualifiers_on_edit=True)
        wiki.login("Tester@bot", "right")
        job = run(wiki, plan_for(wiki, 'Q99|P8|"https://www.wikidata.org/entity/$1"'))
        (report,) = job.verification
        self.assertFalse(report["ok"])
        self.assertIn("Q99$docs (P31) changed", report["problems"][0])

    def test_stop_on_error_halts_the_rest(self):
        wiki = FakeWiki()
        wiki.login("Tester@bot", "right")
        plan = plan_for(wiki, 'Q99|P8|"a"\nQ99|P8|"b"')
        wiki.entities["Q99"]["lastrevid"] = 999   # pretend someone else edited
        original_edit = wiki._edit

        def conflict_once(p):
            if p.get("baserevid") == "100":
                raise osmqs.WikiError("editconflict", "Edit conflict")
            return original_edit(p)
        wiki._edit = conflict_once
        job = run(wiki, plan)
        self.assertEqual([s.status for s in job.plan.steps], ["failed", "skip"])

    def test_edits_need_login(self):
        wiki = FakeWiki()
        with self.assertRaises(osmqs.WikiError):
            wiki.edit({"action": "wbsetlabel"})
        with self.assertRaises(osmqs.WikiError) as ctx:
            wiki.login("Tester@bot", "wrong")
        self.assertEqual(ctx.exception.info, "Incorrect password")


class CreateTest(unittest.TestCase):
    BATCH = "\n".join([
        "CREATE|@parent",
        'LAST|Lmul|"socket:x"',
        "LAST|P6|Q13",
        "CREATE",
        'LAST|Lmul|"socket:x:output"',
        "LAST|P20|P6",
        "LAST|P6|Q13|P6|@parent|S6|@parent",
        "@parent|P6|LAST",
        "Q99|P6|@parent",
    ])

    def test_plan_uses_stand_in_ids(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, self.BATCH)
        self.assertEqual([s.status for s in plan.steps], ["ready"] * 9)
        self.assertEqual(plan.steps[0].action, 'Create a new item "socket:x"')
        self.assertEqual(plan.steps[0].params["data"], "{}")
        self.assertEqual(plan.steps[8].needs, ("NEW1",))
        self.assertEqual(plan.steps[6].needs, ("NEW1", "NEW2"))
        self.assertEqual(plan.labels["NEW2"], "socket:x:output")

    def test_run_creates_items_and_fills_in_their_ids(self):
        wiki = FakeWiki()
        wiki.login("Tester@bot", "right")
        job = run(wiki, plan_for(wiki, self.BATCH))
        self.assertEqual([s.status for s in job.plan.steps], ["done"] * 9)
        parent, child = job.plan.created["NEW1"], job.plan.created["NEW2"]
        self.assertEqual(wiki.entities[parent]["labels"]["mul"]["value"], "socket:x")
        child_claim = wiki.entities[child]["claims"]["P6"][0]
        self.assertTrue(child_claim["id"].startswith(child + "$"))
        self.assertEqual(child_claim["qualifiers"]["P6"][0]["datavalue"]["value"]["id"], parent)
        self.assertEqual(child_claim["references"][0]["snaks"]["P6"][0]["datavalue"]["value"],
                         {"entity-type": "item", "numeric-id": int(parent[1:]), "id": parent})
        self.assertEqual([c["mainsnak"]["datavalue"]["value"]["id"]
                          for c in wiki.entities[parent]["claims"]["P6"]], ["Q13", child])
        self.assertEqual(wiki.entities["Q99"]["claims"]["P6"][-1]["mainsnak"]["datavalue"]["value"]["id"], parent)
        self.assertEqual(sorted(v["item"] for v in job.verification), sorted([parent, child, "Q99"]))
        self.assertTrue(all(v["ok"] for v in job.verification), job.verification)
        self.assertEqual(job.plan.steps[1].item, "NEW1")
        self.assertEqual(osmqs.App(wiki).steps_json(job.plan)[1]["item"], parent)

    def test_existing_label_blocks_the_create(self):
        wiki = FakeWiki()
        plan = plan_for(wiki, 'CREATE\nLAST|Lmul|"brand:wikidata"\nLAST|P6|Q13\nQ99|P6|LAST')
        self.assertEqual([s.status for s in plan.steps], ["error"] * 4)
        self.assertIn("already exists: Q99", plan.steps[0].message)
        self.assertIn("CREATE line has an error", plan.steps[1].message)
        self.assertIn("CREATE line has an error", plan.steps[3].message)

    def test_failed_create_fails_its_lines(self):
        wiki = FakeWiki()
        wiki.login("Tester@bot", "right")
        original_edit = wiki._edit

        def no_create(p):
            if p.get("new"):
                raise osmqs.WikiError("permissiondenied", "no")
            return original_edit(p)
        wiki._edit = no_create
        job = run(wiki, plan_for(wiki, 'CREATE\nLAST|P6|Q13\nQ99|P8|"a"'), stop_on_error=False)
        self.assertEqual([s.status for s in job.plan.steps], ["failed", "failed", "done"])
        self.assertIn("was not created", job.plan.steps[1].message)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.app = osmqs.App(FakeWiki())
        self.server = osmqs.Server(("127.0.0.1", 0), None)
        port = self.server.server_address[1]
        self.server.RequestHandlerClass = osmqs.make_handler(self.app, port)
        self.base = f"http://127.0.0.1:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def call(self, path, body=None, token=True):
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode())
        if token:
            req.add_header("X-OSMQS-Token", self.app.token)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read() or b"{}") if path.startswith("/api") else resp.read().decode()
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_page_carries_the_token(self):
        status, page = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, page)

    def test_api_requires_the_token(self):
        self.assertEqual(self.call("/api/preview", {"commands": 'Q99|P8|"x"'}, token=False)[0], 403)

    def test_foreign_host_header_is_rejected(self):
        req = urllib.request.Request(self.base + "/", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)

    def test_preview_login_and_run_flow(self):
        status, state = self.call("/api/preview", {"commands": 'Q99|P8|"x"\nQ99|P6|Q13'})
        self.assertEqual([s["status"] for s in state["steps"]], ["ready", "skip"])
        self.assertEqual(self.call("/api/run", {})[1]["error"], "log in first")
        self.assertEqual(self.call("/api/login", {"username": "T@b", "password": "right"})[1]["username"], "Tester")
        self.call("/api/run", {})
        for _ in range(50):
            state = self.call("/api/state")[1]
            if not state["running"]:
                break
            threading.Event().wait(0.1)
        self.assertEqual(state["steps"][0]["status"], "done")
        self.assertTrue(state["verification"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
