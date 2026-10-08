import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("analyzer", Path(__file__).with_name("mobile_rdc_batch_analyze.py"))
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


class ReplayAccuracyTests(unittest.TestCase):
    def action(self, eid, **overrides):
        row = dict(eventId=eid, actionId=eid, mainChunkIndex=42, flags=65538,
                   numIndices=6, numInstances=0, indexed=1, indexOffset=3, baseVertex=-2,
                   vertexOffset=0, instanceOffset=0, drawIndex=0, name="DrawIndexed()",
                   path="GBufferPass", meshNames="Mesh_PCG_01", textureNames="StreamTex:Wall_PCG_01_D_mainview",
                   uniqueVertices=4, kind="draw")
        row.update(overrides)
        return row

    def load(self, rows):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            return analyzer.data_from_replay_export(path)

    def test_repeated_chunk_preserves_each_execution(self):
        data = self.load([self.action(10), self.action(20)])
        self.assertEqual([r["event_id"] for r in data["draws"]], [10, 20])
        self.assertEqual(analyzer.total_vertex_count(data["draws"]), 12)
        self.assertEqual(analyzer.geometry_total_text(data["draws"], unique=True), "8")
        self.assertEqual(data["draws"][0]["category_by_d_texture"], "pcg")

    def test_instanced_zero_is_not_noninstanced_default(self):
        data = self.load([self.action(10), self.action(20, flags=196610, numInstances=0, uniqueVertices=0),
                          self.action(30, flags=196610, numInstances=3)])
        self.assertEqual([analyzer.draw_vertex_count(r) for r in data["draws"]], [6, 0, 18])
        self.assertEqual([r["unique_vertex_references"] for r in data["draws"]], [4, 0, 12])

    def test_offline_indirect_is_unknown_and_not_fake_eid(self):
        data = {"draws": [dict(command="vkCmdDrawIndexedIndirect", chunk_index=99,
                                event_id=1000, estimated_event_id=2000, index_count=0, instance_count=1)]}
        analyzer.prepare_offline_rows(data)
        self.assertIsNone(analyzer.draw_vertex_count(data["draws"][0]))
        self.assertEqual(analyzer.geometry_total_text(data["draws"]), "Unknown")
        self.assertEqual(analyzer.eid_value(data["draws"][0]), "chunk:99")
        known = self.load([self.action(10)])["draws"]
        self.assertEqual(analyzer.geometry_total_text(known + data["draws"]), "6 (+1 unknown draws)")

    def test_duplicate_eid_rejected(self):
        with self.assertRaises(ValueError):
            self.load([self.action(10), self.action(10)])

    def test_unknown_not_zero_in_report(self):
        data = self.load([self.action(10, uniqueVertices="")])
        with tempfile.TemporaryDirectory() as directory:
            report = analyzer.write_html_report("test", Path(directory), Path("test.rdc"), data,
                                                analyzer.build_category_groups(data))
            html = report.read_text(encoding="utf-8")
            self.assertIn("Unique Vertex Index", html)
            self.assertIn("Unknown", html)
            self.assertIn("Official replay:", html)
            self.assertNotIn("estimated EID", html)
        self.assertEqual(analyzer.total_vertex_count(data["draws"]), 6)


if __name__ == "__main__":
    unittest.main()
