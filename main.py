import time
import sys
from datetime import timedelta
import os
import shutil
import pyaudio
import wave
import tempfile
import json
from threading import Event, Lock, Thread

# ------------------- Config -------------------
CHUNK = 1024
FORMAT = pyaudio.paInt16   # We keep 16-bit for all quality levels

QUALITY_PRESETS = {
    "high":   {"rate": 48000, "channels": 2, "name": "High (48 kHz stereo)"},
    "medium": {"rate": 44100, "channels": 2, "name": "Medium (44.1 kHz stereo)"},
    "low":    {"rate": 44100, "channels": 1, "name": "Low (44.1 kHz mono)"}
}

# Default fallback
CURRENT_QUALITY = "medium"

SETTINGS_FILE = "voice_recorder_settings.json"

RECORDINGS_DIR = "recordings"
TRASH_DIR = "trash"
# ---------------------------------------------

os.makedirs(RECORDINGS_DIR, exist_ok=True)
os.makedirs(TRASH_DIR, exist_ok=True)

sort_key = "date"       # "date", "name", "duration"
sort_reverse = True     # True = descending, False = ascending


def load_quality_setting():
    global CURRENT_QUALITY
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
            q = data.get("quality")
            if isinstance(q, str) and q in QUALITY_PRESETS:
                CURRENT_QUALITY = q
    except (json.JSONDecodeError, OSError, TypeError):
        pass  # keep default if file is broken or unreadable


def save_quality_setting():
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"quality": CURRENT_QUALITY}, f, indent=2)
    except OSError:
        pass  # silent fail – better than crashing the program


def get_rate():
    return QUALITY_PRESETS[CURRENT_QUALITY]["rate"]


def get_channels():
    return QUALITY_PRESETS[CURRENT_QUALITY]["channels"]


def get_quality_name():
    return QUALITY_PRESETS[CURRENT_QUALITY]["name"]


def colored(text, color):
    colors = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "cyan": "\033[96m",
        "magenta": "\033[95m",
        "reset": "\033[0m"
    }
    return f"{colors.get(color, '')}{text}{colors['reset']}"


def clear():
    os.system('cls' if os.name == 'nt' else 'clear')


columns = shutil.get_terminal_size().columns

stop_event = Event()
pause_event = Event()
playback_event = Event()
frames = []
p = None
stream = None
recording_start_time = 0
paused_duration = 0
last_pause_time = 0
frames_lock = Lock()
preview_p = None
preview_stream = None
is_playing_preview = False
playback_paused = False
is_discarding = False
save_requested = False

ICONS = {
    "recording": "●",
    "paused": "❚❚",
    "playing": "▶",
    "playback_paused": "❚❚"
}


def callback(in_data, frame_count, time_info, status):
    if stop_event.is_set():
        return (None, pyaudio.paComplete)

    if pause_event.is_set():
        silence = b'\x00' * (frame_count * get_channels() * 2)
        return (silence, pyaudio.paContinue)

    with frames_lock:
        frames.append(in_data)
    return (None, pyaudio.paContinue)


def start_recording():
    global p, stream, frames, recording_start_time, paused_duration, is_discarding, save_requested
    frames = []
    stop_event.clear()
    pause_event.clear()
    playback_event.clear()
    recording_start_time = time.time()
    paused_duration = 0
    is_discarding = False
    save_requested = False

    p = pyaudio.PyAudio()
    stream = p.open(
        format=FORMAT,
        channels=get_channels(),
        rate=get_rate(),
        input=True,
        frames_per_buffer=CHUNK,
        stream_callback=callback
    )
    stream.start_stream()


def pause_recording():
    global last_pause_time
    pause_event.set()
    last_pause_time = time.time()


def resume_recording():
    global paused_duration, last_pause_time
    if last_pause_time > 0:
        paused_duration += time.time() - last_pause_time
        last_pause_time = 0
    pause_event.clear()


def save_current_recording_to_temp():
    with frames_lock:
        if not frames:
            return None

        temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
        wf = wave.open(temp_file.name, 'wb')
        wf.setnchannels(get_channels())
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(get_rate())
        wf.writeframes(b''.join(frames))
        wf.close()
        return temp_file.name


