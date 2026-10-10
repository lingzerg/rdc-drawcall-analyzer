import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


events = load("events", "vulkan_offline_events.py")
analyzer = load("analyzer", "mobile_rdc_batch_analyze.py")


class Capture:
    def __init__(self, version="32"):
        self.root = ET.Element("rdc")
        ET.SubElement(ET.SubElement(self.root, "header"), "driver").text = "Vulkan"
        self.chunks = ET.SubElement(self.root, "chunks", version=version)

    def add(self, command, cb=None, baked=None, **fields):
        c = ET.SubElement(self.chunks, "chunk", name=command, chunkIndex=str(len(self.chunks)))
        boundary = command in {"vkBeginCommandBuffer", "vkEndCommandBuffer"}
        if cb is not None:
            ET.SubElement(c, "ResourceId", name="CommandBuffer" if boundary else "commandBuffer",
                          typename="VkCommandBuffer").text = str(cb)
        if baked is not None:
            ET.SubElement(c, "ResourceId", name="BakedCommandBuffer").text = str(baked)
        if command == "vkBeginCommandBuffer":
            ET.SubElement(ET.SubElement(c, "struct", name="AllocateInfo"), "enum", name="level").text = "0"
        for name, val in fields.items():
            ET.SubElement(c, "uint", name=name).text = str(val)
        return c

    def submit(self, *groups, submit2=False):
        c = self.add("vkQueueSubmit2" if submit2 else "vkQueueSubmit", submitCount=len(groups))
        a = ET.SubElement(c, "array", name="pSubmits")
        for group in groups:
            s = ET.SubElement(a, "struct", typename="VkSubmitInfo2" if submit2 else "VkSubmitInfo")
            ET.SubElement(s, "uint", name="commandBufferInfoCount" if submit2 else "commandBufferCount").text = str(len(group))
            b = ET.SubElement(s, "array", name="pCommandBufferInfos" if submit2 else "pCommandBuffers")
            for cb in group:
                parent = ET.SubElement(b, "struct", typename="VkCommandBufferSubmitInfo") if submit2 else b
                ET.SubElement(parent, "ResourceId", name="commandBuffer", typename="VkCommandBuffer").text = str(cb)
        return c


def golden():
    # The frame sequence/chunk IDs and expected EIDs below were exported from
    # native/tests/vulkan_eid_fixture.cpp by the bundled official replay DLL.
    c = Capture()
    for _ in range(24):
        c.add("initialization")
    c.add("Internal::Beginning of Capture")
    c.add("vkBeginCommandBuffer", 93, 113)
    label = c.add("vkCmdBeginDebugUtilsLabelEXT", 93)
    ET.SubElement(label, "string", name="pLabelName").text = "First recorded"
    c.add("vkBeginCommandBuffer", 95, 114)
    c.add("vkCmdBeginRenderPass", 95)
    c.add("vkCmdBindPipeline", 95)
    c.add("vkCmdBindIndexBuffer", 95)
    c.add("vkCmdDrawIndexed", 95, indexCount=6, instanceCount=2)
    c.add("vkCmdEndRenderPass", 95)
    c.add("vkEndCommandBuffer", 95, 114)
    c.add("vkCmdBeginRenderPass", 93)
    c.add("vkCmdBindPipeline", 93)
    c.add("vkCmdDraw", 93, vertexCount=3, instanceCount=1)
    c.add("vkCmdEndRenderPass", 93)
    c.add("vkCmdEndDebugUtilsLabelEXT", 93)
    c.add("vkEndCommandBuffer", 93, 113)
    c.add("vkBeginCommandBuffer", 97, 115)
    c.add("vkCmdBindPipeline", 97)
    c.add("vkCmdDispatch", 97)
    c.add("vkEndCommandBuffer", 97, 115)
    c.add("vkGetFenceStatus")
    c.submit([97, 95, 93], [])
    c.add("vkQueueWaitIdle")
    c.submit([95])
    c.add("vkQueueWaitIdle")
    c.add("vkBeginCommandBuffer", 93, 116)
    c.add("vkCmdBeginRenderPass", 93)
    c.add("vkCmdBindPipeline", 93)
    c.add("vkCmdBindIndexBuffer", 93)
    c.add("vkCmdDrawIndexed", 93, indexCount=6, instanceCount=2)
    c.add("vkCmdEndRenderPass", 93)
    c.add("vkEndCommandBuffer", 93, 116)
    c.submit([93])
    c.add("vkQueueWaitIdle")
    c.add("Internal::End of Capture")
    return c


