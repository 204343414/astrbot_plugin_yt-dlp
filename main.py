import asyncio
import logging
import os
import time
import yt_dlp
import glob
import re
import subprocess
import imageio_ffmpeg  # 新增这一行
import shutil  # 新增这一行
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
            
        # 2. 寻找 FFmpeg (使用 imageio-ffmpeg 库自带的路径，全平台通用)
        try:
            self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            self.logger.info(f"已加载 FFmpeg: {self.ffmpeg_exe}")
        except Exception as e:
            # 备用方案：如果库获取失败，尝试寻找系统命令
            self.ffmpeg_exe = "ffmpeg"
            self.logger.warning(f"imageio-ffmpeg 加载失败，回退到系统命令: {e}")
        # 3. 基础配置
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        self.max_quality = self.config.get("download", {}).get("max_quality", "720p")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 512)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)

    @command("check_env")
    async def cmd_check_env(self, event: AstrMessageEvent):
        """诊断 FFmpeg 环境"""
        yield event.plain_result(f"🔍 诊断中...\nFFmpeg路径: {self.ffmpeg_exe}")
        
        # 判断是否可执行（文件存在 或 系统路径中可找到）
        is_ready = False
        if os.path.exists(self.ffmpeg_exe) and os.path.isfile(self.ffmpeg_exe):
            is_ready = True
        elif shutil.which(self.ffmpeg_exe):
            is_ready = True
            
        if is_ready:
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
            yield event.plain_result(f"❌ 未找到 FFmpeg: Debian请运行 'apt install ffmpeg'，Windows请放入 ffmpeg.exe")

    def _sanitize_filename(self, name: str) -> str:
        if not name: return "video"
        name = re.sub(r'[\\/*?:"<>|]', '_', name)
        name = name.replace('\n', ' ').replace('\r', '')
        return name[:50].strip()

    def _format_size(self, size_bytes):
        """格式化文件大小显示"""
        if size_bytes is None:
            return "未知"
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.2f} KB"
        elif size_bytes < 1024 * 1024 * 1024:
            return f"{size_bytes / (1024 * 1024):.2f} MB"
        else:
            return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"

    async def _manual_merge(self, video_path, audio_path, output_path):
        """Python 手动合并"""
        cmd = [
            self.ffmpeg_exe, "-i", video_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "copy", "-y", output_path
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

    async def _get_video_info_safe(self, url):
        """获取视频信息（标题、预估大小等）"""
        ydl_opts = {
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "extract_flat": False,
        }
        if self.proxy_enabled and self.proxy_url:
            ydl_opts["proxy"] = self.proxy_url

        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(
                None, 
                lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False)
            )
            
            # 获取预估文件大小
            filesize = info.get('filesize') or info.get('filesize_approx')
            
            # 如果主信息没有大小，尝试从 formats 中获取
            if not filesize and info.get('formats'):
                total_size = 0
                for fmt in info.get('formats', []):
                    if fmt.get('filesize'):
                        total_size = max(total_size, fmt.get('filesize', 0))
                    elif fmt.get('filesize_approx'):
                        total_size = max(total_size, fmt.get('filesize_approx', 0))
                if total_size > 0:
                    filesize = total_size
            
            return {
                'title': info.get('title', ''),
                'duration': info.get('duration'),
                'filesize': filesize,
                'resolution': info.get('resolution'),
                'uploader': info.get('uploader'),
            }
        except Exception as e:
            self.logger.error(f"获取视频信息失败: {e}")
            return None

    async def _check_content_safety_llm(self, title: str):
        """核心逻辑：调用 LLM 进行内容审查"""
        provider = self.context.get_using_provider()
        if not provider:
            self.logger.warning("未找到 LLM Provider，跳过安全检查")
            return True

        prompt = f"""
        你现在是内容审核员。请审核以下视频标题，判断内容是否包含【政治敏感/反动/严重色情/严重暴恐】等极易致社交账号被封禁的内容。
        
        待审核标题：{title}
        
        严格遵循以下规则：
        1. 如内容包含上述危险信息，必须回复 "UNSAFE"
        2. 如内容正常的新闻、娱乐、生活、科技、游戏等，回复 "SAFE"
        3. 仅回复一个单词（SAFE 或 UNSAFE），不要解释。
        """
        
        try:
            response = await provider.text_chat(prompt, session_id=None)
            
            ans_text = ""
            if isinstance(response, str):
                ans_text = response
            elif hasattr(response, "completion_text"):
                ans_text = response.completion_text
            else:
                ans_text = str(response)

            self.logger.info(f"LLM 审查结果 [{title}]: {ans_text}")

            if "UNSAFE" in ans_text.upper():
                return False
            return True

        except Exception as e:
            self.logger.error(f"LLM 安全审查调用失败: {e}")
            return True

    async def _download_stream(self, url, format_str, filename_tmpl):
        """下载流"""
        ydl_opts = {
            "outtmpl": filename_tmpl, "format": format_str, "noplaylist": True,
            "quiet": True, "no_warnings": True, "nocheckcertificate": True,
            "ffmpeg_location": self.ffmpeg_exe, 
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

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, send_method: str, content_type: str = "merged"):
        """下载核心逻辑"""
        if not url and send_method != "tool_auto": 
            yield event.plain_result(f"Usage: /download <URL>")
            return

        mode_text = "纯音频" if content_type == "audio_only" else "音画合并"
        yield event.plain_result(f"⏳ 正在获取视频信息...")

        # 获取视频信息（包含预估大小）
        video_info = await self._get_video_info_safe(url)
        if video_info:
            title_preview = video_info.get('title', '未知')[:30]
            est_size = self._format_size(video_info.get('filesize'))
            duration = video_info.get('duration')
            duration_str = f"{int(duration // 60)}分{int(duration % 60)}秒" if duration else "未知"
            
            yield event.plain_result(
                f"📹 标题: {title_preview}...\n"
                f"⏱️ 时长: {duration_str}\n"
                f"📦 预估大小: {est_size}\n"
                f"🎬 模式: {mode_text}\n"
                f"⏳ 开始下载..."
            )
        else:
            yield event.plain_result(f"⏳ 正在下载: {mode_text}...")

        timestamp_id = int(time.time())
        video_tmpl = f"{self.temp_dir}/v_{timestamp_id}_%(id)s.%(ext)s"
        audio_tmpl = f"{self.temp_dir}/a_{timestamp_id}_%(id)s.%(ext)s"
        
        # ========== 画质选择逻辑 ==========
        quality_map = { "480p": 480, "720p": 720, "1080p": 1080, "最高画质": None }
        max_height = quality_map.get(self.max_quality, 720)

        if max_height is None:
            # 最高画质模式：不限制分辨率
            fmt_video = "bestvideo[vcodec^=avc1]/bestvideo"
            fmt_fallback = "best"
            self.logger.info("画质模式: 最高画质（无限制）")
        else:
            # 限制画质模式
            fmt_video = f"bestvideo[vcodec^=avc1][height<=?{max_height}]/bestvideo[height<=?{max_height}]"
            fmt_fallback = f"best[ext=mp4][height<=?{max_height}]/best[height<=?{max_height}]/best"
            self.logger.info(f"画质模式: 限制 {max_height}p")

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
                    
                    yield event.plain_result("⚙️ 正在合并音视频...")
                    output_path = os.path.join(self.temp_dir, f"final_{timestamp_id}.mp4")
                    await self._manual_merge(v_path, a_path, output_path)
                    final_file_path = output_path
                except Exception as e_split:
                    self.logger.warning(f"分流失败，尝试回退: {e_split}")
                    fallback_tmpl = f"{self.temp_dir}/f_{timestamp_id}_%(id)s.%(ext)s"
                    f_path, f_info = await self._download_stream(url, fmt_fallback, fallback_tmpl)
                    video_title = f_info.get('title', 'video')
                    final_file_path = f_path

            if not final_file_path or not os.path.exists(final_file_path):
                raise Exception("文件生成失败")

            # 获取实际文件大小
            file_size_bytes = os.path.getsize(final_file_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            file_size_str = self._format_size(file_size_bytes)
            
            if file_size_mb > self.max_size_mb:
                yield event.plain_result(
                    f"❌ 文件过大!\n"
                    f"📦 实际大小: {file_size_str}\n"
                    f"📏 限制大小: {self.max_size_mb} MB\n"
                    f"💡 请降低画质或选择更短的视频"
                )
                # 清理文件
                try:
                    os.remove(final_file_path)
                except:
                    pass
                return

            yield event.plain_result(
                f"✅ 下载完成!\n"
                f"📦 文件大小: {file_size_str}\n"
                f"📤 正在上传..."
            )
            
            abs_path = os.path.abspath(final_file_path)
            
            if send_method == "file":
                safe_title = self._sanitize_filename(video_title)
                ext = os.path.splitext(abs_path)[1]
                yield event.chain_result([File(file=abs_path, name=f"{safe_title}{ext}")])
            else:
                yield event.chain_result([Video.fromFileSystem(path=abs_path)])

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
            yield event.plain_result(f"❌ 下载错误: {str(e)[:100]}")

    @llm_tool(name="download_video")
    async def cmd_llm_download_video(self, event: AstrMessageEvent, url: str, mode: str = "video_stream"):
        '''下载视频或提取音频的工具。

        Args:
            url(string): 视频的链接地址
            mode(string): 下载模式： "video_stream" (默认)视频消息； "video_file" 文件消息； "audio_only" 仅音频文件。
        '''
        
        # === 1. 智能安全预检 (仅在 Tool 调用时触发) ===
        yield event.plain_result("🔍 AI 正在检测内容安全性...")
        
        video_info = await self._get_video_info_safe(url)
        
        if video_info and video_info.get('title'):
            is_safe = await self._check_content_safety_llm(video_info['title'])
            
            if not is_safe:
                yield event.plain_result(f"System: ⚠️ AI 安全拦截：视频被识别为敏感/高风险内容，下载任务已终止。")
                return
        
        # === 2. 安全检查通过，执行下载 ===
        send_method = "video"
        content_type = "merged"

        if mode == "video_file":
            send_method = "file" 
            content_type = "merged"
        elif mode == "audio_only":
            send_method = "file"
            content_type = "audio_only"

        has_error = False
        error_msg = ""

        try:
            async for result in self._core_download_handler(event, url, send_method=send_method, content_type=content_type):
                yield result
                if isinstance(result, Plain):
                    text = result.text
                    if "❌" in text or "Error" in text or "错误" in text:
                        has_error = True
                        error_msg = text

            if has_error:
                yield event.plain_result(f"System: 下载任务失败: {error_msg}")
            else:
                suffix = "音频" if mode == "audio_only" else "视频"
                yield event.plain_result(f"System: {suffix}已下载并发送完毕。")

        except Exception as e:
            yield event.plain_result(f"System: 插件执行异常: {e}")

    @command("download")
    async def cmd_download_file(self, event: AstrMessageEvent, url: str = ""):
        """指令下载 - 文件模式 (不做 AI 检查)"""
        async for result in self._core_download_handler(event, url, send_method="file", content_type="merged"):
            yield result

    @command("video")
    async def cmd_download_video(self, event: AstrMessageEvent, url: str = ""):
        """指令下载 - 视频模式 (不做 AI 检查)"""
        async for result in self._core_download_handler(event, url, send_method="video", content_type="merged"):
            yield result
            
    @command("extract")
    async def cmd_extract_url(self, event: AstrMessageEvent, url: str = ""):
        """提取直链"""
        if not url: 
            yield event.plain_result("用法: /extract <视频URL>")
            return
        ydl_opts = {"quiet": True}
        if self.proxy_enabled: 
            ydl_opts["proxy"] = self.proxy_url
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None, 
                lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False)
            )
            file_size = self._format_size(info.get('filesize') or info.get('filesize_approx'))
            yield event.plain_result(
                f"✅ 标题: {info.get('title')}\n"
                f"📦 大小: {file_size}\n"
                f"🔗 直链: {info.get('url')}"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 提取失败: {e}")