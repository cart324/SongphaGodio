import re
import asyncio
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import yt_dlp
import subprocess
import discord
from discord.ext import commands
import os
import json
import shutil
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3
from pydub import AudioSegment
from io import BytesIO
import traceback


EXTRACTION_EXECUTOR = ThreadPoolExecutor(max_workers=4)
_VOLUME_EXECUTOR = None


def get_volume_executor() -> ProcessPoolExecutor:
    """Return the shared process pool used for ffmpeg/pydub volume analysis."""
    global _VOLUME_EXECUTOR
    if _VOLUME_EXECUTOR is None:
        _VOLUME_EXECUTOR = ProcessPoolExecutor(max_workers=1)
    return _VOLUME_EXECUTOR


def _find_js_runtime() -> dict:
    """Return a yt-dlp js_runtimes config for the first supported runtime found."""
    runtime_candidates = (
        ("deno", os.path.expanduser("~/.deno/bin/deno")),
        ("deno", shutil.which("deno")),
        ("node", shutil.which("node")),
        ("node", shutil.which("nodejs")),
        ("bun", shutil.which("bun")),
        ("quickjs", shutil.which("qjs")),
    )

    for runtime, path in runtime_candidates:
        if path and os.path.exists(path):
            return {runtime: {"path": path}}
    return {"deno": {}}


BASE_YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'quiet': True,
    'listformats': False,
    'postprocessor_args': [],
    'js_runtimes': _find_js_runtime(),
}

# 영상 추출용 yt-dlp 설정
YDL_OPTIONS = {
    **BASE_YDL_OPTIONS,
    'noplaylist': True,
}

# 플레이리스트 추출용 yt-dlp 옵션 설정
YDL_PLAYLIST_OPTIONS = {
    **BASE_YDL_OPTIONS,
    'extract_flat': 'in_playlist',
    'ignoreerrors': True,
}

# URL 패턴
youtube_pattern = re.compile(r'^(http|https)://((www|m|music)\.)?(youtube\.com|youtu\.be)/')
google_drive_pattern = re.compile(r'^(http|https)://(www\.)?drive\.google\.com/')

# 목표 RMS 값
TARGET_RMS = 2100

neogulman = "https://cdn.discordapp.com/attachments/469870241699069963/1259233014899277955/image.png?ex=6767c2e2&is=67667162&hm=b3d52daea4e3ed108a190d1eb83b094023d8592186d3a18cab66a0fec1cb18da&"

cover_channel = 1337411762252681279


def youtube_download(url: str) -> tuple:
    """
    유튜브 영상에서 정보 추출

    :arg url: 유튜브 url
    :return: (곡 제목, 곡 재생 url, 썸네일 url, 곡 길이(s), 스트림 메타데이터)
    """
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
        stream_metadata = {
            'availability': info.get('availability'),
            'age_limit': info.get('age_limit'),
            'playable_in_embed': info.get('playable_in_embed'),
            'format_id': info.get('format_id'),
            'yt_dlp_version': getattr(getattr(yt_dlp, 'version', None), '__version__', 'unknown'),
            'http_headers': dict(info.get('http_headers') or {}),
        }
        return (
            info.get('title', '제목 없음'),
            info.get('url', None),
            info.get('thumbnail', neogulman),
            info.get('duration', 0),
            stream_metadata,
        )
    except Exception:
        print(f"[ERROR] youtube_download failed for {url}:\n{traceback.format_exc()}")
        return '제목 없음', None, neogulman, 0, {}


def youtube_playlist_extract(playlist_url: str) -> list or False:
    """유튜브 링크에서 플레이리스트 목록을 추출하는 함수. 실패시 False 반환"""
    video_list = []

    try:
        with yt_dlp.YoutubeDL(YDL_PLAYLIST_OPTIONS) as ydl:
            # 플레이리스트 정보 추출
            playlist_info = ydl.extract_info(playlist_url, download=False)

            if 'entries' in playlist_info:
                for entry in playlist_info['entries']:
                    # entry가 None인 경우(오류로 건너뛴 영상)를 처리
                    if entry and entry.get('title'):
                        entry_url = entry.get('webpage_url') or entry.get('original_url') or entry.get('url')
                        if entry_url and not re.compile(r'^(http|https)://').match(entry_url):
                            entry_id = entry.get('id') or entry_url
                            entry_url = f"https://www.youtube.com/watch?v={entry_id}"

                        if entry_url:
                            video_list.append((entry_url, entry['title']))

                return video_list if video_list else False  # 추가된 영상이 하나도 없으면 False 반환
            else:
                return False
    except Exception as e:
        # ignoreerrors로도 해결 안 되는 네트워크 오류나 잘못된 URL 등의 예외 처리
        print(f"[ERROR] youtube_playlist_extract failed for {playlist_url}:\n{traceback.format_exc()}")
        return False


