# wfm_botprototype2_status_final_fixed.py
import os
import sqlite3
import threading
import time
import sys
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import messagebox, simpledialog, StringVar, ttk
from functools import partial
from typing import Optional, List, Any
from pymongo import MongoClient


MONGO_URI = "mongodb+srv://aravi_db_user:mzY50ZDEv31xquIl@wfmcluster.61qxkew.mongodb.net/WFM_DB?retryWrites=true&w=majority&appName=WFMCluster"
MONGO_DB_NAME = "WFM_DB"
MONGO_COLLECTION = "user_activity_log"


mongo_client = None

mongo_collection = None



# ---------- TTS BACKEND (Option A: Pure TTS) ----------
_TTS_OK = False
_tts_engine: Optional[Any] = None
try:
    import pyttsx3  # type: ignore
    _tts_engine = pyttsx3.init()
    _TTS_OK = True
except Exception:
    _tts_engine = None
    _TTS_OK = False

# ---------- CONFIG ----------
LOG_DB_FILE = "prototype.db"
POPUP_INTERVAL_MINUTES = 30  # default interval
IDLE_TIMEOUT_SECONDS = 600   # 10 minutes idle
BREAK_DURATION_MINUTES = 15
LUNCH_DURATION_MINUTES = 30

WINDOW_W, WINDOW_H = 680, 460
FRAME_COLLAPSED_H = 120
FRAME_EXPANDED_H = 220

STATUS_OPTIONS = ["Working", "Lunch", "Meeting", "Personal", "Break", "Offline", "Off work"]

# ---------- GLOBAL STATE ----------
GLOBAL_ROOT: Optional[tk.Tk] = None
USER_EMP_ID: List[Optional[str]] = [None]
current_status: List[str] = ["Working"]
last_response_time: List[datetime] = [datetime.now()]
timed_status_end: List[Optional[datetime]] = [None]
work_remaining_seconds: List[int] = [0]
# flags
break_exceeded_flag = False
idle_flag = False

def init_mongo():
    global mongo_client, mongo_collection
    try:
        mongo_client = MongoClient(MONGO_URI)
        db = mongo_client[MONGO_DB_NAME]
        mongo_collection = db[MONGO_COLLECTION]
        print("MongoDB Connected ✔")
    except Exception as e:
        print("MongoDB Connection Error:", e)
        
def log_to_mongo(response: str, status: str, remark: str):
    try:
        if mongo_collection is None:
            return  # Mongo not ready
        doc = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "emp_id": USER_EMP_ID[0],
            "status": status,
            "response": response,
            "remark": remark
        }
        mongo_collection.insert_one(doc)
        print("MongoDB Log ✔", doc)
    except Exception as e:
        print("MongoDB Write Error:", e)
    

# ---------- DB FUNCTIONS ----------
def init_db() -> None:
    try:
        conn = sqlite3.connect(LOG_DB_FILE)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                emp_id TEXT,
                status TEXT,
                response TEXT,
                remark TEXT
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB init error:", e)


def log_to_db(response: str, status: str, remark: str = "") -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    emp = USER_EMP_ID[0] if USER_EMP_ID[0] else "N/A"
    try:
        conn = sqlite3.connect(LOG_DB_FILE)
        c = conn.cursor()
        c.execute('INSERT INTO activity_log (timestamp, emp_id, status, response, remark) VALUES (?, ?, ?, ?, ?)',
                  (ts, emp, status, response, remark))
        conn.commit()
        conn.close()
    except Exception as e:
        print("DB write error:", e)

    # NEW: log also to MongoDB
    log_to_mongo(response, status, remark)

    print(f"[{ts}] LOGGED | {status} | {response} | {remark}")


# ---------- TTS / BOT POPUP (Option A) ----------
def _tts_speak(text: str) -> None:
    """Speak via pyttsx3 if available. Runs in background thread."""
    try:
        engine = _tts_engine
        if not _TTS_OK or engine is None:
            return
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print("TTS error:", e)


