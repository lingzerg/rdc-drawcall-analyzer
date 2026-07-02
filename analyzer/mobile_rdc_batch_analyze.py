import argparse
import csv
import html
import json
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

    for category, group in sorted(by_category.items(), key=lambda kv: len(kv[1]), reverse=True):
        index_counter = Counter(r.get("index_texture") or "-" for r in group)
        rp_counter = Counter(r.get("renderpass") for r in group)
        category_rows.append(
            {
                "category_second_field": category,
                "draw_calls": len(group),
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
                "chunk_index",
                "renderpass",
                "command",
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
                    "chunk_index": r.get("chunk_index"),
                    "renderpass": r.get("renderpass"),
                    "command": r.get("command"),
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


def write_html_report(stem, out_dir, source_path, data, by_category):
    html_path = out_dir / f"{stem}_analysis.html"
    rows = data.get("draws", [])
    dispatches = data.get("dispatches", [])
    command_counts = Counter(data.get("command_counts", {}))
    ordered = sorted(by_category.items(), key=lambda kv: len(kv[1]), reverse=True)
    textured = sum(1 for r in rows if r.get("texture_count"))
    d_indexed = sum(1 for r in rows if r.get("index_is_d_texture"))

    summary_rows = []
    for category, group in ordered:
        top_idx = Counter(r.get("index_texture") or "-" for r in group).most_common(5)
        renderpasses = Counter(r.get("renderpass") for r in group).most_common(5)
        summary_rows.append(
            "<tr>"
            f"<td><a href='#{html.escape(category)}'>{html.escape(category)}</a></td>"
            f"<td class='num'>{len(group)}</td>"
            f"<td class='num'>{sum(1 for r in group if r.get('texture_count'))}</td>"
            f"<td class='num'>{sum(1 for r in group if r.get('index_is_d_texture'))}</td>"
            f"<td>{html.escape('; '.join(f'{k} ({v})' for k, v in top_idx))}</td>"
            f"<td>{html.escape('; '.join(f'{k}:{v}' for k, v in renderpasses))}</td>"
            "</tr>"
        )

    detail_blocks = []
    for category, group in ordered:
        top_tex = Counter()
        for r in group:
            for tex in r.get("textures") or []:
                top_tex[tex] += 1
        top_tex_html = " ".join(
            f"<span class='pill'>{html.escape(str(t))} <b>{c}</b></span>"
            for t, c in top_tex.most_common(12)
        )
        detail_rows = []
        for r in group:
            idx_or_vert = r.get("index_count") or r.get("vertex_count") or 0
            detail_rows.append(
                "<tr>"
                f"<td><code>{html.escape(str(r.get('index_texture') or '-'))}</code></td>"
                f"<td><code>{html.escape(str(r.get('mesh_name') or ''))}</code></td>"
                f"<td class='num'>{r.get('draw_index')}</td>"
                f"<td>{html_texture_list(r.get('textures') or [])}</td>"
                f"<td class='num'>{r.get('texture_count')}</td>"
                f"<td class='num'>{r.get('chunk_index')}</td>"
                f"<td>{html.escape(str(r.get('renderpass') or ''))}</td>"
                f"<td><code>{html.escape(str(r.get('command') or ''))}</code></td>"
                f"<td class='num'>{idx_or_vert}</td>"
                f"<td class='num'>{r.get('instance_count')}</td>"
                "</tr>"
            )
        detail_blocks.append(
            f"""
            <details id="{html.escape(category)}" class="category">
              <summary>
                <span class="cat">{html.escape(category)}</span>
                <span>{len(group)} draw calls</span>
                <span>{sum(1 for r in group if r.get('texture_count'))} textured</span>
                <span>{sum(1 for r in group if r.get('index_is_d_texture'))} _D indexed</span>
              </summary>
              <div class="toptex">{top_tex_html or '<span class="muted">No textures</span>'}</div>
              <table>
                <thead>
                  <tr>
                    <th>Index texture</th><th>Mesh</th><th>Draw #</th>
                    <th>Textures</th><th>Texture count</th><th>chunkIndex</th>
                    <th>RenderPass/Marker</th><th>Cmd</th><th>idx/verts</th><th>inst</th>
                  </tr>
                </thead>
                <tbody>{''.join(detail_rows)}</tbody>
              </table>
            </details>
            """
        )

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
    th, td {{ border-bottom:1px solid var(--border); padding:7px 8px; vertical-align:top; text-align:left; }}
    th {{ position:sticky; top:0; background:#eef2f7; z-index:1; }}
    .num {{ text-align:right; white-space:nowrap; }}
    code {{ font-family:Consolas, "Cascadia Mono", monospace; font-size:12px; }}
    a {{ color:var(--accent); text-decoration:none; }}
    details.category {{ background:var(--panel); border:1px solid var(--border); border-radius:8px; margin:10px 0; overflow:hidden; }}
    details.category > summary {{ cursor:pointer; display:flex; gap:14px; align-items:center; padding:10px 12px; background:#eef2f7; font-weight:600; }}
    summary .cat {{ min-width:210px; color:#0b4aa2; }}
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
    </div>

    <h2 class="section-title">Category Details</h2>
    {''.join(detail_blocks)}

    <h2 class="section-title">Command Top</h2>
    <table><thead><tr><th>Command</th><th>Count</th></tr></thead><tbody>{cmd_rows}</tbody></table>

    <h2 class="section-title">Texture Category Summary</h2>
    <table>
      <thead><tr><th>Category(second field)</th><th>Draws</th><th>Textured</th><th>_D indexed</th><th>Top index textures</th><th>Top renderpasses</th></tr></thead>
      <tbody>{''.join(summary_rows)}</tbody>
    </table>
  </main>
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
        "| Category(second field) | DrawCalls | Textured | `_D` indexed | Top index textures |",
        "|---|---:|---:|---:|---|",
    ]
    ordered = sorted(by_category.items(), key=lambda kv: len(kv[1]), reverse=True)
    for category, group in ordered:
        top_idx = Counter(r.get("index_texture") or "-" for r in group).most_common(5)
        lines.append(
            f"| `{category}` | {len(group)} | {sum(1 for r in group if r.get('texture_count'))} | "
            f"{sum(1 for r in group if r.get('index_is_d_texture'))} | "
            f"{'; '.join(f'`{k}` ({v})' for k, v in top_idx)} |"
        )

    for category, group in ordered:
        lines += [
            "",
            f"## {category}",
            "",
            f"- DrawCall: {len(group)}",
            f"- Textured: {sum(1 for r in group if r.get('texture_count'))}",
            f"- `_D` indexed: {sum(1 for r in group if r.get('index_is_d_texture'))}",
            "",
            "| Index texture | Mesh | Draw # | Textures | Texture count | chunkIndex | RenderPass | Cmd | idx/verts | inst |",
            "|---|---|---:|---|---:|---:|---|---|---:|---:|",
        ]
        for r in group:
            idx_or_vert = r.get("index_count") or r.get("vertex_count") or 0
            lines.append(
                f"| `{r.get('index_texture')}` | `{r.get('mesh_name')}` | {r.get('draw_index')} | "
                f"{md_texture_list(r.get('textures') or [])} | {r.get('texture_count')} | "
                f"{r.get('chunk_index')} | {r.get('renderpass')} | `{r.get('command')}` | "
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
        print(f"[2/4] Use precomputed pipeline rows: {rows_path}", flush=True)
        data = data_from_precomputed_rows(rows_path)
        probe_json = probe_md = None
    else:
        print("[2/4] Parse draw calls and texture bindings", flush=True)
        probe_json, probe_md = run_probe(xml_path)
        data = json.loads(probe_json.read_text(encoding="utf-8"))

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
