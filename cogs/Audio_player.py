import discord
from discord.ext import commands
from collections import defaultdict
import re
import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import time
import requests
import traceback
from modules.error_notifier import send_error_log

from modules.UI_handler import handling_embed, handling_log
from modules.Song_processer import preprocessing_song, youtube_playlist_extract, refresh_song_urls
from modules.Song_processer import async_normalize_volume

BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=4)

LATEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        f'-user_agent "{LATEST_USER_AGENT}" '  # User-Agent 옵션 추가
        '-analyzeduration 20M -probesize 20M '
        '-rw_timeout 30000000'
    ),
    'options': '-vn -compression_level 10'
}

FFMPEG_OPTIONS_LOCAL = {
    'options': '-vn -compression_level 10'
}

# URL 패턴
youtube_pattern = re.compile(r'^(http|https)://((www|m|music)\.)?(youtube\.com|youtu\.be)/')
google_drive_pattern = re.compile(r'^(http|https)://(www\.)?drive\.google\.com/')

VPN_IP = "121.133.106.37"


# 서버별 정보 저장을 위한 클래스
class ServerInfo:
    def __init__(self):
        self.queue = []
        self.song_cache = None
        self.is_loop = False
        self.is_shuffle = False
        self.voice_client = None
        self.embed_channel = None
        self.embed_id = None
        self.log = []
        self.has_sent_403_warning = False  # 403 오류 메시지 발송 여부 플래그


# 참조한 서버ID가 없을 시 ServerInfo를 기본값으로 새로운 키:벨류 쌍 생성
server_info_dict = defaultdict(ServerInfo)


