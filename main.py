import discord
from discord import ExtensionAlreadyLoaded, ExtensionNotFound, ExtensionNotLoaded
from discord.ext import commands
import os
import shutil
import stat
import traceback
from modules.error_notifier import configure_error_notifier, send_error_log
import time
import requests
import yt_dlp
import asyncio
import sys
from packaging.version import Version


def patch_pycord_voice_remove_ssrc():
    """Work around py-cord dev builds accessing speaking_timer when no reader exists."""
    try:
        from discord.voice.client import VoiceClient
    except Exception as e:
        print_log(f"[WARNING] Could not patch py-cord voice client: {e}")
        return

    if getattr(VoiceClient._remove_ssrc, "_songphagodio_patched", False):
        return

    def _remove_ssrc(self, *, user_id: int) -> None:
        ssrc = self._id_to_ssrc.pop(user_id, None)
        if not ssrc:
            return

        reader = getattr(self, "_reader", None)
        speaking_timer = getattr(reader, "speaking_timer", None)
        if speaking_timer is not None:
            speaking_timer.drop_ssrc(ssrc)
        self._ssrc_to_id.pop(ssrc, None)

    _remove_ssrc._songphagodio_patched = True
    VoiceClient._remove_ssrc = _remove_ssrc


bot = commands.Bot()
configure_error_notifier(bot)
# 봇의 재시작 이유를 저장할 플래그를 새로 만듭니다. (None, 'vpn', 'update')
bot.restart_reason = None

current_cogs_list = []

project_name = "SongphaGodio"
develop_server_ids = [1242846739434569738]


def print_log(text):
    now = str(time.strftime('%Y.%m.%d %H:%M:%S - '))
    print(now + text)


def check_yt_dlp_version():
    """Checks for the latest version of yt-dlp and prints a warning if outdated."""
    try:
        # .strip()을 사용하여 혹시 모를 공백을 제거합니다.
        current_version_str = yt_dlp.version.__version__.strip()

        response = requests.get("https://pypi.org/pypi/yt-dlp/json", timeout=5)
        if response.status_code == 200:
            latest_version_str = response.json()['info']['version'].strip()

            # 문자열을 Version 객체로 변환하여 비교합니다.
            # 이렇게 하면 '2025.9.5'와 '2025.09.05'를 같은 버전으로 올바르게 인식합니다.
            if Version(current_version_str) < Version(latest_version_str):
                print_log(
                    f"[WARNING] yt-dlp is not up to date! Current: {current_version_str}, Latest: {latest_version_str}")
                print_log("[WARNING] Please update with: pip install --upgrade yt-dlp")
            else:
                print_log(f"yt-dlp is up to date. Current version: {current_version_str}")

    except Exception as e:
        print_log(f"[WARNING] Could not check for yt-dlp version: {e}")


def on_rm_error(func, path, exc_info):
    os.chmod(path, stat.S_IWRITE)
    os.unlink(path)


def initial_cog_load():
    global current_cogs_list
    with open("cogs/initial_cog_list.txt") as f:
        for i in f.readlines():
            i = i.strip('\n')
            try:
                bot.load_extension(f"cogs.{i.split('.')[0]}")
                current_cogs_list.append(i)

            except ExtensionNotFound:
                print_log(f"[ERROR] {i} not found.")

            except ExtensionAlreadyLoaded:
                print_log(f"[ERROR] {i} is already loaded.")