def play_preview():
    global is_playing_preview, playback_paused, preview_p, preview_stream

    temp_file = save_current_recording_to_temp()
    if not temp_file:
        return

    is_playing_preview = True
    playback_paused = False
    playback_event.clear()

    try:
        preview_p = pyaudio.PyAudio()

        with wave.open(temp_file, 'rb') as wf:
            def callback_playback(in_data, frame_count, time_info, status):
                if playback_event.is_set():
                    return (None, pyaudio.paComplete)
                data = wf.readframes(frame_count)
                return (data, pyaudio.paContinue)

            preview_stream = preview_p.open(
                format=preview_p.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
                stream_callback=callback_playback
            )

            preview_stream.start_stream()

            while preview_stream.is_active() and not playback_event.is_set():
                time.sleep(0.1)

    except Exception as e:
        print(f"Playback error: {e}")
    finally:
        if preview_stream:
            preview_stream.stop_stream()
            preview_stream.close()
        if preview_p:
            preview_p.terminate()
        if os.path.exists(temp_file):
            os.unlink(temp_file)
        is_playing_preview = False


def stop_preview():
    global is_playing_preview
    playback_event.set()
    is_playing_preview = False


def pause_preview():
    global playback_paused, preview_stream
    if preview_stream and preview_stream.is_active():
        preview_stream.stop_stream()
        playback_paused = True


def resume_preview():
    global playback_paused, preview_stream
    if preview_stream and playback_paused:
        preview_stream.start_stream()
        playback_paused = False


def move_to_trash(filename):
    src = os.path.join(RECORDINGS_DIR, filename)
    dst = os.path.join(TRASH_DIR, filename)

    base, ext = os.path.splitext(filename)
    counter = 1
    while os.path.exists(dst):
        dst = os.path.join(TRASH_DIR, f"{base}_{counter}{ext}")
        counter += 1

    shutil.move(src, dst)
    return os.path.basename(dst)


def discard_recording():
    global stream, p, frames, is_discarding

    stop_event.set()
    stop_preview()

    time.sleep(0.3)

    if stream:
        stream.stop_stream()
        stream.close()
    if p:
        p.terminate()

    with frames_lock:
        frames.clear()

    is_discarding = True

    print(colored("\n🗑️  Recording discarded.", "yellow"))
    time.sleep(1.2)


def get_elapsed_time(start_time):
    if pause_event.is_set() and not is_playing_preview:
        current_pause_duration = time.time() - last_pause_time
        elapsed_secs = int((time.time() - start_time) - paused_duration - current_pause_duration)
    else:
        elapsed_secs = int((time.time() - start_time) - paused_duration)

    return str(timedelta(seconds=elapsed_secs))


def stop_recording_and_save(custom_name_ask=False):
    global stream, p, paused_duration, last_pause_time, save_requested
    stop_event.set()

    stop_preview()

    if last_pause_time > 0:
        paused_duration += time.time() - last_pause_time

    time.sleep(0.4)

    if stream:
        stream.stop_stream()
        stream.close()
    if p:
        p.terminate()

    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    default_name = f"recording_{timestamp}.wav"

    if custom_name_ask:
        clear()
        print(colored("Saving current recording...", "cyan"))
        print(f"Default name: {default_name}\n")
        custom = input(colored("Enter custom name (or press Enter for default): ", "yellow")).strip()

        if custom:
            custom = "".join(c for c in custom if c.isalnum() or c in " -_()[]")
            custom = custom.strip()
            if not custom.lower().endswith('.wav'):
                custom += '.wav'
            filename = os.path.join(RECORDINGS_DIR, custom)
        else:
            filename = os.path.join(RECORDINGS_DIR, default_name)
    else:
        filename = os.path.join(RECORDINGS_DIR, default_name)

    try:
        wf = wave.open(filename, 'wb')
        wf.setnchannels(get_channels())
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(get_rate())

        with frames_lock:
            wf.writeframes(b''.join(frames))
        wf.close()

        print("\n" + colored("✓ Saved successfully!", "green"))
        print(colored(f"   {filename}", "yellow"))

    except Exception as e:
        print(colored(f"Error saving file: {e}", "red"))


def get_current_state():
    if is_playing_preview:
        if playback_paused:
            return "preview_paused"
        return "preview_playing"
    elif pause_event.is_set():
        return "recording_paused"
    else:
        return "recording"


