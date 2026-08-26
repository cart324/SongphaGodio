import discord
from discord.ext import commands
import traceback
import sys
import subprocess
import requests
import asyncio
import yt_dlp
from modules.error_notifier import send_error_log

# Audio_player.py 에서 server_info_dict를 가져오기 위함
# 이 import 경로는 프로젝트 구조에 따라 달라질 수 있습니다.
# 만약 main.py와 cogs 폴더가 같은 레벨에 있다면 이 경로는 유효합니다.
from cogs.Audio_player import server_info_dict

# 개발 서버 ID
DEVELOP_SERVER_ID = 1242846739434569738


class Maintenance(commands.Cog):
    """봇의 유지보수 및 상태 확인을 위한 관리자용 명령어 세트입니다."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.slash_command(
        name="update_yt_dlp",
        description="yt-dlp 라이브러리를 업데이트하고 봇을 재시작합니다. (관리자 전용)",
        guild_ids=[DEVELOP_SERVER_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    async def update_yt_dlp(self, ctx: discord.ApplicationContext):
        """yt-dlp를 최신 버전으로 업데이트하고 봇을 재시작하는 관리자 명령어"""
        await ctx.defer()

        try:
            # 1. 현재 설치된 버전 확인
            current_version = yt_dlp.version.__version__

            # 2. PyPI에서 최신 버전 정보 가져오기
            response = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=5)
            response.raise_for_status()
            latest_version = response.json()["info"]["version"]

            if current_version == latest_version:
                await ctx.followup.send(
                    f"✅ `yt-dlp`는 이미 최신 버전({latest_version})입니다. 재시작이 필요하지 않습니다."
                )
                return

            await ctx.followup.send(
                f"현재 `yt-dlp` 버전({current_version}) -> 최신({latest_version})으로 업데이트를 시작합니다..."
            )

            # 3. pip를 사용하여 yt-dlp 업데이트 (별도 스레드에서 실행)
            process = await asyncio.to_thread(
                subprocess.run,
                [sys.executable, "-m", "pip", "install", "-U", "yt-dlp"],
                capture_output=True, text=True, check=False  # check=False로 에러 발생 시에도 계속 진행
            )

            if process.returncode == 0:
                await ctx.followup.send(
                    f"✅ `yt-dlp`가 {latest_version} 버전으로 성공적으로 업데이트되었습니다.\n"
                    "**봇을 재시작하여 변경사항을 적용합니다.**"
                )
                # main.py의 재시작 로직을 트리거하기 위한 플래그 설정
                self.bot.restart_reason = 'update'
                await self.bot.close()
            else:
                error_message = process.stderr or process.stdout
                await ctx.followup.send(
                    f"❌ 업데이트 중 오류가 발생했습니다.\n"
                    f"```\n{error_message}\n```"
                )

        except requests.RequestException as e:
            await ctx.followup.send(f"PyPI에서 최신 버전 정보를 가져오는 데 실패했습니다: {e}")
        except Exception:
            error_log = traceback.format_exc()
            print(f"yt-dlp update failed: {error_log}")
            await ctx.followup.send(f"알 수 없는 오류가 발생했습니다. 로그를 확인해주세요.")

    @commands.slash_command(
        name="check_players",
        description="현재 봇이 활성화된 (음성 채널에 있는) 서버 목록을 확인합니다. (관리자 전용)",
        guild_ids=[DEVELOP_SERVER_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    async def check_players(self, ctx: discord.ApplicationContext):
        """현재 활성화된 플레이어(음성 채널)가 있는 서버 목록을 확인합니다."""
        await ctx.defer()

        active_guilds = []
        # bot.voice_clients는 현재 봇이 연결된 모든 음성 클라이언트 목록을 담고 있습니다.
        if not self.bot.voice_clients:
            await ctx.followup.send("현재 활성화된 플레이어가 있는 서버가 없습니다.")
            return

        embed = discord.Embed(
            title="🎧 활성 플레이어 목록",
            description=f"현재 총 {len(self.bot.voice_clients)}개의 서버에서 플레이어가 활성화되어 있습니다.",
            color=discord.Color.blue()
        )

        for vc in self.bot.voice_clients:
            guild = vc.guild
            server_info = server_info_dict.get(guild.id)

            # 상태 정보 생성
            status = "재생 중" if vc.is_playing() else "일시정지" if vc.is_paused() else "대기 중"

            # 현재 곡 정보
            current_song = "없음"
            if server_info and server_info.song_cache:
                current_song = server_info.song_cache.get('title', '정보 없음')

            # 대기열 곡 수
            queue_count = 0
            if server_info:
                queue_count = len(server_info.queue)

            embed.add_field(
                name=f"서버: {guild.name} (ID: {guild.id})",
                value=(
                    f"**채널:** {vc.channel.name}\n"
                    f"**상태:** {status}\n"
                    f"**현재 곡:** {current_song}\n"
                    f"**대기열:** {queue_count}개"
                ),
                inline=False
            )

        await ctx.followup.send(embed=embed)

    # is_owner() 체크 실패 시 에러 핸들러
    async def cog_check(self, ctx: discord.ApplicationContext) -> bool:
        return bool(ctx.guild and ctx.author.guild_permissions.administrator)

    async def cog_command_error(self, ctx: discord.ApplicationContext, error: Exception):
        if isinstance(error, commands.CheckFailure):
            await ctx.respond("이 명령어는 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
        else:
            # 다른 종류의 에러가 발생했을 때 콘솔에 로그를 남깁니다.
            error_log = "".join(traceback.format_exception(type(error), error, error.__traceback__))

            await send_error_log(error_log, ctx.author.name)
            if not ctx.interaction.response.is_done():
                await ctx.respond("명령어 처리 중 오류가 발생했습니다.", ephemeral=True)

    @commands.slash_command(
        name="say",
        description="활성화된 모든 플레이어 채널에 관리자 메시지를 전송합니다. (관리자 전용)",
        guild_ids=[DEVELOP_SERVER_ID],
        default_member_permissions=discord.Permissions(administrator=True)
    )
    async def say(self, ctx: discord.ApplicationContext, text: discord.Option(str, "전송할 메시지 내용", required=True)):
        """활성화된 모든 플레이어에게 메시지를 보냅니다."""
        await ctx.defer()

        if not self.bot.voice_clients:
            await ctx.followup.send("메시지를 보낼 활성 플레이어가 없습니다.")
            return

        success_count = 0
        fail_count = 0

        for vc in self.bot.voice_clients:
            try:
                server_info = server_info_dict.get(vc.guild.id)
                if server_info and server_info.embed_channel:
                    await server_info.embed_channel.send(f"📢 **관리자 알림:** {text}")
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                print(f"Failed to send message to guild {vc.guild.id}: {e}")
                fail_count += 1

        await ctx.followup.send(
            f"메시지 전송이 완료되었습니다.\n"
            f"✅ 성공: {success_count} 서버\n"
            f"❌ 실패: {fail_count} 서버"
        )

    async def cog_check(self, ctx: discord.ApplicationContext) -> bool:
        return bool(ctx.guild and ctx.author.guild_permissions.administrator)

    async def cog_command_error(self, ctx: discord.ApplicationContext, error: Exception):
        if isinstance(error, commands.CheckFailure):
            await ctx.respond("이 명령어는 봇 소유자만 사용할 수 있습니다.", ephemeral=True)
        else:
            error_log = "".join(traceback.format_exception(type(error), error, error.__traceback__))

            await send_error_log(error_log, ctx.author.name)


def setup(bot: commands.Bot):
    """Cog를 봇에 등록합니다."""
    bot.add_cog(Maintenance(bot))
