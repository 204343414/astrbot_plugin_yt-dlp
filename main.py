import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "2.3.11")
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
        self.ffmpeg_exe = os.path.join(self.plugin_dir, "ffmpeg.exe")
        
        # 3. 基础配置
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        self.max_quality = self.config.get("download", {}).get("max_quality", "720p")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 512)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)

    @command("check_env")
    async def cmd_check_env(self, event: AstrMessageEvent):
        """诊断 FFmpeg 环境"""
        yield event.plain_result(f"🔍 诊断中...\nFFmpeg: {self.ffmpeg_exe}")
        if os.path.exists(self.ffmpeg_exe):
            try:
                cmd = [self.ffmpeg_exe, "-version"]
                startupinfo = None
                if os.name == 'nt':
                    startupinfo = subprocess.STARTUPINFO()
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                res = subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
                if res.returncode == 0:
                    yield event.plain_result(f"✅ FFmpeg 运行正常: {res.stdout.splitlines()[0]}")
                else:
                    yield event.plain_result(f"❌ 运行报错: {res.stderr}")
            except Exception as e:
                yield event.plain_result(f"❌ 运行异常: {e}")
        else:
            yield event.plain_result(f"❌ 文件不存在: 请确保 ffmpeg.exe 在插件目录下")

    def _sanitize_filename(self, name: str) -> str:
        if not name: return "video"
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.replace('\n', ' ').replace('\r', '')
        return name[:50].strip()

    async def _manual_merge(self, video_path, audio_path, output_path):
        """Python 手动合并，无视路径字符问题"""
        self.logger.info(f"开始合并:\nV: {video_path}\nA: {audio_path}")
        
        # 构造命令: ffmpeg -i video -i audio -c copy output.mp4
        cmd = [
            self.ffmpeg_exe, 
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "copy",
            "-y",
            output_path
        ]
        
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        def _run():
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _run)
        
        if result.returncode != 0:
            raise Exception(f"合并失败: {result.stderr[:100]}")
        return output_path

    async def _download_stream(self, url, format_str, filename_tmpl):
        """单纯下载单个流，不进行任何合并操作"""
        ydl_opts = {
            "outtmpl": filename_tmpl,
            "format": format_str,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            # 彻底禁用合并相关设置
            "merge_output_format": None,
            "ffmpeg_location": None, 
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        if self.proxy_enabled and self.proxy_url:
            ydl_opts["proxy"] = self.proxy_url

        loop = asyncio.get_running_loop()
        def _task():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info
        
        return await loop.run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, send_mode: str):
        if not url:
            cmd = "download" if send_mode == "file" else "video"
            yield event.plain_result(f"Usage: /{cmd} <URL>")
            return

        yield event.plain_result(f"⏳ 正在下载 (分流模式)...")

        timestamp_id = int(time.time())
        # 两个临时文件路径
        video_tmpl = f"{self.temp_dir}/v_{timestamp_id}_%(id)s.%(ext)s"
        audio_tmpl = f"{self.temp_dir}/a_{timestamp_id}_%(id)s.%(ext)s"
        
        quality_map = { "480p": 480, "720p": 720, "1080p": 1080 }
        max_height = quality_map.get(self.max_quality, 720)

        # 定义格式
        # 视频：H.264
        fmt_video = f"bestvideo[vcodec^=avc1][height<=?{max_height}]/bestvideo[height<=?{max_height}]"
        # 音频：AAC
        fmt_audio = "bestaudio[acodec^=mp4a]/bestaudio"
        # 保底：如果网站不支持分离流 (如B站)，直接下载 best
        fmt_fallback = f"best[ext=mp4]/best"

        final_file_path = None
        video_title = "video"
        temp_files_to_clean = []

        try:
            # === 第一步：尝试下载视频流 ===
            self.logger.info("Step 1: Downloading Video Stream")
            try:
                v_path, v_info = await self._download_stream(url, fmt_video, video_tmpl)
                video_title = v_info.get('title', 'video')
                temp_files_to_clean.append(v_path)
                
                # === 第二步：尝试下载音频流 ===
                self.logger.info("Step 2: Downloading Audio Stream")
                a_path, a_info = await self._download_stream(url, fmt_audio, audio_tmpl)
                temp_files_to_clean.append(a_path)
                
                # === 第三步：合并 ===
                self.logger.info("Step 3: Merging")
                yield event.plain_result("⚙️ 正在合并音视频...")
                
                output_path = os.path.join(self.temp_dir, f"final_{timestamp_id}.mp4")
                await self._manual_merge(v_path, a_path, output_path)
                final_file_path = output_path
                
            except Exception as e_split:
                self.logger.warning(f"分流下载失败，尝试单文件回退模式: {e_split}")
                # 如果分流失败（比如B站这种只有单文件的），回退到直接下载 best
                fallback_tmpl = f"{self.temp_dir}/f_{timestamp_id}_%(id)s.%(ext)s"
                f_path, f_info = await self._download_stream(url, fmt_fallback, fallback_tmpl)
                video_title = f_info.get('title', 'video')
                final_file_path = f_path

            # === 第四步：发送 ===
            if not final_file_path or not os.path.exists(final_file_path):
                raise Exception("文件生成失败")

            file_size_mb = os.path.getsize(final_file_path) / (1024 * 1024)
            if file_size_mb > self.max_size_mb:
                yield event.plain_result(f"❌ 文件过大 ({file_size_mb:.2f}MB)")
                return

            yield event.plain_result(f"✅ 完成 ({file_size_mb:.2f}MB)，上传中...")
            
            abs_path = os.path.abspath(final_file_path)
            
            if send_mode == "file":
                safe_title = self._sanitize_filename(video_title)
                display_name = f"{safe_title}.mp4"
                yield event.chain_result([File(file=abs_path, name=display_name)])
            else:
                yield event.chain_result([Video.fromFileSystem(path=abs_path)])

            # === 清理 ===
            async def _cleanup():
                await asyncio.sleep(self.delete_seconds + 30)
                try: 
                    if os.path.exists(abs_path): os.remove(abs_path) 
                except: pass
                for f in temp_files_to_clean:
                    try: 
                        if os.path.exists(f): os.remove(f)
                    except: pass
            asyncio.create_task(_cleanup())

        except Exception as e:
            self.logger.error(f"Error: {e}", exc_info=True)
            yield event.plain_result(f"❌ 错误: {str(e)[:100]}")

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        async for result in self._core_download_handler(event, url, send_mode="file"):
            yield result

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        async for result in self._core_download_handler(event, url, send_mode="video"):
            yield result
            
    @command("extract")
    async def cmd_extract_url(self, event: AstrMessageEvent, url: str = ""):
        if not url: return
        ydl_opts = {"quiet": True}
        if self.proxy_enabled: ydl_opts["proxy"] = self.proxy_url
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False))
            yield event.plain_result(f"✅ {info.get('title')}\n🔗 {info.get('url')}")
        except Exception as e:
            yield event.plain_result(f"❌ {e}")