"""Collections, labels and stars in the workbench catalog."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
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


if __name__ == "__main__":
    unittest.main()
