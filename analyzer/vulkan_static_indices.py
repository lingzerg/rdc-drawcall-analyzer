"""Count unique indices only where complete, CPU-known bytes are provable.

This is deliberately not a buffer replay engine. Any possible write overlapping
the requested range anywhere in the frame rejects that range, even after a draw,
except complete host-upload records applied in the reconstructed event order.
"""

from collections import Counter, OrderedDict, defaultdict
import struct
import zipfile
import zlib


READ_ONLY_USAGE = 1 | 2 | 4 | 16 | 64 | 128 | 256
INDEX_TYPES = {0: (2, "H"), 1: (4, "I"), 1000265000: (1, "B")}
MAX_BLOB_BYTES = 128 * 1024 * 1024
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_INDEX_BYTES = 16 * 1024 * 1024


class Unavailable(ValueError):
    pass


def child(elem, name):
    if elem is None:
        raise Unavailable("Missing resource metadata")
    result = next((c for c in elem if c.get("name") == name), None)
    if result is None:
        raise Unavailable(f"Missing field: {name}")
    return result


def text(elem, name):
    result = (child(elem, name).text or "").strip()
    if not result:
        raise Unavailable(f"Empty field: {name}")
    return result


def number(elem, name):
    try:
        result = int(text(elem, name))
    except ValueError as exc:
        raise Unavailable(f"Invalid integer: {name}") from exc
    if result < 0:
        raise Unavailable(f"Negative integer: {name}")
    return result


def no_extensions(elem):
    return child(elem, "pNext").tag == "null"


class SnapshotReader:
    def __init__(self, path):
        self.archive = zipfile.ZipFile(path) if path is not None else None
        self.cache = OrderedDict()
        self.cache_bytes = 0

    def read(self, blob, size, offset, length):
        if self.archive is None:
            raise Unavailable("Binary snapshot ZIP unavailable")
        name = f"{blob:06d}"
        if name not in self.cache:
            matches = [i for i in self.archive.infolist() if i.filename == name]
            if len(matches) != 1 or matches[0].file_size != size:
                raise Unavailable("Missing or incomplete snapshot blob")
            if size > MAX_BLOB_BYTES:
                raise Unavailable("Snapshot exceeds offline read limit")
            # Read to EOF so zipfile verifies the CRC, not just the requested slice.
            contents = self.archive.read(matches[0])
            while self.cache and self.cache_bytes + size > MAX_CACHE_BYTES:
                _, old = self.cache.popitem(last=False)
                self.cache_bytes -= len(old)
            self.cache[name] = contents
            self.cache_bytes += len(contents)
        self.cache.move_to_end(name)
        if len(self.cache[name]) != size:
            raise Unavailable("Conflicting snapshot sizes")
        if offset < 0 or offset + length > len(self.cache[name]):
            raise Unavailable("Index range exceeds snapshot")
        return self.cache[name][offset:offset + length]

    def close(self):
        if self.archive is not None:
            self.archive.close()
        self.cache.clear()