def bot_audio_and_popup(text: str, parent: Optional[tk.Misc] = None, audio_text: Optional[str] = None) -> None:
    """
    Show a messagebox and speak text via TTS.
    Runs TTS on a background thread, and schedules the messagebox safely on the main thread.
    parent: optional tkinter widget to use as messagebox parent.
    audio_text: optional override for speech content.
    """
    speak_text = audio_text if audio_text else text

    def _worker_speak():
        # speak in background thread (so it doesn't block UI)
        _tts_speak(speak_text)

    def _safe_popup(target_parent: Optional[tk.Misc]):
        try:
            if target_parent is not None:
                # if parent exists and is a widget
                if getattr(target_parent, "winfo_exists", lambda: False)():
                    messagebox.showinfo("Bot", text, parent=target_parent)
                    return
            # fallback: no parent
            messagebox.showinfo("Bot", text)
        except Exception:
            # as last fallback, print to console
            print("Bot popup fallback:", text)

    # start speaking concurrently
    threading.Thread(target=_worker_speak, daemon=True).start()

    # schedule messagebox on main thread via parent or global root
    try:
        # prefer passed parent if valid
        if parent is not None and getattr(parent, "winfo_exists", lambda: False)():
            parent.after(50, lambda: _safe_popup(parent))
            return
        # otherwise use global root if exists
        if GLOBAL_ROOT is not None and getattr(GLOBAL_ROOT, "winfo_exists", lambda: False)():
            GLOBAL_ROOT.after(50, lambda: _safe_popup(GLOBAL_ROOT))
            return
    except Exception:
        # ignore scheduling error, call directly (will block)
        pass
    # last-resort direct popup
    try:
        _safe_popup(None)
    except Exception:
        pass


