import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
import imageio_ffmpeg
import shutil
import socket
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "2.4.0")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.config = config
        
        # 1. 基础路径
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        # 2. 寻找 FFmpeg
        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            self.logger.info(f"已加载 FFmpeg: {self.ffmpeg_exe}")
        except Exception as e:
            self.ffmpeg_exe = "ffmpeg"
            self.logger.warning(f"imageio-ffmpeg 加载失败: {e}")
            
        # 3. 基础配置
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        self.max_quality = self.config.get("download", {}).get("max_quality", "720p")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 50) # 默认改小一点
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        
        # 4. 启动内置 HTTP 服务器
        self.server_port = 0 
        self.server_ip = self._get_local_ip()
        self._start_http_server()
        self.logger.info(f"文件服务器已启动: http://{self.server_ip}:{self.server_port}")

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _start_http_server(self):
        class TempDirHandler(SimpleHTTPRequestHandler):
            def __init__(handler_self, *args, **kwargs):
                super().__init__(*args, directory=self.temp_dir, **kwargs)
            def log_message(self, format, *args):
                pass

        def run_server():
            server = HTTPServer(('0.0.0.0', 0), TempDirHandler)
            self.server_port = server.server_port
            server.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        time.sleep(0.5)

    @command("check_env")
    async def cmd_check_env(self, event: AstrMessageEvent):
        """诊断插件环境"""
        yield event.plain_result(f"🔍 环境诊断:\nFFmpeg: {self.ffmpeg_exe}\nServer: http://{self.server_ip}:{self.server_port}")

    def _sanitize_filename(self, name: str) -> str:
        if not name: return "video"
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.replace('\n', ' ').replace('\r', '')
        return name[:50].strip()

    def _format_size(self, size_bytes):
        if size_bytes is None: return "未知"
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024: return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024: return f"{size_bytes / (1024 * 1024):.2f} MB"
        else: return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    async def _manual_merge(self, video_path, audio_path, output_path):
        cmd = [self.ffmpeg_exe, "-i", video_path, "-i", audio_path, "-c:v", "copy", "-c:a", "copy", "-y", output_path]
        def _run():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
        
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _run)
        if result.returncode != 0:
            raise Exception(f"合并失败: {result.stderr[:100]}")
        return output_path

    async def _get_video_info_safe(self, url):
        ydl_opts = {"quiet": True, "no_warnings": True, "nocheckcertificate": True, "extract_flat": False}
        if self.proxy_enabled and self.proxy_url: ydl_opts["proxy"] = self.proxy_url
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False))
            filesize = info.get('filesize') or info.get('filesize_approx')
            if not filesize and info.get('formats'):
                for fmt in info.get('formats', []):
                    filesize = max(filesize or 0, fmt.get('filesize', 0) or fmt.get('filesize_approx', 0))
            return {
                'title': info.get('title', ''), 'duration': info.get('duration'),
                'filesize': filesize, 'resolution': info.get('resolution'), 'uploader': info.get('uploader'),
            }
        except Exception as e:
            self.logger.error(f"Info Error: {e}")
            return None

    async def _check_content_safety_llm(self, title: str):
        provider = self.context.get_using_provider()
        if not provider: return True
        prompt = f"审核标题：{title}\n包含政治敏感/反动/严重色情/严重暴恐吗？\n包含回复UNSAFE，否则回复SAFE。仅回复一个单词。"
        try:
            response = await provider.text_chat(prompt, session_id=None)
            ans = response if isinstance(response, str) else response.completion_text
            if "UNSAFE" in str(ans).upper(): return False
            return True
        except: return True

    async def _download_stream(self, url, format_str, filename_tmpl):
        ydl_opts = {
            "outtmpl": filename_tmpl, "format": format_str, "noplaylist": True,
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "ffmpeg_location": self.ffmpeg_exe, 
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        if self.proxy_enabled and self.proxy_url: ydl_opts["proxy"] = self.proxy_url
        loop = asyncio.get_running_loop()
        def _task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info
        return await loop.run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, send_method: str, content_type: str = "merged"):
        if not url and send_method != "tool_auto": 
            yield event.plain_result(f"Usage: /download <URL>")
            return

        mode_text = "纯音频" if content_type == "audio_only" else "音画合并"
        yield event.plain_result(f"⏳ 正在获取视频信息...")
        
        video_info = await self._get_video_info_safe(url)
        if video_info:
            est_size = self._format_size(video_info.get('filesize'))
            duration = video_info.get('duration') or 0
            yield event.plain_result(f"📹 标题: {video_info.get('title')[:30]}\n⏱️ 时长: {int(duration//60)}分{int(duration%60)}秒\n📦 预估: {est_size}\n⏳ 开始下载...")
        else:
            yield event.plain_result(f"⏳ 开始下载...")

        timestamp_id = int(time.time())
        video_tmpl = f"{self.temp_dir}/v_{timestamp_id}_%(id)s.%(ext)s"
        audio_tmpl = f"{self.temp_dir}/a_{timestamp_id}_%(id)s.%(ext)s"
        
        quality_map = { "480p": 480, "720p": 720, "1080p": 1080, "最高画质": None }
        max_height = quality_map.get(self.max_quality, 720)
        fmt_video = f"bestvideo[vcodec^=avc1][height<=?{max_height}]" if max_height else "bestvideo[vcodec^=avc1]"
        fmt_fallback = "best"
        fmt_audio = "bestaudio[acodec^=mp4a]/bestaudio"

        final_file_path = None
        video_title = "media"
        temp_files_to_clean = []

        try:
            if content_type == "audio_only":
                a_path, a_info = await self._download_stream(url, fmt_audio, audio_tmpl)
                video_title = a_info.get('title', 'audio')
                final_file_path = a_path 
            else:
                try:
                    v_path, v_info = await self._download_stream(url, fmt_video, video_tmpl)
                    video_title = v_info.get('title', 'video')
                    temp_files_to_clean.append(v_path)
                    a_path, a_info = await self._download_stream(url, fmt_audio, audio_tmpl)
                    temp_files_to_clean.append(a_path)
                    
                    yield event.plain_result("⚙️ 正在合并...")
                    output_path = os.path.join(self.temp_dir, f"final_{timestamp_id}.mp4")
                    await self._manual_merge(v_path, a_path, output_path)
                    final_file_path = output_path
                except Exception:
                    f_path, f_info = await self._download_stream(url, fmt_fallback, video_tmpl)
                    video_title = f_info.get('title', 'video')
                    final_file_path = f_path

            if not final_file_path or not os.path.exists(final_file_path):
                raise Exception("文件生成失败")

            file_size_mb = os.path.getsize(final_file_path) / (1024 * 1024)
            if file_size_mb > self.max_size_mb:
                yield event.plain_result(f"❌ 文件过大 ({file_size_mb:.2f}MB > {self.max_size_mb}MB)")
                return

            yield event.plain_result(f"✅ 下载完成 ({file_size_mb:.2f}MB)\n📤 正在上传...")
            
            # ========== 核心发送逻辑修正 ==========
            file_name = os.path.basename(final_file_path)
            file_url = f"http://{self.server_ip}:{self.server_port}/{file_name}"
            self.logger.info(f"推送链接: {file_url}")

            if send_method == "file":
                # 文件模式：直接调用 API 上传，不走 File 组件
                safe_title = self._sanitize_filename(video_title)
                ext = os.path.splitext(final_file_path)[1]
                full_name = f"{safe_title}{ext}"
                
                # 获取 Session ID
                is_group = False
                target_id = None
                
                if hasattr(event, 'message_obj'):
                    raw_msg = event.message_obj
                    if hasattr(raw_msg, 'group_id') and raw_msg.group_id:
                        is_group = True
                        target_id = raw_msg.group_id
                    elif hasattr(raw_msg, 'user_id'):
                        target_id = raw_msg.user_id
                
                if target_id:
                    try:
                        action = "upload_group_file" if is_group else "upload_private_file"
                        params = {
                            "group_id" if is_group else "user_id": int(target_id),
                            "file": file_url,
                            "name": full_name
                        }
                        self.logger.info(f"调用 API: {action} {params}")
                        await event.bot.call_action(action, **params)
                        # yield event.plain_result("✅ 上传请求已发送")
                    except Exception as e:
                        yield event.plain_result(f"❌ 上传 API 失败: {e}")
                else:
                    yield event.plain_result("❌ 无法获取上传目标 ID")
            else:
                # 视频模式：Video组件对URL支持较好，可以直接用
                yield event.chain_result([Video(file=file_url, url=file_url)])
            
            # ========== 结束 ==========

            async def _cleanup():
                await asyncio.sleep(self.delete_seconds + 30)
                try: 
                    if os.path.exists(final_file_path): os.remove(final_file_path) 
                except: pass
                for f in temp_files_to_clean:
                    try: os.remove(f)
                    except: pass
            asyncio.create_task(_cleanup())

        except Exception as e:
            self.logger.error(f"Error: {e}", exc_info=True)
            yield event.plain_result(f"❌ 错误: {str(e)[:50]}")

    @llm_tool(name="download_video")
    async def cmd_llm_download_video(self, event: AstrMessageEvent, url: str, mode: str = "video_stream"):
        '''下载视频工具 (mode: "video_stream", "video_file", "audio_only")'''
        yield event.plain_result("🔍 安全检查中...")
        info = await self._get_video_info_safe(url)
        if info and not await self._check_content_safety_llm(info.get('title')):
            yield event.plain_result("⚠️ 包含敏感内容，已拦截。")
            return

        method = "file" if mode in ["video_file", "audio_only"] else "video"
        ctype = "audio_only" if mode == "audio_only" else "merged"
        
        async for res in self._core_download_handler(event, url, method, ctype):
            yield res

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        """下载文件"""
        async for res in self._core_download_handler(event, url, "file", "merged"):
            yield res

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        """下载视频"""
        async for res in self._core_download_handler(event, url, "video", "merged"):
            yield res
            
    @command("extract")
    async def cmd_extract_url(self, event: AstrMessageEvent, url: str = ""):
        """提取直链"""
        if not url: return
        ydl_opts = {"quiet": True}
        if self.proxy_enabled: ydl_opts["proxy"] = self.proxy_url
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False))
            yield event.plain_result(f"🔗 {info.get('url')}")
        except Exception as e:
            yield event.plain_result(f"❌ {e}")