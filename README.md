# RenderDoc RDC DrawCall 分析工具

这是一个分析 RenderDoc `.rdc` 截帧的工具，用来统计 DrawCall 构成，并按纹理名第二字段做分类汇总。默认输出一个可展开的 HTML 页面。优先使用 RenderDoc 官方无界面回放；硬件不兼容时回退到离线命令分析。

## EID 与顶点统计准确性

- `AnalyzeRDC.cmd` 自动调用内置的 `rdc_replay_export.exe`，无需打开 RenderDoc 窗口，也无需安装编译器。
- 回放成功时，以 `GetRootActions()` 的实际绘制动作作为主表，直接读取官方 EID、索引/顶点数和实例数；同一 chunk 的多次执行保留为不同的 EID。多重绘制的父节点不重复计数。
- 纹理和 Mesh 名从每个 EID 的管线状态读取，分类继续优先选择 `_D` 纹理，保留 HLOD / PCG HLOD 特殊分类。
- `Unique Vertex Index`（原 `Unique refs`）是每次绘制引用的不同顶点索引数量，乘以实例数后按分类累加。读取索引时使用当前 EID 的缓冲区、绑定偏移、首索引和索引宽度，并排除 primitive restart。它不是场景整体去重顶点数，也不是 GPU 实际执行的 VS 次数。页面优先显示此项。
- `Vertex Index`（原 `Submitted`）是每次绘制的索引数（索引绘制）或顶点数（非索引绘制），乘以有效实例数，包含重复引用。普通 Draw 的实例数按 1 处理，实例化 Draw 的 0 实例则保留为 0。
- 索引数据读取不完整或单次读取超过 64 MiB 时，Unique Vertex Index 显示 `Unknown`。汇总同时显示未知项数量，不将未知值作为完整的 0。
- Vulkan 回放失败时，会尝试按官方源码规则离线重建 EID。当前支持序列化版本 25 / 32 中已覆盖的普通主命令缓冲区指令，按提交顺序展开，重复提交保留多次执行，未提交的录制命令不计入执行数。页面标为 `EID (offline)`，不冒充官方回放结果。
- 离线规则固定参照随包 RenderDoc `050034a0faa37d606ce1b8cf677dba4bc36984ea`。已用本机 Vulkan 测试截帧与官方回放逐动作核对，覆盖交错录制、乱序提交、重复提交、重新录制及空命令缓冲区列表。手机截帧仍需要与同版本 RenderDoc 的实际结果核验。
- 遇到未覆盖指令、次级命令缓冲区、多重/间接绘制、版本差异或数据不完整时，整份 EID 映射停止输出，日志说明首个阻断位置；报告退回录制命令数及 `chunk:编号`，不会继续编造编号。D3D11 官方回放流程不变。
- 离线 EID 重建不等于重放 GPU：纹理仍来自离线绑定解析，动态描述符等情况仍有局限；未读取索引缓冲区时 `Unique Vertex Index` 仍为未知，间接绘制的几何数量也为未知。
- 无界面回放仍需要兼容的 GPU/驱动。手机 Vulkan 的 ASTC 扩展不兼容无法通过命令行绕过。

排查时加 `--keep-intermediate` 可保留官方动作 CSV 和归一化 JSON。`--offline` 可直接跳过回放。官方 CSV 中 `numInstances` 是未经修改的 API 原值；归一化 JSON 中 `instance_count` 是结合 Instanced 标志计算后的有效实例数。

离线 Vulkan 重建成功时，`--keep-intermediate` 还会保留 `*_offline_eid_map.json`，逐事件记录 EID、chunkIndex、原始/烘焙命令缓冲区、提交位置及执行次数索引，方便核对。也可以单独运行 `runtime\python\python.exe analyzer\vulkan_offline_events.py capture.xml events.json`。此命令不创建 GPU 设备。

开发者可运行 `native\BuildExporter.cmd -RenderDocSource D:\workspace\renderdoc` 重新编译导出器，需要 Visual Studio C++ 工具。构建脚本自动选用与内置 DLL 提交版本完全一致的头文件，运行时也会校验版本。

回归测试：`runtime\python\python.exe -m unittest discover -s analyzer -p test*.py`。开发者还可在 VS x64 开发者命令行运行 `powershell -ExecutionPolicy Bypass -File native\tests\build_vulkan_fixture.ps1 -RenderDocSource D:\workspace\renderdoc`，生成无窗口 Vulkan 测试截帧，并自动比较官方回放与离线结果；该集成测试需要本机 Vulkan GPU，普通离线分析不需要。

## 仓库包含什么

这个仓库的目标是：在一台新的 Windows 电脑上下载后，不额外安装 Python 或 RenderDoc，也能直接分析。

已内置内容：

