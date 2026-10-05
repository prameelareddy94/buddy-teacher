"""python -m buddy.voice setup   # download the Whisper model and Piper voices (one time)"""
import sys

from buddy.voice import stt, tts


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] != "setup":
        sys.exit(__doc__)
    if stt.installed():
        print("Listening (Whisper):")
        stt.preload()
    else:
        print("Whisper not installed: pip install -r requirements-voice.txt")
    if tts.installed():
        print("Speaking (Piper voices):")
        tts.download_voices()
        print(f"  ready for: {', '.join(tts.available_langs()) or 'none'}")
    else:
        print("Piper not installed: pip install -r requirements-voice.txt")


if __name__ == "__main__":
    main()
