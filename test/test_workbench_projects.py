"""Several projects in one workbench: pools, paper roots, and paper repositories."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import open_problem_common as common
import workbench
from workbench import CatalogManager, EventHub, PlanError
import workbench_organize as organize
import workbench_projects as projects
from workbench_tasks import build_plan
from test_workbench import make_paper


# Records each request in a file named after the pool, then rewrites nothing.
COMMAND = """
import json, pathlib, sys
request = json.load(sys.stdin)
pathlib.Path(sys.argv[1]).write_text(json.dumps(request))
print("done in " + sys.argv[2])
"""


def labels_file(path: Path, root: Path | None, labels: list[dict], actions: list[dict] = ()) -> Path:
    value = {"schema_version": 1, "labels": labels, "actions": list(actions)}
    if root is not None:
        value["root"] = str(root)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class ProjectTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        # Two pools with the same collection, paper directory and problem id.
        self.pools = {}
        for name in ("pool-a", "pool-b"):
            pool = self.root / name
            paper = make_paper(pool / "papers" / "own")
            (pool / "manuscripts").mkdir(parents=True)
            self.pools[name] = (pool, paper)
        self.repository = self.root / "spanners"
        (self.repository / "socg").mkdir(parents=True)
        (self.repository / "socg" / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}x\\end{document}\n", encoding="utf-8"
        )

    def projects_file(self, extra: str = "") -> Path:
        path = self.root / "config" / "projects.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"""
schema_version = 1

[[project]]
name = "pool-a"
kind = "pool"
papers = "../pool-a/papers"
manuscripts = "../pool-a/manuscripts"
labels = ["../pool-a/labels.json"]
label_command = ["{sys.executable}", "{self.root / 'command.py'}", "{self.root / 'a.json'}", "pool-a"]

[[project]]
name = "pool-b"
id = "problem-commons/pool-b"
kind = "pool"
papers = "{self.root / 'pool-b' / 'papers'}"
manuscripts = "{self.root / 'pool-b' / 'manuscripts'}"
labels = ["{self.root / 'pool-b' / 'labels.json'}"]
label_command = ["{sys.executable}", "{self.root / 'command.py'}", "{self.root / 'b.json'}", "pool-b"]

[[project]]
kind = "paper"
path = "../spanners"

[[project]]
name = "gone"
kind = "paper"
path = "../not-cloned"
{extra}
""", encoding="utf-8")
        return path

    def test_projects_file_and_paper_repositories(self):
        loaded = projects.load_projects(self.projects_file())
        self.assertEqual([project.name for project in loaded], ["pool-a", "pool-b", "spanners", "gone"])
        pool_a, pool_b, spanners, gone = loaded
        self.assertEqual(pool_a.papers, self.root / "pool-a" / "papers")
        self.assertEqual(pool_a.id, "pool-a")
        self.assertEqual(pool_b.id, "problem-commons/pool-b")
        self.assertEqual(pool_b.label_command[-1], "pool-b")
        self.assertEqual(spanners.kind, "paper")
        self.assertEqual([item.entry for item in spanners.paper_manuscripts], ["socg/main.tex"])
        self.assertIn("no loose-ends.toml", " ".join(spanners.problems))
        self.assertEqual(gone.status, "missing")
        self.assertFalse(gone.available)
        self.assertIs(projects.project_for(loaded, self.pools["pool-b"][1] / "OP-001"), pool_b)
        self.assertIsNone(projects.project_for(loaded, self.root / "elsewhere"))
        with self.assertRaisesRegex(projects.ProjectError, "two projects"):
            projects.load_projects(self.projects_file('[[project]]\nname = "pool-a"\npapers = "../pool-a/papers"\n'))
        bad = self.root / "bad.toml"
        bad.write_text('schema_version = 1\n[[project]]\npapers = "x"\nlabel_command = "python3 x"\n', encoding="utf-8")
        with self.assertRaisesRegex(projects.ProjectError, "not a shell string"):
            projects.load_projects(bad)

    def test_shared_paper_description(self):
        (self.repository / "loose-ends.toml").write_text("""
schema_version = 1
id = "oriented-spanners"
title = "Oriented spanners"
writing_profile = "writing/cg"

[[manuscript]]
name = "socg"
entry = "socg/main.tex"
build = ["latexmk", "-pdf", "main.tex"]