# ---------- SIMPLE NLP ----------
def nlp_process_command(text: str, app) -> bool:
    """Simple rule-based NLP to handle commands (returns True when processed)."""
    t = (text or "").strip().lower()
    if not t:
        bot_audio_and_popup("Please type a command.", parent=app.master)
        return True

    # Back to work flow with ticket logic
    if "back to work" in t:
        app.chat_state = "await_ticket_type"
        bot_audio_and_popup("Is this the same ticket or a new ticket?", parent=app.master)
        return True

    if getattr(app, "chat_state", None) == "await_ticket_type":
        if "same" in t:
            log_to_db("Back to Work", "Working", "Same ticket via chatbot")
            current_status[0] = "Working"
            last_response_time[0] = datetime.now()
            timed_status_end[0] = None
            app.update_status_display("Working")
            bot_audio_and_popup("Logged as back to work on same ticket.", parent=app.master)
            app.chat_state = "idle"
            return True
        if "new" in t:
            app.chat_state = "await_ticket_number"
            bot_audio_and_popup("Please provide the new ticket number.", parent=app.master)
            return True

    if getattr(app, "chat_state", None) == "await_ticket_number":
        ticket = t.strip()
        if ticket:
            log_to_db(f"Ticket {ticket}", "Working", "New ticket from chatbot")
            current_status[0] = "Working"
            last_response_time[0] = datetime.now()
            timed_status_end[0] = None
            app.update_status_display("Working")
            bot_audio_and_popup(f"New ticket {ticket} stored. Welcome back to work!", parent=app.master)
            app.chat_state = "idle"
            return True
        else:
            bot_audio_and_popup("I didn't get the ticket number. Try again.", parent=app.master)
            return True

    # status change via text like "click meeting" or simply "meeting"
    for s in ["meeting", "personal", "break", "lunch", "offline", "working", "off work"]:
        if t.startswith("click ") and s in t:
            chosen = s.title() if s != "off work" else "Off work"
            app.submit_activity(chosen)
            bot_audio_and_popup(f"Okay — switched to {chosen}.", parent=app.master)
            return True
        if t == s:
            app.submit_activity(s.title() if s != "off work" else "Off work")
            bot_audio_and_popup(f"Okay — switched to {s.title()}.", parent=app.master)
            return True

    # ask remaining time
    if ("how many" in t or "how much" in t or "minutes" in t) and ("left" in t or "remaining" in t or "until" in t):
        if timed_status_end[0]:
            rem = timed_status_end[0] - datetime.now()
            if rem.total_seconds() > 0:
                mins = int(rem.total_seconds() // 60)
                secs = int(rem.total_seconds() % 60)
                bot_audio_and_popup(f"{mins} minutes and {secs} seconds remaining for {current_status[0]}.", parent=app.master)
            else:
                exceeded = datetime.now() - timed_status_end[0]
                mins = int(exceeded.total_seconds() // 60)
                bot_audio_and_popup(f"You have exceeded {current_status[0]} by {mins} minutes.", parent=app.master)
        else:
            elapsed = (datetime.now() - last_response_time[0]).total_seconds()
            remain = max(0, POPUP_INTERVAL_MINUTES * 60 - int(elapsed))
            bot_audio_and_popup(f"{remain//60} minutes and {remain%60} seconds until the next scheduled prompt.", parent=app.master)
        return True

    # fallback help
    bot_audio_and_popup("Sorry, I couldn't understand that. Try: 'back to work', 'how many minutes left', or 'click meeting'.", parent=app.master)
    return True


# ---------- UI APP ----------
class ActivityApp:
    def __init__(self, master: tk.Tk, interval_minutes: int):
        global GLOBAL_ROOT
        self.master = master
        GLOBAL_ROOT = master
        self.master.title(f"WFM Tracker — {USER_EMP_ID[0] if USER_EMP_ID[0] else ''}")
        self.interval_minutes = interval_minutes
        self.chat_state = "idle"

        # center medium window
        sw, sh = master.winfo_screenwidth(), master.winfo_screenheight()
        x = (sw // 2) - (WINDOW_W // 2)
        y = (sh // 2) - (WINDOW_H // 2)
        master.geometry(f"{WINDOW_W}x{WINDOW_H}+{x}+{y}")
        master.minsize(WINDOW_W - 100, WINDOW_H - 100)

        # top status label
        self.status_label = tk.Label(master, text="", font=("Segoe UI", 14, "bold"))
        self.status_label.pack(pady=(8, 6))

        # task entry
        self.task_label = tk.Label(master, text="Task / Reason (required for Working):", font=("Segoe UI", 10))
        self.task_label.pack()
        self.task_entry = tk.Entry(master, font=("Segoe UI", 11), width=58)
        self.task_entry.pack(padx=8, pady=(0, 8))

        # expandable frame
        self.exp_frame = tk.Frame(master, height=FRAME_COLLAPSED_H, relief=tk.RIDGE, bd=1)
        self.exp_frame.pack(fill=tk.X, padx=12, pady=6)
        self.exp_frame.pack_propagate(False)

        # inner controls
        self.controls_inner = tk.Frame(self.exp_frame)
        self.controls_inner.place(relx=0.5, rely=0.5, anchor="center")

        # buttons grid
        self.buttons = []
        colors = {
            "Working": "#2c9b2c",
            "Meeting": "#ff7a00",
            "Personal": "#ff9b3c",
            "Break": "#d9534f",
            "Lunch": "#c9302c",
            "Offline": "#337ab7",
            "Off work": "#2b7a78",
        }
        c = 0
        r = 0
        for s in STATUS_OPTIONS:
            btn = tk.Button(
                self.controls_inner,
                text=s,
                bg=colors.get(s, "#666"),
                fg="white",
                width=14,
                height=1,
                font=("Segoe UI", 10, "bold"),
                command=partial(self.submit_activity, s),
            )
            btn.grid(row=r, column=c, padx=6, pady=6)
            btn.bind("<Enter>", partial(self._btn_hover, btn))
            btn.bind("<Leave>", partial(self._btn_unhover, btn))
            self.buttons.append(btn)
            c += 1
            if c == 3:
                c = 0
                r += 1

        # expand/collapse bindings
        self.exp_frame.bind("<Enter>", self._expand_frame)
        self.exp_frame.bind("<Leave>", self._collapse_frame)

        # chat area
        chat_frame = tk.Frame(master)
        chat_frame.pack(pady=(6, 4))
        self.chat_entry = tk.Entry(chat_frame, width=48, font=("Segoe UI", 11))
        self.chat_entry.pack(side=tk.LEFT, padx=(6, 4))
        self.chat_button = tk.Button(chat_frame, text="Ask Bot", command=self._on_chat)
        self.chat_button.pack(side=tk.LEFT, padx=(0, 6))

        # help label
        self.help_lbl = tk.Label(
            master,
            text="Try commands: 'back to work', 'how many minutes left', 'click meeting'",
            font=("Segoe UI", 9, "italic"),
        )
        self.help_lbl.pack(pady=(4, 8))

        # init display
        self.update_status_display(current_status[0])
        self._blink = False

        # periodic background tasks
        self.master.after(1000, self._periodic_ui)
        # schedule the prompt check loop on a background thread
        self._schedule_thread = threading.Thread(target=self._run_schedule_loop, daemon=True)
        self._schedule_thread.start()

    # UI helpers
    def _expand_frame(self, event=None):
        self._animate_frame(FRAME_COLLAPSED_H, FRAME_EXPANDED_H, 8)

    def _collapse_frame(self, event=None):
        self._animate_frame(FRAME_EXPANDED_H, FRAME_COLLAPSED_H, 8)

    def _animate_frame(self, start_h: int, end_h: int, steps: int = 8) -> None:
        """
        Smooth animation with floats internally; height config uses int to avoid type errors.
        """
        delta = (end_h - start_h) / float(steps)

        def step(i: int = 0, cur: float = float(start_h)):
            if i >= steps:
                self.exp_frame.config(height=int(end_h))
                return
            cur = cur + delta
            self.exp_frame.config(height=int(cur))
            # schedule next step with float carried over (no type problems)
            self.master.after(20, lambda: step(i + 1, cur))

        step()

    def _btn_hover(self, widget, event=None):
        widget.config(font=("Segoe UI", 11, "bold"))

    def _btn_unhover(self, widget, event=None):
        widget.config(font=("Segoe UI", 10, "bold"))

    # chat handler
    def _on_chat(self) -> None:
        txt = self.chat_entry.get()
        self.chat_entry.delete(0, tk.END)
        nlp_process_command(txt, self)

    # status submission
    def submit_activity(self, status: str) -> None:
        global break_exceeded_flag, idle_flag
        resp = self.task_entry.get().strip()
        # validation
        if status == "Working" and not resp:
            bot_audio_and_popup("Please provide a task/reason before marking as Working.", parent=self.master)
            return

        # If Personal or Meeting without reason -> warn and voice-intimate to HR/RM
        log_remark = ""
        if status in ("Personal", "Meeting") and not resp:
            warning_text = (
                "You are marking '{}' without a reason. This action will be intimated to your Reporting Manager / HR. "
                "Do you want to proceed?"
            ).format(status)
            # TTS warning + popup
            bot_audio_and_popup(
                f"Warning: Marking {status} without reason will be intimated to RM/HR.",
                parent=self.master,
                audio_text=f"Warning. Marking {status} without reason will be intimated.",
            )
            proceed = messagebox.askyesno("Reason Not Entered", warning_text, parent=self.master)
            if not proceed:
                return
            # else, allow but log special remark
            log_remark = "No reason entered; user accepted warning - RM/HR will be intimated."

        # durations for timed statuses
        duration = None
        if status in ("Meeting", "Personal"):
            dur = simpledialog.askstring(f"{status} Duration", "Enter duration in minutes:", parent=self.master)
            try:
                duration = int(dur) if dur else BREAK_DURATION_MINUTES
            except Exception:
                duration = BREAK_DURATION_MINUTES
        elif status == "Break":
            duration = BREAK_DURATION_MINUTES
        elif status == "Lunch":
            duration = LUNCH_DURATION_MINUTES

        # apply state & DB logging
        current_status[0] = status
        log_to_db(resp if resp else "(no reason)", status, remark=f"Started{(' for ' + str(duration) + 'm') if duration else ''} {log_remark}")
        last_response_time[0] = datetime.now()
        if duration:
            timed_status_end[0] = datetime.now() + timedelta(minutes=duration)
        else:
            timed_status_end[0] = None

        self.task_entry.delete(0, tk.END)
        self.update_status_display(status)
        bot_audio_and_popup(f"Status set to {status}.", parent=self.master)

        # special actions
        if status == "Offline":
            log_to_db("Exit", "Offline", "User chose Offline")
            self.master.after(200, lambda: self.master.quit())

    def update_status_display(self, status: str) -> None:
        color_map = {
            "Working": "#2c9b2c",
            "Break": "#d9534f",
            "Lunch": "#c9302c",
            "Meeting": "#ff7a00",
            "Personal": "#ff9b3c",
            "Idle": "#d9534f",
            "Offline": "#337ab7",
            "Off work": "#2b7a78",
        }
        text = f"Current Status: {status}"
        # append timer text
        timer_text = ""
        if status in ("Meeting", "Personal", "Break", "Lunch") and timed_status_end[0]:
            rem = timed_status_end[0] - datetime.now()
            if rem.total_seconds() > 0:
                m = int(rem.total_seconds() // 60)
                s = int(rem.total_seconds() % 60)
                timer_text = f"  ({m}m {s}s remaining)"
            else:
                timer_text = "  (EXCEEDED)"
        elif status == "Working":
            elapsed = (datetime.now() - last_response_time[0]).total_seconds()
            remain = max(0, POPUP_INTERVAL_MINUTES * 60 - int(elapsed))
            timer_text = f"  (next prompt in {remain//60}m {remain%60}s)"
        self.status_label.config(text=text + timer_text, fg=color_map.get(status, "black"))

    def _periodic_ui(self) -> None:
        # blinking if exceeded
        if current_status[0] in ("Meeting", "Personal", "Break", "Lunch") and timed_status_end[0]:
            if timed_status_end[0] < datetime.now():
                self._blink = not self._blink
                self.status_label.config(fg=("#b30000" if self._blink else "#ffffff"))
            else:
                self.status_label.config(fg="#2c9b2c")
        else:
            self.status_label.config(fg="#2c9b2c")
        # refresh timer text
        self.update_status_display(current_status[0])
        self.master.after(1000, self._periodic_ui)

    # schedule loop runs in background thread to decide when to show scheduled prompts
    def _run_schedule_loop(self) -> None:
        global break_exceeded_flag
        while True:
            try:
                # Only prompt when user is Working and not in break_exceeded state
                if current_status[0] == "Working" and not break_exceeded_flag:
                    # compute remaining time to next prompt
                    elapsed = (datetime.now() - last_response_time[0]).total_seconds()
                    remaining = POPUP_INTERVAL_MINUTES * 60 - int(elapsed)
                    if remaining <= 0:
                        # schedule prompt on main thread
                        def do_prompt():
                            # ensure window visible
                            if not self.master.winfo_ismapped() or self.master.wm_state() == "iconic":
                                try:
                                    self.master.deiconify()
                                    self.master.lift()
                                    self.master.focus_force()
                                except Exception:
                                    pass
                            # set label and play voice/popup
                            self.task_label.config(text=f"It's been {POPUP_INTERVAL_MINUTES} minutes. What's your current task?")
                            bot_audio_and_popup("Time to log your activity.", parent=self.master)
                            # log and reset last_response_time
                            log_to_db("Prompt Displayed", "Working", f"Scheduled {POPUP_INTERVAL_MINUTES}m prompt shown.")
                            last_response_time[0] = datetime.now()

                        try:
                            self.master.after(50, do_prompt)
                        except Exception:
                            do_prompt()

                # Check for timed statuses exceeding and set break_exceeded_flag
                if current_status[0] in ("Meeting", "Personal", "Break", "Lunch") and timed_status_end[0]:
                    if timed_status_end[0] <= datetime.now():
                        # log once when reached
                        if not globals().get("break_exceeded_flag_state_set", False):
                            log_to_db(f"Exceeded {current_status[0]}", current_status[0], "Duration reached - mandatory response expected.")
                            globals()["break_exceeded_flag_state_set"] = True
                        # set global flag so other parts can act if needed
                        break_exceeded_flag = True

                time.sleep(1)
            except Exception as e:
                print("Schedule loop error:", e)
                time.sleep(1)


# ---------- TIMED STATUS MONITOR (compat) ----------
def monitor_timed_status(start_time: datetime, status: str, duration_minutes: int, app: ActivityApp) -> None:
    end_time = start_time + timedelta(minutes=duration_minutes)
    while datetime.now() < end_time and current_status[0] == status:
        time.sleep(2)
    if current_status[0] == status:
        log_to_db(f"Exceeded {status}", status, f"{duration_minutes}m exceeded")
        bot_audio_and_popup(f"Hey {USER_EMP_ID[0] if USER_EMP_ID[0] else ''}, your {status} time is over. Please update your status.", parent=GLOBAL_ROOT)


# ---------- IDLE MONITOR (background) ----------
def monitor_idle(app: ActivityApp) -> None:
    global idle_flag, break_exceeded_flag
    while True:
        try:
            time.sleep(5)
            if current_status[0] != "Working":
                continue
            elapsed = (datetime.now() - last_response_time[0]).total_seconds()
            if elapsed >= IDLE_TIMEOUT_SECONDS:
                # double-check mouse movement - use a short wait & compare if pyautogui is available
                try:
                    import pyautogui  # type: ignore
                    pos_before = pyautogui.position()
                    time.sleep(3)
                    pos_after = pyautogui.position()
                    moved = pos_before != pos_after
                except Exception:
                    moved = False

                if not moved:
                    log_to_db("Idle", "Idle", f"No activity for {IDLE_TIMEOUT_SECONDS}s")
                    idle_flag = True
                    break_exceeded_flag = True
                    # force UI to show a warning and require reason when returning to work
                    bot_audio_and_popup("No activity detected for 10 minutes. You have been marked Idle. Please provide reason when you return.", parent=GLOBAL_ROOT)
                    # set status to Idle
                    current_status[0] = "Idle"
                    # reset last_response_time so future calculations measure Idle duration
                    last_response_time[0] = datetime.now()
        except Exception as e:
            print("Idle monitor error:", e)
            time.sleep(1)


# ---------- ENTRY / LAUNCH ----------
def start_main_ui(interval: int) -> None:
    global POPUP_INTERVAL_MINUTES
    POPUP_INTERVAL_MINUTES = interval
    root = tk.Tk()
    app = ActivityApp(root, interval)
    # start idle monitor
    idle_thread = threading.Thread(target=monitor_idle, args=(app,), daemon=True)
    idle_thread.start()
    root.mainloop()


def main() -> None:
    init_db()
    init_mongo()
    # login
    login = tk.Tk()
    login.title("Login")
    sw, sh = login.winfo_screenwidth(), login.winfo_screenheight()
    lx, ly = (sw // 2 - 220), (sh // 2 - 120)
    login.geometry(f"440x240+{lx}+{ly}")
    tk.Label(login, text="Employee ID:", font=("Segoe UI", 11)).pack(pady=(14, 6))
    emp = tk.Entry(login, font=("Segoe UI", 11))
    emp.pack()
    tk.Label(login, text="Popup Interval (mins):", font=("Segoe UI", 11)).pack(pady=(10, 4))
    iv = StringVar(login)
    iv.set(str(POPUP_INTERVAL_MINUTES))
    combo = ttk.Combobox(login, values=["15", "30", "60"], textvariable=iv, state="readonly")
    combo.pack()

    def do_start() -> None:
        val = emp.get().strip()
        if not val:
            messagebox.showerror("Error", "Employee ID required", parent=login)
            return
        USER_EMP_ID[0] = val
        try:
            interval = int(iv.get())
        except Exception:
            interval = 30
        login.destroy()
        start_main_ui(interval)

    tk.Button(login, text="Start", command=do_start).pack(pady=12)
    login.mainloop()


if __name__ == "__main__":
    main()
