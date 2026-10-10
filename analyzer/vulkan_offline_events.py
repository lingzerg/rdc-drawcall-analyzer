"""Reconstruct the supported Vulkan event stream without creating a GPU device.

Rules are pinned to the bundled RenderDoc revision, not to draw/chunk ordinals.
Unknown commands reject the entire map: an unknown event cost shifts later EIDs.
"""

import argparse
from collections import Counter
from copy import deepcopy
import csv
import json
from pathlib import Path
import xml.etree.ElementTree as ET


RENDERDOC_REVISION = "050034a0faa37d606ce1b8cf677dba4bc36984ea"
# Audited against ReplayLog, ReplayQueueSubmit, and the corresponding serializers.
# Deliberately exclude indirect/multi draws, secondary execution and extensions
# whose serializers inject sub-events or require a GPU-produced count.
SINGLE_EVENT_COMMANDS = set("""
vkCmdBindDescriptorSets vkCmdBindIndexBuffer vkCmdBindVertexBuffers
vkCmdBindPipeline vkCmdPipelineBarrier vkCmdPipelineBarrier2
vkCmdDebugMarkerBeginEXT vkCmdDebugMarkerEndEXT vkCmdDebugMarkerInsertEXT
vkCmdBeginDebugUtilsLabelEXT vkCmdEndDebugUtilsLabelEXT vkCmdInsertDebugUtilsLabelEXT
vkCmdDraw vkCmdDrawIndexed vkCmdDispatch
vkCmdSetStencilReference vkCmdSetScissor vkCmdSetViewport vkCmdSetDepthBias
vkCmdSetBlendConstants vkCmdSetLineWidth vkCmdSetDepthBounds
vkCmdSetStencilCompareMask vkCmdSetStencilWriteMask vkCmdPushConstants
vkCmdBeginRenderPass vkCmdNextSubpass vkCmdEndRenderPass
vkCmdCopyBuffer vkCmdFillBuffer vkCmdUpdateBuffer
vkCmdCopyImage vkCmdCopyBufferToImage vkCmdCopyImageToBuffer
vkCmdBlitImage vkCmdResolveImage vkCmdClearColorImage vkCmdClearDepthStencilImage
vkCmdClearAttachments vkCmdResetQueryPool vkCmdBeginQuery vkCmdEndQuery
vkCmdWriteTimestamp vkCmdCopyQueryPoolResults
""".split())
ROOT_EVENT_COMMANDS = {
    "Internal::Coherent Mapped Memory Write", "vkFlushMappedMemoryRanges",
    "vkUnmapMemory", "vkGetFenceStatus", "vkWaitForFences", "vkResetFences",
    "vkQueueWaitIdle", "vkDeviceWaitIdle", "vkQueuePresentKHR",
    "vkUpdateDescriptorSets", "vkUpdateDescriptorSetWithTemplate",
    "vkQueueBeginDebugUtilsLabelEXT", "vkQueueEndDebugUtilsLabelEXT",
    "vkQueueInsertDebugUtilsLabelEXT",
}
ACTION_KINDS = {"vkCmdDraw": "draw", "vkCmdDrawIndexed": "draw", "vkCmdDispatch": "dispatch"}


class UnsupportedCapture(ValueError):
    pass


def field(elem, name):
    return next((c for c in elem if c.get("name") == name), None)


def value(elem, name):
    child = field(elem, name)
    if child is None or not (child.text or "").strip():
        raise UnsupportedCapture(f"Missing {name} in {elem.get('name', elem.tag)}")
    return child.text.strip()


def number(elem, name):
    try:
        result = int(value(elem, name))
    except ValueError as exc:
        raise UnsupportedCapture(f"Invalid {name}: {exc}") from exc
    if result < 0:
        raise UnsupportedCapture(f"Negative {name}")
    return result


def command_buffer(chunk):
    refs = [c for c in chunk if c.tag == "ResourceId" and
            c.get("typename") == "VkCommandBuffer" and c.get("name") != "BakedCommandBuffer"]
    if len(refs) != 1 or not (refs[0].text or "").strip():
        raise UnsupportedCapture("Missing or ambiguous command buffer")
    return refs[0].text.strip()


