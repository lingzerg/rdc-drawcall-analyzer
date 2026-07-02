import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path


DRAW_NAMES = {"vkCmdDraw", "vkCmdDrawIndexed", "vkCmdDrawIndirect", "vkCmdDrawIndexedIndirect"}
DISPATCH_NAMES = {"vkCmdDispatch", "vkCmdDispatchIndirect"}
IMAGE_DESCRIPTOR_TYPES = {
    "VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER",
    "VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE",
    "VK_DESCRIPTOR_TYPE_STORAGE_IMAGE",
    "VK_DESCRIPTOR_TYPE_INPUT_ATTACHMENT",
    # RenderDoc's internal DescriptorSlotType enum uses these names in some versions.
    "CombinedImageSampler",
    "SampledImage",
    "StorageImage",
    "InputAttachment",
}
IMAGE_DESCRIPTOR_NUMBERS = {"1", "2", "3", "4", "10", "11"}


def text_of(elem, default=""):
    return elem.text.strip() if elem is not None and elem.text else default


def child_text(elem, tag, name=None):
    for c in elem.iter(tag):
        if name is None or c.attrib.get("name") == name:
            return text_of(c)
    return ""


def child_attr(elem, tag, name, attr):
    for c in elem.iter(tag):
        if c.attrib.get("name") == name:
            return c.attrib.get(attr, "")
    return ""


def resource_values(elem, name=None, typename=None):
    vals = []
    for r in elem.iter("ResourceId"):
        if name is not None and r.attrib.get("name") != name:
            continue
        if typename is not None and r.attrib.get("typename") != typename:
            continue
        vals.append(text_of(r))
    return vals


def direct_resource_value(elem, name=None, typename=None):
    for r in elem:
        if r.tag != "ResourceId":
            continue
        if name is not None and r.attrib.get("name") != name:
            continue
        if typename is not None and r.attrib.get("typename") != typename:
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


def classify_from_d_texture(name):
    stem = clean_texture_name(name)
    parts = stem.split("_")
    if len(parts) >= 2 and parts[-1].lower() == "d":
        return parts[1].lower()
    return classify_from_texture(name)


def choose_index_texture(textures):
    d_textures = [t for t in textures if is_d_texture(t)]
    if d_textures:
        return sorted(d_textures)[0], True
    non_lightmap = [t for t in textures if not clean_texture_name(t).lower().startswith("lightmap")]
    if non_lightmap:
        return sorted(non_lightmap)[0], False
    return (sorted(textures)[0], False) if textures else ("-", False)


def is_pcg_name(name):
    return "_pcg_" in clean_texture_name(name).lower()


def parse_descriptor_write(write):
    dst_set = child_text(write, "ResourceId", "dstSet")
    binding = child_text(write, "uint", "dstBinding")
    dtype = child_attr(write, "enum", "descriptorType", "string") or child_text(write, "enum", "descriptorType")
    images = []
    for image_info in write.iter("struct"):
        if image_info.attrib.get("typename") != "VkDescriptorImageInfo":
            continue
        image_view = child_text(image_info, "ResourceId", "imageView")
        sampler = child_text(image_info, "ResourceId", "sampler")
        if image_view or sampler:
            images.append({"imageView": image_view, "sampler": sampler})
    return dst_set, binding, dtype, images


def parse_bind_descriptor_sets(chunk):
    first_set_txt = child_text(chunk, "uint", "firstSet")
    first_set = int(first_set_txt) if first_set_txt.isdigit() else 0
    sets = []
    # Vulkan serialisation uses an array of ResourceId children for descriptor sets.
    for arr in chunk.iter("array"):
        if arr.attrib.get("name") in {"pDescriptorSets", "DescriptorSets", "sets"}:
            for r in arr.iter("ResourceId"):
                if r.attrib.get("typename") == "VkDescriptorSet":
                    sets.append(text_of(r))
    if not sets:
        # Fallback: direct ResourceId children named descriptorSet/DescriptorSet.
        for r in chunk.iter("ResourceId"):
            if r.attrib.get("typename") == "VkDescriptorSet":
                sets.append(text_of(r))
    return first_set, sets


