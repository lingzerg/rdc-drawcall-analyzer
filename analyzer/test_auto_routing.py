import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location("analyzer", Path(__file__).with_name("mobile_rdc_batch_analyze.py"))
analyzer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analyzer)


class AutoRoutingTests(unittest.TestCase):
    def capture_xml(self, directory, filename, driver):
        root = ET.Element("rdc")
        ET.SubElement(ET.SubElement(root, "header"), "driver").text = driver
        path = Path(directory) / filename
        ET.ElementTree(root).write(path, encoding="utf-8")
        return path

    def test_parser_uses_capture_api_not_filename(self):
        cases = [("mobile_android.xml", "D3D11", "d3d11_texture_xml_probe.py"),
                 ("desktop_pc.xml", "Vulkan", "mobile_texture_xml_probe.py")]
        with tempfile.TemporaryDirectory() as directory:
            for filename, driver, parser in cases:
                path = self.capture_xml(directory, filename, driver)
                with patch.object(analyzer.subprocess, "run") as run:
                    analyzer.run_probe(path)
                run.assert_called_once_with(
                    [analyzer.sys.executable, str(Path(analyzer.__file__).with_name(parser)), str(path)],
                    check=True,
                )

    def test_unsupported_api_does_not_fall_through_to_vulkan(self):
        with tempfile.TemporaryDirectory() as directory:
            for driver in ("OpenGL", "D3D12", ""):
                path = self.capture_xml(directory, "mobile.xml", driver)
                with patch.object(analyzer.subprocess, "run") as run:
                    with self.assertRaisesRegex(ValueError, "compatible replay is required"):
                        analyzer.run_probe(path)
                    run.assert_not_called()

    def test_one_root_launcher(self):
        root = Path(analyzer.__file__).resolve().parent.parent
        self.assertEqual(sorted(p.name for p in root.glob("Analyze*.cmd")), ["AnalyzeRDC.cmd"])


if __name__ == "__main__":
    unittest.main()
