import discord
from discord.ext import commands
from collections import defaultdict
import re
import asyncio
import os
import time
import requests
import traceback
from modules.error_notifier import send_error_log

from modules.UI_handler import handling_embed, handling_log
from modules.Song_processer import EXTRACTION_EXECUTOR, preprocessing_song, youtube_playlist_extract, refresh_song_urls
from modules.Song_processer import async_normalize_volume
from modules.ffmpeg_log_filter import (
    AccessDeniedClassification,
    FilteredFFmpegPCMAudio,
    translate_http_403_cause,
)

LATEST_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        '-thread_queue_size 4096 '
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
PLAYLIST_DESCRIPTION_LIMIT = 3800


def _pin_playback_workers(voice_client, source) -> None:
    """Keep FFmpeg and Discord's voice sender on the CPU reserved from extraction."""
    if not hasattr(os, 'sched_getaffinity') or not hasattr(os, 'sched_setaffinity'):
        return

    try:
        available_cpus = sorted(os.sched_getaffinity(0))
    except OSError as error:
        print(f"[WARNING] Failed to inspect playback CPU affinity: {error}")
        return

    if len(available_cpus) < 2:
        return

    playback_cpus = {available_cpus[0]}
    ffmpeg_process = getattr(source, '_process', None)
    player_thread = getattr(voice_client, '_player', None)
    targets = (
        ('FFmpeg', getattr(ffmpeg_process, 'pid', None)),
        ('voice sender', getattr(player_thread, 'native_id', None)),
    )

    for target_name, target_id in targets:
        if target_id is None:
            continue
        try:
            os.sched_setaffinity(target_id, playback_cpus)
        except ProcessLookupError:
            pass
        except OSError as error:
            print(f"[WARNING] Failed to pin {target_name} CPU affinity: {error}")


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
        self.playback_id = 0
        self.playback_end_token = None
        self.url_refresh_token = None


# 참조한 서버ID가 없을 시 ServerInfo를 기본값으로 새로운 키:벨류 쌍 생성
server_info_dict = defaultdict(ServerInfo)


async def _refresh_and_retry_expired_stream(
    guild_id: int,
    bot: commands.Bot,
    server_info: ServerInfo,
    current_song: dict,
    playback_id: int,
    playback_end_token: object,
) -> bool | None:
    """Refresh one expired playback snapshot and retry it once.

    Returns True when a retry was started, False when refresh failed, and None
    when the player was stopped or replaced while refresh was in progress.
    """
    previous_url = current_song.get('play_url')
    queue_reference = server_info.queue
    refresh_token = object()
    refresh_targets = []
    seen_song_ids = set()
    for song in (current_song, *list(queue_reference)):
        song_id = id(song)
        if song_id not in seen_song_ids:
            seen_song_ids.add(song_id)
            refresh_targets.append(song)

    current_song['retried'] = True
    server_info.url_refresh_token = refresh_token
    refresh_failed = False

    try:
        try:
            await refresh_song_urls(refresh_targets)
        except Exception:
            refresh_failed = True
            await send_error_log(traceback.format_exc())

        is_still_current = (
            server_info_dict.get(guild_id) is server_info
            and server_info.voice_client is not None
            and server_info.playback_id == playback_id
            and server_info.playback_end_token is playback_end_token
            and server_info.song_cache is current_song
            and server_info.queue is queue_reference
            and server_info.url_refresh_token is refresh_token
        )
        if not is_still_current:
            return None

        refreshed_url = current_song.get('play_url')
        if refresh_failed or not refreshed_url or refreshed_url == previous_url:
            return False

        # Clear the guard immediately before entering the retry loop. There is no
        # event-loop yield between these operations, so another command cannot
        # start a competing source in the middle.
        server_info.url_refresh_token = None
        server_info.playback_end_token = None
        await play_loop(guild_id, bot, retry_song=current_song)
        return True
    finally:
        if (
            server_info_dict.get(guild_id) is server_info
            and server_info.url_refresh_token is refresh_token
        ):
            server_info.url_refresh_token = None


