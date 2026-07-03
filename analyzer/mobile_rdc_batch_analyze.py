import argparse
import csv
import html
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


DEFAULT_RENDERDOCCMD = Path(r"C:\Program Files\RenderDoc\renderdoccmd.exe")


def clean_input_path(raw):
    text = str(raw or "").strip()
    if text.startswith("& "):
        text = text[2:].strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1]
    return Path(text)


def draw_vertex_count(row):
    base = row.get("index_count") or row.get("vertex_count") or 0
    inst = row.get("instance_count") or 1
    try:
        return int(base) * int(inst)
    except (TypeError, ValueError):
        return 0


def total_vertex_count(rows):
    return sum(draw_vertex_count(r) for r in rows)


def normalize_draw_command(name):
    match = re.search(r"(DrawIndexedInstanced|DrawIndexed|DrawInstanced|DrawAuto|Draw)\b", str(name or ""))
    return match.group(1) if match else str(name or "")


def merge_signature(row):
    return (
        str(row.get("renderpass") or "").replace("\\", "/"),
        normalize_draw_command(row.get("command")),
        int(row.get("index_count") or row.get("vertex_count") or 0),
        int(row.get("instance_count") or 1),
    )


def merge_enhanced_rows(xml_data, enhanced_rows):
    if not enhanced_rows:
        return 0
    buckets = defaultdict(list)
    for row in xml_data.get("draws", []):
        buckets[merge_signature(row)].append(row)

    used = Counter()
    merged = 0
    for enhanced in enhanced_rows:
        sig = merge_signature(enhanced)
        used[sig] += 1
        candidates = buckets.get(sig) or []
        if used[sig] > len(candidates):
            continue
        target = candidates[used[sig] - 1]
        target["xml_chunk_index"] = target.get("chunk_index")
        target["event_id"] = enhanced.get("chunk_index")
        target["enhanced_match"] = True
        for key in (
            "index_texture",
            "index_is_d_texture",
            "category_by_d_texture",
            "d_textures",
            "textures",
            "texture_count",
            "mesh_name",
            "pass",
        ):
            target[key] = enhanced.get(key)
        merged += 1
    return merged


def renderpass_label(row):
    value = row.get("renderpass")
    if value in (None, ""):
        return "no_marker"
    if value == 0 or value == "0":
        return "outside"
    if isinstance(value, int) or str(value).isdigit():
        return f"RenderPass #{value}"
    return str(value)


def renderpass_major_label(row):
    explicit = row.get("pass")
    if explicit:
        return str(explicit)
    label = renderpass_label(row)
    parts = [p for p in label.replace("\\", "/").split("/") if p]
    if len(parts) <= 1:
        return label

    priority_patterns = [
        "GBufferPass",
        "ShadowCasterPass",
        "TransparentForwardPass",
        "OffScreenTransparentForwardPass",
        "Offscreen",
        "RenderPass",
        "Camera.Render",
    ]
    for pattern in priority_patterns:
        for part in parts:
            if pattern.lower() in part.lower():
                return part
    for part in parts:
        if "pass" in part.lower():
            return part
    return parts[0]


def eid_value(row):
    if row.get("event_id") not in (None, ""):
        return str(row.get("event_id"))
    value = row.get("chunk_index")
    if value in (None, ""):
        return "-"
    return str(value)


def format_eids(rows, limit=80):
    eids = [eid_value(r) for r in sorted(rows, key=lambda r: r.get("draw_index") or 0)]
    shown = eids[:limit]
    suffix = "" if len(eids) <= limit else f"; ... +{len(eids) - limit}"
    return "; ".join(shown) + suffix


def html_eids(rows, label="EID/chunkIndex", limit=16):
    eids = [eid_value(r) for r in sorted(rows, key=lambda r: r.get("draw_index") or 0)]
    text = "; ".join(eids)
    if len(eids) <= limit:
        return f"<code>{html.escape(text)}</code>"
    shown = "; ".join(eids[:limit])
    return (
        '<details class="eids">'
        f"<summary>{len(eids)} {html.escape(label)}</summary>"
        f"<code>{html.escape(shown)}; ...</code>"
        f"<div><code>{html.escape(text)}</code></div>"
        "</details>"
    )


def html_folded_values(values, label, empty_label="-"):
    clean = [str(v) for v in values if str(v)]
    if not clean:
        return f'<span class="muted">{html.escape(empty_label)}</span>'
    body = "<br>".join(f"<code>{html.escape(v)}</code>" for v in clean)
    return (
        '<details class="fold-list">'
        f"<summary>{len(clean)} {html.escape(label)}</summary>"
        f"<div>{body}</div>"
        "</details>"
    )


def build_renderpass_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[renderpass_major_label(row)].append(row)
    return sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)


def build_index_texture_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        tex = row.get("index_texture")
        if tex:
            groups[tex].append(row)
    return sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)


def aggregate_by_index_texture(rows):
    return build_index_texture_groups(rows)


def category_sort_key(item):
    category, group = item
    return (category == "unclassified", -len(group), category)


def find_renderdoccmd(explicit=None):
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    repo_root = Path(__file__).resolve().parent.parent
    candidates.append(repo_root / "third_party" / "renderdoc" / "renderdoccmd.exe")
    candidates.append(DEFAULT_RENDERDOCCMD)
    for c in candidates:
        if c.exists():
            return c
    return Path("renderdoccmd.exe")


def run_convert(renderdoccmd, rdc_path, xml_path, force=False):
    if xml_path.exists() and not force and xml_path.stat().st_mtime >= rdc_path.stat().st_mtime:
        return
    cmd = [
        str(renderdoccmd),
        "convert",
        f"--filename={rdc_path}",
        f"--output={xml_path}",
        "--input-format=rdc",
        "--convert-format=xml",
    ]
    subprocess.run(cmd, check=True)