class StaticInventory:
    def __init__(self, root):
        self.buffers, self.bindings, self.memories, self.snapshots, self.pipelines = {}, {}, {}, {}, {}
        self.tainted = defaultdict(list)
        self.host_writes = {}
        self.uploads = defaultdict(list)
        self.global_reason = None
        self.chunks = list(root.find("chunks"))
        try:
            self.build()
        except (Unavailable, ValueError) as exc:
            self.global_reason = f"Unsupported resource metadata: {exc}"

    def taint(self, memory, start, size, reason):
        if memory not in self.memories or start + size > self.memories[memory]:
            raise Unavailable("Invalid memory write/alias range")
        if size:
            self.tainted[memory].append((start, start + size, reason))

    def taint_buffer(self, buffer, reason):
        if buffer not in self.bindings or buffer not in self.buffers:
            raise Unavailable("Unresolved writable buffer binding")
        memory, offset = self.bindings[buffer]
        self.taint(memory, offset, self.buffers[buffer]["size"], reason)

    def build(self):
        start = next(i for i, c in enumerate(self.chunks) if c.get("name") == "Internal::Beginning of Capture")
        images = []
        for c in self.chunks[:start]:
            name = c.get("name")
            if name == "vkCreateBuffer":
                info = child(c, "CreateInfo")
                rid = text(c, "Buffer")
                if rid in self.buffers:
                    raise Unavailable("Duplicate buffer creation")
                self.buffers[rid] = dict(size=number(info, "size"), usage=number(info, "usage"),
                                         simple=no_extensions(info) and number(info, "flags") == 0)
                if number(info, "flags") & 7:
                    raise Unavailable("Sparse buffer aliasing is not supported")
                if number(info, "usage") & 0x20000 or not no_extensions(info):
                    raise Unavailable("Buffer device addresses or extended usage are not tracked")
            elif name == "vkAllocateMemory":
                info = child(c, "AllocateInfo")
                if not no_extensions(info):
                    raise Unavailable("Extended/external memory allocation is not supported")
                rid = text(c, "Memory")
                if rid in self.memories:
                    raise Unavailable("Duplicate memory allocation")
                self.memories[rid] = number(info, "allocationSize")
            elif name == "vkBindBufferMemory":
                rid = text(c, "buffer")
                if rid in self.bindings:
                    raise Unavailable("Rebound memory is not supported")
                self.bindings[rid] = (text(c, "memory"), number(c, "memoryOffset"))
            elif name == "vkBindImageMemory":
                images.append(text(c, "memory"))
            elif name == "vkCreateImage" and number(child(c, "CreateInfo"), "flags") & 7:
                raise Unavailable("Sparse image aliasing is not supported")
            elif (name.startswith("vkBind") and "Memory" in name) or name == "vkQueueBindSparse":
                raise Unavailable(f"Unsupported memory binding: {name}")
            elif name == "Internal::Initial Contents" and text(c, "type") == "5":
                rid = text(c, "id")
                if rid in self.snapshots or text(c, "IsSparse") != "false":
                    raise Unavailable("Ambiguous or sparse initial memory contents")
                blob = child(c, "Contents")
                size = number(c, "ContentsSize")
                if blob.tag != "buffer" or int(blob.get("byteLength", "-1")) != size:
                    raise Unavailable("Incomplete initial memory contents")
                self.snapshots[rid] = (int(blob.text), size)
            elif name == "vkCreateGraphicsPipelines":
                rid = text(c, "Pipeline")
                try:
                    info = child(c, "CreateInfo")
                    ia = child(info, "pInputAssemblyState")
                    dynamic = child(info, "pDynamicState")
                    if not no_extensions(info) or not no_extensions(ia):
                        raise Unavailable("Extended pipeline state")
                    if dynamic.tag != "null" and any(int(v.text) not in range(9) for v in child(dynamic, "pDynamicStates")):
                        raise Unavailable("Dynamic primitive restart or extended state")
                    restart = number(ia, "primitiveRestartEnable")
                    if restart not in (0, 1):
                        raise Unavailable("Invalid primitive restart state")
                    self.pipelines[rid] = bool(restart)
                except (Unavailable, ValueError):
                    self.pipelines[rid] = None

        for rid, (memory, offset) in self.bindings.items():
            if rid not in self.buffers or memory not in self.memories or offset + self.buffers[rid]["size"] > self.memories[memory]:
                raise Unavailable("Incomplete buffer memory binding")
            buffer = self.buffers[rid]
            if not buffer["simple"] or buffer["usage"] & ~READ_ONLY_USAGE:
                self.taint_buffer(rid, "Potential GPU-writable buffer or memory alias")
        for memory in images:
            if memory not in self.memories:
                raise Unavailable("Unresolved image memory alias")
            self.taint(memory, 0, self.memories[memory], "Image memory alias is not tracked")

        # GPU writes are excluded across the whole frame. Complete host writes
        # are the only mutations replayed, and are applied later in event order.
        for c in self.chunks[start + 1:]:
            name = c.get("name")
            if name in {"Internal::Coherent Mapped Memory Write", "vkFlushMappedMemoryRanges"}:
                ranges = [e for e in c.iter("struct") if e.get("typename") == "VkMappedMemoryRange"]
                if len(ranges) != number(c, "memRangeCount"):
                    raise Unavailable("Unresolved host memory writes")
                for r in ranges:
                    memory, offset, size = text(r, "memory"), number(r, "offset"), number(r, "size")
                    if not no_extensions(r):
                        raise Unavailable("Extended host memory write")
                    if size == 0xffffffffffffffff:
                        size = self.memories.get(memory, 0) - offset
                    if size < 0:
                        raise Unavailable("Invalid host write size")
                    if memory not in self.memories or offset + size > self.memories[memory]:
                        raise Unavailable("Invalid host memory range")
                    try:
                        payload = child(c, "MapData")
                        if len(ranges) != 1 or payload.tag != "buffer" or int(payload.get("byteLength", "-1")) != size:
                            raise Unavailable("Ambiguous host upload")
                        self.host_writes[int(c.get("chunkIndex"))] = (memory, offset, int(payload.text), size)
                    except (Unavailable, ValueError):
                        self.taint(memory, offset, size, "CPU upload payload is missing or incomplete")
            elif name == "vkUnmapMemory":
                memory = text(c, "memory")
                self.taint(memory, 0, self.memories.get(memory, 0), "Unmapped memory may contain CPU writes")
            elif name in {"vkCmdCopyBuffer", "vkCmdFillBuffer", "vkCmdUpdateBuffer", "vkCmdCopyImageToBuffer", "vkCmdCopyQueryPoolResults"}:
                targets = [e for e in c if e.tag == "ResourceId" and e.get("typename") == "VkBuffer"
                           and e.get("name") in {"buffer", "dstBuffer", "destBuffer"}]
                if len(targets) != 1:
                    raise Unavailable(f"Unresolved write target: {name}")
                self.taint_buffer(targets[0].text.strip(), "GPU copy/update destination is not replayed")

    def apply_upload(self, chunk_index):
        if chunk_index in self.host_writes:
            memory, offset, blob, size = self.host_writes[chunk_index]
            self.uploads[memory].append((offset, offset + size, blob, size))

    def read_range(self, reader, memory, offset, length):
        # Partition by last writer without filling unknown bytes with zeroes.
        segments = [(offset, offset + length, None)]
        for start, end, blob, size in self.uploads[memory]:
            updated = []
            for left, right, source in segments:
                if left >= end or right <= start:
                    updated.append((left, right, source))
                    continue
                if left < start:
                    updated.append((left, start, source))
                updated.append((max(left, start), min(right, end), (start, blob, size)))
                if right > end:
                    updated.append((end, right, source))
            segments = updated
        parts, evidence = [], []
        for left, right, source in segments:
            if source is None:
                if memory not in self.snapshots:
                    raise Unavailable("Initial index memory snapshot unavailable")
                blob, size = self.snapshots[memory]
                if size < self.memories[memory]:
                    raise Unavailable("Partial initial memory snapshot")
                origin, kind = 0, "initial"
            else:
                origin, blob, size = source
                kind = "cpu_upload"
            parts.append(reader.read(blob, size, left - origin, right - left))
            evidence.append(dict(kind=kind, blob=blob, offset=left - origin, length=right - left))
        return b"".join(parts), evidence

    def indexed_count(self, cmd, binding, pipeline, reader):
        if self.global_reason:
            raise Unavailable(self.global_reason)
        if binding is None:
            raise Unavailable("Index buffer binding unavailable")
        buffer, binding_offset, index_type = binding
        if index_type not in INDEX_TYPES:
            raise Unavailable("Unsupported index format")
        restart = self.pipelines.get(pipeline)
        if restart is None:
            raise Unavailable("Primitive restart state unavailable")
        if buffer not in self.buffers or buffer not in self.bindings:
            raise Unavailable("Index buffer memory unavailable")
        if not self.buffers[buffer]["usage"] & 64:
            raise Unavailable("Buffer is not an index buffer")
        width, fmt = INDEX_TYPES[index_type]
        length = number(cmd, "indexCount") * width
        offset = binding_offset + number(cmd, "firstIndex") * width
        if binding_offset % width or offset + length > self.buffers[buffer]["size"]:
            raise Unavailable("Invalid or out-of-bounds index range")
        if length > MAX_INDEX_BYTES:
            raise Unavailable("Index range exceeds offline read limit")
        memory, memory_offset = self.bindings[buffer]
        offset += memory_offset
        for begin, end, reason in self.tainted[memory]:
            if offset < end and offset + length > begin:
                raise Unavailable(reason)
        raw, segments = self.read_range(reader, memory, offset, length)
        values = {v[0] for v in struct.iter_unpack("<" + fmt, raw)}
        if restart:
            values.discard((1 << (width * 8)) - 1)
        return len(values), dict(index_buffer=buffer, memory=memory, segments=segments,
                                 byte_offset=offset, byte_length=length, index_width=width,
                                 primitive_restart=restart)