def submissions(chunk):
    count = number(chunk, "submitCount")
    # Handling zero-submit calls changed after the pinned revision.
    if count == 0:
        raise UnsupportedCapture("Zero submitCount has version-dependent EID rules")
    array = field(chunk, "pSubmits")
    if array is None or len(array) != count:
        raise UnsupportedCapture("submitCount does not match pSubmits")
    result = []
    for submit in array:
        if submit.get("typename") == "VkSubmitInfo":
            count = number(submit, "commandBufferCount")
            buffers = field(submit, "pCommandBuffers")
            ids = [] if buffers is None else [(c.text or "").strip() for c in buffers]
        elif submit.get("typename") == "VkSubmitInfo2":
            count = number(submit, "commandBufferInfoCount")
            buffers = field(submit, "pCommandBufferInfos")
            ids = [] if buffers is None else [value(c, "commandBuffer") for c in buffers]
        else:
            raise UnsupportedCapture(f"Unsupported submit structure: {submit.get('typename')}")
        if len(ids) != count or any(not i or i == "0" for i in ids):
            raise UnsupportedCapture("Invalid command buffer list in submission")
        result.append(ids)
    return result


def reconstruct(root):
    """Return all event occurrences, including repeated executions of one chunk."""
    if root.findtext("./header/driver") != "Vulkan":
        raise UnsupportedCapture("Not a Vulkan capture")
    chunks_elem = root.find("chunks")
    if chunks_elem is None or chunks_elem.get("version") not in {"25", "32"}:
        raise UnsupportedCapture("Only Vulkan serialization versions 25 and 32 have been audited")
    chunks = list(chunks_elem)
    indices = [int(c.get("chunkIndex", "-1")) for c in chunks]
    if indices != list(range(len(chunks))):
        raise UnsupportedCapture("Structured chunks must be complete and contiguous from zero")
    starts = [i for i, c in enumerate(chunks) if c.get("name") == "Internal::Beginning of Capture"]
    ends = [i for i, c in enumerate(chunks) if c.get("name") == "Internal::End of Capture"]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise UnsupportedCapture("Missing or ambiguous capture boundaries")

    live, baked_ids, events, recordings = {}, set(), [], []
    root_eid, execution_count, submit_count = 1, 0, 0

    def event(eid, chunk, kind="api", **extra):
        return dict(event_id=eid, chunk_index=int(chunk.get("chunkIndex")),
                    command=chunk.get("name"), kind=kind, **extra)

    for chunk in chunks[starts[0] + 1:ends[0]]:
        name, idx = chunk.get("name"), int(chunk.get("chunkIndex"))
        try:
            if name == "vkBeginCommandBuffer":
                original = command_buffer(chunk)
                baked = value(chunk, "BakedCommandBuffer")
                if baked in baked_ids or (original in live and live[original]["end"] is None):
                    raise UnsupportedCapture("Overlapping/reused baked command buffer")
                allocate = field(chunk, "AllocateInfo")
                if allocate is None or number(allocate, "level") != 0:
                    raise UnsupportedCapture("Only primary command buffers are supported")
                recording = dict(original=original, baked=baked, begin=chunk, end=None, commands=[])
                live[original] = recording
                baked_ids.add(baked)
                recordings.append(recording)
            elif name == "vkEndCommandBuffer":
                original = command_buffer(chunk)
                recording = live.get(original)
                if recording is None or recording["end"] is not None or value(chunk, "BakedCommandBuffer") != recording["baked"]:
                    raise UnsupportedCapture("Unmatched command buffer end")
                recording["end"] = chunk
            elif name in SINGLE_EVENT_COMMANDS:
                recording = live.get(command_buffer(chunk))
                if recording is None or recording["end"] is not None:
                    raise UnsupportedCapture("Command outside its recording")
                recording["commands"].append(chunk)
            elif name in {"vkQueueSubmit", "vkQueueSubmit2"}:
                submit_count += 1
                for submit_index, buffers in enumerate(submissions(chunk)):
                    events.append(event(root_eid, chunk, "submit", submit_index=submit_index))
                    root_eid += 1
                    if not buffers:
                        # ReplayQueueSubmit inserts a No Command Buffers virtual action.
                        events.append(dict(event_id=root_eid, chunk_index=None, command="No Command Buffers", kind="boundary"))
                        root_eid += 1
                    for buffer_index, original in enumerate(buffers):
                        recording = live.get(original)
                        if recording is None or recording["end"] is None:
                            raise UnsupportedCapture(f"No complete recording for submitted command buffer {original}")
                        execution_count += 1
                        context = dict(command_buffer=original, baked_command_buffer=recording["baked"],
                                       submit_chunk_index=idx, submit_index=submit_index,
                                       buffer_index=buffer_index, execution=execution_count)
                        events.append(event(root_eid, recording["begin"], "boundary", **context))
                        root_eid += 1
                        for local_eid, cmd in enumerate(recording["commands"]):
                            events.append(event(root_eid + local_eid, cmd, ACTION_KINDS.get(cmd.get("name"), "api"),
                                                local_event_id=local_eid, **context))
                        root_eid += len(recording["commands"])
                        events.append(event(root_eid, recording["end"], "boundary", **context))
                        root_eid += 1
            elif name in ROOT_EVENT_COMMANDS:
                events.append(event(root_eid, chunk))
                root_eid += 1
            else:
                raise UnsupportedCapture(f"Unsupported event rule: {name}")
        except UnsupportedCapture as exc:
            raise UnsupportedCapture(f"chunkIndex {idx} ({name}): {exc}") from exc

    if any(r["end"] is None for r in recordings):
        raise UnsupportedCapture("Incomplete command buffer recording")
    events.append(event(root_eid, chunks[ends[0]], "capture_end"))
    return dict(source="vulkan_offline_submit_order", status="reconstructed",
                renderdoc_revision=RENDERDOC_REVISION, serialization_version=int(chunks_elem.get("version")),
                scope="Primary command buffers, audited direct commands only; no GPU replay",
                recorded_command_buffers=len(recordings), command_buffer_executions=execution_count,
                queue_submit_calls=submit_count, max_event_id=root_eid,
                draw_executions=sum(e["kind"] == "draw" for e in events),
                dispatch_executions=sum(e["kind"] == "dispatch" for e in events), events=events)