class OfflineVulkanTests(unittest.TestCase):
    def test_official_golden_eids(self):
        for version in ("25", "32"):
            c = golden()
            c.chunks.set("version", version)
            result = events.reconstruct(c.root)
            actual = [(e["event_id"], e["chunk_index"], e["kind"]) for e in result["events"] if e["kind"] in {"draw", "dispatch"}]
            self.assertEqual(actual, [(5, 42, "dispatch"), (11, 31, "draw"), (18, 36, "draw"),
                                      (30, 31, "draw"), (39, 53, "draw")])
            self.assertEqual(result["max_event_id"], 43)

    def parse(self, root, directory):
        path = Path(directory) / "capture.xml"
        ET.ElementTree(root).write(path, encoding="utf-8")
        subprocess.run([sys.executable, str(Path(__file__).with_name("mobile_texture_xml_probe.py")), str(path)],
                       check=True, capture_output=True)
        return json.loads(path.with_name("capture_texture_probe.json").read_text(encoding="utf-8")), path

    def test_end_to_end_repeated_draw_and_marker_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            data, path = self.parse(golden().root, directory)
            analyzer.prepare_offline_rows(data)
            mapping = analyzer.reconstruct_vulkan_events(data, path)
            self.assertIsNotNone(mapping)
            self.assertEqual([r["event_id"] for r in data["draws"]], [11, 18, 30, 39])
            self.assertEqual(data["draws"][1]["renderpass"], "First recorded")
            self.assertNotEqual(data["draws"][0]["renderpass"], "First recorded")
            self.assertNotEqual(data["draws"][3]["renderpass"], "First recorded")
            self.assertEqual(data["recorded_draw_commands"], 3)
            self.assertEqual(analyzer.total_vertex_count(data["draws"]), 39)
            report = analyzer.write_html_report("test", Path(directory), path, data, analyzer.build_category_groups(data))
            html = report.read_text(encoding="utf-8")
            self.assertIn("EID (offline)", html)
            self.assertNotIn("Official EID mapped draws", html)
            self.assertNotIn("EID unavailable", html)
            self.assertNotIn("recorded commands</span>", html)

    def test_unknown_command_fails_closed_without_mutating_rows(self):
        for command in ("vkCmdDrawIndexedIndirectCount", "vkCmdExecuteCommands", "vkCmdFutureExtension"):
            c = golden()
            c.chunks[31].set("name", command)
            with tempfile.TemporaryDirectory() as directory:
                data, path = self.parse(c.root, directory)
                analyzer.prepare_offline_rows(data)
                before = [dict(r) for r in data["draws"]]
                self.assertIsNone(analyzer.reconstruct_vulkan_events(data, path))
                self.assertEqual(data["draws"], before)
                self.assertIn("chunkIndex 31", data["offline_eid_error"])
                self.assertEqual(data["analysis_mode"], "offline_commands")

    def test_queue_submit2_layout(self):
        c = Capture()
        c.add("Internal::Beginning of Capture")
        c.add("vkBeginCommandBuffer", 1, 2)
        c.add("vkCmdDraw", 1)
        c.add("vkEndCommandBuffer", 1, 2)
        c.submit([1], [1], submit2=True)
        c.add("Internal::End of Capture")
        result = events.reconstruct(c.root)
        self.assertEqual([e["event_id"] for e in result["events"] if e["kind"] == "draw"], [3, 7])

    def test_invalid_inputs_are_rejected(self):
        edits = [lambda c: c.chunks.set("version", "999"),
                 lambda c: c.chunks[31].set("chunkIndex", "30"),
                 lambda c: c.chunks.remove(c.chunks[-1]),
                 lambda c: events.field(events.field(c.chunks[25], "AllocateInfo"), "level").__setattr__("text", "1"),
                 lambda c: events.field(c.chunks[33], "BakedCommandBuffer").__setattr__("text", "999"),
                 lambda c: events.field(c.chunks[45], "submitCount").__setattr__("text", "0")]
        for edit in edits:
            c = golden()
            edit(c)
            with self.assertRaises(events.UnsupportedCapture):
                events.reconstruct(c.root)

    def test_unsubmitted_draw_not_counted(self):
        c = golden()
        c.chunks.remove(c.chunks[-1])
        c.add("vkBeginCommandBuffer", 100, 200)
        c.add("vkCmdDraw", 100, vertexCount=99, instanceCount=1)
        c.add("vkEndCommandBuffer", 100, 200)
        c.add("Internal::End of Capture")
        with tempfile.TemporaryDirectory() as directory:
            data, path = self.parse(c.root, directory)
            analyzer.prepare_offline_rows(data)
            self.assertIsNotNone(analyzer.reconstruct_vulkan_events(data, path))
            self.assertEqual(len(data["draws"]), 4)
            self.assertEqual(data["unsubmitted_draw_commands"], 1)

    def test_binding_state_is_per_command_buffer_and_bind_point(self):
        c = Capture()
        c.add("Internal::Beginning of Capture")
        def bind(cb, ds, point=0):
            chunk = c.add("vkCmdBindDescriptorSets", cb, firstSet=0)
            ET.SubElement(chunk, "enum", name="pipelineBindPoint").text = str(point)
            ET.SubElement(ET.SubElement(chunk, "array", name="pDescriptorSets"), "ResourceId", typename="VkDescriptorSet").text = str(ds)
        c.add("vkBeginCommandBuffer", 1, 11)
        bind(1, 101)
        c.add("vkBeginCommandBuffer", 2, 12)
        bind(2, 202)
        bind(1, 303, point=1)
        c.add("vkCmdDraw", 1)
        c.add("vkCmdDraw", 2)
        c.add("Internal::End of Capture")
        with tempfile.TemporaryDirectory() as directory:
            data, _ = self.parse(c.root, directory)
            self.assertEqual([r["descriptor_sets"] for r in data["draws"]], [["101"], ["202"]])


if __name__ == "__main__":
    unittest.main()
