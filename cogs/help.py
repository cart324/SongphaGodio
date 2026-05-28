import discord
from discord.ext import commands

class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(name="help", description="봇의 명령어 목록을 보여줍니다.")
    async def help_command(self, ctx: discord.ApplicationContext):
        """봇의 모든 명령어에 대한 도움말을 표시합니다."""
        embed = discord.Embed(
            title="명령어 목록",
            description="사용 가능한 모든 명령어 목록입니다.",
            color=discord.Color.blue()
        )

        # AudioPlayer 명령어
        embed.add_field(
            name="🎵 오디오",
            value="`/play` : 음악을 재생목록에 추가합니다.\n"
                  "`/playlist` : 유튜브 재생목록의 모든 음악을 재생목록에 추가합니다.\n"
                  "`/delete_from_queue` : 재생목록에서 특정 곡을 제거합니다.\n"
                  "`/skip` : 현재 재생 중인 곡을 건너뜁니다.\n"
                  "`/set_volume` : 현재 곡의 볼륨을 조절합니다.\n"
                  "`/auto_volume` : 현재 곡의 볼륨을 자동으로 조절합니다.\n"
                  "`/stop` : 음악 재생을 멈추고 재생목록을 비웁니다.\n"
                  "`/pause_resume` : 음악을 일시정지하거나 다시 재생합니다.\n"
                  "`/loop` : 현재 곡을 반복재생합니다.\n"
                  "`/shuffle` : 재생목록을 무작위 순서로 재생합니다.\n"
                  "`/re_embed` : 플레이어 UI를 아래로 내립니다.\n"
                  "`/leave` : 봇을 음성 채널에서 내보냅니다.",
            inline=False
        )

        await ctx.respond(embed=embed)

def setup(bot):
    bot.add_cog(Help(bot))