def unload_all_cogs():
    global current_cogs_list
    for i in current_cogs_list:
        bot.unload_extension(f"cogs.{i.split('.')[0]}")
    current_cogs_list = []


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def cog_list(ctx):
    try:
        avail_cogs_list = []
        for i in os.listdir("cogs"):
            if i.endswith(".py") and (i not in current_cogs_list):
                avail_cogs_list.append(i)

        await ctx.respond("로드 가능한 cog :" + str(avail_cogs_list) + "\n현제 로드된 cog :" + str(current_cogs_list))
    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def unload_cog(ctx, cog_name: discord.Option(str)):
    global current_cogs_list
    try:
        bot.unload_extension(f"cogs.{cog_name}")
        current_cogs_list.remove(f"{cog_name}.py")
        print_log(f"{cog_name} has been unloaded")
        await ctx.respond(f"{cog_name}을 언로드 하였습니다.")

    except ExtensionNotFound:
        await ctx.respond(f"{cog_name}을 찾을 수 없습니다.")

    except ExtensionNotLoaded:
        await ctx.respond(f"{cog_name}은 로드되어 있지 않습니다.")

    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def load_cog(ctx, cog_name: discord.Option(str)):
    global current_cogs_list
    try:
        bot.load_extension(f"cogs.{cog_name}")
        current_cogs_list.append(f"{cog_name}.py")
        print_log(f"{cog_name} has been loaded")
        await ctx.respond(f"{cog_name}을 로드 하였습니다.")

    except ExtensionNotFound:
        await ctx.respond(f"{cog_name}을 찾을 수 없습니다.")

    except ExtensionAlreadyLoaded:
        await ctx.respond(f"{cog_name}은 이미 로드되어 있습니다.")

    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def reload_bot(ctx):
    try:
        unload_all_cogs()
        initial_cog_load()
        await ctx.respond("봇을 재시작 하였습니다.")
    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def sync_commands(ctx):
    try:
        await bot.sync_commands()
        await ctx.respond("커맨드를 동기화하였습니다.")
    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


@bot.slash_command(default_member_permissions=discord.Permissions(administrator=True), guild_ids=develop_server_ids)
async def update(ctx):
    try:
        if os.path.exists(project_name):    # 다운로드 폴더 확인
            shutil.rmtree(project_name, onerror=on_rm_error)

        os.system(f"git clone https://github.com/cart324/{project_name}")

        for i in os.listdir(project_name):
            # Git metadata belongs to the temporary clone, not the deployment folder.
            if i == ".git":
                continue

            source_path = os.path.join(project_name, i)
            if os.path.isfile(source_path):
                if os.path.exists(i):
                    os.remove(i)
                shutil.move(source_path, i)
                continue

            for (root, dirs, files) in os.walk(source_path):
                for file in files:
                    if os.path.exists(i + "/" + file):
                        os.remove(i + "/" + file)
                    shutil.move(project_name + "/" + i + "/" + file, i + "/" + file)
        shutil.rmtree(project_name, onerror=on_rm_error)

        unload_all_cogs()
        initial_cog_load()

        print_log("update completed")
        await ctx.respond("업데이트가 완료되었습니다.")

    except Exception:
        print_log(f"error has been occurred")
        await send_error_log(traceback.format_exc(), ctx.author.name)
        await ctx.respond("알 수 없는 오류입니다.")


# main 함수는 토큰을 인자로 받도록 다시 수정하는 것이 더 명확합니다.
async def main(token):
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    patch_pycord_voice_remove_ssrc()
    check_yt_dlp_version()
    initial_cog_load()
    print_log(f"{project_name} has been started, loaded cogs : {current_cogs_list}")

    with open('token.txt', 'r') as f:
        token = f.read()

    try:
        asyncio.run(main(token))
    except KeyboardInterrupt:
        print_log("bot has been stopped by admin")
    except Exception:
        if bot.restart_reason is None:
            print_log(f"unhandled exception occurred: \n{traceback.format_exc(limit=None, chain=True)}")

    # bot.close() 호출 후 실행되는 부분입니다.
    # 재시작 이유(restart_reason)에 따라 다른 처리를 합니다.
    if bot.restart_reason == 'vpn':
        print_log("reconnecting to discord server for vpn connection...")
        try:
            # 이 명령은 리눅스 환경에서만 유효합니다.
            os.system('sudo nmcli c up "iptime-vpn"')
            print_log("executed VPN connecting command")
        except Exception as e:
            print_log(f"error occurs while connecting VPN: {e}")

        print_log("restarting bot...")
        os.execv(sys.executable, ['python'] + sys.argv)

    elif bot.restart_reason == 'update':
        # 'update'가 이유일 경우, VPN 연결 시도 없이 바로 재시작합니다.
        print_log("restarting bot to apply updates...")
        os.execv(sys.executable, ['python'] + sys.argv)

    else:
        # restart_reason이 None이면 (정상 종료) 메시지만 출력하고 종료합니다.
        print_log("bot has been stopped successfully")