def detect_driver(xml_path):
    root = ET.parse(xml_path).getroot()
    driver = root.find("./header/driver")
    return (driver.text or "").strip() if driver is not None else ""


VULKAN_DRAW_PREFIXES = (
    "vkCmdDraw",
    "vkCmdDispatch",
    "vkCmdTraceRays",
    "vkCmdDrawMeshTasks",
)

VULKAN_EVENT_KEYWORDS = (
    "BeginRenderPass",
    "EndRenderPass",
    "NextSubpass",
    "BeginRendering",
    "EndRendering",
    "Clear",
    "Copy",
    "Blit",
    "Resolve",
    "FillBuffer",
    "UpdateBuffer",
    "PipelineBarrier",
    "WaitEvents",
    "SetEvent",
    "ResetEvent",
    "WriteTimestamp",
    "BeginQuery",
    "EndQuery",
    "ResetQueryPool",
    "CopyQueryPoolResults",
    "DebugMarker",
    "DebugUtilsLabel",
    "ExecuteCommands",
    "BuildAccelerationStructures",
)

D3D11_NO_EVENT_NAMES = {
    "ID3DUserDefinedAnnotation::BeginEvent",
    "ID3DUserDefinedAnnotation::EndEvent",
    "ID3DUserDefinedAnnotation::SetMarker",
    "ID3D11DeviceContext::SetMarkerInt",
    "ID3D11DeviceContext::BeginEventInt",
    "ID3D11DeviceContext::EndEvent",
}


def xml_direct_resource_value(elem, name=None, typename=None):
    for r in elem:
        if r.tag != "ResourceId":
            continue
        if name is not None and r.attrib.get("name") != name:
            continue
        if typename is not None and r.attrib.get("typename") != typename:
            continue
        return (r.text or "").strip()
    return ""


def xml_command_buffer_value(chunk):
    for r in chunk.iter("ResourceId"):
        if r.attrib.get("typename") != "VkCommandBuffer":
            continue
        if r.attrib.get("name") in ("CommandBuffer", "commandBuffer") or not r.attrib.get("name"):
            return (r.text or "").strip()
    return ""


def vulkan_chunk_generates_action_event(name):
    if not name.startswith("vkCmd"):
        return False
    if name.startswith(VULKAN_DRAW_PREFIXES):
        return True
    return any(keyword in name for keyword in VULKAN_EVENT_KEYWORDS)


def vulkan_chunk_increments_command_buffer_eid(name):
    # RenderDoc's Vulkan ReplayLog increments BakedCmdBufferInfo.curEventID for command-buffer
    # chunks after processing each vkCmd* chunk, except begin/end command buffer and annotations.
    return name.startswith("vkCmd")


def submitted_command_buffers(chunk):
    buffers = []
    for arr in chunk.iter("array"):
        if arr.attrib.get("name") != "pCommandBuffers":
            continue
        for r in arr.iter("ResourceId"):
            if r.attrib.get("typename") == "VkCommandBuffer":
                value = (r.text or "").strip()
                if value:
                    buffers.append(value)
    if buffers:
        return buffers

    # vkQueueSubmit2 serialises command buffers inside VkCommandBufferSubmitInfo structs.
    for struct in chunk.iter("struct"):
        if struct.attrib.get("typename") not in ("VkCommandBufferSubmitInfo", "VkCommandBufferSubmitInfoKHR"):
            continue
        for r in struct.iter("ResourceId"):
            if r.attrib.get("typename") == "VkCommandBuffer":
                value = (r.text or "").strip()
                if value:
                    buffers.append(value)
    return buffers


def build_vulkan_event_id_map(root):
    """Recreate RenderDoc's submit-time EID remap for normal Vulkan command buffers.

    RenderDoc records vkCmd* calls under command buffers with local event IDs, then when a
    vkQueueSubmit is replayed it inserts a queue event, a virtual begin-command-buffer event,
    the command buffer's local events with an offset, and a virtual end-command-buffer event.
    The XML export contains chunkIndex and commandBuffer IDs, so we can reconstruct that mapping
    well enough for draw-call reports without needing GPU replay.
    """

    local_chunks = defaultdict(list)
    begin_chunks = {}
    end_chunks = {}

    for chunk in root.findall("./chunks/chunk"):
        name = chunk.attrib.get("name", "")
        chunk_index = int(chunk.attrib.get("chunkIndex", "-1"))

        if name == "vkBeginCommandBuffer":
            command_buffer = xml_direct_resource_value(chunk, "CommandBuffer", "VkCommandBuffer")
            if command_buffer:
                begin_chunks[command_buffer] = chunk_index
            continue

        if name == "vkEndCommandBuffer":
            command_buffer = xml_direct_resource_value(chunk, "CommandBuffer", "VkCommandBuffer")
            if command_buffer:
                end_chunks[command_buffer] = chunk_index
            continue

        command_buffer = xml_command_buffer_value(chunk)
        if command_buffer and vulkan_chunk_increments_command_buffer_eid(name):
            local_chunks[command_buffer].append(chunk_index)

    local_maps = {}
    local_counts = {}
    for command_buffer, chunks in local_chunks.items():
        local_maps[command_buffer] = {chunk_index: local_eid for local_eid, chunk_index in enumerate(chunks)}
        local_counts[command_buffer] = len(chunks)

    event_map = {}
    root_eid = 1
    submitted = 0

    for chunk in root.findall("./chunks/chunk"):
        name = chunk.attrib.get("name", "")
        if name not in ("vkQueueSubmit", "vkQueueSubmit2", "vkQueueSubmit2KHR"):
            continue

        queue_chunk = int(chunk.attrib.get("chunkIndex", "-1"))
        event_map[queue_chunk] = root_eid
        root_eid += 1

        buffers = submitted_command_buffers(chunk)
        if not buffers:
            # RenderDoc creates a virtual "No Command Buffers" action.
            root_eid += 1
            continue

        for command_buffer in buffers:
            if command_buffer in begin_chunks:
                event_map[begin_chunks[command_buffer]] = root_eid
            root_eid += 1

            base_eid = root_eid
            for chunk_index, local_eid in local_maps.get(command_buffer, {}).items():
                event_map[chunk_index] = base_eid + local_eid
            root_eid += local_counts.get(command_buffer, 0)

            if command_buffer in end_chunks:
                event_map[end_chunks[command_buffer]] = root_eid
            root_eid += 1
            submitted += 1

    return event_map, {
        "source": "renderdoc_offline_vulkan_submit_map",
        "mapped_chunks": len(event_map),
        "submitted_command_buffers": submitted,
        "command_buffers_with_events": len(local_maps),
        "max_event_id": max(event_map.values()) if event_map else 0,
    }


