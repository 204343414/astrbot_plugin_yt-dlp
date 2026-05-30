import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
import sys
import imageio_ffmpeg
import shutil
import zipfile
import socket
import threading
from http.server import SimpleHTTPRequestHandler, HTTPServer
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "3.5.5-Cookie")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.config = config

        self.debug_mode = self.config.get("advanced", {}).get("debug", False)
        self._debug_buffer = []

        self.logger.info("🔥 Cookie支持版 (v3.5.5)")
        self._dbg("初始化", f"debug={self.debug_mode}, keys={list(self.config.keys())}")

        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._dbg("初始化", f"temp_dir={self.temp_dir}")

        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        except:
            self.ffmpeg_exe = "ffmpeg"
        self._dbg("初始化", f"ffmpeg={self.ffmpeg_exe}")

        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        self.max_quality = self.config.get("download", {}).get("max_quality", "最高画质")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 100)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        self.prefer_h264 = self.config.get("download", {}).get("prefer_h264", True)

        # ---- Cookie ----
        raw_cookie = self.config.get("youtube", {}).get("cookies_path", "").strip()
        self.cookies_path = raw_cookie if (raw_cookie and os.path.isfile(raw_cookie)) else ""
        if raw_cookie and not self.cookies_path:
            self.logger.warning(f"Cookie 文件不存在, 已忽略: {raw_cookie}")

        self._dbg("初始化",
            f"proxy={self.proxy_enabled}({self.proxy_url}) quality={self.max_quality} "
            f"size={self.max_size_mb}MB h264={self.prefer_h264} "
            f"cookie={'✓' if self.cookies_path else '✗'}")

        self.server_port = 0
        self.server_ip = self._get_local_ip()
        self._start_http_server()
        self.logger.info(f"HTTP: http://{self.server_ip}:{self.server_port}")
        self._dbg("初始化", f"server=:{self.server_port}")

    # ======= Debug =======
    def _dbg(self, step, msg):
        if self.debug_mode:
            self.logger.info(f"[DEBUG][{step}] {msg}")
            self._debug_buffer.append(f"[{step}] {msg}")

    def _dbg_chat(self, event, msg):
        if self.debug_mode:
            return event.plain_result(f"🔍 {msg}")
        return None

    # ======= 基础工具 =======
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
        class H(SimpleHTTPRequestHandler):
            def __init__(s, *a, **kw):
                super().__init__(*a, directory=self.temp_dir, **kw)
            def log_message(self, *a): pass
        def _run():
            srv = HTTPServer(('0.0.0.0', 0), H)
            self.server_port = srv.server_port
            srv.serve_forever()
        threading.Thread(target=_run, daemon=True).start()
        time.sleep(0.5)

    def _sanitize_filename(self, name):
        if not name: return "video"
        return re.sub(r'[\\/*?:"<>|]', '_', name).replace('\n',' ').replace('\r','')[:100].strip()

    def _format_size(self, b):
        if b is None: return "未知"
        if b<1024: return f"{b} B"
        if b<1024**2: return f"{b/1024:.2f} KB"
        if b<1024**3: return f"{b/1024**2:.2f} MB"
        return f"{b/1024**3:.2f} GB"

    # ======= 注入 proxy + cookie 到 opts =======
    def _inject(self, opts):
        if self.proxy_enabled:
            opts["proxy"] = self.proxy_url
        if self.cookies_path:
            opts["cookiefile"] = self.cookies_path
        return opts

    # ======= 自动更新 (PEP 668 兼容) =======
    async def _try_update_ytdlp(self):
        """三级回退：stable → --break-system-packages → nightly (--pre)"""
        self.logger.info("尝试自动更新 yt-dlp...")
        def _run():
            try:
                def _pip(args, desc):
                    self._dbg("更新", f"尝试: {desc}")
                    r = subprocess.run(args, capture_output=True, text=True)
                    out = (r.stdout or "") + (r.stderr or "")
                    self._dbg("更新", f"{desc} -> {out[-200:]}")
                    installed = "Successfully installed" in (r.stdout or "")
                    satisfied = "Requirement already satisfied" in (r.stdout or "")
                    return r, installed, satisfied

                py = [sys.executable, "-m", "pip", "install", "-U"]

                # 第1步: stable
                r, installed, satisfied = _pip(py + ["yt-dlp"], "stable")
                if installed:
                    return True, r.stdout

                # 第2步: PEP 668
                if "externally-managed" in (r.stdout or "") + (r.stderr or ""):
                    r, installed, satisfied = _pip(
                        py + ["--break-system-packages", "yt-dlp"], "break-system")
                    if installed:
                        return True, r.stdout

                # 第3步: nightly (总是尝试, 因为 stable 可能没修已知bug)
                r, installed, satisfied = _pip(
                    py + ["--pre", "--break-system-packages", "yt-dlp[default]"], "nightly")
                if installed or satisfied:
                    return True, r.stdout or "nightly OK"

                # 如果 stable already satisfied 且 nightly 也 satisfied → 已是最新
                if satisfied:
                    return True, r.stdout or "已是最新"

                return False, r.stderr or r.stdout or "未知错误"
            except Exception as e:
                return False, str(e)
        return await asyncio.get_running_loop().run_in_executor(None, _run)

    # ======= FFmpeg 合并 =======
    async def _manual_merge(self, v, a, out):
        self._dbg("合并", f"{os.path.basename(v)} + {os.path.basename(a)}")
        def _ff(cmd):
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return subprocess.run(cmd, capture_output=True, text=True, startupinfo=si)
        r = await asyncio.get_running_loop().run_in_executor(None, _ff,
            [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "copy", "-y", out])
        if r.returncode != 0:
            self._dbg("合并", f"copy失败, 重试AAC: {r.stderr[:200]}")
            r = await asyncio.get_running_loop().run_in_executor(None, _ff,
                [self.ffmpeg_exe, "-i", v, "-i", a, "-c:v", "copy", "-c:a", "aac", "-y", out])
            if r.returncode != 0:
                raise Exception(f"合并失败: {r.stderr[:200]}")
        self._dbg("合并", f"✅ {os.path.basename(out)}")

    # ======= 解析视频信息 =======
    async def _get_video_info_safe(self, url):
        self._dbg("解析", f"URL: {url[:120]}")
        # 排查用：明确打印 cookie/proxy 状态
        self._dbg("解析", f"proxy={self.proxy_enabled} cookie={'✓ '+self.cookies_path if self.cookies_path else '✗ 未找到'}")
        opts = self._inject({
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extract_flat": "in_playlist",
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        })
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(url, download=False))
            if info.get('_type') == 'playlist':
                c = info.get('playlist_count', len(info.get('entries', [])))
                return {'success':True,'is_playlist':True,'title':info.get('title','?'),'count':c,'entries':info.get('entries',[])}
            sz = info.get('filesize') or info.get('filesize_approx')
            self._dbg("解析", f"✅ '{info.get('title','?')}' {self._format_size(sz)}")
            return {'success':True,'is_playlist':False,'title':info.get('title',''),'filesize':sz}
        except Exception as e:
            self.logger.error(f"解析异常: {type(e).__name__}: {e}")
            self._dbg("解析", f"❌ {type(e).__name__}: {str(e)[:500]}")
            return {'success':False,'error':str(e),'error_type':type(e).__name__}

    # ======= 下载流 =======
    async def _download_stream(self, url, fmt, tmpl):
        self._dbg("下载", f"fmt={fmt}")
        opts = self._inject({
            "outtmpl": tmpl, "format": fmt, "noplaylist": True,
            "quiet": True, "ffmpeg_location": None,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        })
        def _task():
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                fn = ydl.prepare_filename(info)
                self._dbg("下载", f"✅ {os.path.basename(fn)}")
                return fn, info
        return await asyncio.get_running_loop().run_in_executor(None, _task)

    # ======= 错误分析 =======
    def _analyze_error(self, err_msg):
        e = err_msg.lower()
        if "sign in to confirm" in e or "not a bot" in e:
            cookie_stat = "✅ 已配置" if self.cookies_path else "❌ 未配置"
            return (
                "\n   ⚠️ YouTube 反爬：VPS IP 被标记\n"
                f"   📌 Cookie 状态: {cookie_stat}\n"
                "   👉 未配置的话: WebUI→插件配置→youtube.cookies_path 填入路径\n"
                "   👉 已配置但无效: 检查 cookies.txt 是否过期/格式不对\n"
                "   📖 获取 cookie: Chrome扩展 'Get cookies.txt LOCALLY'"
            )
        if "412" in err_msg or "precondition" in e:
            return (
                "\n   ⚠️ B站 412：yt-dlp 版本过旧\n"
                "   👉 sudo python3 -m pip install -U --pre --break-system-packages \"yt-dlp[default]\""
            )
        if "403" in err_msg or "forbidden" in e:
            return "\n   ⚠️ HTTP 403：可能需要开代理或 cookie"
        if "externally-managed" in e:
            return "\n   ⚠️ Debian PEP 668：pip 被限制，加 --break-system-packages"
        return ""

    # ======= 主下载流程 =======
    async def _core_download_handler(self, event: AstrMessageEvent, url: str, method: str, ctype: str):
        if not url: return
        self._dbg("核心", f"url={url[:120]} method={method}")

        confirmed = False
        if "--y" in url:
            url = url.replace("--y", "").replace("  ", " ").strip()
            confirmed = True

        d = self._dbg_chat(event, "📡 步骤1: 解析资源信息...")
        if d: yield d

        yield event.plain_result("⏳ 正在解析资源信息...")
        info = await self._get_video_info_safe(url)

        if not info.get('success'):
            err_msg = info.get('error', '?')
            yield event.plain_result(f"❌ 解析失败\n📌 {info.get('error_type','?')}: {err_msg[:300]}")
            yield event.plain_result("🔄 尝试自动更新 yt-dlp 后重试...")
            updated, log = await self._try_update_ytdlp()
            yield event.plain_result(f"{'✅ 已更新' if updated else '⚠️ 更新未成功'}, 重试中...")
            info = await self._get_video_info_safe(url)

        if not info.get('success'):
            err_msg = info.get('error', '?')
            hint = self._analyze_error(err_msg)
            yield event.plain_result(
                f"❌ 重试后仍然失败\n📌 {err_msg[:300]}{hint}\n"
                f"💡 通用: 1)网站反爬更新 2)网络/代理 3)链接失效")
            return

        d = self._dbg_chat(event, "✅ 解析成功")
        if d: yield d

        ts = int(time.time())
        final_password = None

        # ---- 播放列表 ----
        if info.get('is_playlist'):
            count, title = info['count'], info['title']
            if not confirmed:
                yield event.plain_result(
                    f"📂 【{title}】\n🔢 {count}个\n⚠️ 确认？回复: /download {url} --y")
                return
            if count > 30:
                yield event.plain_result(f"❌ {count} 超过限制(30)")
                return

            yield event.plain_result(f"📦 下载播放列表({count}个)...")
            pf = os.path.join(self.temp_dir, f"pl_{ts}")
            os.makedirs(pf, exist_ok=True)

            opts = self._inject({
                "outtmpl": f"{pf}/%(playlist_index)s_%(title)s.%(ext)s",
                "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "quiet": True, "ignoreerrors": True, "noplaylist": False,
                "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
            })
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: yt_dlp.YoutubeDL(opts).download([url]))
            except Exception as e:
                yield event.plain_result(f"⚠️ 部分出错: {e}")

            files = glob.glob(os.path.join(pf, "*"))
            if not files:
                yield event.plain_result("❌ 无文件"); shutil.rmtree(pf); return

            yield event.plain_result(f"🔐 加密打包 {len(files)} 个文件 (密码:123456)...")
            try: import pyzipper
            except ImportError:
                yield event.plain_result("⚙️ 安装 pyzipper...")
                def _pip():
                    cmd = [sys.executable, "-m", "pip", "install", "pyzipper"]
                    r = subprocess.run(cmd, capture_output=True, text=True)
                    if r.returncode != 0 and "externally-managed" in (r.stderr or ""):
                        cmd.append("--break-system-packages")
                        subprocess.run(cmd, capture_output=True)
                await asyncio.get_running_loop().run_in_executor(None, _pip)
                import pyzipper

            zip_name = f"Playlist_{self._sanitize_filename(title)}_Pwd123456.zip"
            zip_path = os.path.join(self.temp_dir, zip_name)
            await asyncio.get_running_loop().run_in_executor(None, lambda: _mzip(zip_path, files))
            shutil.rmtree(pf)
            final_path, video_title_real = zip_path, f"Playlist_{title}"
            method, final_password = "file", "123456"

        # ---- 单视频 ----
        else:
            yield event.plain_result(f"📹 {info['title'][:30]}...\n⏳ 开始下载...")
            v_tmpl = f"{self.temp_dir}/v_{ts}_%(id)s.%(ext)s"
            a_tmpl = f"{self.temp_dir}/a_{ts}_%(id)s.%(ext)s"

            limit, h264 = self.max_quality, self.prefer_h264
            if limit == "最高画质":
                fv = "bestvideo[vcodec^=avc1]/bestvideo[ext=mp4]/bestvideo" if h264 else "bestvideo"
            else:
                h = int(limit.replace('p',''))
                fv = f"bestvideo[height<={h}][vcodec^=avc1]" if h264 else f"bestvideo[height<={h}]"
            fa = "bestaudio[ext=m4a]/bestaudio"
            self._dbg("核心", f"画质={limit} v={fv} a={fa}")

            try:
                if ctype == "audio_only":
                    final_path, ai = await self._download_stream(url, fa, a_tmpl)
                    video_title_real = ai.get('title', 'audio'); temp_files = [final_path]
                else:
                    vp, vi = await self._download_stream(url, fv, v_tmpl)
                    video_title_real = vi.get('title', 'video')
                    ap, ai = await self._download_stream(url, fa, a_tmpl)
                    yield event.plain_result("⚙️ 合并中...")
                    out_path = os.path.join(self.temp_dir, f"final_{ts}.mp4")
                    await self._manual_merge(vp, ap, out_path)
                    final_path, temp_files = out_path, [vp, ap]
            except Exception as e:
                yield event.plain_result(f"❌ 下载错误: {e}")
                updated, _ = await self._try_update_ytdlp()
                if updated: yield event.plain_result("✅ yt-dlp 已更新, 请重试")
                return

        # ---- 上传 ----
        if not final_path or not os.path.exists(final_path):
            yield event.plain_result("❌ 文件生成失败"); return

        fsize_mb = os.path.getsize(final_path) / (1024 * 1024)
        self._dbg("核心", f"文件={os.path.basename(final_path)} {fsize_mb:.1f}MB")
        d = self._dbg_chat(event, f"📦 文件就绪: {fsize_mb:.1f}MB")
        if d: yield d

        max_limit = 500 if info.get('is_playlist') else self.max_size_mb
        pwd_hint = f"\n🔐 **解压密码: {final_password}**" if final_password else ""

        if fsize_mb > max_limit:
            fn = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fn}"
            yield event.plain_result(f"⚠️ 文件过大({fsize_mb:.1f}MB)\n🔗 {furl}{pwd_hint}\n⏳ {self.delete_seconds}s 后清理")
        else:
            fn = os.path.basename(final_path)
            furl = f"http://{self.server_ip}:{self.server_port}/{fn}"
            safe = self._sanitize_filename(video_title_real)
            ext = os.path.splitext(final_path)[1]
            dname = f"{safe}{ext}"
            if final_password and "Pwd" not in dname:
                dname = f"Pwd{final_password}_{dname}"

            if method == "file":
                yield event.plain_result(f"⬆️ 上传中({fsize_mb:.1f}MB)...{pwd_hint}")
                tid, is_group = None, False
                if hasattr(event, 'message_obj'):
                    m = event.message_obj
                    if getattr(m, 'group_id', None):
                        is_group, tid = True, m.group_id
                    elif getattr(m, 'user_id', None):
                        tid = m.user_id
                if not tid: tid = event.session_id

                if tid:
                    act = "upload_group_file" if is_group else "upload_private_file"
                    key = "group_id" if is_group else "user_id"
                    try:
                        await event.bot.call_action(act, **{key: int(tid), "file": furl, "name": dname})
                    except Exception as ue:
                        yield event.plain_result(f"❌ 上传失败: {ue}\n🔗 {furl}{pwd_hint}")
                else:
                    yield event.plain_result(f"🔗 {furl}{pwd_hint}")
            else:
                yield event.chain_result([Video(file=furl, url=furl)])

        async def _clean():
            w = 120 if info.get('is_playlist') else self.delete_seconds + 30
            await asyncio.sleep(w)
            if os.path.exists(final_path): os.remove(final_path)
            if 'temp_files' in locals():
                for f in temp_files:
                    if os.path.exists(f): os.remove(f)
        asyncio.create_task(_clean())

    # ======= 命令 =======
    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str; ful = url
        for p in ["/download ", "download "]:
            if p in raw: ful = raw.split(p, 1)[1].strip()
        if "--y" not in ful and "--y" in raw: ful += " --y"
        async for r in self._core_download_handler(event, ful, "file", "merged"): yield r

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str; ful = url
        for p in ["/video ", "video "]:
            if p in raw: ful = raw.split(p, 1)[1].strip()
        if "--y" not in ful and "--y" in raw: ful += " --y"
        async for r in self._core_download_handler(event, ful, "video", "merged"): yield r

    @command("直链")
    async def cmd_get_direct_url(self, event: AstrMessageEvent, url: str = ""):
        raw = event.message_str; ful = url
        for p in ["/直链 ", "直链 "]:
            if p in raw: ful = raw.split(p, 1)[1].strip()
        if not ful: yield event.plain_result("❌ 请提供视频链接"); return

        yield event.plain_result("⏳ 解析直链...")
        opts = self._inject({
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "noplaylist": True, "skip_download": True,
            "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        })
        try:
            info = await asyncio.get_running_loop().run_in_executor(
                None, lambda: yt_dlp.YoutubeDL(opts).extract_info(ful, download=False))
        except Exception as e:
            yield event.plain_result(f"❌ 解析失败: {e}"); return
        if not info: yield event.plain_result("❌ 无法获取信息"); return

        title = info.get("title", "?"); dur = info.get("duration")
        ds = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
        fmts = info.get("formats", [])

        bc = bv = ba = None
        for f in fmts:
            vc, ac = f.get("vcodec", "none"), f.get("acodec", "none")
            if vc != "none" and ac != "none": bc = f
            if vc != "none" and ac == "none": bv = f
            if vc == "none" and ac != "none": ba = f

        lines = [f"🎬 {title}", f"⏱ {ds}", ""]
        if bc and bc.get("url"):
            lines.append(f"✅ 合并流({bc.get('width','?')}x{bc.get('height','?')} {bc.get('ext','?')}):")
            lines.append(bc["url"])
        elif info.get("url"): lines.append(f"✅ 直链:"); lines.append(info["url"])
        else: lines.append("⚠️ 无合并流")
        lines.append("")
        if bv and bv.get("url"):
            lines.append(f"🎥 视频({bv.get('width','?')}x{bv.get('height','?')} {bv.get('vcodec','?')}):")
            lines.append(bv["url"])
        lines.append("")
        if ba and ba.get("url"):
            lines.append(f"🎵 音频({ba.get('acodec','?')}):")
            lines.append(ba["url"])
        lines.append(""); lines.append("⚠️ 直链有时效性")
        yield event.plain_result("\n".join(lines))


def _mzip(zip_path, files):
    import pyzipper
    with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as zf:
        zf.setpassword(b"123456")
        for f in files: zf.write(f, os.path.basename(f))