def record():
    global save_requested

    clear()
    print(f"🎤 Voice Recorder  –  {get_quality_name()}".center(columns))
    print(colored("─" * 40, "blue"))

    start_time = time.time()
    start_recording()

    print("\n" + colored("Quick Help:", "cyan"))
    print("  P = Pause/Resume   L = Listen   S = Save   D = Discard   Ctrl+C = Save & Exit")
    print(colored("─" * 40, "blue") + "\n")

    try:
        print(f"{ICONS['recording']} Recording...")
        print(f"⏱️  Time: 0:00:00")
        print("\n" + colored("Press 'P' to pause", "yellow"))

        display_lines = 4

        while True:
            key = None
            if sys.platform == 'win32':
                import msvcrt
                if msvcrt.kbhit():
                    key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
            else:
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1).lower()

            if key:
                handle_keypress(key, start_time)

            update_display(start_time, display_lines)

            time.sleep(0.1)

    except KeyboardInterrupt:
        global is_discarding

        print(colored("\n\n⏹️  Stopping...", "yellow"))

        if not is_discarding and not save_requested:
            stop_recording_and_save(custom_name_ask=False)
            print(colored("Recording saved.", "green"))
        elif is_discarding:
            print(colored("Recording discarded, no save performed.", "yellow"))

        input("\nPress Enter to return to menu...")


def handle_keypress(key, start_time):
    global last_pause_time, paused_duration, save_requested

    state = get_current_state()

    if key == 'p':
        if state in ("preview_playing", "preview_paused"):
            print(colored("\n⏹️  Stop listening first (press S)", "red"))
            time.sleep(1)
            return

        if state == "recording_paused":
            resume_recording()
        else:
            pause_recording()
            last_pause_time = time.time()

    elif key == 'l':
        if state != "recording_paused":
            print(colored("\n⏸️  Pause recording first (press P)", "red"))
            time.sleep(1)
            return

        if state in ("preview_playing", "preview_paused"):
            print(colored("\n🎧 Already listening", "yellow"))
            time.sleep(1)
            return

        preview_thread = Thread(target=play_preview, daemon=True)
        preview_thread.start()
        time.sleep(0.1)

    elif key == 's':
        if is_playing_preview:
            stop_preview()
            print(colored("\nStopped preview.", "yellow"))
            time.sleep(0.8)
        else:
            print(colored("\nSaving now...", "yellow"))
            save_requested = True
            stop_recording_and_save(custom_name_ask=True)
            raise KeyboardInterrupt

    elif key == 'd':
        if is_playing_preview:
            print(colored("\nStop listening first (press S)", "red"))
            time.sleep(1.2)
            return

        print(colored("\nAre you sure you want to DISCARD this recording? (y/N): ", "red"), end="")
        confirm = input().strip().lower()
        if confirm in ('y', 'yes'):
            discard_recording()
            raise KeyboardInterrupt
        else:
            print(colored("Discard cancelled.", "green"))
            time.sleep(0.8)

    elif key == ' ':
        if state == "preview_playing":
            pause_preview()
        elif state == "preview_paused":
            resume_preview()


def update_display(start_time, display_lines):
    elapsed_str = get_elapsed_time(start_time)
    state = get_current_state()

    sys.stdout.write(f"\033[{display_lines}A")
    sys.stdout.write("\033[2K")

    if state == "recording":
        print(f"{colored(ICONS['recording'], 'red')} {colored('Recording...', 'green')}")
        controls = colored("P=pause  L=listen  S=save  D=discard  Ctrl+C=save+exit", "yellow")
    elif state == "recording_paused":
        print(f"{ICONS['paused']} {colored('Recording Paused', 'yellow')}")
        controls = colored("P=resume  L=listen  S=save  D=discard", "cyan")
    elif state == "preview_playing":
        print(f"{ICONS['playing']} {colored('Listening to Preview', 'cyan')}")
        controls = colored("Space=pause  S=stop  P=resume rec.", "cyan")
    elif state == "preview_paused":
        print(f"{ICONS['playback_paused']} {colored('Preview Paused', 'yellow')}")
        controls = colored("Space=resume  S=stop  P=resume rec.", "cyan")

    sys.stdout.write("\033[2K")
    print(f"⏱️  Time: {elapsed_str}")

    sys.stdout.write("\033[2K")
    print()

    sys.stdout.write("\033[2K")
    print(controls)

    sys.stdout.flush()