def d3d11_chunk_generates_event(name):
    # D3D11 replay increments m_CurEventID per replay chunk, while annotation chunks do not create
    # events. XML contains the frame chunks in order, so this gives a stable UI-like EID fallback.
    return name.startswith("ID3D11") and name not in D3D11_NO_EVENT_NAMES


def build_d3d11_event_id_map(root):
    event_map = {}
    event_id = 1
    for chunk in root.findall("./chunks/chunk"):
        name = chunk.attrib.get("name", "")
        chunk_index = int(chunk.attrib.get("chunkIndex", "-1"))
        if d3d11_chunk_generates_event(name):
            event_map[chunk_index] = event_id
        if name not in D3D11_NO_EVENT_NAMES and name.startswith(("ID3D11", "ID3DUserDefinedAnnotation")):
            event_id += 1
    return event_map, {
        "source": "renderdoc_offline_d3d11_chunk_order",
        "mapped_chunks": len(event_map),
        "max_event_id": max(event_map.values()) if event_map else 0,
    }


def build_event_id_map(xml_path, driver=None):
    root = ET.parse(xml_path).getroot()
    driver = driver if driver is not None else detect_driver(xml_path)
    if driver == "Vulkan":
        return build_vulkan_event_id_map(root)
    if driver == "D3D11":
        return build_d3d11_event_id_map(root)
    return {}, {"source": "unsupported_driver", "mapped_chunks": 0, "driver": driver}


def apply_event_id_map(data, event_map, metadata):
    mapped = 0
    for row in data.get("draws", []):
        chunk_index = row.get("chunk_index")
        try:
            key = int(chunk_index)
        except (TypeError, ValueError):
            continue
        event_id = event_map.get(key)
        if event_id is None:
            continue
        row["xml_chunk_index"] = chunk_index
        row["event_id"] = event_id
        mapped += 1
    data["event_id_map"] = metadata
    data["event_id_mapped_draws"] = mapped
    return mapped


def run_probe(xml_path):
    driver = detect_driver(xml_path)
    if driver == "D3D11":
        script = Path(__file__).with_name("d3d11_texture_xml_probe.py")
    else:
        script = Path(__file__).with_name("mobile_texture_xml_probe.py")
    print(f"      Driver: {driver or 'unknown'}; parser: {script.name}", flush=True)
    subprocess.run([sys.executable, str(script), str(xml_path)], check=True)
    json_path = xml_path.with_name(xml_path.stem + "_texture_probe.json")
    md_path = xml_path.with_name(xml_path.stem + "_texture_probe.md")
    return json_path, md_path


def csv_text(value):
    if isinstance(value, list):
        return "\n".join(str(v) for v in value)
    return "" if value is None else str(value)


def texture_category(row):
    names = [
        row.get("mesh_name"),
        row.get("index_texture"),
        *(row.get("textures") or []),
    ]
    text = " ".join(str(name or "") for name in names).lower()
    mesh_text = str(row.get("mesh_name") or "").lower()
    if "hlod" in text:
        if "brg" in mesh_text and "hlod" in mesh_text:
            return "PCG HLOD"
        return "HLOD"
    return row.get("category_by_d_texture") or "unclassified"


def is_d_texture_name(name):
    stem = Path(str(name or "").replace("\\", "/")).name.rsplit(".", 1)[0].lower()
    return stem.endswith("_d") and not stem.startswith("lightmap")


