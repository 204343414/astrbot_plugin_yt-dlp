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

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "3.3.0-AegisubFix")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.logger.info("🔥 正在加载 Aegisub 兼容版 (v3.3)...") 
        self.config = config
        
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir): os.makedirs(self.temp_dir)
            
        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except:
            self.ffmpeg_exe = "ffmpeg"
            
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        
        # === 关键配置 ===
        self.max_quality = self.config.get("download", {}).get("max_quality", "720p")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 100)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        # 读取是否强制 H.264 (默认开启，为了兼容性)
        self.prefer_h264 = self.config.get("download", {}).get("prefer_h264", True)
        
        self.server_port = 0 
        self.server_ip = self._get_local_ip()
        self._start_http_server()
        self.logger.info(f"文件服务器: http://{self.server_ip}:{self.server_port}")

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except: return "127.0.0.1"

    def _start_http_server(self):
        class TempDirHandler(SimpleHTTPRequestHandler):
            def __init__(handler_self, *args, **kwargs):
                super().__init__(*args, directory=self.temp_dir, **kwargs)
            def log_message(self, format, *args): pass

        def run_server():
            server = HTTPServer(('0.0.0.0', 0), TempDirHandler)
            self.server_port = server.server_port
            server.serve_forever()

        t = threading.Thread(target=run_server, daemon=True)
        t.start()
        time.sleep(0.5)

    def _sanitize_filename(self, name: str) -> str:
        if not name: return "video"
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        return name.replace('\n', ' ').replace('\r', '')[:50].strip()

    def _format_size(self, size_bytes):
        if size_bytes is None: return "未知"
        if size_bytes < 1024: return f"{size_bytes} B"
        elif size_bytes < 1024**2: return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024**3: return f"{size_bytes/1024**2:.2f} MB"
        else: return f"{size_bytes/1024**3:.2f} GB"

    async def _manual_merge(self, v, a, out):
        # 使用 copy 模式无损合并
        cmd = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "copy", "-y", out]
        def _run():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
        
        res = await asyncio.get_running_loop().run_in_executor(None, _run)
        if res.returncode != 0:
            # 兼容性回退：如果 copy 失败，尝试重编码 (确保能出片)
            cmd_re = [self.ffmpeg_exe, "-i", v, "-i", a, "-q:v", "2", "-y", out]
            res = await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(cmd_re, capture_output=True))
            if res.returncode != 0: raise Exception("合并失败")

    async def _get_video_info_safe(self, url):
        opts = {"quiet":True, "no_warnings":True, "nocheckcertificate":True}
        if self.proxy_enabled: opts["proxy"] = self.proxy_url
        try:
            info = await asyncio.get_running_loop().run_in_executor(None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))
            sz = info.get('filesize') or info.get('filesize_approx')
            return {'title':info.get('title',''), 'duration':info.get('duration'), 'filesize':sz}
        except: return None

    async def _download_stream(self, url, fmt, tmpl):
        opts = {"outtmpl":tmpl, "format":fmt, "noplaylist":True, "quiet":True, "ffmpeg_location":None}
        if self.proxy_enabled: opts["proxy"] = self.proxy_url
        def _task():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info
        return await asyncio.get_running_loop().run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, method: str, ctype: str):
        if not url: return
        yield event.plain_result(f"⏳ 获取信息中...")
        info = await self._get_video_info_safe(url)
        if info: yield event.plain_result(f"📹 {info['title'][:20]}...\n📦 {self._format_size(info['filesize'])}\n⏳ 下载中...")
        
        ts = int(time.time())
        v_tmpl = f"{self.temp_dir}/v_{ts}_%(id)s.%(ext)s"
        a_tmpl = f"{self.temp_dir}/a_{ts}_%(id)s.%(ext)s"
        
        # ========== 核心：画质与编码选择 ==========
        limit = self.max_quality
        prefer_h264 = self.prefer_h264
        
        # 1. 确定编码过滤器
        # vcodec^=avc1 代表 H.264
        codec_filter = "[vcodec^=avc1]" if prefer_h264 else ""
        
        # 2. 确定高度过滤器
        if limit == "最高画质":
            height_filter = "" 
        else:
            h = int(limit.replace("p", ""))
            height_filter = f"[height<={h}]"
            
        # 3. 组合 format 字符串
        # 逻辑：优先下载满足 (H.264 + 限制高度) 的 mp4
        # 如果没有(比如只要H.264但没那个分辨率)，则回退到 (H.264 + 任意高度)
        # 再没有，才回退到 bestvideo (VP9/AV1)
        if prefer_h264:
            self.logger.info(f"模式: {limit} | 强制 H.264")
            fmt_v = f"bestvideo[ext=mp4]{codec_filter}{height_filter}/bestvideo[ext=mp4]{codec_filter}/bestvideo{height_filter}"
        else:
            self.logger.info(f"模式: {limit} | 编码不限")
            fmt_v = f"bestvideo[ext=mp4]{height_filter}/bestvideo{height_filter}"

        fmt_a = "bestaudio[ext=m4a]/bestaudio"

        try:
            final_path = None
            temp_files = []

            if ctype == "audio_only":
                final_path, _ = await self._download_stream(url, fmt_a, a_tmpl)
            else:
                v_path, v_info = await self._download_stream(url, fmt_v, v_tmpl)
                temp_files.append(v_path)
                a_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                temp_files.append(a_path)
                
                yield event.plain_result("⚙️ 正在无损合并...")
                out_path = os.path.join(self.temp_dir, f"final_{ts}.mp4")
                await self._manual_merge(v_path, a_path, out_path)
                final_path = out_path

            if not final_path or not os.path.exists(final_path): raise Exception("文件生成失败")
            
            fsize_mb = os.path.getsize(final_path) / (1024 * 1024)
            if fsize_mb > self.max_size_mb:
                 yield event.plain_result(f"❌ 文件过大 ({fsize_mb:.1f}MB)，已停止。")
            else:
                fname = os.path.basename(final_path)
                furl = f"http://{self.server_ip}:{self.server_port}/{fname}"
                
                if method == "file":
                    # ID 获取逻辑
                    tid = None
                    is_group = False
                    if hasattr(event, 'message_obj'):
                        msg = event.message_obj
                        if getattr(msg, 'group_id', None):
                            is_group = True
                            tid = msg.group_id
                        elif getattr(msg, 'user_id', None):
                            tid = msg.user_id
                    if not tid: tid = event.session_id
                    
                    if tid:
                        act = "upload_group_file" if is_group else "upload_private_file"
                        key = "group_id" if is_group else "user_id"
                        self.logger.info(f"API调用: {act} -> {tid}")
                        await event.bot.call_action(act, **{key: int(tid), "file": furl, "name": fname})
                    else:
                        yield event.plain_result("❌ 无法获取目标ID")
                else:
                    yield event.chain_result([Video(file=furl, url=furl)])
            
            async def _clean():
                await asyncio.sleep(self.delete_seconds+20)
                if os.path.exists(final_path): os.remove(final_path)
                for f in temp_files:
                    if os.path.exists(f): os.remove(f)
            asyncio.create_task(_clean())

        except Exception as e:
            self.logger.error(f"Err: {e}")
            yield event.plain_result(f"❌ 错误: {e}")

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        async for res in self._core_download_handler(event, url, "file", "merged"): yield res

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        async for res in self._core_download_handler(event, url, "video", "merged"): yield res
EOF