def get_file_duration(file_path):
    try:
        with wave.open(file_path, 'rb') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            duration = frames / float(rate)
            return duration
    except:
        return 0


def format_duration(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


# ────────────────────────────────────────────────
#                  SETTINGS MENU
# ────────────────────────────────────────────────

def settings_menu():
    global CURRENT_QUALITY

    while True:
        clear()
        print("⚙️  Settings".center(columns))
        print(colored("─" * 40, "blue") + "\n")

        print(f"Current recording quality: {colored(get_quality_name(), 'green')}\n")
        print("Available presets:\n")

        for key, data in QUALITY_PRESETS.items():
            marker = "→ " if key == CURRENT_QUALITY else "  "
            print(f"  {marker}{key.capitalize():<8}  {data['name']}")

        print("\n" + colored(" b = back to main menu", "cyan"))

        sel = input(colored("\nSelect quality (high / medium / low) or b: ", "cyan")).strip().lower()

        if sel in QUALITY_PRESETS:
            CURRENT_QUALITY = sel
            save_quality_setting()
            print(colored(f"\nQuality updated to: {get_quality_name()}", "green"))
            time.sleep(1.6)
        elif sel in ('b', 'back'):
            return
        else:
            print(colored("Invalid choice", "red"))
            time.sleep(1.2)


# ────────────────────────────────────────────────
#                  TRASH FUNCTIONS
# ────────────────────────────────────────────────

def restore_from_trash(file_index, files):
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid number!", "red"))
        time.sleep(1.5)
        return

    filename = files[file_index - 1]
    src = os.path.join(TRASH_DIR, filename)
    dst = os.path.join(RECORDINGS_DIR, filename)

    counter = 1
    base, ext = os.path.splitext(filename)
    while os.path.exists(dst):
        dst = os.path.join(RECORDINGS_DIR, f"{base} (restored {counter}){ext}")
        counter += 1

    try:
        shutil.move(src, dst)
        print(colored(f"\n✓ Restored: {os.path.basename(dst)}", "green"))
        time.sleep(1.8)
    except Exception as e:
        print(colored(f"Restore failed: {e}", "red"))
        time.sleep(2)


def permanent_delete_from_trash(file_index, files):
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid number!", "red"))
        time.sleep(1.5)
        return

    filename = files[file_index - 1]
    path = os.path.join(TRASH_DIR, filename)

    clear()
    print(colored("⚠️  PERMANENT DELETE", "red").center(columns))
    print(colored("─" * 40, "red") + "\n")
    print(f"File: {colored(filename, 'yellow')}")
    print(colored("This action CANNOT be undone!", "red"))

    confirm = input(colored("\nType 'DELETE' to permanently remove: ", "red")).strip()
    if confirm.upper() == 'DELETE':
        try:
            os.remove(path)
            print(colored(f"\n✓ Permanently deleted: {filename}", "green"))
            time.sleep(1.8)
        except Exception as e:
            print(colored(f"Error: {e}", "red"))
            time.sleep(2)
    else:
        print(colored("Cancelled.", "yellow"))
        time.sleep(1.2)


def empty_trash():
    clear()
    print(colored("🗑️  EMPTY TRASH", "red").center(columns))
    print(colored("─" * 40, "red") + "\n")

    files = [f for f in os.listdir(TRASH_DIR) if f.lower().endswith(".wav")]
    if not files:
        print(colored("Trash is already empty.", "yellow"))
        time.sleep(1.5)
        return

    print(f"Found {len(files)} file(s) in trash.")
    confirm = input(colored("\nType 'EMPTY' to permanently delete ALL items: ", "red")).strip()

    if confirm.upper() == 'EMPTY':
        count = 0
        for f in files:
            try:
                os.remove(os.path.join(TRASH_DIR, f))
                count += 1
            except:
                pass
        print(colored(f"\n✓ Emptied trash ({count} file(s) removed)", "green"))
        time.sleep(1.8)
    else:
        print(colored("Cancelled.", "yellow"))
        time.sleep(1.2)


def trash_menu():
    global sort_key, sort_reverse

    while True:
        clear()
        print("🗑️  Trash / Recycle Bin".center(columns))
        print(colored("─" * 40, "blue") + "\n")

        files = [f for f in os.listdir(TRASH_DIR) if f.lower().endswith(".wav")]

        if not files:
            print(colored("Trash is empty", "yellow"))
            print("Deleted recordings will appear here.")
            input("\nPress Enter to return...")
            return

        sort_func = lambda f: os.path.getmtime(os.path.join(TRASH_DIR, f))
        files = sorted(files, key=sort_func, reverse=True)

        print(f"  {colored(len(files), 'magenta')} items in trash")
        print(colored("─" * 75, "blue"))

        print(f"{colored('No.', 'cyan'):<4} {colored('Name', 'cyan'):<40} {colored('Deleted Date', 'cyan'):<20}")
        print(colored("─" * 75, "blue"))

        for i, f in enumerate(files, 1):
            path = os.path.join(TRASH_DIR, f)
            try:
                mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
            except:
                mtime = "—"

            display_name = f if len(f) <= 38 else f[:35] + "..."
            print(f"{colored(str(i), 'yellow'):<4} {display_name:<40} {mtime:<20}")

        print(colored("─" * 75, "blue"))

        print(f"\n{colored('Commands:', 'cyan')}")
        print("  [number]     select recording")
        print("  r            Restore to recordings")
        print("  d            Permanent delete")
        print("  e            Empty trash (all items)")
        print("  b            Back to main menu")

        choice = input(colored("\nEnter choice: ", "cyan")).strip().lower()

        if choice == 'b':
            return

        elif choice == 'e':
            empty_trash()
            continue

        elif choice in ('r', 'd'):
            try:
                num = int(input(colored(f"Enter number to { 'restore' if choice=='r' else 'permanently delete' }: ", "yellow")).strip())
                if choice == 'r':
                    restore_from_trash(num, files)
                else:
                    permanent_delete_from_trash(num, files)
            except:
                print(colored("Invalid number", "red"))
                time.sleep(1.5)
            continue

        else:
            try:
                num = int(choice)
                if 1 <= num <= len(files):
                    filename = files[num - 1]
                    clear()
                    print(f"🗑️  Trashed Recording: {colored(filename, 'cyan')}".center(columns))
                    print(colored("─" * 40, "blue") + "\n")

                    path = os.path.join(TRASH_DIR, filename)
                    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))

                    print(f"  Name:       {colored(filename, 'yellow')}")
                    print(f"  Deleted:    {colored(mtime, 'blue')}")

                    print(f"\n{colored('Options:', 'cyan')}")
                    print("  1   ↩ Restore")
                    print("  2   🗑️  Permanent Delete")
                    print("  3   ↩ Back to trash list")

                    sub = input(colored("\nSelect (1-3): ", "cyan")).strip()

                    if sub == '1':
                        restore_from_trash(num, files)
                    elif sub == '2':
                        permanent_delete_from_trash(num, files)
            except:
                print(colored("Invalid input", "red"))
                time.sleep(1.2)


