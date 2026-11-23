import asyncio
import queue
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from cogs.radio import Radio, _RadioBuffer


class PCMFixture(discord.AudioSource):
    def __init__(self):
        self.frame = struct.pack('<h', 1000) * 1920
        self.cleaned = False

    def read(self):
        return self.frame

    def cleanup(self):
        self.cleaned = True


class RadioAudioQualityTest(unittest.IsolatedAsyncioTestCase):
    async def _start(self, bitrate=384_000, volume=100, actual_bitrate=None):
        guild = SimpleNamespace(id=123)
        channel = SimpleNamespace(id=456, guild=guild, bitrate=bitrate)
        actual = channel if actual_bitrate is None else SimpleNamespace(
            id=789, guild=guild, bitrate=actual_bitrate
        )
        vc = SimpleNamespace(channel=actual, play=Mock(), encoder=Mock())
        cog = Radio.__new__(Radio)
        cog.bot = SimpleNamespace(loop=asyncio.get_running_loop())
        cog._stream_sources = {}
        cog._stream_failures = {}
        cog._stream_blocked_until = {}
        cog._resolve_stream = AsyncMock(return_value='https://example.com/radio.mp3')
        cog._set_voice_channel_status = AsyncMock()
        pcm = PCMFixture()
        with patch('cogs.radio.discord.FFmpegPCMAudio', return_value=pcm) as ffmpeg, patch(
            'cogs.radio._RadioBuffer', side_effect=lambda original: original
        ):
            self.assertTrue(await cog.start_playing(vc, channel, {'volume': volume}))
        await asyncio.sleep(0)
        return vc, pcm, ffmpeg

    async def test_music_encoding_tracks_channel_budget_and_sdk_limits(self):
        # discord.py's bitrate unit is 1024 bits/s; its PCM encoder accepts 16..512.
        for channel_bitrate, expected in ((384_000, 375), (96_000, 93), (32_000, 31), (8_000, 16), (600_000, 512)):
            with self.subTest(channel_bitrate=channel_bitrate):
                vc, _, _ = await self._start(channel_bitrate)
                self.assertEqual(vc.play.call_args.kwargs['bitrate'], expected)
                self.assertEqual(vc.play.call_args.kwargs['application'], 'audio')
                self.assertEqual(vc.play.call_args.kwargs['signal_type'], 'music')
                self.assertEqual(vc.play.call_args.kwargs['bandwidth'], 'full')

    async def test_start_uses_connected_channel_instead_of_old_destination(self):
        vc, _, _ = await self._start(384_000, actual_bitrate=32_000)
        self.assertEqual(vc.play.call_args.kwargs['bitrate'], 31)

    async def test_channel_updates_and_moves_apply_in_audio_reader_thread(self):
        vc, _, _ = await self._start()
        source = vc.play.call_args.args[0]
        calls = []
        vc.encoder.set_bitrate.side_effect = lambda value: calls.append((value, threading.get_ident()))

        def read_after_updates():
            source.read()  # Stable bitrate must leave the encoder untouched.
            vc.channel.bitrate = 96_000
            source.read()
            source.read()
            vc.channel = SimpleNamespace(bitrate=32_000)
            source.read()
            return threading.get_ident()

        reader_thread = await asyncio.to_thread(read_after_updates)
        self.assertNotEqual(reader_thread, threading.get_ident())
        self.assertEqual(calls, [(93, reader_thread), (31, reader_thread)])

    async def test_pcm_volume_cleanup_and_ffmpeg_url_policy_are_preserved(self):
        for volume, sample in ((100, 1000), (50, 500), (0, 0)):
            with self.subTest(volume=volume):
                vc, pcm, ffmpeg = await self._start(volume=volume)
                source = vc.play.call_args.args[0]
                self.assertFalse(source.is_opus())
                self.assertEqual(source.read(), struct.pack('<h', sample) * 1920)
                source.cleanup()
                self.assertTrue(pcm.cleaned)
                self.assertEqual(ffmpeg.call_args.kwargs['options'], '-vn')
                self.assertIn('-protocol_whitelist http,https,tcp,tls,crypto', ffmpeg.call_args.kwargs['before_options'])

    async def test_old_source_cannot_reconfigure_replacement_encoder(self):
        vc, _, _ = await self._start()
        source = vc.play.call_args.args[0]
        source.read()
        old_encoder = vc.encoder
        vc.encoder = Mock()
        vc.channel.bitrate = 32_000
        source.read()
        old_encoder.set_bitrate.assert_called_once_with(31)
        vc.encoder.set_bitrate.assert_not_called()

    async def test_missing_channel_keeps_previous_budget_until_channel_is_available(self):
        vc, _, _ = await self._start()
        source = vc.play.call_args.args[0]
        for channel in (None, discord.Object(id=456)):
            vc.channel = channel
            self.assertTrue(source.read())
            vc.encoder.set_bitrate.assert_not_called()
        vc.channel = SimpleNamespace(bitrate=96_000)
        source.read()
        vc.encoder.set_bitrate.assert_called_once_with(93)

    async def test_missing_bitrate_at_start_uses_sdk_default_then_adapts(self):
        vc, _, _ = await self._start(bitrate=None)
        self.assertEqual(vc.play.call_args.kwargs['bitrate'], 128)
        vc.channel.bitrate = 384_000
        vc.play.call_args.args[0].read()
        vc.encoder.set_bitrate.assert_called_once_with(375)

    async def test_ffmpeg_failure_is_visible_to_audio_player_at_eof(self):
        vc, pcm, _ = await self._start(volume=50)
        source = vc.play.call_args.args[0]
        pcm.frame = b''
        pcm._current_error = RuntimeError('FFmpeg exited')
        self.assertEqual(source.read(), b'')
        self.assertIs(source._current_error, pcm._current_error)