def is_descriptor_set_initial_contents(chunk):
    if chunk.attrib.get("name") != "Internal::Initial Contents":
        return False
    for enum in chunk:
        if enum.tag == "enum" and enum.attrib.get("name") == "type":
            return enum.attrib.get("string") == "eResDescriptorSet" or text_of(enum) == "19"
    return False


def parse_initial_descriptor_set(chunk, view_to_image):
    descriptor_set = direct_resource_value(chunk, "id", "VkDescriptorSet")
    slots = defaultdict(list)
    if not descriptor_set:
        return "", slots

    bindings = None
    for arr in chunk:
        if arr.tag == "array" and arr.attrib.get("name") == "Bindings":
            bindings = arr
            break
    if bindings is None:
        return descriptor_set, slots

    for slot_index, slot in enumerate(bindings):
        if slot.tag != "struct" or slot.attrib.get("typename") != "DescriptorSetSlot":
            continue
        dtype = child_attr(slot, "enum", "type", "string") or child_text(slot, "enum", "type")
        if dtype not in IMAGE_DESCRIPTOR_TYPES and child_text(slot, "enum", "type") not in IMAGE_DESCRIPTOR_NUMBERS:
            continue
        image_view = child_text(slot, "ResourceId", "resource")
        sampler = child_text(slot, "ResourceId", "sampler")
        if not image_view or image_view == "0":
            continue
        slots[str(slot_index)].append(
            {
                "type": dtype,
                "imageView": image_view,
                "image": view_to_image.get(image_view, ""),
                "sampler": sampler,
            }
        )
    return descriptor_set, slots


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python mobile_texture_xml_probe.py capture.xml")
    xml_path = Path(sys.argv[1])
    tree = ET.parse(xml_path)
    root = tree.getroot()

    object_names = {}
    image_to_view = {}
    view_to_image = {}
    descriptor_sets = defaultdict(lambda: defaultdict(list))
    all_texture_names = Counter()
    stats = Counter()

    rows = []
    dispatch_rows = []
    current_sets = {}
    draw_index = 0
    dispatch_index = 0
    current_renderpass = 0
    renderpass_open = False

    for chunk in root.findall("./chunks/chunk"):
        name = chunk.attrib.get("name", "")
        chunk_index = int(chunk.attrib.get("chunkIndex", "-1"))
        if name.startswith("vkCmd"):
            stats[f"cmd::{name}"] += 1

        if name == "vkCreateImageView":
            image = child_text(chunk, "ResourceId", "image")
            view = direct_resource_value(chunk, "View", "VkImageView")
            if image and view:
                image_to_view[image] = view
                view_to_image[view] = image

        elif name == "vkDebugMarkerSetObjectNameEXT":
            obj = direct_resource_value(chunk, "Object", "ResourceId")
            # The name field differs slightly across Vulkan marker paths, so accept any string.
            strings = [text_of(s) for s in chunk.iter("string") if text_of(s)]
            if obj and strings:
                object_names[obj] = strings[-1]

        elif name in {"vkUpdateDescriptorSets", "vkUpdateDescriptorSetWithTemplate"}:
            explicit_set = direct_resource_value(chunk, "descriptorSet", "VkDescriptorSet")
            for struct in chunk.iter("struct"):
                if struct.attrib.get("typename") != "VkWriteDescriptorSet":
                    continue
                dst_set, binding, dtype, images = parse_descriptor_write(struct)
                # Template updates serialise Decoded Writes with dstSet = 0, while the target
                # descriptor set is stored as a top-level field.
                if name == "vkUpdateDescriptorSetWithTemplate" and (not dst_set or dst_set == "0"):
                    dst_set = explicit_set
                if not dst_set:
                    continue
                descriptor_sets[dst_set][binding] = []
                for img in images:
                    if not img["imageView"] or img["imageView"] == "0":
                        continue
                    descriptor_sets[dst_set][binding].append(
                        {
                            "type": dtype,
                            "imageView": img["imageView"],
                            "image": view_to_image.get(img["imageView"], ""),
                            "sampler": img["sampler"],
                        }
                    )

        elif is_descriptor_set_initial_contents(chunk):
            descriptor_set, slots = parse_initial_descriptor_set(chunk, view_to_image)
            if descriptor_set:
                stats["initial_descriptor_sets"] += 1
                if slots:
                    stats["initial_descriptor_sets_with_images"] += 1
                for binding, descs in slots.items():
                    # Initial contents represent the complete descriptor set state before
                    # frame replay. Later vkUpdateDescriptor* chunks override individual bindings.
                    descriptor_sets[descriptor_set][binding] = descs

        elif name == "vkCmdBindDescriptorSets":
            first_set, bound = parse_bind_descriptor_sets(chunk)
            if bound:
                for i, descriptor_set in enumerate(bound):
                    current_sets[first_set + i] = descriptor_set

        elif name == "vkCmdBeginRenderPass":
            current_renderpass += 1
            renderpass_open = True

        elif name == "vkCmdEndRenderPass":
            renderpass_open = False

        elif name in DRAW_NAMES:
            draw_index += 1
            tex = []
            bound_sets = [current_sets[i] for i in sorted(current_sets)]
            for ds in bound_sets:
                for binding, descs in descriptor_sets.get(ds, {}).items():
                    for d in descs:
                        rid = d["image"] or d["imageView"]
                        tex_name = object_names.get(d["imageView"]) or object_names.get(d["image"]) or rid
                        if tex_name:
                            tex.append(tex_name)
            tex_unique = sorted(set(tex))
            for t in tex_unique:
                all_texture_names[t] += 1
            d_tex = [t for t in tex_unique if is_d_texture(t)]
            index_texture, index_is_d = choose_index_texture(tex_unique)
            category = classify_from_d_texture(index_texture) if index_is_d else classify_from_texture(index_texture)
            rows.append(
                {
                    "draw_index": draw_index,
                    "chunk_index": chunk_index,
                    "command": name,
                    "renderpass": current_renderpass if renderpass_open else 0,
                    "index_count": uint_value(chunk, "indexCount"),
                    "vertex_count": uint_value(chunk, "vertexCount"),
                    "instance_count": uint_value(chunk, "instanceCount", 1),
                    "descriptor_sets": bound_sets,
                    "texture_count": len(tex_unique),
                    "index_texture": index_texture,
                    "index_is_d_texture": index_is_d,
                    "category_by_d_texture": category,
                    "d_textures": d_tex,
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
                    "x": uint_value(chunk, "x"),
                    "y": uint_value(chunk, "y"),
                    "z": uint_value(chunk, "z"),
                    "renderpass": current_renderpass if renderpass_open else 0,
                }
            )

    out_json = xml_path.with_name(xml_path.stem + "_texture_probe.json")
    out_md = xml_path.with_name(xml_path.stem + "_texture_probe.md")
    command_counts = Counter()
    for key, count in stats.items():
        if key.startswith("cmd::"):
            command_counts[key.split("::", 1)[1]] = count

    out_json.write_text(
        json.dumps(
            {
                "draws": rows,
                "dispatches": dispatch_rows,
                "texture_usage": all_texture_names,
                "command_counts": command_counts,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    d_counter = Counter()
    category_counter = Counter()
    renderpass_counter = defaultdict(lambda: Counter(draws=0, textured=0, d=0, dispatch=0))
    pcg_by_d = defaultdict(list)
    textured_draws = 0
    d_draws = 0
    for r in rows:
        rp = renderpass_counter[r["renderpass"]]
        rp["draws"] += 1
        if r["texture_count"]:
            textured_draws += 1
            rp["textured"] += 1
        if r["d_textures"]:
            d_draws += 1
            rp["d"] += 1
            for t in r["d_textures"]:
                d_counter[t] += 1
        if r["category_by_d_texture"]:
            category_counter[r["category_by_d_texture"]] += 1
        if r["index_is_d_texture"] and (is_pcg_name(r["index_texture"]) or r["category_by_d_texture"] == "pcg"):
            pcg_by_d[r["index_texture"]].append(r)
    for r in dispatch_rows:
        renderpass_counter[r["renderpass"]]["dispatch"] += 1

    lines = [
        "# Mobile RDC 离线 Draw/纹理分析",
        "",
        f"- XML: `{xml_path}`",
        f"- Draws: {len(rows)}",
        f"- Dispatches: {len(dispatch_rows)}",
        f"- Draws with any resolved texture: {textured_draws}",
        f"- Draws with `_D` texture: {d_draws}",
        f"- Named resources: {len(object_names)}",
        f"- Descriptor sets with image descriptors: {len(descriptor_sets)}",
        f"- Initial descriptor sets: {stats['initial_descriptor_sets']}",
        f"- Initial descriptor sets with image descriptors: {stats['initial_descriptor_sets_with_images']}",
        "",
        "## 纹理分类 DrawCall 排序",
        "| Texture category(second field) | Draws | `_D` indexed draws | Textured draws |",
        "|---|---:|---:|---:|",
    ]
    category_rollup = defaultdict(lambda: Counter(draws=0, d=0, textured=0))
    for r in rows:
        cat = r["category_by_d_texture"] or "unclassified"
        category_rollup[cat]["draws"] += 1
        if r["index_is_d_texture"]:
            category_rollup[cat]["d"] += 1
        if r["texture_count"]:
            category_rollup[cat]["textured"] += 1
    for cat, cnt in sorted(category_rollup.items(), key=lambda kv: kv[1]["draws"], reverse=True):
        lines.append(f"| `{cat}` | {cnt['draws']} | {cnt['d']} | {cnt['textured']} |")

    lines += [
        "",
        "## vkCmd 构成 Top",
        "| Command | Count |",
        "|---|---:|",
    ]
    for cmd, cnt in command_counts.most_common(40):
        lines.append(f"| `{cmd}` | {cnt} |")

    lines += [
        "",
        "## RenderPass 分段",
        "| RenderPass | Draws | Textured | `_D` Draws | Dispatches |",
        "|---:|---:|---:|---:|---:|",
    ]
    for rp, cnt in sorted(renderpass_counter.items()):
        label = "outside" if rp == 0 else str(rp)
        lines.append(f"| {label} | {cnt['draws']} | {cnt['textured']} | {cnt['d']} | {cnt['dispatch']} |")

    lines += [
        "",
        "## `_D` 第二段分类",
        "| Category | Draws |",
        "|---|---:|",
    ]
    for cat, cnt in category_counter.most_common(80):
        lines.append(f"| `{cat}` | {cnt} |")

    lines += [
        "",
        "## `_D` texture draw usage top",
        "| Texture | Draws |",
        "|---|---:|",
    ]
    for tex, cnt in d_counter.most_common(80):
        lines.append(f"| `{tex}` | {cnt} |")
    lines += ["", "## Any texture draw usage top", "| Texture | Draws |", "|---|---:|"]
    for tex, cnt in all_texture_names.most_common(80):
        lines.append(f"| `{tex}` | {cnt} |")
    lines += [
        "",
        "## PCG 明细（按 `_D` 纹理索引去重）",
        "| `_D` Texture | Draws | EID/chunkIndex | 关联贴图示例 |",
        "|---|---:|---|---|",
    ]
    for tex, group in sorted(pcg_by_d.items(), key=lambda kv: len(kv[1]), reverse=True):
        all_group_tex = Counter()
        for r in group:
            for t in r["textures"]:
                all_group_tex[t] += 1
        chunks = ", ".join(str(r["chunk_index"]) for r in group)
        sample_tex = "<br>".join(f"`{t}` ({c})" for t, c in all_group_tex.most_common(12))
        lines.append(f"| `{tex}` | {len(group)} | {chunks} | {sample_tex} |")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(out_md)
    print(out_json)


if __name__ == "__main__":
    main()