async def play_loop(guild_id: int, bot: commands.Bot) -> None:
    """재생이 끝날 때 마다 queue에서 노래를 하나씩 불러와 반복 실행되는 루프 함수"""
    server_info = server_info_dict[guild_id]
    try:
        # 연결이 의도적으로 종료됐을 때 루프 종료 (ex: /leave)
        if server_info.voice_client is None:
            return

        # 다음 재생곡이 없고 루프 중이 아닐 때 루프 종료
        elif len(server_info.queue) == 0 and not server_info.is_loop:
            server_info.song_cache = None
            await handling_embed(server_info)

        else:
            current_song = None
            # 루프 중일 때는 캐쉬 갱신 안함
            if server_info.is_loop and (server_info.song_cache is not None):
                current_song = server_info.song_cache
            else:
                # 셔플 중 랜덤 팝업
                if server_info.is_shuffle and len(server_info.queue) > 0:
                    random_number = time.localtime().tm_sec % len(server_info.queue)
                    current_song = server_info.queue.pop(random_number - 1)
                elif len(server_info.queue) > 0:
                    current_song = server_info.queue.pop(0)
                else:  # 큐가 비어있으면 루프 종료
                    server_info.song_cache = None
                    await handling_embed(server_info)
                    return

            server_info.song_cache = current_song

            # 스트리밍과 로컬파일 ffmpeg 옵션 구분
            if re.compile(r'^(http|https)://').match(current_song['play_url']):
                option = FFMPEG_OPTIONS
            else:
                option = FFMPEG_OPTIONS_LOCAL

            # ffmpeg로 스트림 재생
            source = discord.FFmpegPCMAudio(current_song['play_url'], **option)
            audio_with_volume = discord.PCMVolumeTransformer(source, volume=current_song['volume'])
            server_info.voice_client.play(audio_with_volume,
                                          after=lambda e: bot.loop.create_task(play_loop(guild_id, bot)))

            # 성공적으로 재생이 시작되면 경고 플래그 초기화
            server_info.has_sent_403_warning = False
            # URL을 갱신했던 재시도 플래그 초기화
            if 'retried' in current_song:
                del current_song['retried']

            await handling_embed(server_info)
            handling_log('play_queue', song_title=current_song['title'], index1=len(server_info.queue))

    except Exception as e:
        error_log = traceback.format_exc(limit=None, chain=True)

        # 403 Forbidden 오류 처리
        if "403 Forbidden" in str(e) or "403 Forbidden" in error_log:
            if server_info.song_cache:
                # 1. 처음 오류가 발생했을 경우 (재시도 플래그가 없을 때)
                if not server_info.song_cache.get('retried', False):
                    print(f"403 Forbidden error in guild {guild_id}. Silently refreshing URLs and retrying.")
                    server_info.song_cache['retried'] = True
                    # 현재 곡을 다시 큐의 최상단에 추가 (재시도용)
                    server_info.queue.insert(0, server_info.song_cache)
                    server_info.song_cache = None

                    try:
                        # 큐에 있는 모든 인터넷 링크 곡의 URL 갱신 (조용하게 진행)
                        await refresh_song_urls(server_info.queue)
                    except Exception as refresh_err:
                        print(f"Failed to refresh URLs: {refresh_err}")

                # 2. 링크를 갱신하고 재시도했음에도 또 실패했을 경우 (yt_dlp 버전/구조 문제 의심)
                else:
                    print(f"Song already retried and failed, skipping: {server_info.song_cache.get('title')}")

                    # 도배 방지를 위해 서버당 1번만 발송
                    if not server_info.has_sent_403_warning:
                        server_info.has_sent_403_warning = True

                        # 서버 채널에 안내 메시지 전송
                        if server_info.embed_channel:
                            try:
                                await server_info.embed_channel.send(
                                    "곡 재생에 실패하여 곡을 건너뜁니다."
                                )
                            except Exception as channel_error:
                                print(f"Failed to send 403 error message to channel: {channel_error}")

                        # 관리자에게 DM 발송 (yt_dlp 이슈 의심)
                        try:
                            guild = bot.get_guild(guild_id)
                            guild_name = guild.name if guild else "알 수 없는 서버"
                            requester_name = server_info.song_cache.get('requester', '알 수 없는 사용자')

                            dm_message = (
                                f""
                                f"HTTP 403 Forbidden 오류 발생 (재시도 실패)\n"
                                f"서버 = {guild_name}\n"
                                f"요청자 = {requester_name}\n"
                                f"곡 제목 = {server_info.song_cache.get('title', '알 수 없음')}\n"
                                f"{error_log}"
                                f""
                            )
                            await send_error_log(dm_message)
                        except Exception:
                            pass

                    # 실패한 곡이므로 건너뛰기 위해 캐시를 완전히 비움
                    server_info.song_cache = None

            # 오류 처리(갱신 후 재진입 또는 스킵)가 끝나면 다음 재생을 위해 루프 재호출
            bot.loop.create_task(play_loop(guild_id, bot))

        # 그 외 다른 오류 처리 (기존 로직 유지)
        else:
            await send_error_log(traceback.format_exc())
            if server_info.embed_channel:
                try:
                    await server_info.embed_channel.send("재생 중 알 수 없는 오류가 발생하여 플레이어를 중지합니다.")
                except Exception as channel_e:
                    print(f"Failed to send message to channel: {channel_e}")

            # Cleanup
            if server_info.voice_client and server_info.voice_client.is_connected():
                server_info.voice_client.stop()
            server_info.queue.clear()
            server_info.song_cache = None
            try:
                await handling_embed(server_info)
            except Exception as embed_e:
                print(f"Failed to update embed during play_loop cleanup: {embed_e}")


