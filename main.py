import time
import sys
from datetime import timedelta
import os
import shutil
import pyaudio
import wave
import tempfile
from threading import Event, Lock, Thread

# ------------------- Config -------------------
CHUNK = 1024
FORMAT = pyaudio.paInt16
CHANNELS = 2
RATE = 44100
RECORDINGS_DIR = "recordings"
# ---------------------------------------------

os.makedirs(RECORDINGS_DIR, exist_ok=True)

def colored(text, color):
    colors = {
        "red": "\033[91m",
        "green": "\033[92m",
        "yellow": "\033[93m",
        "blue": "\033[94m",
        "cyan": "\033[96m",
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
        silence = b'\x00' * (frame_count * CHANNELS * 2)
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
        channels=CHANNELS,
        rate=RATE,
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
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
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

def discard_recording():
    """Stop recording and throw away everything"""
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
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(p.get_sample_size(FORMAT))
        wf.setframerate(RATE)
        
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
    print("🎤 Voice Recorder".center(columns))
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
        print(f"{ICONS['playing']} {colored('Listening to Preview', "cyan")}")
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
    """Get duration of a WAV file in seconds"""
    try:
        with wave.open(file_path, 'rb') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            duration = frames / float(rate)
            return duration
    except Exception as e:
        print(f"Error getting duration for {file_path}: {e}")
        return 0

def format_duration(seconds):
    """Format seconds to HH:MM:SS or MM:SS"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"

def rename_recording(file_index, files):
    """Rename a recording file"""
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
    print("Enter new name (without .wav extension, or press Enter to cancel):")
    
    new_name = input(colored("New name: ", "cyan")).strip()
    
    if not new_name:
        print(colored("Rename cancelled.", "yellow"))
        time.sleep(1.5)
        return
    
    new_name = "".join(c for c in new_name if c.isalnum() or c in " -_()[]")
    new_name = new_name.strip()
    
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
        print(colored(f"\n✓ Renamed successfully!", "green"))
        print(colored(f"'{old_filename}' → '{new_name}'", "yellow"))
        time.sleep(2)
    except Exception as e:
        print(colored(f"Error renaming file: {e}", "red"))
        time.sleep(2)

def delete_recording(file_index, files):
    """Delete a recording file"""
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid file number!", "red"))
        time.sleep(1.5)
        return
    
    filename = files[file_index - 1]
    filepath = os.path.join(RECORDINGS_DIR, filename)
    
    clear()
    print("🗑️ Delete Recording".center(columns))
    print(colored("─" * 40, "blue") + "\n")
    
    print(f"File to delete: {colored(filename, 'red')}")
    
    try:
        stat_info = os.stat(filepath)
        file_size = stat_info.st_size
        modified_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat_info.st_mtime))
        
        print(f"Date: {modified_time}")
        print(f"Size: {file_size:,} bytes")
        
        duration = get_file_duration(filepath)
        if duration > 0:
            print(f"Duration: {format_duration(duration)}")
    except:
        pass
    
    print(colored("\nAre you sure you want to DELETE this recording?", "red"))
    print(colored("This action cannot be undone!", "red"))
    
    confirm = input(colored("\nType 'DELETE' to confirm, or press Enter to cancel: ", "red")).strip()
    
    if confirm.upper() == 'DELETE':
        try:
            os.remove(filepath)
            print(colored(f"\n✓ Recording '{filename}' deleted successfully!", "green"))
            time.sleep(2)
        except Exception as e:
            print(colored(f"Error deleting file: {e}", "red"))
            time.sleep(2)
    else:
        print(colored("Deletion cancelled.", "yellow"))
        time.sleep(1.5)

def play_recording(file_index, files):
    """Play a recording file"""
    if file_index < 1 or file_index > len(files):
        print(colored("Invalid file number!", "red"))
        time.sleep(1.5)
        return
    
    filename = files[file_index - 1]
    filepath = os.path.join(RECORDINGS_DIR, filename)
    
    clear()
    print("▶ Playing Recording".center(columns))
    print(colored("─" * 40, "blue") + "\n")
    
    print(f"Now playing: {colored(filename, 'cyan')}")
    
    duration = get_file_duration(filepath)
    if duration > 0:
        print(f"Duration: {format_duration(duration)}")
    
    print("\n" + colored("Controls:", "cyan"))
    print("  Space = Pause/Resume")
    print("  S = Stop playback")
    print("  Any other key = Return to list")
    print(colored("─" * 40, "blue") + "\n")
    
    try:
        playback_p = pyaudio.PyAudio()
        
        with wave.open(filepath, 'rb') as wf:
            def callback_playback(in_data, frame_count, time_info, status):
                if playback_event.is_set():
                    return (None, pyaudio.paComplete)
                data = wf.readframes(frame_count)
                return (data, pyaudio.paContinue)
            
            playback_stream = playback_p.open(
                format=playback_p.get_format_from_width(wf.getsampwidth()),
                channels=wf.getnchannels(),
                rate=wf.getframerate(),
                output=True,
                stream_callback=callback_playback
            )
            
            playback_stream.start_stream()
            
            while playback_stream.is_active() and not playback_event.is_set():
                if sys.platform == 'win32':
                    import msvcrt
                    if msvcrt.kbhit():
                        key = msvcrt.getch().decode('utf-8', errors='ignore').lower()
                        if key == ' ':
                            if playback_stream.is_active():
                                playback_stream.stop_stream()
                            else:
                                playback_stream.start_stream()
                        elif key == 's':
                            playback_event.set()
                            break
                        else:
                            playback_event.set()
                            break
                else:
                    import select
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = sys.stdin.read(1).lower()
                        if key == ' ':
                            if playback_stream.is_active():
                                playback_stream.stop_stream()
                            else:
                                playback_stream.start_stream()
                        elif key == 's':
                            playback_event.set()
                            break
                        else:
                            playback_event.set()
                            break
                
                time.sleep(0.1)
        
        playback_stream.stop_stream()
        playback_stream.close()
        playback_p.terminate()
        playback_event.clear()
        
    except Exception as e:
        print(colored(f"Playback error: {e}", "red"))
        time.sleep(2)
    
    print(colored("\nPlayback stopped.", "yellow"))
    time.sleep(1)

def list_of_recordings():
    clear()
    print("📁 Recordings".center(columns))
    print(colored("─" * 40, "blue") + "\n")
    
    files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")])
    
    if not files:
        print(colored("No recordings yet", "yellow"))
        print("Record something first!")
        input("\nPress Enter to return to menu...")
        return
    
    while True:
        clear()
        print("📁 Recordings".center(columns))
        print(colored("─" * 40, "blue") + "\n")
        
        print(f"{colored('No.', 'cyan'):<4} {colored('Name', 'cyan'):<35} {colored('Duration', 'cyan'):<12} {colored('Date/Time', 'cyan'):<20}")
        print(colored("─" * 75, "blue"))
        
        total_duration = 0
        
        for i, f in enumerate(files, 1):
            file_path = os.path.join(RECORDINGS_DIR, f)
            
            stat_info = os.stat(file_path)
            modified_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat_info.st_mtime))
            
            duration_secs = get_file_duration(file_path)
            duration_str = format_duration(duration_secs)
            
            display_name = f
            if len(display_name) > 33:
                display_name = display_name[:30] + "..."
            
            print(f"{colored(str(i), 'yellow'):<4} {display_name:<35} {duration_str:<12} {modified_time:<20}")
            
            total_duration += duration_secs
        
        print(colored("─" * 75, "blue"))
        
        print(f"\n{colored('Total:', 'green')} {len(files)} recordings")
        print(f"{colored('Total duration:', 'green')} {format_duration(total_duration)}")
        
        print(f"\n{colored('Options:', 'cyan')}")
        print("  [number] = Select recording")
        print("  R = Rename selected recording")
        print("  D = Delete selected recording")
        print("  P = Play selected recording")
        print("  B = Back to main menu")
        
        choice = input(colored("\nEnter choice: ", "cyan")).strip().lower()
        
        if choice == 'b':
            return
        elif choice == 'r':
            try:
                file_num = int(input(colored("Enter recording number to rename: ", "yellow")).strip())
                rename_recording(file_num, files)
                files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")])
            except ValueError:
                print(colored("Invalid number!", "red"))
                time.sleep(1.5)
        elif choice == 'd':
            try:
                file_num = int(input(colored("Enter recording number to delete: ", "red")).strip())
                delete_recording(file_num, files)
                files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")])
            except ValueError:
                print(colored("Invalid number!", "red"))
                time.sleep(1.5)
        elif choice == 'p':
            try:
                file_num = int(input(colored("Enter recording number to play: ", "cyan")).strip())
                play_recording(file_num, files)
            except ValueError:
                print(colored("Invalid number!", "red"))
                time.sleep(1.5)
        else:
            try:
                file_num = int(choice)
                if 1 <= file_num <= len(files):
                    filename = files[file_num - 1]
                    clear()
                    print(f"📄 Recording: {colored(filename, 'cyan')}".center(columns))
                    print(colored("─" * 40, "blue") + "\n")
                    
                    print(f"{colored('Options for this recording:', 'cyan')}")
                    print("  1. ▶ Play")
                    print("  2. 📝 Rename")
                    print("  3. 🗑️  Delete")
                    print("  4. ↩ Back to list")
                    
                    sub_choice = input(colored("\nSelect option (1-4): ", "cyan")).strip()
                    
                    if sub_choice == '1':
                        play_recording(file_num, files)
                    elif sub_choice == '2':
                        rename_recording(file_num, files)
                        files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")])
                    elif sub_choice == '3':
                        delete_recording(file_num, files)
                        files = sorted([f for f in os.listdir(RECORDINGS_DIR) if f.endswith(".wav")])
                    elif sub_choice == '4':
                        continue
                    else:
                        print(colored("Invalid choice!", "red"))
                        time.sleep(1.5)
                else:
                    print(colored("Invalid recording number!", "red"))
                    time.sleep(1.5)
            except ValueError:
                print(colored("Invalid input!", "red"))
                time.sleep(1.5)

def main_screen():
    while True:
        clear()
        print("🎤 Voice Recorder".center(columns))
        print(colored("─" * 40, "blue"))
        print("\n")
        
        menu_items = [
            ("1. 📝 Record New", "Start a new recording"),
            ("2. 📁 View Recordings", "List, play, rename or delete recordings"),
            ("3. 🗑️  Trash", "Manage deleted files"),
            ("4. ⚙️  Settings", "Configure recorder"),
            ("5. 🚪 Exit", "Close the application")
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
            print(colored("\n🗑️  Trash feature coming soon!", "yellow"))
            time.sleep(1.5)
        elif choice == "4":
            print(colored("\n⚙️  Settings coming soon!", "yellow"))
            time.sleep(1.5)
        elif choice == "5":
            clear()
            print(colored("\n👋 Goodbye!\n", "green"))
            sys.exit(0)
        else:
            print(colored("\n❌ Invalid choice", "red"))
            time.sleep(1)

if __name__ == "__main__":
    try:
        main_screen()
    except KeyboardInterrupt:
        clear()
        print(colored("\n👋 Goodbye!\n", "green"))
        