def apply_to_analysis(data, mapping):
    """Join by chunk, then expand into execution order. Never overwrite repeats."""
    expanded = {}
    for key, kind in (("draws", "draw"), ("dispatches", "dispatch")):
        recorded = data.get(key, [])
        by_chunk = {int(r["chunk_index"]): r for r in recorded}
        if len(by_chunk) != len(recorded):
            raise UnsupportedCapture(f"Duplicate recorded {kind} chunk")
        rows = []
        for e in mapping["events"]:
            if e["kind"] != kind:
                continue
            if e["chunk_index"] not in by_chunk:
                raise UnsupportedCapture(f"Missing {kind} texture/parameter row for chunk {e['chunk_index']}")
            row = deepcopy(by_chunk[e["chunk_index"]])
            if row.get("command") != e["command"]:
                raise UnsupportedCapture(f"Command mismatch at chunk {e['chunk_index']}")
            row.update({k: v for k, v in e.items() if k != "kind"})
            row[f"{kind}_index"] = len(rows) + 1
            row["event_id_source"] = mapping["source"]
            rows.append(row)
        expanded[key] = rows
    data["recorded_draw_commands"] = len(data.get("draws", []))
    executed_chunks = {r["chunk_index"] for r in expanded["draws"]}
    data["unsubmitted_draw_commands"] = sum(r["chunk_index"] not in executed_chunks for r in data.get("draws", []))
    data.update(expanded)
    data["analysis_mode"] = "offline_vulkan_events"
    data["event_id_mapped_draws"] = len(data["draws"])
    data["event_id_map"] = {k: v for k, v in mapping.items() if k != "events"}
    data["texture_usage"] = dict(Counter(t for r in data["draws"] for t in r.get("textures", [])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--compare", type=Path, help="Official rdc_replay_export action CSV")
    args = parser.parse_args()
    result = reconstruct(ET.parse(args.xml).getroot())
    if args.compare:
        with args.compare.open(encoding="utf-8-sig", newline="") as stream:
            official = [(int(r["eventId"]), int(r["mainChunkIndex"]), r["kind"]) for r in csv.DictReader(stream)]
        ours = [(e["event_id"], e["chunk_index"], e["kind"]) for e in result["events"] if e["kind"] in {"draw", "dispatch"}]
        if official != ours:
            raise SystemExit(f"Official/offline EID mismatch: official={official[:20]}, offline={ours[:20]}")
        result["official_comparison"] = dict(matched_actions=len(ours), exact_match=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "events"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