def find_precomputed_rows(source_path, out_dir):
    candidates = [
        source_path.with_name(source_path.stem + "_rows.json"),
        source_path.parent / "renderdoc_mcp_work" / f"{source_path.stem}_rows.json",
        out_dir / f"{source_path.stem}_rows.json",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def data_from_precomputed_rows(rows_path):
    source_rows = json.loads(rows_path.read_text(encoding="utf-8"))
    rows = []
    for i, row in enumerate(source_rows, 1):
        primary = row.get("primary_texture") or ""
        textures = [primary] if primary else []
        d_textures = [primary] if is_d_texture_name(primary) else []
        marker = row.get("marker_path") or row.get("pass") or ""
        rows.append(
            {
                "draw_index": i,
                "chunk_index": row.get("event_id"),
                "command": row.get("draw_name") or "",
                "renderpass": marker,
                "index_count": row.get("num_indices") or 0,
                "vertex_count": row.get("mesh_vertex_count") or 0,
                "instance_count": row.get("num_instances") or 1,
                "descriptor_sets": row.get("vertex_buffer_names") or [],
                "texture_count": len(textures),
                "index_texture": primary or row.get("mesh_name") or "-",
                "index_is_d_texture": bool(d_textures),
                "category_by_d_texture": row.get("category") or "unclassified",
                "d_textures": d_textures,
                "textures": textures,
                "mesh_name": row.get("mesh_name") or "",
                "pass": row.get("pass") or "",
            }
        )
    return {
        "draws": rows,
        "dispatches": [],
        "texture_usage": Counter(r["index_texture"] for r in rows if r.get("index_texture")),
        "command_counts": Counter(r.get("command") or "<unknown>" for r in rows),
    }


def build_category_groups(data):
    by_category = defaultdict(list)
    for row in data.get("draws", []):
        by_category[texture_category(row)].append(row)
    return by_category


def write_csvs(data, stem, out_dir):
    rows = data.get("draws", [])
    category_rows = []
    by_category = build_category_groups(data)

    for category, group in sorted(by_category.items(), key=category_sort_key):
        index_counter = Counter(r.get("index_texture") or "-" for r in group)
        rp_counter = Counter(r.get("renderpass") for r in group)
        category_rows.append(
            {
                "category_second_field": category,
                "draw_calls": len(group),
                "total_vertices": total_vertex_count(group),
                "d_indexed_draws": sum(1 for r in group if r.get("index_is_d_texture")),
                "textured_draws": sum(1 for r in group if r.get("texture_count")),
                "renderpasses": "; ".join(f"{k}:{v}" for k, v in rp_counter.most_common()),
                "top_index_textures": "; ".join(f"{k} ({v})" for k, v in index_counter.most_common(8)),
            }
        )

    summary_csv = out_dir / f"{stem}_texture_category_summary.csv"
    detail_csv = out_dir / f"{stem}_draw_details_by_texture_category.csv"

    with summary_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "category_second_field",
                "draw_calls",
                "total_vertices",
                "d_indexed_draws",
                "textured_draws",
                "renderpasses",
                "top_index_textures",
            ],
        )
        writer.writeheader()
        writer.writerows(category_rows)

    with detail_csv.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "category_second_field",
                "draw_index",
                "event_id",
                "chunk_index",
                "renderpass",
                "command",
                "vertices",
                "index_count",
                "vertex_count",
                "instance_count",
                "texture_count",
                "index_texture",
                "index_is_d_texture",
                "d_textures",
                "textures",
                "descriptor_sets",
            ],
        )
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "category_second_field": texture_category(r),
                    "draw_index": r.get("draw_index"),
                    "event_id": r.get("event_id"),
                    "chunk_index": r.get("chunk_index"),
                    "renderpass": r.get("renderpass"),
                    "command": r.get("command"),
                    "vertices": draw_vertex_count(r),
                    "index_count": r.get("index_count"),
                    "vertex_count": r.get("vertex_count"),
                    "instance_count": r.get("instance_count"),
                    "texture_count": r.get("texture_count"),
                    "index_texture": r.get("index_texture"),
                    "index_is_d_texture": r.get("index_is_d_texture"),
                    "d_textures": csv_text(r.get("d_textures") or []),
                    "textures": csv_text(r.get("textures") or []),
                    "descriptor_sets": csv_text(r.get("descriptor_sets") or []),
                }
            )

    return summary_csv, detail_csv, by_category


def md_texture_list(textures, limit=16):
    if not textures:
        return "-"
    shown = textures[:limit]
    suffix = "" if len(textures) <= limit else f"<br>... +{len(textures) - limit}"
    return "<br>".join(f"`{t}`" for t in shown) + suffix


def html_texture_list(textures, limit=18):
    if not textures:
        return '<span class="muted">-</span>'
    shown = textures[:limit]
    escaped = [f"<code>{html.escape(str(t))}</code>" for t in shown]
    if len(textures) > limit:
        escaped.append(f'<span class="muted">... +{len(textures) - limit}</span>')
    return "<br>".join(escaped)


def html_draw_detail_row(row):
    vertices = draw_vertex_count(row)
    idx_or_vert = row.get("index_count") or row.get("vertex_count") or 0
    mesh_name = row.get("mesh_name") or "-"
    return (
        "<tr>"
        f"<td><code>{html.escape(str(row.get('index_texture') or '-'))}</code></td>"
        f"<td><code>{html.escape(str(mesh_name))}</code></td>"
        f"<td class='num'>{vertices:,}</td>"
        f"<td class='num'>{row.get('draw_index')}</td>"
        f"<td>{html_texture_list(row.get('textures') or [])}</td>"
        f"<td class='num'>{row.get('texture_count')}</td>"
        f"<td class='num'>{html.escape(eid_value(row))}</td>"
        f"<td>{html.escape(str(row.get('renderpass') or ''))}</td>"
        f"<td><code>{html.escape(str(row.get('command') or ''))}</code></td>"
        f"<td class='num'>{idx_or_vert}</td>"
        f"<td class='num'>{row.get('instance_count')}</td>"
        "</tr>"
    )


def html_draw_detail_header(eid_label="EID/chunkIndex"):
    return f"""
                <thead>
                  <tr>
                    <th>Index texture</th><th>Mesh</th><th>Vertices</th><th>Draw #</th>
                    <th>Textures</th><th>Texture count</th><th>{html.escape(eid_label)}</th>
                    <th>RenderPass/Marker</th><th>Cmd</th><th>idx/verts</th><th>inst</th>
                  </tr>
                </thead>
    """


def html_texture_aggregate_row(texture, group, eid_label="EID/chunkIndex"):
    renderpasses = Counter(renderpass_label(r) for r in group).most_common(5)
    meshes = Counter(r.get("mesh_name") or "-" for r in group).most_common(8)
    texture_counts = Counter(r.get("texture_count") or 0 for r in group).most_common(3)
    return (
        "<tr>"
        f"<td><code>{html.escape(str(texture or '-'))}</code></td>"
        f"<td class='num' data-value='{len(group)}'>{len(group)}</td>"
        f"<td class='num' data-value='{total_vertex_count(group)}'>{total_vertex_count(group):,}</td>"
        f"<td>{html_eids(group, eid_label)}</td>"
        f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in meshes))}</td>"
        f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
        f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in texture_counts))}</td>"
        "</tr>"
    )


def html_texture_aggregate_header(eid_label="EID/chunkIndex"):
    return f"""
                <thead>
                  <tr>
                    <th data-sort="text">Index texture</th>
                    <th data-sort="number">Draws</th>
                    <th data-sort="number">Total vertices</th>
                    <th>{html.escape(eid_label)}</th>
                    <th data-sort="text">Meshes</th>
                    <th data-sort="text">Top renderpasses</th>
                    <th data-sort="text">Texture count</th>
                  </tr>
                </thead>
    """


