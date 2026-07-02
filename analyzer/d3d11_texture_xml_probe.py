import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


DRAW_NAMES = {
    "ID3D11DeviceContext::Draw",
    "ID3D11DeviceContext::DrawIndexed",
    "ID3D11DeviceContext::DrawInstanced",
    "ID3D11DeviceContext::DrawIndexedInstanced",
    "ID3D11DeviceContext::DrawAuto",
}
DISPATCH_NAMES = {"ID3D11DeviceContext::Dispatch", "ID3D11DeviceContext::DispatchIndirect"}
SRV_SET_NAMES = {
    "ID3D11DeviceContext::VSSetShaderResources": "VS",
    "ID3D11DeviceContext::PSSetShaderResources": "PS",
    "ID3D11DeviceContext::GSSetShaderResources": "GS",
    "ID3D11DeviceContext::HSSetShaderResources": "HS",
    "ID3D11DeviceContext::DSSetShaderResources": "DS",
    "ID3D11DeviceContext::CSSetShaderResources": "CS",
}


def text_of(elem, default=""):
    return elem.text.strip() if elem is not None and elem.text else default


def child_text(elem, tag, name=None):
    for c in elem.iter(tag):
        if name is None or c.attrib.get("name") == name:
            return text_of(c)
    return ""


def direct_resource_value(elem, name=None):
    for r in elem:
        if r.tag != "ResourceId":
            continue
        if name is not None and r.attrib.get("name") != name:
            continue
        return text_of(r)
    return ""


def uint_value(elem, name, default=0):
    txt = child_text(elem, "uint", name)
    try:
        return int(txt)
    except (TypeError, ValueError):
        return default


