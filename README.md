# 🎬 AstrBot 全能视频下载插件 (Video Downloader)

一个基于 `yt-dlp` 的强大视频解析与下载插件。
告别繁琐的网页转换，直接在群聊中发送链接，机器人自动下载并发送高清视频！

## ✨ 功能特色

*   **全平台支持**：完美支持 **Bilibili** (自动合并音画)、**YouTube** (最高720p)、**Twitter/X**、**TikTok** 等主流平台。
*   **智能下载**：自动选择最适合 QQ 发送的画质，避免文件过大发送失败。
*   **无感体验**：支持解析短链接（b23.tv, youtu.be），自动识别 URL。
    
## 📦 安装说明

### 轻量安装 (需自行配置 FFmpeg)
0. 定位AstrBot\venv\Scripts\python.exe的文件位置，cmd或PowerShell执行器执行& "AstrBot\venv\Scripts\python.exe" -m pip install yt-dlp安装yt-dlp库
1. 将文件夹放入 `AstrBot/data/plugins/` 目录。
2. 确保你的电脑/服务器已安装 `FFmpeg` 并配置了环境变量。
3. https://www.gyan.dev/ffmpeg/builds/官网下载包后把ffmpeg压缩包内的ffmpeg.exe和ffprobe.exe放入插件同一目录内。
4. 重启 AstrBot。

> **注意**：如果不安装 FFmpeg，B站视频将无法下载（因为 B站 高清视频是音画分离的，需要 FFmpeg 合并）。

## 📖 使用指南

### 1. 下载视频 (核心功能)
发送指令：
`/下载 <视频链接>`

示例：
`/下载 https://www.bilibili.com/video/BV1xxxxxx`

机器人会自动：
1. 解析视频信息。
2. 下载视频流和音频流。
3. 合并为 MP4 文件。
4. 发送到当前群聊/私聊。
5. 自动清理临时文件。

### 2. 提取直链 (仅获取链接)
发送指令：
`/提取 <视频链接>`

仅返回视频的直链地址（URL），适合需要快速复制链接的场景。

## ⚙️ 配置说明 (可选)

在 `main.py` 顶部可以修改以下配置：

```python
# 本地代理地址 (如果你在中国大陆运行，通常需要配置梯子)
PROXY_URL = "http://127.0.0.1:7897" 
