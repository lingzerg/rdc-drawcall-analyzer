"""Compare every offline index count in a generated fixture to official replay."""
import csv
import importlib.util
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


def module(name):
    path = Path(__file__).resolve().parents[2] / "analyzer" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


root = ET.parse(sys.argv[1]).getroot()
mapping = module("vulkan_offline_events").reconstruct(root)
data = {"draws": [dict(e) for e in mapping["events"] if e["kind"] == "draw"]}
module("vulkan_static_indices").analyze(data, root, mapping, Path(sys.argv[1]).with_suffix(""))
with open(sys.argv[2], encoding="utf-8-sig", newline="") as stream:
    official = {int(r["eventId"]): int(r["uniqueVertices"]) * (int(r["numInstances"]) if int(r["flags"]) & 0x20000 else 1)
                for r in csv.DictReader(stream) if r["kind"] == "draw"}
offline = {r["event_id"]: r["unique_vertex_references"] for r in data["draws"]}
if offline != official:
    raise SystemExit(f"Geometry mismatch: official={official}, offline={offline}, details={data}")
print(f"Official/offline unique indices match for every draw: {offline}")