def clean_texture_name(name):
    name = str(name or "")
    if ":" in name:
        name = name.split(":", 1)[1]
    for suffix in ("_mainview", "_view0", "_view1"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return Path(name.replace("\\", "/")).name.rsplit(".", 1)[0]


def is_d_texture(name):
    stem = clean_texture_name(name).lower()
    return stem.endswith("_d") and not stem.startswith("lightmap")


def classify_from_texture(name):
    stem = clean_texture_name(name)
    parts = stem.split("_")
    if len(parts) >= 2:
        return parts[1].lower()
    return ""


def choose_index_texture(textures):
    d_textures = [t for t in textures if is_d_texture(t)]
    if d_textures:
        return sorted(d_textures)[0], True
    non_lightmap = [t for t in textures if not clean_texture_name(t).lower().startswith("lightmap")]
    if non_lightmap:
        return sorted(non_lightmap)[0], False
    return (sorted(textures)[0], False) if textures else ("-", False)


def parse_srv_array(chunk):
    for arr in chunk.iter("array"):
        if arr.attrib.get("name") != "ppShaderResourceViews":
            continue
        return [text_of(r) for r in arr.iter("ResourceId")]
    return []


def write_outputs(xml_path, rows, dispatch_rows, command_counts, resource_names, srv_to_resource):
    out_json = xml_path.with_name(xml_path.stem + "_texture_probe.json")
    out_md = xml_path.with_name(xml_path.stem + "_texture_probe.md")

    texture_usage = Counter()
    category_rollup = defaultdict(lambda: Counter(draws=0, d=0, textured=0))
    d_counter = Counter()
    for row in rows:
        cat = row.get("category_by_d_texture") or "unclassified"
        category_rollup[cat]["draws"] += 1
        if row.get("index_is_d_texture"):
            category_rollup[cat]["d"] += 1
        if row.get("texture_count"):
            category_rollup[cat]["textured"] += 1
        for tex in row.get("textures") or []:
            texture_usage[tex] += 1
        for tex in row.get("d_textures") or []:
            d_counter[tex] += 1

    out_json.write_text(
        json.dumps(
            {
                "draws": rows,
                "dispatches": dispatch_rows,
                "texture_usage": texture_usage,
                "command_counts": command_counts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    lines = [
        "# D3D11 RDC offline draw/texture analysis",
        "",
        f"- XML: `{xml_path}`",
        f"- Draws: {len(rows)}",
        f"- Dispatches: {len(dispatch_rows)}",
        f"- Draws with any resolved texture: {sum(1 for r in rows if r.get('texture_count'))}",
        f"- Draws with `_D` texture: {sum(1 for r in rows if r.get('d_textures'))}",
        f"- Named resources: {len(resource_names)}",
        f"- Shader resource views: {len(srv_to_resource)}",
        "",
        "## Texture category draw-call order",
        "| Texture category(second field) | Draws | `_D` indexed draws | Textured draws |",
        "|---|---:|---:|---:|",
    ]
    for cat, cnt in sorted(category_rollup.items(), key=lambda kv: kv[1]["draws"], reverse=True):
        lines.append(f"| `{cat}` | {cnt['draws']} | {cnt['d']} | {cnt['textured']} |")

    lines += ["", "## Command Top", "| Command | Count |", "|---|---:|"]
    for cmd, cnt in command_counts.most_common(50):
        lines.append(f"| `{cmd}` | {cnt} |")

    lines += ["", "## `_D` texture draw usage top", "| Texture | Draws |", "|---|---:|"]
    for tex, cnt in d_counter.most_common(80):
        lines.append(f"| `{tex}` | {cnt} |")

    lines += ["", "## Any texture draw usage top", "| Texture | Draws |", "|---|---:|"]
    for tex, cnt in texture_usage.most_common(80):
        lines.append(f"| `{tex}` | {cnt} |")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(out_md)
    print(out_json)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python d3d11_texture_xml_probe.py capture.xml")
    xml_path = Path(sys.argv[1])
    root = ET.parse(xml_path).getroot()

    resource_names = {}
    srv_to_resource = {}
    current_srvs = defaultdict(dict)
    marker_stack = []
    command_counts = Counter()
    rows = []
    dispatch_rows = []
    draw_index = 0
    dispatch_index = 0

    for chunk in root.findall("./chunks/chunk"):
        name = chunk.attrib.get("name", "")
        chunk_index = int(chunk.attrib.get("chunkIndex", "-1"))
        if name.startswith("ID3D"):
            command_counts[name] += 1

        if name == "ID3D11Resource::SetDebugName":
            rid = direct_resource_value(chunk, "pResource")
            label = child_text(chunk, "string", "Name")
            if rid and label:
                resource_names[rid] = label

        elif name == "ID3D11Device::CreateShaderResourceView":
            resource = direct_resource_value(chunk, "pResource")
            view = direct_resource_value(chunk, "pView")
            if view:
                srv_to_resource[view] = resource

        elif name in SRV_SET_NAMES:
            stage = SRV_SET_NAMES[name]
            start_slot = uint_value(chunk, "StartSlot")
            views = parse_srv_array(chunk)
            for i, view in enumerate(views):
                current_srvs[stage][start_slot + i] = view

        elif name == "ID3DUserDefinedAnnotation::BeginEvent":
            label = child_text(chunk, "string", "Name") or child_text(chunk, "string")
            marker_stack.append(label or f"chunk_{chunk_index}")

        elif name == "ID3DUserDefinedAnnotation::EndEvent":
            if marker_stack:
                marker_stack.pop()

        elif name in DRAW_NAMES:
            draw_index += 1
            textures = []
            bound_views = []
            for stage in sorted(current_srvs):
                for slot in sorted(current_srvs[stage]):
                    view = current_srvs[stage][slot]
                    if not view or view == "0":
                        continue
                    bound_views.append(f"{stage}{slot}:{view}")
                    resource = srv_to_resource.get(view, "")
                    tex_name = resource_names.get(view) or resource_names.get(resource) or resource or view
                    if tex_name:
                        textures.append(tex_name)
            tex_unique = sorted(set(textures))
            d_textures = [t for t in tex_unique if is_d_texture(t)]
            index_texture, index_is_d = choose_index_texture(tex_unique)
            category = classify_from_texture(index_texture)
            rows.append(
                {
                    "draw_index": draw_index,
                    "chunk_index": chunk_index,
                    "command": name,
                    "renderpass": "/".join(marker_stack),
                    "index_count": uint_value(chunk, "IndexCount") or uint_value(chunk, "indexCount"),
                    "vertex_count": uint_value(chunk, "VertexCount") or uint_value(chunk, "vertexCount"),
                    "instance_count": uint_value(chunk, "InstanceCount", 1) or uint_value(chunk, "instanceCount", 1),
                    "descriptor_sets": bound_views,
                    "texture_count": len(tex_unique),
                    "index_texture": index_texture,
                    "index_is_d_texture": index_is_d,
                    "category_by_d_texture": category,
                    "d_textures": d_textures,
                    "textures": tex_unique,
                }
            )

        elif name in DISPATCH_NAMES:
            dispatch_index += 1
            dispatch_rows.append(
                {
                    "dispatch_index": dispatch_index,
                    "chunk_index": chunk_index,
                    "command": name,
                    "x": uint_value(chunk, "ThreadGroupCountX"),
                    "y": uint_value(chunk, "ThreadGroupCountY"),
                    "z": uint_value(chunk, "ThreadGroupCountZ"),
                    "renderpass": "/".join(marker_stack),
                }
            )

    write_outputs(xml_path, rows, dispatch_rows, command_counts, resource_names, srv_to_resource)


if __name__ == "__main__":
    main()
