import aiohttp
import discord
from discord.ext import commands
import io
import traceback
from urllib.parse import unquote, urlsplit
from modules.error_notifier import send_error_log

develop_server_ids = [1242846739434569738]

upload_channel_id = 1337411731449843834

allowed_file_hosts = {'cdn.discordapp.com', 'media.discordapp.net'}


async def download_discord_file(url: str, max_size: int) -> tuple[bytes, str]:
    parsed_url = urlsplit(url)
    if (
        parsed_url.scheme != 'https'
        or parsed_url.hostname not in allowed_file_hosts
        or parsed_url.port not in (None, 443)
    ):
        raise ValueError('Discord 첨부파일의 HTTPS 링크만 사용할 수 있습니다.')

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as response:
            if 300 <= response.status < 400:
                raise ValueError('파일 다운로드 중 리디렉션이 발생했습니다.')

            response.raise_for_status()
            if response.content_length is not None and response.content_length > max_size:
                raise ValueError('파일이 Discord 서버의 업로드 용량 제한을 초과합니다.')

            data = await response.read()
            if len(data) > max_size:
                raise ValueError('파일이 Discord 서버의 업로드 용량 제한을 초과합니다.')

            content_disposition = response.content_disposition
            filename = content_disposition.filename if content_disposition else None
            if not filename:
                filename = unquote(parsed_url.path.rsplit('/', 1)[-1])

    filename = filename.replace('\\', '/').rsplit('/', 1)[-1] or 'upload'
    return data, filename


class FileUploader(commands.Cog, name="file_uploader"):
    def __init__(self, bot):
        self.bot = bot

    @commands.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
    async def upload(self, ctx, url):
        try:
            await ctx.defer()
            url = url.strip("‪")
            channel = await self.bot.fetch_channel(upload_channel_id)

            if urlsplit(url).scheme in ('http', 'https'):
                data, filename = await download_discord_file(
                    url, channel.guild.filesize_limit
                )

                with io.BytesIO(data) as buffer:
                    file = discord.File(buffer, filename=filename)
                    try:
                        await channel.send(file=file)
                    finally:
                        file.close()
            else:
                await channel.send(file=discord.File(url))

            print(f"{url}")
            await ctx.followup.send("완료")
        except ValueError as error:
            if ctx.response.is_done():
                await ctx.followup.send(str(error), ephemeral=True)
            else:
                await ctx.respond(str(error), ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)


def setup(bot):
    bot.add_cog(FileUploader(bot))