async def download_cover(cover_path: str, bot: commands.Bot) -> str:
    try:
        with open("covers/cover_link.json", "r", encoding="utf-8") as json_file:
            data = json.load(json_file)
            cover_massage_id = data.get(cover_path)
            if cover_massage_id is None:
                return None
            channel = await bot.fetch_channel(cover_channel)
            cover_massage = await channel.fetch_message(cover_massage_id)
            cover = cover_massage.attachments[0].url
        return cover
    except Exception:
        print(f"[ERROR] download_cover failed for {cover_path}:\n{traceback.format_exc()}")
        return None


async def upload_cover(cover_path: str, bot: commands.Bot) -> str:
    try:
        channel = await bot.fetch_channel(1337411762252681279)
        massage = await channel.send(file=discord.File(cover_path))
        cover_massage = massage.id
        cover = massage.attachments[0].url

        with open("covers/cover_link.json", "r", encoding="utf-8") as json_file:
            data = json.load(json_file)
        data[f"{cover_path}"] = cover_massage
        with open("covers/cover_link.json", "w", encoding="utf-8") as json_file:
            json.dump(data, json_file, ensure_ascii=False, indent=4)

        return cover
    except Exception:
        print(f"[ERROR] upload_cover failed for {cover_path}:\n{traceback.format_exc()}")
        return None


def google_drive_download(url: str):
    """
    스트리밍 가능한 Google Drive 링크를 변환하고 ffmpeg로 재생하는 함수.

    Parameters:
        url (str): Google Drive 공유 링크 (e.g., "https://drive.google.com/file/d/FILE_ID/view?usp=sharing")
    """
    try:
        # 1. Google Drive 공유 링크에서 FILE_ID 추출
        file_id_match = re.search(r"/d/([a-zA-Z0-9_-]+)/", url)
        if not file_id_match:
            raise ValueError("유효한 Google Drive 링크가 아닙니다.")

        file_id = file_id_match.group(1)
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        # ffmpeg 명령 실행 (메타데이터 추출)
        try:
            result = subprocess.run(
                ["ffmpeg", "-i", download_url, "-f", "ffmetadata", "-"],
                capture_output=True,
                text=True,
                timeout=10 # Add a timeout for subprocess
            )
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 오류: {result.stderr}")

            metadata = result.stderr  # 메타데이터는 stderr에 출력됨
        except FileNotFoundError:
            raise RuntimeError("ffmpeg가 설치되어 있지 않습니다.")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg 명령 실행 시간 초과.")
        except subprocess.SubprocessError as e:
            raise RuntimeError(f"ffmpeg 실행 오류: {e}")

        # 제목(title) 추출
        title_match = re.search(r"title\s*:\s*(.+)", metadata, re.IGNORECASE)
        title = title_match.group(1).strip() if title_match else "제목 없음"

        # 음악 길이(duration) 추출
        duration_match = re.search(r"Duration:\s*([0-9:.]+)", metadata)
        if duration_match:
            duration_str = duration_match.group(1)
            h, m, s = map(float, duration_str.split(":"))
            duration = int(h * 3600 + m * 60 + s)  # 초 단위로 변환
        else:
            duration = 0

        cover_path = f"covers/{file_id}.jpg"
        if not os.path.exists(cover_path):
            # 앨범 커버 추출 및 저장
            try:
                subprocess.run(
                    ["ffmpeg", "-i", download_url, "-an", "-vcodec", "copy", cover_path],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10 # Add a timeout for subprocess
                )
            except subprocess.TimeoutExpired:
                print(f"[WARNING] ffmpeg cover extraction timed out for {url}")
                cover_path = None
            except subprocess.SubprocessError:
                print(f"[WARNING] ffmpeg cover extraction failed for {url}")
                cover_path = None

        return title, download_url, cover_path, duration
    except Exception:
        print(f"[ERROR] google_drive_download failed for {url}:\n{traceback.format_exc()}")
        return '제목 없음', None, None, 0


