import asyncio
import logging
import os
import yt_dlp
import glob
from astrbot.api.all import *
from astrbot.api.message_components import Video, Plain, File

# 版本号升级为 2.2.2
@register("yt_dlp_plugin", "YourName", "全能视频下载助手", "2.2.2")
class YtDlpPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("astrbot_plugin_yt_dlp")
        self.config = config
        
        # 插件目录路径
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        self.temp_dir = os.path.join(self.plugin_dir, "temp")
        if not os.path.exists(self.temp_dir):
            os.makedirs(self.temp_dir)
        
        # 从配置读取代理设置
        self.proxy_enabled = self.config.get("proxy", {}).get("enabled", False)
        self.proxy_url = self.config.get("proxy", {}).get("url", "")
        
        # 从配置读取下载设置
        self.max_quality = self.config.get("download", {}).get("max_quality", "720p")
        self.max_size_mb = self.config.get("download", {}).get("max_size_mb", 512)
        self.delete_seconds = self.config.get("download", {}).get("auto_delete_seconds", 60)
        
        # FFmpeg 路径处理
        self.ffmpeg_path = self._get_ffmpeg_path()
        
        self.logger.info(f"代理设置: {'已启用 - ' + self.proxy_url if self.proxy_enabled else '未启用'}")
        self.logger.info(f"画质限制: {self.max_quality}, 大小限制: {self.max_size_mb}MB")

    def _get_ffmpeg_path(self):
        """智能获取 FFmpeg 路径"""
        if self.config.get("ffmpeg", {}).get("use_builtin", True):
            builtin_path = os.path.join(self.plugin_dir, "ffmpeg.exe")
            if os.path.exists(builtin_path):
                return builtin_path
        
        custom_path = self.config.get("ffmpeg", {}).get("custom_path", "")
        if custom_path and os.path.exists(custom_path):
            return custom_path
        return None

    def _get_quality_format(self):
        """根据配置生成 yt-dlp 的 format 字符串"""
        quality_map = { "480p": 480, "720p": 720, "1080p": 1080 }
        max_height = quality_map.get(self.max_quality, 720)
        return f"bestvideo[ext=mp4][height<=?{max_height}]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    async def _core_download_handler(self, event: AstrMessageEvent, url: str, send_mode: str):
        """
        核心下载逻辑
        :param send_mode: 'video' 或 'file'
        """
        if not url:
            cmd_name = "群文件" if send_mode == "file" else "下载"
            yield event.plain_result(f"请提供视频链接。用法: /{cmd_name} <URL>")
            return

        yield event.plain_result(f"⏳ 正在下载 (最高画质: {self.max_quality})...")

        # 构建 yt-dlp 配置
        # file 模式下使用 title 作为文件名，video 模式使用 id (避免文件名特殊字符导致发送失败)
        out_tmpl = f"{self.temp_dir}/%(title)s.%(ext)s" if send_mode == "file" else f"{self.temp_dir}/%(id)s.%(ext)s"
        
        ydl_opts = {
            "outtmpl": out_tmpl,
            "restrictfilenames": True, # 限制文件名特殊字符
            "format": self._get_quality_format(),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "max_filesize": self.max_size_mb * 1024 * 1024,
            "nocheckcertificate": True,
            "merge_output_format": "mp4",
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        
        if self.proxy_enabled and self.proxy_url:
            ydl_opts["proxy"] = self.proxy_url
        if self.ffmpeg_path:
            ydl_opts["ffmpeg_location"] = self.ffmpeg_path

        file_path = None
        
        try:
            loop = asyncio.get_running_loop()
            
            def _download_task():
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    return ydl.prepare_filename(info)

            file_path = await loop.run_in_executor(None, _download_task)

            if not file_path or not os.path.exists(file_path):
                base_name = os.path.splitext(file_path)[0]
                possible = glob.glob(f"{base_name}*")
                if possible:
                    file_path = possible[0]
                else:
                    raise Exception("未找到下载文件")

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if file_size_mb > self.max_size_mb:
                yield event.plain_result(f"❌ 视频太大 ({file_size_mb:.2f}MB)，超过限制")
                return

            yield event.plain_result(f"✅ 下载成功 ({file_size_mb:.2f}MB)，正在上传...")

            abs_path = os.path.abspath(file_path)
            
            if send_mode == "file":
                # 群文件：使用 file 参数，并指定 name
                file_name = os.path.basename(abs_path)
                file_component = File(file=abs_path, name=file_name)
                yield event.chain_result([file_component])
            else:
                # 视频消息：使用 Video 组件
                video_component = Video.fromFileSystem(path=abs_path)
                yield event.chain_result([video_component])

        except Exception as e:
            self.logger.error(f"下载异常: {e}")
            err_msg = str(e)
            if "HTTP Error 404" in err_msg:
                yield event.plain_result("❌ 视频不存在")
            elif "ffmpeg" in err_msg.lower():
                yield event.plain_result("❌ FFmpeg 配置错误")
            else:
                yield event.plain_result(f"❌ 出错: {err_msg[:50]}")

        finally:
            if file_path and os.path.exists(file_path):
                async def _delayed_remove(p):
                    # 群文件上传较慢，等待时间设长一点
                    wait_time = self.delete_seconds + 30 if send_mode == "file" else self.delete_seconds
                    await asyncio.sleep(wait_time)
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except: pass
                asyncio.create_task(_delayed_remove(file_path))

    @command("下载")
    async def download_video(self, event: AstrMessageEvent, url: str = ""):
        """下载视频并发送"""
        async for result in self._core_download_handler(event, url, send_mode="video"):
            yield result

    @command("群文件")
    async def download_as_group_file(self, event: AstrMessageEvent, url: str = ""):
        """下载视频并上传群文件"""
        async for result in self._core_download_handler(event, url, send_mode="file"):
            yield result

    @command("提取")
    async def extract_url(self, event: AstrMessageEvent, url: str = ""):
        """提取直链"""
        if not url:
            yield event.plain_result("用法: /提取 <URL>")
            return
        
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
        if self.proxy_enabled: ydl_opts["proxy"] = self.proxy_url
        
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, lambda: yt_dlp.YoutubeDL(ydl_opts).extract_info(url, download=False))
            
            best_url = info.get("url")
            if not best_url:
                for f in reversed(info.get("formats", [])):
                    if f.get("url"): 
                        best_url = f.get("url")
                        break
            
            yield event.plain_result(f"✅ 标题: {info.get('title')}\n🔗 直链: {best_url}")
        except Exception as e:
            yield event.plain_result(f"❌ 解析失败: {e}")