# RenderDoc RDC Analyzer

Offline draw-call and texture-category analyzer for RenderDoc `.rdc` captures.

## What is included

This repository is intended to work after cloning/downloading on a fresh Windows PC:

- `AnalyzeRDC.cmd`: main entry point.
- `runtime/python`: embedded Python runtime used by the analyzer.
- `third_party/renderdoc`: minimal RenderDoc command-line runtime used for `renderdoccmd convert`.
- `analyzer`: Python scripts for Vulkan/mobile and D3D11/PC XML analysis.

No Python or RenderDoc installation is required if the bundled runtime folders are present.

## Usage

Double-click `AnalyzeRDC.cmd`, then paste or drag a `.rdc` file path into the window.

You can also drag a `.rdc` file directly onto `AnalyzeRDC.cmd`.

Convenience wrappers:

- `AnalyzeMobileRDC.cmd`
- `AnalyzePCRDC.cmd`

Both wrappers call the same auto-detect analyzer.

## Output

Results are written to:

```text
analysis_results/<capture_name>/<capture_name>_analysis.html
```

Open the HTML in a browser. The page shows category details first; each category
can be expanded to inspect draw rows, texture bindings, chunkIndex, renderpass,
and command information.

By default only the HTML report is kept. Intermediate XML/JSON/CSV/MD files are
deleted after analysis. For debugging, run:

```bat
runtime\python\python.exe analyzer\mobile_rdc_batch_analyze.py your_capture.rdc --keep-intermediate
```

## Supported Captures

- Vulkan/mobile: parses descriptor set initial contents and command binding state.
- D3D11/PC: parses shader resource view binding state from XML.

For D3D11 captures, texture category quality depends on debug names stored in the
capture. If only generic names like `Texture2D-SRV-*` exist, asset-level `_D`
classification cannot be recovered from offline XML alone.

For PC captures with precomputed pipeline rows, the analyzer will automatically
prefer `<capture_name>_rows.json` when it exists either next to the `.rdc` file,
inside a sibling `renderdoc_mcp_work` folder, or inside the output folder. This
rows file preserves real asset texture names such as `*_D`, so PC reports can be
classified by actual texture category instead of generic SRV debug names.

## Third-party Runtime

`third_party/renderdoc` contains a minimal RenderDoc command-line runtime copied
from a local RenderDoc installation. See `third_party/renderdoc/LICENSE.rtf`.