async def get_audio_metadata(file_path: str, bot: commands.Bot) -> tuple:
    """
    오디오 파일(FALC/MP3)의 제목, 길이, 커버 이미지를 반환합니다.

    Parameters:
        file_path (str): 오디오 파일 경로.
        bot (commands.bot): 봇 객체.

    Returns:
        tuple: title, song_url, image, duration
    """
    try:
        if not os.path.exists(file_path):
            print(f"[ERROR] get_audio_metadata: File not found at {file_path}")
            return "Unknown Title", file_path, neogulman, int(0)

        file_extension = os.path.splitext(file_path)[-1].lower()

        # 커버 이름 자동 생성
        folder_name = file_path.split("\\")[-2] if len(file_path.split("\\")) > 1 else ""
        file_name = os.path.basename(file_path).split(".")[0]
        cover_path = f"covers/{folder_name}_{file_name}.jpg"

        cover = None
        if os.path.exists(cover_path):
            cover = await download_cover(cover_path, bot)
        
        if cover is None and os.path.exists(cover_path):
            # If download_cover failed or returned None, try to upload it again
            cover = await upload_cover(cover_path, bot)

        title = "Unknown Title"
        duration = 0

        if file_extension == ".flac":
            # FLAC 파일 처리
            audio = FLAC(file_path)
            title = audio.get("title", ["Unknown Title"])[0]
            duration = audio.info.length

            if cover is None:
                # FLAC 커버 이미지 추출
                picture = next((p for p in audio.pictures if p.type == 3), None)  # type 3: Cover(front)
                if picture:
                    temp_cover_path = f"covers/temp_{file_name}.jpg"
                    with open(temp_cover_path, "wb") as img_file:
                        img_file.write(picture.data)
                    cover = await upload_cover(temp_cover_path, bot)
                    os.remove(temp_cover_path) # Clean up temp file
                else:
                    cover = neogulman

        elif file_extension == ".mp3":
            # MP3 파일 처리
            audio = MP3(file_path, ID3=ID3)
            title = audio.tags.get("TIT2", "Unknown Title").text[0] if audio.tags and "TIT2" in audio.tags else "Unknown Title"
            duration = audio.info.length

            if cover is None:
                # MP3 커버 이미지 추출
                if audio.tags and "APIC:" in audio.tags:
                    apic = audio.tags["APIC:"]
                    temp_cover_path = f"covers/temp_{file_name}.jpg"
                    with open(temp_cover_path, "wb") as img_file:
                        img_file.write(apic.data)
                    cover = await upload_cover(temp_cover_path, bot)
                    os.remove(temp_cover_path) # Clean up temp file
                else:
                    cover = neogulman

        else:
            print(f"[WARNING] Unsupported file format: {file_extension} for {file_path}")
            return "Unknown Title", file_path, neogulman, int(0)

        return title, file_path, cover if cover else neogulman, duration

    except Exception:
        print(f"[ERROR] get_audio_metadata failed for {file_path}:\n{traceback.format_exc()}")
        return "Unknown Title", file_path, neogulman, int(0)