async def _handle_playback_end(
    guild_id: int,
    bot: commands.Bot,
    server_info: ServerInfo,
    current_song: dict,
    playback_id: int,
    playback_end_token: object,
    classification: AccessDeniedClassification | None,
) -> None:
    """Handle one source completion on the bot event loop."""
    if server_info_dict.get(guild_id) is not server_info or server_info.voice_client is None:
        return

    is_current_playback = (
        server_info.playback_id == playback_id
        and server_info.playback_end_token is playback_end_token
        and server_info.song_cache is current_song
    )
    if not is_current_playback:
        return

    if classification is None:
        current_song.pop('retried', None)
        server_info.playback_end_token = None
        await play_loop(guild_id, bot)
        return

    if (
        classification.cause == 'expired_stream_url'
        and not current_song.get('retried', False)
    ):
        retry_result = await _refresh_and_retry_expired_stream(
            guild_id,
            bot,
            server_info,
            current_song,
            playback_id,
            playback_end_token,
        )
        if retry_result is not False:
            return

    notification_message = None

    if server_info.embed_channel:
        title = current_song.get('title', '알 수 없는 곡')
        cause = translate_http_403_cause(classification.cause)
        notification_message = f"{title} 재생하는데 실패했습니다. ({cause})"

    # A failed looped song must not be selected again by the next play_loop call.
    server_info.song_cache = None
    server_info.playback_end_token = None

    # Start the next song before waiting for the Discord message request. A newer
    # playback means another command already continued the queue for us.
    await play_loop(guild_id, bot)

    if (
        notification_message is not None
        and server_info_dict.get(guild_id) is server_info
        and server_info.voice_client is not None
        and server_info.embed_channel is not None
    ):
        try:
            await server_info.embed_channel.send(
                notification_message,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass
        except Exception:
            await send_error_log(traceback.format_exc())


async def play_loop(guild_id: int, bot: commands.Bot, retry_song: dict | None = None) -> None:
    """재생이 끝날 때 마다 queue에서 노래를 하나씩 불러와 반복 실행되는 루프 함수"""
    server_info = server_info_dict[guild_id]
    try:
        # URL 갱신 중 호출된 일반 재생 루프는 갱신 작업이 이어서 처리합니다.
        if (
            retry_song is None
            and (
                server_info.playback_end_token is not None
                or server_info.url_refresh_token is not None
            )
        ):
            return

        # 연결이 의도적으로 종료됐을 때 루프 종료 (ex: /leave)
        if server_info.voice_client is None:
            return

        # 여러 명령이 동시에 재생 루프를 요청해도 활성 source를 덮어쓰지 않습니다.
        if server_info.voice_client.is_playing() or server_info.voice_client.is_paused():
            return

        # 다음 재생곡이 없고 루프 중이 아닐 때 루프 종료
        elif retry_song is None and len(server_info.queue) == 0 and not server_info.is_loop:
            server_info.song_cache = None
            await handling_embed(server_info)

        else:
            current_song = None
            if retry_song is not None:
                current_song = retry_song
            # 루프 중일 때는 캐쉬 갱신 안함
            elif server_info.is_loop and (server_info.song_cache is not None):
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

            # ffmpeg stderr 원문은 터미널에 노출하지 않고 재생 종료 시 분류합니다.
            guild = bot.get_guild(guild_id)
            source = FilteredFFmpegPCMAudio(
                current_song['play_url'],
                song_title=current_song.get('title'),
                guild_name=guild.name if guild else str(guild_id),
                stream_metadata=current_song.get('stream_metadata'),
                expected_ip=VPN_IP,
                **option,
            )
            audio_with_volume = discord.PCMVolumeTransformer(source, volume=current_song['volume'])
            server_info.playback_id += 1
            playback_id = server_info.playback_id
            playback_end_token = object()
            server_info.playback_end_token = playback_end_token

            def after_playback(_error):
                # Pycord는 자체 cleanup보다 after를 먼저 호출하므로 여기서 FFmpeg를 먼저 종료합니다.
                try:
                    audio_with_volume.cleanup()
                finally:
                    classification = source.consume_access_denied_classification()

                    def schedule_next_song():
                        if not bot.loop.is_closed():
                            bot.loop.create_task(
                                _handle_playback_end(
                                    guild_id,
                                    bot,
                                    server_info,
                                    current_song,
                                    playback_id,
                                    playback_end_token,
                                    classification,
                                )
                            )

                    try:
                        bot.loop.call_soon_threadsafe(schedule_next_song)
                    except RuntimeError:
                        pass

            try:
                server_info.voice_client.play(audio_with_volume, after=after_playback)
                _pin_playback_workers(server_info.voice_client, source)
            except Exception:
                audio_with_volume.cleanup()
                classification = source.consume_access_denied_classification()
                if classification is not None:
                    await _handle_playback_end(
                        guild_id,
                        bot,
                        server_info,
                        current_song,
                        playback_id,
                        playback_end_token,
                        classification,
                    )
                    return
                raise

            await handling_embed(server_info)
            handling_log('play_queue', song_title=current_song['title'], index1=len(server_info.queue))

    except Exception:
        await send_error_log(traceback.format_exc())
        if server_info.embed_channel:
            try:
                await server_info.embed_channel.send("재생 중 알 수 없는 오류가 발생하여 플레이어를 중지합니다.")
            except Exception:
                await send_error_log(traceback.format_exc())

        # Cleanup
        if server_info.voice_client and server_info.voice_client.is_connected():
            server_info.voice_client.stop()
        server_info.queue.clear()
        server_info.song_cache = None
        server_info.playback_end_token = None
        server_info.url_refresh_token = None
        try:
            await handling_embed(server_info)
        except Exception:
            await send_error_log(traceback.format_exc())


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
            server_info.playback_end_token = None
            server_info.url_refresh_token = None
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
            handling_log('play', user_name=ctx.author.name, song_title=song_info_dict['title'])

            # 현재 재생 중이 아닐 경우 플레이 루프 시작
            if (
                not ctx.voice_client.is_playing()
                and not ctx.voice_client.is_paused()
                and server_info.song_cache is None
                and server_info.playback_end_token is None
                and server_info.url_refresh_token is None
            ):
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
                song_list = await loop.run_in_executor(EXTRACTION_EXECUTOR, youtube_playlist_extract, playlist_url)

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
            added_song_count = 0
            omitted_song_count = 0
            playlist_cancelled = False

            for playlist_index, (url, title) in enumerate(song_list, start=1):
                if (
                    server_info_dict.get(ctx.guild.id) is not server_info
                    or server_info.voice_client is None
                ):
                    playlist_cancelled = True
                    break

                song_info_dict = await self._add_song_to_queue(ctx.author, url)

                # /leave가 추출 도중 실행됐다면 방금 추출한 곡도 큐에 넣지 않습니다.
                if (
                    server_info_dict.get(ctx.guild.id) is not server_info
                    or server_info.voice_client is None
                ):
                    playlist_cancelled = True
                    break

                if song_info_dict:
                    server_info.queue.append(song_info_dict)
                    handling_log(
                        'playlist_play',
                        user_name=ctx.author.name,
                        song_title=song_info_dict['title'],
                        index1=playlist_index,
                        index2=len(song_list),
                    )
                    added_song_count += 1
                    song_line = f"{added_song_count}. {title}\n"
                    if (
                        omitted_song_count > 0
                        or len(added_songs_text) + len(song_line) > PLAYLIST_DESCRIPTION_LIMIT
                    ):
                        omitted_song_count += 1
                    else:
                        added_songs_text += song_line

                    # 첫 곡이 준비되는 즉시 재생하고, 재생목록 추출은 계속 진행합니다.
                    if (
                        server_info.song_cache is None
                        and server_info.playback_end_token is None
                        and server_info.url_refresh_token is None
                        and not server_info.voice_client.is_playing()
                        and not server_info.voice_client.is_paused()
                    ):
                        await play_loop(ctx.guild.id, self.bot)

            if (
                playlist_cancelled
                or server_info_dict.get(ctx.guild.id) is not server_info
                or server_info.voice_client is None
            ):
                await ctx.followup.send(
                    "봇이 음성 채널에서 퇴장하여 플레이리스트 추가를 중단했습니다.",
                    ephemeral=True,
                )
                return

            if omitted_song_count > 0:
                omitted_text = f"... 외 {omitted_song_count}곡"
                available_length = PLAYLIST_DESCRIPTION_LIMIT - len(omitted_text) - 1
                added_songs_text = added_songs_text[:max(0, available_length)].rstrip()
                added_songs_text = f"{added_songs_text}\n{omitted_text}" if added_songs_text else omitted_text

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
                server_info.playback_end_token = None
                server_info.url_refresh_token = None
                server_info.queue = []
                server_info.is_loop = False
                server_info.song_cache = None
                ctx.voice_client.stop()
                server_info.log.append((ctx.author.display_name, 'stop', None, time.time()))  # 사용자 로깅
                await ctx.respond("재생을 중지하고 재생목록을 초기화했습니다.", ephemeral=True)
                await handling_embed(server_info)
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
