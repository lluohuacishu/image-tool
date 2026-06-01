# 图片处理工具

一个基于 Python Tkinter 和 Pillow 的桌面图片处理工具，支持批量处理图片。当前主要面向 Windows 使用，也可以直接用 Python 源码运行。release上传的时候忘给exe改名了（悲）。

## 功能

- 按百分比等比缩小图片像素尺寸
- 按轻度 / 中度 / 重度压缩图片体积
- 批量图片格式转换
- 支持导出 Xilinx COE 初始化文本
- 单张图片裁切与旋转
- 实验性透明背景生成
- 图片预览、勾选批处理、右键删除或勾选
- 设置输出目录、日志目录、是否扫描子文件夹
- 可打包为无需 Python 环境的 Windows exe

## 支持格式

常见输入格式包括：

`jpg`、`jpeg`、`png`、`webp`、`bmp`、`tif`、`tiff`、`ico`、`gif`、`ppm`、`pgm`、`pbm`、`pnm`、`tga`、`pcx`、`dds`

常见输出格式包括：

`png`、`jpg`、`jpeg`、`webp`、`bmp`、`tif`、`tiff`、`ico`、`gif`、`ppm`、`tga`、`coe`

## 运行源码

建议使用 Python 3.10 或更高版本。

```bash
pip install -r requirements.txt
python image_tool_gui.py
```

其中 `pyinstaller` 只在需要打包 exe 时使用，日常运行只需要 `pillow`。

## 打包 exe

项目里已经包含 PyInstaller 配置文件：

```bash
pyinstaller --noconfirm --clean 图片处理工具.spec
```

打包完成后，程序会生成在：

```text
dist/图片处理工具.exe
```

## 使用说明

1. 点击 `添加图片` 或 `添加文件夹` 导入图片。
2. 在列表中勾选需要处理的图片，也可以使用 `全选`。
3. 选择处理功能：像素压缩、格式转换、体积压缩、裁切旋转或透明背景。
4. 在设置中选择输出目录。
5. 点击开始按钮执行处理。

## 透明背景说明

透明背景功能目前是实验性功能。它会从图片边缘识别连通背景色，并尝试进行边缘去毛刺和去白边。

如果主体本身包含接近背景的颜色，例如白色衣服、浅色头发、浅色花朵，自动处理可能仍然不完美。可以尝试降低背景容差，或调整边缘净化档位。

## 配置和日志

默认情况下，配置文件和日志会保存在：

```text
%APPDATA%/ImageTool
```

日志目录可以在软件设置里修改。

## 项目结构

```text
image_tool_gui.py        主界面
image_tool_core.py       通用图片工具和安全限制
image_conversion.py      格式转换和 COE 导出
image_compression.py     体积压缩
image_transform.py       裁切/旋转保存逻辑
crop_rotate_editor.py    裁切/旋转编辑窗口
image_transparency.py    实验性透明背景处理
图片处理工具.spec        PyInstaller 打包配置
```