def convert_sec_to_hour(sec: int) -> str:
    """초를 입력 받으면 "시:분:초"형식으로 변환해주는 함수"""
    try:
        sec = int(sec)
        if sec == 0:
            return ""
        else:
            second = sec % 60  # 초에서 60으로 나눈 나머지
            minute = (sec // 60) % 60  # 초를 분으로 환산하여 60으로 나눈 나머지
            hour = sec // 60 // 60  # 초를 분으로 환산하고, 그 분을 시간으로 환산한 몫
            if hour == 0:
                return f"{minute}:{second:02d}"
            else:
                return f"{hour}:{minute}:{second:02d}"
    except Exception:
        print(f"[ERROR] convert_sec_to_hour failed for {sec}:\n{traceback.format_exc()}")
        return "0:00"


async def preprocessing_song(url, bot: commands.Bot) -> dict:
    """주어진 url을 유튜브, 구글 드라이브, 로컬 저장소로 구분한 뒤 정보를 추출하여 song_dict를 반환하는 함수"""
    song_dict = {
        'title': '알 수 없는 오류',
        'original_url': url,
        'play_url': None,
        'requester': None,
        'cover': neogulman,
        'duration': "0:00",
        'volume': 0.2,
        'volume_change': 100,
        'stream_metadata': {},
    }
    try:
        loop = asyncio.get_event_loop()
        stream_metadata = {}

        # 유튜브 링크일 경우
        if youtube_pattern.match(url):
            # yt-dlp는 네트워크/외부 런타임 대기가 대부분이라 프로세스 생성 없이 스레드에서 처리합니다.
            title, song_url, image, duration, stream_metadata = await loop.run_in_executor(
                EXTRACTION_EXECUTOR, youtube_download, url
            )

            # 고정 볼륨
            volume_adjustment = 0.2

        # 구글 드라이브 링크일 경우
        elif google_drive_pattern.match(url):
            title, song_url, cover_path, duration = await loop.run_in_executor(
                EXTRACTION_EXECUTOR, google_drive_download, url
            )
            # 커버 추출 실패 시
            if cover_path is None:
                image = neogulman
            else:
                # 이미지 url 처리
                image = await download_cover(cover_path, bot)
                if image is None:
                    image = await upload_cover(cover_path, bot)

            # 동적 볼륨 계산
            volume_adjustment = await async_normalize_volume(song_url)

        # 로컬 파일일 경우
        else:
            # 파일 정보 추출
            title, song_url, image, duration = await get_audio_metadata(url, bot)
            # 고정 볼륨
            volume_adjustment = 0.2

        song_dict = {
            'title': title if title else '제목 없음',
            'original_url': url,
            'play_url': song_url,
            'requester': None,
            'cover': image if image else neogulman,
            'duration': convert_sec_to_hour(duration),
            'volume': volume_adjustment,
            'volume_change': 100,
            'stream_metadata': stream_metadata,
        }
        return song_dict
    except Exception:
        print(f"[ERROR] preprocessing_song failed for {url}:\n{traceback.format_exc()}")
        return song_dict # Return default error dict


def normalize_volume(audio_url: str) -> float:
    """볼륨 정규화를 위한 RMS 분석 및 조정 배율 계산"""
    try:
        # ffmpeg로 오디오 데이터를 추출하여 pydub로 로드
        process = subprocess.Popen(
            ['ffmpeg', '-i', audio_url, '-f', 'mp3', '-ar', '44100', '-ac', '2', '-'], # Add sample rate and channels
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        audio_data = BytesIO(process.stdout.read())
        audio = AudioSegment.from_file(audio_data, format="mp3")
        rms = audio.rms
        adjustment_factor = TARGET_RMS / max(rms, 1)
        return min(max(adjustment_factor, 0.01), 1.0)  # 0.01배 ~ 1.0배로 제한
    except Exception:
        print(f"[ERROR] normalize_volume failed for {audio_url}:\n{traceback.format_exc()}")
        return 0.15  # 오류 시 기본 배율


async def async_normalize_volume(audio_url: str) -> float:
    """Run volume analysis in a shared process pool so playback is isolated."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(get_volume_executor(), normalize_volume, audio_url)

# =========================================================
# 새로 추가된 URL 갱신 로직
# =========================================================

def get_refreshed_stream(original_url: str) -> tuple[str | None, dict | None]:
    """단일 곡의 재생 URL과 제한 메타데이터를 다시 추출합니다."""
    if youtube_pattern.match(original_url):
        _, song_url, _, _, stream_metadata = youtube_download(original_url)
        return song_url, stream_metadata
    if google_drive_pattern.match(original_url):
        _, song_url, _, _ = google_drive_download(original_url)
        return song_url, {}
    return original_url, None


async def async_get_refreshed_stream(original_url: str, loop, executor):
    """비동기적으로 get_refreshed_stream을 병렬 실행합니다."""
    if re.compile(r'^(http|https)://').match(original_url):
        return await loop.run_in_executor(executor, get_refreshed_stream, original_url)
    return original_url, None


async def refresh_song_urls(song_list: list) -> None:
    """큐에 있는 모든 인터넷 링크 곡의 재생 URL(play_url)을 새로 갱신합니다."""
    if not song_list:
        return

    loop = asyncio.get_event_loop()
    # 재생목록 내의 모든 곡을 병렬로 다시 추출
    tasks = [
        async_get_refreshed_stream(song.get('original_url'), loop, EXTRACTION_EXECUTOR)
        for song in song_list
    ]
    results = await asyncio.gather(*tasks)

    # 추출 결과를 기존 리스트의 딕셔너리에 업데이트
    for song, (new_url, stream_metadata) in zip(song_list, results):
        if new_url:
            song['play_url'] = new_url
            if stream_metadata is not None:
                song['stream_metadata'] = stream_metadata