class RadioBufferTest(unittest.TestCase):
    def _timed_playback(self, stall, buffered):
        """Drive the real SDK scheduler with a deterministic HTTP-style stall."""
        clock = SimpleNamespace(now=0.0)
        frame_count = 150
        pending = list(range(frame_count))

        def arrival(index):
            timestamp = index * .02
            return max(timestamp, .8 + stall) if .8 <= timestamp < .8 + stall else timestamp

        def frame(index):
            return struct.pack('<h', index + 1) * 1920

        class StallingPCM(discord.AudioSource):
            def read(self):
                if not pending:
                    return b''
                index = pending.pop(0)
                clock.now = max(clock.now, arrival(index))
                return frame(index)

        source = StallingPCM()
        if buffered:
            source = _RadioBuffer(source)
            # Simulate the independent producer at each virtual-clock boundary.
            source._reader = Mock()
            original_read = source.read

            def read_buffer():
                while pending and arrival(pending[0]) <= clock.now + 1e-9:
                    try:
                        source._frames.put_nowait(frame(pending[0]))
                    except queue.Full:
                        break
                    pending.pop(0)
                if not pending:
                    source._finished.set()
                return original_read()

            source.read = read_buffer

        sent = []
        client = SimpleNamespace(
            is_connected=lambda: True,
            send_audio_packet=lambda data, **kwargs: sent.append((clock.now, struct.unpack('<h', data[:2])[0])),
        )
        player = discord.player.AudioPlayer(source, client)
        with patch('discord.player.time.perf_counter', side_effect=lambda: clock.now), patch(
            'discord.player.time.sleep', side_effect=lambda duration: setattr(clock, 'now', clock.now + duration)
        ), patch.object(player, '_speak'), patch.object(player, 'send_silence'):
            player._do_run()
        return sent

    def test_buffer_hides_microstall_and_keeps_normal_cadence_after_long_stall(self):
        for stall in (.12, .6):
            with self.subTest(stall=stall):
                baseline = self._timed_playback(stall, buffered=False)
                self.assertTrue(any(b[0] - a[0] < .001 for a, b in zip(baseline, baseline[1:])))
                sent = self._timed_playback(stall, buffered=True)
                # SDK has a 40ms initial sleep; thereafter every packet stays at20ms,
                # including silence during rebuffering and sustained resumed audio.
                for a, b in zip(sent[1:], sent[2:]):
                    self.assertAlmostEqual(b[0] - a[0], .02, places=7)
                music = [(when, sample) for when, sample in sent if sample]
                self.assertEqual([sample for _, sample in music], list(range(1, 151)))
                if stall == .12:
                    self.assertAlmostEqual(music[-1][0] - music[0][0], 149 * .02, places=7)
                else:
                    self.assertTrue(any(b[0] - a[0] > .1 for a, b in zip(music, music[1:])))

    def test_prefill_underflow_and_short_eof_drain_without_blocking(self):
        source = _RadioBuffer(PCMFixture())
        source._reader = Mock()
        self.assertEqual(source.read(), source.SILENCE)
        source._frames.put_nowait(b'last frame')
        self.assertEqual(source.read(), source.SILENCE)
        source._finished.set()
        self.assertEqual(source.read(), b'last frame')
        self.assertEqual(source.read(), b'')

    def test_last_frame_arriving_during_empty_read_is_drained_before_eof(self):
        source = _RadioBuffer(PCMFixture())
        source._reader = Mock()
        source._buffering = False

        def finish_during_read():
            source._frames.put_nowait(b'last frame')
            source._finished.set()
            raise queue.Empty

        with patch.object(source._frames, 'get_nowait', side_effect=finish_during_read):
            self.assertEqual(source.read(), source.SILENCE)
        self.assertEqual(source.read(), b'last frame')
        self.assertEqual(source.read(), b'')

    def test_cleanup_is_idempotent_and_does_not_join_its_own_thread(self):
        for reader in (Mock(), threading.current_thread()):
            with self.subTest(reader=reader):
                original = PCMFixture()
                original.cleanup = Mock()
                source = _RadioBuffer(original)
                source._reader = reader
                source.cleanup()
                source.cleanup()
                original.cleanup.assert_called_once()
                if isinstance(reader, Mock):
                    reader.join.assert_called_once()

    def test_cleanup_unblocks_reader_and_full_queue(self):
        for blocked_read in (False, True):
            with self.subTest(blocked_read=blocked_read):
                entered = threading.Event()
                release = threading.Event()

                class PCM(discord.AudioSource):
                    count = 0

                    def read(self):
                        self.count += 1
                        if blocked_read or self.count > _RadioBuffer.FRAME_COUNT:
                            entered.set()
                        if blocked_read:
                            release.wait(2)
                        return _RadioBuffer.SILENCE

                    def cleanup(self):
                        release.set()

                source = _RadioBuffer(PCM())
                source.read()
                self.assertTrue(entered.wait(1))
                source.cleanup()
                self.assertFalse(source._reader.is_alive())
                self.assertEqual(source.read(), b'')
                self.assertLessEqual(source._frames.qsize(), source.FRAME_COUNT)

    def test_reader_error_reaches_eof_after_buffered_audio(self):
        original = PCMFixture()
        source = _RadioBuffer(original)
        failure = RuntimeError('PCM read failed')
        release = threading.Event()

        def first_frame():
            release.wait(1)
            return original.frame

        reads = iter((True, False))

        def read():
            if next(reads):
                return first_frame()
            raise failure

        original.read = read
        self.assertEqual(source.read(), source.SILENCE)
        release.set()
        self.assertTrue(source._finished.wait(1))
        self.assertEqual(source.read(), original.frame)
        self.assertEqual(source.read(), b'')
        self.assertIs(source._current_error, failure)
        source.cleanup()


if __name__ == '__main__':
    unittest.main()