# ────────────────────────────────────────────────
#               SORTING & LIST FUNCTIONS
# ────────────────────────────────────────────────

def get_sort_key_func(key):
    if key == "date":
        return lambda f: os.path.getmtime(os.path.join(RECORDINGS_DIR, f))
    elif key == "name":
        return lambda f: f.lower()
    elif key == "duration":
        return lambda f: get_file_duration(os.path.join(RECORDINGS_DIR, f))
    return lambda f: 0


def list_of_recordings():
    global sort_key, sort_reverse

    while True:
        clear()
        print("📁 Recordings".center(columns))
        print(colored("─" * 40, "blue") + "\n")

        files = [f for f in os.listdir(RECORDINGS_DIR) if f.lower().endswith(".wav")]

        if not files:
            print(colored("No recordings yet", "yellow"))
            print("Record something first!")
            input("\nPress Enter to return to menu...")
            return

        sort_func = get_sort_key_func(sort_key)
        files = sorted(files, key=sort_func, reverse=sort_reverse)

        sort_names = {"date": "Date Created", "name": "Name", "duration": "Duration"}
        sort_name = sort_names.get(sort_key, "Unknown")
        order_name = "↓ Newest first" if sort_key == "date" and sort_reverse else \
                     "↓ Descending" if sort_reverse else "↑ Ascending"

        print(f"  Sorted by: {colored(sort_name, 'cyan')} {colored(order_name, 'magenta')}")
        print(colored("─" * 75, "blue"))

        print(f"{colored('No.', 'cyan'):<4} {colored('Name', 'cyan'):<35} {colored('Duration', 'cyan'):<12} {colored('Date/Time', 'cyan'):<20}")
        print(colored("─" * 75, "blue"))

        total_duration = 0

        for i, f in enumerate(files, 1):
            path = os.path.join(RECORDINGS_DIR, f)
            try:
                stat = os.stat(path)
                mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
            except:
                mtime_str = "—"

            dur_sec = get_file_duration(path)
            dur_str = format_duration(dur_sec)

            display_name = f if len(f) <= 33 else f[:30] + "..."

            print(f"{colored(str(i), 'yellow'):<4} {display_name:<35} {dur_str:<12} {mtime_str:<20}")
            total_duration += dur_sec

        print(colored("─" * 75, "blue"))

        print(f"\n{colored('Total:', 'green')} {len(files)} recordings • {format_duration(total_duration)} total duration")

        print(f"\n{colored('Commands:', 'cyan')}")
        print("  [number]     select & view options")
        print("  s            change Sort field")
        print("  o            toggle Order")
        print("  r / d / p    Rename / Delete / Play  (then number)")
        print("  b            Back")

        choice = input(colored("\nEnter choice: ", "cyan")).strip().lower()

        if choice == 'b':
            return

        elif choice == 's':
            clear()
            print("Change sort field:\n")
            print("  1   Date created")
            print("  2   Name")
            print("  3   Duration")
            print(f"\nCurrent: {colored(sort_name, 'cyan')}")

            s = input(colored("\nSelect (1-3) or Enter: ", "cyan")).strip()
            if s == '1': sort_key = "date"
            elif s == '2': sort_key = "name"
            elif s == '3': sort_key = "duration"
            continue

        elif choice == 'o':
            sort_reverse = not sort_reverse
            print(colored(f"\nOrder: {'Descending' if sort_reverse else 'Ascending'}", "green"))
            time.sleep(1.2)
            continue

        elif choice in ('r', 'd', 'p'):
            try:
                num = int(input(colored(f"Enter number to { {'r':'rename','d':'delete','p':'play'}[choice] }: ", "yellow")).strip())
                if choice == 'r':
                    rename_recording(num, files)
                elif choice == 'd':
                    filename = files[num - 1]
                    moved_name = move_to_trash(filename)
                    print(colored(f"\nMoved to trash: {moved_name}", "yellow"))
                    time.sleep(1.5)
                elif choice == 'p':
                    play_recording(num, files)
            except:
                print(colored("Invalid number", "red"))
                time.sleep(1.5)
            continue

        else:
            try:
                num = int(choice)
                if 1 <= num <= len(files):
                    filename = files[num - 1]
                    clear()
                    print(f"📄 Recording: {colored(filename, 'cyan')}".center(columns))
                    print(colored("─" * 40, "blue") + "\n")

                    path = os.path.join(RECORDINGS_DIR, filename)
                    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path)))
                    dur = format_duration(get_file_duration(path))

                    print(f"  Name:       {colored(filename, 'yellow')}")
                    print(f"  Duration:   {colored(dur, 'green')}")
                    print(f"  Modified:   {colored(mtime, 'blue')}")

                    print(f"\n{colored('Options:', 'cyan')}")
                    print("  1   ▶ Play")
                    print("  2   📝 Rename")
                    print("  3   🗑️  Move to Trash")
                    print("  4   ↩ Back")

                    sub = input(colored("\nSelect (1-4): ", "cyan")).strip()

                    if sub == '1':
                        play_recording(num, files)
                    elif sub == '2':
                        rename_recording(num, files)
                    elif sub == '3':
                        moved_name = move_to_trash(filename)
                        print(colored(f"\nMoved to trash: {moved_name}", "yellow"))
                        time.sleep(1.5)
            except:
                print(colored("Invalid input", "red"))
                time.sleep(1.2)