[[manuscript]]
entry = "journal/paper.tex"
pdf = "journal/build/paper.pdf"
""", encoding="utf-8")
        shared = projects.read_paper_project(self.repository)
        self.assertEqual(shared["id"], "oriented-spanners")
        socg, journal = shared["manuscripts"]
        self.assertEqual((socg.name, socg.build, socg.pdf_path), ("socg", ("latexmk", "-pdf", "main.tex"), "socg/main.pdf"))
        self.assertEqual((journal.name, journal.pdf_path), ("journal-paper", "journal/build/paper.pdf"))
        (self.repository / "loose-ends.toml").write_text(
            'schema_version = 1\n[[manuscript]]\nentry = "../outside.tex"\n', encoding="utf-8"
        )
        with self.assertRaisesRegex(projects.ProjectError, "inside the repository"):
            projects.read_paper_project(self.repository)

    def test_labels_and_skips_stay_in_their_pool(self):
        (pool_a, paper_a), (pool_b, paper_b) = self.pools.values()
        root_a, root_b = pool_a / "papers", pool_b / "papers"
        target = f"own/{paper_a.name}/OP-001"
        file_a = labels_file(pool_a / "labels.json", None, [
            {"target": target, "label": "excluded", "hidden": True, "skip": True},
        ])
        file_b = labels_file(pool_b / "labels.json", root_b, [{"target": target, "label": "frechet"}])
        source = organize.LabelSource([file_a, file_b], {file_a: str(root_a)})
        value = {"reviews": [
            {"paperDirectory": str(paper), "problemId": "OP-001", "problemKey": f"{paper}::OP-001"}
            for paper in (paper_a, paper_b)
        ]}
        roots = {str(root_a): {"name": "pool-a"}, str(root_b): {"name": "pool-b"}}
        organize.organize(value, [root_a, root_b], source.current(), {"papers": set(), "problems": set()},
                          projects=roots)
        first, second = value["reviews"]
        self.assertEqual([label["label"] for label in first["labels"]], ["excluded"])
        self.assertEqual([label["label"] for label in second["labels"]], ["frechet"])
        self.assertEqual((first["project"]["name"], second["project"]["name"]), ("pool-a", "pool-b"))
        # The exclusion of pool A keeps runs off pool A's problem only.
        labels, _ = organize.read_labels(labels_file(
            self.root / "env.json", root_a, [{"target": target, "label": "excluded", "skip": True}]
        ))
        self.assertTrue(organize.skip_reason(labels, paper_a, "OP-001"))
        self.assertIsNone(organize.skip_reason(labels, paper_b, "OP-001"))

    def test_label_actions_run_the_command_of_their_pool(self):
        (self.root / "command.py").write_text(COMMAND, encoding="utf-8")
        tag = {"id": "tag", "label": "Add tag", "kinds": ["problem"],
               "fields": [{"name": "value", "label": "Tag", "required": True}]}
        (pool_a, paper_a), (pool_b, _) = self.pools.values()
        labels_file(pool_a / "labels.json", pool_a / "papers", [], [tag])
        labels_file(pool_b / "labels.json", pool_b / "papers", [], [tag])
        app = workbench.WorkbenchApplication(
            paths=[], manuscripts=None, state_directory=self.root / "state",
            projects=projects.load_projects(self.projects_file()),
        )
        self.addCleanup(app.close)
        self.assertEqual(app.paths, [pool_a / "papers", pool_b / "papers"])
        self.assertEqual(app.manuscript_roots, [pool_a / "manuscripts", pool_b / "manuscripts"])
        self.assertIn(self.repository, app.allowed_roots)
        actions = app.labels.current_actions()
        self.assertEqual(len({action.key for action in actions}), 2)
        action_b = next(action for action in actions if action.root == str(pool_b / "papers"))
        target = f"own/{paper_a.name}/OP-001"
        result = app.run_label_action({"kind": "problem", "target": target, "key": action_b.key,
                                       "values": {"value": "x"}})
        self.assertEqual(result["message"], "done in pool-b")
        self.assertTrue((self.root / "b.json").is_file())
        self.assertFalse((self.root / "a.json").exists())
        snapshot = app.organize({"papers": [], "reviews": [
            {"paperDirectory": str(paper_a), "problemId": "OP-001", "problemKey": "k"},
        ], "manuscripts": [{"path": str(pool_b / "manuscripts" / "m"), "name": "m"}]})
        row = snapshot["reviews"][0]
        self.assertEqual([action["root"] for action in row["labelActions"]], [str(pool_a / "papers")])
        self.assertEqual(snapshot["manuscripts"][0]["project"]["name"], "pool-b")

    def test_manuscripts_of_every_project_and_write_routing(self):
        (pool_a, paper_a), (pool_b, paper_b) = self.pools.values()
        for pool in (pool_a, pool_b):
            draft = pool / "manuscripts" / "same-name" / "draft-001"
            draft.mkdir(parents=True)
            common.write_json(draft / "manifest.json", {"schema_version": 1, "title": pool.name})
            common.write_json(draft / "paper-result.json", {"title": pool.name})
        manager = CatalogManager(
            [pool_a / "papers", pool_b / "papers"],
            [pool_a / "manuscripts", pool_b / "manuscripts"],
            EventHub(),
        )
        self.addCleanup(manager.close)
        self.assertTrue(manager.wait_until_ready(10))
        listed = manager.snapshot()["manuscripts"]
        self.assertEqual(sorted(item["latest"]["title"] for item in listed), ["pool-a", "pool-b"])

        loaded = projects.load_projects(self.projects_file())

        def plan(targets):
            return build_plan(
                {"action": "write", "targets": [{"kind": "paper", "path": str(path)} for path in targets],
                 "options": {}},
                project_root=workbench.PROJECT_ROOT, allowed_roots=[self.root],
                manuscripts=self.root / "default", catalog_version=1,
                manuscripts_for=lambda path: (
                    projects.project_for(loaded, path).manuscripts
                    if projects.project_for(loaded, path) else None
                ),
            )["units"][0]["argv"]

        argv = plan([paper_b])
        self.assertEqual(argv[argv.index("--output-dir") + 1], str(pool_b / "manuscripts"))
        with self.assertRaisesRegex(PlanError, "one project"):
            plan([paper_a, paper_b])

    def test_paper_repositories_show_their_reviews_and_offer_review_tasks(self):
        import hashlib
        import review_manuscript

        app = workbench.WorkbenchApplication(
            paths=[], manuscripts=None, state_directory=self.root / "state",
            projects=projects.load_projects(self.projects_file()),
        )
        self.addCleanup(app.close)
        entry = self.repository / "socg" / "main.tex"
        review = self.repository / "manuscript-reviews" / "2026-11-06-socg-math-codex-abcdef"
        review.mkdir(parents=True)
        common.write_json(review / "manifest.json", {
            "schema_version": 1, "kind": "math", "generated_at": "2026-11-06T10:00:00Z",
            "manuscript": {"name": "socg", "entry": "socg/main.tex"},
            "files": [{"path": "socg/main.tex", "sha256": hashlib.sha256(entry.read_bytes()).hexdigest()}],
            "revision": {"commit": "0123456789abcdef", "uncommitted": []},
            "build": {"status": "ok"}, "requested_agent": "claude",
        })
        common.write_json(review / "review.json", {"verdict": "needs_minor_revision", "findings": [{"id": "M-001"}]})
        (review / "math-review.md").write_text("# Review\n\nM-001\n", encoding="utf-8")

        spanners, gone = app.paper_projects()
        self.assertIs(app.paper_projects()[0], spanners)  # cached
        self.assertIsNot(app.paper_projects(max_age=0)[0], spanners)  # read again
        self.assertEqual((spanners["name"], gone["status"]), ("spanners", "missing"))
        self.assertIsNone(spanners["source"]["commit"])  # not a git checkout
        [manuscript] = spanners["manuscriptsInRepository"]
        self.assertEqual((manuscript["name"], manuscript["entryPath"], manuscript["pdfState"]),
                         ("socg", str(entry), "missing"))
        [row] = manuscript["reviews"]
        self.assertEqual((row["verdict"], row["current"], row["build"], row["agent"]),
                         ("needs_minor_revision", True, "ok", "claude"))
        self.assertEqual(row["report"], str(review / "math-review.md"))
        # The PDF is compared with its sources; an edited source makes the review outdated.
        pdf = entry.parent / "main.pdf"
        pdf.write_bytes(b"%PDF")
        now = entry.stat().st_mtime
        os.utime(pdf, (now + 60, now + 60))
        self.assertEqual(review_manuscript.manuscript_overview(
            self.repository, spanners_manuscript := projects.detect_manuscripts(self.repository)[0]
        )["pdfState"], "current")
        entry.write_text(entry.read_text(encoding="utf-8") + "% edited\n", encoding="utf-8")
        os.utime(pdf, (now - 60, now - 60))
        overview = review_manuscript.manuscript_overview(self.repository, spanners_manuscript)
        self.assertEqual(overview["pdfState"], "older than its sources")
        self.assertEqual(overview["reviews"][0]["changed"], ["socg/main.tex"])

        plan = app.create_plan({
            "action": "manuscript-review",
            "targets": [{"kind": "latex", "path": str(entry)}],
            "options": {"reviewKind": "exposition", "audience": "computational geometers",
                        "expositionAgent": "claude", "buildMode": "none"},
        })
        [unit] = plan["units"]
        self.assertTrue(unit["argv"][2].endswith("review_manuscript.py"))
        self.assertEqual(unit["argv"][3:8], [str(entry), "--repository", str(self.repository),
                                             "--exposition", "--no-build"])
        self.assertIn("--exposition-agent", unit["argv"])
        self.assertEqual(plan["title"], "Manuscript review: main.tex")
        self.assertIn(f"latex:{entry}", unit["resources"])
        with self.assertRaisesRegex(PlanError, "not a recognized"):
            app.create_plan({"action": "manuscript-review",
                             "targets": [{"path": str(self.repository / "socg" / "missing.tex")}]})
        outside = self.root / "elsewhere.tex"
        outside.write_text("\\documentclass{article}\n", encoding="utf-8")
        with self.assertRaisesRegex(PlanError, "outside the configured roots"):
            app.create_plan({"action": "manuscript-review", "targets": [{"path": str(outside)}]})


if __name__ == "__main__":
    unittest.main()
