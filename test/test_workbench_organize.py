"""Collections, labels and stars in the workbench catalog."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import workbench
from workbench import CatalogManager, EventHub, PlanError
import workbench_organize as organize
from test_workbench import make_paper


def write_labels(path: Path, labels: list[dict]) -> Path:
    path.write_text(json.dumps({"schema_version": 1, "labels": labels}), encoding="utf-8")
    return path


class OrganizeTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_collections_are_the_first_level_below_a_root(self):
        papers = self.root / "papers"
        self.assertEqual(
            organize.paper_identity(str(papers / "own" / "arXiv-1"), [papers]),
            ("own/arXiv-1", "own"),
        )
        self.assertEqual(organize.paper_identity(str(papers / "arXiv-1"), [papers]), ("arXiv-1", ""))
        self.assertEqual(organize.paper_identity(str(papers), [papers]), ("papers", ""))
        self.assertEqual(organize.paper_identity("/elsewhere/arXiv-2", [papers]), ("arXiv-2", ""))

    def test_labels_are_validated_and_reach_papers_and_problems(self):
        path = write_labels(self.root / "labels.json", [
            {"target": "own/arXiv-1234.56789", "label": "frechet"},
            {"target": "own/arXiv-1234.56789v1/OP-001", "label": "excluded", "hidden": True,
             "title": "Ongoing work"},
            {"target": "own/other", "label": ""},
            "nonsense",
        ])
        labels, errors = organize.read_labels(path)
        self.assertEqual(len(labels), 2)
        self.assertEqual(len(errors), 2)
        papers = self.root / "papers"
        paper = str(papers / "own" / "arXiv-1234.56789v1")
        value = {
            "papers": [{"key": paper, "path": paper}],
            "reviews": [
                {"paperDirectory": paper, "problemId": "OP-001", "problemKey": f"{paper}::OP-001"},
                {"paperDirectory": paper, "problemId": "OP-002", "problemKey": f"{paper}::OP-002"},
            ],
        }
        organize.organize(value, [papers], labels, {"papers": set(), "problems": {f"{paper}::OP-002"}})
        self.assertEqual(value["papers"][0]["collection"], "own")
        self.assertEqual([label["label"] for label in value["papers"][0]["labels"]], ["frechet"])
        self.assertFalse(value["papers"][0]["hidden"])
        first, second = value["reviews"]
        self.assertEqual(sorted(label["label"] for label in first["labels"]), ["excluded", "frechet"])
        self.assertTrue(first["hidden"])
        self.assertFalse(first["starred"])
        self.assertEqual([label["label"] for label in second["labels"]], ["frechet"])
        self.assertTrue(second["starred"])

    def test_missing_or_wrong_label_files_are_reported(self):
        self.assertIn("not found", organize.read_labels(self.root / "absent.json")[1][0])
        (self.root / "old.json").write_text('{"labels": []}', encoding="utf-8")
        self.assertIn("schema_version", organize.read_labels(self.root / "old.json")[1][0])

    def test_label_source_notices_changes(self):
        path = write_labels(self.root / "labels.json", [{"target": "a", "label": "x"}])
        source = organize.LabelSource([path])
        self.assertEqual([label.label for label in source.current()], ["x"])
        self.assertFalse(source.refresh())
        time.sleep(0.01)
        write_labels(path, [{"target": "a", "label": "x"}, {"target": "b", "label": "longer y"}])
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10_000_000))
        self.assertTrue(source.refresh())
        self.assertEqual(len(source.current()), 2)

    def test_stars_persist_in_the_state_directory(self):
        stars = organize.StarStore(self.root / "state")
        stars.set("problems", "p::OP-001", True)
        stars.set("papers", "p", True)
        stars.set("papers", "p", False)
        self.assertEqual(organize.StarStore(self.root / "state").load(),
                         {"papers": set(), "problems": {"p::OP-001"}})
        with self.assertRaises(ValueError):
            stars.set("drafts", "x", True)

    def test_catalog_snapshot_and_star_requests(self):
        papers = self.root / "papers"
        paper = make_paper(papers / "own")
        labels = organize.LabelSource([write_labels(self.root / "labels.json", [
            {"target": f"own/{paper.name}/OP-001", "label": "excluded", "hidden": True},
        ])])
        manager = CatalogManager([papers], self.root / "manuscripts", EventHub())
        self.addCleanup(manager.close)
        self.assertTrue(manager.wait_until_ready(8))
        app = object.__new__(workbench.WorkbenchApplication)
        app.catalog = manager
        app.hub = manager.hub
        app.labels = labels
        app.stars = organize.StarStore(self.root / "state")
        app.label_command = []
        manager.organizer = app.organize
        row = manager.snapshot()["reviews"][0]
        self.assertEqual((row["collection"], row["hidden"], row["starred"]), ("own", True, False))
        with patch.object(app.hub, "publish") as publish:
            app.set_star({"kind": "problems", "key": row["problemKey"], "starred": True})
            publish.assert_called_once()
        self.assertTrue(manager.snapshot()["reviews"][0]["starred"])
        self.assertTrue(manager.review_detail(row["itemKey"])["starred"])
        with self.assertRaises(PlanError):
            app.set_star({"kind": "problems", "key": row["problemKey"], "starred": "yes"})


TAG_ACTIONS = [
    {"id": "tag", "label": "Add tag", "kinds": ["paper", "problem"],
     "fields": [{"name": "value", "label": "Tag", "required": True, "suggestions": ["frechet"]},
                {"name": "note", "label": "Note", "type": "textarea"}]},
    {"id": "untag", "label": "Remove tag frechet", "kinds": ["paper"],
     "target": "own/arXiv-1234.56789", "values": {"value": "frechet"}},
    {"id": "worth", "label": "Worth attempting?", "kinds": ["problem"],
     "fields": [{"name": "value", "label": "Worth", "type": "select", "value": "skip",
                 "options": [["attempt", "attempt"], ["skip", "skip"]]}]},
]

# Records each request and rewrites the labels file, like a project's command.
COMMAND = """
import json, pathlib, sys
request = json.load(sys.stdin)
if request["values"].get("value") == "fail":
    print("cannot do that", file=sys.stderr)
    sys.exit(3)
