import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from cogs import Audio_player as player


class PlayerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gid = 98765
        self.sequence = []
        self.bot = SimpleNamespace(user=SimpleNamespace(id=123), loop=asyncio.get_running_loop())
        self.guild = SimpleNamespace(id=self.gid, name='test', voice_client=None,
                                     me=SimpleNamespace(voice=None))
        self.bot.get_guild = lambda gid: self.guild
        self.channel = SimpleNamespace(id=456, connect=AsyncMock(side_effect=self.connect))
        self.text_channel = SimpleNamespace(send=AsyncMock(), fetch_message=AsyncMock())
        self.ctx = SimpleNamespace(
            guild=self.guild, channel=self.text_channel,
            author=SimpleNamespace(name='requester', display_name='requester',
                                   voice=SimpleNamespace(channel=self.channel)),
            defer=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()),
            response=SimpleNamespace(is_done=lambda: True), respond=AsyncMock(),
        )
        # ApplicationContext.voice_client is a live property, not a captured connection.
        class Context(SimpleNamespace):
            @property
            def voice_client(ctx):
                return ctx.guild.voice_client
        self.ctx = Context(**vars(self.ctx))
        self.cog = player.AudioPlayer(self.bot)
        self.guild.change_voice_state = AsyncMock(side_effect=self.depart)
        self.bot.wait_for = AsyncMock(side_effect=self.wait_for_departure)
        self.departure = None
        self.session_count = 0
        player.server_info_dict[self.gid] = player.ServerInfo()
        self.ui_patch = patch.object(player, 'handling_embed', new=AsyncMock())
        self.ui_patch.start()
        self.log_patch = patch.object(player, 'handling_log')
        self.log = self.log_patch.start()
        self.error_patch = patch.object(player, 'send_error_log', new=AsyncMock())
        self.errors = self.error_patch.start()

    async def asyncTearDown(self):
        self.ui_patch.stop()
        self.log_patch.stop()
        self.error_patch.stop()
        player.server_info_dict.pop(self.gid, None)
        player._voice_locks.pop(self.gid, None)
        player._voice_maintenance.discard(self.gid)

    def new_voice(self, session='current'):
        voice = SimpleNamespace(
            guild=self.guild, client=self.bot, channel=self.channel, session_id=session,
            is_connected=Mock(return_value=True), is_playing=Mock(return_value=False),
            is_paused=Mock(return_value=False), _player=None, stop=Mock(), play=Mock(),
        )
        def cleanup():
            self.sequence.append('cleanup')
            if self.guild.voice_client is voice:
                self.guild.voice_client = None
        async def disconnect(**kwargs):
            self.sequence.append('disconnect')
            voice.is_connected.return_value = False
            self.guild.me.voice = None
            cleanup()
        voice.cleanup = Mock(side_effect=cleanup)
        voice.disconnect = AsyncMock(side_effect=disconnect)
        return voice

    async def connect(self, **kwargs):
        self.sequence.append('connect')
        self.session_count += 1
        voice = self.new_voice(f'new-{self.session_count}')
        self.guild.voice_client = voice
        self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id=voice.session_id)
        return voice

    def healthy_state(self):
        voice = self.new_voice()
        self.guild.voice_client = voice
        self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id=voice.session_id)
        state = player.server_info_dict[self.gid]
        state.voice_client = voice
        state.embed_channel = self.text_channel
        return state, voice

    async def wait_for_departure(self, event, check, timeout):
        self.sequence.append('listen')
        self.departure = asyncio.get_running_loop().create_future()
        self.departure_check = check
        return await asyncio.wait_for(self.departure, timeout)

    async def depart(self, **kwargs):
        self.sequence.append('gateway_leave')
        self.assertIsNone(kwargs['channel'])
        member = SimpleNamespace(id=self.bot.user.id, guild=self.guild)
        before = self.guild.me.voice
        after = SimpleNamespace(channel=None)
        self.guild.me.voice = None
        await self.cog.on_voice_state_update(member, before, after)
        if self.departure_check(member, before, after):
            self.departure.set_result((member, before, after))

    def song(self, title='test'):
        return {'title': title, 'play_url': 'https://example.com/audio',
                'original_url': 'https://example.com/audio', 'volume': 0.2}

    async def test_valid_session_is_reused(self):
        state, voice = self.healthy_state()
        state.queue.append(self.song())
        self.assertIs(await self.cog._ensure_voice_connection(self.ctx), state)
        self.channel.connect.assert_not_awaited()
        voice.disconnect.assert_not_awaited()
        self.assertEqual(len(state.queue), 1)

    async def test_reference_mismatch_is_reset_not_adopted(self):
        state, voice = self.healthy_state()
        state.voice_client = None
        state.queue.append(self.song())
        restored = await self.cog._ensure_voice_connection(self.ctx)
        self.assertIsNot(restored, state)
        self.assertIsNot(restored.voice_client, voice)
        self.assertEqual(state.queue, [])
        self.assertLess(self.sequence.index('disconnect'), self.sequence.index('connect'))

    async def test_ghost_without_local_client_requires_departure_before_connect(self):
        self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id='previous-process')
        restored = await self.cog._ensure_voice_connection(self.ctx)
        self.assertEqual(self.sequence, ['listen', 'gateway_leave', 'connect'])
        self.assertTrue(player._current_voice_session(self.guild, self.bot, restored.voice_client))
        self.assertIs(player.server_info_dict[self.gid], restored)

    async def test_no_departure_confirmation_means_no_new_connection(self):
        self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id='previous-process')
        self.guild.change_voice_state.side_effect = None
        with patch.object(player, 'VOICE_RECOVERY_TIMEOUT', 0.02):
            with self.assertRaises(TimeoutError):
                await self.cog._ensure_voice_connection(self.ctx)
        self.channel.connect.assert_not_awaited()
        self.assertNotIn(self.gid, player._voice_maintenance)

    async def test_ghost_reappearing_after_departure_is_not_adopted(self):
        self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id='previous-process')
        async def depart_then_reappear(**kwargs):
            await self.depart(**kwargs)
            self.guild.me.voice = SimpleNamespace(channel=self.channel, session_id='another-process')
        self.guild.change_voice_state.side_effect = depart_then_reappear
        with self.assertRaisesRegex(RuntimeError, 'presence remained'):
            await self.cog._ensure_voice_connection(self.ctx)
        self.channel.connect.assert_not_awaited()

    async def test_wrong_session_owner_channel_or_disconnected_client_is_replaced(self):
        for problem in ('session', 'owner', 'channel', 'disconnected'):
            with self.subTest(problem=problem):
                player.server_info_dict[self.gid] = player.ServerInfo()
                state, voice = self.healthy_state()
                if problem == 'session':
                    voice.session_id = 'previous-session'
                elif problem == 'owner':
                    voice.client = object()
                elif problem == 'channel':
                    voice.channel = SimpleNamespace(id=999)
                else:
                    voice.is_connected.return_value = False
                restored = await self.cog._ensure_voice_connection(self.ctx)
                self.assertIsNot(restored, state)
                voice.disconnect.assert_awaited_once_with(force=True)
                self.assertTrue(player._current_voice_session(self.guild, self.bot, restored.voice_client))

    async def test_new_connection_must_also_pass_session_validation(self):
        async def wrong_session(**kwargs):
            voice = await self.connect(**kwargs)
            voice.session_id = 'wrong'
            return voice
        self.channel.connect.side_effect = wrong_session
        with self.assertRaisesRegex(RuntimeError, 'could not be verified'):
            await self.cog._ensure_voice_connection(self.ctx)
        self.assertIsNone(player.server_info_dict[self.gid].voice_client)
        self.assertIsNone(self.guild.voice_client)

    async def test_disconnect_failure_never_falls_through_to_connect(self):
        state, voice = self.healthy_state()
        state.voice_client = None
        voice.disconnect.side_effect = TimeoutError('test')
        with self.assertRaises(TimeoutError):
            await self.cog._ensure_voice_connection(self.ctx)
        self.channel.connect.assert_not_awaited()
        self.assertNotIn(self.gid, player._voice_maintenance)

    async def test_two_simultaneous_requests_only_connect_once(self):
        first, second = await asyncio.gather(
            self.cog._ensure_voice_connection(self.ctx), self.cog._ensure_voice_connection(self.ctx))
        self.assertIs(first, second)
        self.channel.connect.assert_awaited_once()

    async def test_leave_waits_for_connection_then_invalidates_its_snapshot(self):
        connecting, proceed = asyncio.Event(), asyncio.Event()
        async def slow_connect(**kwargs):
            connecting.set()
            await proceed.wait()
            return await self.connect(**kwargs)
        self.channel.connect.side_effect = slow_connect
        ensure_task = asyncio.create_task(self.cog._ensure_voice_connection(self.ctx))
        await connecting.wait()
        leave_task = asyncio.create_task(self.cog._close_player(self.guild, disconnect=True))
        await asyncio.sleep(0)
        self.assertFalse(leave_task.done())
        proceed.set()
        snapshot, closed = await asyncio.gather(ensure_task, leave_task)
        self.assertTrue(closed)
        self.assertIsNot(player.server_info_dict[self.gid], snapshot)
        self.assertIsNone(self.guild.voice_client)

    async def test_active_and_paused_playback_are_not_reset(self):
        state, voice = self.healthy_state()
        state.song_cache = self.song()
        state.playback_end_token = object()
        for paused in (False, True):
            voice.is_playing.return_value = not paused
            voice.is_paused.return_value = paused
            self.assertIs(await self.cog._ensure_voice_connection(self.ctx), state)
        voice.disconnect.assert_not_awaited()

    async def test_cleanup_thread_and_end_task_are_not_mistaken_for_stale_flags(self):
        state, voice = self.healthy_state()
        state.song_cache = self.song()
        state.playback_end_token = object()
        voice._player = SimpleNamespace(is_alive=lambda: True)
        self.assertIs(await self.cog._ensure_voice_connection(self.ctx), state)
        voice._player = None
        pending = asyncio.create_task(asyncio.Event().wait())
        state.playback_end_task = pending
        state.url_refresh_token = object()
        try:
            self.assertIs(await self.cog._ensure_voice_connection(self.ctx), state)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        voice.disconnect.assert_not_awaited()

    async def test_stopped_player_with_orphan_flags_recovers(self):
        state, voice = self.healthy_state()
        state.song_cache = self.song()
        state.playback_end_token = object()
        restored = await self.cog._ensure_voice_connection(self.ctx)
        self.assertIsNot(restored, state)
        self.assertIsNone(restored.song_cache)
        self.assertIsNone(restored.playback_end_token)

    async def test_play_loop_resets_invalid_session_before_ffmpeg(self):
        state, voice = self.healthy_state()
        state.queue.append(self.song())
        voice.is_connected.return_value = False
        with patch.object(player, 'FilteredFFmpegPCMAudio') as ffmpeg:
            await player.play_loop(self.gid, self.bot)
        ffmpeg.assert_not_called()
        self.assertIsNot(player.server_info_dict[self.gid], state)
        self.assertEqual(state.queue, [])

    async def test_normal_end_task_starts_next_song(self):
        state, voice = self.healthy_state()
        state.song_cache = self.song('previous')
        state.queue.append(self.song('next'))
        state.playback_end_task = asyncio.current_task()
        with patch.object(player, 'FilteredFFmpegPCMAudio') as ffmpeg, \
             patch.object(player.discord, 'PCMVolumeTransformer'), \
             patch.object(player, '_pin_playback_workers'):
            await player.play_loop(self.gid, self.bot)
        ffmpeg.assert_called_once()
        self.assertEqual(state.song_cache['title'], 'next')
        voice.play.assert_called_once()
        voice.disconnect.assert_not_awaited()

    async def test_real_after_callback_publishes_task_before_health_check(self):
        state, voice = self.healthy_state()
        state.queue.extend([self.song('first'), self.song('second')])
        voice.play.side_effect = lambda *args, **kwargs: setattr(voice.is_playing, 'return_value', True)
        with patch.object(player, 'FilteredFFmpegPCMAudio') as ffmpeg, \
             patch.object(player.discord, 'PCMVolumeTransformer'), \
             patch.object(player, '_pin_playback_workers'):
            ffmpeg.return_value.consume_access_denied_classification.return_value = None
            await player.play_loop(self.gid, self.bot)
            callback = voice.play.call_args.kwargs['after']
            voice.is_playing.return_value = False
            callback(None)
            for _ in range(8):
                await asyncio.sleep(0)
            self.assertEqual(voice.play.call_count, 2)
            self.assertEqual(state.song_cache['title'], 'second')
            self.assertIs(player.server_info_dict[self.gid], state)
        voice.disconnect.assert_not_awaited()

    async def test_delayed_leave_event_does_not_close_verified_new_session(self):
        state, voice = self.healthy_state()
        await self.cog.on_voice_state_update(
            SimpleNamespace(id=self.bot.user.id, guild=self.guild),
            SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        self.assertIs(player.server_info_dict[self.gid], state)
        voice.disconnect.assert_not_awaited()

    async def test_waiting_old_close_cannot_reset_new_state(self):
        old = player.server_info_dict[self.gid]
        player.server_info_dict[self.gid] = player.ServerInfo()
        state, voice = self.healthy_state()
        self.assertFalse(await self.cog._close_player(self.guild, disconnect=False, expected_state=old))
        self.assertIs(player.server_info_dict[self.gid], state)

    async def test_play_adds_to_recovered_snapshot(self):
        old, voice = self.healthy_state()
        old.voice_client = None
        with patch.object(self.cog, '_add_song_to_queue', new=AsyncMock(return_value=self.song())), \
             patch.object(player, 'play_loop', new=AsyncMock()):
            await player.AudioPlayer.play.callback(self.cog, self.ctx, 'test')
        restored = player.server_info_dict[self.gid]
        self.assertIsNot(restored, old)
        self.assertEqual(len(restored.queue), 1)
        self.errors.assert_not_awaited()

    async def test_play_does_not_add_after_leave_during_extraction(self):
        old, voice = self.healthy_state()
        async def finish_after_leave(*args):
            await asyncio.sleep(0.01)
            old.voice_client = None
            player.server_info_dict[self.gid] = player.ServerInfo()
            return self.song()
        with patch.object(self.cog, '_add_song_to_queue', side_effect=finish_after_leave), \
             patch.object(player, 'play_loop', new=AsyncMock()) as play:
            await player.AudioPlayer.play.callback(self.cog, self.ctx, 'test')
        self.assertEqual(old.queue, [])
        self.assertEqual(player.server_info_dict[self.gid].queue, [])
        play.assert_not_awaited()
        self.assertIn('취소', self.ctx.followup.send.call_args.args[0])

    async def run_playlist(self):
        with patch.object(player, 'youtube_playlist_extract', return_value=[('a', 'A'), ('b', 'B')]), \
             patch.object(player, 'EXTRACTION_EXECUTOR', None):
            await player.AudioPlayer.playlist.callback(self.cog, self.ctx, 'https://youtube.com/playlist?list=test')

    async def test_playlist_uses_recovered_snapshot_for_all_tracks(self):
        old, voice = self.healthy_state()
        old.voice_client = None
        with patch.object(self.cog, '_add_song_to_queue', new=AsyncMock(return_value=self.song())), \
             patch.object(player, 'play_loop', new=AsyncMock()):
            await self.run_playlist()
        restored = player.server_info_dict[self.gid]
        self.assertIsNot(restored, old)
        self.assertEqual(len(restored.queue), 2)
        self.errors.assert_not_awaited()

    async def test_playlist_cancels_if_closed_during_extraction(self):
        old, voice = self.healthy_state()
        async def finish_after_leave(*args):
            old.voice_client = None
            player.server_info_dict[self.gid] = player.ServerInfo()
            return self.song()
        with patch.object(self.cog, '_add_song_to_queue', side_effect=finish_after_leave), \
             patch.object(player, 'play_loop', new=AsyncMock()) as play:
            await self.run_playlist()
        self.assertEqual(old.queue, [])
        self.assertEqual(player.server_info_dict[self.gid].queue, [])
        play.assert_not_awaited()
        self.assertIn('중단', self.ctx.followup.send.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