class AudioPlayer(commands.Cog, name="audio_player"):
    def __init__(self, bot):
        self.bot = bot
        self._disconnect_locks = defaultdict(asyncio.Lock)

    async def _close_player(self, guild: discord.Guild, *, disconnect: bool, auto_leave: bool = False) -> bool:
        """Finalize one guild player exactly once across command and voice-state events."""
        async with self._disconnect_locks[guild.id]:
            server_info = server_info_dict[guild.id]
            voice_client = guild.voice_client or server_info.voice_client
            is_connected = voice_client is not None and voice_client.is_connected()

            # A previous concurrent event already finalized this player. Pycord can briefly
            # retain a disconnected VoiceClient object, so object existence alone is insufficient.
            if (
                not is_connected
                and server_info.voice_client is None
                and server_info.embed_channel is None
                and server_info.embed_id is None
            ):
                return False

            embed_channel = server_info.embed_channel
            embed_id = server_info.embed_id

            # Stop playback callbacks from starting another track while closing.
            server_info.voice_client = None
            server_info.queue.clear()
            server_info.song_cache = None

            if embed_channel is not None and embed_id is not None:
                try:
                    message = await embed_channel.fetch_message(embed_id)
                    await message.edit(embed=discord.Embed(title="플레이어가 종료되었습니다."), view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
                except Exception:
                    await send_error_log(traceback.format_exc())

            if disconnect and is_connected:
                await voice_client.disconnect()

            server_info_dict[guild.id] = ServerInfo()
            handling_log('player_end', index1=guild.name)
            if auto_leave:
                handling_log('auto_leave')
            return True

    async def _ensure_voice_connection(self, ctx: discord.ApplicationContext) -> bool:
        """봇이 음성 채널에 연결되어 있는지 확인하고, 연결되어 있지 않으면 연결을 시도합니다."""
        server_info = server_info_dict[ctx.guild.id]
        if ctx.voice_client:
            return True

        if not ctx.author.voice or not ctx.author.voice.channel:
            await ctx.followup.send("먼저 음성 채널에 접속해주세요.", ephemeral=True)
            return False

        await ctx.author.voice.channel.connect()
        server_info.voice_client = ctx.voice_client
        server_info.embed_channel = ctx.channel
        await handling_embed(server_info)
        handling_log('player_start', index1=ctx.guild.name)
        return True

    async def _add_song_to_queue(self, author: discord.Member, url: str) -> dict | None:
        """노래를 처리하고 큐에 추가할 노래 정보를 반환합니다."""
        song_info_dict = await preprocessing_song(url, self.bot)
        if song_info_dict.get('play_url') is None:
            return None

        song_info_dict['requester'] = author.display_name
        handling_log('play', user_name=author.name, song_title=song_info_dict['title'])
        return song_info_dict

    @commands.slash_command()
    async def play(self, ctx, url):
        """음악을 재생목록에 추가합니다. 유튜브, 구글 드라이브, 온라인 파일 링크(ex:디스코드 첨부파일링크)를 지원합니다."""
        try:
            url = url.strip("‪")
            server_info = server_info_dict[ctx.guild.id]
            await ctx.defer(ephemeral=True)
            handling_log('play_init', user_name=ctx.author.name)

            if not ctx.voice_client and (not ctx.author.voice or not ctx.author.voice.channel):
                await ctx.followup.send("먼저 음성 채널에 접속해주세요.", ephemeral=True)
                return

            voice_connection_task = asyncio.create_task(self._ensure_voice_connection(ctx))
            song_info_task = asyncio.create_task(self._add_song_to_queue(ctx.author, url))
            is_connected, song_info_dict = await asyncio.gather(voice_connection_task, song_info_task)

            if not is_connected:
                return

            if song_info_dict is None:
                await ctx.followup.send("알 수 없는 링크입니다.", ephemeral=True)
                return

            server_info.queue.append(song_info_dict)

            # 현재 재생 중이 아닐 경우 플레이 루프 시작
            if not ctx.voice_client.is_playing():
                await play_loop(ctx.guild.id, self.bot)
            else:
                await handling_embed(server_info)

            await ctx.followup.send(
                f"노래를 재생목록에 추가하였습니다!\n"
                f"제목: {song_info_dict['title']}\n"
                f"URL: {song_info_dict['original_url']}",
                ephemeral=True
            )
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def playlist(self, ctx, playlist_url):
        """유튜브 플레이리스트에서 곡을 불러옵니다. 한곡에 약 4초씩 소요되니 차분히 기다려주세요."""
        try:
            server_info = server_info_dict[ctx.guild.id]
            await ctx.defer(ephemeral=True)

            if not await self._ensure_voice_connection(ctx):
                return

            # 유튜브 링크일 경우
            if youtube_pattern.match(playlist_url):
                loop = asyncio.get_event_loop()
                song_list = await loop.run_in_executor(BACKGROUND_EXECUTOR, youtube_playlist_extract, playlist_url)

                if song_list is False:
                    await ctx.followup.send("정보를 불러오는데 실패하였습니다.", ephemeral=True)
                    return
            else:
                # 알 수 없는 인터넷 링크일 경우 종료
                if re.compile(r'^(http|https)://').match(playlist_url):
                    await ctx.followup.send("잘못된 링크입니다.", ephemeral=True)
                    return

                song_list = []
                try:
                    for filename in os.listdir(playlist_url):
                        file_path = os.path.join(playlist_url, filename)
                        if os.path.isfile(file_path):
                            song_list.append((file_path, os.path.splitext(filename)[0]))
                except FileNotFoundError:
                    await ctx.followup.send("로컬 경로를 찾을 수 없습니다.", ephemeral=True)
                    return

            handling_log('playlist', user_name=ctx.author.name, index1=len(song_list))
            added_songs_text = ""

            is_first_song_added = not ctx.voice_client.is_playing()

            for url, title in song_list:
                song_info_dict = await self._add_song_to_queue(ctx.author, url)
                if song_info_dict:
                    server_info.queue.append(song_info_dict)
                    added_songs_text += f"{len(added_songs_text.splitlines()) + 1}. {title}\n"

            # 재생 중이 아니었고, 곡이 추가되었을 경우 플레이 루프 시작
            if is_first_song_added and len(server_info.queue) > 0:
                await play_loop(ctx.guild.id, self.bot)

            await handling_embed(server_info)

            if not added_songs_text:
                await ctx.followup.send("플레이리스트에 추가할 수 있는 곡이 없습니다.", ephemeral=True)
                return

            embed = discord.Embed(title="추가된 곡 목록", description=added_songs_text)
            await ctx.followup.send(embed=embed, ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    # ... (delete_from_queue, skip, set_volume, auto_volume, stop, pause_resume, loop, shuffle, re_embed, leave, on_voice_state_update 등 나머지 코드는 변경 없이 그대로 사용)
    @commands.slash_command()
    async def delete_from_queue(self, ctx, start_index: discord.Option(int),
                                end_index: discord.Option(int, default=None)):
        """재생목록에 등록된 곡을 제거합니다. end_index를 입력하지 않으면 한 곡만 제거합니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            start_index -= 1
            end_index = (start_index + 1) if end_index is None else end_index  # end_index 입력이 없을 시 자동 할당

            if start_index < 0 or end_index < 0:
                await ctx.respond("입력은 1보다 작을 수 없습니다.", ephemeral=True)
                return
            elif start_index > len(server_info.queue) or end_index > len(server_info.queue):
                await ctx.respond("인덱스 범위를 벗어난 입력입니다.", ephemeral=True)
                return
            elif start_index >= end_index:
                await ctx.respond("종료 인덱스는 시작 인덱스보다 먼저일 수 없습니다.", ephemeral=True)
                return

            del server_info.queue[start_index:end_index]

            # 다음 재생 곡을 제거 하였을 때 임배드 수정
            if start_index == 0:
                await handling_embed(server_info)

            if start_index + 1 == end_index:
                await ctx.respond(f"{start_index + 1}번 곡을 재생목록에서 제거하였습니다.", ephemeral=True)
            else:
                await ctx.respond(f"{start_index + 1}번부터 {end_index}번 곡을 재생목록에서 제거하였습니다.", ephemeral=True)

            handling_log('delete_from_queue', user_name=ctx.author.name, index1=start_index, index2=end_index)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def skip(self, ctx):
        """현재 곡을 건너뜁니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if ctx.voice_client and ctx.voice_client.is_playing():
                ctx.voice_client.stop()
                server_info.log.append((ctx.author.display_name, 'skip', None, time.time()))  # 사용자 로깅
                await ctx.respond("현재 곡을 건너뜁니다.", ephemeral=True)
            else:
                await ctx.respond("재생 중인 곡이 없습니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def set_volume(self, ctx, volume_percent: discord.Option(int, description="볼륨을 %로 입력하세요 (0-200).")):
        """현재 재생 중인 곡의 볼륨을 조정합니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if ctx.voice_client and ctx.voice_client.is_playing() and ctx.voice_client.source:
                if not (0 <= volume_percent <= 200):
                    await ctx.respond("볼륨은 0에서 200 사이의 값으로 설정해야 합니다.", ephemeral=True)
                    return

                # PCMVolumeTransformer의 볼륨을 직접 조절합니다.
                # 기본 볼륨(곡마다 정규화된 값)에 사용자 설정값을 곱합니다.
                base_volume = server_info.song_cache.get('volume', 0.2)
                new_volume = base_volume * (volume_percent / 100.0)

                # discord.py의 PCMVolumeTransformer 볼륨 최대값은 2.0입니다.
                ctx.voice_client.source.volume = min(new_volume, 2.0)

                # UI 표시를 위해 사용자가 입력한 퍼센트 값을 저장합니다.
                server_info.song_cache['volume_change'] = volume_percent

                server_info.log.append((ctx.author.display_name, 'set_volume', volume_percent, time.time()))
                await handling_embed(server_info)  # 변경된 볼륨 정보를 임베드에 반영합니다.
                await ctx.respond(f"현재 곡의 볼륨을 {volume_percent}%로 설정했습니다.", ephemeral=True)
            else:
                await ctx.respond("재생 중인 오디오가 없습니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def auto_volume(self, ctx):
        """현재 재생 중인 곡의 볼륨을 자동으로 적절하게 조절합니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if not (ctx.voice_client and ctx.voice_client.is_playing() and server_info.song_cache):
                await ctx.respond("재생 중인 곡이 없습니다.", ephemeral=True)
                return

            await ctx.defer(ephemeral=True)

            play_url = server_info.song_cache.get('play_url')
            if not play_url:
                await ctx.followup.send("볼륨을 조절할 수 없는 곡입니다.", ephemeral=True)
                return

            new_base_volume = await async_normalize_volume(play_url)

            server_info.song_cache['volume'] = new_base_volume
            ctx.voice_client.source.volume = new_base_volume

            server_info.log.append((ctx.author.display_name, 'auto_volume', None, time.time()))
            await handling_embed(server_info)
            await ctx.followup.send("현재 곡의 볼륨을 자동으로 조절했습니다.", ephemeral=True)
            handling_log('auto_volume', user_name=ctx.author.name, index1=new_base_volume)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def stop(self, ctx):
        """재생을 중지하고 재생목록 초기화합니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if ctx.voice_client:
                server_info.queue = []
                ctx.voice_client.stop()
                server_info.log.append((ctx.author.display_name, 'stop', None, time.time()))  # 사용자 로깅
                await ctx.respond("재생을 중지하고 재생목록을 초기화했습니다.", ephemeral=True)
            else:
                await ctx.respond("재생 중이 아닙니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def pause_resume(self, ctx):
        """재생을 일시정지 하거나 재개합니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if ctx.voice_client and ctx.voice_client.is_playing():
                ctx.voice_client.pause()
                await handling_embed(server_info)
                server_info.log.append((ctx.author.display_name, 'pause', None, time.time()))  # 사용자 로깅
                await ctx.respond("재생을 일시정지 하였습니다.", ephemeral=True)

            elif ctx.voice_client and ctx.voice_client.is_paused():
                ctx.voice_client.resume()
                await handling_embed(server_info)
                server_info.log.append((ctx.author.display_name, 'resume', None, time.time()))  # 사용자 로깅
                await ctx.respond("재생을 재개하였습니다.", ephemeral=True)

            else:
                await ctx.respond("재생 중이 아닙니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def loop(self, ctx):
        """현재 곡의 루프를 켜고 끕니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if server_info.is_loop:
                server_info.is_loop = False
                await handling_embed(server_info)
                await ctx.respond("루프가 꺼졌습니다.", ephemeral=True)

            else:
                if ctx.voice_client and (server_info.song_cache is not None) and ctx.voice_client.is_playing():
                    server_info.is_loop = True
                    await handling_embed(server_info)
                    await ctx.respond("루프가 켜졌습니다.", ephemeral=True)
                else:
                    await ctx.respond("재생 중이 아닙니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def shuffle(self, ctx):
        """재생목록 셔플을 켜고 끕니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if server_info.is_shuffle:
                server_info.is_shuffle = False
                await handling_embed(server_info)
                await ctx.respond("셔플이 꺼졌습니다.", ephemeral=True)

            else:
                if ctx.voice_client and (server_info.song_cache is not None) and ctx.voice_client.is_playing():
                    server_info.is_shuffle = True
                    await handling_embed(server_info)
                    await ctx.respond("셔플이 켜졌습니다.", ephemeral=True)
                else:
                    await ctx.respond("재생 중이 아닙니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def re_embed(self, ctx):
        """플레이어 UI를 지웠다 다시 보내어 채팅의 아래에 위치시킵니다."""
        try:
            server_info = server_info_dict[ctx.guild.id]

            if server_info.embed_id is None:
                await ctx.respond("현재 활성화된 플레이어가 없습니다.", ephemeral=True)
            else:
                message = await server_info.embed_channel.fetch_message(server_info.embed_id)
                await message.delete()
                server_info.embed_id = None
                server_info.embed_channel = ctx.channel
                await handling_embed(server_info)
                await ctx.respond("플레이어를 갱신하였습니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.slash_command()
    async def leave(self, ctx):
        """노래 재생을 중단하고 음성 채널에서 퇴장합니다."""
        try:
            if await self._close_player(ctx.guild, disconnect=True):
                await ctx.respond("봇이 음성 채널에서 퇴장하였습니다.", ephemeral=True)
            else:
                await ctx.respond("봇이 음성 채널에 있지 않습니다.", ephemeral=True)
        except Exception:
            await send_error_log(traceback.format_exc(), ctx.author.name)

            if ctx.response.is_done():
                await ctx.followup.send("알 수 없는 오류입니다.", ephemeral=True)
            else:
                await ctx.respond("알 수 없는 오류입니다.", ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """
        음성 상태 변경 이벤트를 처리합니다.
        사람이 없는 음성 채널에서 봇이 나가도록 처리합니다.
        """
        try:
            guild = member.guild

            # Handle an external kick/forced disconnect even after guild.voice_client is cleared.
            if self.bot.user and member.id == self.bot.user.id:
                if before.channel is not None and after.channel is None:
                    await self._close_player(guild, disconnect=False)
                return

            voice_client = guild.voice_client
            if not voice_client:
                return

            voice_channel = voice_client.channel
            if (
                voice_channel is None
                or before.channel != voice_channel
                or after.channel == before.channel
            ):
                return

            human_members = [m for m in voice_channel.members if not m.bot]
            if not human_members:
                await self._close_player(guild, disconnect=True, auto_leave=True)
        except Exception:
            await send_error_log(traceback.format_exc())


def setup(bot):
    bot.add_cog(AudioPlayer(bot))
