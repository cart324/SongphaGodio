import discord
from discord.ext import commands
import traceback
from modules.error_notifier import send_error_log

develop_server_ids = [1242846739434569738]

upload_channel_id = 1337411731449843834


class FileUploader(commands.Cog, name="file_uploader"):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
    async def upload(self, ctx, url):
        try:
            url = url.strip("‪")
            channel = await self.bot.fetch_channel(upload_channel_id)
            await channel.send(file=discord.File(url))
            print(f"{url}")
            await ctx.respond("완료")
        except Exception:
            error_log = traceback.format_exc(limit=None, chain=True)
            await send_error_log(f"Error in {ctx.command.name} by {ctx.author.name} in {ctx.guild.name}\n{error_log}")
            await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)


def setup(bot):
    bot.add_cog(FileUploader(bot))