def rename_recording(file_index, files):
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid file number!", "red"))
        time.sleep(1.5)
        return

    old_filename = files[file_index - 1]
    old_filepath = os.path.join(RECORDINGS_DIR, old_filename)

    clear()
    print("📝 Rename Recording".center(columns))
    print(colored("─" * 40, "blue") + "\n")

    print(f"Current name: {colored(old_filename, 'yellow')}")
    new_name = input(colored("New name (without .wav or Enter=cancel): ", "cyan")).strip()

    if not new_name:
        print(colored("Rename cancelled.", "yellow"))
        time.sleep(1.5)
        return

    new_name = "".join(c for c in new_name if c.isalnum() or c in " -_()[]").strip()
    if not new_name:
        print(colored("Invalid name!", "red"))
        time.sleep(1.5)
        return

    if not new_name.lower().endswith('.wav'):
        new_name += '.wav'

    new_filepath = os.path.join(RECORDINGS_DIR, new_name)

    if os.path.exists(new_filepath):
        print(colored(f"File '{new_name}' already exists!", "red"))
        time.sleep(1.5)
        return

    try:
        os.rename(old_filepath, new_filepath)
        print(colored(f"\n✓ Renamed: {new_name}", "green"))
        time.sleep(1.8)
    except Exception as e:
        print(colored(f"Error: {e}", "red"))
        time.sleep(2)


