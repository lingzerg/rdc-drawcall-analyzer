# RenderDoc RDC DrawCall 分析工具

这是一个离线分析 RenderDoc `.rdc` 截帧的工具，用来统计 DrawCall 构成，并按纹理名第二字段做分类汇总。默认输出一个可展开的 HTML 页面。

## 仓库包含什么

这个仓库的目标是：在一台新的 Windows 电脑上下载后，不额外安装 Python 或 RenderDoc，也能直接分析。

已内置内容：

- `AnalyzeRDC.cmd`：主入口，推荐使用。
- `AnalyzeMobileRDC.cmd`：移动端截帧入口，本质上调用同一个自动分析器。
- `AnalyzePCRDC.cmd`：PC 截帧入口，本质上调用同一个自动分析器。
- `runtime/python`：内置 Python 运行时。
- `third_party/renderdoc`：最小 RenderDoc 命令行运行集，用于 `renderdoccmd convert`。
- `analyzer`：分析脚本，包含 Vulkan/mobile 和 D3D11/PC 两条解析路径。

## 使用方法

双击：

```text
AnalyzeRDC.cmd
```

然后把 `.rdc` 文件路径粘贴进去，或者把 `.rdc` 文件拖进命令行窗口后按回车。

也可以直接把 `.rdc` 文件拖到 `AnalyzeRDC.cmd` 图标上运行。

## 输出结果

分析结果会写到：

```text
analysis_results/<截帧文件名>/<截帧文件名>_analysis.html
```

默认只保留这一个 HTML 文件。打开 HTML 后：

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

如果找到了这个 rows 文件，工具会优先使用它生成 HTML。这个文件里保留了真实 `primary_texture`，例如：

```text
Roscaelifer_Ground_FloorTwo_01_01_D
```

这样 PC 截帧也可以按真实纹理分类，而不是按 `Texture2D-SRV-*` 分类。

## 注意事项

- 移动端 Vulkan 截帧：通常可以直接离线分析 draw/纹理构成。
- PC D3D11 截帧：如果没有 rows.json，只能按捕获里已有的 SRV debug name 分析，分类质量取决于截帧本身。
- 工具只分析 DrawCall 和纹理绑定构成，不保证还原最终画面。

## 第三方运行时

`third_party/renderdoc` 里包含从本机 RenderDoc 安装目录拷贝的最小命令行运行集。RenderDoc 许可文件见：

```text
third_party/renderdoc/LICENSE.rtf
```
