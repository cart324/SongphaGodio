import discord
from discord.ext import commands
import traceback

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
            print(f"Unhandled error in {ctx.command.name}:\n{error_log}")
            cart = self.bot.get_user(344384179552780289)
            if cart:
                try:
                    await cart.send(f"```Error in {ctx.command.name} by {ctx.author.name} in {ctx.guild.name}\n{error_log}```")
                except Exception as e:
                    print(f"Failed to send error DM: {e}")
            await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)


def setup(bot):
    bot.add_cog(FileUploader(bot))
