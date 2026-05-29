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

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "3.5.1-DebugMode")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.config = config

        # --- 调试模式 ---
        self.debug_mode = self.config.get("advanced", {}).get("debug", False)
        # -----------------

        self.logger.info("🔥 加载最高画质版 (v3.5.1-DebugMode)...")
        self._debug_log("初始化", f"config keys: {list(self.config.keys())}")

        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
            self._debug_log("初始化", f"创建临时目录: {self.temp_dir}")
        else:
            self._debug_log("初始化", f"临时目录已存在: {self.temp_dir}")

        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            self._debug_log("初始化", f"imageio_ffmpeg 找到 FFmpeg: {self.ffmpeg_exe}")
        except Exception as e:
            self.ffmpeg_exe = "ffmpeg"
            self._debug_log("初始化", f"imageio_ffmpeg 未找到, 回退到系统 ffmpeg: {e}")

        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        # 默认改为最高画质
        self.max_quality = self.config.get("download", {}).get("max_quality", "最高画质")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 100)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        self.prefer_h264 = self.config.get("download", {}).get("prefer_h264", True)

        self._debug_log("初始化",
            f"proxy={self.proxy_enabled}({self.proxy_url}), "
            f"quality={self.max_quality}, max_size={self.max_size_mb}MB, "
            f"h264={self.prefer_h264}, delete_after={self.delete_seconds}s")

        self.server_port = 0
        self.server_ip = self._get_local_ip()
        self._debug_log("初始化", f"本机IP: {self.server_ip}")
        self._start_http_server()
        self.logger.info(f"文件服务器: http://{self.server_ip}:{self.server_port}")
        self.logger.info(f"画质设置: {self.max_quality} | H.264优先: {self.prefer_h264}")
        self._debug_log("初始化", f"HTTP服务器已启动, 端口: {self.server_port}")

    # ==================== 调试日志 ====================
    def _debug_log(self, step: str, msg: str):
        """仅在 debug_mode=True 时输出详细日志"""
        if self.debug_mode:
            self.logger.info(f"[DEBUG][{step}] {msg}")

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
        self._debug_log("更新yt-dlp", f"使用解释器: {sys.executable}")
        def _run_update():
            try:
                # 使用当前python解释器调用pip更新
                cmd = [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"]
                res = subprocess.run(cmd, capture_output=True, text=True)
                self._debug_log("更新yt-dlp", f"pip stdout: {res.stdout[:200]}")
                if res.stderr:
                    self._debug_log("更新yt-dlp", f"pip stderr: {res.stderr[:200]}")
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
        self._debug_log("合并", f"视频: {v}, 音频: {a}, 输出: {out}")
        cmd = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "copy", "-y", out]
        self._debug_log("合并", f"ffmpeg命令(尝试1): {' '.join(cmd)}")
        def _run():
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=startupinfo)

        res = await asyncio.get_running_loop().run_in_executor(None, _run)
        if res.returncode != 0:
            self._debug_log("合并", f"第一次合并失败 (code={res.returncode}): {res.stderr[:300]}")
            cmd_re = [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "aac", "-y", out]
            self._debug_log("合并", f"重试命令(尝试2 AAC): {' '.join(cmd_re)}")
            res = await asyncio.get_running_loop().run_in_executor(
                None, lambda: subprocess.run(cmd_re, capture_output=True))
            if res.returncode != 0:
                self._debug_log("合并", f"第二次也失败 (code={res.returncode}): {res.stderr[:300]}")
                raise Exception("合并失败")
        self._debug_log("合并", f"合并成功 -> {out}")

    async def _get_video_info_safe(self, url):
        self._debug_log("解析信息", f"开始解析 URL: {url}")
        # extract_flat=True 加快列表解析速度
        opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extract_flat": "in_playlist"
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
            self._debug_log("解析信息", f"使用代理: {self.proxy_url}")
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))

            # 判断是否为列表
            if info.get('_type') == 'playlist':
                count = info.get('playlist_count', len(info.get('entries', [])))
                self._debug_log("解析信息", f"检测到播放列表: '{info.get('title', '?')}', 共 {count} 个")
                return {
                    'is_playlist': True,
                    'title': info.get('title', 'Playlist'),
                    'count': count,
                    'entries': info.get('entries', [])
                }

            sz = info.get('filesize') or info.get('filesize_approx')
            self._debug_log("解析信息",
                f"单视频: '{info.get('title', '?')}', 大小: {self._format_size(sz)}")
            return {'is_playlist': False, 'title': info.get('title', ''), 'filesize': sz}
        except Exception as e:
            self.logger.error(f"Info error: {e}")
            self._debug_log("解析信息", f"解析失败: {e}")
            return None

    async def _download_stream(self, url, fmt, tmpl):
        self._debug_log("下载流", f"URL: {url[:60]}..., fmt: {fmt}, tmpl: {tmpl}")
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
                filename = ydl.prepare_filename(info)
                self._debug_log("下载流", f"下载完成: {filename}")
                return filename, info
        return await asyncio.get_running_loop().run_in_executor(None, _task)

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, method: str, ctype: str):
        if not url:
            self._debug_log("核心处理", "空URL, 直接返回")
            return

        self._debug_log("核心处理", f"开始处理: url={url[:80]}..., method={method}, ctype={ctype}")

        # 1. 检查是否包含确认参数
        confirmed = False
        if "--y" in url:
            url = url.replace("--y", "").replace("  ", " ").strip()
            confirmed = True
            self._debug_log("核心处理", "检测到 --y 确认标记")

        yield event.plain_result(f"⏳ 正在解析资源信息...")
        info = await self._get_video_info_safe(url)

        if not info:
            self._debug_log("核心处理", "解析返回空, 终止")
            yield event.plain_result(f"❌ 无法解析链接，请检查网络或链接有效性。")
            return

        ts = int(time.time())
        final_password = None # 用于最后提示密码

        # ==================== 播放列表逻辑 ====================
        if info.get('is_playlist'):
            count = info['count']
            title = info['title']
            self._debug_log("核心处理", f"进入播放列表分支: count={count}, title={title}")

            # 交互确认机制
            if not confirmed:
                self._debug_log("核心处理", "播放列表未确认, 提示用户")
                yield event.plain_result(
                    f"📂 检测到播放列表:【{title}】\n"
                    f"🔢 包含视频数: {count} 个\n\n"
                    f"⚠️ 为防止炸服，请确认是否下载并打包（加密）？\n"
                    f"✅ 确认下载请回复:\n/download {url} --y"
                )
                return

            if count > 30: # 阈值可自己改
                self._debug_log("核心处理", f"播放列表超过限制: {count} > 30")
                yield event.plain_result(f"❌ 视频数量 ({count}) 超过单次限制 (30)。")
                return

            yield event.plain_result(f"📦 开始下载播放列表 ({count}个)... 请耐心等待。")
            self._debug_log("核心处理", f"开始播放列表下载, ts={ts}")

            playlist_folder = os.path.join(self.temp_dir, f"pl_{ts}")
            if not os.path.exists(playlist_folder):
                os.makedirs(playlist_folder)
                self._debug_log("核心处理", f"创建播放列表目录: {playlist_folder}")

            # 下载列表
            playlist_tmpl = f"{playlist_folder}/%(playlist_index)s_%(title)s.%(ext)s"
            # 列表建议限制画质以减小体积
            fmt_v = "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
            self._debug_log("核心处理", f"播放列表格式: {fmt_v}")

            opts = {
                "outtmpl": playlist_tmpl,
                "format": fmt_v,
                "quiet": True,
                "ignoreerrors": True,
                "noplaylist": False,
            }
            if self.proxy_enabled: opts["proxy"] = self.proxy_url

            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: yt_dlp.YoutubeDL(opts).download([url]))
                self._debug_log("核心处理", "播放列表下载完成")
            except Exception as e:
                self._debug_log("核心处理", f"播放列表下载异常: {e}")
                yield event.plain_result(f"⚠️ 下载部分出错: {e}")

            files = glob.glob(os.path.join(playlist_folder, "*"))
            self._debug_log("核心处理", f"播放列表文件数: {len(files)}")
            if not files:
                yield event.plain_result("❌ 列表下载失败，无文件。")
                shutil.rmtree(playlist_folder)
                return

            # ========== 加密打包逻辑 ==========
            yield event.plain_result(f"🔐 正在加密打包 {len(files)} 个文件 (密码: 123456)...")
            self._debug_log("核心处理", f"开始加密打包, {len(files)} 个文件")

            # 尝试导入 pyzipper，如果没有则自动安装
            try:
                import pyzipper
                self._debug_log("核心处理", "pyzipper 已安装")
            except ImportError:
                self.logger.info("未找到 pyzipper，正在自动安装...")
                self._debug_log("核心处理", "pyzipper 未安装, 自动安装中...")
                yield event.plain_result("⚙️ 首次运行正在安装加密依赖库...")
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: subprocess.run([sys.executable, "-m", "pip", "install", "pyzipper"], capture_output=True)
                )
                import pyzipper # 安装后再次导入
                self._debug_log("核心处理", "pyzipper 安装完成")

            # 文件名加上 pwd 提示
            zip_name = f"Playlist_{self._sanitize_filename(title)}_Pwd123456.zip"
            zip_path = os.path.join(self.temp_dir, zip_name)

            def _do_encrypted_zip():
                # 使用 AES 加密
                with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                    zf.setpassword(b"123456") # 设置二进制密码
                    for f in files:
                        zf.write(f, os.path.basename(f))

            await asyncio.get_running_loop().run_in_executor(None, _do_encrypted_zip)
            self._debug_log("核心处理", f"加密ZIP已创建: {zip_path}")

            shutil.rmtree(playlist_folder) # 清理源文件
            self._debug_log("核心处理", f"清理播放列表目录: {playlist_folder}")
            final_path = zip_path
            video_title_real = f"Playlist_{title}"
            method = "file" # 强制转为文件发送
            final_password = "123456"

        # ==================== 单视频逻辑 ====================
        else:
            self._debug_log("核心处理", "进入单视频分支")
            yield event.plain_result(f"📹 {info['title'][:30]}...\n⏳ 开始下载...")

            v_tmpl = f"{self.temp_dir}/v_{ts}_%(id)s.%(ext)s"
            a_tmpl = f"{self.temp_dir}/a_{ts}_%(id)s.%(ext)s"

            # 画质逻辑
            limit = self.max_quality
            prefer_h264 = self.prefer_h264
            if limit == "最高画质":
                fmt_v = "bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo" if prefer_h264 else "bestvideo"
            else:
                height = int(limit.replace('p', ''))
                fmt_v = f"bestvideo[height<={height}][vcodec^=avc1]" if prefer_h264 else f"bestvideo[height<={height}]"
            fmt_a = "bestaudio[ext=m4a]/bestaudio"

            self._debug_log("核心处理",
                f"画质限制: {limit}, h264优先: {prefer_h264}, "
                f"fmt_v: {fmt_v}, fmt_a: {fmt_a}")

            try:
                if ctype == "audio_only":
                    self._debug_log("核心处理", "仅音频模式")
                    final_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    video_title_real = a_info.get('title', 'audio')
                    temp_files = [final_path]
                else:
                    self._debug_log("核心处理", "开始下载视频流...")
                    v_path, v_info = await self._download_stream(url, fmt_v, v_tmpl)
                    video_title_real = v_info.get('title', 'video')
                    self._debug_log("核心处理", f"视频流下载完成: {v_path}")

                    self._debug_log("核心处理", "开始下载音频流...")
                    a_path, a_info = await self._download_stream(url, fmt_a, a_tmpl)
                    self._debug_log("核心处理", f"音频流下载完成: {a_path}")

                    yield event.plain_result(f"⚙️ 合并中...")
                    out_path = os.path.join(self.temp_dir, f"final_{ts}.mp4")
                    await self._manual_merge(v_path, a_path, out_path)
                    final_path = out_path
                    temp_files = [v_path, a_path]
                    self._debug_log("核心处理", f"合并输出: {final_path}")
            except Exception as e:
                # 自动检测 yt-dlp 更新逻辑
                err_str = str(e).lower()
                self._debug_log("核心处理", f"下载/合并异常: {e}")
                yield event.plain_result(f"❌ 错误: {e}")
                updated, log = await self._try_update_ytdlp()
                if updated:
                    yield event.plain_result(f"✅ 核心组件已自动更新，请重启机器人后重试。")
                return

        # ==================== 统一上传逻辑 ====================
        if not final_path or not os.path.exists(final_path):
            self._debug_log("核心处理", f"最终文件不存在: {final_path}")
            yield event.plain_result("❌ 文件生成失败。")
            return

        fsize_mb = os.path.getsize(final_path) / (1024 * 1024)
        self._debug_log("核心处理", f"最终文件: {final_path}, 大小: {fsize_mb:.1f}MB")

        # 播放列表通常允许更大一点的体积 (500MB)，单视频跟随配置
        max_limit = 500 if info.get('is_playlist') else self.max_size_mb
        self._debug_log("核心处理", f"大小限制: {max_limit}MB, 实际: {fsize_mb:.1f}MB")

        # 构造密码提示文本
        pwd_hint = f"\n🔐 **解压密码: {final_password}**" if final_password else ""

        if fsize_mb > max_limit:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            self._debug_log("核心处理", f"文件过大, 提供直链: {furl}")
            yield event.plain_result(
                f"⚠️ 文件过大 ({fsize_mb:.1f}MB)，无法直接发送。\n"
                f"🔗 直链下载: {furl}\n"
                f"{pwd_hint}\n"
                f"⏳ 有效期 {self.delete_seconds} 秒"
            )
        else:
            fname_disk = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fname_disk}"
            safe_title = self._sanitize_filename(video_title_real)
            ext = os.path.splitext(final_path)[1]
            display_name = f"{safe_title}{ext}"

            # 如果是加密包，强制在文件名里也写上密码，防止用户忘
            if final_password and "Pwd" not in display_name:
                display_name = f"Pwd{final_password}_{display_name}"

            if method == "file":
                self._debug_log("核心处理", f"文件上传模式, display_name={display_name}")
                yield event.plain_result(f"⬆️ 正在上传 ({fsize_mb:.1f}MB)...{pwd_hint}")
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

                self._debug_log("核心处理", f"上传目标: is_group={is_group}, tid={tid}")

                if tid:
                    act = "upload_group_file" if is_group else "upload_private_file"
                    key = "group_id" if is_group else "user_id"
                    try:
                        await event.bot.call_action(act, **{key: int(tid), "file": furl, "name": display_name})
                        self._debug_log("核心处理", "上传成功")
                    except Exception as upload_err:
                        self._debug_log("核心处理", f"上传失败: {upload_err}")
                        yield event.plain_result(f"❌ 上传超时或失败: {upload_err}\n🔗 请使用直链: {furl}{pwd_hint}")
                else:
                    self._debug_log("核心处理", "无tid, 提供直链")
                    yield event.plain_result(f"🔗 直链: {furl}{pwd_hint}")
            else:
                self._debug_log("核心处理", f"Video消息模式, furl={furl}")
                yield event.chain_result([Video(file=furl, url=furl)])

        # 清理任务
        async def _clean():
            wait_time = 120 if info.get('is_playlist') else self.delete_seconds + 30
            self._debug_log("清理", f"将在 {wait_time}s 后清理文件")
            await asyncio.sleep(wait_time)
            if os.path.exists(final_path):
                os.remove(final_path)
                self._debug_log("清理", f"已删除: {final_path}")
            if 'temp_files' in locals():
                for f in temp_files:
                    if os.path.exists(f):
                        os.remove(f)
                        self._debug_log("清理", f"已删除临时文件: {f}")
        asyncio.create_task(_clean())

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str
        self._debug_log("命令/download", f"raw={raw[:80]}")
        # 兼容多种格式: /download, download, 或直接url
        full_url = url
        for prefix in ["/download ", "download "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        # 如果url参数里没有--y，但原始消息有，补上
        if "--y" not in full_url and "--y" in raw:
            full_url = full_url + " --y"
        self._debug_log("命令/download", f"full_url={full_url[:80]}")
        async for res in self._core_download_handler(event, full_url, "file", "merged"):
            yield res

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str
        self._debug_log("命令/video", f"raw={raw[:80]}")
        full_url = url
        for prefix in ["/video ", "video "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        if "--y" not in full_url and "--y" in raw:
            full_url = full_url + " --y"
        self._debug_log("命令/video", f"full_url={full_url[:80]}")
        async for res in self._core_download_handler(event, full_url, "video", "merged"):
            yield res

    @command("直链")
    async def cmd_get_direct_url(self, event: AstrMessageEvent, url: str = ""):
        """提取视频直链，不下载"""
        raw = event.message_str
        self._debug_log("命令/直链", f"raw={raw[:80]}")
        full_url = url
        for prefix in ["/直链 ", "直链 "]:
            if prefix in raw:
                full_url = raw.split(prefix, 1)[1].strip()
                break
        if not full_url:
            self._debug_log("命令/直链", "空URL")
            yield event.plain_result("❌ 请提供视频链接，例如: /直链 https://www.youtube.com/watch?v=xxx")
            return

        self._debug_log("命令/直链", f"解析: {full_url}")
        yield event.plain_result("⏳ 正在解析直链，请稍候...")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "skip_download": True,
        }
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
            self._debug_log("命令/直链", f"使用代理: {self.proxy_url}")

        try:
            def _extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(full_url, download=False)

            info = await asyncio.get_running_loop().run_in_executor(None, _extract)
            self._debug_log("命令/直链", f"提取成功, formats数: {len(info.get('formats', []))}")
        except Exception as e:
            self._debug_log("命令/直链", f"提取失败: {e}")
            yield event.plain_result(f"❌ 解析失败: {e}")
            return

        if not info:
            self._debug_log("命令/直链", "info为空")
            yield event.plain_result("❌ 无法获取视频信息。")
            return

        title = info.get("title", "未知标题")
        duration = info.get("duration")
        dur_str = f"{int(duration)//60}:{int(duration)%60:02d}" if duration else "未知"

        # 如果有直接可用的 url（合并格式）
        direct_url = info.get("url")

        # 收集所有 format 中的直链
        formats = info.get("formats", [])

        # 找最佳合并流（同时含视频和音频的格式）
        best_combined = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec != "none":
                best_combined = f

        # 找最佳纯视频流
        best_video = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec != "none" and acodec == "none":
                best_video = f

        # 找最佳纯音频流
        best_audio = None
        for f in formats:
            vcodec = f.get("vcodec", "none")
            acodec = f.get("acodec", "none")
            if vcodec == "none" and acodec != "none":
                best_audio = f

        self._debug_log("命令/直链",
            f"combined={'found' if best_combined else 'none'}, "
            f"video={'found' if best_video else 'none'}, "
            f"audio={'found' if best_audio else 'none'}")

        lines = []
        lines.append(f"🎬 标题: {title}")
        lines.append(f"⏱ 时长: {dur_str}")
        lines.append("")

        if best_combined and best_combined.get("url"):
            res_h = best_combined.get("height", "?")
            res_w = best_combined.get("width", "?")
            ext = best_combined.get("ext", "?")
            fsize = self._format_size(best_combined.get("filesize") or best_combined.get("filesize_approx"))
            lines.append(f"✅ 最佳合并流 ({res_w}x{res_h}, {ext}, {fsize}):")
            lines.append(best_combined["url"])
        elif direct_url:
            lines.append(f"✅ 直链:")
            lines.append(direct_url)
        else:
            lines.append("⚠️ 无合并流直链")

        lines.append("")

        if best_video and best_video.get("url"):
            res_h = best_video.get("height", "?")
            res_w = best_video.get("width", "?")
            ext = best_video.get("ext", "?")
            vcodec = best_video.get("vcodec", "?")
            fsize = self._format_size(best_video.get("filesize") or best_video.get("filesize_approx"))
            lines.append(f"🎥 最佳视频流 ({res_w}x{res_h}, {vcodec}, {ext}, {fsize}):")
            lines.append(best_video["url"])
        else:
            lines.append("⚠️ 无单独视频流直链")

        lines.append("")

        if best_audio and best_audio.get("url"):
            acodec = best_audio.get("acodec", "?")
            ext = best_audio.get("ext", "?")
            fsize = self._format_size(best_audio.get("filesize") or best_audio.get("filesize_approx"))
            lines.append(f"🎵 最佳音频流 ({acodec}, {ext}, {fsize}):")
            lines.append(best_audio["url"])
        else:
            lines.append("⚠️ 无单独音频流直链")

        lines.append("")
        lines.append("⚠️ 直链有时效性，请尽快使用。")

        yield event.plain_result("\n".join(lines))