def play_recording(file_index, files):
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid number!", "red"))
        time.sleep(1.5)
        return

    filename = files[file_index - 1]
    filepath = os.path.join(RECORDINGS_DIR, filename)

    clear()
    print("▶ Playing Recording".center(columns))
    print(colored("─" * 40, "blue") + "\n")

    print(f"Now playing: {colored(filename, 'cyan')}")
    dur = get_file_duration(filepath)
    if dur > 0:
        print(f"Duration: {format_duration(dur)}")

    print("\n" + colored("Controls:  Space = Pause/Resume    S = Stop    other = exit", "cyan"))
    print(colored("─" * 40, "blue") + "\n")

    try:
        playback_p = pyaudio.PyAudio()
        with wave.open(filepath, 'rb') as wf:
            def cb(in_data, frame_count, time_info, status):
                if playback_event.is_set():
                    return (None, pyaudio.paComplete)
                data = wf.readframes(frame_count)
                return (data, pyaudio.paContinue if data else pyaudio.paComplete)

            stream = playback_p.open(
                format=playback_p.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
                stream_callback=cb
            )
            stream.start_stream()

            while stream.is_active() and not playback_event.is_set():
                if sys.platform == 'win32':
                    import msvcrt
                    if msvcrt.kbhit():
                        k = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                        if k == ' ':
                            if stream.is_active():
                                stream.stop_stream()
                            else:
                                stream.start_stream()
                        else:
                            playback_event.set()
                            break
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0)[0]:
                        k = sys.stdin.read(1).lower()
                        if k == ' ':
                            if stream.is_active():
                                stream.stop_stream()
                            else:
                                stream.start_stream()
                        else:
                            playback_event.set()
                            break
                time.sleep(0.1)

        stream.stop_stream()
        stream.close()
        playback_p.terminate()
        playback_event.clear()

    except Exception as e:
        print(colored(f"Playback error: {e}", "red"))

    print(colored("\nPlayback finished.", "yellow"))
    time.sleep(1)


def main_screen():
    while True:
        clear()
        print("🎤 Voice Recorder".center(columns))
        print(colored("─" * 40, "blue"))
        print("\n")

        menu_items = [
            ("1. 📝 Record New", "Start a new recording"),
            ("2. 📁 View Recordings", "List, play, rename, trash"),
            ("3. 🗑️  Trash", "Manage deleted recordings"),
            ("4. ⚙️  Settings", f"Current: {get_quality_name()}"),
            ("5. 🚪 Exit", "Close application")
        ]

        for item, desc in menu_items:
            print(f"{item}")
            print(f"   {colored(desc, 'blue')}\n")

        choice = input(colored("Select option (1-5): ", "cyan")).strip()

        if choice == "1":
            record()
        elif choice == "2":
            list_of_recordings()
        elif choice == "3":
            trash_menu()
        elif choice == "4":
            settings_menu()
        elif choice == "5":
            clear()
            print(colored("\n👋 Goodbye!\n", "green"))
            sys.exit(0)
        else:
            print(colored("\n❌ Invalid choice", "red"))
            time.sleep(1)


if __name__ == "__main__":
    load_quality_setting()  
    try:
        main_screen()
    except KeyboardInterrupt:
        clear()
        print(colored("\n👋 Goodbye!\n", "green"))
        sys.exit(0)
        