labels = pathlib.Path(sys.argv[1])
value = json.loads(labels.read_text())
value["labels"].append({"target": request["target"], "label": request["values"]["value"]})
labels.write_text(json.dumps(value))
pathlib.Path(sys.argv[2]).write_text(json.dumps(request))
print("tagged " + request["target"])
"""


class LabelActionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def labels_file(self, actions=TAG_ACTIONS) -> Path:
        path = self.root / "labels.json"
        path.write_text(json.dumps({"schema_version": 1, "labels": [], "actions": actions}),
                        encoding="utf-8")
        return path

    def test_actions_are_validated_and_offered_where_they_apply(self):
        path = self.labels_file(TAG_ACTIONS + [
            {"id": "Bad Id", "label": "x"},
            {"id": "pick", "label": "Pick", "fields": [{"name": "v", "label": "V", "type": "select"}]},
            {"id": "where", "label": "Where", "kinds": ["manuscript"]},
        ])
        actions, errors = organize.read_actions(path)
        self.assertEqual([action.id for action in actions], ["tag", "untag", "worth"])
        self.assertEqual(len(errors), 3)
        tag, untag, worth = actions
        self.assertTrue(untag.applies_to("paper", "own/arXiv-1234.56789v2"))
        self.assertFalse(untag.applies_to("paper", "own/other"))
        self.assertFalse(untag.applies_to("problem", "own/arXiv-1234.56789/OP-001"))
        self.assertTrue(worth.applies_to("problem", "own/other/OP-002"))
        self.assertNotEqual(tag.key, untag.key)
        self.assertEqual(tag.key, organize.read_actions(path)[0][0].key)

        papers = self.root / "papers"
        paper = str(papers / "own" / "arXiv-1234.56789v2")
        value = {"papers": [{"key": paper, "path": paper}],
                 "reviews": [{"paperDirectory": paper, "problemId": "OP-001", "problemKey": "k"}]}
        organize.organize(value, [papers], [], {"papers": set(), "problems": set()}, actions)
        self.assertEqual([a["id"] for a in value["papers"][0]["labelActions"]], ["tag", "untag"])
        self.assertEqual([a["id"] for a in value["reviews"][0]["labelActions"]], ["tag", "worth"])
        self.assertEqual(value["papers"][0]["labelActions"][1]["values"], {"value": "frechet"})
        self.assertEqual(value["reviews"][0]["labelActions"][1]["fields"][0]["value"], "skip")
        organize.organize(value, [papers], [], {"papers": set(), "problems": set()})
        self.assertEqual(value["papers"][0]["labelActions"], [])

    def test_values_are_checked_before_the_command_runs(self):
        tag, untag, worth = organize.read_actions(self.labels_file())[0]
        self.assertEqual(organize.action_values(tag, {"value": " frechet ", "note": ""}),
                         {"value": "frechet", "note": ""})
        self.assertEqual(organize.action_values(untag, {"value": "other"}), {"value": "frechet"})
        for action, values, message in (
            (tag, {"value": ""}, "required"),
            (tag, {"value": 3}, "text"),
            (worth, {"value": "maybe"}, "options"),
            (tag, "nonsense", "object"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                organize.action_values(action, values)

    def test_the_application_runs_offered_actions_only(self):
        labels_path = self.labels_file()
        record = self.root / "request.json"
        script = self.root / "command.py"
        script.write_text(COMMAND, encoding="utf-8")
        app = object.__new__(workbench.WorkbenchApplication)
        app.labels = organize.LabelSource([labels_path])
        app.label_command = [sys.executable, str(script), str(labels_path), str(record)]
        app.label_action_lock = threading.Lock()
        app.hub = EventHub()
        app.catalog = type("Catalog", (), {"version": 1})()
        tag = app.labels.current_actions()[0]
        target = "own/arXiv-1234.56789v2/OP-001"
        result = app.run_label_action(
            {"kind": "problem", "target": target, "key": tag.key, "values": {"value": "new"}}
        )
        self.assertEqual(result["message"], f"tagged {target}")
        self.assertEqual(json.loads(record.read_text())["values"], {"value": "new", "note": ""})
        self.assertEqual([label.label for label in app.labels.current()], ["new"])
        with self.assertRaisesRegex(PlanError, "cannot do that"):
            app.run_label_action(
                {"kind": "problem", "target": target, "key": tag.key, "values": {"value": "fail"}}
            )
        with self.assertRaisesRegex(PlanError, "no longer offered"):
            app.run_label_action({"kind": "problem", "target": target, "key": "0" * 16})
        untag = app.labels.current_actions()[1]
        with self.assertRaisesRegex(PlanError, "no longer offered"):
            app.run_label_action({"kind": "paper", "target": "own/other", "key": untag.key})
        app.label_command = []
        with self.assertRaisesRegex(PlanError, "--label-command"):
            app.run_label_action({"kind": "paper", "target": "own/other", "key": tag.key})

    def test_collections_are_import_targets(self):
        papers = self.root / "papers"
        for name in ("own", "watchmen", ".loose-ends"):
            (papers / name).mkdir(parents=True)
        make_paper(papers)  # a paper directly in the root is not a collection
        (papers / "README.md").write_text("", encoding="utf-8")
        self.assertEqual(
            organize.import_targets([papers]),
            [papers / "own", papers / "watchmen", papers],
        )
        self.assertEqual(organize.import_targets([self.root / "absent"]), [self.root / "absent"])


if __name__ == "__main__":
    unittest.main()
