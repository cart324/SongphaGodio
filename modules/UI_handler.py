import discord
import re
import time
import traceback
from modules.error_notifier import send_error_log


async def handling_embed(server_info) -> None:
    """임베드 처리"""
    try:
        song_cache = server_info.song_cache

        # 재생목록이 비었을 때
        if song_cache is None:
            embed = discord.Embed(title="현재 재생중인 곡이 없습니다.", description="`/play`를 사용하여 노래를 틀어보세요!")
            view = None
        # 그 외
        else:
            # 재생 볼륨 처리
            volume_text = "" if song_cache.get('volume_change', 100) == 100 else f"(볼륨 조정: {song_cache.get('volume_change', 100)}%)"

            # 현재 곡 정보 입력
            if re.compile(r'^(http|https)://').match(server_info.song_cache['play_url']):
                embed = discord.Embed(
                    title="현재 재생중인 곡" + volume_text,
                    description=f"[{song_cache['title']}]({song_cache['original_url']})  [{song_cache['duration']}]\n​"
                )
            else:
                embed = discord.Embed(
                    title="현재 재생중인 곡" + volume_text,
                    description=f"{song_cache['title']}  [{song_cache['duration']}]\n​"
                )

            # 현재 재생 곡 커버 추가
            embed.set_image(url=song_cache['cover'])

            # 다음 곡 제목 추출
            if len(server_info.queue) == 0:
                next_title = "없음"
            else:
                next_title = server_info.queue[0]['title']

            # 루프, 셔플일 경우의 다음 재생 곡 정보
            if server_info.is_loop:
                embed.add_field(name="다음 재생곡", value="현재 루프가 켜져있습니다.")
            elif server_info.is_shuffle:
                embed.add_field(name="다음 재상곡", value="현재 셔플이 켜져있습니다.")
            else:
                embed.add_field(name="다음 재생곡", value=next_title)

            # 신청자 표시
            embed.set_footer(text=f"신청자 : {song_cache['requester']}")

            # 일시정지일 경우의 하단 버튼 배열
            if server_info.voice_client and server_info.voice_client.is_paused():
                view = PausingControlButton(server_info.queue, server_info)
            else:
                view = PlayingControlButton(server_info.queue, server_info)

        # 첫 임베드 생성일 경우 새 메시지 보내기
        if server_info.embed_id is None:
            if server_info.embed_channel:
                message = await server_info.embed_channel.send(embed=embed, view=view)
                server_info.embed_id = message.id
        else:
            try:
                message = await server_info.embed_channel.fetch_message(server_info.embed_id)
                await message.edit(embed=embed, view=view)
            except discord.NotFound:
                # If message is not found, create a new one
                if server_info.embed_channel:
                    message = await server_info.embed_channel.send(embed=embed, view=view)
                    server_info.embed_id = message.id
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if server_info and server_info.embed_channel:
            try:
                await server_info.embed_channel.send("플레이어 UI를 업데이트하는 중 오류가 발생했습니다. 일부 기능이 정상적으로 보이지 않을 수 있습니다.")
            except Exception as e:
                print(f"Failed to send error message to embed channel: {e}")


def handling_log(request_type, user_name=None, song_title=None, index1=None, index2=None):
    now = str(time.strftime('%Y.%m.%d %H:%M:%S - '))

    log_text_dict = {
        'player_start': f'player has been started at {index1}',
        'player_end': f'player has been terminated at {index1}',
        'play': 'adding song success',
        'play_init': 'song request received',
        'play_queue': f'playing song success({index1} remain)',
        'playlist': f'playlist request received(total {index1} songs)',
        'delete_from_queue': f'deleting song success[{index1}->{index2}]',
        'auto_volume': f'auto volume adjusting success({index1})',
        'playlist_play': f'adding song success(from playlist)[{index1}/{index2}]',
        'auto_leave': 'disconnecting success due to no remaining human in voice channel',
        'debug': f'debug point {index1}'
    }

    log_text = log_text_dict.get(request_type, f"Unknown request type: {request_type}")
    user_text = (", user = " + user_name) if (user_name is not None) else ""
    title_text = (" || " + song_title) if (song_title is not None) else ""
    print(now + log_text + user_text + title_text)


