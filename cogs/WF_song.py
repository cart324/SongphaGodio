import discord
from discord.ext import commands
from functools import wraps
import traceback
from modules.error_notifier import send_error_log

song_channel_id = 1337411731449843834


async def download_song(message_id: int, bot: discord.Bot) -> str:
    song_channel = await bot.fetch_channel(song_channel_id)
    message = await song_channel.fetch_message(message_id)
    song_url = message.attachments[0].url
    return song_url


def song_command(source):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, ctx):
            try:
                if isinstance(source, int):
                    link = await download_song(source, self.bot)
                else:
                    link = source

                audio_player_cog = self.bot.get_cog('audio_player')
                await audio_player_cog.play.callback(audio_player_cog, ctx, url=link)
            except Exception:
                await send_error_log(traceback.format_exc(), ctx.author.name)

                if ctx.response.is_done():
                    await ctx.followup.send('알 수 없는 오류입니다.', ephemeral=True)
                else:
                    await ctx.respond('알 수 없는 오류입니다.', ephemeral=True)

        return commands.slash_command(
            guild_ids=[312795500757909506, 1242846739434569738]
        )(wrapper)

    return decorator


class WFSong(commands.Cog, name="wf_song"):
    def __init__(self, bot):
        self.bot = bot

    @song_command(1337422572161335368)
    async def back_to_lobby(self, ctx):
        """Nacas의 불후의 명곡 'back to lobby'를 재생합니다."""

    @song_command(1542261321570193499)
    async def back_to_lobby2(self, ctx):
        """Nacas의 최신곡 'Back to Lobby 2'를 재생합니다."""

    @song_command(1337422577886302273)
    async def rage(self, ctx):
        """아니피1이야왜시발나무뒤에쳐숨어서쉴배나빨고있냐고나가서좀잡아시발요한아좀잡아"""

    @song_command(1337434532303081513)
    async def rage2(self, ctx):
        """아니준서왜컨버터인데왜아직도딜량이꼴등이야아오제발(제발)"""

    @song_command(1337422585482444954)
    async def great_emperor_boobs(self, ctx):
        """:clown:"""

    @song_command("https://www.youtube.com/watch?v=mINlmbJcP-M")
    async def box_now_box_for_hard(self, ctx):
        """🤌🤡🤌"""

    @song_command("https://www.youtube.com/watch?v=Vxf41Pf3Ij0")
    async def waiting_for_player(self, ctx):
        """waiting for ya-ho-han wa cao ni ma sha bi"""

    @song_command("https://www.youtube.com/watch?v=ezXluhqaqfI")
    async def i_sold_rice_shower(self, ctx):
        """ooh! ooh! ohh! 米! 米! 米!"""


def setup(bot):
    bot.add_cog(WFSong(bot))
