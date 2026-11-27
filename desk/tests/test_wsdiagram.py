import json
import tempfile
import unittest
from pathlib import Path

from ray import scenes, visuals, wsdiagram

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")  # 中身は見ないので頭だけ


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class FindLoadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Cmd"
        self.canvas = write(self.root / "図解" / "2026-11-27" / "レイの構成.diagram.json", json.dumps({
            "workstation": "diagram", "version": 1, "tags": ["設計"],
            "nodes": [{"id": "a", "t": "rect", "text": "声"}, {"id": "img", "t": "image", "src": "i1"}],
            "edges": [{"a": "a", "b": "img"}],
            "assets": {"i1": "レイの構成.assets/i1.png", "unused": "レイの構成.assets/x.png", "out": "../../秘密.png"}}, ensure_ascii=False))
        (self.root / "図解" / "2026-11-27" / "レイの構成.assets").mkdir()
        (self.root / "図解" / "2026-11-27" / "レイの構成.assets" / "i1.png").write_bytes(PNG)
        (self.root / "秘密.png").write_bytes(PNG)
        write(self.root / "図解" / "流れ.mmd", "%% tags: 設計\nflowchart LR\n  A --> B\n")
        write(self.root / ".obsidian" / "隠し.mmd", "flowchart LR\n A --> B")
        write(self.root / "Notes" / "メモ.md", "図ではない")

    def tearDown(self):
        self.tmp.cleanup()

    def test_list_skips_hidden_and_other_files(self):
        names = sorted(wsdiagram.title_of(f) for f in wsdiagram.list_files(self.root))
        self.assertEqual(names, ["レイの構成", "流れ"])

    def test_find_by_name_or_path(self):
        self.assertEqual(wsdiagram.find(self.root, "レイの構成"), self.canvas)
        self.assertEqual(wsdiagram.find(self.root, "構成"), self.canvas)            # 一部だけでも
        self.assertEqual(wsdiagram.find(self.root, "レイ の 構成"), self.canvas)    # 空白は気にしない
        self.assertEqual(wsdiagram.find(self.root, path="図解/2026-11-27/レイの構成.diagram.json"), self.canvas)
        self.assertIsNone(wsdiagram.find(self.root, "ない図"))
        self.assertIsNone(wsdiagram.find(self.root, path="../外.mmd"))              # Cmd の外は見ない

    def test_load_canvas_reads_used_images_inside_folder_only(self):
        d = wsdiagram.load(self.canvas)
        self.assertEqual([n["id"] for n in d["nodes"]], ["a", "img"])
        self.assertEqual(list(d["assets"]), ["i1"])
        self.assertTrue(d["assets"]["i1"].startswith("data:image/png;base64,"))

    def test_load_mermaid_drops_tags_line(self):
        self.assertEqual(wsdiagram.load(self.root / "図解" / "流れ.mmd"), {"code": "flowchart LR\n  A --> B"})

    def test_scene_by_name_keeps_highlight_and_title(self):
        sc = scenes.parse('@画面 図解 {"name": "レイの構成", "highlight": ["a"]}\nまず声を聞きます。')[0]
        v = visuals.build(sc.kind, sc.params, {"cmd_dir": str(self.root)})
        self.assertEqual(v["kind"], "diagram")
        self.assertEqual(v["data"]["title"], "レイの構成")
        self.assertEqual(v["data"]["highlight"], ["a"])
        self.assertEqual(v["data"]["source"], "図解/2026-11-27/レイの構成.diagram.json")
        self.assertEqual(len(v["data"]["nodes"]), 2)

    def test_scene_missing_diagram_is_error_not_crash(self):
        v = visuals.build("diagram", {"name": "ない図"}, {"cmd_dir": str(self.root)})
        self.assertIn("見つかりません", v["data"]["error"])

    def test_inline_nodes_and_mermaid_pass_through(self):
        cfg = {"cmd_dir": str(self.root)}
        nodes = {"title": "t", "nodes": [{"id": "a", "t": "rect"}], "edges": []}
        self.assertEqual(visuals.build("diagram", nodes, cfg)["data"], nodes)
        self.assertEqual(visuals.build("diagram", {"code": "flowchart LR\nA-->B"}, cfg)["data"]["code"], "flowchart LR\nA-->B")


class VendorTest(unittest.TestCase):
    def test_vendored_files_exist(self):
        draw = (wsdiagram.VENDOR / "diagram-draw.js").read_text(encoding="utf-8")
        self.assertIn("root.DiagramDraw = API", draw)
        icons = (wsdiagram.VENDOR / "diagram-icons.js").read_text(encoding="utf-8")
        for name in ["server", "database", "cloud", "user"]:
            self.assertIn(f'"{name}"', icons)

    def test_sync_from_workstation_checkout(self):
        with tempfile.TemporaryDirectory() as t:
            ws, out = Path(t) / "ws", Path(t) / "out"
            write(ws / "src/renderer/diagram-draw.js", "(function(root){ root.DiagramDraw = {}; })(window);\n")
            write(ws / "src/renderer/icons.js", '// x\nwindow.ICONS = {"server":"<rect/>","other":"<path/>"};\n')
            self.assertEqual(wsdiagram.sync(ws, out), ["diagram-draw.js", "diagram-icons.js"])
            self.assertIn("root.DiagramDraw", (out / "diagram-draw.js").read_text(encoding="utf-8"))
            icons = (out / "diagram-icons.js").read_text(encoding="utf-8")
            self.assertIn('"server"', icons)
            self.assertNotIn('"other"', icons)  # 図で使わないアイコンは写さない


if __name__ == "__main__":
    unittest.main()