def write_html_report(stem, out_dir, source_path, data, by_category):
    html_path = out_dir / f"{stem}_analysis.html"
    rows = data.get("draws", [])
    enhanced_rows = data.get("enhanced_draws", [])
    dispatches = data.get("dispatches", [])
    command_counts = Counter(data.get("command_counts", {}))
    ordered = sorted(by_category.items(), key=category_sort_key)
    textured = sum(1 for r in rows if r.get("texture_count"))
    d_indexed = sum(1 for r in rows if r.get("index_is_d_texture"))
    category_total = sum(len(group) for _, group in ordered)
    enhanced_merged = data.get("enhanced_merged_draws") or sum(1 for r in rows if r.get("enhanced_match"))
    event_meta = data.get("event_id_map") or {}
    event_mapped = data.get("event_id_mapped_draws") or 0
    eid_label = "EID" if event_mapped else "chunkIndex"

    summary_rows = []
    for category, group in ordered:
        top_idx = Counter(r.get("index_texture") or "-" for r in group).most_common(5)
        renderpasses = Counter(r.get("renderpass") for r in group).most_common(5)
        summary_rows.append(
            "<tr>"
            f"<td><a href='#{html.escape(category)}'>{html.escape(category)}</a></td>"
            f"<td class='num'>{len(group)}</td>"
            f"<td class='num'>{total_vertex_count(group):,}</td>"
            f"<td class='num'>{sum(1 for r in group if r.get('texture_count'))}</td>"
            f"<td class='num'>{sum(1 for r in group if r.get('index_is_d_texture'))}</td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in top_idx))}</td>"
            f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
            "</tr>"
        )

    def make_category_detail_blocks(source_rows, eid_label=eid_label, id_prefix=""):
        grouped = defaultdict(list)
        for row in source_rows:
            grouped[texture_category(row)].append(row)
        blocks = []
        for category, group in sorted(grouped.items(), key=category_sort_key):
            top_tex = Counter()
            for r in group:
                for tex in r.get("textures") or []:
                    top_tex[tex] += 1
            top_tex_html = " ".join(
                f"<span class='pill'>{html.escape(str(t))} <b>{c}</b></span>"
                for t, c in top_tex.most_common(12)
            )
            detail_rows = []
            for texture, texture_group in aggregate_by_index_texture(group):
                detail_rows.append(html_texture_aggregate_row(texture, texture_group, eid_label))
            blocks.append(
                f"""
                <details id="{html.escape(id_prefix + category)}" class="category">
                  <summary>
                    <span class="cat">{html.escape(category)}</span>
                    <span>{len(group)} draw calls</span>
                    <span>{total_vertex_count(group):,} vertices</span>
                    <span>{sum(1 for r in group if r.get('texture_count'))} textured</span>
                    <span>{sum(1 for r in group if r.get('index_is_d_texture'))} _D indexed</span>
                  </summary>
                  <div class="toptex">{top_tex_html or '<span class="muted">No textures</span>'}</div>
                  <table class="sortable">
                    {html_texture_aggregate_header(eid_label)}
                    <tbody>{''.join(detail_rows)}</tbody>
                  </table>
                </details>
                """
            )
        return blocks

    category_detail_rows = rows
    category_detail_label = eid_label
    detail_blocks = make_category_detail_blocks(category_detail_rows, category_detail_label)

    pass_blocks = []
    for pass_name, pass_group in build_renderpass_groups(rows):
        pass_by_category = defaultdict(list)
        for r in pass_group:
            pass_by_category[texture_category(r)].append(r)
        pass_category_blocks = []
        for category, group in sorted(pass_by_category.items(), key=category_sort_key):
            marker_paths = Counter(renderpass_label(r) for r in group).most_common(3)
            texture_rows_for_category = []
            for texture, texture_group in build_index_texture_groups(group):
                texture_rows_for_category.append(
                    "<tr>"
                    f"<td><code>{html.escape(str(texture or '-'))}</code></td>"
                    f"<td class='num'>{len(texture_group)}</td>"
                    f"<td class='num'>{total_vertex_count(texture_group):,}</td>"
                    f"<td>{html_folded_values([eid_value(r) for r in sorted(texture_group, key=lambda r: r.get('draw_index') or 0)], eid_label)}</td>"
                    f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in Counter(renderpass_label(r) for r in texture_group).most_common(3)))}</td>"
                    "</tr>"
                )
            pass_category_blocks.append(
                f"""
                <details class="pass-subgroup">
                  <summary>
                    <span class="cat">{html.escape(category)}</span>
                    <span>{len(group)} draw calls</span>
                    <span>{total_vertex_count(group):,} vertices</span>
                    <span>{sum(1 for r in group if r.get('texture_count'))} textured</span>
                    <span>{sum(1 for r in group if r.get('index_is_d_texture'))} _D indexed</span>
                    <span>{len(texture_rows_for_category)} textures</span>
                  </summary>
                  <div class="marker-paths">{html.escape('; '.join(f'{k} ({v})' for k, v in marker_paths))}</div>
                  <table class="pass-texture-table">
                    <thead>
                      <tr>
                        <th>Index texture</th><th>Draws</th><th>Total vertices</th>
                        <th>{html.escape(eid_label)}</th><th>Marker paths</th>
                      </tr>
                    </thead>
                    <tbody>{''.join(texture_rows_for_category)}</tbody>
                  </table>
                </details>
                """
            )
        pass_blocks.append(
            f"""
            <details class="category pass-group">
              <summary>
                <span class="cat">{html.escape(pass_name)}</span>
                <span>{len(pass_group)} draw calls</span>
                <span>{total_vertex_count(pass_group):,} vertices</span>
                <span>{sum(1 for r in pass_group if r.get('texture_count'))} textured</span>
              </summary>
              <div class="pass-subgroups">{''.join(pass_category_blocks)}</div>
            </details>
            """
        )

    texture_rows = []
    for tex, group in build_index_texture_groups(rows):
        categories = Counter(texture_category(r) for r in group).most_common(5)
        renderpasses = Counter(renderpass_label(r) for r in group).most_common(5)
        meshes = Counter(r.get("mesh_name") or "-" for r in group).most_common(6)
        texture_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(tex))}</code></td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in categories))}</td>"
            f"<td class='num'>{len(group)}</td>"
            f"<td class='num'>{total_vertex_count(group):,}</td>"
            f"<td><code>{html.escape(format_eids(group))}</code></td>"
            f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in meshes))}</td>"
            "</tr>"
        )

    enhanced_category_rows = []
    if enhanced_rows:
        enhanced_by_category = defaultdict(list)
        for r in enhanced_rows:
            enhanced_by_category[texture_category(r)].append(r)
        for category, group in sorted(enhanced_by_category.items(), key=category_sort_key):
            top_idx = Counter(r.get("index_texture") or "-" for r in group).most_common(8)
            renderpasses = Counter(renderpass_label(r) for r in group).most_common(5)
            enhanced_category_rows.append(
                "<tr>"
                f"<td>{html.escape(category)}</td>"
                f"<td class='num'>{len(group)}</td>"
                f"<td class='num'>{total_vertex_count(group):,}</td>"
                f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in top_idx))}</td>"
                f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
                f"<td><code>{html.escape(format_eids(group))}</code></td>"
                "</tr>"
            )

    enhanced_texture_rows = []
    for tex, group in build_index_texture_groups(enhanced_rows):
        categories = Counter(texture_category(r) for r in group).most_common(5)
        renderpasses = Counter(renderpass_label(r) for r in group).most_common(5)
        meshes = Counter(r.get("mesh_name") or "-" for r in group).most_common(6)
        enhanced_texture_rows.append(
            "<tr>"
            f"<td><code>{html.escape(str(tex))}</code></td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in categories))}</td>"
            f"<td class='num'>{len(group)}</td>"
            f"<td class='num'>{total_vertex_count(group):,}</td>"
            f"<td><code>{html.escape(format_eids(group))}</code></td>"
            f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in meshes))}</td>"
            "</tr>"
        )

    enhanced_section = ""
    if enhanced_rows:
        enhanced_section = f"""
    <h2 class="section-title">Enhanced Pipeline Texture Rows</h2>
    <table>
      <thead>
        <tr>
          <th>Texture category</th><th>Draws</th><th>Total vertices</th>
          <th>Top index textures</th><th>Top renderpasses</th><th>EID</th>
        </tr>
      </thead>
      <tbody>{''.join(enhanced_category_rows)}</tbody>
    </table>

    <details class="section">
      <summary>Enhanced Index Texture Usage</summary>
      <table>
        <thead>
          <tr>
            <th>Index texture</th><th>Categories</th><th>Draws</th><th>Total vertices</th>
            <th>EID</th><th>Top renderpasses</th><th>Top meshes</th>
          </tr>
        </thead>
        <tbody>{''.join(enhanced_texture_rows)}</tbody>
      </table>
    </details>
"""

    cmd_rows = "".join(
        f"<tr><td><code>{html.escape(str(cmd))}</code></td><td class='num'>{cnt}</td></tr>"
        for cmd, cnt in command_counts.most_common(30)
    )

    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(stem)} RDC Analysis</title>
  <style>
    :root {{ color-scheme: light; --border:#d8dee8; --text:#17202a; --muted:#667085; --bg:#f6f8fb; --panel:#fff; --accent:#1155cc; }}
    body {{ margin:0; font:14px/1.45 "Segoe UI", Arial, sans-serif; color:var(--text); background:var(--bg); }}
    header {{ padding:20px 28px 14px; background:#101828; color:white; }}
    h1 {{ margin:0 0 8px; font-size:22px; font-weight:650; }}
    .meta {{ color:#d0d5dd; overflow-wrap:anywhere; }}
    main {{ padding:22px 28px 40px; }}
    .cards {{ display:grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap:12px; margin-bottom:18px; }}
    .card {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; padding:12px; }}
    .card b {{ display:block; font-size:22px; margin-top:4px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--border); margin:10px 0 18px; }}
    th, td {{ border-right:1px solid var(--border); border-bottom:1px solid var(--border); padding:7px 8px; vertical-align:top; text-align:left; }}
    th:last-child, td:last-child {{ border-right:0; }}
    th {{ position:sticky; top:0; background:#eef2f7; z-index:1; }}
    th[data-sort] {{ cursor:pointer; user-select:none; }}
    th[data-sort]::after {{ content:" ⇅"; color:var(--muted); font-weight:400; }}
    .num {{ text-align:right; white-space:nowrap; }}
    code {{ font-family:Consolas, "Cascadia Mono", monospace; font-size:12px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    details.eids > summary {{ cursor:pointer; color:#0b4aa2; }}
    details.eids div {{ margin-top:6px; max-width:820px; overflow-wrap:anywhere; }}
    details.category {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; margin:10px 0; overflow:hidden; }}
    details.category > summary {{ cursor:pointer; display:flex; gap:14px; align-items:center; padding:10px 12px; background:#eef2f7; font-weight:600; }}
    details.pass-group > summary {{ display:grid; grid-template-columns:minmax(320px, 430px) repeat(3, max-content); column-gap:28px; row-gap:6px; padding:14px 18px; }}
    details.pass-group > summary .cat {{ min-width:0; overflow-wrap:anywhere; }}
    .pass-subgroups {{ padding:10px 12px 14px; }}
    details.pass-subgroup {{ border:1px solid var(--border); border-radius:8px; margin:8px 0; overflow:hidden; background:#fff; }}
    details.pass-subgroup > summary {{ cursor:pointer; display:grid; grid-template-columns:minmax(150px, 260px) repeat(5, max-content); column-gap:18px; row-gap:6px; align-items:center; padding:9px 10px; background:#f8fafc; font-weight:600; }}
    details.pass-subgroup > summary .cat {{ min-width:0; overflow-wrap:anywhere; }}
    .marker-paths {{ padding:8px 10px 0; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }}
    .pass-texture-table {{ margin:8px 0 0; border-left:0; border-right:0; border-bottom:0; }}
    .pass-texture-table th:nth-child(1) {{ min-width:260px; }}
    .pass-texture-table th:nth-child(4) {{ min-width:120px; }}
    .pass-texture-table th:nth-child(5) {{ min-width:260px; }}
    details.section {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; margin:18px 0; overflow:hidden; }}
    details.section > summary {{ cursor:pointer; padding:10px 12px; background:#eef2f7; font-size:18px; font-weight:650; }}
    details.section > table {{ margin:0; border-left:0; border-right:0; border-bottom:0; }}
    summary .cat {{ min-width:210px; color:#0b4aa2; }}
    .fold-list > summary {{ cursor:pointer; color:#0b4aa2; white-space:nowrap; }}
    .fold-list > div {{ margin-top:6px; max-width:520px; overflow-wrap:anywhere; }}
    .toptex {{ padding:10px 12px 0; }}
    .pill {{ display:inline-block; margin:0 6px 6px 0; padding:3px 7px; border:1px solid var(--border); border-radius:999px; background:#fafafa; }}
    .muted {{ color:var(--muted); }}
    .section-title {{ margin:22px 0 8px; font-size:18px; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(stem)} RDC Analysis</h1>
    <div class="meta">Source: {html.escape(str(source_path))}</div>
  </header>
  <main>
    <div class="cards">
      <div class="card">Draw calls<b>{len(rows)}</b></div>
      <div class="card">Dispatches<b>{len(dispatches)}</b></div>
      <div class="card">Textured draws<b>{textured}</b></div>
      <div class="card">_D indexed draws<b>{d_indexed}</b></div>
      <div class="card">Categorized draws<b>{category_total}</b></div>
      <div class="card">Enhanced texture rows<b>{len(enhanced_rows)}</b></div>
      <div class="card">Merged enhanced draws<b>{enhanced_merged}</b></div>
      <div class="card">EID mapped draws<b>{event_mapped}</b><span class="muted">{html.escape(str(event_meta.get('source') or 'fallback'))}</span></div>
    </div>

    <h2 class="section-title">RenderPass/Marker Major Groups</h2>
    {''.join(pass_blocks)}

    <h2 class="section-title">Category Details</h2>
    {''.join(detail_blocks)}

    <details class="section">
      <summary>Index Texture Usage</summary>
      <table>
        <thead>
          <tr>
            <th>Index texture</th><th>Categories</th><th>Draws</th><th>Total vertices</th>
            <th>{html.escape(eid_label)}</th><th>Top renderpasses</th><th>Top meshes</th>
          </tr>
        </thead>
        <tbody>{''.join(texture_rows)}</tbody>
      </table>
    </details>

    {enhanced_section}

    <details class="section">
      <summary>Texture Category Summary</summary>
      <table>
        <thead><tr><th>Category(second field)</th><th>Draws</th><th>Total vertices</th><th>Textured</th><th>_D indexed</th><th>Top index textures</th><th>Top renderpasses</th></tr></thead>
        <tbody>{''.join(summary_rows)}</tbody>
      </table>
    </details>

    <h2 class="section-title">Command Top</h2>
    <table><thead><tr><th>Command</th><th>Count</th></tr></thead><tbody>{cmd_rows}</tbody></table>
  </main>
  <script>
    document.querySelectorAll('table.sortable th[data-sort]').forEach((th) => {{
      th.addEventListener('click', () => {{
        const table = th.closest('table');
        const tbody = table.querySelector('tbody');
        const index = Array.from(th.parentElement.children).indexOf(th);
        const type = th.dataset.sort;
        const current = th.dataset.dir === 'asc' ? 'desc' : 'asc';
        th.parentElement.querySelectorAll('th[data-sort]').forEach((item) => delete item.dataset.dir);
        th.dataset.dir = current;
        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => {{
          const ac = a.children[index];
          const bc = b.children[index];
          const av = ac.dataset.value || ac.textContent.trim();
          const bv = bc.dataset.value || bc.textContent.trim();
          let result;
          if (type === 'number') {{
            result = Number(av.replace(/,/g, '')) - Number(bv.replace(/,/g, ''));
          }} else {{
            result = av.localeCompare(bv, undefined, {{ numeric: true, sensitivity: 'base' }});
          }}
          return current === 'asc' ? result : -result;
        }});
        rows.forEach((row) => tbody.appendChild(row));
      }});
    }});
  </script>
</body>
</html>"""
    html_path.write_text(doc, encoding="utf-8")
    return html_path


def write_category_detail_md(stem, out_dir, by_category):
    md_path = out_dir / f"{stem}_texture_category_details.md"
    lines = [
        f"# {stem} texture category details",
        "",
        "## Category Summary",
        "| Category(second field) | DrawCalls | Total vertices | Textured | `_D` indexed | Top index textures |",
        "|---|---:|---:|---:|---:|---|",
    ]
    ordered = sorted(by_category.items(), key=category_sort_key)
    for category, group in ordered:
        top_idx = Counter(r.get("index_texture") or "-" for r in group).most_common(5)
        lines.append(
            f"| `{category}` | {len(group)} | {total_vertex_count(group)} | {sum(1 for r in group if r.get('texture_count'))} | "
            f"{sum(1 for r in group if r.get('index_is_d_texture'))} | "
            f"{'; '.join(f'`{k}` ({v})' for k, v in top_idx)} |"
        )

    for category, group in ordered:
        lines += [
            "",
            f"## {category}",
            "",
            f"- DrawCall: {len(group)}",
            f"- Total vertices: {total_vertex_count(group)}",
            f"- Textured: {sum(1 for r in group if r.get('texture_count'))}",
            f"- `_D` indexed: {sum(1 for r in group if r.get('index_is_d_texture'))}",
            "",
            "| Index texture | Mesh | Vertices | Draw # | Textures | Texture count | EID | chunkIndex | RenderPass | Cmd | idx/verts | inst |",
            "|---|---|---:|---:|---|---:|---:|---:|---|---|---:|---:|",
        ]
        for r in group:
            idx_or_vert = r.get("index_count") or r.get("vertex_count") or 0
            lines.append(
                f"| `{r.get('index_texture')}` | `{r.get('mesh_name')}` | {draw_vertex_count(r)} | {r.get('draw_index')} | "
                f"{md_texture_list(r.get('textures') or [])} | {r.get('texture_count')} | "
                f"{r.get('event_id') or '-'} | {r.get('chunk_index')} | {r.get('renderpass')} | `{r.get('command')}` | "
                f"{idx_or_vert} | {r.get('instance_count')} |"
            )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def write_index_md(stem, out_dir, source_path, xml_path, probe_md, probe_json, summary_csv, detail_csv, detail_md, html_report):
    index_md = out_dir / f"{stem}_analysis_outputs.md"
    lines = [
        f"# {stem} mobile RDC analysis outputs",
        "",
        f"- Source: `{source_path}`",
        f"- HTML report: `{html_report}`",
        f"- XML: `{xml_path}`",
        f"- Main MD report: `{probe_md}`",
        f"- Raw JSON: `{probe_json}`",
        f"- Category summary CSV: `{summary_csv}`",
        f"- Draw detail CSV: `{detail_csv}`",
        f"- Category detail MD: `{detail_md}`",
        "",
        "CSV files use UTF-8 BOM so they can be opened directly in Excel.",
    ]
    index_md.write_text("\n".join(lines), encoding="utf-8")
    return index_md


def main():
    parser = argparse.ArgumentParser(description="Offline mobile Vulkan RDC draw/texture analyzer.")
    parser.add_argument("capture", nargs="?", help="Path to .rdc or already exported .xml")
    parser.add_argument("--renderdoccmd", help="Path to renderdoccmd.exe")
    parser.add_argument("--force-convert", action="store_true", help="Regenerate XML even if it exists")
    parser.add_argument("--keep-intermediate", action="store_true", help="Keep XML/JSON/CSV/MD helper files")
    args = parser.parse_args()

    raw = args.capture or input("Drag/paste .rdc path: ")
    source_path = clean_input_path(raw).resolve()
    if not source_path.exists():
        raise SystemExit(f"File not found: {source_path}")

    repo_root = Path(__file__).resolve().parent.parent
    out_dir = repo_root / "analysis_results" / source_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    if source_path.suffix.lower() == ".rdc":
        xml_path = out_dir / f"{source_path.stem}.xml"
        renderdoccmd = find_renderdoccmd(args.renderdoccmd)
        print(f"[1/4] Convert RDC to XML: {xml_path}", flush=True)
        run_convert(renderdoccmd, source_path, xml_path, args.force_convert)
    elif source_path.suffix.lower() == ".xml":
        xml_path = source_path
    else:
        raise SystemExit("Input must be .rdc or .xml")

    driver = detect_driver(xml_path)
    rows_path = find_precomputed_rows(source_path, out_dir)
    if driver == "D3D11" and rows_path is not None:
        print("[2/4] Parse full D3D11 draw calls from XML", flush=True)
        probe_json, probe_md = run_probe(xml_path)
        data = json.loads(probe_json.read_text(encoding="utf-8"))
        print(f"      Load enhanced pipeline rows: {rows_path}", flush=True)
        data["enhanced_draws"] = data_from_precomputed_rows(rows_path)["draws"]
        merged = merge_enhanced_rows(data, data["enhanced_draws"])
        data["enhanced_merged_draws"] = merged
        print(f"      Merged enhanced rows into full XML draws: {merged}/{len(data['enhanced_draws'])}", flush=True)
        data["enhanced_source"] = str(rows_path)
    else:
        print("[2/4] Parse draw calls and texture bindings", flush=True)
        probe_json, probe_md = run_probe(xml_path)
        data = json.loads(probe_json.read_text(encoding="utf-8"))

    print("      Build offline EID map", flush=True)
    event_map, event_meta = build_event_id_map(xml_path, driver)
    mapped_draws = apply_event_id_map(data, event_map, event_meta)
    print(
        f"      EID map: {event_meta.get('source')} mapped {mapped_draws}/{len(data.get('draws', []))} draws; "
        f"max EID {event_meta.get('max_event_id', 0)}",
        flush=True,
    )

    print("[3/4] Write HTML report", flush=True)
    by_category = build_category_groups(data)
    html_report = write_html_report(source_path.stem, out_dir, source_path, data, by_category)

    print("[4/4] Cleanup", flush=True)
    if args.keep_intermediate:
        summary_csv, detail_csv, by_category = write_csvs(data, source_path.stem, out_dir)
        detail_md = write_category_detail_md(source_path.stem, out_dir, by_category)
        write_index_md(source_path.stem, out_dir, source_path, xml_path, probe_md, probe_json, summary_csv, detail_csv, detail_md, html_report)
    else:
        for path in (probe_md, probe_json):
            if path is None:
                continue
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
        if source_path.suffix.lower() == ".rdc":
            try:
                xml_path.unlink()
            except FileNotFoundError:
                pass

    print("")
    print("Done.")
    print(f"HTML report: {html_report}")
    print(f"Output folder: {out_dir}")


if __name__ == "__main__":
    main()