# --------------------버튼 코드--------------------

class PlayerUISuperClass(discord.ui.View):
    def __init__(self, queue_list, server_info):
        super().__init__(timeout=None)
        self.server_info = server_info
        # 재생목록에 곡이 1개 이상 존재할 때 재생목록 추가
        if len(queue_list) > 0:
            options = []
            
            items_to_show = queue_list
            if len(queue_list) > 25:
                items_to_show = queue_list[:24]

            for index, item in enumerate(items_to_show, start=1):
                title = item.get('title', '제목 없음')
                requester = item.get('requester', '알 수 없음')
                
                prefix = f"{index}. "
                suffix = f" || {requester}"
                
                max_title_len = 100 - (len(prefix) + len(suffix))
                
                if len(title) > max_title_len:
                    if max_title_len >= 4:
                        title = title[:max_title_len - 3] + "..."
                    elif max_title_len >= 0:
                        title = title[:max_title_len]
                    else:
                        title = ""

                label = prefix + title + suffix
                options.append(discord.SelectOption(label=label, value=str(index)))

            if len(queue_list) > 25:
                options.append(discord.SelectOption(label=f"위 목록 외 {len(queue_list)-24}개의 재생목록이 있습니다.", value="more"))

            # Select 컴포넌트 생성
            select = discord.ui.Select(
                placeholder="재생목록을 보려면 클릭하세요.", min_values=1, max_values=1, options=options
            )

            # Select의 콜백 함수 정의
            async def select_callback(interaction: discord.Interaction):
                try:
                    if not select.values or select.values[0] == "more":
                        await interaction.response.defer(ephemeral=True, thinking=False)
                        return

                    index = int(select.values[0])
                    song = queue_list[index - 1]
                    await interaction.response.send_message(
                        f"{index}번 곡의 정보입니다.\n제목: {song.get('title', '제목 없음')}\nurl: {song.get('original_url', 'URL 없음')}", ephemeral=True
                    )
                except Exception:
                    error_log = traceback.format_exc(limit=None, chain=True)
                    await send_error_log(traceback.format_exc())
                    if not interaction.response.is_done():
                        await interaction.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
                    else:
                        await interaction.followup.send("알 수 없는 오류입니다.", ephemeral=True)

            select.callback = select_callback
            self.add_item(select)


class PlayingControlButton(PlayerUISuperClass):
    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_play(self, button, interaction):
        await stop_button(interaction, self.server_info)

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.secondary)
    async def pause_play(self, button, interaction):
        await pause_resume_button(interaction, self.server_info)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, button, interaction):
        await skip_button(interaction, self.server_info)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, button, interaction):
        await loop_button(interaction, self.server_info)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, button, interaction):
        await shuffle_button(interaction, self.server_info)


