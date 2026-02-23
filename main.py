import os
import json
import logging
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from astrbot.api.all import *
from astrbot.api.message_components import Plain

# 全局变量存储最近事件
recent_events = []
MAX_EVENTS = 50

class GmodEventHandler(BaseHTTPRequestHandler):
    """接收 GMod 服务器发来的数据"""

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length).decode('utf-8', errors='ignore')

            # 解析 payload
            import urllib.parse
            params = urllib.parse.parse_qs(body)
            payload_str = params.get('payload', [''])[0]

            if payload_str:
                event_data = json.loads(payload_str)
                recent_events.append(event_data)

                # 保持列表不超过上限
                while len(recent_events) > MAX_EVENTS:
                    recent_events.pop(0)

                logging.getLogger("gmod_monitor").info(
                    f"收到事件: {event_data.get('event', 'unknown')}"
                )

            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            logging.getLogger("gmod_monitor").error(f"处理请求出错: {e}")
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, format, *args):
        pass  # 不打印每次请求日志


@register("gmod_monitor", "YourName", "GMod服务器监控", "2.0.0")
class GmodMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict, *args, **kwargs):
        super().__init__(context)
        self.logger = logging.getLogger("gmod_monitor")
        self.config = config

        # 启动 HTTP 接收服务器
        self.http_port = 9876
        self._start_receiver()

        # 启动后台通知循环
        self.notify_group_id = self.config.get("monitor", {}).get("notify_group_id", "")
        self.last_event_count = 0
        asyncio.create_task(self._notify_loop())

        self.logger.info(f"GMod 监控插件已启动，HTTP 端口: {self.http_port}")

    def _start_receiver(self):
        def run():
            server = HTTPServer(('0.0.0.0', self.http_port), GmodEventHandler)
            self.logger.info(f"HTTP 接收器已启动: 0.0.0.0:{self.http_port}")
            server.serve_forever()

        t = threading.Thread(target=run, daemon=True)
        t.start()

    async def _notify_loop(self):
        """检查是否有需要主动推送的事件"""
        while True:
            try:
                if len(recent_events) > self.last_event_count:
                    for event in recent_events[self.last_event_count:]:
                        event_type = event.get("event", "")

                        # 崩溃和封禁事件主动推送到群
                        if event_type in ("crash", "ban", "meltdown"):
                            if self.notify_group_id:
                                await self._send_group_msg(event)

                    self.last_event_count = len(recent_events)
            except Exception as e:
                self.logger.error(f"通知循环出错: {e}")

            await asyncio.sleep(5)

    async def _send_group_msg(self, event):
        """发送消息到QQ群"""
        event_type = event.get("event", "unknown")
        data = event.get("data", {})
        time_str = event.get("time", "未知时间")

        if event_type == "crash":
            msg = f"🚨 GMod 服务器崩溃！\n⏰ {time_str}\n🔄 看门狗已自动重启"

        elif event_type == "ban":
            msg = (
                f"🔨 自动封禁通知\n"
                f"⏰ {time_str}\n"
                f"👤 {data.get('player_name', '未知')}\n"
                f"🆔 {data.get('player_sid', '未知')}\n"
                f"📝 {data.get('reason', '未知原因')}"
            )

        elif event_type == "meltdown":
            culprits = data.get("culprits", {})
            names = ", ".join(culprits.values()) if culprits else "未找到"
            msg = (
                f"⚠️ 服务器触发熔断保护！\n"
                f"⏰ {time_str}\n"
                f"🕵️ 嫌疑人: {names}\n"
                f"🧹 已自动清图"
            )
        else:
            return

        try:
            await self.context.send_message(
                self.notify_group_id,
                [Plain(msg)]
            )
        except Exception as e:
            self.logger.error(f"发送群消息失败: {e}")

    @command("gmod状态")
    async def cmd_status(self, event: AstrMessageEvent):
        total = len(recent_events)
        crashes = sum(1 for e in recent_events if e.get("event") == "crash")
        bans = sum(1 for e in recent_events if e.get("event") == "ban")
        e2s = sum(1 for e in recent_events if e.get("event") == "e2_upload")

        lines = [
            "📊 GMod 服务器监控",
            "",
            f"📦 总事件数: {total}",
            f"💥 崩溃次数: {crashes}",
            f"🔨 封禁次数: {bans}",
            f"📝 E2上传数: {e2s}",
        ]

        if recent_events:
            last = recent_events[-1]
            lines.append(f"")
            lines.append(f"最后事件: {last.get('event')} @ {last.get('time')}")

        yield event.plain_result("\n".join(lines))

    @command("最近e2")
    async def cmd_recent_e2(self, event: AstrMessageEvent, count: str = "3"):
        try:
            n = min(int(count), 10)
        except:
            n = 3

        e2_events = [e for e in recent_events if e.get("event") == "e2_upload"]
        show = e2_events[-n:]

        if not show:
            yield event.plain_result("📭 暂无 E2 上传记录")
            return

        lines = [f"📋 最近 {len(show)} 条 E2 上传:", ""]

        for i, ev in enumerate(show, 1):
            d = ev.get("data", {})
            lines.append(
                f"【{i}】{d.get('player_name','?')} "
                f"({d.get('player_sid','?')}) "
                f"{d.get('code_length', 0)}字符 "
                f"@ {ev.get('time','?')}"
            )

        yield event.plain_result("\n".join(lines))

    @command("分析e2")
    async def cmd_analyze(self, event: AstrMessageEvent):
        e2_events = [e for e in recent_events if e.get("event") == "e2_upload"]

        if not e2_events:
            yield event.plain_result("📭 暂无 E2 记录")
            return

        last = e2_events[-1]
        code = last.get("data", {}).get("code", "无代码")
        player = last.get("data", {}).get("player_name", "未知")

        yield event.plain_result("🔍 正在分析...")

        prompt = (
            f"你是 GMod Wiremod Expression 2 代码审计专家。\n"
            f"玩家 {player} 上传了以下代码：\n\n"
            f"```\n{code}\n```\n\n"
            f"请判断：\n"
            f"1. 是否恶意？(是/否/不确定)\n"
            f"2. 风险等级：(高/中/低/无)\n"
            f"3. 简短原因\n"
            f"4. 建议处理"
        )

        try:
            resp = await self.context.get_using_provider().text_chat(
                prompt=prompt,
                session_id=event.session_id
            )

            if resp and resp.completion_text:
                yield event.plain_result(
                    f"🤖 E2 代码分析:\n\n{resp.completion_text}"
                )
            else:
                yield event.plain_result("❌ LLM 无返回")
        except Exception as e:
            yield event.plain_result(f"❌ 分析失败: {e}")
