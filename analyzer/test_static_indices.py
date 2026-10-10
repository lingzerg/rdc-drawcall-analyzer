import copy
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


events = load("vulkan_offline_events")
geometry = load("vulkan_static_indices")


def field(parent, name, value, tag="uint", **attrs):
    e = ET.SubElement(parent, tag, name=name, **attrs)
    if value is not None:
        e.text = str(value)
    return e


class Fixture:
    def __init__(self, index_type=0, values=(0, 1, 2, 0, 2, 3), restart=0):
        self.root = ET.fromstring('<rdc><header><driver>Vulkan</driver></header><chunks version="32"/></rdc>')
        self.chunks = self.root.find("chunks")
        width, fmt = geometry.INDEX_TYPES[index_type]
        self.raw = struct.pack("<" + fmt * len(values), *values)
        self.blobs = {0: self.raw}
        c = self.chunk("vkCreateBuffer")
        self.buffer = field(c, "CreateInfo", None, "struct")
        field(self.buffer, "pNext", None, "null")
        field(self.buffer, "size", len(self.raw))
        field(self.buffer, "flags", 0)
        field(self.buffer, "usage", 64)
        field(c, "Buffer", 10, "ResourceId")
        c = self.chunk("vkAllocateMemory")
        self.allocate = field(c, "AllocateInfo", None, "struct")
        field(self.allocate, "pNext", None, "null")
        field(self.allocate, "allocationSize", len(self.raw))
        field(c, "Memory", 20, "ResourceId")
        self.memory_binding = self.chunk("vkBindBufferMemory")
        field(self.memory_binding, "buffer", 10, "ResourceId")
        field(self.memory_binding, "memory", 20, "ResourceId")
        field(self.memory_binding, "memoryOffset", 0)
        self.initial = self.chunk("Internal::Initial Contents")
        field(self.initial, "type", 5, "enum")
        field(self.initial, "id", 20, "ResourceId")
        field(self.initial, "IsSparse", "false", "bool")
        field(self.initial, "ContentsSize", len(self.raw))
        field(self.initial, "Contents", 0, "buffer", byteLength=str(len(self.raw)))
        c = self.chunk("vkCreateGraphicsPipelines")
        field(c, "Pipeline", 30, "ResourceId")
        self.pipeline = field(c, "CreateInfo", None, "struct")
        field(self.pipeline, "pNext", None, "null")
        ia = field(self.pipeline, "pInputAssemblyState", None, "struct")
        field(ia, "pNext", None, "null")
        field(ia, "primitiveRestartEnable", restart)
        field(self.pipeline, "pDynamicState", None, "null")
        self.chunk("Internal::Beginning of Capture")
        c = self.chunk("vkBeginCommandBuffer")
        field(c, "CommandBuffer", 40, "ResourceId", typename="VkCommandBuffer")
        field(c, "BakedCommandBuffer", 41, "ResourceId")
        field(field(c, "AllocateInfo", None, "struct"), "level", 0, "enum")
        c = self.command("vkCmdBindPipeline")
        field(c, "pipelineBindPoint", 0, "enum")
        field(c, "pipeline", 30, "ResourceId")
        self.binding = self.command("vkCmdBindIndexBuffer")
        field(self.binding, "buffer", 10, "ResourceId", typename="VkBuffer")
        field(self.binding, "offset", 0)
        field(self.binding, "indexType", index_type, "enum")
        self.draw = self.command("vkCmdDrawIndexed")
        field(self.draw, "indexCount", len(values))
        field(self.draw, "firstIndex", 0)
        field(self.draw, "instanceCount", 2)
        field(self.draw, "vertexOffset", -5, "int")
        c = self.chunk("vkEndCommandBuffer")
        field(c, "CommandBuffer", 40, "ResourceId", typename="VkCommandBuffer")
        field(c, "BakedCommandBuffer", 41, "ResourceId")
        self.submit = self.chunk("vkQueueSubmit")
        field(self.submit, "submitCount", 1)
        s = ET.SubElement(field(self.submit, "pSubmits", None, "array"), "struct", typename="VkSubmitInfo")
        field(s, "commandBufferCount", 1)
        field(field(s, "pCommandBuffers", None, "array"), "commandBuffer", 40, "ResourceId", typename="VkCommandBuffer")
        self.end = self.chunk("Internal::End of Capture")

    def chunk(self, name):
        return ET.SubElement(self.chunks, "chunk", name=name)

    def command(self, name):
        c = self.chunk(name)
        field(c, "commandBuffer", 40, "ResourceId", typename="VkCommandBuffer")
        return c

    def set(self, elem, name, value):
        geometry.child(elem, name).text = str(value)

    def upload(self, offset, payload, late=False):
        c = self.chunk("Internal::Coherent Mapped Memory Write")
        field(c, "memRangeCount", 1)
        r = field(c, "MemRange", None, "struct", typename="VkMappedMemoryRange")
        field(r, "pNext", None, "null")
        field(r, "memory", 20, "ResourceId")
        field(r, "offset", offset)
        field(r, "size", len(payload))
        blob = max(self.blobs) + 1
        self.blobs[blob] = payload
        field(c, "MapData", blob, "buffer", byteLength=str(len(payload)))
        self.chunks.remove(c)
        self.chunks.insert(list(self.chunks).index(self.end if late else self.submit), c)
        return c

    def analyze(self, missing_zip=False):
        for i, c in enumerate(self.chunks):
            c.set("chunkIndex", str(i))
        mapping = events.reconstruct(self.root)
        data = {"draws": [dict(e) for e in mapping["events"] if e["kind"] == "draw"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for blob, payload in self.blobs.items():
                    archive.writestr(f"{blob:06d}", payload)
            geometry.analyze(data, self.root, mapping, None if missing_zip else path)
        return data


class StaticIndexTests(unittest.TestCase):
    def test_formats_instances_base_vertex_and_restart(self):
        for index_type, (width, _) in geometry.INDEX_TYPES.items():
            maximum = (1 << (width * 8)) - 1
            for restart, unique in ((0, 3), (1, 2)):
                f = Fixture(index_type, (0, 1, maximum, 1), restart)
                self.assertEqual(f.analyze()["draws"][0]["unique_vertex_references"], unique * 2)

    def test_memory_binding_and_first_index_offsets(self):
        f = Fixture(values=(9, 8, 7, 0, 1, 2, 0, 2, 3))
        f.set(f.memory_binding, "memoryOffset", 2)
        f.set(f.buffer, "size", len(f.raw) - 2)
        f.set(f.binding, "offset", 2)
        f.set(f.draw, "firstIndex", 1)
        f.set(f.draw, "indexCount", 6)
        r = f.analyze()["draws"][0]
        self.assertEqual(r["unique_vertex_references"], 8)
        self.assertEqual(r["unique_vertex_evidence"]["byte_offset"], 6)

    def test_complete_cpu_upload_and_repeated_submission(self):
        f = Fixture()
        f.upload(0, bytes(len(f.raw)), late=True)
        f.chunks.insert(list(f.chunks).index(f.end), copy.deepcopy(f.submit))
        rows = f.analyze()["draws"]
        self.assertEqual([r["unique_vertex_references"] for r in rows], [8, 2])
        self.assertEqual(rows[0]["unique_vertex_source"], "verified_static_index_snapshot")
        self.assertEqual(rows[1]["unique_vertex_source"], "verified_cpu_index_snapshot")

    def test_partial_overlapping_cpu_uploads_use_last_writer(self):
        f = Fixture()
        f.upload(6, struct.pack("<H", 5))
        self.assertEqual(f.analyze()["draws"][0]["unique_vertex_references"], 10)
        f.upload(6, struct.pack("<H", 0))
        self.assertEqual(f.analyze()["draws"][0]["unique_vertex_references"], 8)

    def test_full_upload_can_replace_absent_initial_bytes(self):
        f = Fixture()
        f.chunks.remove(f.initial)
        f.upload(0, bytes(len(f.raw)))
        self.assertEqual(f.analyze()["draws"][0]["unique_vertex_references"], 2)

    def test_incomplete_host_write_never_uses_old_snapshot(self):
        f = Fixture()
        upload = f.upload(0, bytes(len(f.raw)))
        geometry.child(upload, "MapData").set("byteLength", "1")
        row = f.analyze()["draws"][0]
        self.assertIsNone(row["unique_vertex_references"])
        self.assertIn("CPU upload", row["unique_vertex_reason"])

    def test_gpu_writable_buffer_is_unknown(self):
        f = Fixture()
        f.set(f.buffer, "usage", 64 | 32)
        row = f.analyze()["draws"][0]
        self.assertIsNone(row["unique_vertex_references"])
        self.assertIn("GPU-writable", row["unique_vertex_reason"])

    def test_aliasing_storage_buffer_is_unknown(self):
        f = Fixture()
        b = copy.deepcopy(f.chunks[0])
        f.set(b, "Buffer", 11)
        f.set(geometry.child(b, "CreateInfo"), "usage", 32)
        f.chunks.insert(0, b)
        bind = copy.deepcopy(f.memory_binding)
        f.set(bind, "buffer", 11)
        f.chunks.insert(4, bind)
        self.assertIsNone(f.analyze()["draws"][0]["unique_vertex_references"])

    def test_gpu_copy_destination_is_unknown_even_after_draw(self):
        f = Fixture()
        c = f.command("vkCmdCopyBuffer")
        field(c, "destBuffer", 10, "ResourceId", typename="VkBuffer")
        f.chunks.remove(c)
        f.chunks.insert(list(f.chunks).index(f.draw) + 1, c)
        row = f.analyze()["draws"][0]
        self.assertIsNone(row["unique_vertex_references"])
        self.assertIn("copy", row["unique_vertex_reason"])

    def test_missing_zip_truncated_blob_and_invalid_bounds(self):
        self.assertIsNone(Fixture().analyze(missing_zip=True)["draws"][0]["unique_vertex_references"])
        f = Fixture()
        f.blobs[0] = f.raw[:-1]
        self.assertIsNone(f.analyze()["draws"][0]["unique_vertex_references"])
        f = Fixture()
        f.set(f.draw, "indexCount", 7)
        self.assertIsNone(f.analyze()["draws"][0]["unique_vertex_references"])

    def test_zero_and_nonindexed_draws_need_no_blob(self):
        f = Fixture()
        f.set(f.draw, "instanceCount", 0)
        self.assertEqual(f.analyze(missing_zip=True)["draws"][0]["unique_vertex_references"], 0)
        f = Fixture()
        f.draw.set("name", "vkCmdDraw")
        field(f.draw, "vertexCount", 6)
        self.assertEqual(f.analyze(missing_zip=True)["draws"][0]["unique_vertex_references"], 12)

    def test_dynamic_restart_unavailable(self):
        f = Fixture()
        dynamic = geometry.child(f.pipeline, "pDynamicState")
        dynamic.tag = "struct"
        field(field(dynamic, "pDynamicStates", None, "array"), "state", 1000377004, "enum")
        self.assertIsNone(f.analyze()["draws"][0]["unique_vertex_references"])


if __name__ == "__main__":
    unittest.main()