class PausingControlButton(PlayerUISuperClass):
    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.secondary)
    async def stop_play(self, button, interaction):
        await stop_button(interaction, self.server_info)

    @discord.ui.button(label="▶️", style=discord.ButtonStyle.secondary)
    async def resume_play(self, button, interaction):
        await pause_resume_button(interaction, self.server_info)

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary)
    async def skip(self, button, interaction):
        await skip_button(interaction, self.server_info)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary)
    async def loop(self, button, interaction):
        await loop_button(interaction, self.server_info)

    @discord.ui.button(label="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle(self, button, interaction):
        await shuffle_button(interaction, self.server_info)


async def stop_button(ita, server_info):
    """재생을 중지하고 재생목록 초기화"""
    try:
        if ita.guild.voice_client:
            server_info.queue.clear()
            server_info.is_loop = False
            server_info.log.append((ita.user.display_name, 'stop', None, time.time()))
            ita.guild.voice_client.stop()
            await ita.response.send_message("재생을 중지하고 재생목록을 초기화했습니다.", ephemeral=True)
        else:
            await ita.response.send_message("재생 중이 아닙니다.", ephemeral=True)
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if not ita.response.is_done():
            await ita.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
        else:
            await ita.followup.send("알 수 없는 오류입니다.", ephemeral=True)


async def pause_resume_button(ita, server_info):
    """재생을 일시정지 하거나 재개합니다."""
    try:
        if ita.guild.voice_client and ita.guild.voice_client.is_playing():
            ita.guild.voice_client.pause()
            server_info.log.append((ita.user.display_name, 'pause', None, time.time()))
            await handling_embed(server_info)
            await ita.response.send_message("재생을 일시정지 하였습니다.", ephemeral=True)

        elif ita.guild.voice_client and ita.guild.voice_client.is_paused():
            ita.guild.voice_client.resume()
            server_info.log.append((ita.user.display_name, 'resume', None, time.time()))
            await handling_embed(server_info)
            await ita.response.send_message("재생을 재개하였습니다.", ephemeral=True)

        else:
            await ita.response.send_message("재생 중이 아닙니다.", ephemeral=True)
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if not ita.response.is_done():
            await ita.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
        else:
            await ita.followup.send("알 수 없는 오류입니다.", ephemeral=True)


async def skip_button(ita, server_info):
    """현재 곡을 건너뜁니다."""
    try:
        if ita.guild.voice_client and ita.guild.voice_client.is_playing():
            server_info.log.append((ita.user.display_name, 'skip', None, time.time()))
            ita.guild.voice_client.stop()
            await ita.response.send_message("현재 곡을 건너뜁니다.", ephemeral=True)
        else:
            await ita.response.send_message("재생 중인 곡이 없습니다.", ephemeral=True)
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if not ita.response.is_done():
            await ita.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
        else:
            await ita.followup.send("알 수 없는 오류입니다.", ephemeral=True)


async def loop_button(ita, server_info):
    """현재 곡의 루프를 켜고 끕니다."""
    try:
        if server_info.is_loop:
            server_info.is_loop = False
            await handling_embed(server_info)
            await ita.response.send_message("루프를 해제 합니다.", ephemeral=True)

        else:
            if ita.guild.voice_client and (server_info.song_cache is not None) and ita.guild.voice_client.is_playing():
                server_info.is_loop = True
                await handling_embed(server_info)
                await ita.response.send_message("루프를 활성화 합니다.", ephemeral=True)
            else:
                await ita.response.send_message("재생 중이 아닙니다.", ephemeral=True)
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if not ita.response.is_done():
            await ita.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
        else:
            await ita.followup.send("알 수 없는 오류입니다.", ephemeral=True)


async def shuffle_button(ita, server_info):
    """재생목록 셔플을 켜고 끕니다."""
    try:
        if server_info.is_shuffle:
            server_info.is_shuffle = False
            await handling_embed(server_info)
            await ita.response.send_message("셔플이 꺼졌습니다.", ephemeral=True)

        else:
            if ita.guild.voice_client and (server_info.song_cache is not None) and ita.guild.voice_client.is_playing():
                server_info.is_shuffle = True
                await handling_embed(server_info)
                await ita.response.send_message("셔플이 켜졌습니다.", ephemeral=True)
            else:
                await ita.response.send_message("재생 중이 아닙니다.", ephemeral=True)
    except Exception:
        error_log = traceback.format_exc(limit=None, chain=True)
        await send_error_log(traceback.format_exc())
        if not ita.response.is_done():
            await ita.response.send_message("알 수 없는 오류입니다.", ephemeral=True)
        else:
            await ita.followup.send("알 수 없는 오류입니다.", ephemeral=True)