- `AnalyzeRDC.cmd`：唯一入口，自动处理 PC 和移动端截帧，不需要选择平台。
- `runtime/python`：内置 Python 运行时。
- `third_party/renderdoc`：RenderDoc 命令行运行集，包含官方回放导出器和离线转换工具。
- `analyzer`：分析脚本，包含 Vulkan/mobile 和 D3D11/PC 两条解析路径。

启动时 `AnalyzeRDC.cmd` 会检查这些运行时是否存在：

- 优先使用仓库内置的 `runtime/python/python.exe`。
- 优先使用仓库内置的 `third_party/renderdoc/renderdoccmd.exe`。
- 如果内置文件缺失，会尝试使用系统 PATH 里的 `python` 或 `renderdoccmd.exe`。
- 如果都找不到，会在命令行里给出修复方式和下载地址。

## 使用方法

双击：

```text
AnalyzeRDC.cmd
```

然后把 `.rdc` 文件路径粘贴进去，或者把 `.rdc` 文件拖进命令行窗口后按回车。

也可以直接把 `.rdc` 文件拖到 `AnalyzeRDC.cmd` 图标上运行。

程序优先尝试官方无界面回放；无法回放时，读取截帧内的图形 API 标识，D3D11 使用对应的离线解析器，Vulkan 使用 Vulkan 离线解析器并尝试重建 EID。分支不依据文件名，也不会把所有 Vulkan 截帧都认作手机截帧。其他 API 在回放失败后会提示离线解析暂不支持，不会误套已有分支。旧的两个平台专用 CMD 已移除。

## 输出结果

分析结果会写到：

```text
analysis_results/<截帧文件名>/<截帧文件名>_analysis.html
```

默认保留 HTML 和 `analysis.log` 日志。打开 HTML 后：

- 最上方是每个分类的可展开明细。
- 点击分类可以展开该分类下的 DrawCall。
- 每条 DrawCall 会显示纹理、chunkIndex、RenderPass/Marker、命令、实例数等信息。
- 页面底部有 Texture Category Summary 汇总表。

如果需要保留中间文件（XML/JSON/CSV/MD）用于排查，可以手动执行：

```bat
runtime\python\python.exe analyzer\mobile_rdc_batch_analyze.py your_capture.rdc --keep-intermediate
```

## 支持的截帧

### Vulkan / 移动端截帧

适用于手机 Vulkan RenderDoc 截帧，尤其是本机 RenderDoc 因为 GPU/扩展不兼容无法 replay 的情况。

工具会离线解析：

- `vkCmdDraw*`
- `vkCmdDispatch*`
- `vkCmdBindDescriptorSets`
- descriptor set initial contents
- image view 和纹理资源名

这条路径不需要打开画面，也不需要真机。

### D3D11 / PC 截帧

PC D3D11 截帧会优先走 XML 离线解析，读取 shader resource view 绑定关系。

但要注意：如果 D3D11 截帧的 XML 里只有 `Texture2D-SRV-*`、`RenderTexture-SRV-*` 这类通用 debug name，那么离线 XML 无法还原真实资产纹理名，也就无法可靠按 `*_D` 纹理分类。

为了解决这个问题，工具会自动查找同名的 pipeline rows 文件：

```text
<capture_name>_rows.json
```

查找位置：

- `.rdc` 文件同目录
- `.rdc` 文件同目录下的 `renderdoc_mcp_work`
- 当前输出目录

如果找到了这个 rows 文件，工具不会再用它替代全量统计，而是采用“双口径”输出：

- XML 离线解析结果作为主账，保证 `Draw calls` / `Categorized draws` 覆盖截帧里的全部 draw call。
- rows 文件作为增强纹理表，显示在 `Enhanced Pipeline Texture Rows` 和 `Enhanced Index Texture Usage` 中，用来查看真实 `primary_texture`、mesh 和 EID。

rows 文件里保留了真实 `primary_texture`，例如：

```text
Roscaelifer_Ground_FloorTwo_01_01_D
```

这样 PC 截帧可以先看到全量 draw call 构成，再用增强表按真实纹理分析重点部分，而不是只按 `Texture2D-SRV-*` 分类。

## 注意事项

- 移动端 Vulkan 截帧：通常可以直接离线分析 draw/纹理构成。
- PC D3D11 截帧：如果没有 rows.json，只能按捕获里已有的 SRV debug name 分析，分类质量取决于截帧本身。
- 工具只分析 DrawCall 和纹理绑定构成，不保证还原最终画面。

## 第三方运行时

`third_party/renderdoc` 里包含从本机 RenderDoc 安装目录拷贝的最小命令行运行集。RenderDoc 许可文件见：

```text
third_party/renderdoc/LICENSE.rtf
```
