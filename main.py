import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
import sys  # <--- 新增这行，用于调用当前环境的pip
import imageio_ffmpeg
import shutil
import zipfile
import socket
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "3.5.0-MaxQuality")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.logger.info("🔥 加载最高画质版 (v3.5)...")
        self.config = config
        
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            
        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except:
            self.ffmpeg_exe = "ffmpeg"
            
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        # 默认改为最高画质
        self.max_quality = self.config.get("download", {}).get("max_quality", "最高画质")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 100)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        self.prefer_h264 = self.config.get("download", {}).get("prefer_h264", True)
        
        self.server_port = 0
        self.server_ip = self._get_local_ip()
        self._start_http_server()
        self.logger.info(f"文件服务器: http://{self.server_ip}:{self.server_port}")
        self.logger.info(f"画质设置: {self.max_quality} | H.264优先: {self.prefer_h264}")

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
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

    def _sanitize_filename(self, name: str) -> str:
        if not name:
            return "video"
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        return name.replace('\n', ' ').replace('\r', '')[:100].strip()

    def _format_size(self, size_bytes):
        if size_bytes is None:
            return "未知"
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024**2:
            return f"{size_bytes/1024:.2f} KB"
        elif size_bytes < 1024**3:
            return f"{size_bytes/1024**2:.2f} MB"
        else:
            return f"{size_bytes/1024**3:.2f} GB"
    async def _try_update_ytdlp(self):
        self.logger.info("正在尝试自动更新 yt-dlp...")
        def _run_update():
            try:
                # 使用当前python解释器调用pip更新
                cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
                res = subprocess.run(cmd, capture_output=True, text=True)
                # 检查输出中是否有更新成功的关键词
                if "Successfully installed" in res.stdout:
                    return True, res.stdout
                elif "Requirement already satisfied" in res.stdout:
                    return False, "Already latest"
                return False, res.stderr
            except Exception as e:
                return False, str(e)
        
        return await asyncio.get_running_loop().run_in_executor(None, _run_update)
    async def _manual_merge(self, v, a, out):
        cmd = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "copy", "-y", out]
        def _run():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)
        
        res = await asyncio.get_running_loop().run_in_executor(None, _run)
        if res.returncode != 0:
            cmd_re = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "aac", "-y", out]
            res = await asyncio.get_running_loop().run_in_executor(None, lambda: subprocess.run(cmd_re, capture_output=True))
            if res.returncode != 0:
                raise Exception("合并失败")

    async def _get_video_info_safe(self, url):
        # extract_flat=True 加快列表解析速度
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extract_flat": "in_playlist" 
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))
            
            # 判断是否为列表
            if info.get('_type') == 'playlist':
                return {
                    'is_playlist': True,
                    'title': info.get('title', 'Playlist'),
                    'count': info.get('playlist_count', len(info.get('entries', []))),
                    'entries': info.get('entries', [])
                }
            
            sz = info.get('filesize') or info.get('filesize_approx')
            return {'is_playlist': False, 'title': info.get('title', ''), 'filesize': sz}
        except Exception as e:
            self.logger.error(f"Info error: {e}")
            return None

    async def _download_stream(self, url, fmt, tmpl):
        opts = {
            "outtmpl": tmpl,
            "format": fmt,
            "noplaylist": True,
            "quiet": True,
            "ffmpeg_location": None
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        def _task():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return ydl.prepare_filename(info), info
        return await asyncio.get_running_loop().run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, method: str, ctype: str):
        if not url:
            return
        
        # 1. 检查是否包含确认参数
        confirmed = False
        if " --y" in url:
            url = url.replace(" --y", "").strip()
            confirmed = True
            
        yield event.plain_result(f"⏳ 正在解析资源信息...")
        info = await self._get_video_info_safe(url)
        
        if not info:
            yield event.plain_result(f"❌ 无法解析链接，请检查网络或链接有效性。")
            return

        ts = int(time.time())

        # ==================== 播放列表逻辑 ====================
        if info.get('is_playlist'):
            count = info['count']
            title = info['title']
            
            # 交互确认机制
            if not confirmed:
                yield event.plain_result(
                    f"📂 检测到播放列表:【{title}】\n"
                    f"🔢 包含视频数: {count} 个\n\n"
                    f"⚠️ 为防止服务器过载，请确认是否下载并打包？\n"
                    f"✅ 确认下载请回复:\n/download {url} --y"
                )
                return

            if count > 20: # 安全阈值，防止炸服
                yield event.plain_result(f"❌ 视频数量 ({count}) 超过单次限制 (20)，请分批下载。")
                return

            yield event.plain_result(f"📦 开始处理播放列表 ({count}个)... 可能会花费较长时间，请耐心等待。")
            
            # 创建临时文件夹用于存放本组视频
            playlist_folder = os.path.join(self.temp_dir, f"pl_{ts}")
            if not os.path.exists(playlist_folder):
                os.makedirs(playlist_folder)

            downloaded_files = []
            
            # 循环下载列表中的每个视频
            # 注意：这里我们简化逻辑，直接调用 yt-dlp 下载整个列表到指定文件夹
            playlist_tmpl = f"{playlist_folder}/%(playlist_index)s_%(title)s.%(ext)s"
            
            limit = self.max_quality
            # 列表下载通常不建议用最高画质，容易太大，这里锁定为 1080p 或 720p 以保证成功率，或者跟随设置
            fmt_v = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            
            opts = {
                "outtmpl": playlist_tmpl,
                "format": fmt_v,
                "quiet": True,
                "ignoreerrors": True, # 忽略单个下载失败
                "noplaylist": False,  # 允许列表
            }
            if self.proxy_enabled: opts["proxy"] = self.proxy_url

            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: yt_dlp.YoutubeDL(opts).download([url]))
            except Exception as e:
                yield event.plain_result(f"⚠️ 下载过程中出现部分错误: {e}")

            # 统计下载好的文件
            files = glob.glob(os.path.join(playlist_folder, "*"))
            if not files:
                yield event.plain_result("❌ 播放列表下载失败，未能获取任何文件。")
                shutil.rmtree(playlist_folder)
                return

            # 打包 ZIP
            yield event.plain_result(f"🗜️ 正在将 {len(files)} 个视频打包为 ZIP...")
            zip_path = os.path.join(self.temp_dir, f"Playlist_{ts}.zip")
            
            def _do_zip():
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for f in files:
                        zf.write(f, os.path.basename(f))
            
            await asyncio.get_running_loop().run_in_executor(None, _do_zip)
            
            # 清理视频散文件，只留 ZIP
            shutil.rmtree(playlist_folder)
            
            final_path = zip_path
            video_title_real = f"Playlist_{title}"
            # 标记为文件上传
            method = "file" 

        # ==================== 单视频逻辑 (原有逻辑优化) ====================
        else:
            yield event.plain_result(f"📹 {info['title'][:30]}...\n⏳ 开始下载...")
            
            v_tmpl = f"{self.temp_dir}/v_{ts}_%(id)s.%(ext)s"
            a_tmpl = f"{self.temp_dir}/a_{ts}_%(id)s.%(ext)s"
            
            # ... (保留原有的画质选择逻辑，此处为节省篇幅简略，请确保你的代码里有 fmt_v 定义) ...
            limit = self.max_quality
            prefer_h264 = self.prefer_h264
            if limit == "最高画质":
                fmt_v = "bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo" if prefer_h264 else "bestvideo"
            else:
                height = int(limit.replace('p', ''))
                fmt_v = f"bestvideo[height<={height}][vcodec^=avc1]" if prefer_h264 else f"bestvideo[height<={height}]"
            fmt_a = "bestaudio[ext=m4a]/bestaudio"
            # ...

            try:
                if ctype == "audio_only":
                    final_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    video_title_real = a_info.get('title', 'audio')
                    temp_files = [final_path]
                else:
                    v_path, v_info = await self._download_stream(url, fmt_v, v_tmpl)
                    video_title_real = v_info.get('title', 'video')
                    a_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    
                    yield event.plain_result(f"⚙️ 合并音视频...")
                    out_path = os.path.join(self.temp_dir, f"final_{ts}.mp4")
                    await self._manual_merge(v_path, a_path, out_path)
                    final_path = out_path
                    temp_files = [v_path, a_path]
            except Exception as e:
                # 之前添加的自动更新检测代码放在这里
                err_str = str(e).lower()
                yield event.plain_result(f"❌ 错误: {e}")
                updated, log = await self._try_update_ytdlp()
                if updated:
                    yield event.plain_result(f"✅ 组件已自动更新，请重启机器人后重试。")
                return

        # ==================== 统一上传逻辑 ====================
        if not final_path or not os.path.exists(final_path):
            yield event.plain_result("❌ 文件生成失败。")
            return

        fsize_mb = os.path.getsize(final_path) / (1024 * 1024)
        
        # 增加 ZIP 大小警告
        max_upload_size = 500 if info.get('is_playlist') else self.max_size_mb
        
        if fsize_mb > max_upload_size:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            yield event.plain_result(f"⚠️ 文件过大 ({fsize_mb:.1f}MB)，无法直接通过聊天窗口发送。\n🔗 直链下载: {furl}\n⏳ 文件将在 {self.delete_seconds}秒后删除。")
        else:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            safe_title = self._sanitize_filename(video_title_real)
            ext = os.path.splitext(final_path)[1]
            display_name = f"{safe_title}{ext}"

            if method == "file":
                yield event.plain_result(f"⬆️ 正在上传 ({fsize_mb:.1f}MB)...")
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
                    try:
                        await event.bot.call_action(act, **{key: int(tid), "file": furl, "name": display_name})
                    except Exception as upload_err:
                        yield event.plain_result(f"❌ 上传失败 (可能是文件太大): {upload_err}\n🔗 请尝试直链: {furl}")
                else:
                    yield event.plain_result(f"🔗 直链: {furl}")
            else:
                yield event.chain_result([Video(file=furl, url=furl)])

        # 清理任务
        async def _clean():
            await asyncio.sleep(self.delete_seconds + 60) # 列表通常大，多留点时间
            if os.path.exists(final_path):
                os.remove(final_path)
            if 'temp_files' in locals():
                for f in temp_files:
                    if os.path.exists(f): os.remove(f)
        asyncio.create_task(_clean())

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        async for res in self._core_download_handler(event, url, "file", "merged"):
            yield res

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        async for res in self._core_download_handler(event, url, "video", "merged"):
            yield res