def analyze(data, root, mapping, zip_path=None):
    inventory = StaticInventory(root)
    rows = {r["event_id"]: r for r in data["draws"]}
    reasons, sources = Counter(), Counter()
    reader = None
    reader_error = None
    try:
        reader = SnapshotReader(zip_path)
    except (OSError, zipfile.BadZipFile) as exc:
        reader_error = f"Snapshot ZIP unavailable: {exc}"
    binding, pipeline = None, None
    try:
        for event in mapping["events"]:
            if event["chunk_index"] is None:
                continue
            cmd = inventory.chunks[event["chunk_index"]]
            name = cmd.get("name")
            inventory.apply_upload(event["chunk_index"])
            if name == "vkBeginCommandBuffer":
                binding, pipeline = None, None
            elif name == "vkCmdBindIndexBuffer":
                try:
                    binding = (text(cmd, "buffer"), number(cmd, "offset"), number(cmd, "indexType"))
                except Unavailable:
                    binding = None
            elif name == "vkCmdBindPipeline":
                try:
                    if number(cmd, "pipelineBindPoint") == 0:
                        pipeline = text(cmd, "pipeline")
                except Unavailable:
                    pipeline = None
            elif event["kind"] == "draw":
                row = rows[event["event_id"]]
                row["unique_vertex_references"] = None
                row["unique_vertex_source"] = "unknown"
                try:
                    instances = number(cmd, "instanceCount")
                    count = number(cmd, "indexCount" if name == "vkCmdDrawIndexed" else "vertexCount")
                    if count == 0 or instances == 0:
                        unique, source = 0, "zero_draw_parameters"
                    elif name == "vkCmdDraw":
                        unique, source = count, "nonindexed_draw_parameters"
                    else:
                        if reader_error:
                            raise Unavailable(reader_error)
                        unique, evidence = inventory.indexed_count(cmd, binding, pipeline, reader)
                        row["unique_vertex_evidence"] = evidence
                        source = ("verified_cpu_index_snapshot" if any(s["kind"] == "cpu_upload" for s in evidence["segments"])
                                  else "verified_static_index_snapshot")
                    row["unique_vertex_references"] = unique * instances
                    row["unique_vertex_source"] = source
                    row["unique_vertex_reason"] = ""
                    sources[source] += 1
                except (Unavailable, OSError, zipfile.BadZipFile, KeyError, RuntimeError, EOFError, zlib.error) as exc:
                    reason = str(exc)
                    row["unique_vertex_reason"] = reason
                    reasons[reason] += 1
    finally:
        if reader is not None:
            reader.close()
    data["offline_unique_vertex_index"] = dict(
        known_draws=sum(sources.values()), unknown_draws=sum(reasons.values()),
        sources=dict(sources), unknown_reasons=dict(reasons),
        policy="Initial snapshots plus complete chronological CPU uploads; potential GPU writes/copies excluded",
    )
